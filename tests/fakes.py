"""An in-memory store with the same service methods as the SQLite one.

Not a mock, and the distinction is the whole point. What `app/store.py` gets
right is almost entirely expressed *in SQL* -- the COALESCE merge, the offset
cut on the log ring, the fact that an unknown device is never created. A mock
that returned prepared rows would let a broken UPDATE pass every test in the
suite.

So this is a second implementation of the same contract, written in plain
Python, and `test_store.py` runs the same bodies against both. Where the two
disagree, one of them is wrong -- which is the only useful thing a fake can
tell you. `test_server.py` uses this one because it is fast and needs no file.

What it deliberately does NOT do is create devices. No service statement
inserts into `devices`, so a fake that quietly conjured a row on first
telemetry would hide the one behaviour that keeps self-registration closed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from app.models import Telemetry
from app.store import (
    LOG_RETENTION,
    DeviceRecord,
    FirmwareRecord,
    LogEntry,
    _capped_payload,
    new_row_id,
)


def _merge(new: Any, old: Any) -> Any:
    """COALESCE(new, old), in Python. None means "not reported this wake"."""
    return old if new is None else new


class InMemoryStore:
    """Same surface as `app.store.Store`, no database."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceRecord] = {}
        self.logs: list[LogEntry] = []
        self.firmware: dict[str, FirmwareRecord] = {}

    # -- test helpers, not part of the contract -----------------------------

    def seed_device(self, device: DeviceRecord) -> DeviceRecord:
        """Put a row in place the way the CLI would. Tests only."""
        self.devices[device.id] = device
        return device

    def seed_firmware(self, firmware: FirmwareRecord) -> FirmwareRecord:
        self.firmware[firmware.id] = firmware
        return firmware

    def migrate(self) -> None:
        """Nothing to create. The lifespan calls this on the real store."""

    # -- devices ------------------------------------------------------------

    def get_device(self, device_id: str) -> Optional[DeviceRecord]:
        return self.devices.get((device_id or "").strip())

    def record_telemetry(
        self,
        device_id: str,
        telemetry: Telemetry,
        now: datetime,
        *,
        model: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> Optional[DeviceRecord]:
        key = (device_id or "").strip()
        row = self.devices.get(key)
        if row is None:
            # No INSERT here, mirroring the store. An unknown id is a 401, and
            # a store that created the row would make that impossible to test.
            return None
        merged = replace(
            row,
            last_seen=now,
            first_seen=row.first_seen or now,
            battery_voltage_mv=_merge(telemetry.battery_voltage_mv, row.battery_voltage_mv),
            battery_percent=_merge(telemetry.battery_percent, row.battery_percent),
            is_charging=_merge(telemetry.is_charging, row.is_charging),
            temperature_c=_merge(telemetry.temperature_c, row.temperature_c),
            humidity_pct=_merge(telemetry.humidity_pct, row.humidity_pct),
            rssi_dbm=_merge(telemetry.rssi_dbm, row.rssi_dbm),
            fw_version=_merge(telemetry.fw_version, row.fw_version),
            wake_reason=_merge(telemetry.wake_reason, row.wake_reason),
            width=_merge(telemetry.width, row.width),
            height=_merge(telemetry.height, row.height),
            model=_merge(model, row.model),
        )
        self.devices[key] = merged
        return merged

    def record_paint(self, device_id: str, etag: str, now: datetime) -> None:
        key = (device_id or "").strip()
        row = self.devices.get(key)
        if row is None:
            return
        self.devices[key] = replace(
            row,
            last_etag=etag,
            last_painted_at=now,
            refresh_count=row.refresh_count + 1,
        )

    def record_image_fetch(self, device_id: str, now: datetime) -> None:
        key = (device_id or "").strip()
        row = self.devices.get(key)
        if row is None:
            return
        self.devices[key] = replace(row, last_image_fetched_at=now)

    def set_low_battery(self, device_id: str, flag: bool, now: datetime) -> None:
        key = (device_id or "").strip()
        row = self.devices.get(key)
        if row is None:
            return
        self.devices[key] = replace(row, low_battery=flag)

    def list_devices(self) -> list[DeviceRecord]:
        # `datetime.min` alone is NAIVE, and every other stamp here is aware --
        # so the moment a second device exists that has never reported (exactly
        # what a CLI-created row looks like before its first wake), sorted()
        # raises TypeError. Found by a test, not by review.
        #
        # The real store orders by COALESCE(last_seen_at, created_at); this fake
        # has no created_at, so a never-seen device sorts last here and by its
        # creation time there. Immaterial with one device, worth knowing with two.
        never = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(
            self.devices.values(),
            key=lambda d: d.last_seen or d.first_seen or never,
            reverse=True,
        )

    def latest_device(self) -> Optional[DeviceRecord]:
        rows = self.list_devices()
        return rows[0] if rows else None

    # -- firmware -----------------------------------------------------------

    def get_firmware(self, firmware_id: str) -> Optional[FirmwareRecord]:
        return self.firmware.get((firmware_id or "").strip())

    # -- logs ---------------------------------------------------------------

    def add_log(
        self,
        device_id: str,
        payload: dict[str, Any],
        now: datetime,
        *,
        max_rows: int = LOG_RETENTION,
    ) -> str:
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
        # Through the same capping the real store uses, and then back again.
        # Skipping it would let this half of the suite accept a payload the
        # real column refuses, so the fake would be quietly more permissive
        # than production in exactly the place the ring is meant to be robust.
        entry = LogEntry(
            id=new_row_id(),
            device_id=key,
            created_at=now,
            level=level[:64],
            message=message[:2000],
            payload=json.loads(_capped_payload(payload)),
        )
        self.logs.append(entry)
        if max_rows > 0:
            mine = [e for e in self.logs if e.device_id == key]
            if len(mine) > max_rows:
                doomed = {id(e) for e in sorted(mine, key=lambda e: e.created_at)[: len(mine) - max_rows]}
                self.logs = [e for e in self.logs if id(e) not in doomed]
        return entry.id

    def recent_logs(self, limit: int = 20) -> list[LogEntry]:
        return sorted(self.logs, key=lambda e: e.created_at, reverse=True)[: max(1, limit)]

    def count_logs(self) -> int:
        return len(self.logs)


def make_device(
    device_id: str = "d1",
    *,
    token_hash: str = "0" * 64,
    label: str = "Wand",
    **kwargs: Any,
) -> DeviceRecord:
    """A row in the shape the CLI would have written."""
    return DeviceRecord(id=device_id, label=label, token_hash=token_hash, **kwargs)
