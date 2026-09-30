"""Tests for the Open-Meteo client.

Nothing here opens a socket. RECORDED_JSON is a capture of a real call made on
2026-08-25 (forecast_days=2), so the shape being parsed is the shape the API
actually sends, down to the integer percentages and the single
utc_offset_seconds that turns out to matter across a DST switch. Only the
location fields were replaced (with Vienna's); the readings are as recorded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest

from app.models import Weather
from app.sources import weather as weather_mod
from app.sources.weather import (
    WEATHER_CODES,
    describe,
    fetch_weather,
    fetch_weather_async,
    parse_forecast,
)

VIENNA_SUMMER = timezone(timedelta(hours=2))

# --------------------------------------------------------------------------
# Fixture: a real Open-Meteo response, captured 2026-08-25, location replaced
# --------------------------------------------------------------------------

RECORDED_JSON = """
{
 "latitude": 48.2, "longitude": 16.38, "generationtime_ms": 33.31148624420166,
 "utc_offset_seconds": 7200, "timezone": "Europe/Vienna", "timezone_abbreviation": "GMT+2",
 "elevation": 179.0,
 "hourly_units": {"time": "iso8601", "temperature_2m": "\\u00b0C",
                  "precipitation_probability": "%", "weather_code": "wmo code"},
 "hourly": {
  "time": ["2026-08-25T00:00", "2026-08-25T01:00", "2026-08-25T02:00", "2026-08-25T03:00", "2026-08-25T04:00", "2026-08-25T05:00", "2026-08-25T06:00", "2026-08-25T07:00", "2026-08-25T08:00", "2026-08-25T09:00", "2026-08-25T10:00", "2026-08-25T11:00", "2026-08-25T12:00", "2026-08-25T13:00", "2026-08-25T14:00", "2026-08-25T15:00", "2026-08-25T16:00", "2026-08-25T17:00", "2026-08-25T18:00", "2026-08-25T19:00", "2026-08-25T20:00", "2026-08-25T21:00", "2026-08-25T22:00", "2026-08-25T23:00", "2026-08-26T00:00", "2026-08-26T01:00", "2026-08-26T02:00", "2026-08-26T03:00", "2026-08-26T04:00", "2026-08-26T05:00", "2026-08-26T06:00", "2026-08-26T07:00", "2026-08-26T08:00", "2026-08-26T09:00", "2026-08-26T10:00", "2026-08-26T11:00", "2026-08-26T12:00", "2026-08-26T13:00", "2026-08-26T14:00", "2026-08-26T15:00", "2026-08-26T16:00", "2026-08-26T17:00", "2026-08-26T18:00", "2026-08-26T19:00", "2026-08-26T20:00", "2026-08-26T21:00", "2026-08-26T22:00", "2026-08-26T23:00"],
  "temperature_2m": [16.0, 16.0, 15.0, 14.1, 13.4, 12.3, 12.6, 12.8, 14.4, 16.1, 17.7, 19.1, 18.3, 17.6, 18.4, 18.3, 17.9, 17.7, 16.9, 16.5, 16.2, 15.8, 15.4, 14.7, 14.1, 13.6, 13.3, 13.1, 12.9, 12.7, 12.4, 12.7, 13.8, 16.3, 18.2, 20.2, 21.7, 23.1, 24.0, 24.9, 25.7, 25.7, 25.0, 23.8, 21.8, 19.1, 18.7, 18.0],
  "precipitation_probability": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 18, 43, 65, 75, 90, 90, 90, 93, 100, 73, 18, 13, 5, 0, 0, 0, 0, 0, 0, 3, 5, 25, 25, 20, 43, 50, 38, 18, 0, 8, 5, 3, 3, 3, 0, 0, 0, 0],
  "weather_code": [3, 3, 2, 2, 3, 3, 3, 3, 3, 3, 3, 61, 61, 61, 61, 61, 61, 61, 61, 61, 3, 3, 2, 2, 45, 45, 45, 45, 45, 45, 45, 45, 80, 2, 3, 3, 3, 2, 2, 1, 0, 3, 1, 3, 3, 3, 3, 3]
 },
 "daily_units": {"time": "iso8601", "temperature_2m_min": "\\u00b0C", "temperature_2m_max": "\\u00b0C"},
 "daily": {"time": ["2026-08-25", "2026-08-26"],
           "temperature_2m_min": [12.3, 12.4],
           "temperature_2m_max": [19.1, 25.7]}
}
"""


@pytest.fixture()
def payload() -> dict[str, Any]:
    return json.loads(RECORDED_JSON)


def at(hour: int, minute: int = 0, day: int = 25) -> datetime:
    """A local (CEST) instant on the recorded day."""
    return datetime(2026, 8, day, hour, minute, tzinfo=VIENNA_SUMMER)


# --------------------------------------------------------------------------
# Hour selection
# --------------------------------------------------------------------------


def test_picks_the_hour_on_the_hour(payload: dict[str, Any]) -> None:
    """A 15:00 wake reads the 15:00 row, not the 16:00 one."""
    w = parse_forecast(payload, at(15, 0))
    assert w is not None
    assert (w.temp_c, w.code, w.precip_prob_pct) == (18.3, 61, 90)


@pytest.mark.parametrize(
    "minute, expected_temp",
    [
        (0, 18.3),   # exactly 15:00
        (5, 18.3),   # just past: still the 15:00 row
        (29, 18.3),
        (31, 17.9),  # past the midpoint: the 16:00 row
        (59, 17.9),
    ],
)
def test_rounds_to_the_nearest_full_hour(
    payload: dict[str, Any], minute: int, expected_temp: float
) -> None:
    w = parse_forecast(payload, at(15, minute))
    assert w is not None
    assert w.temp_c == expected_temp


def test_all_three_wake_slots_resolve(payload: dict[str, Any]) -> None:
    """07:00, 15:00 and 20:00 are the only times that matter in production."""
    expected = {7: (12.8, 3, 0), 15: (18.3, 61, 90), 20: (16.2, 3, 18)}
    for hour, want in expected.items():
        w = parse_forecast(payload, at(hour))
        assert w is not None, hour
        assert (w.temp_c, w.code, w.precip_prob_pct) == want, hour


def test_before_the_window_clamps_to_the_first_row(payload: dict[str, Any]) -> None:
    """A clock skewed into yesterday must still yield a frame, not None."""
    w = parse_forecast(payload, at(23, 0, day=24))
    assert w is not None
    assert w.temp_c == 16.0  # 2026-08-25T00:00


def test_after_the_window_clamps_to_the_last_row(payload: dict[str, Any]) -> None:
    w = parse_forecast(payload, at(12, 0, day=28))
    assert w is not None
    assert w.temp_c == 18.0  # 2026-08-26T23:00


# --------------------------------------------------------------------------
# Daily min/max
# --------------------------------------------------------------------------


def test_daily_range_comes_from_the_matching_day(payload: dict[str, Any]) -> None:
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert (w.temp_min_c, w.temp_max_c) == (12.3, 19.1)


def test_daily_range_follows_the_hour_over_midnight(payload: dict[str, Any]) -> None:
    """23:40 picks tomorrow's 00:00 row, so it must pick tomorrow's range too.

    Positional indexing into `daily` would have returned 2026-08-25 here.
    """
    w = parse_forecast(payload, at(23, 40))
    assert w is not None
    assert w.temp_c == 14.1  # 2026-08-26T00:00
    assert (w.temp_min_c, w.temp_max_c) == (12.4, 25.7)


def test_missing_daily_block_is_not_fatal(payload: dict[str, Any]) -> None:
    """Optional fields exist so we can admit not knowing."""
    payload.pop("daily")
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.temp_min_c is None and w.temp_max_c is None
    assert w.temp_c == 18.3


def test_daily_block_without_a_matching_date_yields_none(payload: dict[str, Any]) -> None:
    payload["daily"]["time"] = ["2020-01-01", "2020-01-02"]
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.temp_min_c is None and w.temp_max_c is None


# --------------------------------------------------------------------------
# WMO code table
# --------------------------------------------------------------------------


def test_umlauts_survive() -> None:
    """The LED-matrix transliteration must not have come along for the ride."""
    assert WEATHER_CODES[2] == "Teilw. bewölkt"
    assert WEATHER_CODES[3] == "Bewölkt"
    assert "bewoelkt" not in " ".join(WEATHER_CODES.values()).lower()


def test_code_table_covers_every_wmo_code_open_meteo_emits() -> None:
    emitted = {
        0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
        71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
    }
    assert emitted <= set(WEATHER_CODES)


def test_descriptions_fit_the_narrow_column() -> None:
    """400 px wide panel: a 30-character description has nowhere to go."""
    assert max(len(v) for v in WEATHER_CODES.values()) <= 22


def test_unknown_code_is_labelled_not_blanked() -> None:
    assert describe(1234) == "Unbekannt"


def test_description_matches_the_selected_code(payload: dict[str, Any]) -> None:
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.description == "Leichter Regen"


# --------------------------------------------------------------------------
# Damaged payloads: every one of these must degrade, never raise
# --------------------------------------------------------------------------


def test_null_rows_are_skipped_not_faked(payload: dict[str, Any]) -> None:
    """Open-Meteo nulls rows past its horizon. Code 0 would print "Klar"."""
    payload["hourly"]["weather_code"][15] = None
    payload["hourly"]["temperature_2m"][16] = None
    w = parse_forecast(payload, at(15, 0))
    assert w is not None
    assert w.temp_c == 18.4  # 15:00 and 16:00 are unusable, 14:00 is nearest
    assert w.code == 61


def test_missing_precipitation_probability_becomes_zero(payload: dict[str, Any]) -> None:
    payload["hourly"]["precipitation_probability"][15] = None
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.precip_prob_pct == 0


def test_short_precipitation_column_becomes_zero(payload: dict[str, Any]) -> None:
    payload["hourly"]["precipitation_probability"] = [0, 0, 0]
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.precip_prob_pct == 0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.clear(), id="empty-object"),
        pytest.param(lambda p: p.pop("hourly"), id="no-hourly"),
        pytest.param(lambda p: p["hourly"].update(time=[]), id="empty-time"),
        pytest.param(lambda p: p["hourly"].update(temperature_2m=[]), id="empty-temps"),
        pytest.param(
            lambda p: p["hourly"].update(time="not-a-list"), id="time-not-a-list"
        ),
        pytest.param(
            lambda p: p["hourly"]["time"].__setitem__(0, "gestern"), id="bad-timestamp"
        ),
        pytest.param(
            lambda p: p["hourly"].update(
                temperature_2m=[None] * 48, weather_code=[None] * 48
            ),
            id="all-null",
        ),
    ],
)
def test_broken_payload_returns_none(
    payload: dict[str, Any], mutate: Callable[[dict[str, Any]], Any]
) -> None:
    mutate(payload)
    assert parse_forecast(payload, at(15)) is None


def test_non_numeric_temperature_returns_none(payload: dict[str, Any]) -> None:
    payload["hourly"]["temperature_2m"][15] = "warm"
    assert parse_forecast(payload, at(15)) is None


# --------------------------------------------------------------------------
# Timestamps: the single fixed offset, not a DST-aware zone
# --------------------------------------------------------------------------


def test_labels_are_read_with_the_reported_offset(payload: dict[str, Any]) -> None:
    """15:00 with utc_offset_seconds=7200 is 13:00 UTC, so a 13:05 UTC now hits it."""
    w = parse_forecast(payload, datetime(2026, 8, 25, 13, 5, tzinfo=timezone.utc))
    assert w is not None
    assert w.temp_c == 18.3


def test_offset_is_honoured_even_when_it_contradicts_the_zone_name() -> None:
    """Open-Meteo keeps one offset for the whole window; a DST-aware zone would
    re-apply an hour that was never applied. Verified against the live API on
    2026-08-25 with America/Santiago across Chile's switch: 24 rows per day,
    utc_offset_seconds constant at -14400.
    """
    payload = {
        "utc_offset_seconds": -14400,
        "timezone": "America/Santiago",
        "hourly": {
            "time": ["2026-09-06T09:00", "2026-09-06T10:00"],
            "temperature_2m": [7.0, 9.0],
            "precipitation_probability": [10, 20],
            "weather_code": [1, 2],
        },
    }
    # 09:00 at UTC-4 is 13:00 UTC. Had we attached ZoneInfo("America/Santiago"),
    # which is UTC-3 on that date, the same label would resolve to 12:00 UTC and
    # this now would have selected the 10:00 row instead.
    w = parse_forecast(payload, datetime(2026, 9, 6, 13, 10, tzinfo=timezone.utc))
    assert w is not None
    assert w.temp_c == 7.0


def test_implausible_offset_falls_back_to_the_configured_zone(
    payload: dict[str, Any],
) -> None:
    payload["utc_offset_seconds"] = 999_999
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.temp_c == 18.3  # Europe/Vienna is +02:00 on 2026-08-25 either way


def test_naive_now_is_rejected_not_silently_misread(payload: dict[str, Any]) -> None:
    """A naive `now` would be read as the host's local time. Refuse it instead."""
    with pytest.raises(ValueError):
        parse_forecast(payload, datetime(2026, 8, 25, 15, 0))


