"""Device registry, device-log ring and firmware catalogue, in one SQLite file.

The service owns this file outright: it lives in the data volume next to the
rendered frames, and nothing else writes to it except the operator CLI
(`python -m app.cli`). Three rules hold it together:

**A device row is never created by a device.** There is no enrolment endpoint.
Rows are born in the CLI, which is also the only place the token is ever shown;
the database keeps only the SHA-256 of the full `<id>.<secret>` token. The
service statements below (`SQL_*`) contain no INSERT on `devices` at all, and a
test holds that line.

**The service writes telemetry and state, never identity or settings.** Wake
times, panel-care thresholds, `active`, `token_hash` and the firmware
assignment belong to the operator (`ADMIN_SQL_*`). Keeping the two statement
sets apart is what the column grants did in the shared-database version.

**Timestamps are stored as UTC ISO-8601 text and come back as aware
datetimes.** Stored in UTC, the text sorts in time order, which the "latest
device" query relies on; converted back on read, no caller ever sees a naive
datetime.

The schema is created and versioned here (`SCHEMA_SQL`, `PRAGMA user_version`)
and applied by `Store.migrate()` at startup and by the CLI -- never implicitly
by a service statement, so a device request can never be the thing that
changes a table.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import Telemetry

#: How many device log lines to keep per device. The board posts a handful per
#: wake, so this is roughly two months of history. Trimmed by the writer: at
#: three wakes a day it costs nothing, and a ring nobody trims is a disk that
#: fills in a year nobody is watching.
LOG_RETENTION = 500

#: Refused before the JSON reaches the database. A payload this size is a
#: firmware bug; it is replaced by a note about itself rather than stored.
_MAX_PAYLOAD_CHARS = 8000

#: Seconds SQLite waits for a lock held by another thread or the CLI before it
#: gives up. The service writes a few rows per wake, so contention only ever
#: comes from an operator command running at the same moment.
_BUSY_TIMEOUT_S = 5.0

#: Bumped whenever SCHEMA_SQL changes shape. `migrate()` refuses a file written
#: by a newer version rather than guessing.
SCHEMA_VERSION = 1

#: Default wake times, minutes since local midnight: 06:50, 14:50, 19:50.
DEFAULT_SLOTS: tuple[int, ...] = (410, 890, 1190)


class DbUnavailable(RuntimeError):
    """The store could not be opened, or answered with an error.

    Distinct from "no such device" on purpose. Returning None for every failure
    would read as "unknown token" at the authorisation check, and a device told
    its identity is broken may end up in its setup portal over a full disk.
    Callers answer this with 503, never 401: the firmware treats any non-200 as
    "end this wake without content" and sleeps its retry ladder, which is
    exactly right.
    """


def new_row_id() -> str:
    """20 hex characters. Also the part of a device token in front of the dot."""
    return secrets.token_hex(10)


def new_device_token(device_id: str) -> str:
    """`<id>.<secret>`: the id makes the lookup a primary-key hit, the secret is
    base64url and therefore never contains a dot of its own."""
    return f"{device_id}.{secrets.token_urlsafe(24)}"


def hash_device_token(token: str) -> str:
    """SHA-256 hex of the FULL `<id>.<secret>` token. The plaintext is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_mac(raw: str) -> str:
    """Uppercase, and punctuate bare hex as AA:BB:CC:DD:EE:FF.

    The board sends the MAC in whatever shape its SDK produced. The table has a
    CHECK on exactly this canonical spelling, so normalising on the way in is
    what keeps a lowercase MAC from being rejected instead of stored.
    """
    cleaned = (raw or "").strip().upper()
    hex_only = "".join(c for c in cleaned if c in "0123456789ABCDEF")
    if len(hex_only) == 12 and len(cleaned.replace(":", "").replace("-", "")) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return cleaned


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceRecord:
    """One row of `devices`.

    Three kinds of field, and the distinction is not cosmetic: identity and
    settings, which only the operator writes; telemetry and state, which only
    the service writes; and the token hash, which nobody reads for anything but
    a constant-time comparison.
    """

    id: str
    label: str
    token_hash: str
    active: bool = True
    location: Optional[str] = None
    mac: Optional[str] = None

    # -- settings, operator-owned ------------------------------------------
    slots: tuple[int, ...] = DEFAULT_SLOTS
    refresh_jitter_s: int = 20
    min_refresh_gap_s: int = 180
    max_image_age_s: int = 82_800
    min_refresh_temp_c: float = 15.0
    battery_low_mv: int = 3300
    battery_recover_mv: int = 3500
    low_battery_sleep_s: int = 21_600
    ota_min_battery_pct: int = 50
    firmware_id: Optional[str] = None

    # -- telemetry, service-owned ------------------------------------------
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
    model: Optional[str] = None

    # -- state, service-owned ----------------------------------------------
    low_battery: bool = False
    last_etag: Optional[str] = None
    #: "Told the board to paint." Written before the image is requested, so on
    #: its own it says nothing about what is on the glass.
    last_painted_at: Optional[datetime] = None
    #: "The board fetched the bytes." The moment the server actually knows.
    last_image_fetched_at: Optional[datetime] = None
    refresh_count: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_error: Optional[str] = None

    def as_telemetry(self) -> Telemetry:
        """The last known state of the board, for a request that carried none."""
        return Telemetry(
            device_id=self.mac or self.id,
            battery_voltage_mv=self.battery_voltage_mv,
            battery_percent=self.battery_percent,
            is_charging=self.is_charging,
            temperature_c=self.temperature_c,
            humidity_pct=self.humidity_pct,
            rssi_dbm=self.rssi_dbm,
            fw_version=self.fw_version,
            wake_reason=self.wake_reason,
            width=self.width,
            height=self.height,
        )

    @property
    def painted_or_fetched_at(self) -> Optional[datetime]:
        """The later of the two clocks, for the 180 s panel-care floor.

        `last_painted_at` alone let anyone who could reach /api/display keep the
        floor artificially fresh and freeze the panel; `last_image_fetched_at`
        alone would forget a repaint the board was told about but never
        collected. The floor wants the most recent evidence of either kind.
        """
        stamps = [s for s in (self.last_painted_at, self.last_image_fetched_at) if s]
        return max(stamps) if stamps else None


