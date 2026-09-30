"""Tests for the SQLite device registry, the device-log ring and the firmware catalogue.

Every body that takes the `db` fixture runs twice: against `InMemoryStore` from
tests/fakes.py, and against the real `app.store.Store` on a fresh SQLite file
in `tmp_path`. Where the two disagree one of them is wrong -- which is the only
useful thing a second implementation can tell you.

The fixture is called `db` and not `store` because it carries more than a
store: the id of a device row, and two helpers that plant rows *around* the
service surface. That detour is the point rather than an inconvenience. No
service statement inserts into `devices`, so nothing a device request can do
brings a device into being; a fixture that registered its own device through
the service methods would have removed the one behaviour that keeps
self-registration closed. The operator path (`create_device` and friends) has
its own tests further down.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

import app.store as store_module
from app.models import Telemetry
from app.store import (
    _MAX_PAYLOAD_CHARS,
    DEFAULT_SLOTS,
    SCHEMA_VERSION,
    DbUnavailable,
    FirmwareRecord,
    LogEntry,
    Store,
    _capped_payload,
    hash_device_token,
    normalize_mac,
)
from tests.fakes import InMemoryStore, make_device

TEST_PREFIX = "einktest-"

DEVICE_ID = TEST_PREFIX + "d1"
SECOND_DEVICE_ID = TEST_PREFIX + "d2"

#: Canonical spelling, because the column has a CHECK on exactly this shape.
MAC = "AA:BB:CC:DD:EE:01"
SECOND_MAC = "AA:BB:CC:DD:EE:02"

NOW = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)

FIRMWARE = FirmwareRecord(
    id=TEST_PREFIX + "fw1",
    target="eink",
    version="0.0.0-einktest",
    file_path="einktest.bin",
    file_name="einktest.bin",
    size_bytes=1_351_680,
    sha256="ab" * 32,
)


def _token_hash(device_id: str) -> str:
    """A filler hash that is unique per row -- the column is UNIQUE."""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The fixture
# --------------------------------------------------------------------------


@dataclass
class StoreFixture:
    """One store, one planted device, and a way to plant more."""

    store: Any
    device_id: str
    mac: str
    seed_device: Callable[..., str]
    seed_firmware: Callable[[], FirmwareRecord]


def _memory_store() -> StoreFixture:
    store = InMemoryStore()

    def seed_device(device_id: str, mac: Optional[str] = None) -> str:
        store.seed_device(
            make_device(device_id, mac=mac, token_hash=_token_hash(device_id))
        )
        return device_id

    seed_device(DEVICE_ID, MAC)
    return StoreFixture(
        store=store,
        device_id=DEVICE_ID,
        mac=MAC,
        seed_device=seed_device,
        seed_firmware=lambda: store.seed_firmware(FIRMWARE),
    )


#: Planted with plain sqlite3 rather than through `create_device`, so the test
#: controls the id -- and so the service half of the suite never depends on
#: the operator path it is meant to be independent of.
_SQLITE_INSERT_DEVICE = """
    INSERT INTO devices (id, label, token_hash, mac, created_at, updated_at)
         VALUES (:id, :label, :token_hash, :mac, :now, :now)
"""

_SQLITE_INSERT_FIRMWARE = """
    INSERT INTO firmware (id, target, version, file_path, file_name, size_bytes, sha256, created_at)
         VALUES (:id, :target, :version, :file_path, :file_name, :size_bytes, :sha256, :now)
