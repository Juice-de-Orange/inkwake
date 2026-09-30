"""Upcoming events from any number of iCalendar (ICS) feeds.

Point `EVENTS_ICS_URLS` at one or more calendars -- a Nextcloud or Google
calendar's secret ICS link, a club's public feed, a venue's programme -- and
the events panel shows what is coming up. No calendar configured means no
events panel content and no failure: that is a configuration choice.

Two noise filters apply to whatever the feeds deliver:

  * The same event in two calendars ("Summer Concert" vs "Summer  concert ")
    is one line: comparison runs on a normalised title, not the printed one.
  * A title spanning more than MAX_RUN_DAYS days is a run (a festival, an
    exhibition, a market every day this week) and collapses to one row, or it
    would fill the panel.

The module comes apart in two halves, and the seam is load-bearing:
`collect_events` talks to the network and knows only about days, while
`trim_events` knows the time of day and touches nothing. assemble.py caches
the first and runs the second on every frame, so a panel built at 14:50
prints what is still ahead at 14:50 without the prewarm - which runs 300 s
earlier - losing its effect. Anything time-dependent added here belongs in
the second half.

Failure policy: a calendar that times out, answers 5xx or sends something
that is not iCalendar yields no events plus its name in the returned
failures, never an exception. On top of that the last good answer of each
calendar is cached on disk, so a feed that disappears costs freshness, not
the panel -- and the caller still reports it, so the frame's footer admits
the content may be stale (HARDWARE.md 9.1: show the stale content and say
so).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar

from ..config import settings
from ..models import Event

log = logging.getLogger(__name__)

#: A title appearing on more days than this is a run, not a series, and is
#: reduced to a single row. Two is the smallest threshold that still shows a
#: genuine two-night performance in full.
MAX_RUN_DAYS = 2

#: Occurrences expanded per recurring event and window. A daily rule over a
#: seven-day window needs eight; the cap only stops a malformed rule (no
#: UNTIL, no COUNT, FREQ=SECONDLY) from spinning.
MAX_OCCURRENCES = 400

#: Sort key for an event with no start time. Untimed entries land after the
#: timed ones for their day: on a panel that shows six rows, "19:30 Concert"
#: beats "sometime today, a festival that has been running for a month".
_NO_TIME_SORT_KEY = "99:99"

_WS_RE = re.compile(r"\s+")


def calendar_name(index: int) -> str:
    """The failure name of the n-th configured calendar (1-based)."""
    return f"calendar-{index}"


def _squeeze(raw: Any) -> str:
    """Collapse whitespace. Nothing else -- calendar text is data, not markup."""
    return _WS_RE.sub(" ", str(raw or "")).strip()


# --------------------------------------------------------------------------
# iCalendar parsing
# --------------------------------------------------------------------------


def _local(value: date | datetime, tz: ZoneInfo) -> tuple[date, Optional[str]]:
    """(day, "HH:MM" or None) in local wall-clock time.

    A DATE (all-day) has no time. A DATE-TIME with a zone is converted to the
    display zone; a floating one (no zone) is taken to already be local, which
    is what RFC 5545 says floating time means.
    """
    if not isinstance(value, datetime):
        return value, None
    if value.tzinfo is not None:
        value = value.astimezone(tz)
    return value.date(), value.strftime("%H:%M")


def _occurrences(component: Any, window_start: datetime, window_end: datetime) -> list[date | datetime]:
    """Start values of one VEVENT inside the window, recurrences expanded."""
    start = component.decoded("dtstart")
    rule = component.get("rrule")
    if rule is None:
        return [start]

    all_day = not isinstance(start, datetime)
    anchor = datetime.combine(start, time.min) if all_day else start
    try:
        rset = rrulestr(
            rule.to_ical().decode("utf-8"),
            dtstart=anchor,
            forceset=True,
            ignoretz=anchor.tzinfo is None,
        )
        for exdate in _as_list(component.get("exdate")):
            for item in exdate.dts:
                value = item.dt
                if all_day and not isinstance(value, datetime):
                    value = datetime.combine(value, time.min)
                rset.exdate(value)
        lo = window_start if anchor.tzinfo is not None else window_start.replace(tzinfo=None)
        hi = window_end if anchor.tzinfo is not None else window_end.replace(tzinfo=None)
        found = []
        for occurrence in rset.between(lo, hi, inc=True):
            found.append(occurrence.date() if all_day else occurrence)
            if len(found) >= MAX_OCCURRENCES:
                log.warning("events: recurrence of %r capped at %d", _squeeze(component.get("summary")), MAX_OCCURRENCES)
                break
        return found
    except (ValueError, TypeError) as exc:
        # A rule dateutil cannot evaluate (for example an UNTIL in UTC against a
        # floating start). One occurrence is a better answer than no event.
        log.warning("events: cannot expand recurrence (%s), using the first occurrence", exc)
        return [start]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _duration(component: Any) -> Optional[timedelta]:
    """DTEND - DTSTART, or DURATION, or None when the entry has neither."""
    try:
        start = component.decoded("dtstart")
        end = component.decoded("dtend")
    except KeyError:
        duration = component.get("duration")
        return duration.dt if duration is not None else None
    try:
        return end - start
    except TypeError:
        # A DATE against a DATE-TIME, or an aware against a floating value.
        return None


def _span_days(component: Any, start: date | datetime, tz: ZoneInfo) -> int:
    """How many local calendar days one occurrence touches.

    All-day: DTEND is exclusive, so 24.-28. is four days. Timed: every day
    from the start's to the end's, except that an end at exactly 00:00 does
    not touch the day it lands on. A weekend festival from Friday 18:00 to
    Sunday 23:00 is on the panel on all three days -- on Saturday it is the
    thing that is happening right now.
    """
    duration = _duration(component)
    if duration is None or duration <= timedelta(0):
        return 1
    if not isinstance(start, datetime):
        return max(1, duration.days)
    first, _ = _local(start, tz)
    end = start + duration
    end_local = end.astimezone(tz) if end.tzinfo is not None else end
    last = end_local.date()
    if end_local.time() == time.min:
        last -= timedelta(days=1)
    return max(1, (last - first).days + 1)


def parse_ics(text: str | bytes, today: date, window_end: date, tz: Optional[ZoneInfo] = None) -> list[Event]:
    """All events of one calendar that fall on a day in [today, window_end].

    Raises ValueError when the text is not iCalendar at all; an individual
    malformed VEVENT is skipped with a warning instead.
    """
    tz = tz or settings.timezone
    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # icalendar raises ValueError and friends
        raise ValueError(f"not an iCalendar document: {exc}") from exc

    window_start = datetime.combine(today, time.min, tzinfo=tz)
    window_stop = datetime.combine(window_end, time.max, tzinfo=tz)

    # Occurrences moved or cancelled one by one (RECURRENCE-ID) replace the
    # occurrence of the base rule they name.
    overridden: set[tuple[str, str]] = set()
    for component in cal.walk("VEVENT"):
        rid = component.get("recurrence-id")
        if rid is None:
            continue
        try:
            overridden.add((str(component.get("uid", "")), _stamp(rid.dt)))
        except Exception as exc:  # noqa: BLE001 - see the loop below
            log.warning("events: ignoring a malformed RECURRENCE-ID (%s: %s)", type(exc).__name__, exc)

    events: list[Event] = []
    for component in cal.walk("VEVENT"):
        try:
            if str(component.get("status", "")).upper() == "CANCELLED":
                continue
            title = _squeeze(component.get("summary"))
            if not title:
                continue
            location = _squeeze(component.get("location"))
            uid = str(component.get("uid", ""))
            is_override = component.get("recurrence-id") is not None
            for start in _occurrences(component, window_start - timedelta(days=31), window_stop):
                if not is_override and (uid, _stamp(start)) in overridden:
                    continue
                day, hhmm = _local(start, tz)
                span = _span_days(component, start, tz)
                for offset in range(span):
                    current = day + timedelta(days=offset)
                    if today <= current <= window_end:
                        # Only the first day of a multi-day entry has a start
                        # time; the following days are "still going", which
                        # trim_events keeps all day. A run longer than
                        # MAX_RUN_DAYS is collapsed to one row later.
                        events.append(Event(current, hhmm if offset == 0 else None, title, location))
        except Exception as exc:  # noqa: BLE001 - one bad VEVENT must not cost the calendar
            log.warning("events: skipping a malformed VEVENT (%s: %s)", type(exc).__name__, exc)
    return events


def _stamp(value: date | datetime) -> str:
    """Comparable identity of an occurrence, for RECURRENCE-ID matching."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