@dataclass(frozen=True)
class FirmwareRecord:
    """One row of `firmware`. `file_path` is relative to the firmware directory."""

    id: str
    target: str
    version: str
    file_path: str
    file_name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class LogEntry:
    id: str
    device_id: str
    created_at: datetime
    level: str
    message: str
    payload: dict[str, Any]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

#: The hardware-protection CHECKs are the point of this schema, not decoration.
#: The CLI validates the same ranges with friendlier messages; these protect the
#: panel from everything that does not come through the CLI.
#:
#: * 180 s between refreshes is the manufacturer's limit for this panel, and the
#:   documented consequence of going below it is permanent damage.
#: * `battery_recover_mv >= battery_low_mv`: swapped, the hysteresis inverts and
#:   the device latches into "low" for good -- a panel that never gets content
#:   again and keeps showing its last picture, because e-ink is bistable.
#: * `min_refresh_temp_c` is what the cold-panel gate reads; -273 would switch
#:   the gate off silently.
#: * 1..8 wake times: zero would leave the countdown without a target.
#: * Telemetry ranges throw rather than clamp. Clamping happens in
#:   `parse_headers`; this is the last line, so a device that serialises a
#:   uint32 counter by accident cannot store nonsense.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS firmware (
    id          TEXT PRIMARY KEY NOT NULL,
    target      TEXT NOT NULL DEFAULT 'eink' CHECK (target = 'eink'),
    version     TEXT NOT NULL UNIQUE,
    file_path   TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id                    TEXT PRIMARY KEY NOT NULL,
    label                 TEXT NOT NULL,
    location              TEXT,
    mac                   TEXT UNIQUE CHECK (mac IS NULL OR mac GLOB
        '[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]'),
    token_hash            TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    slots                 TEXT NOT NULL DEFAULT '[410,890,1190]'
        CHECK (json_valid(slots) AND json_array_length(slots) BETWEEN 1 AND 8),
    refresh_jitter_s      INTEGER NOT NULL DEFAULT 20     CHECK (refresh_jitter_s BETWEEN 0 AND 900),
    min_refresh_gap_s     INTEGER NOT NULL DEFAULT 180    CHECK (min_refresh_gap_s BETWEEN 180 AND 86400),
    max_image_age_s       INTEGER NOT NULL DEFAULT 82800  CHECK (max_image_age_s BETWEEN 3600 AND 604800),
    min_refresh_temp_c    REAL    NOT NULL DEFAULT 15     CHECK (min_refresh_temp_c BETWEEN -10 AND 40),
    battery_low_mv        INTEGER NOT NULL DEFAULT 3300   CHECK (battery_low_mv BETWEEN 3000 AND 4300),
    battery_recover_mv    INTEGER NOT NULL DEFAULT 3500   CHECK (battery_recover_mv BETWEEN 3000 AND 4300),
    low_battery_sleep_s   INTEGER NOT NULL DEFAULT 21600  CHECK (low_battery_sleep_s BETWEEN 3600 AND 86400),
    ota_min_battery_pct   INTEGER NOT NULL DEFAULT 50     CHECK (ota_min_battery_pct BETWEEN 0 AND 100),
    firmware_id           TEXT REFERENCES firmware(id) ON DELETE SET NULL,
    battery_voltage_mv    INTEGER CHECK (battery_voltage_mv IS NULL OR battery_voltage_mv BETWEEN 0 AND 20000),
    battery_percent       INTEGER CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
    is_charging           INTEGER CHECK (is_charging IS NULL OR is_charging IN (0, 1)),
    temperature_c         REAL    CHECK (temperature_c IS NULL OR temperature_c BETWEEN -50 AND 100),
    humidity_pct          REAL    CHECK (humidity_pct IS NULL OR humidity_pct BETWEEN 0 AND 100),
    rssi_dbm              INTEGER CHECK (rssi_dbm IS NULL OR rssi_dbm BETWEEN -200 AND 0),
    last_firmware_version TEXT,
    wake_reason           TEXT,
    model                 TEXT,
    width                 INTEGER,
    height                INTEGER,
    low_battery           INTEGER NOT NULL DEFAULT 0 CHECK (low_battery IN (0, 1)),
    last_etag             TEXT,
    last_painted_at       TEXT,
    last_image_fetched_at TEXT,
    refresh_count         INTEGER NOT NULL DEFAULT 0,
    first_seen_at         TEXT,
    last_seen_at          TEXT,
    last_ip               TEXT,
    last_error            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (battery_recover_mv >= battery_low_mv)
);

