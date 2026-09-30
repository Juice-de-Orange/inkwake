"""Sunrise and sunset for one calendar day — computed, never fetched.

NOAA Solar Calculator (Astronomical Almanac), ported line by line. Accuracy at
mid latitudes is roughly one minute, which is two orders below anything this panel prints.

Deliberately pure arithmetic: no network, no ephemeris table, no extra
dependency. Sunrise is the single most predictable fact on the dashboard, and
a source that can fail is a source that will fail on the one morning the
uplink is down.

The timezone question lives entirely in the last step. Everything up to
`sun_instants` works in UTC, because the hour angle is a solar quantity and
knows nothing about civil time; only the finished instant is projected into
`tz`. That is what keeps the DST weekend boring — the instants keep drifting
their usual one to two minutes per day, and the local clock shows exactly the
one-hour step the clocks themselves took, not two and not none.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import SunTimes

_RAD = math.pi / 180.0

#: Zenith angle counted as sunrise/sunset: 90 deg plus atmospheric refraction
#: plus half the solar disc. Using a plain 90 puts both events several minutes
#: off, which is more than our whole error budget.
ZENITH_SUNRISE = 90.833

#: Civil twilight, kept because the port is parameterised by zenith anyway.
#: Nothing in models.SunTimes carries it today.
ZENITH_CIVIL = 96.0

_UNIX_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


def _julian_day_noon(day: date) -> float:
    """Julian day number for 12:00 UTC on `day`.

    Noon rather than midnight because the NOAA series are tabulated for the
    middle of the day; anchoring at midnight biases the declination by half a
    day's motion.
    """
    return (day.toordinal() - _UNIX_EPOCH_ORDINAL) + 0.5 + 2440587.5


class _SolarDay:
    """The day's solar constants: declination and the equation of time."""

    __slots__ = ("declination_deg", "eq_time_min")

    def __init__(self, day: date) -> None:
        t = (_julian_day_noon(day) - 2451545.0) / 36525.0

        # Geometric mean longitude and mean anomaly of the sun, in degrees.
        # The modulo differs from JavaScript's for pre-2000 dates, but every
        # use of l0 below is inside sin/cos of a multiple of it, so a 360 deg
        # offset cancels.
        l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
        m_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)

        # Equation of the centre -> true longitude.
        centre = (
            math.sin(_RAD * m_anom) * (1.914602 - t * (0.004817 + 0.000014 * t))
            + math.sin(_RAD * 2 * m_anom) * (0.019993 - 0.000101 * t)
            + math.sin(_RAD * 3 * m_anom) * 0.000289
        )
        true_long = l0 + centre

        # Apparent longitude: nutation and aberration.
        omega = 125.04 - 1934.136 * t
        lam = true_long - 0.00569 - 0.00478 * math.sin(_RAD * omega)

        # Obliquity of the ecliptic, with the same correction term.
        eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
        eps = eps0 + 0.00256 * math.cos(_RAD * omega)

        self.declination_deg = math.asin(math.sin(_RAD * eps) * math.sin(_RAD * lam)) / _RAD

        # Equation of time, in minutes.
        var_y = math.tan(_RAD * (eps / 2)) ** 2
        ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
        self.eq_time_min = (
            4
            * (
                var_y * math.sin(2 * _RAD * l0)
                - 2 * ecc * math.sin(_RAD * m_anom)
                + 4 * ecc * var_y * math.sin(_RAD * m_anom) * math.cos(2 * _RAD * l0)
                - 0.5 * var_y * var_y * math.sin(4 * _RAD * l0)
                - 1.25 * ecc * ecc * math.sin(2 * _RAD * m_anom)
            )
            / _RAD
        )


