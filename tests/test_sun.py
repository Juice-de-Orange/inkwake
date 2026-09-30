"""Tests for the NOAA sunrise/sunset port.

Three independent yardsticks, in increasing strictness:

  1. Reality. Open-Meteo's own daily sunrise/sunset for Vienna, queried in
     UTC on 2026-09-30 so no timezone handling of theirs is in the loop. They
     print whole minutes and use their own implementation, so agreement is
     asserted to within 90 seconds.
  2. The original. The TypeScript implementation this module was ported
     from, executed with node 22 on 2026-09-30. The port has to reproduce it
     to the millisecond, which is what turns "looks about right" into "is the
     same calculation".
  3. Behaviour. Monotonicity, symmetry about the equinox, the DST weekends,
     and the polar degenerate cases.

Nothing here touches the network, and test_no_network_access proves it.
"""

from __future__ import annotations

import socket
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import SunTimes
from app.sources.sun import ZENITH_CIVIL, ZENITH_SUNRISE, sun_instants, sun_times

VIENNA = ZoneInfo("Europe/Vienna")
UTC = timezone.utc

#: Vienna, Stephansplatz -- the same example location as the default config.
LAT, LON = 48.2082, 16.3738


def secs(t: time | datetime) -> float:
    """Seconds after local midnight, so deltas read in seconds not in timedelta."""
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def hhmm(text: str) -> float:
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


# --------------------------------------------------------------------------
# 1. Against reality
# --------------------------------------------------------------------------

#: Open-Meteo archive, daily=sunrise,sunset, timezone=UTC, queried 2026-09-30
#: for 48.2082/16.3738. Whole minutes, UTC, DST nowhere near it.
OPEN_METEO_UTC = {
    date(2025, 12, 19): ("06:41", "15:02"),
    date(2025, 12, 21): ("06:42", "15:03"),
    date(2025, 12, 23): ("06:43", "15:04"),
    date(2026, 3, 27): ("04:43", "17:17"),
    date(2026, 3, 29): ("04:39", "17:20"),
    date(2026, 3, 31): ("04:35", "17:23"),
    date(2026, 6, 19): ("02:53", "18:58"),
    date(2026, 6, 21): ("02:54", "18:58"),
    date(2026, 6, 23): ("02:54", "18:58"),
    date(2026, 8, 20): ("03:55", "17:59"),
    date(2026, 8, 22): ("03:58", "17:55"),
}


@pytest.mark.parametrize("day", sorted(OPEN_METEO_UTC))
def test_matches_open_meteo_to_the_minute(day: date) -> None:
    """Whole minutes from a different implementation: 90 s, and nothing more.

    Measured spread on these dates is -34 s to +68 s, so the bound has room for
    their rounding and none for a real error, which would be minutes.
    """
    want_rise, want_set = OPEN_METEO_UTC[day]
    rise, dusk = sun_instants(day, LAT, LON)
    assert rise is not None and dusk is not None
    assert abs(secs(rise.time()) - hhmm(want_rise)) < 90, f"{day} sunrise"
    assert abs(secs(dusk.time()) - hhmm(want_set)) < 90, f"{day} sunset"


def test_vienna_summer_solstice() -> None:
    """21.06.2026 in Vienna: roughly 05:00 to 21:00 CEST.

    Open-Meteo (UTC) and the TypeScript original both put it at 04:54 / 20:58
    local. Asserted here in local time, which is what the panel prints.
    """
    s = sun_times(date(2026, 6, 21), LAT, LON, VIENNA)
    assert abs(secs(s.sunrise) - hhmm("04:54")) <= 180
    assert abs(secs(s.sunset) - hhmm("20:58")) <= 180
    assert hhmm("04:30") <= secs(s.sunrise) <= hhmm("05:15")
    assert hhmm("20:45") <= secs(s.sunset) <= hhmm("21:15")


def test_vienna_winter_solstice() -> None:
    """21.12.2026 in Vienna: roughly 07:45 to 16:00 CET. Original: 07:42 / 16:02."""
    s = sun_times(date(2026, 12, 21), LAT, LON, VIENNA)
    assert abs(secs(s.sunrise) - hhmm("07:42")) <= 180
    assert abs(secs(s.sunset) - hhmm("16:02")) <= 180
    assert hhmm("07:30") <= secs(s.sunrise) <= hhmm("08:00")
    assert hhmm("15:45") <= secs(s.sunset) <= hhmm("16:15")


# --------------------------------------------------------------------------
# 2. Against the original implementation
# --------------------------------------------------------------------------

