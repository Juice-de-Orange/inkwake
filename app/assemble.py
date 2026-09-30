"""Fan out to the content sources and fold the answers into one Dashboard.

Two rules shape this module, both from the same premise: the panel is
bistable, so a late frame costs nothing and a missing frame costs a whole
cycle.

  1. **No source may raise.** Every fetch is wrapped; a failure becomes an
     entry in Dashboard.failures and an empty value. A frame with four of five
     panels is right, a stack trace is not.
  2. **Sources run concurrently.** The modules are synchronous (psycopg,
     httpx.get), so they go into a thread pool. Run sequentially, five sources
     with a 15 s timeout each would let one dead website push the response
     past a minute, and the board gives up long before that.

Failures arrive by two different routes and both have to be collected. A
source that raises is caught here. A source that handles its own errors - all
of ours do - reports them either by returning None or by filling in a
`failures` list it was handed, and this module has to go looking for that: a
source database that is merely down returns an empty booking list, which is
indistinguishable from "nothing is booked" unless somebody asks.

The names put into Dashboard.failures are the keys of layout.FAILURE_LABELS.
Anything else reaches the panel as a raw slug inside the staleness badge.

On top of that sits a small in-process cache. Content is fetched three times a
day; without it a manual /preview or a device retry would re-scrape every
upstream. Values that fail to refresh fall back to the last good one, with the
failure still reported - HARDWARE.md 9.1 is explicit: show the stale content
and say so, do not throw away a good frame.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence

from .config import settings
from .models import Booking, Dashboard, Event, Sighting, SunTimes, Telemetry, Weather
from .schedule import next_slot

log = logging.getLogger(__name__)

#: Names that may appear in Dashboard.failures. These are not free-form: the
#: renderer looks each one up in FAILURE_LABELS to build the staleness badge,
#: and an unknown name is printed on the panel verbatim.
SOURCE_BOOKINGS = "bookings"
SOURCE_SIGHTING = "sighting"
SOURCE_WEATHER = "weather"
SOURCE_SUN = "sun"
SOURCE_EVENTS = "events"
SOURCE_TELEMETRY = "telemetry"


#: How long a value may be served after its TTL expired, when the refresh
#: failed. A day-old weather reading is still worth showing next to a "stale"
#: badge; a week-old one is a lie.
STALE_MAX_S = 24 * 3600

#: Threads outlive an abandoned future, so the pool is shared. Sized for two
#: concurrent collections, not one: _plan() yields up to five jobs, and the
#: background prewarm can overlap a device request. At six workers those ten
#: jobs would queue four deep, and a device request whose jobs wait behind a
#: slow prewarm can time out on every source and paint a frame badged stale
#: across the board - the exact outcome the prewarm exists to prevent.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
#: Set by shutdown(), cleared by reopen(). Without it a worker that outlives
#: shutdown() would quietly build a replacement pool on its way past _pool().
_closed = False


class ShuttingDown(RuntimeError):
    """A source job was submitted after shutdown() latched the pool closed."""


def _pool() -> ThreadPoolExecutor:
    """The shared source pool, recreated after a reopen().

    Recreating matters because shutdown() is wired to the app lifespan, and a
    process that starts the app twice (the test suite, a reload) would
    otherwise hit a permanently dead executor. Recreating *implicitly*,
    however, is how an abandoned worker resurrects the pool after the process
    has decided to stop - so a new pool takes an explicit reopen().
    """
    global _executor
    with _executor_lock:
        if _closed:
            raise ShuttingDown("source pool is shut down")
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="source")
        return _executor


def reopen() -> None:
    """Allow a new pool after a shutdown. Called when the app starts."""
    global _closed
    with _executor_lock:
        _closed = False


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    value: Any
    #: Failures the source reported when this value was produced, cached with
    #: it. A partial result served from cache is still partial: the badge must
    #: not vanish at 15:00 for an outage that is still going on.
    failures: tuple[str, ...]
    stored_at: float  # time.monotonic(), immune to NTP steps and DST


_cache: dict[str, _Entry] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Drop every cached source value. Used by /preview?fresh=1 and by tests."""
    with _cache_lock:
        _cache.clear()


def cache_state() -> dict[str, float]:
    """Age in seconds of each cached value, for the status page."""
    now = time.monotonic()
    with _cache_lock:
        return {key: now - entry.stored_at for key, entry in _cache.items()}


