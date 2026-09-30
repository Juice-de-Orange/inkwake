"""Configuration, read once from the environment at import time.

Everything that differs between a laptop and a server lives here. Nothing
else in the codebase reads os.environ, so the full set of knobs is this file
and .env.example stays honest by construction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str) -> str:
    """Empty counts as unset, like the numeric helpers below.

    docker-compose.yml passes every setting as `${VAR:-}`, so an operator who
    leaves a line empty in .env sends an empty string, not nothing. Taking that
    literally would turn USER_AGENT or TZ_NAME into "" instead of the default.
    """
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_slots(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse "06:50,14:50,19:50" into ((6, 50), (14, 50), (19, 50)).

    Sorted and de-duplicated, because the scheduler's "next slot" search
    assumes ascending order and a duplicate would make one wake a no-op.
    """
    slots: set[tuple[int, int]] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            hour, minute = int(hh), int(mm)
        except ValueError as exc:
            raise ValueError(
                f"REFRESH_SLOTS entry {part!r} is not HH:MM"
            ) from exc
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"REFRESH_SLOTS entry {part!r} is not a valid time")
        slots.add((hour, minute))
    if not slots:
        raise ValueError("REFRESH_SLOTS is empty; at least one HH:MM is required")
    return tuple(sorted(slots))