# --------------------------------------------------------------------------
# Disk cache: the last good answer per calendar
# --------------------------------------------------------------------------

#: Older than this and a cached answer is not served at all. Every event
#: carries an absolute date, so a stale file mostly filters itself out of the
#: window; the cap is what stops a long run from being printed for weeks with
#: nothing but a footer hint to say so.
CACHE_MAX_AGE_DAYS = 7


def _cache_file(url: str, cache_dir: Optional[Path | str]) -> Path:
    """One file per feed, named by a hash of the URL -- secret ICS links never
    end up in a filename."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    base = Path(cache_dir) if cache_dir is not None else settings.data_dir
    return base / f"events_ics_{digest}.json"


def _read_cache(path: Path) -> Optional[str]:
    """The cached calendar text, or None. Every failure mode is the same answer."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ics"), str):
        return None
    try:
        fetched_at = datetime.fromisoformat(str(payload.get("fetched_at")))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched_at > timedelta(days=CACHE_MAX_AGE_DAYS):
        log.warning("events: cache at %s is older than %d days, ignoring it", path, CACHE_MAX_AGE_DAYS)
        return None
    return payload["ics"]


def _write_cache(path: Path, text: str) -> None:
    """Persist one good answer. Never raises; a read-only volume costs a cache,
    not a frame. Written via a unique temporary file and renamed, because the
    prewarm and a device request can collect at the same time."""
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "ics": text}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("events: cannot write cache to %s (%s)", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Merging and collapsing
# --------------------------------------------------------------------------

#: Folded before comparison so "für" and "fuer" meet: umlauts to their
#: two-letter forms, then combining marks stripped.
_UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalise_title(title: str) -> str:
    """Fold a title down to what two calendars are likely to agree on.

    Comparison only -- the printed title keeps its punctuation and umlauts.
    casefold() already turns "ß" into "ss", which is why it is not in the map.
    """
    folded = title.casefold().translate(_UMLAUT_MAP)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_NON_WORD_RE.sub(" ", stripped).split())


