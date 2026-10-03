"""The TRMNL-BYOS server the board talks to.

Contract: HARDWARE.md 9.2. Four device endpoints -- /api/display, /api/image,
/api/log and /api/firmware -- telemetry carried in request headers, and the
image fetched separately with a conditional GET. Every one of them wants a
bearer token; there is no enrolment endpoint any more, because the operator
creates the device with the CLI (`python -m app.cli device add`) and hands
it its token.

The whole design is built around one number from 9.5: a wake where nothing
changed and no repaint happens costs about a fifth of a full wake. So the
server's job is not "produce an image", it is "decide whether the panel needs
to move at all", and every path here is arranged to make the answer "no" as
cheap as possible:

  * the ETag is a hash of the *packed panel bytes*, so a re-render that lands
    on identical pixels still yields 304;
  * `filename` only changes when those bytes change, so the board can skip
    even the image request;
  * Cache-Control is `private, no-store` — we want a revalidation from the
    board and nothing at all from anything in between. It used to be
    `no-cache`, which is the right instruction for a cache and the wrong one
    here: the path ends in `.png`, Cloudflare caches by file extension, and an
    edge copy of a frame carrying guests' names would sit in front of every
    bearer check.

Two failure modes are explicitly not allowed here. A missing source must not
turn into a 500 (the board would sleep with a blank screen), and a request
without telemetry must not turn into a KeyError (a factory-fresh board sends
almost no headers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import numpy as np
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont

from . import assemble
from .config import settings
from .models import Dashboard, Telemetry
from .render import encode, palette
from .schedule import next_slot, seconds_until_next, should_repaint
from . import auth as device_auth
from .store import DbUnavailable, DeviceRecord, FirmwareRecord, Store, normalize_mac

log = logging.getLogger("inkwake")

#: Frames kept on disk. The board may still ask for the frame it was told
#: about one cycle ago, and a handful of 10 kB PNGs is not worth a cleanup
#: policy beyond "keep the last dozen".
FRAME_RETENTION = 12

#: Filenames we are willing to look up. The name comes back from the device,
#: so it is untrusted input pointed straight at a filesystem path.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: Where a renderer might live. The layout module is written separately; the
#: server must start and serve a (plain) frame before it exists.
_RENDERERS: tuple[tuple[str, str], ...] = (
    (".render.layout", "render_dashboard"),
    (".render.frame", "render_dashboard"),
    (".render.layout", "render"),
    (".render", "render_dashboard"),
)


# --------------------------------------------------------------------------
# Telemetry headers
# --------------------------------------------------------------------------

#: HTTP header names are case-insensitive, but "_" and "-" are genuinely
#: different characters and both spellings are in the wild (9.2). Listing both
#: is cheaper than being wrong about which firmware we are talking to.
_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("ID", "Device-Id", "DEVICE_ID", "MAC"),
    "access_token": ("ACCESS_TOKEN", "Access-Token"),
    "battery_voltage": ("BATTERY_VOLTAGE", "Battery-Voltage"),
    "percent_charged": ("PERCENT_CHARGED", "Percent-Charged"),
    "battery_charging": ("BATTERY_CHARGING", "Battery-Charging", "USB_CONNECTED"),
    "rssi": ("RSSI", "Rssi", "WIFI_RSSI"),
    "fw_version": ("FW_VERSION", "FW-Version", "Firmware-Version"),
    "width": ("WIDTH",),
    "height": ("HEIGHT",),
    "model": ("MODEL",),
    "refresh_rate": ("REFRESH_RATE", "Refresh-Rate"),
    "wake_time": ("WAKE_TIME", "Wake-Time"),
    "temperature": ("TEMPERATURE", "TEMPERATURE_C", "Temperature"),
    "humidity": ("HUMIDITY", "HUMIDITY_PCT", "Humidity"),
    "wake_reason": ("WAKE_REASON", "Wake-Reason", "WAKEUP_REASON"),
}


@dataclass(frozen=True)
class DeviceHeaders:
    """The parsed request headers, split into contract and extras.

    Telemetry is the frozen contract from models.py; everything else the board
    sends that has no home there (model, wake time, the token) rides alongside
    rather than being dropped.
    """

    telemetry: Telemetry
    access_token: Optional[str] = None
    model: Optional[str] = None
    wake_time: Optional[str] = None
    device_refresh_rate: Optional[int] = None

    @property
    def mac(self) -> str:
        return self.telemetry.device_id


def _raw(headers: Any, field: str) -> Optional[str]:
    for name in _ALIASES[field]:
        value = headers.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _as_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    # Values arrive with units attached often enough ("-67 dBm", "3.9V") that
    # pulling the leading number out is worth more than rejecting the header.
    match = re.match(r"[-+]?\d*\.?\d+", raw.replace(",", "."))
    try:
        return float(match.group()) if match else None
    except ValueError:
        return None


def _as_int(raw: Optional[str]) -> Optional[int]:
    value = _as_float(raw)
    return None if value is None else int(round(value))


def _as_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on", "charging"}


def _voltage_mv(raw: Optional[str]) -> Optional[int]:
    """Accept both millivolts and volts.

    TRMNL firmware reports volts ("4.05"); our own reports millivolts. The
    cell never sits below 100 mV — it hard-shuts down at 3100 — so anything
    under 100 can only be a volt reading, and guessing wrong here would trip
    the low-battery gate on a full battery.
    """
    value = _as_float(raw)
    if value is None:
        return None
    mv = int(round(value * 1000)) if value < 100 else int(round(value))
    return mv if 0 < mv < 20000 else None


def parse_headers(headers: Any) -> DeviceHeaders:
    """Read telemetry out of the request headers. Never raises."""
    percent = _as_int(_raw(headers, "percent_charged"))
    if percent is not None:
        percent = max(0, min(100, percent))

    telemetry = Telemetry(
        device_id=normalize_mac(_raw(headers, "id") or ""),
        battery_voltage_mv=_voltage_mv(_raw(headers, "battery_voltage")),
        battery_percent=percent,
        is_charging=_as_bool(_raw(headers, "battery_charging")),
        temperature_c=_as_float(_raw(headers, "temperature")),
        humidity_pct=_as_float(_raw(headers, "humidity")),
        rssi_dbm=_as_int(_raw(headers, "rssi")),
        fw_version=_raw(headers, "fw_version"),
        wake_reason=_raw(headers, "wake_reason"),
        width=_as_int(_raw(headers, "width")),
        height=_as_int(_raw(headers, "height")),
    )
    return DeviceHeaders(
        telemetry=telemetry,
        access_token=_raw(headers, "access_token"),
        model=_raw(headers, "model"),
        wake_time=_raw(headers, "wake_time"),
        device_refresh_rate=_as_int(_raw(headers, "refresh_rate")),
    )


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One finished frame, in every form somebody asks for it.

    `key` is the bare hash and `name` is `key + ".png"`, which is what lets
    /api/image answer a conditional GET for a frame this process never
    rendered: the validator is derivable from the filename, so nothing has to
    survive a restart.
    """

    key: str           # bare sha256 hex of the packed bytes
    png: bytes         # indexed 4 bpp, what the board downloads
    packed: bytes      # 120 000 B at 400x600, the raw wire format
    built_at: datetime
    dashboard: Dashboard

    @property
    def name(self) -> str:
        return f"{self.key}.png"

    @property
    def etag(self) -> str:
        """The full ETag header value, quotes included."""
        return f'"{self.key}"'


