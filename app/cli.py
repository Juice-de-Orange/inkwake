"""Operator commands: create devices, change their settings, register firmware.

    python -m app.cli migrate
    python -m app.cli device add --label "Hallway" --mac AA:BB:CC:DD:EE:FF
    python -m app.cli device list
    python -m app.cli device set <id> --slots 06:50,19:50 --min-refresh-temp-c 12
    python -m app.cli device remove <id>
    python -m app.cli firmware add firmware.bin --version 1.1.0
    python -m app.cli firmware list
    python -m app.cli device set <id> --firmware <firmware-id>

This is the only way a device comes into existence. `device add` prints the
token exactly once; the database keeps only its SHA-256, so a lost token means
`device remove` + `device add`, not recovery. Inside the container:
`docker compose exec app python -m app.cli …`.

The ranges checked here are the same the schema enforces with CHECKs. They are
repeated so the operator gets a sentence instead of "CHECK constraint failed".
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import settings
from .store import DbUnavailable, Store, new_row_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _store() -> Store:
    store = Store(settings.db_path)
    store.migrate()
    return store


def parse_slots(raw: str) -> tuple[int, ...]:
    """"06:50,14:50" -> minutes since local midnight. 1 to 8 distinct times."""
    minutes: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            hour, minute = int(hh), int(mm)
        except ValueError as exc:
            raise ValueError(f"wake time {part!r} is not HH:MM") from exc
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"wake time {part!r} is not a valid time of day")
        minutes.add(hour * 60 + minute)
    if not 1 <= len(minutes) <= 8:
        raise ValueError("give between one and eight wake times")
    return tuple(sorted(minutes))


def _fmt_slots(slots: Sequence[int]) -> str:
    return ",".join(f"{m // 60:02d}:{m % 60:02d}" for m in slots)


def _fmt_ts(value: Optional[datetime]) -> str:
    return value.astimezone(settings.timezone).strftime("%Y-%m-%d %H:%M") if value else "-"


# -- range checks, mirroring the schema's CHECKs ------------------------------

_RANGES: dict[str, tuple[float, float, str]] = {
    "min_refresh_gap_s": (180, 86400, "the panel's documented minimum is 180 s between refreshes"),
    "max_image_age_s": (3600, 604800, "leaving one image up longer risks burn-in"),
    "min_refresh_temp_c": (-10, 40, "the cold-panel gate reads this value"),
    "battery_low_mv": (3000, 4300, "millivolts"),
    "battery_recover_mv": (3000, 4300, "millivolts"),
    "low_battery_sleep_s": (3600, 86400, "seconds"),
    "refresh_jitter_s": (0, 900, "seconds"),
    "ota_min_battery_pct": (0, 100, "percent"),
}


def _check_ranges(fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        if name in _RANGES:
            lo, hi, why = _RANGES[name]
            if not lo <= float(value) <= hi:
                raise ValueError(f"--{name.replace('_', '-')} must be between {lo:g} and {hi:g} ({why})")


# -- commands -----------------------------------------------------------------


def cmd_migrate(_: argparse.Namespace) -> int:
    _store()
    print(f"schema ready: {settings.db_path}")
    return 0


def cmd_device_add(args: argparse.Namespace) -> int:
    slots = parse_slots(args.slots) if args.slots else None
    kwargs: dict[str, Any] = {"mac": args.mac, "location": args.location}
    if slots:
        kwargs["slots"] = slots
    device, token = _store().create_device(args.label, _now(), **kwargs)
    print(f"device created: {device.id}  ({device.label})")
    print(f"wake times:     {_fmt_slots(device.slots)}")
    print()
    print("Device token -- shown once, only its hash is stored:")
    print()
    print(f"    {token}")
    print()
    print("Enter it, together with your server URL, in the board's setup portal.")
    return 0


def cmd_device_list(_: argparse.Namespace) -> int:
    devices = _store().list_devices()
    if not devices:
        print("no devices. Create one with: python -m app.cli device add --label <name>")
        return 0
    print(f"{'ID':<20}  {'LABEL':<16} {'ACTIVE':<6} {'WAKES':<18} {'BATT':>5}  {'LAST SEEN':<16} FIRMWARE")
    for d in devices:
        batt = f"{d.battery_percent}%" if d.battery_percent is not None else "-"
        print(
            f"{d.id:<20}  {d.label[:16]:<16} {'yes' if d.active else 'no':<6} "
            f"{_fmt_slots(d.slots):<18} {batt:>5}  {_fmt_ts(d.last_seen):<16} {d.firmware_id or '-'}"
        )
    return 0


def cmd_device_set(args: argparse.Namespace) -> int:
    fields: dict[str, Any] = {}
    for name in ("label", "location", "min_refresh_gap_s", "max_image_age_s", "min_refresh_temp_c",
                 "battery_low_mv", "battery_recover_mv", "low_battery_sleep_s",
                 "refresh_jitter_s", "ota_min_battery_pct"):
        value = getattr(args, name)
        if value is not None:
            fields[name] = value
    if args.slots:
        fields["slots"] = parse_slots(args.slots)
    if args.active is not None:
        fields["active"] = args.active
    if args.firmware is not None:
        fields["firmware_id"] = None if args.firmware.lower() in {"", "none"} else args.firmware
    if not fields:
        print("nothing to change", file=sys.stderr)
        return 2
    _check_ranges(fields)
    store = _store()
    current = store.get_device(args.id)
    if current is None:
        print(f"no device {args.id}", file=sys.stderr)
        return 1
    low = fields.get("battery_low_mv", current.battery_low_mv)
    recover = fields.get("battery_recover_mv", current.battery_recover_mv)
    if recover < low:
        raise ValueError("--battery-recover-mv must not be below --battery-low-mv (the hysteresis would latch)")
    if fields.get("firmware_id") and store.get_firmware(fields["firmware_id"]) is None:
        print(f"no firmware {fields['firmware_id']} (see: firmware list)", file=sys.stderr)
        return 1
    device = store.update_device(args.id, _now(), **fields)
    assert device is not None
    print(f"updated {device.id}: " + ", ".join(sorted(fields)))
    return 0


def cmd_device_remove(args: argparse.Namespace) -> int:
    if not _store().delete_device(args.id):
        print(f"no device {args.id}", file=sys.stderr)
        return 1
    print(f"removed {args.id} (its token no longer works)")
    return 0


def cmd_firmware_add(args: argparse.Namespace) -> int:
    source = Path(args.path)
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 1
    data = source.read_bytes()
    if not data:
        print("refusing an empty firmware image", file=sys.stderr)
        return 1
    settings.firmware_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{new_row_id()}.bin"
    target = settings.firmware_dir / stored_name
    shutil.copyfile(source, target)
    try:
        firmware = _store().add_firmware(
            version=args.version,
            file_path=stored_name,
            file_name=source.name,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            now=_now(),
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print(f"firmware registered: {firmware.id}  version {firmware.version}  "
          f"{firmware.size_bytes} bytes  sha256 {firmware.sha256[:12]}…")
    print(f"assign it with: python -m app.cli device set <device-id> --firmware {firmware.id}")
    return 0


def cmd_firmware_list(_: argparse.Namespace) -> int:
    items = _store().list_firmware()
    if not items:
        print("no firmware registered")
        return 0
    for f in items:
        print(f"{f.id}  {f.version:<12} {f.size_bytes:>9} B  sha256 {f.sha256[:12]}…  ({f.file_name})")
    return 0


def cmd_firmware_remove(args: argparse.Namespace) -> int:
    store = _store()
    firmware = store.get_firmware(args.id)
    if firmware is None or not store.delete_firmware(args.id):
        print(f"no firmware {args.id}", file=sys.stderr)
        return 1
    (settings.firmware_dir / firmware.file_path).unlink(missing_ok=True)
    print(f"removed firmware {args.id}; devices it was assigned to fall back to none")
    return 0


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="inkwake operator commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create or upgrade the database schema").set_defaults(func=cmd_migrate)

    device = sub.add_parser("device", help="manage devices").add_subparsers(dest="action", required=True)

    add = device.add_parser("add", help="create a device and print its token once")
    add.add_argument("--label", required=True)
    add.add_argument("--mac", help="AA:BB:CC:DD:EE:FF (optional, informational)")
    add.add_argument("--location")
    add.add_argument("--slots", help="wake times, e.g. 06:50,14:50,19:50")
    add.set_defaults(func=cmd_device_add)

    device.add_parser("list", help="list devices").set_defaults(func=cmd_device_list)

    upd = device.add_parser("set", help="change a device's settings")
    upd.add_argument("id")
    upd.add_argument("--label")
    upd.add_argument("--location")
    upd.add_argument("--slots", help="wake times, e.g. 06:50,19:50")
    state = upd.add_mutually_exclusive_group()
    state.add_argument("--enable", dest="active", action="store_const", const=True)
    state.add_argument("--disable", dest="active", action="store_const", const=False)
    upd.add_argument("--firmware", help="firmware id to offer over the air, or 'none'")
    upd.add_argument("--min-refresh-gap-s", type=int)
    upd.add_argument("--max-image-age-s", type=int)
    upd.add_argument("--min-refresh-temp-c", type=float)
    upd.add_argument("--battery-low-mv", type=int)
    upd.add_argument("--battery-recover-mv", type=int)
    upd.add_argument("--low-battery-sleep-s", type=int)
    upd.add_argument("--refresh-jitter-s", type=int)
    upd.add_argument("--ota-min-battery-pct", type=int)
    upd.set_defaults(func=cmd_device_set, active=None)

    rem = device.add_parser("remove", help="delete a device (its token stops working)")
    rem.add_argument("id")
    rem.set_defaults(func=cmd_device_remove)

    fw = sub.add_parser("firmware", help="manage OTA firmware images").add_subparsers(dest="action", required=True)
    fadd = fw.add_parser("add", help="register a firmware .bin")
    fadd.add_argument("path")
    fadd.add_argument("--version", required=True, help="must equal FW_VERSION compiled into the image")
    fadd.set_defaults(func=cmd_firmware_add)
    fw.add_parser("list", help="list firmware images").set_defaults(func=cmd_firmware_list)
    frem = fw.add_parser("remove", help="delete a firmware image")
    frem.add_argument("id")
    frem.set_defaults(func=cmd_firmware_remove)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.IntegrityError as exc:
        print(f"refused by the database: {exc}", file=sys.stderr)
        return 2
    except DbUnavailable as exc:
        print(f"database unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
