"""Tests for the operator CLI (`python -m app.cli`).

The CLI is the only way a device comes into existence, and the only place its
token is ever shown. These tests drive `main()` with argument lists against a
SQLite file in tmp_path; nothing else is touched.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from app import cli
from app.config import settings
from app.store import Store, hash_device_token

TOKEN_RE = re.compile(r"^\s{4}([0-9a-f]{20}\.[A-Za-z0-9_-]{30,})$", re.MULTILINE)


@pytest.fixture(autouse=True)
def _data_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(cli, "settings", replace(settings, data_dir=tmp_path))
    return tmp_path


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "inkwake.sqlite3")


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _image(version: str = "1.1.0", *, size: int = 600 * 1024) -> bytes:
    """A synthetic file that is laid out like a PlatformIO `firmware.bin`.

    Not firmware -- the two segments are filler -- but every structure the
    checks read is real: the ESP image header for an ESP32-S3, the app
    descriptor magic at 0x20, the version marker, the padded XOR checksum and
    the appended SHA-256. Above the 512 KiB the board accepts as a minimum.
    """
    first = struct.pack("<I", 0xABCD5432) + b"\x00" * 28
    first += b"INKWAKE-FW-VERSION:" + version.encode() + b"\x00"
    first += bytes(range(256)) * ((size - len(first)) // 256)
    first += b"\x00" * (-len(first) % 4)
    second = b"\xa5\x5a\x01\x02" * 64

    header = bytearray(24)
    header[0] = 0xE9
    header[1] = 2  # segments
    struct.pack_into("<H", header, 12, 0x0009)  # chip id: ESP32-S3
    header[23] = 1  # a SHA-256 follows the checksum

    body = bytes(header)
    checksum = 0xEF
    for address, data in ((0x3C0A0020, first), (0x42000020, second)):
        body += struct.pack("<II", address, len(data)) + data
        for byte in data:
            checksum ^= byte
    body += b"\x00" * (15 - len(body) % 16) + bytes([checksum])
    return body + hashlib.sha256(body).digest()


def _add(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[str, str]:
    code, out, _ = _run(capsys, "device", "add", "--label", "Hallway", *extra)
    assert code == 0, out
    match = TOKEN_RE.search(out)
    assert match, out
    token = match.group(1)
    return token.split(".", 1)[0], token


def test_migrate_creates_the_database(capsys, tmp_path) -> None:
    code, out, _ = _run(capsys, "migrate")
    assert code == 0
    assert (tmp_path / "inkwake.sqlite3").is_file()
    assert "schema ready" in out


def test_device_add_prints_a_token_that_matches_the_stored_hash(capsys, tmp_path) -> None:
    device_id, token = _add(capsys, "--mac", "aa:bb:cc:dd:ee:ff", "--slots", "07:00,19:30")

    device = _store(tmp_path).get_device(device_id)
    assert device is not None
    assert device.token_hash == hash_device_token(token)
    assert device.mac == "AA:BB:CC:DD:EE:FF"
    assert device.slots == (7 * 60, 19 * 60 + 30)


def test_device_list_never_shows_a_token_or_its_hash(capsys, tmp_path) -> None:
    device_id, token = _add(capsys)
    code, out, _ = _run(capsys, "device", "list")

    assert code == 0
    assert device_id in out and "Hallway" in out
    assert token.split(".", 1)[1] not in out
    assert hash_device_token(token) not in out


def test_device_list_on_an_empty_registry_says_how_to_start(capsys) -> None:
    code, out, _ = _run(capsys, "device", "list")
    assert code == 0
    assert "device add" in out


def test_device_set_changes_settings(capsys, tmp_path) -> None:
    device_id, _ = _add(capsys)
    code, out, _ = _run(
        capsys, "device", "set", device_id,
        "--slots", "06:50,14:50", "--min-refresh-temp-c", "12", "--disable",
    )
    assert code == 0, out

    device = _store(tmp_path).get_device(device_id)
    assert device is not None
    assert device.slots == (410, 890)
    assert device.min_refresh_temp_c == 12.0
    assert device.active is False


@pytest.mark.parametrize(
    "args, message",
    [
        (["--min-refresh-gap-s", "60"], "180"),
        (["--battery-low-mv", "3600", "--battery-recover-mv", "3400"], "hysteresis"),
        (["--slots", "25:00"], "valid time"),
        (["--slots", "7"], "HH:MM"),
    ],
)
def test_device_set_explains_a_refusal(capsys, tmp_path, args, message) -> None:
    device_id, _ = _add(capsys)
    before = _store(tmp_path).get_device(device_id)

    code, _, err = _run(capsys, "device", "set", device_id, *args)

    assert code == 2
    assert message in err
    assert _store(tmp_path).get_device(device_id) == before


def test_device_set_without_changes_is_an_error(capsys) -> None:
    device_id, _ = _add(capsys)
    code, _, err = _run(capsys, "device", "set", device_id)
    assert code == 2
    assert "nothing to change" in err


def test_unknown_device_is_reported(capsys) -> None:
    _run(capsys, "migrate")
    assert _run(capsys, "device", "set", "0" * 20, "--label", "x")[0] == 1
    assert _run(capsys, "device", "remove", "0" * 20)[0] == 1


def test_device_remove_revokes_the_token(capsys, tmp_path) -> None:
    device_id, _ = _add(capsys)
    code, out, _ = _run(capsys, "device", "remove", device_id)
    assert code == 0, out
    assert _store(tmp_path).get_device(device_id) is None


def test_firmware_add_copies_hashes_and_can_be_assigned(capsys, tmp_path) -> None:
    image = tmp_path / "firmware.bin"
    payload = _image("1.1.0")
    image.write_bytes(payload)
    device_id, _ = _add(capsys)

    code, out, _ = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")
    assert code == 0, out
    (firmware,) = _store(tmp_path).list_firmware()
    assert firmware.sha256 == hashlib.sha256(payload).hexdigest()
    stored = tmp_path / "firmware" / firmware.file_path
    assert stored.read_bytes() == payload

    assert _run(capsys, "device", "set", device_id, "--firmware", firmware.id)[0] == 0
    device = _store(tmp_path).get_device(device_id)
    assert device is not None and device.firmware_id == firmware.id

    code, _, _ = _run(capsys, "firmware", "remove", firmware.id)
    assert code == 0
    assert not stored.exists()
    device = _store(tmp_path).get_device(device_id)
    assert device is not None and device.firmware_id is None


def test_firmware_add_checks_the_version_compiled_into_the_image(capsys, tmp_path) -> None:
    image = tmp_path / "firmware.bin"
    image.write_bytes(_image("1.0.1"))

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.0.2")
    assert code == 1
    assert "1.0.2" in err and "1.0.1" in err
    # Refused before anything is copied or registered.
    assert not (tmp_path / "firmware").exists() or not list((tmp_path / "firmware").iterdir())

    code, out, _ = _run(capsys, "firmware", "add", str(image), "--version", "1.0.1")
    assert code == 0, out
    (firmware,) = _store(tmp_path).list_firmware()
    assert firmware.version == "1.0.1"


def test_firmware_add_refuses_an_empty_or_missing_file(capsys, tmp_path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert _run(capsys, "firmware", "add", str(empty), "--version", "1")[0] == 1
    assert _run(capsys, "firmware", "add", str(tmp_path / "nope.bin"), "--version", "1")[0] == 1


def test_a_duplicate_firmware_version_is_refused_and_leaves_no_file(capsys, tmp_path) -> None:
    image = tmp_path / "firmware.bin"
    image.write_bytes(_image("1.1.0"))
    assert _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")[0] == 0

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")

    assert code == 2
    # A sentence, not "UNIQUE constraint failed: firmware.version".
    assert "already registered" in err and "constraint" not in err
    assert len(list((tmp_path / "firmware").iterdir())) == 1


def _nothing_registered(tmp_path: Path) -> bool:
    # A refusal comes before the database is even opened, so the schema may
    # not exist yet.
    store = _store(tmp_path)
    store.migrate()
    stored = tmp_path / "firmware"
    return not store.list_firmware() and (not stored.exists() or not list(stored.iterdir()))


def test_firmware_add_refuses_random_bytes(capsys, tmp_path) -> None:
    """Used to be registered with a warning about the missing marker -- and
    was then offered to the board as an update."""
    image = tmp_path / "firmware.bin"
    image.write_bytes(hashlib.sha256(b"seed").digest() * 20000)  # 640 kB of noise

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")

    assert code == 1
    assert "not an ESP32 application image" in err
    assert "nothing was registered" in err
    assert _nothing_registered(tmp_path)


def test_firmware_add_refuses_a_truncated_image(capsys, tmp_path) -> None:
    """The dangerous one: a copy that stopped early still starts with 0xE9 and
    still carries the version marker, so it used to pass without a word. Cut
    at 90 % it is above the board's minimum size too -- only walking the
    segments finds it."""
    whole = _image("1.1.0")
    image = tmp_path / "firmware.bin"
    image.write_bytes(whole[: len(whole) * 9 // 10])

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")

    assert code == 1
    assert "truncated" in err
    assert _nothing_registered(tmp_path)


@pytest.mark.parametrize(
    "damage, message",
    [
        # One byte flipped in the middle of a segment.
        (lambda b: b[:300_000] + bytes([b[300_000] ^ 0x01]) + b[300_001:], "checksum"),
        # The last 48 bytes gone: checksum and digest missing, segments intact.
        (lambda b: b[:-48], "truncated"),
        # Something appended, as a merged or signed file would have.
        (lambda b: b + b"\xff" * 16, "follow the end of the image"),
        # Built for another chip (0x0000 is the plain ESP32).
        (lambda b: b[:12] + b"\x00\x00" + b[14:], "chip id"),
        # A build whose marker the linker dropped.
        (lambda b: b.replace(b"INKWAKE-FW-VERSION:", b"XNKWAKE-FW-VERSION:"), "version marker not found"),
        # Smaller than anything the board will download.
        (lambda _: _image("1.1.0", size=64 * 1024), "the board accepts"),
    ],
)
def test_firmware_add_names_what_is_wrong_with_an_image(capsys, tmp_path, damage, message) -> None:
    image = tmp_path / "firmware.bin"
    image.write_bytes(damage(_image("1.1.0")))

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")

    assert code == 1
    assert message in err
    assert _nothing_registered(tmp_path)


def test_firmware_add_force_registers_a_file_that_fails_the_checks(capsys, tmp_path) -> None:
    """The escape hatch, for an image these checks are wrong about. It says
    what it found and it does not extend to a version that contradicts the
    marker."""
    whole = _image("1.1.0")
    image = tmp_path / "firmware.bin"
    image.write_bytes(whole[:-48])

    code, out, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0", "--force")
    assert code == 0, err
    assert "warning" in err and "truncated" in err
    (firmware,) = _store(tmp_path).list_firmware()
    assert firmware.size_bytes == len(whole) - 48

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.2.0", "--force")
    assert code == 1
    assert "1.2.0" in err and "1.1.0" in err


def test_the_synthetic_image_passes_the_checks_it_is_tested_against() -> None:
    """Otherwise every refusal above could be the fixture's fault."""
    from app import firmware_image

    inspection = firmware_image.inspect(_image("1.2.3"))
    assert inspection.problems == ()
    assert inspection.version == "1.2.3"