def _cached(key: str, ttl_s: float) -> Optional[_Entry]:
    with _cache_lock:
        entry = _cache.get(key)
    if entry is None or (time.monotonic() - entry.stored_at) > ttl_s:
        return None
    return entry


def _store(key: str, value: Any, failures: Sequence[str]) -> None:
    with _cache_lock:
        _cache[key] = _Entry(
            value=value, failures=tuple(failures), stored_at=time.monotonic()
        )


# --------------------------------------------------------------------------
# Source loading
# --------------------------------------------------------------------------


def _load(module: str, attr: str) -> Callable[..., Any]:
    """Import a source function at call time, never at import time.

    The source modules are separate deployables in practice: one of them
    failing to import (a missing psycopg, a syntax error mid-edit) must cost
    that one panel, not the whole server's startup.
    """
    mod = importlib.import_module(f".sources.{module}", package=__package__)
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise AttributeError(f"app.sources.{module}.{attr} is missing")
    return fn


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # C callables have no inspectable signature
        return False


def _call(fn: Callable[..., Any], args: tuple[Any, ...], sink: list[str]) -> Any:
    """Call a source, handing it our failure sink when it offers one.

    The sink is how a source separates "nothing to report" from "could not
    ask" - the database source returns the same empty list either way. A source written
    to the plain signature simply does not get the keyword.
    """
    return fn(*args, failures=sink) if _accepts(fn, "failures") else fn(*args)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

#: A job returns its value plus whatever the source itself wanted reported.
JobResult = tuple[Any, list[str]]


@dataclass(frozen=True)
class _Job:
    name: str          # key into `values`, and the failure label if it blows up
    key: str           # cache key, carries every parameter that changes the result
    call: Callable[[], JobResult]
    empty: Any         # what the dashboard gets when there is nothing at all


def _weather_job() -> JobResult:
    value = _load("weather", "fetch_weather")(
        settings.weather_lat,
        settings.weather_lon,
        settings.http_timeout_s,
        settings.user_agent,
    )
    if not isinstance(value, Weather):
        # None is how the module says "Open-Meteo let us down"; anything else
        # is a source that broke its own return contract. Both are outages.
        return None, [SOURCE_WEATHER]
    return value, []


def _sun_job(today: date) -> JobResult:
    value = _load("sun", "sun_times")(
        today, settings.weather_lat, settings.weather_lon, settings.timezone
    )
    if not isinstance(value, SunTimes):
        return None, [SOURCE_SUN]
    return value, []


def _events_job(today: date) -> JobResult:
    """Everything in the window, uncut and unlimited. The cache's half.

    What comes back is NOT what the frame prints: build_dashboard runs
    events.trim_events over it with the frame's own clock. The split is what
    lets the prewarm work at all - it fills this cache 300 s before a slot, so
    anything that depended on the exact minute would be a miss at the wake
    (see the module docstring in sources/events.py).

    **There is deliberately no fallback to `fetch_events`, and that is a
    correctness fix, not tidying.** It trims internally - collapse included - and `build_dashboard`
    then trims the result a second time. After the first collapse a run is a
    single row on its earliest day, so the second pass cannot recognise it as
    a run any more and simply cuts it once that day's start has passed: a
    five-day series that should have moved to tomorrow disappears from the
    panel instead. Measured against the real functions, not reasoned about.
    The information the second pass would need is gone by then, so the only
    fix is to have exactly one place that trims.

    A module without `collect_events` therefore raises out of here, which
    `_collect` turns into one badged panel - the same outcome an unimportable
    source module has always had, and the contract `_load` exists for.

    Keywords, not positionals: `collect_events` has no `limit`, so its
    signature differs by one position from `fetch_events`. A positional call
    would hand `events_count` in as the HTTP timeout and fail silently.
    """
    events, sub_failures = _load("events", "collect_events")(
        days_ahead=settings.events_days_ahead,
        timeout=settings.http_timeout_s,
        user_agent=settings.user_agent,
        today=today,
    )
    return _events_result(events, sub_failures)


