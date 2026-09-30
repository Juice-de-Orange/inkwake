"""When the board should wake up next.

The device holds no timezone database and does no calendar arithmetic. It
receives a plain seconds countdown and sleeps that long (HARDWARE.md 9.6).
That is a deliberate split: NTP costs up to 4 s of radio-on per wake, which is
comparable to the entire rest of the cycle, and a thin client that never needs
to know the wall-clock time never needs to pay it.

The consequence is that every DST question lives in this file. On the March
transition the gap between the 15:00 and 20:00 slots is one hour shorter, and
on the October transition one hour longer; because we always compute the
countdown from *now* to the next slot expressed in local time, both fall out
of the arithmetic without a special case.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


def _localise(day: date, hh: int, mm: int, tz: ZoneInfo) -> datetime:
    """Build an aware local datetime, surviving both DST discontinuities.

    Two edge cases matter, and neither raises on its own:

      * Spring forward: 02:30 does not exist. zoneinfo silently returns a
        datetime whose UTC projection lands outside the gap, so the slot
        effectively shifts by an hour rather than vanishing.
      * Autumn back: 02:30 happens twice. fold=0 picks the first, which is
        the earlier of the two and keeps the sequence monotonic.

    Callers compare in UTC, so an hour of ambiguity never turns into a
    negative countdown.
    """
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


def iter_slots(
    now: datetime,
    slots: Iterable[tuple[int, int]],
    tz: ZoneInfo,
    days: int = 3,
) -> list[datetime]:
    """All slot instants from the start of today through `days` ahead, sorted.

    Sorting happens on the UTC projection rather than on the naive local
    fields, because during the autumn transition two local times can be out
    of order relative to real elapsed time.
    """
    today = now.astimezone(tz).date()
    out = [
        _localise(today + timedelta(days=offset), hh, mm, tz)
        for offset in range(days)
        for hh, mm in slots
    ]
    return sorted(out, key=lambda d: d.astimezone(tz).timestamp())


def next_slot(
    now: datetime,
    slots: Iterable[tuple[int, int]],
    tz: ZoneInfo,
) -> datetime:
    """The first scheduled slot strictly after `now`."""
    slots = tuple(slots)
    if not slots:
        raise ValueError("no refresh slots configured")

    now_ts = now.timestamp()
    for candidate in iter_slots(now, slots, tz):
        if candidate.timestamp() > now_ts:
            return candidate
    # Unreachable with days=3 and at least one slot, but a silent wrong answer
    # here would strand the device for a day, so fail loudly instead.
    raise RuntimeError(f"no slot found after {now.isoformat()}")


def seconds_until_next(
    now: datetime,
    slots: Iterable[tuple[int, int]],
    tz: ZoneInfo,
    *,
    jitter_s: int = 0,
    minimum_s: int = 60,
    rng: Optional[random.Random] = None,
) -> tuple[int, datetime]:
    """Countdown to the next slot, plus the slot itself.

    `jitter_s` spreads retries so a failing upstream cannot pull every wake
    into lockstep. It is added, never subtracted: waking early would risk
    landing before the slot and burning a whole extra cycle to discover that.

    `minimum_s` is a floor against a pathological zero-or-negative sleep. A
    board that is handed 0 would power-cycle continuously and flatten the
    battery in hours, which is the single most expensive bug this file can
    have.
    """
    target = next_slot(now, slots, tz)
    delta = int((target - now).total_seconds())

    if jitter_s > 0:
        delta += (rng or random).randint(0, jitter_s)

    return max(delta, minimum_s), target


def should_repaint(
    *,
    content_changed: bool,
    last_painted_at: Optional[datetime],
    now: datetime,
    min_gap_s: int,
    max_age_s: int,
    temperature_c: Optional[float],
    min_temp_c: float,
) -> tuple[bool, str]:
    """Decide whether the panel may be repainted, and say why.

    Ordered by severity, because the reasons are not equally negotiable:

      1. Too soon. Vendor guidance is a hard 180 s minimum and the documented
         failure mode is permanent damage (4.4 rule 1). This overrides
         everything, including a genuine content change.
      2. Too cold. Below roughly 15 C the panel develops a colour cast whose
         remedy is hours at room temperature (4.4 rule 4). Skipping costs one
         stale cycle; repainting costs a visibly wrong screen for the rest of
         the day.
      3. Too old. A panel holding one image indefinitely is the documented
         burn-in path (4.4 rule 2), so an unchanged frame still gets forced
         out once a day.
      4. Otherwise: repaint exactly when the content differs.

    Returns (repaint, reason). The reason is logged and, for the skip cases,
    is what the staleness badge explains to whoever is standing in front of
    the panel.
    """
    if last_painted_at is not None:
        elapsed = (now - last_painted_at).total_seconds()
        if elapsed < min_gap_s:
            return False, f"panel-care: only {int(elapsed)}s since last refresh (min {min_gap_s}s)"

    if temperature_c is not None and temperature_c < min_temp_c:
        return False, f"panel-care: {temperature_c:.1f}C is below the {min_temp_c:.0f}C floor"

    if last_painted_at is not None:
        age = (now - last_painted_at).total_seconds()
        if age >= max_age_s:
            return True, f"panel-care: forced daily refresh, image is {int(age / 3600)}h old"

    if content_changed:
        return True, "content changed"

    return False, "content unchanged"