def _merge(groups: Iterable[list[Event]]) -> tuple[list[Event], int]:
    """Combine per-calendar lists, dropping repeats of (day, normalised title).

    Order matters: the first group wins. Live answers are passed before cached
    ones. Within one calendar the same key also collapses.
    """
    seen: dict[tuple[date, str], Event] = {}
    dropped = 0
    for group in groups:
        for event in group:
            key = (event.day, _normalise_title(event.title))
            if key in seen:
                dropped += 1
                continue
            seen[key] = event
    return list(seen.values()), dropped


def _collapse_runs(
    events: list[Event], run_days_from: Optional[Sequence[Event]] = None
) -> tuple[list[Event], int]:
    """Reduce a title occupying more than MAX_RUN_DAYS days to its first day.

    **`run_days_from` decides what counts as a run; `events` is what gets
    filtered.** They are the same list unless a caller has already thrown rows
    away -- and then they must not be, because "is this a run?" is a property
    of the event, not of what is left of it. `trim_events` cuts everything
    before now out of `events`; a Mon/Tue/Wed series seen on Monday evening
    would then be two days long, fall under the threshold and print TWICE on a
    panel with six lines. Measured against the wrong list, the guard switches
    itself off exactly when the panel is fullest.

    The keeper, on the other hand, is chosen from `events`: picking the
    earliest of ALL its days could name a day that was cut away, and the whole
    series would vanish instead of moving to tomorrow.
    """
    classify = list(run_days_from) if run_days_from is not None else events

    days_by_title: dict[str, set[date]] = {}
    for event in classify:
        days_by_title.setdefault(_normalise_title(event.title), set()).add(event.day)
    runs = {title for title, days in days_by_title.items() if len(days) > MAX_RUN_DAYS}

    remaining: dict[str, set[date]] = {}
    for event in events:
        title = _normalise_title(event.title)
        if title in runs:
            remaining.setdefault(title, set()).add(event.day)
    keepers: dict[str, date] = {title: min(days) for title, days in remaining.items()}

    kept: list[Event] = []
    dropped = 0
    for event in events:
        title = _normalise_title(event.title)
        if title in keepers and event.day != keepers[title]:
            dropped += 1
            continue
        kept.append(event)
    return kept, dropped


def _sort_key(event: Event) -> tuple[date, str]:
    return event.day, event.start or _NO_TIME_SORT_KEY


# --------------------------------------------------------------------------
# Collecting
# --------------------------------------------------------------------------