CREATE TABLE IF NOT EXISTS device_logs (
    id         TEXT PRIMARY KEY NOT NULL,
    device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload))
);

CREATE INDEX IF NOT EXISTS device_logs_device_at_idx ON device_logs (device_id, at DESC);
"""


# --------------------------------------------------------------------------
# Service SQL -- what device requests may send
# --------------------------------------------------------------------------
#
# Named once and collected in ALL_STATEMENTS, so a test can read every
# statement a device request can cause and assert that none is DDL and none
# creates a device.

SQL_GET_DEVICE = "SELECT * FROM devices WHERE id = :id"

SQL_LIST_DEVICES = "SELECT * FROM devices ORDER BY COALESCE(last_seen_at, created_at) DESC, id"

#: One statement. Every telemetry field goes through COALESCE(new, old): a wake
#: that reports no temperature must leave yesterday's reading alone rather than
#: blank it, because the cold-panel gate reads that column and a NULL there
#: silently disables the gate. `last_seen_at` is the one field written
#: unconditionally -- it is the whole point of the request.
SQL_MERGE_TELEMETRY = """
    UPDATE devices SET
        last_seen_at          = :now,
        first_seen_at         = COALESCE(first_seen_at, :now),
        updated_at            = :now,
        last_ip               = COALESCE(:ip,       last_ip),
        battery_voltage_mv    = COALESCE(:voltage,  battery_voltage_mv),
        battery_percent       = COALESCE(:percent,  battery_percent),
        is_charging           = COALESCE(:charging, is_charging),
        temperature_c         = COALESCE(:temp,     temperature_c),
        humidity_pct          = COALESCE(:humidity, humidity_pct),
        rssi_dbm              = COALESCE(:rssi,     rssi_dbm),
        last_firmware_version = COALESCE(:fw,       last_firmware_version),
        wake_reason           = COALESCE(:wake,     wake_reason),
        width                 = COALESCE(:width,    width),
        height                = COALESCE(:height,   height),
        model                 = COALESCE(:model,    model)
     WHERE id = :id
 RETURNING *