_store: Optional[Store] = None
_frame: Optional[Frame] = None
_render_lock = threading.Lock()

#: The live prewarm task, or None. Module-level purely so a test can assert
#: the shutdown ordering: cancelling is observable only from inside
#: assemble.shutdown(), because asyncio.run cancels every pending task on its
#: way out and would make the naive check pass either way.
_prewarm_task_for_test: "Optional[asyncio.Task[None]]" = None


def get_store() -> Store:
    """The store, built on first use rather than at import.

    Import time is too early: the DSN is read from the environment, and a test
    that re-points settings must be able to do so before anything connects.
    """
    global _store
    if _store is None:
        _store = Store(settings.db_path)
    return _store


def set_store(store: Optional[Store]) -> None:
    """Swap the store. The seam the tests use to run without a database.

    `tests/fakes.py` puts an in-memory implementation here. It is a genuine
    second implementation of the same nine methods rather than a mock: the
    COALESCE merge *is* the SQL, and a fake that simply returned rows would let
    a broken UPDATE pass unnoticed. The same test bodies run against the real
    SQLite store as well (tests/test_store.py).
    """
    global _store
    _store = store


def reset_state() -> None:
    """Drop every cached handle. For tests that re-point settings.data_dir."""
    global _store, _frame
    _store = None
    _frame = None
    assemble.clear_cache()
    # shutdown() latches the source pool closed, and a lifespan ends on every
    # TestClient exit. Without unlatching here the next test in the file would
    # find a permanently dead pool - which is the kind of order-dependent leak
    # pytest-randomly exists to surface.
    assemble.reopen()


def current_frame() -> Optional[Frame]:
    return _frame


def _resolve_renderer() -> Optional[Callable[[Dashboard], Image.Image]]:
    import importlib

    for module_name, attr in _RENDERERS:
        try:
            module = importlib.import_module(module_name, package=__package__)
        except Exception:  # noqa: BLE001 - a broken renderer is not fatal
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    return None


def _placeholder(dash: Dashboard) -> Image.Image:
    """A legible frame for when the layout module is absent or broke.

    Deliberately ugly and deliberately black-on-white: the point is that a
    fresh checkout, or a renderer that raised at 07:00, still puts something
    truthful on the panel instead of leaving yesterday up with no explanation.
    """
    img = Image.new("RGB", (settings.panel_width, settings.panel_height), palette.WHITE)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    lines = [
        "inkwake",
        dash.now.strftime("%a %d.%m.%Y  %H:%M"),
        "",
        "no layout module - placeholder frame",
        "",
        f"weather: {dash.weather.description if dash.weather else '-'}",
        f"sun: {dash.sun.sunrise.strftime('%H:%M') if dash.sun else '-'}"
        f" / {dash.sun.sunset.strftime('%H:%M') if dash.sun else '-'}",
        f"bookings: {len(dash.bookings)}",
        f"events: {len(dash.events)}",
        f"sighting: {dash.sighting.caption[:40] if dash.sighting else '-'}",
        "",
        f"next: {dash.next_refresh.strftime('%d.%m. %H:%M')}",
        f"failures: {', '.join(dash.failures) if dash.failures else 'none'}",
    ]
    y = 12
    for line in lines:
        draw.text((12, y), line, font=font, fill=palette.BLACK)
        y += 16
    return img


def _normalise(img: Image.Image) -> np.ndarray:
    """Native geometry, exact palette. The gate in front of both encoders.

    Two things happen here that the renderer is allowed to be sloppy about.
    A landscape image is rotated to the native 400x600 portrait (9.4), and an
    image that is not already palette-exact is snapped to the nearest ink in
    Lab.

    The snap is a safety net, not a feature. encode_* would rather raise than
    round, and that is right for the layout module's own tests; here, at
    07:00, with a board waiting, an approximately-correct frame beats no
    frame. It is logged loudly because an off-palette pixel means some drawing
    code is bypassing DEVICE_RGB.
    """
    rgb = img.convert("RGB")
    want = (settings.panel_width, settings.panel_height)
    if rgb.size == (want[1], want[0]):
        rgb = rgb.rotate(90, expand=True)
    if rgb.size != want:
        log.warning("renderer produced %s, resizing to %s", rgb.size, want)
        rgb = rgb.resize(want, Image.Resampling.LANCZOS)

    arr = np.asarray(rgb, dtype=np.uint8)
    try:
        palette.assert_palette_exact(arr)
        return arr
    except palette.PaletteError as exc:
        log.warning("frame was not palette-exact (%s); snapping to nearest ink", exc)
        indices = palette.nearest_ink(palette.srgb_to_lab(arr))
        return palette.DEVICE_LUT[indices]


def _held_names() -> set[str]:
    """Frames some panel is currently showing. Never prune these."""
    devices = _record(lambda: get_store().list_devices(), "devices") or []
    return {f"{d.last_etag}.png" for d in devices if d.last_etag}