def _describe(exc: Exception) -> str:
    """A log-safe account of a failed fetch.

    httpx puts the full request URL into its exception messages, and a
    private calendar's link is its password. Only the status or the error
    class goes into the log; parse errors carry no URL and keep their text.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


def _fetch_ics(client: httpx.Client, url: str) -> str:
    response = client.get(url, headers={"Accept": "text/calendar, */*;q=0.5"})
    response.raise_for_status()
    return response.text


def collect_events(
    days_ahead: Optional[int] = None,
    timeout: Optional[float] = None,
    user_agent: Optional[str] = None,
    today: Optional[date] = None,
    cache_path: Optional[Path | str] = None,
    urls: Optional[Sequence[str]] = None,
) -> tuple[list[Event], list[str]]:
    """Every event in the window, plus the calendars that did not answer live.

    The half of this module that touches the network, and deliberately the
    half that does NOT depend on the time of day: merged, de-duplicated,
    filtered to whole days and sorted. Runs are still expanded and there is no
    limit -- both of those decisions need the clock (see `trim_events`).

    Serving a calendar from the disk cache counts as a failure of that
    calendar even though events come back: it is the only signal the frame
    has that what it prints may be out of date.
    """
    days_ahead = settings.events_days_ahead if days_ahead is None else days_ahead
    timeout = settings.http_timeout_s if timeout is None else timeout
    user_agent = user_agent or settings.user_agent
    today = today or datetime.now(settings.timezone).date()
    urls = list(settings.events_ics_urls if urls is None else urls)
    window_end = today + timedelta(days=days_ahead)

    if not urls:
        return [], []

    failures: list[str] = []
    live: list[list[Event]] = []
    cached: list[list[Event]] = []

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for index, url in enumerate(urls, start=1):
            name = calendar_name(index)
            cache_file = _cache_file(url, cache_path)
            try:
                text = _fetch_ics(client, url)
                events = parse_ics(text, today, window_end)
            except Exception as exc:
                # Everything: timeouts, TLS, 5xx, a body that is not iCalendar.
                # The URL itself is not logged -- secret calendar links are
                # credentials.
                log.warning("events: %s unavailable (%s)", name, _describe(exc))
                failures.append(name)
                text_cached = _read_cache(cache_file)
                if text_cached:
                    try:
                        cached.append(parse_ics(text_cached, today, window_end))
                        log.warning("events: %s served from its cache", name)
                    except ValueError:
                        pass
                continue
            _write_cache(cache_file, text)
            live.append(events)

    merged, duplicates = _merge([*live, *cached])
    merged = [e for e in merged if today <= e.day <= window_end]
    # Sorted here even though trim_events sorts again after the collapse: order
    # does not depend on the time of day, so it belongs in the cached half.
    merged.sort(key=_sort_key)
    log.info(
        "events: %d calendars, %d live, %d cached -> %d in window (%d duplicates)",
        len(urls),
        len(live),
        len(cached),
        len(merged),
        duplicates,
    )
    return merged, failures


def _still_ahead(event: Event, now: datetime) -> bool:
    """Is this event still to come at `now`?

      * A day in the past is gone. Only reachable if a caller trims against a
        later day than it collected for -- which is exactly what a frame built
        just after midnight from a cache filled before it does.
      * On today, an event is dropped only when we KNOW its start has passed.
        `start is None` therefore always survives: an all-day entry, or a
        multi-day entry that began before today and is still going. Throwing a
        festival that is running right now off the panel at 15:00 is the worse
        of the two possible mistakes.

    Strictly earlier, so an event starting at the exact build minute stays.
    The strings are zero-padded "HH:MM", so comparing them as text is
    comparing them as time.
    """
    today = now.date()
    if event.day < today:
        return False
    if event.day > today or event.start is None:
        return True
    return event.start >= now.strftime("%H:%M")


def trim_events(events: Iterable[Event], now: datetime, limit: int) -> list[Event]:
    """What a frame built at `now` should print: the next `limit` events.

    The time-of-day half of this module, kept pure and network-free so that
    build_dashboard can run it on every frame against a cached collection.
    `limit <= 0` means no limit.

    **`now` is compared as local wall-clock time.** `parse_ics` already
    converted every start into the display zone, so only `.date()` and
    `.strftime()` are used here.

    **The cut comes before the collapse, and swapping them loses events.**
    `_collapse_runs` reduces a long run to its EARLIEST day. Collapse first
    and a weekly market whose earliest day was this morning keeps only that
    row -- which the cut then removes, so the market vanishes although it is
    on again tomorrow. **But the collapse still judges the run by its UNCUT
    days** (`run_days_from`), or a three-day series shrinks below the
    threshold as its first day passes and prints twice from the evening on.
    """
    events = list(events)
    ahead = [e for e in events if _still_ahead(e, now)]
    kept, run_rows = _collapse_runs(ahead, run_days_from=events)
    kept.sort(key=_sort_key)
    log.info(
        "events: %d ahead of %s (%d multi-day rows dropped) -> %d printed",
        len(ahead),
        now.strftime("%d.%m. %H:%M"),
        run_rows,
        min(len(kept), limit) if limit > 0 else len(kept),
    )
    return kept[:limit] if limit > 0 else kept


def fetch_events(
    days_ahead: Optional[int] = None,
    limit: Optional[int] = None,
    timeout: Optional[float] = None,
    user_agent: Optional[str] = None,
    today: Optional[date] = None,
    cache_path: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    urls: Optional[Sequence[str]] = None,
) -> tuple[list[Event], list[str]]:
    """collect_events() and trim_events() in one call, for callers with no cache.

    Without `now` the cut is taken from midnight of `today`, which cuts
    nothing. assemble.py does not come through here; it caches the two halves
    apart.
    """
    limit = settings.events_count if limit is None else limit
    today = today or datetime.now(settings.timezone).date()
    if now is None:
        now = datetime.combine(today, time.min, tzinfo=settings.timezone)
    events, failures = collect_events(days_ahead, timeout, user_agent, today, cache_path, urls)
    return trim_events(events, now, limit), failures