def _cos_hour_angle(solar: _SolarDay, lat: float, zenith: float) -> float:
    """Cosine of the hour angle at which the sun crosses `zenith`.

    Outside [-1, 1] the crossing never happens: above +1 the sun stays below
    the angle all day (polar night), below -1 it stays above it (polar day).
    Kept as one function because both callers need the same number and two
    copies of this formula would eventually disagree.
    """
    return (
        math.cos(_RAD * zenith) - math.sin(_RAD * lat) * math.sin(_RAD * solar.declination_deg)
    ) / (math.cos(_RAD * lat) * math.cos(_RAD * solar.declination_deg))


def _minutes_after_utc_midnight(
    solar: _SolarDay, lat: float, lon: float, zenith: float, morning: bool
) -> Optional[float]:
    """Minutes after 00:00 UTC at which the sun crosses `zenith`.

    None when it never does. Central Europe never gets there, but a caller that
    passes Tromso deserves an honest answer rather than a domain error out
    of acos().
    """
    cos_h = _cos_hour_angle(solar, lat, zenith)
    if cos_h > 1.0 or cos_h < -1.0:
        return None
    hour_angle = (math.acos(cos_h) / _RAD) * (1.0 if morning else -1.0)
    return 720.0 - 4.0 * (lon + hour_angle) - solar.eq_time_min


def _solar_noon_minutes(solar: _SolarDay, lon: float) -> float:
    """Minutes after 00:00 UTC of local solar noon. Always defined."""
    return 720.0 - 4.0 * lon - solar.eq_time_min


def _instant(day: date, minutes_after_utc_midnight: float) -> datetime:
    """UTC-aware datetime for `day` 00:00 UTC plus the given minutes.

    The offset routinely runs past 24 h or below zero for far-east/far-west
    longitudes; timedelta carries the day over, so the result is a real
    instant and not a wrapped clock reading.
    """
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
        minutes=minutes_after_utc_midnight
    )


def sun_instants(
    day: date,
    lat: float = settings.weather_lat,
    lon: float = settings.weather_lon,
    *,
    zenith: float = ZENITH_SUNRISE,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """(sunrise, sunset) as absolute UTC instants, or None inside the polar circles.

    This is the honest layer: it can say "no sunrise today", which
    models.SunTimes cannot. Anything comparing against a stored timestamp
    (a Sighting.captured_at, say) should use this and skip the civil-time
    round trip entirely.
    """
    solar = _SolarDay(day)
    rise_min = _minutes_after_utc_midnight(solar, lat, lon, zenith, morning=True)
    set_min = _minutes_after_utc_midnight(solar, lat, lon, zenith, morning=False)
    return (
        None if rise_min is None else _instant(day, rise_min),
        None if set_min is None else _instant(day, set_min),
    )


def sun_times(
    day: date,
    lat: float = settings.weather_lat,
    lon: float = settings.weather_lon,
    tz: ZoneInfo = settings.timezone,
) -> SunTimes:
    """Local wall-clock sunrise and sunset for `day`.

    The returned `time` objects are naive and already in `tz`, matching the
    rule in models.py that no renderer ever does timezone arithmetic.

    Polar edge cases are folded into a degenerate answer, because SunTimes has
    no way to express "never": polar night collapses both fields onto solar
    noon (a day of zero length), polar day spans 00:00:00 to 23:59:59. Callers
    that must tell the difference want `sun_instants` instead.
    """
    solar = _SolarDay(day)
    rise_min = _minutes_after_utc_midnight(solar, lat, lon, ZENITH_SUNRISE, morning=True)
    set_min = _minutes_after_utc_midnight(solar, lat, lon, ZENITH_SUNRISE, morning=False)

    if rise_min is None or set_min is None:
        if _cos_hour_angle(solar, lat, ZENITH_SUNRISE) < -1.0:  # sun never sets
            return SunTimes(sunrise=time(0, 0), sunset=time(23, 59, 59))
        noon = _instant(day, _solar_noon_minutes(solar, lon)).astimezone(tz)
        return SunTimes(sunrise=noon.time(), sunset=noon.time())

    return SunTimes(
        sunrise=_instant(day, rise_min).astimezone(tz).time(),
        sunset=_instant(day, set_min).astimezone(tz).time(),
    )