@dataclass(frozen=True)
class Settings:
    # -- identity / display -------------------------------------------------
    #: Native panel geometry, portrait. Not a preference: the framebuffer is
    #: 400x600 and the packer refuses anything else (HARDWARE.md 4.1).
    panel_width: int = 400
    panel_height: int = 600

    # -- scheduling ---------------------------------------------------------
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(_env("TZ_NAME", "Europe/Vienna")))
    #: Local wall-clock times the board wakes at. The server converts these to
    #: a seconds countdown per request, which is what makes DST a non-issue
    #: for the firmware (4.4 / 9.6).
    #:
    #: A FALLBACK, not the truth: every device carries its own list in its
    #: registry row (`python -m app.cli device set <id> --slots …`), and both
    #: the countdown and the printed "next refresh" come from there. This value
    #: is what a render with no device uses, and what the prewarm loop falls
    #: back to when the registry cannot be read. It must therefore stay in step
    #: with DEFAULT_SLOTS in store.py -- a prewarm aimed at times no device
    #: wakes at is a prewarm that warms nothing, and nothing anywhere turns red.
    refresh_slots: tuple[tuple[int, int], ...] = field(
        default_factory=lambda: _parse_slots(_env("REFRESH_SLOTS", "06:50,14:50,19:50"))
    )
    # The panel-care values and the battery thresholds below are DEFAULTS
    # ONLY. Each device carries its own copy in the registry (set with the
    # CLI); /api/display reads the row, never these. They stay here because a
    # render without a device still needs a sane value.
    #: Never instruct a repaint sooner than this after the previous one.
    #: Vendor guidance is 180 s and the documented failure is permanent panel
    #: damage, so the server enforces it too rather than trusting firmware
    #: (4.4 rule 1).
    min_refresh_gap_s: int = field(default_factory=lambda: _env_int("MIN_REFRESH_GAP_S", 180))
    #: Force a repaint if the panel has held one image this long, even when
    #: the content hash is unchanged. Leaving an image up indefinitely is the
    #: documented burn-in path (4.4 rule 2).
    max_image_age_s: int = field(default_factory=lambda: _env_int("MAX_IMAGE_AGE_S", 23 * 3600))
    #: Small random spread so a retry storm cannot synchronise. Seconds.
    refresh_jitter_s: int = field(default_factory=lambda: _env_int("REFRESH_JITTER_S", 20))

    # -- panel care ---------------------------------------------------------
    #: Below this the panel shows a colour cast and the vendor's remedy is
    #: hours at room temperature. We skip the repaint instead (4.4 rule 4).
    min_refresh_temp_c: float = field(
        default_factory=lambda: _env_float("MIN_REFRESH_TEMP_C", 15.0)
    )

    # -- battery ------------------------------------------------------------
    #: Below this, stop fetching content and show a charge prompt. The
    #: factory firmware hard-shutdowns at 3100 mV, so this leaves headroom
    #: for several more wakes (6.2 / 9.6).
    battery_low_mv: int = field(default_factory=lambda: _env_int("BATTERY_LOW_MV", 3300))
    battery_recover_mv: int = field(default_factory=lambda: _env_int("BATTERY_RECOVER_MV", 3500))
    #: Community discharge study calibration points (6.2).
    battery_empty_mv: int = field(default_factory=lambda: _env_int("BATTERY_EMPTY_MV", 3350))
    battery_full_mv: int = field(default_factory=lambda: _env_int("BATTERY_FULL_MV", 4160))

    # -- content sources ----------------------------------------------------
    #: Optional read-only Postgres DSN for the bookings and sightings panels
    #: (see app/sources/database.py for the view contract). Empty disables both
    #: panels rather than failing the whole frame.
    sources_dsn: str = field(default_factory=lambda: _env("SOURCES_DSN", ""))
    bookings_view: str = field(default_factory=lambda: _env("BOOKINGS_VIEW", "inkwake_bookings"))
    sightings_view: str = field(default_factory=lambda: _env("SIGHTINGS_VIEW", "inkwake_sightings"))
    #: Where the sighting images live; `image_path` in the view is relative to it.
    sightings_dir: Path = field(
        default_factory=lambda: Path(_env("SIGHTINGS_DIR", "/data/sightings"))
    )
    bookings_count: int = field(default_factory=lambda: _env_int("BOOKINGS_COUNT", 3))
    #: Comma-separated ICS feed URLs for the events panel. Secret calendar links
    #: are credentials: keep them in .env, never in a committed file.
    events_ics_urls: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            u.strip() for u in _env("EVENTS_ICS_URLS", "").split(",") if u.strip()
        )
    )
    events_count: int = field(default_factory=lambda: _env_int("EVENTS_COUNT", 6))
    events_days_ahead: int = field(default_factory=lambda: _env_int("EVENTS_DAYS_AHEAD", 7))

    #: Section headings printed on the frame.
    bookings_title: str = field(default_factory=lambda: _env("BOOKINGS_TITLE", "Buchungen"))
    events_title: str = field(default_factory=lambda: _env("EVENTS_TITLE", "Termine"))
    sighting_title: str = field(default_factory=lambda: _env("SIGHTING_TITLE", "Sichtung"))

    #: Location for weather and sun times. The default is only an example
    #: (Vienna); set your own.
    weather_lat: float = field(default_factory=lambda: _env_float("WEATHER_LAT", 48.2082))
    weather_lon: float = field(default_factory=lambda: _env_float("WEATHER_LON", 16.3738))

    #: How long a fetched source stays usable. Content is refreshed three
    #: times a day; caching for an hour means a wake never waits on five
    #: upstreams, and a brief outage is invisible.
    source_cache_ttl_s: int = field(default_factory=lambda: _env_int("SOURCE_CACHE_TTL_S", 3600))
    http_timeout_s: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT_S", 15.0))
    #: What the BOARD allows one request to take, mirroring HTTP_TIMEOUT_MS in
    #: firmware/src/config.h. Not a preference - a fact about the thing on the
    #: wall, and the real ceiling on /api/display: the board hangs up at this
    #: number, counts the wake as failed and comes back at its next slot, so a
    #: request still gathering sources by then has already lost whatever it
    #: eventually renders. Changing it means a flash, not a restart.
    device_http_timeout_s: float = field(
        default_factory=lambda: _env_float("DEVICE_HTTP_TIMEOUT_S", 8.0)
    )
    user_agent: str = field(
        default_factory=lambda: _env(
            "USER_AGENT", "inkwake/1.0 (+https://github.com/Juice-de-Orange/inkwake)"
        )
    )

    # -- server -------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))
    #: Base URL the device uses to build image links. Must be reachable from
    #: the board, not from the container.
    public_base_url: str = field(
        default_factory=lambda: _env("PUBLIC_BASE_URL", "http://127.0.0.1:8099").rstrip("/")
    )
    #: Guards /preview and /api/internal/status. Both render or describe a
    #: frame, and a frame can carry personal data (booking names). Accepted as
    #: `X-Admin-Key` or `Authorization: Bearer`, never as a query parameter --
    #: a credential in a URL ends up in access logs and browser history.
    #: Empty means unguarded, which is right on a laptop and wrong anywhere else.
    admin_api_key: str = field(default_factory=lambda: _env("ADMIN_API_KEY", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    #: Render a frame on startup so a fresh container never answers the first
    #: device request with a cold cache.
    warm_on_start: bool = field(default_factory=lambda: _env_bool("WARM_ON_START", True))
    #: How long before each refresh slot to re-fetch the sources, so the wake
    #: that follows never waits on an upstream. A battery setting, not a
    #: freshness one: the board holds its radio up at ~120 mA for every second
    #: the server spends scraping, so a round trip paid here is one the cell
    #: does not pay. 0 disables it.
    #:
    #: Tied to the slots rather than run on a fixed interval, and that is the
    #: whole design. A plain interval has to be shorter than
    #: source_cache_ttl_s to be useful at all, which meant ~48 fetch rounds a
    #: day against every calendar feed, plus the same again at Open-Meteo and
    #: twice that into the source database. The
    #: payoff is about 40 mAh a year, so that trade was sixteen times the
    #: third-party traffic for three per cent of one charge. Firing once
    #: shortly before each slot buys the same saving with three.
    #:
    #: Must stay well under source_cache_ttl_s: the cache has to still be warm
    #: when the board actually calls, including its wake jitter.
    #:
    #: Only effective with a single worker process - the source cache in
    #: assemble.py is module-global, so a second worker would keep its own.
    prewarm_lead_s: int = field(default_factory=lambda: _env_int("PREWARM_LEAD_S", 300))

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def db_path(self) -> Path:
        """The device registry. Lives in the data volume; back it up (it holds
        the token hashes -- losing it means re-provisioning every board)."""
        return self.data_dir / "inkwake.sqlite3"

    @property
    def firmware_dir(self) -> Path:
        """OTA images registered with `python -m app.cli firmware add`."""
        return self.data_dir / "firmware"


settings = Settings()