def _prune_frames(keep: int = FRAME_RETENTION) -> None:
    """Drop old frames, but never the one a panel is holding.

    Each /preview renders a frame of its own, so a few minutes of clicking
    around the status page can push a device's frame off the end of the list.
    Deleting it costs a full repaint the next wake - the exact expense the
    filename cache exists to avoid - so it is worth one SELECT to avoid.
    """
    try:
        protected = _held_names()
        files = sorted(
            settings.image_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for path in files[keep:]:
            if path.name not in protected:
                path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("frame prune failed: %s", exc)


def render_frame(
    telemetry: Telemetry,
    now: datetime,
    *,
    slots: Optional[tuple[tuple[int, int], ...]] = None,
    budget_s: Optional[float] = None,
) -> Frame:
    """Assemble, render, quantise, pack, publish. Synchronous; call in a thread.

    The lock is not about correctness of the file on disk (the name is the
    content hash, so two renders of the same content write the same bytes) but
    about cost: two wakes arriving together should not scrape every source
    twice.

    `slots` belongs to the device this frame is for and only decides the
    printed "nächste Aktualisierung". Two devices with different wake times
    therefore render different bytes and get different hashes, which is the
    honest outcome: they are looking at different frames.
    """
    global _frame
    with _render_lock:
        dash = assemble.build_dashboard(telemetry, now, slots=slots, budget_s=budget_s)

        renderer = _resolve_renderer()
        img: Optional[Image.Image] = None
        if renderer is not None:
            try:
                img = renderer(dash)
            except Exception as exc:  # noqa: BLE001 - never lose the frame
                log.exception("renderer failed: %s", exc)
                if "render" not in dash.failures:
                    dash.failures.append("render")
        if img is None:
            img = _placeholder(dash)

        arr = _normalise(img)
        packed = encode.encode_raw(arr)
        png = encode.encode_png(arr)
        # The ETag is over the packed panel bytes, never over the PNG: a
        # Pillow upgrade must not move the hash of an unchanged picture.
        key = encode.frame_etag(packed).strip('"')

        settings.image_dir.mkdir(parents=True, exist_ok=True)
        path = settings.image_dir / f"{key}.png"
        try:
            if not path.exists():
                path.write_bytes(png)
            else:
                path.touch()
            _prune_frames()
        except OSError as exc:
            # An unwritable volume still leaves the in-memory frame usable for
            # this response; /api/image falls back to it.
            log.warning("could not write %s: %s", path, exc)

        _frame = Frame(
            key=key,
            png=png,
            packed=packed,
            built_at=now,
            dashboard=dash,
        )
        return _frame


async def render_frame_async(
    telemetry: Telemetry,
    now: datetime,
    *,
    slots: Optional[tuple[tuple[int, int], ...]] = None,
    budget_s: Optional[float] = None,
) -> Frame:
    return await asyncio.to_thread(
        render_frame, telemetry, now, slots=slots, budget_s=budget_s
    )


def _prewarm_sources() -> None:
    """Fill the source cache so a wake never pays for an upstream round trip.

    assemble.prewarm and not build_dashboard: prewarm ignores the cache age,
    so it actually refreshes. Going through build_dashboard would skip every
    entry that had not expired yet, and a prewarm could then only refill a
    hole it happened to land in.

    Nothing is published either. A frame from a background task would move
    `_frame`, write a PNG and run the pruner, and the pruner protects only
    frames a device is currently holding - what is on the glass is the device
    request's business.

    Deliberately NOT under _render_lock. Sharing it would serialise the
    prewarm against real device requests, and the prewarm can hold it for the
    whole source budget - long enough to blow the board's 8 s header timeout
    and cost it the slot. The lock costs a picture; running the two
    concurrently costs some duplicated fetching, and the source pool is sized
    for both collections at once so the duplication does not turn into
    queueing (see assemble._pool).

    Nothing here can weaken a panel-care rule even by accident: `assemble`
    imports only config, models and schedule, so it cannot reach the store and
    cannot touch `last_painted_at`, `last_etag` or the persisted telemetry
    that the 180 s gate and the 15 C gate read.
    """
    failures = assemble.prewarm(_now())
    if failures:
        log.debug("source prewarm: %s did not answer", ", ".join(failures))


def _prewarm_slots() -> tuple[tuple[int, int], ...]:
    """Every wake time any active device has, merged into one list.

    The prewarm used to read `settings.refresh_slots`, while the boards get
    their countdown from their registry rows. Both lists happened to hold the
    same three times, so nothing showed - but the moment the operator moved a
    wake time with the CLI (which is exactly where that decision belongs), the
    prewarm would have gone on firing at the old times: every wake back to
    paying a full round trip with its radio up at ~120 mA, and not one thing
    anywhere turning red. The rows are the truth, so the rows are what this
    reads.

    The union, not one device's list: the cache is process-global, so warming
    it early enough for the earliest waker covers the rest for free. The CHECK
    caps a device at 8 slots, so two boards can push this to 16 fetch rounds a
    day against every calendar feed instead of three. That is still far from the ~48 a
    plain interval would cost (see Settings.prewarm_lead_s), and it only
    happens if someone deliberately gives the two boards disjoint schedules.

    Falls back to the configured default whenever the database cannot answer
    or has nothing active - a cold cache is a worse answer than a slightly
    misaimed warm one.
    """
    try:
        devices = get_store().list_devices()
    except Exception as exc:  # noqa: BLE001 - deliberately total, like _record
        log.warning(
            "prewarm cannot read the device rows (%s: %s), using the default slots",
            type(exc).__name__,
            exc,
        )
        return settings.refresh_slots

    # `active` is filtered here and not in SQL: list_devices() carries no
    # WHERE, and the CLI's own list wants the inactive rows too.
    minutes = {m for d in devices if d.active for m in d.slots}
    if not minutes:
        return settings.refresh_slots
    return tuple(divmod(int(m), 60) for m in sorted(minutes))


async def _prewarm_loop(lead_s: int) -> None:
    """Re-fetch the sources shortly before each refresh slot, until cancelled.

    Slot-aligned rather than periodic, so the upstreams see three extra fetch
    rounds a day instead of dozens - see Settings.prewarm_lead_s.

    The slot list is re-read every pass, not captured once: a wake time
    changed with the CLI has to take effect without restarting the container,
    and the loop sleeps for hours at a time. It is read off the event loop --
    see the comment at the call.

    A failure is a non-event: the cache simply stays as cold as it is today
    and the next wake pays what it pays now. So this never stops on an error,
    and never lets one escape into the event loop.
    """
    while True:
        try:
            now = _now()
            try:
                # to_thread, like the prewarm itself below: _prewarm_slots
                # opens the SQLite registry, and sqlite3 is synchronous. On the
                # loop it would stall EVERY request for up to the busy timeout
                # (5 s, store.py) whenever the CLI holds a lock -- and this loop
                # resumes about 30 s after each slot, which is exactly when the
                # boards are polling against an 8 s header budget.
                slots = await asyncio.to_thread(_prewarm_slots)
                target = next_slot(now, slots, settings.timezone)
            except (ValueError, RuntimeError) as exc:
                # A misconfigured slot list must not spin this loop.
                log.warning("prewarm cannot find the next slot: %s", exc)
                await asyncio.sleep(3600)
                continue

            wait_s = (target - now).total_seconds() - lead_s
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            await asyncio.to_thread(_prewarm_sources)
            log.debug("source prewarm done, slot at %s", target.isoformat())

            # Sleep past the slot before looking again, or next_slot would
            # still return this one and the loop would warm in a tight circle
            # for the whole lead window.
            remaining = (target - _now()).total_seconds() + 30.0
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a cold cache is the fallback
            log.warning("source prewarm failed: %s: %s", type(exc).__name__, exc)
            # Never spin: an immediate retry on a persistent fault would be a
            # busy loop against the upstreams this feature is meant to spare.
            await asyncio.sleep(60)


def _image_url(name: str) -> str:
    return f"{settings.public_base_url}/api/image/{name}"


def _firmware_url(firmware_id: str) -> str:
    """Absolute, because net.cpp does not run this one through absolutise()."""
    return f"{settings.public_base_url}/api/firmware/{firmware_id}"


def _frame_path(name: str) -> Optional[Path]:
    if not _SAFE_NAME.match(name):
        return None
    path = settings.image_dir / name
    return path if path.is_file() else None


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


class _SkipHealthzAccessLog(logging.Filter):
    """Keep the container probe out of the access log.

    The healthcheck runs every 30 s for as long as the container lives, so
    without this the log is almost entirely probe and the lines that matter --
    three board wakes a day -- scroll out of reach. uvicorn passes the request
    path as args[2]; any other record shape is one we do not recognise and must
    pass through rather than risk dropping something real.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (
            isinstance(args, tuple) and len(args) >= 3 and args[2] == "/healthz"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Guarded because uvicorn.access is process-global and lifespan runs per
    # app instance: a TestClient per test would otherwise stack hundreds of
    # identical filters onto one logger, and every access record would then be
    # run through all of them. Exactly the cross-test state leak pytest-randomly
    # is here to catch.
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _SkipHealthzAccessLog) for f in access_log.filters):
        access_log.addFilter(_SkipHealthzAccessLog())
    # httpx logs every request URL at INFO. A private calendar's ICS link
    # carries its secret in the URL, so that line would put a credential into
    # the container log on every wake.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings.image_dir.mkdir(parents=True, exist_ok=True)
    get_store()
    # shutdown() latches the source pool closed so an abandoned worker cannot
    # build itself a replacement on the way out. A second app in the same
    # process - the test suite, a reload - has to unlatch it.
    assemble.reopen()

    if not settings.admin_api_key:
        # Said once, at startup, because the consequence is invisible from the
        # outside: /preview renders a frame on demand, and a frame can carry
        # booking names. On a laptop this is fine. On a reachable host it is a
        # leak that looks exactly like a working service.
        log.warning(
            "ADMIN_API_KEY is empty: /preview and /api/internal/status "
            "are unguarded. Fine locally; set one before deploying."
        )
    try:
        get_store().migrate()
    except DbUnavailable as exc:
        # Louder than the above, because without the registry nothing works:
        # no device can authenticate and no telemetry is stored.
        log.error("device registry unavailable (%s): no device can authenticate", exc)
    else:
        if not _record(lambda: get_store().list_devices(), "devices"):
            log.warning(
                "no devices registered yet. Create one with: "
                "python -m app.cli device add --label <name>"
            )

    if settings.warm_on_start:
        # A cold container answering the first wake would make the board wait
        # for five upstreams. Pay that here instead, and never let it stop
        # the server from binding.
        try:
            await render_frame_async(Telemetry(device_id=""), _now())
        except Exception as exc:  # noqa: BLE001
            log.warning("warm-up render failed: %s", exc)

    global _prewarm_task_for_test
    prewarm: Optional[asyncio.Task[None]] = None
    if settings.prewarm_lead_s > 0:
        if settings.prewarm_lead_s >= settings.source_cache_ttl_s:
            # Not fatal, but it makes the task pointless: what it fetched
            # would have expired again by the time the board calls.
            log.warning(
                "PREWARM_LEAD_S (%d) is not below SOURCE_CACHE_TTL_S (%d); "
                "the cache will be cold again by the time it is used",
                settings.prewarm_lead_s,
                settings.source_cache_ttl_s,
            )
        prewarm = asyncio.create_task(_prewarm_loop(settings.prewarm_lead_s))
    _prewarm_task_for_test = prewarm

    try:
        yield
    finally:
        # Cancel first, but do not mistake this for stopping the work:
        # asyncio.to_thread is not cancellable, so a prewarm already inside
        # assemble.prewarm keeps running in its worker. What actually contains
        # that thread is assemble.shutdown() latching the pool closed - the
        # abandoned worker then raises ShuttingDown out of _pool() instead of
        # quietly building a replacement executor that nobody would ever close.
        # Cancelling here only stops the *next* tick from being scheduled.
        if prewarm is not None:
            prewarm.cancel()
            try:
                await prewarm
            except asyncio.CancelledError:
                pass
        assemble.shutdown()


app = FastAPI(title="inkwake", lifespan=lifespan, docs_url=None, redoc_url=None)


def _now() -> datetime:
    return datetime.now(settings.timezone)


def _countdown(
    now: datetime,
    *,
    jitter_s: int,
    slots: Optional[tuple[tuple[int, int], ...]] = None,
) -> tuple[int, datetime]:
    """Seconds until the next slot, and the slot.

    `now` is handed over as UTC on purpose, and this is not cosmetic.
    Subtracting two aware datetimes that carry the *same* tzinfo object makes
    Python ignore the zone and subtract the wall-clock fields, so across a DST
    transition the countdown comes out an hour wrong - 10.5 h on the March
    night that is really 9.5 h long. Different tzinfo objects force the
    comparison through UTC, which is the answer the board needs: it has no
    timezone database and simply sleeps the number it is given.
    """
    return seconds_until_next(
        now.astimezone(timezone.utc),
        slots or settings.refresh_slots,
        settings.timezone,
        jitter_s=jitter_s,
    )


def _record(fn: Callable[[], Any], what: str) -> Any:
    """Run a store write that must never cost the device its response."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("store write (%s) failed: %s", what, exc)
        return None


# -- authorisation ----------------------------------------------------------
#
# A frame can carry personal data -- the names in the bookings panel -- so the
# endpoints that serve or render one are the endpoints that matter here.
#
# There is no open call left. The pairing endpoint used to be deliberately
# open -- that was how a factory-fresh board got its key at all -- and it is
# gone, along with the ALLOWED_DEVICES list that tried to make it safe. A
# device is created with the CLI, told its token once, and every request it
# makes carries it. Not a hardening pass: the old arrangement let an unknown
# MAC through, and /healthz published the frame name, so anyone could fetch
# that PNG from a public host.


def _unauthorised(reason: str) -> JSONResponse:
    """401 with a short reason. Never echoes the token that was offered."""
    return JSONResponse({"status": 401, "message": reason}, status_code=401)


def _forbidden(reason: str) -> JSONResponse:
    """403. The device is who it says it is; the operator switched it off."""
    return JSONResponse({"status": 403, "message": reason}, status_code=403)


def _unavailable(reason: str) -> JSONResponse:
    """503, and never 401, when the store cannot answer.

    The distinction is the whole reason `DbUnavailable` exists. A 401 tells a
    future firmware that its identity is broken and, once the provisioning
    ladder is in place, sends a perfectly good board into setup mode -- over a
    locked or unreadable registry. A 503 is just "no content this wake", which the retry
    ladder already handles.
    """
    return JSONResponse({"status": 503, "message": reason}, status_code=503)


def _authorise(request: Request) -> tuple[Optional[DeviceRecord], Optional[JSONResponse]]:
    """Resolve the bearer token to a device. Returns (device, error response).

    What this replaced is worth remembering. The old check hung on
    `paired_at` and let an **unknown MAC through**, on the reasoning that
    demanding a token from a row the board had never been told about would lock
    out the real device. That was true while the server minted keys itself; it
    stopped being true the moment the operator creates the row and hands over
    the token. What remained was an open door -- and, chained with the frame
    name that /healthz used to publish, a way for anyone to pull a PNG carrying
    guests' names off a public host.
    """
    token = device_auth.bearer_from_headers(request.headers)
    try:
        result = device_auth.authenticate(get_store(), token)
    except DbUnavailable as exc:
        log.warning("device lookup unavailable: %s", exc)
        return None, _unavailable("store unavailable")

    if result.ok:
        return result.device, None
    if result.reason is device_auth.Reason.DISABLED:
        log.warning("device %s is disabled", result.device.id if result.device else "?")
        return None, _forbidden("device disabled")
    return None, _unauthorised("bad or missing device token")


def _authorise_internal(request: Request) -> Optional[JSONResponse]:
    """Guard the operator endpoints (/preview, /api/internal/status).

    The key comes as `X-Admin-Key` or `Authorization: Bearer`. No `?token=`:
    a credential in a URL ends up in every access log and every browser
    history, guarding a page that can render personal data.
    """
    expected = settings.admin_api_key
    if not expected:
        return None

    offered = request.headers.get("X-Admin-Key") or ""
    if not offered:
        header = request.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            offered = header[7:].strip()

    if not secrets.compare_digest(offered, expected):
        return _unauthorised("admin key required")
    return None


# -- /api/display -----------------------------------------------------------


def _battery_gate(device: Optional[DeviceRecord], telemetry: Telemetry) -> tuple[bool, Optional[int]]:
    """Should this wake skip content entirely? Returns (gate, millivolts).

    Hysteresis, not a bare threshold: a cell that reads 3299 mV under WiFi
    load would otherwise flap in and out of the charge-me state every wake,
    and each flap costs a full refresh. Once gated, recovery needs
    battery_recover_mv (9.6).
    """
    mv = telemetry.battery_voltage_mv
    if mv is None and device is not None:
        mv = device.battery_voltage_mv
    if mv is None:
        return False, None

    # Only this wake's answer counts. The stored flag is carried forward by
    # COALESCE and has no age, so a board last seen on a cable whose PMIC then
    # stops answering would look like it is charging for the rest of its life
    # -- and the gate that exists to stop repainting a draining cell would
    # never close again. "We do not know" has to mean "not charging" here, the
    # same way it does in the firmware's own gate.
    if telemetry.is_charging:
        return False, mv

    was_low = bool(device is not None and device.low_battery)
    # From the row, not from the environment: these are per-device, and the
    # operator sets them with the CLI. The settings values remain only as the
    # defaults for a render without a device.
    low = device.battery_low_mv if device is not None else settings.battery_low_mv
    recover = device.battery_recover_mv if device is not None else settings.battery_recover_mv
    return mv < (recover if was_low else low), mv


def _safe_filename(raw: str) -> str:
    """Only what belongs in a Content-Disposition, nothing that closes it."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (raw or "firmware.bin").strip())
    return cleaned[:100] or "firmware.bin"


def _firmware_path(relative: str) -> Optional[Path]:
    """Resolve a registered firmware file, or None if it does not stay put.

    The value comes from our own registry, but it is still treated as
    untrusted here -- the same shape of check as `_resolve_image` in
    sources/database.py, including catching ValueError, which is what a stored
    path with an embedded NUL raises.
    """
    root = Path(settings.firmware_dir)
    try:
        candidate = (root / (relative or "").replace("\\", "/").lstrip("/")).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _shorten_ip(raw: Optional[str]) -> Optional[str]:
    """IPv4 to /24, IPv6 to /48. Never the full address.

    The address that arrives is a proxy's or the home connection's public one,
    never the device's address on the LAN. Storing it whole would be a dated,
    provider-resolvable record of somebody's connection for no diagnostic gain:
    what the column is actually good for is "which way is it coming in".

    IPv6 is expanded before it is cut. The naive version splits on ":" and keeps
    three groups, which for `fd00::abcd:1` stores interface bits as if they were
    the network.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if "." in value and ":" not in value:
        parts = value.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3]) + ".0/24"
        return None
    if ":" in value:
        try:
            import ipaddress

            addr = ipaddress.IPv6Address(value)
        except ValueError:
            return None
        return str(ipaddress.IPv6Network((addr, 48), strict=False))
    return None