def test_fetch_survives_a_parse_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outer guard: no payload may turn into an exception at the seam."""
    _mock_transport(monkeypatch, _ok)
    monkeypatch.setattr(
        weather_mod, "parse_forecast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert fetch_weather() is None


# --------------------------------------------------------------------------
# Transport: request shape and every failure mode
# --------------------------------------------------------------------------


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route both httpx clients through `handler`; return the captured requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client, real_async = httpx.Client, httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(record), **kwargs)

    def async_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_async(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(weather_mod.httpx, "Client", client)
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", async_client)
    return seen


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=json.loads(RECORDED_JSON))


def test_fetch_returns_a_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, _ok)
    w = fetch_weather()
    assert isinstance(w, Weather)
    assert w.description == describe(w.code)


def test_request_asks_for_what_the_renderer_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_transport(monkeypatch, _ok)
    fetch_weather(lat=48.2082, lon=16.3738, user_agent="inkwake/test")
    assert len(seen) == 1
    q = seen[0].url.params
    assert q["latitude"] == "48.2082" and q["longitude"] == "16.3738"
    assert q["timezone"] == "Europe/Vienna"
    assert "temperature_2m" in q["hourly"]
    assert "precipitation_probability" in q["hourly"]
    assert "weather_code" in q["hourly"]
    assert "temperature_2m_min" in q["daily"]
    assert "temperature_2m_max" in q["daily"]
    # One day is not enough: at 23:40 the nearest full hour is tomorrow's 00:00.
    assert int(q["forecast_days"]) >= 2
    assert seen[0].headers["user-agent"] == "inkwake/test"


def test_timeout_is_passed_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        seen.update(kwargs)
        return real_client(transport=httpx.MockTransport(_ok), **kwargs)

    monkeypatch.setattr(weather_mod.httpx, "Client", client)
    fetch_weather(timeout=4.25)
    assert seen["timeout"] == 4.25


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503])
def test_http_error_returns_none(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(status, text="nope"))
    assert fetch_weather() is None


def test_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _mock_transport(monkeypatch, boom)
    assert fetch_weather() is None


def test_connection_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _mock_transport(monkeypatch, boom)
    assert fetch_weather() is None


def test_non_json_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, text="<html>502</html>"))
    assert fetch_weather() is None


def test_json_that_is_not_an_object_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, json=[1, 2, 3]))
    assert fetch_weather() is None


def test_async_matches_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, _ok)
    assert asyncio.run(fetch_weather_async()) == fetch_weather()


def test_async_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, lambda r: httpx.Response(503))
    assert asyncio.run(fetch_weather_async()) is None


def test_junk_daily_values_cost_the_line_not_the_frame(payload: dict[str, Any]) -> None:
    payload["daily"]["temperature_2m_min"] = ["kalt", 12.4]
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.temp_min_c is None
    assert w.temp_max_c == 19.1


def test_precipitation_probability_is_clamped(payload: dict[str, Any]) -> None:
    payload["hourly"]["precipitation_probability"][15] = 1000
    w = parse_forecast(payload, at(15))
    assert w is not None
    assert w.precip_prob_pct == 100