"""

SQL_RECORD_PAINT = """
    UPDATE devices
       SET last_etag = :etag,
           last_painted_at = :now,
           refresh_count = refresh_count + 1,
           updated_at = :now
     WHERE id = :id
"""

SQL_RECORD_IMAGE_FETCH = """
    UPDATE devices
       SET last_image_fetched_at = :now,
           updated_at = :now
     WHERE id = :id
"""

SQL_SET_LOW_BATTERY = """
    UPDATE devices
       SET low_battery = :flag, updated_at = :now
     WHERE id = :id
"""

SQL_GET_FIRMWARE = "SELECT * FROM firmware WHERE id = :id"

SQL_ADD_LOG = """
    INSERT INTO device_logs (id, device_id, at, level, message, payload)
         VALUES (:id, :device_id, :at, :level, :message, :payload)
"""

#: Keeps the newest `keep` rows of one device. Ordered by (at, id) so rows
#: written in the same instant still yield exactly `keep` survivors -- a wake
#: that posts several lines can easily land them in one second, and a trim that
#: compared on `at` alone would delete nothing at all in that case.
SQL_TRIM_LOGS = """
    DELETE FROM device_logs
     WHERE device_id = :device_id
       AND id NOT IN (SELECT id FROM device_logs
                       WHERE device_id = :device_id
                       ORDER BY at DESC, id DESC
                       LIMIT :keep)
"""

SQL_RECENT_LOGS = """
    SELECT * FROM device_logs
     ORDER BY at DESC, id DESC
     LIMIT :limit
"""

SQL_COUNT_LOGS = "SELECT count(*) AS n FROM device_logs"

#: Every service statement, for the guards in the tests.
ALL_STATEMENTS: tuple[str, ...] = (
    SQL_GET_DEVICE,
    SQL_LIST_DEVICES,
    SQL_MERGE_TELEMETRY,
    SQL_RECORD_PAINT,
    SQL_RECORD_IMAGE_FETCH,
    SQL_SET_LOW_BATTERY,
    SQL_GET_FIRMWARE,
    SQL_ADD_LOG,
    SQL_TRIM_LOGS,
    SQL_RECENT_LOGS,
    SQL_COUNT_LOGS,
)


# --------------------------------------------------------------------------
# Operator SQL -- only the CLI sends these
# --------------------------------------------------------------------------

ADMIN_SQL_INSERT_DEVICE = """
    INSERT INTO devices (id, label, location, mac, token_hash, slots, created_at, updated_at)
         VALUES (:id, :label, :location, :mac, :token_hash, :slots, :now, :now)
"""

ADMIN_SQL_DELETE_DEVICE = "DELETE FROM devices WHERE id = :id"

#: Columns the operator may change with `device set`. Anything not in here --
#: telemetry, state, the token -- is not settable, by construction.
ADMIN_SETTABLE: tuple[str, ...] = (
    "label",
    "location",
    "active",
    "slots",
    "refresh_jitter_s",
    "min_refresh_gap_s",
    "max_image_age_s",
    "min_refresh_temp_c",
    "battery_low_mv",
    "battery_recover_mv",
    "low_battery_sleep_s",
    "ota_min_battery_pct",
    "firmware_id",
)

ADMIN_SQL_INSERT_FIRMWARE = """
    INSERT INTO firmware (id, target, version, file_path, file_name, size_bytes, sha256, created_at)
         VALUES (:id, 'eink', :version, :file_path, :file_name, :size_bytes, :sha256, :now)