def _client_ip(request: Request) -> Optional[str]:
    """The caller's address, already shortened. Never the raw value."""
    header = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for"
    )
    if header:
        first = header.split(",")[0].strip()
    else:
        first = request.client.host if request.client else ""
    return _shorten_ip(first)


def _device_slots(device: Optional[DeviceRecord]) -> tuple[tuple[int, int], ...]:
    """The row's wake times as (hour, minute), the shape schedule.py wants.

    Stored as minutes since midnight because that is what a CHECK can express
    and what the CLI round-trips without ambiguity.
    """
    if device is None or not device.slots:
        return settings.refresh_slots
    return tuple(divmod(int(m), 60) for m in sorted(set(device.slots)))


def _burn_in_refresh_due(device: DeviceRecord, now: datetime) -> bool:
    """Is the panel past the point where rule 2 forces a repaint?

    Deliberately not derived from should_repaint(): that needs a rendered frame,
    and this decision has to be made before the render to be worth anything. The
    two must agree on the threshold, which is why both read the same column --
    and on the clock, which is why both use the later of "told it to paint" and
    "it fetched the bytes".

    A device that has never been painted for counts as due. It has nothing on the
    glass, so there is nothing to protect and everything to show.
    """
    last = device.painted_or_fetched_at
    if last is None:
        return True
    return (now - last).total_seconds() >= device.max_image_age_s