def _events_result(
    events: Any, sub_failures: Sequence[str], calendars: Optional[int] = None
) -> JobResult:
    """Turn the calendar answer into a job result, badge included.

    The individual calendars (`calendar-1`, …) never reach Dashboard.failures
    themselves -- the frame names the panel, not the feed. They decide whether
    the panel earns the staleness badge:

    * Nothing at all: badge.
    * Every calendar down, but events still present: those came out of the
      disk cache and may be days old. This is exactly what the badge is for
      (9.7) -- a panel that prints last week with complete confidence is worse
      than one that admits its age.
    * Some calendars down while another answered live: no badge. The frame is
      still current, and a badge under six visible events is how a reader
      learns to ignore badges altogether. The partial outage goes to the log.

    `events` is the whole window, before the time cut. That is the right list
    to judge an outage on: "nothing left today after 19:50" is not a broken
    calendar.
    """
    calendars = len(settings.events_ics_urls) if calendars is None else calendars
    reported: list[str] = []
    if sub_failures:
        log.warning("events: calendars unavailable: %s", ", ".join(sub_failures))
        all_down = calendars > 0 and len(set(sub_failures)) >= calendars
        if not events or all_down:
            reported.append(SOURCE_EVENTS)

    if not isinstance(events, list):
        return [], [SOURCE_EVENTS]
    return [e for e in events if isinstance(e, Event)], reported


def _bookings_job(today: date) -> JobResult:
    reported: list[str] = []
    value = _call(
        functools.partial(_load("database", "fetch_bookings"), view=settings.bookings_view),
        (settings.sources_dsn, settings.bookings_count, today),
        reported,
    )
    if not isinstance(value, list):
        return [], [SOURCE_BOOKINGS]
    return [b for b in value if isinstance(b, Booking)], reported


def _sighting_job() -> JobResult:
    reported: list[str] = []
    value = _call(
        functools.partial(_load("database", "fetch_latest_sighting"), view=settings.sightings_view),
        (settings.sources_dsn, settings.sightings_dir),
        reported,
    )
    # None is a legitimate answer here - nothing published yet - which is
    # exactly why this source reports its outages through the sink instead.
    if value is not None and not isinstance(value, Sighting):
        return None, [SOURCE_SIGHTING]
    return value, reported


def _plan(now_local: datetime) -> list[_Job]:
    today: date = now_local.date()
    jobs = [
        _Job(SOURCE_WEATHER, "weather", _weather_job, None),
        _Job(SOURCE_SUN, f"sun:{today.isoformat()}", lambda: _sun_job(today), None),
        # No events_count in the key: the cached value is the whole window,
        # uncut and unlimited. The count applies in build_dashboard, where the
        # frame's clock is - putting it here again would only make the key
        # lie about what determines the value.
        _Job(
            SOURCE_EVENTS,
            f"events:{today.isoformat()}:{settings.events_days_ahead}",
            lambda: _events_job(today),
            [],
        ),
    ]

    # An unset DSN disables both database panels by design (config.py), and
    # that is a configuration choice, not an outage: no failure is recorded.
    if settings.sources_dsn:
        jobs.append(
            _Job(
                SOURCE_BOOKINGS,
                f"bookings:{today.isoformat()}:{settings.bookings_count}",
                lambda: _bookings_job(today),
                [],
            )
        )
        jobs.append(_Job(SOURCE_SIGHTING, "sighting", _sighting_job, None))
    return jobs


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


@dataclass
class _Collected:
    values: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def note(self, names: Sequence[str]) -> None:
        for name in names:
            if name and name not in self.failures:
                self.failures.append(name)


def _localise(now: datetime) -> datetime:
    tz = settings.timezone
    return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)


def _source_budget_s() -> float:
    """Twice the per-request HTTP timeout: enough for one retry inside a
    source, short enough that a black-holed TCP connection cannot hold the
    wake open. What /preview and prewarm() spend."""
    return max(5.0, settings.http_timeout_s * 2)


def device_budget_s() -> float:
    """The same, bounded by what the board will actually sit through.

    The budget above is thirty seconds at the default timeout. The firmware
    gives up after device_http_timeout_s - eight - calls the wake failed and
    does not come back until its next slot, so the remaining twenty-two
    seconds buy a frame nobody will ever receive.

    Normally none of this is reached: the prewarm refills the cache minutes
    before each slot, so a device request goes nowhere near the network. It is
    the morning the prewarm failed that this exists for, and then a frame with
    "ohne Termine" in the footer beats a timeout the board turns into a whole
    slot of nothing. Three quarters, so the render and the encode still have
    somewhere to come from.
    """
    return max(2.0, min(_source_budget_s(), settings.device_http_timeout_s * 0.75))