#: sunTimes() of the TypeScript implementation this module was ported from,
#: run under node 22 on 2026-09-30 for 48.2082/16.3738. ISO UTC, millisecond
#: precision. A drift here means the port stopped being a port.
SUN_TS_GOLDEN = {
    date(2025, 12, 19): ("06:41:28.746", "15:01:57.036"),
    date(2025, 12, 21): ("06:42:34.411", "15:02:50.102"),
    date(2026, 3, 27): ("04:42:39.980", "17:17:00.392"),
    date(2026, 3, 28): ("04:40:36.241", "17:18:27.966"),
    date(2026, 3, 29): ("04:38:32.656", "17:19:55.469"),
    date(2026, 6, 21): ("02:53:59.753", "18:58:39.735"),
    date(2026, 8, 25): ("04:02:49.028", "17:50:24.338"),
    date(2026, 10, 24): ("05:27:44.208", "15:49:34.612"),
    date(2026, 10, 25): ("05:29:15.719", "15:47:48.480"),
    date(2026, 12, 21): ("06:42:26.712", "15:02:43.107"),
}


@pytest.mark.parametrize("day", sorted(SUN_TS_GOLDEN))
def test_reproduces_the_original_implementation(day: date) -> None:
    want_rise, want_set = SUN_TS_GOLDEN[day]
    rise, dusk = sun_instants(day, LAT, LON)
    assert rise is not None and dusk is not None
    # JavaScript Date truncates at the millisecond; 2 ms covers that.
    assert abs(secs(rise.time()) - secs(datetime.strptime(want_rise, "%H:%M:%S.%f"))) < 0.002
    assert abs(secs(dusk.time()) - secs(datetime.strptime(want_set, "%H:%M:%S.%f"))) < 0.002


def test_instants_are_utc_aware() -> None:
    rise, dusk = sun_instants(date(2026, 6, 21), LAT, LON)
    assert rise is not None and dusk is not None
    assert rise.tzinfo is UTC and dusk.tzinfo is UTC
    assert rise < dusk


# --------------------------------------------------------------------------
# 3. Behaviour
# --------------------------------------------------------------------------


def test_returns_the_contract_type() -> None:
    s = sun_times(date(2026, 6, 21), LAT, LON, VIENNA)
    assert isinstance(s, SunTimes)
    assert s.sunrise.tzinfo is None and s.sunset.tzinfo is None


def test_sunrise_precedes_sunset_all_year() -> None:
    day = date(2026, 1, 1)
    while day.year == 2026:
        s = sun_times(day, LAT, LON, VIENNA)
        assert s.sunrise < s.sunset, day
        day += timedelta(days=1)


def test_longest_and_shortest_day_are_the_solstices() -> None:
    def length(day: date) -> float:
        rise, dusk = sun_instants(day, LAT, LON)
        assert rise is not None and dusk is not None
        return (dusk - rise).total_seconds()

    longest = max((date(2026, 6, 1) + timedelta(days=i) for i in range(40)), key=length)
    shortest = min((date(2026, 12, 1) + timedelta(days=i) for i in range(40)), key=length)
    assert abs((longest - date(2026, 6, 21)).days) <= 1
    assert abs((shortest - date(2026, 12, 21)).days) <= 1
    assert length(date(2026, 6, 21)) / 3600 == pytest.approx(16.0, abs=0.2)
    assert length(date(2026, 12, 21)) / 3600 == pytest.approx(8.4, abs=0.2)


def test_equinox_day_is_a_little_over_twelve_hours() -> None:
    """Refraction and the solar disc buy about eight extra minutes, not zero."""
    rise, dusk = sun_instants(date(2026, 3, 20), LAT, LON)
    assert rise is not None and dusk is not None
    assert 12 * 3600 + 300 < (dusk - rise).total_seconds() < 12 * 3600 + 900


def test_civil_twilight_brackets_sunrise() -> None:
    """The zenith parameterisation survived the port even if SunTimes ignores it."""
    assert ZENITH_CIVIL > ZENITH_SUNRISE
    rise, dusk = sun_instants(date(2026, 6, 21), LAT, LON)
    dawn, night = sun_instants(date(2026, 6, 21), LAT, LON, zenith=ZENITH_CIVIL)
    assert dawn is not None and night is not None and rise is not None and dusk is not None
    assert dawn < rise < dusk < night


def test_longitude_shifts_the_clock_the_right_way() -> None:
    """Four minutes per degree, and east is earlier."""
    east, _ = sun_instants(date(2026, 3, 20), LAT, LON + 1.0)
    here, _ = sun_instants(date(2026, 3, 20), LAT, LON)
    assert east is not None and here is not None
    assert (here - east).total_seconds() == pytest.approx(240, abs=5)


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------

#: The two European transitions in 2026: 29.03 (02:00 -> 03:00) and
#: 25.10 (03:00 -> 02:00).
DST_DAYS = [date(2026, 3, 29), date(2026, 10, 25)]