def _held_frame_name(device: Optional[DeviceRecord]) -> Optional[str]:
    """The frame this board is currently showing, if we can still serve it.

    Not the same thing as current_frame(): a render whose repaint was refused
    (too soon, too cold) still becomes the server's current frame while the
    panel keeps the older picture.
    """
    if device is None or not device.last_etag:
        return None
    name = f"{device.last_etag}.png"
    return name if _frame_path(name) else None


def _display_payload(
    *,
    filename: str,
    image_url: str,
    refresh_rate: int,
    firmware: Optional[FirmwareRecord] = None,
) -> dict[str, Any]:
    """The BYOS display response, verbatim field set from 9.2, plus two.

    `firmware_sha256` and `firmware_size` are **not** BYOS. They are here
    because the board has to know both before it opens the stream: it checks the
    digest before it writes the OTA partition, it has neither Range nor resume,
    and `Update.begin()` needs the length up front. The same consideration put
    them in the poll response on the sibling device rather than in a header --
    a header would arrive only once the download had already started.

    Flat siblings rather than a nested `firmware` object, because everything
    else in this response is flat BYOS and the firmware already reads
    `doc["firmware_url"]` and `doc["update_firmware"]` from the top level.
    """
    payload: dict[str, Any] = {
        "filename": filename,
        "firmware_url": None,
        "firmware_version": None,
        "firmware_sha256": None,
        "firmware_size": None,
        "image_url": image_url,
        "image_url_timeout": 0,
        "maximum_compatibility": False,
        "refresh_rate": refresh_rate,
        "reset_firmware": False,
        "special_function": "none",
        "temperature_profile": "default",
        "touchbar_mode": "tap",
        "update_firmware": False,
    }
    if firmware is not None:
        payload.update(
            {
                # Absolute, because net.cpp runs only `image_url` through
                # absolutise() -- a relative firmware_url would be resolved
                # against nothing and the download would never start.
                "firmware_url": _firmware_url(firmware.id),
                "firmware_version": firmware.version,
                "firmware_sha256": firmware.sha256,
                "firmware_size": firmware.size_bytes,
                "update_firmware": True,
            }
        )
    return payload