def _collect(jobs: list[_Job], ttl_s: float, budget_s: float) -> _Collected:
    """Run every job that has no fresh cache entry, within one shared budget.

    A ttl of 0 makes nothing fresh, which is how prewarm() forces a refetch.
    """
    out = _Collected()
    pending: dict[str, tuple[_Job, Future[JobResult]]] = {}

    for job in jobs:
        entry = _cached(job.key, ttl_s)
        if entry is not None:
            out.values[job.name] = entry.value
            out.note(entry.failures)
            continue
        try:
            pending[job.name] = (job, _pool().submit(job.call))
        except (ShuttingDown, RuntimeError) as exc:
            # The pool closed under us, which means the process is going away.
            # A frame with fewer sources beats an exception out of a request
            # handler that is still being served.
            log.warning("source %s not submitted: %s", job.name, exc)
            out.note([job.name])
            stale = _cached(job.key, STALE_MAX_S)
            out.values[job.name] = stale.value if stale is not None else job.empty

    deadline = time.monotonic() + budget_s
    for name, (job, future) in pending.items():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            value, reported = future.result(timeout=remaining)
        except Exception as exc:  # noqa: BLE001 - deliberately total
            # TimeoutError included: the worker keeps running, but this request
            # has stopped waiting for it.
            future.cancel()
            log.warning("source %s failed: %s: %s", name, type(exc).__name__, exc)
            out.note([job.name])
            stale = _cached(job.key, STALE_MAX_S)
            out.values[name] = stale.value if stale is not None else job.empty
            continue

        if reported:
            # A source that reports trouble must not evict a good value. Ours
            # signal failure by *returning normally* with their empty value
            # plus a name in `reported` - fetch_weather returns None, the
            # database source returns an empty list - so storing that unconditionally replaces
            # the last good reading with nothing. That is the opposite of this
            # module's own promise to serve stale content and say so, and it
            # only ever looked harmless because wakes are hours apart and the
            # poisoned entry always expired before the next one.
            #
            # Not storing also leaves `stored_at` where it was, so the entry
            # stays expired and the next collection retries live rather than
            # serving the failure back for a whole TTL.
            previous = _cached(job.key, STALE_MAX_S)
            if previous is not None and not previous.failures:
                out.values[name] = previous.value
                out.note(reported)
                continue

            # No good predecessor either, and this is the case that bites.
            # Storing here would still be storing a failure - with a *fresh*
            # stored_at, so the next collection reads it as valid for a whole
            # TTL instead of retrying. Prewarming makes that next collection
            # the wake itself, 300 s later against a 3600 s TTL. The first slot
            # of any day is exactly this shape: `sun`, `events` and `bookings`
            # key on the date, so no entry for today has ever existed yet, and
            # a 30-second upstream blip at 06:55 would blank the 07:00 frame
            # until 15:00 - a frame an unprewarmed wake would have filled.
            out.values[name] = value
            out.note(reported)
            continue

        _store(job.key, value, reported)
        out.values[name] = value
        out.note(reported)

    return out


def build_dashboard(
    telemetry: Telemetry,
    now: datetime,
    *,
    slots: Optional[Sequence[tuple[int, int]]] = None,
    budget_s: Optional[float] = None,
) -> Dashboard:
    """Everything one frame needs, assembled from whatever answered in time.

    Synchronous on purpose: it is called from a worker thread (see
    build_dashboard_async) and fans out into the pool itself, so nothing here
    ever touches the event loop.

    `slots` are the wake times of the device this frame is for, straight from
    its row. They decide the "nächste Aktualisierung" printed on the panel, so
    they have to be the same list the countdown is computed from - a frame
    that names a time the board will not wake at is a small lie the reader has
    no way to catch. None falls back to the configured default, which is right
    for /preview and the warm-up, where there is no particular device.
    """
    now_local = _localise(now)

    jobs = _plan(now_local)
    # An argument, because the two callers have genuinely different ceilings: a
    # board on a battery hangs up after eight seconds, a browser does not.
    collected = _collect(
        jobs,
        settings.source_cache_ttl_s,
        _source_budget_s() if budget_s is None else max(1.0, budget_s),
    )

    if telemetry.temperature_c is None and telemetry.humidity_pct is None:
        # The board reports its own SHT40 in the request headers, so a missing
        # reading is a source outage like any other - and it is the one that
        # also disables the panel-care temperature gate, so it is worth saying.
        collected.note([SOURCE_TELEMETRY])

    try:
        target = next_slot(now_local, slots or settings.refresh_slots, settings.timezone)
    except (ValueError, RuntimeError) as exc:
        # A misconfigured slot list must not cost the frame; the renderer only
        # prints this value.
        log.error("next_slot failed: %s", exc)
        target = now_local
        collected.note(["schedule"])

    return Dashboard(
        now=now_local,
        next_refresh=target,
        telemetry=telemetry,
        weather=collected.values.get(SOURCE_WEATHER),
        sun=collected.values.get(SOURCE_SUN),
        bookings=collected.values.get(SOURCE_BOOKINGS) or [],
        events=_trim(collected.values.get(SOURCE_EVENTS) or [], now_local),
        sighting=collected.values.get(SOURCE_SIGHTING),
        failures=collected.failures,
    )