"""


def _sqlite_store(path: Path) -> Iterator[StoreFixture]:
    store = Store(path)
    store.migrate()

    def execute(statement: str, params: dict[str, Any]) -> None:
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute(statement, {**params, "now": NOW.isoformat()})
        finally:
            conn.close()

    def seed_device(device_id: str, mac: Optional[str] = None) -> str:
        execute(
            _SQLITE_INSERT_DEVICE,
            {"id": device_id, "label": "Wand", "token_hash": _token_hash(device_id), "mac": mac},
        )
        return device_id

    def seed_firmware() -> FirmwareRecord:
        execute(_SQLITE_INSERT_FIRMWARE, FIRMWARE.__dict__)
        return FIRMWARE

    seed_device(DEVICE_ID, MAC)
    yield StoreFixture(
        store=store,
        device_id=DEVICE_ID,
        mac=MAC,
        seed_device=seed_device,
        seed_firmware=seed_firmware,
    )


@pytest.fixture(params=["memory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreFixture]:
    if request.param == "memory":
        yield _memory_store()
        return
    yield from _sqlite_store(tmp_path / "inkwake.sqlite3")


@pytest.fixture
def sqlite_store(tmp_path: Path) -> Store:
    """A migrated, empty store for the operator-path tests."""
    store = Store(tmp_path / "inkwake.sqlite3")
    store.migrate()
    return store

# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------


def test_telemetry_merges_and_never_blanks(db: StoreFixture) -> None:
    db.store.record_telemetry(
        db.device_id,
        Telemetry(
            device_id=db.mac,
            battery_voltage_mv=3980,
            battery_percent=82,
            is_charging=False,
            temperature_c=21.5,
            humidity_pct=44.0,
            rssi_dbm=-61,
            fw_version="1.2.3",
            wake_reason="rtc",
            width=400,
            height=600,
        ),
        NOW,
        model="PaperColor",
    )
    # A later wake that reports only the voltage must not erase the
    # temperature: the panel-care gate reads that column, and a NULL there
    # turns the gate off without anything looking broken.
    device = db.store.record_telemetry(
        db.device_id,
        Telemetry(device_id=db.mac, battery_voltage_mv=3900),
        NOW + timedelta(hours=8),
    )

    assert device is not None
    assert device.battery_voltage_mv == 3900
    # 21.5, 44.0 and 18.25 are exact in binary floating point, so the
    # comparison can stay exact on both halves.
    assert device.temperature_c == 21.5
    assert device.humidity_pct == 44.0
    assert device.rssi_dbm == -61
    assert device.fw_version == "1.2.3"
    assert device.model == "PaperColor"
    # Three-valued: False must survive the merge, because COALESCE against a
    # reported False is the case a naive `or` would get wrong.
    assert device.is_charging is False
    assert device.last_seen == NOW + timedelta(hours=8)


def test_as_telemetry_round_trip(db: StoreFixture) -> None:
    db.store.record_telemetry(
        db.device_id,
        Telemetry(device_id=db.mac, battery_percent=55, temperature_c=18.25, is_charging=True),
        NOW,
    )
    device = db.store.get_device(db.device_id)
    assert device is not None

    telemetry = device.as_telemetry()
    # The MAC, not the row id: this object stands in for a request that carried
    # no telemetry, and the frame renders what a real request would have.
    assert telemetry.device_id == db.mac
    assert telemetry.battery_percent == 55
    assert telemetry.temperature_c == 18.25
    assert telemetry.is_charging is True


def test_unknown_device_is_not_created_by_a_write(db: StoreFixture) -> None:
    """Self-registration is closed, and this is what keeps it closed.

    An earlier design minted an api key for whatever MAC turned up, and a write
    for an unknown device created its row. Now a write for an id nobody created
    with the CLI has to be a no-op that returns None -- the caller reads that as
    401. The fake has to refuse just as flatly, or every authorisation test
    written against it would be testing a door that is only shut in production.
    """
    ghost = TEST_PREFIX + "ghost"

    merged = db.store.record_telemetry(
        ghost, Telemetry(device_id=ghost, battery_voltage_mv=4010), NOW
    )
    assert merged is None
    assert db.store.get_device(ghost) is None

    # The other writers take the same silence, and none of them leaves a row
    # behind either.
    db.store.record_paint(ghost, "abc123", NOW)
    db.store.record_image_fetch(ghost, NOW)
    db.store.set_low_battery(ghost, True, NOW)

    assert db.store.get_device(ghost) is None
    assert [device.id for device in db.store.list_devices()] == [db.device_id]


# --------------------------------------------------------------------------
# Paint, fetch, battery
# --------------------------------------------------------------------------


def test_record_paint_counts_and_stamps(db: StoreFixture) -> None:
    db.store.record_paint(db.device_id, "abc123", NOW)
    db.store.record_paint(db.device_id, "def456", NOW + timedelta(hours=8))

    device = db.store.get_device(db.device_id)
    assert device is not None
    assert device.last_etag == "def456"
    assert device.last_painted_at == NOW + timedelta(hours=8)
    assert device.refresh_count == 2


def test_image_fetch_leaves_the_paint_clock_alone(db: StoreFixture) -> None:
    db.store.record_paint(db.device_id, "abc123", NOW)
    db.store.record_image_fetch(db.device_id, NOW + timedelta(seconds=5))

    device = db.store.get_device(db.device_id)
    assert device is not None
    assert device.last_image_fetched_at == NOW + timedelta(seconds=5)
    # Two clocks, two meanings: "we told the board to paint" and "the board
    # collected the bytes". A fetch that moved `last_painted_at` would erase the
    # difference, and a 304 -- which fetches nothing new -- would count as a
    # repaint.
    assert device.last_painted_at == NOW
    assert device.refresh_count == 1


def test_painted_or_fetched_at_is_the_later_of_the_two(db: StoreFixture) -> None:
    """The number the 180 s panel-care floor hangs on.

    It has to be a maximum and not "whichever was written last", or a paint
    stamped with an older time -- a retry, a clock the board corrected from the
    Date header -- would move the floor backwards and let the panel be refreshed
    inside the manufacturer's minimum gap. That is documented as permanent
    damage, not as a flicker.
    """
    device = db.store.get_device(db.device_id)
    assert device is not None and device.painted_or_fetched_at is None

    db.store.record_image_fetch(db.device_id, NOW)
    device = db.store.get_device(db.device_id)
    assert device is not None and device.painted_or_fetched_at == NOW

    db.store.record_paint(db.device_id, "abc123", NOW - timedelta(minutes=5))
    device = db.store.get_device(db.device_id)
    assert device is not None and device.painted_or_fetched_at == NOW

    db.store.record_paint(db.device_id, "def456", NOW + timedelta(minutes=5))
    device = db.store.get_device(db.device_id)
    assert device is not None
    assert device.painted_or_fetched_at == NOW + timedelta(minutes=5)


def test_low_battery_flag_latches(db: StoreFixture) -> None:
    # The name is inherited, and it names the caller's job: the hysteresis lives
    # in main.py, which is why `set_low_battery(False)` clears the flag here
    # without argument. What the store owes is that the flag is *stored* rather
    # than recomputed from the last voltage -- a device that woke below the
    # trigger and back above it must not look recovered until the higher
    # recovery threshold says so.
    device = db.store.get_device(db.device_id)
    assert device is not None and device.low_battery is False

    db.store.set_low_battery(db.device_id, True, NOW)
    device = db.store.get_device(db.device_id)
    assert device is not None and device.low_battery is True

    db.store.set_low_battery(db.device_id, False, NOW + timedelta(hours=1))
    device = db.store.get_device(db.device_id)
    assert device is not None and device.low_battery is False


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def test_unknown_device_lookups_are_none(db: StoreFixture) -> None:
    assert db.store.get_device(TEST_PREFIX + "nobody") is None
    # An empty id never reaches the database: it is the shape a request with no
    # token has, and it must cost nothing rather than one round trip per wake.
    assert db.store.get_device("") is None
    assert db.store.record_telemetry("", Telemetry(device_id=""), NOW) is None
    # A write for a device with no id is a no-op, not a crash.
    db.store.record_paint("", "x", NOW)
    db.store.record_image_fetch("", NOW)
    db.store.set_low_battery("", True, NOW)
    assert db.store.get_firmware("") is None


def test_latest_device_prefers_the_freshest(db: StoreFixture) -> None:
    db.seed_device(SECOND_DEVICE_ID, SECOND_MAC)
    db.store.record_telemetry(db.device_id, Telemetry(device_id=db.mac), NOW)
    db.store.record_telemetry(
        SECOND_DEVICE_ID,
        Telemetry(device_id=SECOND_MAC),
        NOW + timedelta(hours=2),
    )

    latest = db.store.latest_device()
    assert latest is not None
    assert latest.id == SECOND_DEVICE_ID
    assert latest.mac == SECOND_MAC


def test_get_firmware(db: StoreFixture) -> None:
    assert db.store.get_firmware(TEST_PREFIX + "no-such-image") is None

    firmware = db.seed_firmware()
    assert db.store.get_firmware(firmware.id) == firmware


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_logs_are_stored_and_capped(db: StoreFixture) -> None:
    for i in range(25):
        db.store.add_log(
            db.device_id,
            {"message": f"line {i}", "level": "info", "battery_voltage": 3.9},
            NOW + timedelta(seconds=i),
            max_rows=10,
        )

    # `max_rows` means max_rows, not max_rows plus one. An offset cut is off by
    # exactly one row if it takes its boundary at OFFSET keep -- the (keep+1)-th
    # newest line -- rather than at OFFSET keep-1, and a ring one line too long
    # is something nothing else in this system would ever notice.
    assert db.store.count_logs() == 10

    recent = db.store.recent_logs(5)
    assert [entry.message for entry in recent] == [f"line {i}" for i in (24, 23, 22, 21, 20)]
    assert isinstance(recent[0], LogEntry)
    assert recent[0].device_id == db.device_id
    assert recent[0].payload["battery_voltage"] == 3.9
    assert recent[0].level == "info"


def test_log_accepts_junk_payload(db: StoreFixture) -> None:
    row_id = db.store.add_log(
        db.device_id, {"body": "not json at all", "nested": {"a": [1, 2]}}, NOW
    )

    entry = db.store.recent_logs(1)[0]
    assert entry.id == row_id
    # No level and no message in the payload leaves both empty rather than
    # unset: both columns are NOT NULL.
    assert entry.level == ""
    assert entry.message == ""
    assert entry.payload["nested"] == {"a": [1, 2]}

    # An empty device id would fail deep inside the INSERT on the foreign key;
    # refusing it up front is what keeps the error readable.
    with pytest.raises(ValueError):
        db.store.add_log("", {"message": "orphan"}, NOW)


def test_add_log_returns_a_hex_row_id(db: StoreFixture) -> None:
    row_id = db.store.add_log(db.device_id, {"message": "hello"}, NOW)

    # 20 hex characters, the same shape as every other row id in the file.
    assert isinstance(row_id, str)
    assert len(row_id) == 20
    int(row_id, 16)


# --------------------------------------------------------------------------
# The store's own guarantees, no database involved
# --------------------------------------------------------------------------


_DDL = re.compile(r"\b(create|alter|drop|truncate)\b", re.IGNORECASE)
_DEVICE_INSERT = re.compile(r"\binsert\s+into\s+devices\b", re.IGNORECASE)


def test_service_statements_issue_no_ddl_and_create_no_devices() -> None:
    """The request path neither changes tables nor brings devices into being.

    The schema is applied by `migrate()` at startup and by the CLI; a stray
    CREATE in a service statement would make a device request the thing that
    changes a table. And an INSERT into `devices` would reopen the
    self-registration the token model closed.
    """
    for statement in store_module.ALL_STATEMENTS:
        assert not _DDL.search(statement), statement
        assert not _DEVICE_INSERT.search(statement), statement

    # The guard is worth exactly its coverage. A new SQL_ constant that nobody
    # remembered to add to ALL_STATEMENTS would sail past the loop above, and
    # that omission is the likely one -- adding a statement and adding it to a
    # tuple are two edits.
    declared = {
        value
        for name, value in vars(store_module).items()
        if name.startswith("SQL_") and isinstance(value, str)
    }
    assert declared == set(store_module.ALL_STATEMENTS)


def test_capped_payload_stays_valid_json() -> None:
    """Cap before serialising, never after.

    Chopping the JSON string at 8000 characters would leave invalid JSON, which
    the `json_valid` CHECK on the payload column rejects outright -- and takes
    the whole log line with it, so the diagnostic that would have explained the
    firmware bug is the one thing lost.
    """
    payload = {"blob": "x" * (_MAX_PAYLOAD_CHARS * 2)}

    encoded = _capped_payload(payload)

    note = json.loads(encoded)
    assert note["_truncated"] is True
    assert note["size"] > _MAX_PAYLOAD_CHARS
    assert note["head"].startswith('{"blob": "xxx')
    assert len(encoded) < _MAX_PAYLOAD_CHARS


def test_capped_payload_passes_small_and_unserialisable_values() -> None:
    assert json.loads(_capped_payload({"a": 1})) == {"a": 1}
    # A datetime in a payload would raise inside json.dumps and lose the line;
    # `default=str` is what turns that into a readable string instead.
    assert json.loads(_capped_payload({"when": NOW}))["when"].startswith("2026-08-25")


def test_an_unreachable_store_raises_instead_of_answering_none() -> None:
    """A swallowed failure would read as "no such device" at the auth check.

    None means "no such device" everywhere in this codebase, so a store that
    answered None on an error would turn a broken disk into a stream of 401s --
    and a board that gets 401 is a board somebody sets up again by hand. The
    store raises, and the caller answers 503.
    """
    with pytest.raises(DbUnavailable):
        Store("").get_device(DEVICE_ID)


def test_a_path_that_cannot_be_opened_is_unavailable(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    with pytest.raises(DbUnavailable):
        Store(blocker / "inkwake.sqlite3").get_device(DEVICE_ID)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        ("AABBCCDDEEFF", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        ("  aa:bb:cc:dd:ee:ff  ", "AA:BB:CC:DD:EE:FF"),
        ("", ""),
        ("not-a-mac", "NOT-A-MAC"),
    ],
)
def test_normalize_mac(raw: str, expected: str) -> None:
    assert normalize_mac(raw) == expected


# --------------------------------------------------------------------------
# Schema and migration
# --------------------------------------------------------------------------


def test_migrate_is_idempotent_and_stamps_the_version(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "inkwake.sqlite3"
    store = Store(path)
    store.migrate()
    device, _ = store.create_device("Hallway", NOW)
    # A second start must neither fail nor lose rows.
    store.migrate()

    assert store.get_device(device.id) is not None
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_migrate_refuses_a_file_from_a_newer_version(tmp_path: Path) -> None:
    path = tmp_path / "inkwake.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    # Guessing at a newer layout would be how a rollback corrupts the registry.
    with pytest.raises(DbUnavailable, match="newer"):
        Store(path).migrate()


# --------------------------------------------------------------------------
# Operator path (CLI only)
# --------------------------------------------------------------------------


def test_create_device_returns_the_token_once_and_stores_only_its_hash(
    sqlite_store: Store, tmp_path: Path
) -> None:
    device, token = sqlite_store.create_device(
        " Hallway ", NOW, mac="aa-bb-cc-dd-ee-ff", location="Flur"
    )

    device_id, _, secret = token.partition(".")
    assert device_id == device.id
    assert len(secret) >= 32
    assert device.label == "Hallway"
    assert device.mac == "AA:BB:CC:DD:EE:FF"
    assert device.slots == DEFAULT_SLOTS
    assert device.active is True
    assert device.token_hash == hash_device_token(token)

    # The plaintext is nowhere in the file -- not in a column, not in a page.
    raw = (tmp_path / "inkwake.sqlite3").read_bytes()
    wal = tmp_path / "inkwake.sqlite3-wal"
    if wal.exists():
        raw += wal.read_bytes()
    assert secret.encode() not in raw


def test_two_devices_get_different_tokens(sqlite_store: Store) -> None:
    first, token_a = sqlite_store.create_device("A", NOW)
    second, token_b = sqlite_store.create_device("B", NOW)
    assert first.id != second.id
    assert token_a != token_b


def test_update_device_changes_operator_settings(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)

    updated = sqlite_store.update_device(
        device.id,
        NOW + timedelta(minutes=1),
        slots=(1190, 410, 410),
        min_refresh_temp_c=12.0,
        active=False,
    )

    assert updated is not None
    # Sorted and de-duplicated, as the countdown expects.
    assert updated.slots == (410, 1190)
    assert updated.min_refresh_temp_c == 12.0
    assert updated.active is False


def test_update_device_refuses_service_owned_columns(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)
    # The token and the telemetry are not the operator's to type in; a typo in
    # `device set` must not be able to lock a board out.
    for field in ("token_hash", "last_painted_at", "battery_percent", "id"):
        with pytest.raises(ValueError, match="not settable"):
            sqlite_store.update_device(device.id, NOW, **{field: "x"})


@pytest.mark.parametrize(
    "fields",
    [
        {"min_refresh_gap_s": 179},
        {"min_refresh_temp_c": -273.0},
        {"battery_low_mv": 3600, "battery_recover_mv": 3400},
        {"slots": ()},
        {"slots": tuple(range(0, 900, 100))},
    ],
    ids=["gap-below-panel-limit", "cold-gate-off", "hysteresis-inverted", "no-slots", "nine-slots"],
)
def test_the_schema_refuses_settings_that_would_harm_the_panel(
    sqlite_store: Store, fields: dict[str, Any]
) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)

    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.update_device(device.id, NOW, **fields)

    # Refused whole, not half-applied.
    assert sqlite_store.get_device(device.id) == device


def test_telemetry_out_of_range_is_refused_not_clamped(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.record_telemetry(
            device.id, Telemetry(device_id="", battery_percent=4_294_967_295), NOW
        )


def test_deleting_a_device_takes_its_logs_and_its_token(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)
    sqlite_store.add_log(device.id, {"message": "hello"}, NOW)

    assert sqlite_store.delete_device(device.id) is True
    assert sqlite_store.get_device(device.id) is None
    assert sqlite_store.count_logs() == 0
    assert sqlite_store.delete_device(device.id) is False


def test_firmware_catalogue_and_assignment(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)
    firmware = sqlite_store.add_firmware(
        version="1.1.0",
        file_path="0123456789abcdef0123.bin",
        file_name="firmware.bin",
        size_bytes=1024,
        sha256="cd" * 32,
        now=NOW,
    )
    assert sqlite_store.list_firmware() == [firmware]
    assert sqlite_store.get_firmware(firmware.id) == firmware

    # One image per version: two different files under one version number
    # would make "which one does the board have" unanswerable.
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.add_firmware(
            version="1.1.0", file_path="x.bin", file_name="x.bin",
            size_bytes=1, sha256="ef" * 32, now=NOW,
        )

    sqlite_store.update_device(device.id, NOW, firmware_id=firmware.id)
    assert sqlite_store.delete_firmware(firmware.id) is True
    # The assignment falls back to none rather than pointing at a missing file.
    refreshed = sqlite_store.get_device(device.id)
    assert refreshed is not None and refreshed.firmware_id is None


def test_timestamps_come_back_aware_and_in_utc(sqlite_store: Store) -> None:
    device, _ = sqlite_store.create_device("Hallway", NOW)
    vienna_noon = datetime(2026, 8, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    sqlite_store.record_paint(device.id, "etag", vienna_noon)

    painted = sqlite_store.get_device(device.id)
    assert painted is not None and painted.last_painted_at is not None
    assert painted.last_painted_at == vienna_noon
    assert painted.last_painted_at.utcoffset() == timedelta(0)