def _firmware_offer(
    device: Optional[DeviceRecord],
    reported_version: Optional[str],
    battery_percent: Optional[int],
) -> Optional[FirmwareRecord]:
    """The image to offer this wake, or None. Five conditions, all required.

    1. An image is assigned to *this* device. There is deliberately no "newest
       for everyone": a staged rollout is the only kind this project does.
    2. The device reported its running version **in this request**. Not the
       stored value -- COALESCE carries that forward, so after a successful
       update it would keep saying the old number for as long as the new
       firmware omits the header, and the server would reissue the same job
       every wake. "Unknown" does not count as "out of date".
    3. The reported version differs from the assigned one.
    4. The image is for this device class, checked here and again where the
       bytes go out.
    5. The cell can afford it: an update is 1.35 MB over WiFi at ~120 mA plus
       a flash write, on a battery-powered board. A charge reported in this
       request below `ota_min_battery_pct` holds the offer back.

    A wake that reports NO charge is not held back, and that is on purpose.
    The firmware omits both battery headers exactly when every PMIC read
    failed (power.cpp returns 0, net.cpp then sends nothing), and it applies
    the same rule to its own gate: unknown is not empty (main.cpp, `charged`).
    Refusing here would make a board with a broken battery read impossible to
    update -- possibly with the very image that repairs the read -- while a
    board that does know its voltage still refuses a flat cell by itself.

    "Differs", not "newer": versions are compared as strings and never
    ordered, so assigning an older image is a downgrade the board will take.
    """
    if device is None or not device.firmware_id:
        return None
    if not reported_version:
        # Worth a line in the log: from the outside this is indistinguishable
        # from "no update available", and the operator has just assigned one.
        log.warning(
            "device %s has firmware %s assigned but reported no version",
            device.id,
            device.firmware_id,
        )
        return None
    try:
        firmware = get_store().get_firmware(device.firmware_id)
    except DbUnavailable as exc:
        log.warning("firmware lookup unavailable: %s", exc)
        return None
    if firmware is None or firmware.target != "eink":
        if firmware is not None:
            log.error(
                "device %s is assigned %s, which targets %s",
                device.id,
                firmware.id,
                firmware.target,
            )
        return None
    if firmware.version == reported_version:
        return None
    if battery_percent is not None and battery_percent < device.ota_min_battery_pct:
        log.info(
            "update %s held back for %s: %s%% charge, needs %s%%",
            firmware.version,
            device.id,
            battery_percent,
            device.ota_min_battery_pct,
        )
        return None
    return firmware