def _trim(events: list[Event], now_local: datetime) -> list[Event]:
    """Cut the cached window down to what is still ahead, then to the count.

    Runs on every build rather than on every fetch, and that is the whole
    reason the events source comes in two halves: the cache is filled 300 s
    before a slot by the prewarm, so a cut baked into the cached value would
    carry the prewarm's clock instead of the frame's.

    Never fatal, but not free either: without trim_events the panel loses
    the cut AND the run collapse, so a weekly market can take several of the
    six lines. What it keeps is the order, because collect_events sorts. That
    is the honest trade - a frame beats a stack trace, and this path is only
    reachable if someone removes trim_events from the source module.
    """
    try:
        trim = _load("events", "trim_events")
    except (ImportError, AttributeError):
        return events[: settings.events_count] if settings.events_count > 0 else events
    try:
        return list(trim(events, now_local, settings.events_count))
    except Exception as exc:  # noqa: BLE001 - a frame beats a stack trace
        log.warning("events: trim failed (%s: %s), printing the window", type(exc).__name__, exc)
        return events[: settings.events_count] if settings.events_count > 0 else events


async def build_dashboard_async(
    telemetry: Telemetry,
    now: datetime,
    *,
    slots: Optional[Sequence[tuple[int, int]]] = None,
) -> Dashboard:
    """build_dashboard off the event loop, for the request handlers."""
    return await asyncio.to_thread(build_dashboard, telemetry, now, slots=slots)


def prewarm(now: datetime) -> list[str]:
    """Refetch every source regardless of cache age. Returns the failures.

    Separate from build_dashboard, and the difference is the whole point: a
    ttl of zero, so nothing counts as fresh. Going through build_dashboard
    would make a prewarm a no-op on any entry that has not expired yet, which
    means it could only ever refill a hole it happened to fall into - with a
    prewarm interval of half the ttl, two runs in three would do nothing and
    the cache would still be cold for a third of the time.

    No Dashboard is built and nothing is returned but the failure names: this
    exists to leave _cache warm, and whatever ends up on the panel is the
    device request's business.
    """
    jobs = _plan(_localise(now))
    return _collect(jobs, 0.0, _source_budget_s()).failures


def shutdown() -> None:
    """Stop accepting work. Abandoned source threads are not joined - a hung
    upstream must not be able to hold the container's shutdown open."""
    global _executor, _closed
    with _executor_lock:
        pool, _executor = _executor, None
        # Latched, because shutting the pool down does not stop the workers
        # already in it. One of those can outlive this call, reach _pool() and
        # build itself a replacement that nobody will ever shut down - and its
        # non-daemon threads are then joined by concurrent.futures' atexit
        # hook, holding the container open for exactly as long as the hung
        # upstream that shutdown(wait=False) was meant to escape.
        _closed = True
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "build_dashboard",
    "build_dashboard_async",
    "cache_state",
    "clear_cache",
    "prewarm",
    "reopen",
    "shutdown",
    "ShuttingDown",
    "SOURCE_BOOKINGS",
    "SOURCE_EVENTS",
    "SOURCE_SIGHTING",
    "SOURCE_SUN",
    "SOURCE_TELEMETRY",
    "SOURCE_WEATHER",
    "STALE_MAX_S",
]
