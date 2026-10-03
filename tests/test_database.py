"""Tests for the optional Postgres source (bookings and sightings views).

No database is touched here. `psycopg.connect` is replaced by a fake whose
rows have exactly the types psycopg hands over for the documented view
columns, so the mapping code is exercised against real shapes while the test
stays offline. All names and captions are made up.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql as psycopg_sql

from app.models import Booking, Sighting
from app.sources import database

VIENNA = ZoneInfo("Europe/Vienna")
DSN = "postgresql://reader@db.example.invalid:5432/example"


# --------------------------------------------------------------------------
# Fixtures: rows exactly as psycopg would hand them over
# --------------------------------------------------------------------------

BOOKING_ROWS: list[dict[str, Any]] = [
    {
        "arrival_date": date(2026, 10, 7),
        "departure_date": date(2026, 10, 8),
        "nights": 1,
        "name": "Alex",
        "guest_count": 1,
        "comment": "",
    },
    {
        "arrival_date": date(2026, 10, 22),
        "departure_date": date(2026, 10, 23),
        "nights": 1,
        "name": "Alex",
        "guest_count": 1,
        "comment": "",
    },
    {
        "arrival_date": date(2027, 1, 26),
        "departure_date": date(2027, 1, 28),
        "nights": 2,
        "name": "Alex",
        "guest_count": 1,
        "comment": "",
    },
]

SIGHTING_ROW: dict[str, Any] = {
    "captured_at": datetime(2026, 8, 25, 9, 1, 14, tzinfo=timezone.utc),
    "camera": "Meadow",
    "species": "Fuchs",
    "individual": "Rusty",
    "caption": "Ein Wiedersehen mit Rusty am Waldrand.",
    "image_path": "cam5/0a1b2c3d4e5f60718293.jpg",
}


# --------------------------------------------------------------------------
# Fake psycopg
# --------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConnection", rows: list[dict[str, Any]]) -> None:
        self._conn = conn
        self._rows = rows
        self.closed = False

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.closed = True
        return False

    def execute(self, query: Any, params: Any = None) -> "FakeCursor":
        # The module composes its statements with psycopg.sql; render them the
        # way the server would receive them, so tests can read the view name.
        if isinstance(query, psycopg_sql.Composable):
            query = query.as_string(None)
        self._conn.executed.append((query, params))
        if self._conn.query_error is not None:
            raise self._conn.query_error
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> Optional[dict[str, Any]]:
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]], query_error: Optional[Exception]) -> None:
        self.rows = rows
        self.query_error = query_error
        self.executed: list[tuple[str, Any]] = []
        self.read_only: Optional[bool] = None
        self.read_only_at_begin: Optional[bool] = None
        self.transactions = 0
        self.closed = False
        self.connect_kwargs: dict[str, Any] = {}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self, self.rows)

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        return FakeCursor(self, [])

    @contextmanager
    def transaction(self) -> Iterator["FakeConnection"]:
        self.transactions += 1
        self.read_only_at_begin = self.read_only
        yield self

    def close(self) -> None:
        self.closed = True


def install_fake(
    monkeypatch: pytest.MonkeyPatch,
    rows: Optional[list[dict[str, Any]]] = None,
    *,
    connect_error: Optional[Exception] = None,
    query_error: Optional[Exception] = None,
) -> list[FakeConnection]:
    """Replace psycopg.connect and hand back the connections it produced."""
    made: list[FakeConnection] = []

    def fake_connect(conninfo: str = "", **kwargs: Any) -> FakeConnection:
        if connect_error is not None:
            raise connect_error
        conn = FakeConnection(list(rows or []), query_error)
        conn.connect_kwargs = {"conninfo": conninfo, **kwargs}
        made.append(conn)
        return conn

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return made


def forbid_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("connect() must not be called")

    monkeypatch.setattr(psycopg, "connect", boom)


# --------------------------------------------------------------------------
# Feature switched off
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dsn", ["", "   "])
def test_empty_dsn_means_no_connection_and_no_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str
) -> None:
    forbid_connect(monkeypatch)
    failures: list[str] = []

    assert database.fetch_bookings(dsn, 3, date(2026, 8, 25), failures=failures) == []
    assert database.fetch_latest_sighting(dsn, tmp_path, failures=failures) is None
    # Disabled is not broken: nothing may show up on the staleness badge.
    assert failures == []


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_short_circuits(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    forbid_connect(monkeypatch)
    assert database.fetch_bookings(DSN, limit, date(2026, 8, 25)) == []


# --------------------------------------------------------------------------
# Bookings
# --------------------------------------------------------------------------


def test_bookings_map_live_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, BOOKING_ROWS)

    got = database.fetch_bookings(DSN, 3, date(2026, 8, 25))

    assert got == [
        Booking(date(2026, 10, 7), date(2026, 10, 8), 1, "Alex", 1, ""),
        Booking(date(2026, 10, 22), date(2026, 10, 23), 1, "Alex", 1, ""),
        Booking(date(2027, 1, 26), date(2027, 1, 28), 2, "Alex", 1, ""),
    ]


def test_bookings_nights_fall_back_to_date_arithmetic() -> None:
    row = {
        "arrival_date": date(2026, 10, 7),
        "departure_date": date(2026, 10, 10),
        "nights": None,
        "name": " Alex ",
        "guest_count": 2,
        "comment": None,
    }
    booking = database._to_booking(row)
    assert booking.nights == 3
    assert booking.name == "Alex"
    assert booking.comment == ""


def test_bookings_pass_today_as_a_bind_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    made = install_fake(monkeypatch, BOOKING_ROWS)
    today = date(2026, 8, 25)

    database.fetch_bookings(DSN, 3, today)

    sql, params = made[0].executed[-1]
    assert params == {"today": today, "limit": 3}
    assert "%(today)s" in sql


def test_bookings_sql_never_asks_the_server_for_the_date() -> None:
    """The 00:00-02:00 trap: a UTC server's CURRENT_DATE is not Vienna's."""
    sql = database.BOOKINGS_SQL.lower()
    assert "current_date" not in sql
    assert "now()" not in sql
    assert "current_timestamp" not in sql
    # A stay departing this morning is over.
    assert "departure_date > %(today)s" in sql


def test_bookings_read_the_configured_view(monkeypatch: pytest.MonkeyPatch) -> None:
    made = install_fake(monkeypatch, BOOKING_ROWS)

    database.fetch_bookings(DSN, 3, date(2026, 8, 25), view="reporting.stays")

    sql, _ = made[0].executed[-1]
    assert 'FROM "reporting"."stays"' in sql


def test_sighting_reads_the_configured_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    made = install_fake(monkeypatch, [SIGHTING_ROW])

    database.fetch_latest_sighting(DSN, tmp_path, view="camera_feed", tz=VIENNA)

    sql, _ = made[0].executed[-1]
    assert 'FROM "camera_feed"' in sql


def test_view_names_are_quoted_not_interpolated() -> None:
    # A hostile or mistyped name must become a missing view, never a second
    # statement.
    rendered = database._view('x; DROP TABLE y').as_string(None)
    assert rendered == '"x; DROP TABLE y"'


@pytest.mark.parametrize("name", ["", "   ", "a.b.c", "."])
def test_view_names_that_cannot_be_a_view_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        database._view(name)


def test_an_invalid_view_name_costs_the_panel_not_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake(monkeypatch, BOOKING_ROWS)
    failures: list[str] = []

    assert database.fetch_bookings(DSN, 3, date(2026, 8, 25), view="a.b.c", failures=failures) == []
    assert failures == [database.BOOKINGS_FAILURE]


def test_session_is_read_only_before_the_transaction_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = install_fake(monkeypatch, BOOKING_ROWS)

    database.fetch_bookings(DSN, 3, date(2026, 8, 25))

    conn = made[0]
    assert conn.connect_kwargs["conninfo"] == DSN
    assert conn.connect_kwargs["autocommit"] is True
    assert conn.connect_kwargs["connect_timeout"] == database._CONNECT_TIMEOUT_S
    # read_only only reaches the server as part of a BEGIN, so it has to be
    # set first and there has to be a BEGIN.
    assert conn.read_only is True
    assert conn.transactions == 1
    assert conn.read_only_at_begin is True
    assert conn.executed[0][0] == f"SET statement_timeout = {database._STATEMENT_TIMEOUT_MS}"
    assert conn.closed is True


def test_bookings_survive_an_unreachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, connect_error=psycopg.OperationalError("connection timed out"))
    failures: list[str] = []

    assert database.fetch_bookings(DSN, 3, date(2026, 8, 25), failures=failures) == []
    assert failures == [database.BOOKINGS_FAILURE]


def test_an_unreachable_database_costs_one_log_line_per_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A database that is down is asked again on every render. With a full
    traceback each time the log held nothing else; libpq's answer also spans
    several lines."""
    error = psycopg.OperationalError(
        'connection failed: connection to server at "192.0.2.10", port 5432 failed:\n'
        "\tIs the server running on that host and accepting TCP/IP connections?"
    )
    install_fake(monkeypatch, connect_error=error)

    with caplog.at_level("WARNING", logger=database.log.name):
        database.fetch_bookings(DSN, 3, date(2026, 8, 25))
        database.fetch_latest_sighting(DSN, tmp_path)

    assert len(caplog.records) == 2
    for record in caplog.records:
        assert record.exc_info is None
        message = record.getMessage()
        assert "\n" not in message
        assert "OperationalError" in message and "192.0.2.10" in message