"""

ADMIN_SQL_LIST_FIRMWARE = "SELECT * FROM firmware ORDER BY created_at DESC"

ADMIN_SQL_DELETE_FIRMWARE = "DELETE FROM firmware WHERE id = :id"


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


def _ts(value: Optional[datetime]) -> Optional[str]:
    """Aware datetime -> UTC ISO text. Naive input is taken to be UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _flag(value: Any) -> Optional[bool]:
    """Three-valued: NULL stays None. `bool(None)` would turn "we do not know"
    into "not charging", and the battery gate distinguishes the two."""
    return None if value is None else bool(value)


def _slots(value: Any) -> tuple[int, ...]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return tuple(int(m) for m in parsed) or DEFAULT_SLOTS
    except (TypeError, ValueError):
        return DEFAULT_SLOTS


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class Store:
    """All persistent server state. Safe to call from FastAPI's thread pool.

    A connection per operation and no pool: three wakes a day times about four
    requests is a dozen opens in twenty-four hours, and a shared connection
    across threads would need locking that SQLite already does for us.
    """

    def __init__(self, path: Path | str, *, timeout_s: float = _BUSY_TIMEOUT_S) -> None:
        self.path = str(path or "").strip()
        self.timeout_s = timeout_s

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path:
            raise DbUnavailable("no database path configured")
        try:
            conn = sqlite3.connect(self.path, timeout=self.timeout_s, isolation_level=None)
        except sqlite3.Error as exc:
            raise DbUnavailable(str(exc)) from exc
        try:
            conn.row_factory = sqlite3.Row
            # Off by default in SQLite, per connection. Without it the log ring
            # would outlive a deleted device and a firmware row could vanish
            # from under its assignment.
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        except DbUnavailable:
            raise
        except sqlite3.IntegrityError:
            # A CHECK or UNIQUE refused the write. That is a caller error, not
            # an outage, and the CLI wants to say which constraint it was.
            raise
        except sqlite3.Error as exc:
            raise DbUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def migrate(self) -> None:
        """Create or upgrade the schema. Idempotent; run at startup and by the CLI."""
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DbUnavailable(str(exc)) from exc
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise DbUnavailable(
                    f"database schema version {version} is newer than this code ({SCHEMA_VERSION})"
                )
            # WAL lets the CLI read while a wake writes. Persistent per file.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> DeviceRecord:
        return DeviceRecord(
            id=row["id"],
            label=row["label"],
            token_hash=row["token_hash"],
            active=bool(row["active"]),
            location=row["location"],
            mac=row["mac"],
            slots=_slots(row["slots"]),
            refresh_jitter_s=row["refresh_jitter_s"],
            min_refresh_gap_s=row["min_refresh_gap_s"],
            max_image_age_s=row["max_image_age_s"],
            min_refresh_temp_c=float(row["min_refresh_temp_c"]),
            battery_low_mv=row["battery_low_mv"],
            battery_recover_mv=row["battery_recover_mv"],
            low_battery_sleep_s=row["low_battery_sleep_s"],
            ota_min_battery_pct=row["ota_min_battery_pct"],
            firmware_id=row["firmware_id"],
            battery_voltage_mv=row["battery_voltage_mv"],
            battery_percent=row["battery_percent"],
            is_charging=_flag(row["is_charging"]),
            temperature_c=row["temperature_c"],
            humidity_pct=row["humidity_pct"],
            rssi_dbm=row["rssi_dbm"],
            fw_version=row["last_firmware_version"],
            wake_reason=row["wake_reason"],
            width=row["width"],
            height=row["height"],
            model=row["model"],
            low_battery=bool(row["low_battery"]),
            last_etag=row["last_etag"],
            last_painted_at=_dt(row["last_painted_at"]),
            last_image_fetched_at=_dt(row["last_image_fetched_at"]),
            refresh_count=row["refresh_count"] or 0,
            first_seen=_dt(row["first_seen_at"]),
            last_seen=_dt(row["last_seen_at"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _row_to_firmware(row: sqlite3.Row) -> FirmwareRecord:
        return FirmwareRecord(
            id=row["id"],
            target=row["target"],
            version=row["version"],
            file_path=row["file_path"],
            file_name=row["file_name"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
        )

    # -- devices ------------------------------------------------------------

    def get_device(self, device_id: str) -> Optional[DeviceRecord]:
        key = (device_id or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(SQL_GET_DEVICE, {"id": key}).fetchone()
        return self._row_to_device(row) if row else None

    def record_telemetry(
        self,
        device_id: str,
        telemetry: Telemetry,
        now: datetime,
        *,
        model: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> Optional[DeviceRecord]:
        """Merge one wake's telemetry into the device row.

        Returns None when the row is gone -- which after a successful
        authorisation can only mean the operator deleted it between the lookup
        and this write. The caller treats that as 401: the token no longer
        belongs to anything.
        """
        key = (device_id or "").strip()
        if not key:
            return None
        charging = telemetry.is_charging
        params = {
            "id": key,
            "now": _ts(now),
            "ip": ip,
            "voltage": telemetry.battery_voltage_mv,
            "percent": telemetry.battery_percent,
            "charging": None if charging is None else int(charging),
            "temp": telemetry.temperature_c,
            "humidity": telemetry.humidity_pct,
            "rssi": telemetry.rssi_dbm,
            "fw": telemetry.fw_version,
            "wake": telemetry.wake_reason,
            "width": telemetry.width,
            "height": telemetry.height,
            "model": model,
        }
        with self._connect() as conn:
            row = conn.execute(SQL_MERGE_TELEMETRY, params).fetchone()
        return self._row_to_device(row) if row else None

    def record_paint(self, device_id: str, etag: str, now: datetime) -> None:
        """Remember which frame we told the board to paint, and when."""
        key = (device_id or "").strip()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(SQL_RECORD_PAINT, {"id": key, "etag": etag, "now": _ts(now)})

    def record_image_fetch(self, device_id: str, now: datetime) -> None:
        """Remember that the board actually collected the bytes.

        Called on 200 **and** on 304. The 304 is the common case and proves
        reachability just as well -- the board asked and already holds what we
        would have sent.
        """
        key = (device_id or "").strip()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(SQL_RECORD_IMAGE_FETCH, {"id": key, "now": _ts(now)})

    def set_low_battery(self, device_id: str, flag: bool, now: datetime) -> None:
        """Latch the charge-me state so recovery needs the higher threshold."""
        key = (device_id or "").strip()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(SQL_SET_LOW_BATTERY, {"id": key, "flag": int(flag), "now": _ts(now)})

    def list_devices(self) -> list[DeviceRecord]:
        with self._connect() as conn:
            rows = conn.execute(SQL_LIST_DEVICES).fetchall()
        return [self._row_to_device(row) for row in rows]

    def latest_device(self) -> Optional[DeviceRecord]:
        devices = self.list_devices()
        return devices[0] if devices else None

    # -- firmware -----------------------------------------------------------

    def get_firmware(self, firmware_id: str) -> Optional[FirmwareRecord]:
        key = (firmware_id or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(SQL_GET_FIRMWARE, {"id": key}).fetchone()
        return self._row_to_firmware(row) if row else None

    # -- logs ---------------------------------------------------------------

    def add_log(
        self,
        device_id: str,
        payload: dict[str, Any],
        now: datetime,
        *,
        max_rows: int = LOG_RETENTION,
    ) -> str:
        """Append one device log line and trim the tail. Returns the row id."""
        key = (device_id or "").strip()
        if not key:
            raise ValueError("empty device id")
        level = str(payload.get("level") or payload.get("log_level") or "")
        message = str(
            payload.get("message")
            or payload.get("log_message")
            or payload.get("msg")
            or ""
        )
        row_id = new_row_id()
        with self._connect() as conn:
            conn.execute(
                SQL_ADD_LOG,
                {
                    "id": row_id,
                    "device_id": key,
                    "at": _ts(now),
                    "level": level[:64],
                    "message": message[:2000],
                    "payload": _capped_payload(payload),
                },
            )
            if max_rows > 0:
                conn.execute(SQL_TRIM_LOGS, {"device_id": key, "keep": max_rows})
        return row_id

    def recent_logs(self, limit: int = 20) -> list[LogEntry]:
        with self._connect() as conn:
            rows = conn.execute(SQL_RECENT_LOGS, {"limit": max(1, limit)}).fetchall()
        out: list[LogEntry] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {"value": row["payload"]}
            out.append(
                LogEntry(
                    id=row["id"],
                    device_id=row["device_id"],
                    created_at=_dt(row["at"]) or datetime.now(timezone.utc),
                    level=row["level"] or "",
                    message=row["message"] or "",
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                )
            )
        return out

    def count_logs(self) -> int:
        with self._connect() as conn:
            row = conn.execute(SQL_COUNT_LOGS).fetchone()
        return int(row["n"]) if row else 0

    # -- operator (CLI only) ------------------------------------------------

    def create_device(
        self,
        label: str,
        now: datetime,
        *,
        mac: Optional[str] = None,
        location: Optional[str] = None,
        slots: tuple[int, ...] = DEFAULT_SLOTS,
    ) -> tuple[DeviceRecord, str]:
        """Create a device and return it with its token. The token is not stored
        and cannot be shown again -- only its hash is kept."""
        device_id = new_row_id()
        token = new_device_token(device_id)
        with self._connect() as conn:
            conn.execute(
                ADMIN_SQL_INSERT_DEVICE,
                {
                    "id": device_id,
                    "label": label.strip(),
                    "location": (location or "").strip() or None,
                    "mac": normalize_mac(mac) if mac else None,
                    "token_hash": hash_device_token(token),
                    "slots": json.dumps(sorted(set(slots))),
                    "now": _ts(now),
                },
            )
            row = conn.execute(SQL_GET_DEVICE, {"id": device_id}).fetchone()
        return self._row_to_device(row), token

    def update_device(self, device_id: str, now: datetime, **fields: Any) -> Optional[DeviceRecord]:
        """Change operator-owned settings. Unknown or service-owned fields are refused."""
        unknown = set(fields) - set(ADMIN_SETTABLE)
        if unknown:
            raise ValueError(f"not settable: {', '.join(sorted(unknown))}")
        if not fields:
            return self.get_device(device_id)
        values = dict(fields)
        if "slots" in values:
            values["slots"] = json.dumps(sorted(set(int(m) for m in values["slots"])))
        if "active" in values:
            values["active"] = int(bool(values["active"]))
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        statement = f"UPDATE devices SET {assignments}, updated_at = :now WHERE id = :id"
        with self._connect() as conn:
            conn.execute(statement, {**values, "id": device_id, "now": _ts(now)})
            row = conn.execute(SQL_GET_DEVICE, {"id": device_id}).fetchone()
        return self._row_to_device(row) if row else None

    def delete_device(self, device_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(ADMIN_SQL_DELETE_DEVICE, {"id": device_id})
        return cur.rowcount > 0

    def add_firmware(
        self,
        *,
        version: str,
        file_path: str,
        file_name: str,
        size_bytes: int,
        sha256: str,
        now: datetime,
    ) -> FirmwareRecord:
        firmware_id = new_row_id()
        with self._connect() as conn:
            conn.execute(
                ADMIN_SQL_INSERT_FIRMWARE,
                {
                    "id": firmware_id,
                    "version": version,
                    "file_path": file_path,
                    "file_name": file_name,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "now": _ts(now),
                },
            )
            row = conn.execute(SQL_GET_FIRMWARE, {"id": firmware_id}).fetchone()
        return self._row_to_firmware(row)

    def list_firmware(self) -> list[FirmwareRecord]:
        with self._connect() as conn:
            rows = conn.execute(ADMIN_SQL_LIST_FIRMWARE).fetchall()
        return [self._row_to_firmware(row) for row in rows]

    def delete_firmware(self, firmware_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(ADMIN_SQL_DELETE_FIRMWARE, {"id": firmware_id})
        return cur.rowcount > 0


def _capped_payload(payload: dict[str, Any]) -> str:
    """Serialise, and cap BEFORE storing -- never after.

    Truncating the JSON string would store invalid JSON (the payload column has
    a `json_valid` CHECK), so the whole log line would be refused. An oversized
    payload is replaced by a note about itself instead.
    """
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) <= _MAX_PAYLOAD_CHARS:
        return encoded
    return json.dumps(
        {"_truncated": True, "size": len(encoded), "head": encoded[:2000]},
        ensure_ascii=False,
    )
