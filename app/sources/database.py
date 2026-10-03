"""Bookings and wildlife-camera sightings, read from two Postgres views.

Optional. Set `SOURCES_DSN` to a read-only Postgres connection and create two
views with the columns below; leave the DSN empty and both panels stay empty
without a failure. The views are the whole contract -- whatever schema your
booking tool or camera pipeline uses, a view maps it onto these columns
(`docs/data-sources.md` has examples):

  BOOKINGS_VIEW   (default `inkwake_bookings`)
      arrival_date    date      first night
      departure_date  date      the day the place is free again (hotel convention)
      name            text      what the panel prints for the booking
      guest_count     integer
      comment         text      optional, may be NULL

  SIGHTINGS_VIEW  (default `inkwake_sightings`)
      captured_at     timestamptz
      caption         text      printed under the photo
      species         text      may be ''
      individual      text      may be ''
      camera          text      may be ''
      image_path      text      relative to SIGHTINGS_DIR, may be NULL

Two panels come out of one database, so they share a connection helper and
nothing else. Both entry points are total functions: they return an empty
result rather than raising, because one unreachable database must cost us a
panel, never the frame.

The date trap this module exists to avoid
-----------------------------------------
A database often runs in UTC while the panel lives in local time. Between
00:00 and 02:00 local time `CURRENT_DATE` on such a server is still yesterday,
so a booking that ended last night would reappear on the early frame twice a
year -- and the bug would only ever show up at night, when nobody is looking.
Every date comparison therefore uses the `today` the caller computed in local
time and passes in as a bind parameter. There is deliberately no `now()` or
`CURRENT_DATE` anywhere in the SQL below, and a test asserts that.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ..config import settings
from ..models import Booking, Sighting

log = logging.getLogger(__name__)

#: Names this module contributes to Dashboard.failures. They match the
#: Dashboard field they leave empty, so the badge can say which panel is stale.
BOOKINGS_FAILURE = "bookings"
SIGHTING_FAILURE = "sighting"

#: Seconds to wait for the TCP handshake and the server greeting.
_CONNECT_TIMEOUT_S = 5
#: Milliseconds a single statement may run. connect_timeout does not cover a
#: connection that is established and then never answers, which is exactly what
#: a locked-up database looks like from here.
_STATEMENT_TIMEOUT_MS = 5000

# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

#: A stay is still current while departure is strictly after today -- a
#: booking departing this morning is over. Which bookings count at all
#: (accepted, confirmed, not cancelled) is the view's business, not ours.
BOOKINGS_SQL = """
    SELECT arrival_date,
           departure_date,
           (departure_date - arrival_date)                    AS nights,
           name,
           guest_count,
           comment
      FROM {view}
     WHERE departure_date > %(today)s
     ORDER BY arrival_date ASC, departure_date ASC, name ASC
     LIMIT %(limit)s
"""

#: The newest capture. The view decides what is publishable.
SIGHTING_SQL = """
    SELECT captured_at, camera, species, individual, caption, image_path
      FROM {view}
     ORDER BY captured_at DESC
     LIMIT 1
"""


def _view(name: str) -> sql.Composable:
    """A possibly schema-qualified view name, quoted as identifiers.

    The name comes from configuration, not from a request, but it still goes
    through `sql.Identifier` rather than string formatting: a view called
    `x; DROP TABLE y` must be a missing view, not a second statement.
    """
    parts = [part for part in (name or "").strip().split(".") if part]
    if not parts or len(parts) > 2:
        raise ValueError(f"invalid view name {name!r}")
    return sql.Identifier(*parts)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


@contextmanager
def _connect(dsn: str) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A short-lived, read-only session on the source database.

    `read_only` alone is a comment, not a guarantee: psycopg only puts it on
    the wire as part of a BEGIN, and in autocommit mode there is no BEGIN
    unless an explicit transaction block asks for one. Both halves together
    are what makes the server refuse a write, which is the point - this
    process has no business changing anything in someone else's data.
    """
    conn: psycopg.Connection[dict[str, Any]] = psycopg.connect(
        dsn,
        connect_timeout=_CONNECT_TIMEOUT_S,
        autocommit=True,
        row_factory=dict_row,
    )
    try:
        conn.read_only = True
        # SET takes no bind parameters, hence the interpolated int constant.
        conn.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def _note(failures: Optional[list[str]], name: str) -> None:
    if failures is not None and name not in failures:
        failures.append(name)