def test_bookings_survive_a_failing_query(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, BOOKING_ROWS, query_error=psycopg.errors.UndefinedColumn("boom"))
    failures: list[str] = []

    assert database.fetch_bookings(DSN, 3, date(2026, 8, 25), failures=failures) == []
    assert failures == [database.BOOKINGS_FAILURE]


def test_bookings_survive_a_broken_row(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, [{"arrival_date": date(2026, 10, 7)}])
    failures: list[str] = []

    assert database.fetch_bookings(DSN, 3, date(2026, 8, 25), failures=failures) == []
    assert failures == [database.BOOKINGS_FAILURE]


def test_failure_names_are_not_repeated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake(monkeypatch, connect_error=psycopg.OperationalError("down"))
    failures: list[str] = []

    database.fetch_bookings(DSN, 3, date(2026, 8, 25), failures=failures)
    database.fetch_bookings(DSN, 3, date(2026, 8, 25), failures=failures)
    database.fetch_latest_sighting(DSN, tmp_path, failures=failures)

    assert failures == [database.BOOKINGS_FAILURE, database.SIGHTING_FAILURE]


def test_failures_argument_is_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake(monkeypatch, connect_error=psycopg.OperationalError("down"))
    assert database.fetch_bookings(DSN, 3, date(2026, 8, 25)) == []
    assert database.fetch_latest_sighting(DSN, tmp_path) is None


