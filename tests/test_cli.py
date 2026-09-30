"""Tests for the operator CLI (`python -m app.cli`).

The CLI is the only way a device comes into existence, and the only place its
token is ever shown. These tests drive `main()` with argument lists against a
SQLite file in tmp_path; nothing else is touched.
"""

from __future__ import annotations

import hashlib
import re
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
    payload = b"\xe9" + b"not really firmware" * 10
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


def test_firmware_add_refuses_an_empty_or_missing_file(capsys, tmp_path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert _run(capsys, "firmware", "add", str(empty), "--version", "1")[0] == 1
    assert _run(capsys, "firmware", "add", str(tmp_path / "nope.bin"), "--version", "1")[0] == 1


def test_a_duplicate_firmware_version_is_refused_and_leaves_no_file(capsys, tmp_path) -> None:
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"\xe9abc")
    assert _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")[0] == 0

    code, _, err = _run(capsys, "firmware", "add", str(image), "--version", "1.1.0")

    assert code == 2
    assert "refused by the database" in err
    assert len(list((tmp_path / "firmware").iterdir())) == 1


def test_a_device_can_point_only_at_existing_firmware(capsys) -> None:
    device_id, _ = _add(capsys)
    code, _, err = _run(capsys, "device", "set", device_id, "--firmware", "f" * 20)
    assert code == 1
    assert "no firmware" in err
