"""Open-Meteo client for the forecast panel.

No API key, no account, no rate-limit header to respect at three requests a
day. The location comes from WEATHER_LAT / WEATHER_LON.

The German WMO code table keeps its umlauts: an LED matrix without an "ö"
glyph might have to write "Bewoelkt", this panel renders real type and a
reader would notice.

Nothing in here raises. A frame with four of five sources is the right answer;
a frame that never renders because Open-Meteo returned a 502 is not. Every
failure path lands on None, and the caller records the source name in
Dashboard.failures.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional, Sequence

import httpx

from app.config import settings
from app.models import Weather

log = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARIABLES = "temperature_2m,precipitation_probability,weather_code"
DAILY_VARIABLES = "temperature_2m_min,temperature_2m_max"

#: Two days, not one. The hourly block starts at 00:00 local, so a frame
#: rendered at 23:40 has to reach into tomorrow to find any hour at all.
FORECAST_DAYS = 2

#: WMO 4677 weather codes in German. Short enough for a narrow column: this panel is 400 px wide and a
#: description longer than ~18 characters gets ellipsised by the layout.
#: The table is complete for the codes Open-Meteo emits.
WEATHER_CODES: dict[int, str] = {
    0: "Klar",
    1: "Meist klar",
    2: "Teilw. bewölkt",
    3: "Bewölkt",
    45: "Nebel",
    48: "Nebel",
    51: "Leichter Niesel",
    53: "Niesel",
    55: "Starker Niesel",
    56: "Eisniesel",
    57: "Eisniesel",
    61: "Leichter Regen",
    63: "Regen",
    65: "Starkregen",
    66: "Eisregen",
    67: "Eisregen",
    71: "Leichter Schnee",
    73: "Schnee",
    75: "Starker Schnee",
    77: "Schneekorn",
    80: "Regenschauer",
    81: "Schauer",
    82: "Starke Schauer",
    85: "Schneeschauer",
    86: "Starke Schneeschauer",
    95: "Gewitter",
    96: "Gewitter+Hagel",
    99: "Gewitter+Hagel",
}

#: Shown when Open-Meteo invents a code we have no wording for. Better than an
#: empty string, which the layout would render as a blank line that looks like
#: a bug rather than like missing data.
UNKNOWN_DESCRIPTION = "Unbekannt"


def describe(code: int) -> str:
    """German wording for a WMO weather code."""
    return WEATHER_CODES.get(code, UNKNOWN_DESCRIPTION)


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


def _params(lat: float, lon: float) -> dict[str, Any]:
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": HOURLY_VARIABLES,
        "daily": DAILY_VARIABLES,
        # Ask Open-Meteo to localise. The alternative - fetching UTC and
        # converting here - would put DST arithmetic in a source module, which
        # models.py explicitly puts at the edges instead.
        "timezone": settings.timezone.key,
        "forecast_days": FORECAST_DAYS,
    }


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept": "application/json"}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _payload_tzinfo(payload: dict[str, Any]) -> tzinfo:
    """The offset the hourly labels are actually expressed in.

    A fixed offset, deliberately, and not ZoneInfo(payload["timezone"]).

    Open-Meteo does not put civil time in those labels. Measured against the
    live API on 2026-08-25: asking for America/Santiago over the 16 days that
    contain Chile's DST switch returns exactly 24 x 16 hourly rows with a
    single utc_offset_seconds of -14400 - no doubled hour, no missing hour.
    The labels are therefore UTC plus one constant offset, and after the
    switch they sit an hour off the local clock.

    Reading them back with that same constant recovers the exact UTC instant
    of every row, which is the only thing "which hour is nearest to now"
    needs. Attaching ZoneInfo instead would re-apply DST that was never
    applied and put half the switch day an hour wrong.
    """
    offset = payload.get("utc_offset_seconds")
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        if -86400 < offset < 86400:
            return timezone(timedelta(seconds=int(offset)))
        log.warning("open-meteo returned an implausible utc_offset_seconds %r", offset)
    return settings.timezone


def _column(block: Any, key: str) -> Sequence[Any]:
    values = block.get(key) if isinstance(block, dict) else None
    return values if isinstance(values, list) else []


def _pick_hour(
    stamps: Sequence[datetime], temps: Sequence[Any], codes: Sequence[Any], now: datetime
) -> Optional[int]:
    """Index of the full hour closest to `now`, skipping unusable rows.

    "Closest to now", not "the next one": at a 15:00 wake both readings agree,
    and off-slot a render at 15:05 should show 15:00 rather than jump an hour
    ahead of the person standing in front of the panel.

    A row missing either the temperature or the code is not a candidate.
    Open-Meteo nulls trailing rows at the edge of its horizon, and a fabricated
    code 0 would print "Klar" over a thunderstorm.
    """
    usable = [
        i
        for i in range(min(len(stamps), len(temps), len(codes)))
        if temps[i] is not None and codes[i] is not None
    ]
    if not usable:
        return None
    return min(usable, key=lambda i: abs((stamps[i] - now).total_seconds()))


def _daily_range(
    payload: dict[str, Any], day_iso: str
) -> tuple[Optional[float], Optional[float]]:
    """Today's min/max, matched by date rather than by position.

    Positional indexing would silently return tomorrow's range whenever the
    chosen hour has rolled past midnight. Unmatched stays None, because
    Weather.temp_min_c is Optional precisely so we can admit not knowing.
    """
    daily = payload.get("daily")
    days = _column(daily, "time")
    mins = _column(daily, "temperature_2m_min")
    maxs = _column(daily, "temperature_2m_max")
    def number(column: Sequence[Any], i: int) -> Optional[float]:
        if i >= len(column) or column[i] is None:
            return None
        try:
            return float(column[i])
        except (TypeError, ValueError):
            # A junk min/max costs a line of the layout, not the frame.
            return None

    for i, d in enumerate(days):
        if d == day_iso:
            return number(mins, i), number(maxs, i)
    return None, None


def parse_forecast(payload: dict[str, Any], now: Optional[datetime] = None) -> Optional[Weather]:
    """Turn one Open-Meteo response into a Weather, or None if it is unusable.

    Split out from the request so the tests can feed a recorded response and
    a fixed `now` without a socket anywhere in sight.
    """
    tz = _payload_tzinfo(payload)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        # datetime.astimezone() would quietly reinterpret a naive value as the
        # *host's* local time. That happens to be right on a server set to the
        # display's zone and wrong in every container without TZ set, which is the worst kind of bug:
        # correct in production until the day it is not.
        raise ValueError("parse_forecast needs an aware `now`")
    else:
        now = now.astimezone(tz)

    hourly = payload.get("hourly")
    raw_times = _column(hourly, "time")
    temps = _column(hourly, "temperature_2m")
    probs = _column(hourly, "precipitation_probability")
    codes = _column(hourly, "weather_code")

    stamps: list[datetime] = []
    for raw in raw_times:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            log.warning("open-meteo returned an unparsable timestamp %r", raw)
            return None
        # Labels are naive by contract. Honour an explicit offset if the API
        # ever starts sending one rather than silently overwriting it.
        stamps.append(parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz))

    index = _pick_hour(stamps, temps, codes, now)
    if index is None:
        log.warning("open-meteo response carried no usable hourly reading")
        return None

    try:
        temp_c = float(temps[index])
        code = int(codes[index])
    except (TypeError, ValueError):
        log.warning("open-meteo hourly row %d is not numeric", index)
        return None

    prob = probs[index] if index < len(probs) else None
    try:
        # Clamped, because the layout sizes this field for three characters
        # and a stray 1000 would push the rest of the row off a 400 px panel.
        precip_prob_pct = 0 if prob is None else max(0, min(100, int(round(float(prob)))))
    except (TypeError, ValueError):
        precip_prob_pct = 0

    temp_min_c, temp_max_c = _daily_range(payload, stamps[index].date().isoformat())

    return Weather(
        temp_c=temp_c,
        code=code,
        description=describe(code),
        precip_prob_pct=precip_prob_pct,
        temp_min_c=temp_min_c,
        temp_max_c=temp_max_c,
    )


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def fetch_weather(
    lat: float = settings.weather_lat,
    lon: float = settings.weather_lon,
    timeout: float = settings.http_timeout_s,
    user_agent: str = settings.user_agent,
) -> Optional[Weather]:
    """Current-ish conditions for (lat, lon), or None if Open-Meteo let us down.

    None is the whole error protocol. There is no exception to catch upstream
    and no partial Weather to interpret - the caller adds "weather" to
    Dashboard.failures and the frame renders without the panel.
    """
    try:
        with httpx.Client(timeout=timeout, headers=_headers(user_agent)) as client:
            response = client.get(API_URL, params=_params(lat, lon))
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning("weather fetch failed: %s: %s", type(exc).__name__, exc)
        return None

    if not isinstance(payload, dict):
        log.warning("weather fetch returned %s, expected an object", type(payload).__name__)
        return None

    try:
        return parse_forecast(payload)
    except Exception as exc:
        # Defence in depth. parse_forecast is written not to raise, but a
        # malformed payload must never be the reason the whole frame dies.
        log.warning("weather parse failed: %s: %s", type(exc).__name__, exc)
        return None


async def fetch_weather_async(
    lat: float = settings.weather_lat,
    lon: float = settings.weather_lon,
    timeout: float = settings.http_timeout_s,
    user_agent: str = settings.user_agent,
) -> Optional[Weather]:
    """Async twin of fetch_weather, for gathering all sources concurrently.

    Same contract, same None-on-failure. Exists so a FastAPI request handler
    can fan out to five upstreams without blocking the event loop on the
    slowest one.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_headers(user_agent)) as client:
            response = await client.get(API_URL, params=_params(lat, lon))
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning("weather fetch failed: %s: %s", type(exc).__name__, exc)
        return None

    if not isinstance(payload, dict):
        log.warning("weather fetch returned %s, expected an object", type(payload).__name__)
        return None

    try:
        return parse_forecast(payload)
    except Exception as exc:
        log.warning("weather parse failed: %s: %s", type(exc).__name__, exc)
        return None


__all__ = [
    "API_URL",
    "WEATHER_CODES",
    "describe",
    "fetch_weather",
    "fetch_weather_async",
    "parse_forecast",
]