# --------------------------------------------------------------------------
# Sightings
# --------------------------------------------------------------------------


def _with_image(tmp_path: Path, relative: str = "cam5/0a1b2c3d4e5f60718293.jpg") -> Path:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return target


def test_sighting_maps_live_row_and_converts_to_vienna(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake(monkeypatch, [SIGHTING_ROW])
    image = _with_image(tmp_path)

    got = database.fetch_latest_sighting(DSN, tmp_path, tz=VIENNA)

    assert isinstance(got, Sighting)
    assert got.captured_at == datetime(2026, 8, 25, 11, 1, 14, tzinfo=VIENNA)
    assert got.captured_at.utcoffset().total_seconds() == 2 * 3600
    assert got.caption == "Ein Wiedersehen mit Rusty am Waldrand."
    assert got.species == "Fuchs"
    assert got.individual == "Rusty"
    assert got.camera == "Meadow"
    assert got.image_path == str(image.resolve())


def test_sighting_sql_takes_the_newest_row() -> None:
    sql = database.SIGHTING_SQL.lower()
    assert "order by captured_at desc" in sql
    assert "limit 1" in sql


def test_sighting_without_published_rows_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake(monkeypatch, [])
    failures: list[str] = []

    assert database.fetch_latest_sighting(DSN, tmp_path, failures=failures) is None
    # An empty camera trap is not an outage.
    assert failures == []


def test_sighting_keeps_the_caption_when_the_file_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake(monkeypatch, [SIGHTING_ROW])

    got = database.fetch_latest_sighting(DSN, tmp_path, tz=VIENNA)

    assert got is not None
    assert got.image_path is None
    assert got.caption.startswith("Ein Wiedersehen")


def test_sighting_survives_a_failing_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake(monkeypatch, [SIGHTING_ROW], query_error=psycopg.OperationalError("gone"))
    failures: list[str] = []

    assert database.fetch_latest_sighting(DSN, tmp_path, failures=failures) is None
    assert failures == [database.SIGHTING_FAILURE]


def test_naive_captured_at_is_read_as_utc() -> None:
    naive = datetime(2026, 8, 25, 9, 1, 14)
    assert database._to_local(naive, VIENNA) == datetime(2026, 8, 25, 11, 1, 14, tzinfo=VIENNA)


def test_captured_at_across_the_dst_boundary() -> None:
    # 2026-10-25 03:30 UTC is 04:30 CET, the winter side of the switch.
    winter = datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc)
    local = database._to_local(winter, VIENNA)
    assert local.hour == 4
    assert local.utcoffset().total_seconds() == 3600


# --------------------------------------------------------------------------
# Image path resolution
# --------------------------------------------------------------------------


def test_image_path_resolves_against_the_sightings_dir(tmp_path: Path) -> None:
    image = _with_image(tmp_path, "cam2/9f8e7d6c5b4a39281706.jpg")
    got = database._resolve_image("cam2/9f8e7d6c5b4a39281706.jpg", tmp_path)
    assert got == str(image.resolve())


def test_image_path_tolerates_a_leading_slash(tmp_path: Path) -> None:
    image = _with_image(tmp_path, "cam3/abc.jpg")
    assert database._resolve_image("/cam3/abc.jpg", tmp_path) == str(image.resolve())


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        None,
        42,
        "../outside.jpg",
        "cam2/../../outside.jpg",
        "cam2/missing.jpg",
        "cam2/nul\x00byte.jpg",
    ],
)
def test_image_path_refuses_anything_it_cannot_vouch_for(tmp_path: Path, value: Any) -> None:
    # The escape targets exist, so only the containment check can reject them.
    (tmp_path.parent / "outside.jpg").write_bytes(b"x")
    (tmp_path / "cam2").mkdir(parents=True, exist_ok=True)
    assert database._resolve_image(value, tmp_path) is None
