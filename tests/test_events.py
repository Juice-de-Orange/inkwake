"""Tests for the iCalendar events source.

No network is touched here. Calendars are served from httpx.MockTransport,
and every calendar in this file is made up: the feeds below are hand-written
to carry one trap each -- a zone to convert, a floating time, an all-day
entry spanning several days, a weekly rule with an EXDATE, one occurrence
moved by a RECURRENCE-ID, a cancelled entry, a malformed one, and entries on
both sides of the window.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import settings
from app.models import Event
from app.sources import events

TODAY = date(2026, 8, 25)  # a Tuesday
WINDOW_END = TODAY + timedelta(days=7)
VIENNA = ZoneInfo("Europe/Vienna")

URL_A = "https://calendar.example.org/club.ics"
URL_B = "https://feeds.example.net/town/events.ics?key=not-a-real-key"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _calendar(*vevents: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//inkwake tests//EN\r\n"
        + "".join(vevents)
        + "END:VCALENDAR\r\n"
    )


def _vevent(uid: str, *lines: str) -> str:
    body = "".join(f"{line}\r\n" for line in (f"UID:{uid}", "DTSTAMP:20260801T000000Z", *lines))
    return f"BEGIN:VEVENT\r\n{body}END:VEVENT\r\n"


CALENDAR_A = _calendar(
    _vevent("a1", "SUMMARY:Sommerkonzert", "LOCATION:Stadtpark",
            "DTSTART;TZID=Europe/Vienna:20260825T193000"),
    # UTC, so 10:00 on the wall.
    _vevent("a2", "SUMMARY:Frühschoppen", "DTSTART:20260826T080000Z"),
    # Floating: no zone at all, which RFC 5545 defines as local time.
    _vevent("a3", "SUMMARY:Lesung", "DTSTART:20260827T180000"),
    # All day, 24.-27. (DTEND is exclusive). Began yesterday, still going.
    _vevent("a4", "SUMMARY:Kunstwoche", "DTSTART;VALUE=DATE:20260824",
            "DTEND;VALUE=DATE:20260828"),
    # Every Tuesday since 4 August, except 1 September.
    _vevent("a5", "SUMMARY:Chorprobe", "DTSTART;TZID=Europe/Vienna:20260804T190000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=Europe/Vienna:20260901T190000"),
    # Five mornings, the third one moved to 09:00.
    _vevent("a6", "SUMMARY:Yoga", "DTSTART;TZID=Europe/Vienna:20260825T070000",
            "RRULE:FREQ=DAILY;COUNT=5"),
    _vevent("a6", "SUMMARY:Yoga", "RECURRENCE-ID;TZID=Europe/Vienna:20260827T070000",
            "DTSTART;TZID=Europe/Vienna:20260827T090000"),
    _vevent("a7", "SUMMARY:Abgesagt", "STATUS:CANCELLED",
            "DTSTART;TZID=Europe/Vienna:20260826T200000"),
    _vevent("a8", "SUMMARY:Viel später", "DTSTART;TZID=Europe/Vienna:20260910T200000"),
    _vevent("a9", "SUMMARY:Gestern", "DTSTART;TZID=Europe/Vienna:20260824T200000"),
    _vevent("a10", "DTSTART;TZID=Europe/Vienna:20260826T120000"),
    _vevent("a11", "SUMMARY:Kaputt", "DTSTART:notadate"),
)

#: A second calendar that repeats one entry of the first in other spelling.
CALENDAR_B = _calendar(
    _vevent("b1", "SUMMARY:sommerkonzert ", "DTSTART;TZID=Europe/Vienna:20260825T193000"),
    _vevent("b2", "SUMMARY:Stadtfest", "LOCATION:Marktplatz",
            "DTSTART;TZID=Europe/Vienna:20260829T140000"),
)


def _parse(text: str) -> list[Event]:
    return sorted(events.parse_ics(text, TODAY, WINDOW_END, VIENNA), key=events._sort_key)


def _rows(result: list[Event]) -> list[tuple[date, str | None, str]]:
    return [(e.day, e.start, e.title) for e in result]


def _titles(result: list[Event]) -> list[str]:
    return [e.title for e in result]


# --------------------------------------------------------------------------
# Transport plumbing
# --------------------------------------------------------------------------


def _route(request: httpx.Request) -> httpx.Response:
    """Serve both calendars."""
    if request.url.host == "calendar.example.org":
        return httpx.Response(200, text=CALENDAR_A)
    return httpx.Response(200, text=CALENDAR_B)


@pytest.fixture
def seen() -> list[httpx.Request]:
    """Every request the module made, in order."""
    return []


@pytest.fixture
def transport(
    monkeypatch: pytest.MonkeyPatch, seen: list[httpx.Request]
) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Install a request handler in place of the real network.

    Patches httpx.Client itself rather than passing a transport in, because
    collect_events owns its client -- that is the point of the module, and a
    test that reached in to hand it one would be testing a different function.
    """
    real_client = httpx.Client

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        def factory(**kwargs: object) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(recording), **kwargs)

        monkeypatch.setattr(events.httpx, "Client", factory)

    return install


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test forgets to install a transport."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("test attempted a real network connection")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture(autouse=True)
def _settings_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the default cache at tmp_path and configure the two calendars.

    Autouse and not optional: the default cache location is settings.data_dir,
    i.e. /data in the container, and a test suite that writes there is a test
    suite with a side effect on the running service.
    """
    monkeypatch.setattr(
        events,
        "settings",
        replace(settings, data_dir=tmp_path, events_ics_urls=(URL_A, URL_B), timezone=VIENNA),
    )
    return tmp_path


def _collect(**kwargs: object) -> tuple[list[Event], list[str]]:
    return events.collect_events(days_ahead=7, today=TODAY, **kwargs)


def _cache_file(tmp_path: Path, url: str) -> Path:
    return events._cache_file(url, tmp_path)


def _write_cache(path: Path, ics: str, age_days: float = 0.0) -> None:
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    path.write_text(json.dumps({"fetched_at": stamp.isoformat(), "ics": ics}), encoding="utf-8")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_zoned_start_keeps_its_wall_clock_time() -> None:
    concert = [e for e in _parse(CALENDAR_A) if e.title == "Sommerkonzert"]
    assert [(e.day, e.start, e.location) for e in concert] == [(TODAY, "19:30", "Stadtpark")]


def test_a_utc_start_is_converted_to_the_display_zone() -> None:
    # 08:00Z in late August is 10:00 in Vienna.
    assert (date(2026, 8, 26), "10:00", "Frühschoppen") in _rows(_parse(CALENDAR_A))


def test_the_same_utc_start_in_winter_is_one_hour_less() -> None:
    text = _calendar(_vevent("w", "SUMMARY:Winter", "DTSTART:20261202T080000Z"))
    got = events.parse_ics(text, date(2026, 12, 1), date(2026, 12, 8), VIENNA)
    assert _rows(got) == [(date(2026, 12, 2), "09:00", "Winter")]


def test_a_floating_start_is_taken_as_local() -> None:
    assert (date(2026, 8, 27), "18:00", "Lesung") in _rows(_parse(CALENDAR_A))


def test_an_all_day_entry_has_no_start_and_covers_every_day_in_the_window() -> None:
    week = [(e.day, e.start) for e in _parse(CALENDAR_A) if e.title == "Kunstwoche"]
    # 24.-27.: the 24th is before the window, DTEND (28th) is exclusive.
    assert week == [(date(2026, 8, 25), None), (date(2026, 8, 26), None), (date(2026, 8, 27), None)]


def test_a_timed_entry_over_several_days_is_on_every_one_of_them() -> None:
    """A weekend festival is the thing happening right now on Saturday.

    Only the first day carries the start time; the rest are "still going",
    which the time-of-day cut keeps all day.
    """
    text = _calendar(
        _vevent("f", "SUMMARY:Wochenendfest",
                "DTSTART;TZID=Europe/Vienna:20260828T180000",
                "DTEND;TZID=Europe/Vienna:20260830T230000"),
        # Ending at midnight does not touch the day it ends on.
        _vevent("n", "SUMMARY:Bis Mitternacht",
                "DTSTART;TZID=Europe/Vienna:20260826T200000",
                "DTEND;TZID=Europe/Vienna:20260827T000000"),
    )
    assert _rows(_parse(text)) == [
        (date(2026, 8, 26), "20:00", "Bis Mitternacht"),
        (date(2026, 8, 28), "18:00", "Wochenendfest"),
        (date(2026, 8, 29), None, "Wochenendfest"),
        (date(2026, 8, 30), None, "Wochenendfest"),
    ]


def test_a_weekly_rule_is_expanded_and_its_exdate_honoured() -> None:
    choir = [(e.day, e.start) for e in _parse(CALENDAR_A) if e.title == "Chorprobe"]
    # 25.08. is in, 01.09. is excluded, the August Tuesdays before are outside.
    assert choir == [(TODAY, "19:00")]


def test_a_moved_occurrence_replaces_the_one_it_names() -> None:
    yoga = [(e.day, e.start) for e in _parse(CALENDAR_A) if e.title == "Yoga"]
    assert yoga == [
        (date(2026, 8, 25), "07:00"),
        (date(2026, 8, 26), "07:00"),
        (date(2026, 8, 27), "09:00"),
        (date(2026, 8, 28), "07:00"),
        (date(2026, 8, 29), "07:00"),
    ]


def test_an_all_day_rule_with_an_all_day_exdate() -> None:
    text = _calendar(
        _vevent("d", "SUMMARY:Täglich", "DTSTART;VALUE=DATE:20260820",
                "RRULE:FREQ=DAILY", "EXDATE;VALUE=DATE:20260826"),
    )
    days = [e.day for e in _parse(text)]
    assert date(2026, 8, 26) not in days
    assert days[0] == TODAY and days[-1] == WINDOW_END


def test_an_unbounded_rule_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "MAX_OCCURRENCES", 10)
    text = _calendar(
        _vevent("h", "SUMMARY:Stündlich", "DTSTART;TZID=Europe/Vienna:20260825T000000",
                "RRULE:FREQ=HOURLY"),
    )
    assert len(_parse(text)) == 10


def test_cancelled_untitled_and_out_of_window_entries_are_left_out() -> None:
    titles = set(_titles(_parse(CALENDAR_A)))
    assert "Abgesagt" not in titles
    assert "Viel später" not in titles
    assert "Gestern" not in titles
    assert "" not in titles


def test_one_malformed_entry_costs_that_entry_not_the_calendar() -> None:
    titles = set(_titles(_parse(CALENDAR_A)))
    assert "Kaputt" not in titles
    assert "Sommerkonzert" in titles


def test_a_malformed_recurrence_id_costs_nothing_else() -> None:
    text = _calendar(
        _vevent("r", "SUMMARY:Ok", "DTSTART;TZID=Europe/Vienna:20260826T100000"),
        _vevent("r2", "SUMMARY:Schief", "RECURRENCE-ID:garbage",
                "DTSTART;TZID=Europe/Vienna:20260826T110000"),
    )
    assert "Ok" in _titles(_parse(text))


def test_whitespace_is_squeezed_but_text_is_not_unescaped_twice() -> None:
    text = _calendar(
        _vevent("s", "SUMMARY:  Jazz   &amp;  Blues\\, live  ",
                "DTSTART;TZID=Europe/Vienna:20260826T200000"),
    )
    # iCalendar escaping (\\,) is undone by the parser; HTML entities are data.
    assert _titles(_parse(text)) == ["Jazz &amp; Blues, live"]


@pytest.mark.parametrize("text", ["", "hello", "<html><body>Login</body></html>"])
def test_something_that_is_not_icalendar_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        events.parse_ics(text, TODAY, WINDOW_END, VIENNA)


def test_an_empty_calendar_is_no_events_not_an_error() -> None:
    assert events.parse_ics(_calendar(), TODAY, WINDOW_END, VIENNA) == []


# --------------------------------------------------------------------------
# Talking to the feeds
# --------------------------------------------------------------------------


def test_no_calendar_configured_means_no_request_and_no_failure(transport, seen) -> None:
    transport(_route)
    assert events.collect_events(days_ahead=7, today=TODAY, urls=()) == ([], [])
    assert seen == []


def test_every_calendar_costs_exactly_one_request(transport, seen) -> None:
    transport(_route)
    _collect()
    assert [str(r.url) for r in seen] == [URL_A, URL_B]


def test_the_request_is_polite(transport, seen) -> None:
    transport(_route)
    _collect(user_agent="inkwake-test/1.0")
    assert seen[0].headers["User-Agent"] == "inkwake-test/1.0"
    assert "text/calendar" in seen[0].headers["Accept"]


def test_calendars_are_merged_and_duplicates_dropped(transport) -> None:
    transport(_route)
    window, failures = _collect()

    assert failures == []
    concerts = [e for e in window if events._normalise_title(e.title) == "sommerkonzert"]
    # The first calendar's spelling wins.
    assert [e.title for e in concerts] == ["Sommerkonzert"]
    assert "Stadtfest" in _titles(window)


def test_collect_events_returns_the_window_sorted(transport) -> None:
    """Sorting is the one ordering decision that does not need a clock.

    It therefore lives in the cached half -- and it is what assemble prints if
    `trim_events` is ever missing or throws. The calendar below is written out
    of order on purpose.
    """
    text = _calendar(
        _vevent("3", "SUMMARY:Spät", "DTSTART;TZID=Europe/Vienna:20260827T200000"),
        _vevent("1", "SUMMARY:Früh", "DTSTART;TZID=Europe/Vienna:20260825T090000"),
        _vevent("2", "SUMMARY:Mitte", "DTSTART;TZID=Europe/Vienna:20260826T120000"),
    )
    transport(lambda request: httpx.Response(200, text=text))

    window, failures = _collect(urls=(URL_A,))

    assert failures == []
    assert _titles(window) == ["Früh", "Mitte", "Spät"]


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


def _calendar_a_down(status: int = 500) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "calendar.example.org":
            return httpx.Response(status, text="nope")
        return _route(request)

    return handler


def test_a_500_costs_that_calendar_and_names_it(transport) -> None:
    transport(_calendar_a_down(500))
    window, failures = _collect()

    assert failures == [events.calendar_name(1)]
    assert "Stadtfest" in _titles(window)
    assert "Yoga" not in _titles(window)


def test_a_timeout_is_a_failure_not_an_exception(transport) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    transport(handler)
    assert _collect() == ([], [events.calendar_name(1), events.calendar_name(2)])


def test_a_login_page_instead_of_a_calendar_is_a_failure(transport) -> None:
    # What an expired secret link typically answers: 200 and HTML.
    transport(lambda request: httpx.Response(200, text="<html><body>Sign in</body></html>"))
    _, failures = _collect(urls=(URL_A,))
    assert failures == [events.calendar_name(1)]


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(200, text="<html>not a calendar</html>"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
    ],
    ids=["http-500", "not-icalendar", "connect-error"],
)
def test_the_secret_url_is_not_logged(transport, caplog: pytest.LogCaptureFixture, handler) -> None:
    """A private calendar's ICS link is its password.

    httpx writes the URL into its exception messages; this module must not
    pass those on. (httpx's own per-request INFO line is silenced in main.py.)
    """
    transport(handler)
    with caplog.at_level("DEBUG", logger="app"):
        _collect(urls=(URL_B,))
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "calendar-1 unavailable" in ours
    assert "not-a-real-key" not in ours


def test_a_calendar_with_no_events_is_not_a_failure(transport) -> None:
    transport(lambda request: httpx.Response(200, text=_calendar()))
    assert _collect() == ([], [])


# --------------------------------------------------------------------------
# The disk cache
# --------------------------------------------------------------------------


def test_a_good_answer_is_cached_under_a_hashed_name(transport, tmp_path) -> None:
    transport(_route)
    _collect()

    cache = _cache_file(tmp_path, URL_B)
    assert cache.is_file()
    # A secret ICS link is a credential; it must not end up in a filename.
    assert "not-a-real-key" not in cache.name
    assert json.loads(cache.read_text(encoding="utf-8"))["ics"] == CALENDAR_B


def test_the_cache_carries_the_panel_when_a_feed_is_gone(transport, tmp_path) -> None:
    _write_cache(_cache_file(tmp_path, URL_A), CALENDAR_A, age_days=1)
    transport(_calendar_a_down(503))

    window, failures = _collect()

    assert "Yoga" in _titles(window)
    # Served from the cache is still reported: it is the only signal the frame
    # has that what it prints may be out of date.
    assert failures == [events.calendar_name(1)]


def test_live_answers_win_over_cached_ones(transport, tmp_path) -> None:
    stale = _calendar(_vevent("x", "SUMMARY:Stadtfest", "LOCATION:Alter Ort",
                              "DTSTART;TZID=Europe/Vienna:20260829T100000"))
    _write_cache(_cache_file(tmp_path, URL_A), stale)
    transport(_calendar_a_down(500))

    window, _ = _collect()
    fest = [e for e in window if e.title == "Stadtfest"]

    assert [(e.start, e.location) for e in fest] == [("14:00", "Marktplatz")]


def test_a_broken_answer_does_not_overwrite_the_cache(transport, tmp_path) -> None:
    cache = _cache_file(tmp_path, URL_A)
    _write_cache(cache, CALENDAR_A)
    transport(lambda request: httpx.Response(200, text="<html>maintenance</html>"))

    _collect(urls=(URL_A,))

    assert json.loads(cache.read_text(encoding="utf-8"))["ics"] == CALENDAR_A


def test_a_cache_older_than_the_cap_is_ignored(transport, tmp_path) -> None:
    _write_cache(_cache_file(tmp_path, URL_A), CALENDAR_A, age_days=events.CACHE_MAX_AGE_DAYS + 1)
    transport(_calendar_a_down(500))

    window, _ = _collect(urls=(URL_A,))

    assert window == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"ics": 42, "fetched_at": "2026-08-25T00:00:00+00:00"}',
        '{"ics": "BEGIN:VCALENDAR", "fetched_at": "yesterday"}',
        '{"ics": "garbage", "fetched_at": "2099-01-01T00:00:00+00:00"}',
    ],
)
def test_an_unusable_cache_is_ignored(transport, tmp_path, content: str) -> None:
    _cache_file(tmp_path, URL_A).write_text(content, encoding="utf-8")
    transport(_calendar_a_down(500))

    assert _collect(urls=(URL_A,)) == ([], [events.calendar_name(1)])


def test_a_cache_that_cannot_be_written_costs_a_cache_not_a_frame(transport, tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    transport(_route)

    window, failures = _collect(cache_path=blocker / "sub")

    assert failures == []
    assert "Yoga" in _titles(window)


def test_the_cache_lives_in_the_data_dir_by_default(tmp_path) -> None:
    assert events._cache_file(URL_A, None).parent == tmp_path


# --------------------------------------------------------------------------
# Normalising, dedup, run collapsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left, right",
    [
        ("Flavourama Performatory", "flavourama  performatory "),
        ("Der Menschenfeind!", "Der Menschenfeind"),
        ("Große Nachtmusik", "Grosse Nachtmusik"),
        ("Für Elise", "Fuer Elise"),
        ("Café Central", "Cafe Central"),
        ("Live im Park | Sommerkino", "Live im Park - Sommerkino"),
    ],
)
def test_titles_that_must_compare_equal(left: str, right: str) -> None:
    assert events._normalise_title(left) == events._normalise_title(right)


def test_titles_that_must_stay_distinct() -> None:
    assert events._normalise_title("Flavourama Voices") != events._normalise_title(
        "Flavourama LOOP"
    )


def test_merge_keeps_the_same_title_on_different_days() -> None:
    a = Event(day=date(2026, 8, 25), start="20:00", title="Theater im Hof")
    b = Event(day=date(2026, 8, 26), start="20:00", title="Theater im Hof")

    merged, dropped = events._merge(([a, b],))

    assert len(merged) == 2
    assert dropped == 0


def test_collapse_runs_keeps_the_next_date_only() -> None:
    # A daily exhibition: three days in, it is a run and owns exactly one row.
    run = [
        Event(day=date(2026, 8, 25 + offset), start="10:00", title="The Living Archive")
        for offset in range(3)
    ]

    kept, dropped = events._collapse_runs(run)

    assert dropped == 2
    assert [e.day for e in kept] == [date(2026, 8, 25)]


def test_collapse_runs_leaves_a_two_night_guest_performance_alone() -> None:
    pair = [
        Event(day=date(2026, 8, 25), start="20:00", title="Der Menschenfeind"),
        Event(day=date(2026, 8, 26), start="20:00", title="Der Menschenfeind"),
    ]

    kept, dropped = events._collapse_runs(pair)

    assert dropped == 0
    assert len(kept) == 2


# --------------------------------------------------------------------------
# The time-of-day cut
# --------------------------------------------------------------------------
#
# `trim_events` is the half of this module that knows what time it is. It runs
# on every frame build against a collection the prewarm filled minutes
# earlier, which is why it is a pure function taking `now` rather than
# something baked into the fetch.

TOMORROW = TODAY + timedelta(days=1)


def _at(hh: int, mm: int, day: date = TODAY) -> datetime:
    """A build time, in the same wall clock the events carry."""
    return datetime.combine(day, dt_time(hh, mm), tzinfo=settings.timezone)


def _run(title: str, days: int, start: str = "10:00") -> list[Event]:
    """One row per day, the way parse_ics expands a series."""
    return [
        Event(day=TODAY + timedelta(days=offset), start=start, title=title)
        for offset in range(days)
    ]


def test_trim_drops_what_has_already_started_today() -> None:
    morning = Event(day=TODAY, start="07:00", title="Bauernmarkt")
    evening = Event(day=TODAY, start="20:00", title="Die Zauberflöte")

    kept = events.trim_events([morning, evening], _at(14, 50), 0)

    assert [e.title for e in kept] == ["Die Zauberflöte"]


def test_trim_keeps_an_event_starting_at_the_build_minute() -> None:
    """Strictly earlier, not earlier-or-equal.

    The frame is painted 15-35 s after it is built and then read for hours; an
    event starting this very minute is the most relevant row on the panel, not
    the least.
    """
    now = Event(day=TODAY, start="14:50", title="Jetzt gleich")

    assert events.trim_events([now], _at(14, 50), 0) == [now]


def test_trim_keeps_an_event_with_no_start_time() -> None:
    """Both sources of `start=None` have to survive, for different reasons.

    A multi-day entry that began before today is happening RIGHT NOW; an
    all-day entry has no time to give, and it could just as well be an evening
    concert. Dropping either would be inventing information.
    """
    running = Event(day=TODAY, start=None, title="Die Seele am Faden")

    assert events.trim_events([running], _at(23, 59), 0) == [running]


def test_trim_leaves_every_later_day_alone() -> None:
    early_tomorrow = Event(day=TOMORROW, start="05:00", title="Schranne")

    assert events.trim_events([early_tomorrow], _at(23, 0), 0) == [early_tomorrow]


def test_trim_drops_a_day_that_is_already_over() -> None:
    """Reachable when a cached collection outlives midnight."""
    yesterday = Event(day=TODAY - timedelta(days=1), start="20:00", title="Gestern")

    assert events.trim_events([yesterday], _at(0, 30), 0) == []


def test_a_run_is_still_one_row_after_its_first_day_has_passed() -> None:
    """The flood guard must judge the run by its whole length, not its rest.

    A three-day series seen on the evening of day one has two days left. Asked
    "is this longer than MAX_RUN_DAYS?" about the REMAINDER, the answer flips
    to no and the series prints twice - on a panel with six lines, and only
    ever at the fullest end of the day. The classification therefore runs on
    the uncut list while the filtering runs on the cut one.
    """
    series = _run("The Living Archive", 3)

    kept = events.trim_events(series, _at(19, 50), 0)

    assert [(e.day, e.title) for e in kept] == [(TOMORROW, "The Living Archive")]


def test_a_collapsed_run_moves_to_its_next_day_instead_of_vanishing() -> None:
    """The keeper is the earliest day LEFT, not the earliest day there was.

    Chosen from the uncut list it would be this morning - a day the cut has
    already removed - and the whole run would disappear from the panel even
    though it is on again tomorrow.
    """
    series = _run("Sommerspielraum", 5)

    kept = events.trim_events(series, _at(14, 50), 0)

    assert [e.day for e in kept] == [TOMORROW]


def test_a_two_night_guest_performance_survives_the_cut_intact() -> None:
    """Two days is not a run, and the cut must not make it one either."""
    pair = [
        Event(day=TODAY, start="20:00", title="Der Menschenfeind"),
        Event(day=TOMORROW, start="20:00", title="Der Menschenfeind"),
    ]

    assert len(events.trim_events(pair, _at(9, 0), 0)) == 2


def test_the_limit_is_applied_after_the_cut_not_before() -> None:
    """Otherwise a busy morning eats the panel.

    Six lines picked first and cut afterwards would leave two rows on a day
    that has plenty still to come.
    """
    day = [
        Event(day=TODAY, start=f"{hour:02d}:00", title=f"Termin {hour}")
        for hour in range(6, 24)
    ]

    kept = events.trim_events(day, _at(14, 50), 6)

    assert len(kept) == 6
    assert [e.start for e in kept] == ["15:00", "16:00", "17:00", "18:00", "19:00", "20:00"]


def test_trim_sorts_timed_before_untimed_within_the_same_day() -> None:
    untimed = Event(day=TODAY, start=None, title="Dauerläufer")
    timed = Event(day=TODAY, start="20:00", title="Konzert")

    kept = events.trim_events([untimed, timed], _at(14, 50), 0)

    assert [e.title for e in kept] == ["Konzert", "Dauerläufer"]


def test_fetch_events_without_now_cuts_nothing(transport) -> None:
    """The default is midnight of `today`, i.e. the behaviour before the cut.

    Not a convenience: it is what makes every other test in this file a
    regression test for the split into collect_events + trim_events.
    """
    transport(_route)

    result, _ = events.fetch_events(days_ahead=7, limit=0, today=TODAY)

    assert (TODAY, "07:00", "Yoga") in _rows(result)


def test_fetch_events_with_now_drops_this_morning(transport) -> None:
    transport(_route)

    result, _ = events.fetch_events(days_ahead=7, limit=0, today=TODAY, now=_at(14, 50))
    today_rows = [(e.start, e.title) for e in result if e.day == TODAY]

    # 07:00 Yoga is gone; the evening and the still-running week are not.
    assert today_rows == [("19:00", "Chorprobe"), ("19:30", "Sommerkonzert"), (None, "Kunstwoche")]


def test_fetch_events_collapses_a_run_and_honours_the_limit(transport) -> None:
    transport(_route)

    result, _ = events.fetch_events(days_ahead=7, limit=0, today=TODAY)
    # Yoga is on five days, so it is a run and keeps one row; the three-day
    # Kunstwoche likewise.
    assert _titles(result).count("Yoga") == 1
    assert _titles(result).count("Kunstwoche") == 1

    limited, _ = events.fetch_events(days_ahead=7, limit=3, today=TODAY)
    assert len(limited) == 3


def test_a_run_that_began_before_the_window_keeps_its_upcoming_row(transport) -> None:
    # Collapsing before filtering would elect the past day as the one row to
    # keep and then drop it as out of window, and the event would vanish.
    text = _calendar(
        _vevent("m", "SUMMARY:Ferienprogramm", "DTSTART;TZID=Europe/Vienna:20260823T100000",
                "RRULE:FREQ=DAILY;COUNT=5"),
    )
    transport(lambda request: httpx.Response(200, text=text))

    result, _ = events.fetch_events(days_ahead=7, limit=0, today=TODAY, urls=(URL_A,))

    assert [(e.day, e.start) for e in result if e.title == "Ferienprogramm"] == [(TODAY, "10:00")]
