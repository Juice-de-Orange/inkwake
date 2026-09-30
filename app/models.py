"""Shared data contracts.

Every source module returns these types and the renderer consumes only these
types. Nothing else crosses that boundary — a source that reaches into the
renderer, or a renderer that knows a source exists, is a bug.

All dates and times in here are already local to the configured timezone
(`TZ_NAME`). Timezone conversion happens at the edges (the database reader,
the calendar parser, the weather client),
never in the layout code, because a renderer that does arithmetic on times is
a renderer that renders the wrong time twice a year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

# --------------------------------------------------------------------------
# Device -> server
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Telemetry:
    """What the board tells us about itself, carried in request headers.

    Every field is optional because the first request from a factory-fresh
    board carries almost none of them, and a missing battery reading must
    degrade to "unknown" rather than to a crash or to a fake 0 %.
    """

    device_id: str
    battery_voltage_mv: Optional[int] = None
    battery_percent: Optional[int] = None
    is_charging: Optional[bool] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    rssi_dbm: Optional[int] = None
    fw_version: Optional[str] = None
    wake_reason: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Booking:
    """One booked stay, from the optional bookings view.

    Hotel convention: `departure` is the day the place is free again, so the
    last occupied night is `departure - 1 day`. `nights` is precomputed
    because the renderer must never do date arithmetic.
    """

    arrival: date
    departure: date
    nights: int
    name: str
    guests: int
    comment: str = ""


@dataclass(frozen=True)
class Event:
    """One calendar event, from the configured ICS feeds."""

    day: date
    start: Optional[str]  # "HH:MM", or None when the source gives no time
    title: str
    location: str = ""
    source: str = ""


@dataclass(frozen=True)
class Weather:
    """Current-ish conditions plus the rest of today, for the configured location."""

    temp_c: float
    code: int
    description: str
    precip_prob_pct: int
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None


@dataclass(frozen=True)
class SunTimes:
    sunrise: time
    sunset: time


@dataclass(frozen=True)
class Sighting:
    """A wildlife-camera capture with its caption."""

    captured_at: datetime
    caption: str
    species: str = ""
    individual: str = ""
    camera: str = ""
    image_path: Optional[str] = None  # absolute path on disk, already resolved


# --------------------------------------------------------------------------
# Renderer input
# --------------------------------------------------------------------------


@dataclass
class Dashboard:
    """Everything one frame needs. Assembled once, rendered once.

    `failures` names the sources that could not be reached for this frame.
    The renderer turns that into a small badge rather than an error screen:
    on a bistable panel the honest move is to show the stale content and say
    so, not to throw away a good frame because one of five sources timed out.
    """

    now: datetime
    next_refresh: datetime
    telemetry: Telemetry
    weather: Optional[Weather] = None
    sun: Optional[SunTimes] = None
    bookings: list[Booking] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    sighting: Optional[Sighting] = None
    failures: list[str] = field(default_factory=list)