# --------------------------------------------------------------------------
# Bookings
# --------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    """One line for the log. A database that is down is asked on every render,
    and a full psycopg traceback each time buries everything else; libpq also
    answers in several lines. Type and message say what an operator can act on."""
    return f"{type(exc).__name__}: {' '.join(str(exc).split())}"


def fetch_bookings(
    dsn: str,
    limit: int,
    today: date,
    *,
    view: str = "inkwake_bookings",
    failures: Optional[list[str]] = None,
) -> list[Booking]:
    """The next `limit` accepted stays that are not over yet.

    `today` must already be the local date - see the module docstring.
    An empty DSN means the feature is switched off and yields an empty list
    without a failure entry; a database that is merely unreachable yields the
    same empty list plus `BOOKINGS_FAILURE` in `failures`, so the caller can
    tell "nothing booked" from "could not ask".
    """
    if not dsn or not dsn.strip() or limit <= 0:
        return []

    try:
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(BOOKINGS_SQL).format(view=_view(view)),
                    {"today": today, "limit": limit},
                )
                rows = cur.fetchall()
            return [_to_booking(row) for row in rows]
    except Exception as exc:
        # Includes the mapping step: a row we cannot read means the schema
        # moved under us, and half a booking list is worse than none.
        log.warning("database: bookings unavailable (%s)", _describe(exc))
        _note(failures, BOOKINGS_FAILURE)
        return []


def _to_booking(row: dict[str, Any]) -> Booking:
    arrival: date = row["arrival_date"]
    departure: date = row["departure_date"]
    nights = row.get("nights")
    return Booking(
        arrival=arrival,
        departure=departure,
        nights=int(nights) if nights is not None else (departure - arrival).days,
        name=str(row["name"] or "").strip(),
        guests=int(row["guest_count"]),
        comment=str(row.get("comment") or "").strip(),
    )


# --------------------------------------------------------------------------
# Sightings
# --------------------------------------------------------------------------


def fetch_latest_sighting(
    dsn: str,
    sightings_dir: Path,
    *,
    view: str = "inkwake_sightings",
    tz: Optional[ZoneInfo] = None,
    failures: Optional[list[str]] = None,
) -> Optional[Sighting]:
    """The most recent wildlife-camera capture, or None.

    The picture is optional and the caption is not: if the file behind
    `image_path` is missing the Sighting still comes back with
    `image_path=None`, because the renderer can show the caption alone and a
    missing JPEG is not a reason to drop the panel.
    """
    if not dsn or not dsn.strip():
        return None

    tz = tz or settings.timezone
    try:
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL(SIGHTING_SQL).format(view=_view(view)))
                row = cur.fetchone()
            if row is None:
                return None
            return Sighting(
                captured_at=_to_local(row["captured_at"], tz),
                caption=str(row.get("caption") or "").strip(),
                species=str(row.get("species") or "").strip(),
                individual=str(row.get("individual") or "").strip(),
                camera=str(row.get("camera") or "").strip(),
                image_path=_resolve_image(row.get("image_path"), sightings_dir),
            )
    except Exception as exc:
        log.warning("database: sighting unavailable (%s)", _describe(exc))
        _note(failures, SIGHTING_FAILURE)
        return None


def _to_local(value: datetime, tz: ZoneInfo) -> datetime:
    """Move a captured_at into local time, as models.py requires.

    timestamptz always arrives aware, so the naive branch only fires if the
    column type ever changes. UTC is then the right guess: that is what the
    server stores and what its session timezone reports.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _resolve_image(image_path: Any, sightings_dir: Path) -> Optional[str]:
    """Turn the stored relative path into an absolute one that really exists.

    Two things can go wrong and both end as None rather than as an exception:
    the row can point at a file that was pruned from disk, and the row is
    attacker-shaped data as far as this process is concerned - a value like
    `../../etc/shadow` would otherwise hand the renderer an arbitrary file to
    open. Leading separators are stripped and the result must stay inside
    `sightings_dir`.
    """
    if not isinstance(image_path, str):
        return None
    relative = image_path.strip().replace("\\", "/").lstrip("/")
    if not relative:
        return None

    try:
        root = Path(sightings_dir).resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            log.warning("database: image_path %r escapes the sightings dir", image_path)
            return None
        if not candidate.is_file():
            # Loud, because on the panel this is indistinguishable from a
            # layout that had no room for the tile - the caption renders either
            # way. A wrong /data/sightings mount or a changed path format in
            # the source costs every sighting its picture, silently, forever.
            log.warning("database: sighting image %s is not on disk", candidate)
            return None
    except (OSError, ValueError):
        # ValueError is not theoretical: a stored path with an embedded null
        # byte raises here, and swallowing it costs the picture while letting
        # it out would cost the caption too.
        return None
    return str(candidate)