@app.get("/api/display")
async def api_display(request: Request) -> JSONResponse:
    now = _now()
    device, denied = _authorise(request)
    if denied is not None:
        return denied
    assert device is not None  # _authorise returns one or the other
    store = get_store()
    dh = parse_headers(request.headers)

    try:
        merged = store.record_telemetry(
            device.id, dh.telemetry, now, model=dh.model, ip=_client_ip(request)
        )
    except DbUnavailable as exc:
        log.warning("telemetry write unavailable: %s", exc)
        return _unavailable("store unavailable")
    if merged is None:
        # Authorised a moment ago and gone now: the operator deleted the row
        # between the lookup and this write. 401 is the honest answer -- the
        # token no longer belongs to anything.
        return _unauthorised("device no longer exists")
    device = merged

    slots = _device_slots(device)
    refresh_rate, target = _countdown(now, jitter_s=device.refresh_jitter_s, slots=slots)

    gated, mv = _battery_gate(device, dh.telemetry)
    if bool(device.low_battery) != gated:
        _record(lambda: store.set_low_battery(device.id, gated, now), "low_battery")

    if gated:
        # No sources, no render, no repaint: a board that is nearly flat must
        # not spend what is left telling us it is nearly flat.
        #
        # The name we hand back must be the one the board is *holding*, not the
        # newest frame the server happens to have rendered for someone else.
        # Those differ (a render whose repaint was refused still becomes the
        # current frame), and handing over the newer one would provoke exactly
        # the download-and-repaint this branch exists to avoid. Only a board we
        # have never painted for gets the current frame, because one repaint
        # beats leaving a blank panel with no explanation.
        serve = _held_frame_name(device) or (current_frame().name if current_frame() else "")
        sleep_s = device.low_battery_sleep_s
        log.warning("device %s gated at %s mV, sleeping %ss", device.id, mv, sleep_s)
        return JSONResponse(
            _display_payload(
                filename=serve,
                image_url=_image_url(serve) if serve else "",
                refresh_rate=sleep_s,
            )
        )

    # An update and a repaint never share a wake. Flashing is 1.35 MB of radio
    # and a flash write; a cold panel refresh is 35 s of panel drive. Both
    # inside one cycle would push a healthy wake against the watchdog, and the
    # firmware powers the panel rail down before it starts the transfer anyway.
    # One slot of delay buys the certainty that they cannot overlap.
    #
    # But a repaint that panel-care rule 2 REQUIRES outranks the update, and
    # that is not a nicety. The offer is repeated on every wake for as long as
    # the board keeps reporting the old version, so an update that can never
    # finish -- a weak signal, a wrong image, a full disk -- would otherwise
    # suspend the mandatory daily refresh indefinitely. The documented failure
    # mode for that is burn-in: permanent, and caused by the very feature that
    # was supposed to make the device easier to look after.
    #
    # One slot of delay for the update, against a rule whose violation cannot
    # be undone.
    firmware = _firmware_offer(device, dh.telemetry.fw_version, dh.telemetry.battery_percent)
    if firmware is not None and _burn_in_refresh_due(device, now):
        log.warning(
            "device %s is due its daily refresh, holding firmware %s back one slot",
            device.id,
            firmware.version,
        )
        firmware = None
    if firmware is not None:
        serve = _held_frame_name(device) or (current_frame().name if current_frame() else "")
        log.info(
            "device %s offered firmware %s (%s bytes), no repaint this wake",
            device.id,
            firmware.version,
            firmware.size_bytes,
        )
        return JSONResponse(
            _display_payload(
                filename=serve,
                image_url=_image_url(serve) if serve else "",
                refresh_rate=refresh_rate,
                firmware=firmware,
            )
        )

    # The stored row is a superset of this wake's headers (record_telemetry
    # merges rather than overwrites), so a wake that omitted the temperature
    # still gets the panel-care gate applied against the last known reading.
    telemetry = device.as_telemetry()

    try:
        # The same list the countdown above used, deliberately not a second
        # lookup: the printed "nächste Aktualisierung" and the seconds the
        # board actually sleeps have to name the same moment.
        # The board's own ceiling, not ours: it hangs up at
        # device_http_timeout_s and does not come back until its next slot. With
        # the prewarm doing its job this costs no freshness at all - nothing
        # here goes near the network - and on the morning the prewarm failed it
        # is the difference between a frame with "ohne Termine" in the footer
        # and no frame until the afternoon.
        frame: Optional[Frame] = await render_frame_async(
            telemetry, now, slots=slots, budget_s=assemble.device_budget_s()
        )
    except Exception as exc:  # noqa: BLE001 - last line of defence
        # assemble and the renderer both promise not to raise; if one breaks
        # that promise the board must still be told when to wake up again,
        # because a 500 here means it sleeps on its firmware default or not at
        # all.
        log.exception("frame build failed: %s", exc)
        frame = current_frame()

    if frame is None:
        return JSONResponse(
            _display_payload(filename="", image_url="", refresh_rate=refresh_rate)
        )

    content_changed = frame.key != device.last_etag
    repaint, reason = should_repaint(
        content_changed=content_changed,
        # The later of "told it to paint" and "it fetched the bytes". Using the
        # first alone let anyone who could reach this endpoint keep the floor
        # fresh and freeze the panel; using the second alone would forget a
        # repaint that was ordered but never collected.
        last_painted_at=device.painted_or_fetched_at,
        now=now,
        min_gap_s=device.min_refresh_gap_s,
        max_age_s=device.max_image_age_s,
        temperature_c=telemetry.temperature_c,
        min_temp_c=device.min_refresh_temp_c,
    )

    if repaint:
        serve = frame.name
        _record(lambda: store.record_paint(device.id, frame.key, now), "paint")
    else:
        # Hand back the name the board already holds. It compares filenames
        # before it opens the image connection, so this is the cheap wake.
        serve = _held_frame_name(device) or frame.name

    log.info(
        "display device=%s mac=%s repaint=%s (%s) serve=%s next=%s in %ss",
        device.id,
        device.mac or "?",
        repaint,
        reason,
        serve,
        target.isoformat(timespec="minutes"),
        refresh_rate,
    )
    return JSONResponse(
        _display_payload(filename=serve, image_url=_image_url(serve), refresh_rate=refresh_rate)
    )


# -- /api/image -------------------------------------------------------------


def _if_none_match(raw: Optional[str]) -> set[str]:
    """Parse an If-None-Match header into bare validators.

    Proxies and firmware both mangle this: some send the quotes, some do not,
    some prefix W/. Comparing the bare token is the only version that works
    against all of them.
    """
    if not raw:
        return set()
    out: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        out.add(token.strip('"'))
    return out