@pytest.mark.parametrize("switch", DST_DAYS)
def test_absolute_instants_do_not_jump_at_a_dst_switch(switch: date) -> None:
    """In real time, sunrise drifts a couple of minutes a day. Always.

    This is the check that catches a timezone bug: `sun_instants` is computed
    entirely in UTC, so a switch weekend must be indistinguishable from any
    other in absolute terms.
    """
    days = [switch + timedelta(days=i) for i in range(-3, 4)]
    rises = [sun_instants(d, LAT, LON)[0] for d in days]
    sets = [sun_instants(d, LAT, LON)[1] for d in days]
    for series, label in ((rises, "sunrise"), (sets, "sunset")):
        for a, b in zip(series, series[1:]):
            assert a is not None and b is not None
            drift = (b - a).total_seconds() - 86400
            assert abs(drift) < 180, f"{label} moved {drift:.0f}s across {switch}"


def test_spring_forward_moves_the_local_clock_by_exactly_one_hour() -> None:
    before = sun_times(date(2026, 3, 28), LAT, LON, VIENNA)
    after = sun_times(date(2026, 3, 29), LAT, LON, VIENNA)
    step = secs(after.sunrise) - secs(before.sunrise)
    # One hour forward, less the ~2 min/day the sun itself moved in March.
    assert 3600 - 300 < step < 3600 + 60


def test_fall_back_moves_the_local_clock_by_exactly_one_hour() -> None:
    before = sun_times(date(2026, 10, 24), LAT, LON, VIENNA)
    after = sun_times(date(2026, 10, 25), LAT, LON, VIENNA)
    step = secs(before.sunrise) - secs(after.sunrise)
    assert 3600 - 300 < step < 3600 + 60


@pytest.mark.parametrize("switch", DST_DAYS)
def test_no_ordinary_day_near_a_switch_jumps(switch: date) -> None:
    """Everything except the switch day itself moves by minutes, not by an hour."""
    for i in (-3, -2, 1, 2):
        a = sun_times(switch + timedelta(days=i - 1), LAT, LON, VIENNA)
        b = sun_times(switch + timedelta(days=i), LAT, LON, VIENNA)
        assert abs(secs(b.sunrise) - secs(a.sunrise)) < 300, switch + timedelta(days=i)


def test_the_timezone_argument_is_the_only_thing_that_moves_the_clock() -> None:
    day = date(2026, 8, 25)
    vienna = sun_times(day, LAT, LON, VIENNA)
    utc = sun_times(day, LAT, LON, ZoneInfo("UTC"))
    assert secs(vienna.sunrise) - secs(utc.sunrise) == pytest.approx(7200, abs=1)


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_polar_day_spans_the_whole_day() -> None:
    """Tromso in June. SunTimes cannot say "never", so it says "all of it"."""
    oslo = ZoneInfo("Europe/Oslo")
    s = sun_times(date(2026, 6, 21), 69.65, 18.96, oslo)
    assert (s.sunrise.hour, s.sunrise.minute) == (0, 0)
    assert (s.sunset.hour, s.sunset.minute) == (23, 59)
    assert sun_instants(date(2026, 6, 21), 69.65, 18.96) == (None, None)


def test_polar_night_collapses_onto_solar_noon() -> None:
    oslo = ZoneInfo("Europe/Oslo")
    s = sun_times(date(2026, 12, 21), 69.65, 18.96, oslo)
    assert s.sunrise == s.sunset
    assert 11 <= s.sunrise.hour <= 12  # solar noon, CET
    assert sun_instants(date(2026, 12, 21), 69.65, 18.96) == (None, None)


def test_equator_is_close_to_six_and_six() -> None:
    """Both events sit within the ~15 min the equation of time can shift them,
    and the day is the 12 h 07 m that refraction makes of a 12 h equinox."""
    s = sun_times(date(2026, 3, 20), 0.0, 0.0, ZoneInfo("UTC"))
    assert abs(secs(s.sunrise) - hhmm("06:00")) < 900
    assert abs(secs(s.sunset) - hhmm("18:00")) < 900
    assert 12 * 3600 + 300 < secs(s.sunset) - secs(s.sunrise) < 12 * 3600 + 600


def test_southern_hemisphere_seasons_are_inverted() -> None:
    def length(day: date) -> float:
        rise, dusk = sun_instants(day, -33.87, 151.21)  # Sydney
        assert rise is not None and dusk is not None
        return (dusk - rise).total_seconds()

    assert length(date(2026, 12, 21)) > length(date(2026, 6, 21))


def test_leap_day_is_ordinary() -> None:
    a = sun_times(date(2028, 2, 28), LAT, LON, VIENNA)
    b = sun_times(date(2028, 2, 29), LAT, LON, VIENNA)
    c = sun_times(date(2028, 3, 1), LAT, LON, VIENNA)
    assert a.sunrise > b.sunrise > c.sunrise


def test_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the module: sunrise must work with the radio off."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("sun.py opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    s = sun_times(date(2026, 6, 21), LAT, LON, VIENNA)
    assert abs(secs(s.sunrise) - hhmm("04:54")) < 60