def test_verify_image_tool_runs_the_same_checks(tmp_path) -> None:
    """firmware/tools/verify-image.py is the workstation half of the same net.
    It imports app.firmware_image instead of carrying a copy, and this pins
    that it still runs from a checkout."""
    import subprocess
    import sys

    tool = Path(__file__).resolve().parents[1] / "firmware" / "tools" / "verify-image.py"
    good = tmp_path / "good.bin"
    good.write_bytes(_image("1.2.3"))
    bad = tmp_path / "bad.bin"
    bad.write_bytes(_image("1.2.3")[:-48])

    done = subprocess.run([sys.executable, str(tool), str(good)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "version   1.2.3" in done.stdout and "OK." in done.stdout

    done = subprocess.run([sys.executable, str(tool), str(bad)], capture_output=True, text=True)
    assert done.returncode == 1
    assert "truncated" in done.stdout and "do NOT register" in done.stdout


@pytest.mark.parametrize("mac", ["zz", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00"])
def test_a_malformed_mac_gets_a_sentence(capsys, tmp_path, mac) -> None:
    code, _, err = _run(capsys, "device", "add", "--label", "Hallway", "--mac", mac)
    assert code == 2
    assert "is not a MAC address" in err and "constraint" not in err
    store = _store(tmp_path)
    store.migrate()
    assert store.list_devices() == []


def test_a_duplicate_mac_gets_a_sentence(capsys, tmp_path) -> None:
    _add(capsys, "--mac", "AA:BB:CC:DD:EE:FF")
    code, _, err = _run(capsys, "device", "add", "--label", "Kitchen", "--mac", "aa-bb-cc-dd-ee-ff")
    assert code == 2
    assert "already exists" in err and "constraint" not in err
    assert len(_store(tmp_path).list_devices()) == 1


def test_a_device_can_point_only_at_existing_firmware(capsys) -> None:
    device_id, _ = _add(capsys)
    code, _, err = _run(capsys, "device", "set", device_id, "--firmware", "f" * 20)
    assert code == 1
    assert "no firmware" in err