@app.get("/api/image/{name}")
async def api_image(name: str, request: Request) -> Response:
    # Checked before the 304 shortcut below, so an unauthorised caller cannot
    # use the ETag as an oracle for which frames exist.
    device, denied = _authorise(request)
    if denied is not None:
        return denied
    assert device is not None

    candidates = _if_none_match(request.headers.get("If-None-Match"))
    stem = name[:-4] if name.endswith(".png") else name

    # Only the frame this board holds, or the one it was just told to paint.
    # Without this an authenticated device could walk the last twelve frames --
    # every one of which carries guests' names -- simply by guessing ETags off
    # its own history. Same shape of rule as "only the assigned image" on the
    # firmware route, and for the same reason: an authorisation that does not
    # bound *what* may be fetched is only half of one.
    current = current_frame()
    allowed = {stem for stem in (device.last_etag, current.key if current else None) if stem}
    if stem not in allowed:
        log.warning("device %s asked for frame %s, which is not its own", device.id, stem)
        return JSONResponse({"status": 404, "message": "no such frame"}, status_code=404)

    # Never cached anywhere but on the device. The path ends in .png, and
    # Cloudflare caches by file extension -- an edge cache would sit in front of
    # every bearer check, holding a picture with guests' names on it.
    headers = {
        "ETag": f'"{stem}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if stem in candidates or "*" in candidates:
        # 304 is the whole point of the design: no body, no panel drive,
        # roughly a fifth of a full wake (9.5). It also proves the board is
        # reachable and holding what we would have sent, so it counts as a
        # fetch just as much as a 200 does.
        _record(lambda: get_store().record_image_fetch(device.id, _now()), "image_fetch")
        return Response(status_code=304, headers=headers)

    path = _frame_path(name)
    if path is not None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            data = b""
        if data:
            _record(lambda: get_store().record_image_fetch(device.id, _now()), "image_fetch")
            return Response(content=data, media_type="image/png", headers=headers)

    frame = current_frame()
    if frame is not None and name == frame.name:
        _record(lambda: get_store().record_image_fetch(device.id, _now()), "image_fetch")
        return Response(content=frame.png, media_type="image/png", headers=headers)

    return JSONResponse({"status": 404, "message": "no such frame"}, status_code=404)


# -- /api/log ---------------------------------------------------------------


@app.post("/api/log")
async def api_log(request: Request) -> Response:
    """Swallow whatever the board posts and answer 204 (9.2).

    Permissive about the *body* -- it is the one thing a device sends when
    something is already wrong, and rejecting it with a 422 would lose exactly
    the message we want. Not permissive about who may write: an open log
    endpoint on a public host is a free disk-filling primitive.
    """
    device, denied = _authorise(request)
    if denied is not None:
        return denied
    assert device is not None

    raw = await request.body()
    payload: dict[str, Any]
    try:
        parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        payload = parsed if isinstance(parsed, dict) else {"body": parsed}
    except ValueError:
        payload = {"body": raw.decode("utf-8", "replace")[:2000]}

    # TRMNL nests the real fields under "log" or "logs_array"; flatten one
    # level so the status page shows a message instead of "{...}".
    inner = payload.get("log")
    if isinstance(inner, dict):
        payload = {**inner, **{k: v for k, v in payload.items() if k != "log"}}

    # The identity comes from the authenticated row, never from the header the
    # caller set. Reading `ID` here let any authorised device file its lines
    # under another device's name.
    _record(lambda: get_store().add_log(device.id, payload, _now()), "log")
    log.info("device log device=%s %s", device.id, str(payload)[:300])
    return Response(status_code=204)


# -- operator endpoints -----------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness only. Deliberately says nothing else.

    It used to return the current frame's name, its age and the failing
    sources. The frame name *is* the ETag *is* the image path, so an unguarded
    /healthz was the first half of a chain that ended in a PNG with guests'
    names on it, fetched without any credential at all. The container health
    check only ever looked at the status code.

    What the operator wants from it moved to /api/internal/status, behind the
    internal key.
    """
    return JSONResponse({"status": "ok"})


@app.get("/api/internal/status")
async def internal_status(request: Request) -> JSONResponse:
    """What /healthz used to say, for the operator. Needs ADMIN_API_KEY when set."""
    denied = _authorise_internal(request)
    if denied is not None:
        return denied
    frame = current_frame()
    return JSONResponse(
        {
            "status": "ok",
            "frame": frame.name if frame else None,
            "frame_age_s": int((_now() - frame.built_at).total_seconds()) if frame else None,
            "failures": list(frame.dashboard.failures) if frame else [],
            "sources": assemble.cache_state(),
        }
    )


@app.get("/preview")
async def preview(request: Request) -> Response:
    """Render right now and hand back the pixels. The layout workbench.

    ?raw=1 returns the packed 4 bpp buffer instead — exactly 120 000 bytes at
    400x600, which is the quickest way to prove the wire format is intact.
    ?fresh=1 drops the source cache first.
    """
    denied = _authorise_internal(request)
    if denied is not None:
        return denied

    if request.query_params.get("fresh"):
        assemble.clear_cache()

    device = _record(lambda: get_store().latest_device(), "latest")
    telemetry = device.as_telemetry() if device else Telemetry(device_id="")
    try:
        frame = await render_frame_async(telemetry, _now())
    except Exception as exc:  # noqa: BLE001
        # The browser is the one caller that wants an error, not a fallback.
        # The reason goes to the log, not into the response: an exception
        # text can carry a path, a DSN or a calendar URL, and this endpoint is
        # open whenever ADMIN_API_KEY is unset.
        log.exception("preview render failed: %s", exc)
        return JSONResponse(
            {"status": 503, "message": "render failed, see the server log"}, status_code=503
        )

    if request.query_params.get("raw"):
        return Response(
            content=frame.packed,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{frame.key}.bin"',
            },
        )
    return Response(
        content=frame.png, media_type="image/png", headers={"Cache-Control": "no-store"}
    )


# -- firmware delivery ------------------------------------------------------


@app.get("/api/firmware/{firmware_id}")
async def api_firmware(firmware_id: str, request: Request) -> Response:
    """Hand over one OTA image, and only to the device it is assigned to.

    The assignment check is the point: without it the staged rollout would be a property
    of the display response and not of the download. Pull a rollout back after
    it goes wrong on one device, and a board that had already been handed the
    URL would still fetch the withdrawn image -- this one has neither Range nor
    resume, so a restart mid-download means starting over from whatever URL it
    remembered.
    """
    device, denied = _authorise(request)
    if denied is not None:
        return denied
    assert device is not None

    if device.firmware_id != firmware_id:
        log.warning(
            "device %s asked for firmware %s, which is not the one assigned to it (%s)",
            device.id,
            firmware_id,
            device.firmware_id,
        )
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)

    try:
        firmware = get_store().get_firmware(firmware_id)
    except DbUnavailable as exc:
        log.warning("firmware lookup unavailable: %s", exc)
        return _unavailable("store unavailable")
    if firmware is None:
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)

    # Second class check. The first is where the offer is made; this one is
    # what stops a hand-crafted request. A frame image on this board would pass
    # the checksum, flash, and not boot.
    if firmware.target != "eink":
        log.error("firmware %s targets %s, refusing", firmware.id, firmware.target)
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)

    path = _firmware_path(firmware.file_path)
    if path is None:
        log.error("firmware file missing or escapes the firmware dir: %r", firmware.file_path)
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)

    # Cheap integrity check against a half-written upload, caught here rather
    # than on the device. The board verifies the digest itself -- it has it from
    # the display response -- so this is only about not spending a wake and
    # 1.35 MB of radio on bytes that cannot possibly match.
    try:
        actual = path.stat().st_size
    except OSError as exc:
        log.error("cannot stat firmware %s: %s", firmware.id, exc)
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)
    if actual != firmware.size_bytes:
        log.error(
            "firmware %s is %s bytes on disk but %s in the row",
            firmware.id,
            actual,
            firmware.size_bytes,
        )
        return JSONResponse({"status": 404, "message": "not found"}, status_code=404)

    log.info(
        "serving firmware %s (%s, %s bytes) to %s",
        firmware.version,
        firmware.id,
        actual,
        device.id,
    )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(firmware.file_name)}"',
            "X-Firmware-Version": firmware.version,
            "X-Firmware-SHA256": firmware.sha256,
            # `no-store` because the image is authorised per device and the edge
            # must not hold it; `Accept-Ranges: none` so nobody later builds a
            # resume the firmware does not have.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "none",
        },
    )
