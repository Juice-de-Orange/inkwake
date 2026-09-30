"""Tests for the deployment artefacts: Dockerfile, .dockerignore, compose.

These are not smoke tests for docker -- they guard the ways this setup breaks
*silently*, where "silently" means the container starts, the board gets a
picture, and something is quietly wrong anyway:

  * a setting exists in config.py that the compose environment never mentions,
    so nobody knows it can be changed and the default is the only value it will
    ever have;
  * the port is published on every interface by default, putting a device API
    and an unguarded preview on the network before anyone has set a key;
  * PUBLIC_BASE_URL falls back to a loopback address, so the board is handed an
    image_url on its own loopback and never loads a picture while the server
    books the frame as painted;
  * the sightings mount and SIGHTINGS_DIR drift apart, so every caption renders
    and every image is missing.

The telemetry header trap that nginx's `underscores_in_headers` used to paper
over is fixed at the source: the firmware sends hyphenated names, which no
proxy strips, and there is a test for that below.

Everything here is text and YAML parsing. No docker, no network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

#: The repository root: the image context and the compose project directory.
APP = Path(__file__).resolve().parent.parent

DOCKERFILE = APP / "Dockerfile"
DOCKERIGNORE = APP / ".dockerignore"
COMPOSE = APP / "docker-compose.yml"
ENV_EXAMPLE = APP / ".env.example"

#: Paths inside the container, fixed by the image and the mounts rather than
#: tunable, so they are not expected to have a ${VAR} placeholder.
IMAGE_FIXED_VARS = {"DATA_DIR", "SIGHTINGS_DIR"}

#: Settings fields that are deliberately NOT exposed through compose.
#:
#: The panel-protection and battery thresholds live on the device row, where
#: the operator sets them per device with the CLI. The values left in
#: config.py are only the defaults a new row gets; exposing them as
#: environment variables again would give one rule two homes, and the one in
#: the environment would look authoritative while changing nothing.
PER_DEVICE_VARS = {
    "REFRESH_SLOTS",
    "MIN_REFRESH_GAP_S",
    "MAX_IMAGE_AGE_S",
    "REFRESH_JITTER_S",
    "MIN_REFRESH_TEMP_C",
    "BATTERY_LOW_MV",
    "BATTERY_RECOVER_MV",
    "BATTERY_EMPTY_MV",
    "BATTERY_FULL_MV",
}

#: Values that are credentials or can contain one (a DSN password, a private
#: calendar link). They must reach the container from .env only.
SECRET_VARS = {"ADMIN_API_KEY", "SOURCES_DSN", "EVENTS_ICS_URLS"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_read(COMPOSE))


def _service() -> dict[str, Any]:
    return _compose()["services"]["inkwake"]


def _environment() -> dict[str, str]:
    return {k: str(v) for k, v in _service()["environment"].items()}


def _config_env_names() -> set[str]:
    """Every environment variable config.py reads, by name."""
    source = _read(APP / "app" / "config.py")
    return set(re.findall(r'_env(?:_int|_float|_bool)?\(\s*"([A-Z0-9_]+)"', source))


def _mount_target(mount: str) -> str:
    """The container side of a short-syntax volume, `${VAR:-default}` sources included."""
    parts = mount.split(":")
    if parts[-1] in {"ro", "rw"}:
        parts = parts[:-1]
    return parts[-1]


def _env_example_names() -> set[str]:
    return set(re.findall(r"^([A-Z0-9_]+)=", _read(ENV_EXAMPLE), re.MULTILINE))


# --------------------------------------------------------------------------
# Settings and the compose whitelist
# --------------------------------------------------------------------------


def test_every_setting_is_reachable_or_deliberately_not() -> None:
    """No silent default.

    The compose environment block is an explicit whitelist -- what is not
    listed does not reach the container. A setting that config.py reads and
    compose never mentions can therefore never be changed without editing
    code, and nothing anywhere says so. Either it is in the block, or it is on
    one of the two lists above with a reason.
    """
    declared = set(_environment())
    for name in sorted(_config_env_names()):
        if name in PER_DEVICE_VARS:
            continue
        assert name in declared, (
            f"{name} is read by config.py but not in the compose environment block. "
            "Add it there, or add it to PER_DEVICE_VARS with a reason."
        )


def test_every_tunable_setting_is_explained_in_env_example() -> None:
    """.env.example is the documentation an operator actually reads."""
    documented = _env_example_names()
    for name in sorted(set(_environment()) - IMAGE_FIXED_VARS - {"TZ"}):
        assert name in documented, f"{name} is configurable but missing from .env.example"
    # And the compose-only knobs, which config.py never sees.
    for name in ("INKWAKE_BIND", "INKWAKE_PORT", "SIGHTINGS_HOST_DIR"):
        assert name in documented, name


def test_per_device_settings_are_not_also_environment_variables() -> None:
    """One rule, one home."""
    clash = sorted(PER_DEVICE_VARS & set(_environment()))
    assert not clash, f"per-device settings also set in compose: {clash}"


def test_compose_holds_no_real_secret() -> None:
    """Credentials come from .env, never from the tracked file."""
    env = _environment()
    for name in sorted(SECRET_VARS):
        assert env[name] == f"${{{name}:-}}", (name, env[name])
    example = _read(ENV_EXAMPLE)
    for name in sorted(SECRET_VARS):
        assert re.search(rf"^{name}=$", example, re.MULTILINE), (
            f"{name} must be empty in .env.example"
        )


def test_public_base_url_has_no_silent_default() -> None:
    """Compose refuses to start without it, rather than falling back to loopback."""
    value = _environment()["PUBLIC_BASE_URL"]
    assert value.startswith("${PUBLIC_BASE_URL:?"), value


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


def test_compose_parses() -> None:
    assert isinstance(_compose(), dict)


def test_the_port_is_published_on_loopback_by_default() -> None:
    """Behind a reverse proxy nothing else needs the port.

    A default of all interfaces would put /preview -- which renders a frame
    with booking names on demand -- on the network before an operator has had
    a chance to set ADMIN_API_KEY. Opening it up is one line in .env.
    """
    ports = _service()["ports"]
    assert ports == ["${INKWAKE_BIND:-127.0.0.1}:${INKWAKE_PORT:-8099}:8000"], ports


def test_container_paths_match_the_mounts() -> None:
    """If SIGHTINGS_DIR and the sightings mount drift, every caption renders and
    every image is missing -- the panel looks almost right."""
    env = _environment()
    targets = [_mount_target(m) for m in _service()["volumes"]]
    assert env["DATA_DIR"] in targets
    assert env["SIGHTINGS_DIR"] in targets


def test_foreign_data_is_mounted_read_only() -> None:
    """Everything except the service's own volume is somebody else's data."""
    for mount in _service()["volumes"]:
        if not mount.startswith("inkwake_data:"):
            assert mount.endswith(":ro"), mount


def test_the_data_lives_in_a_named_volume() -> None:
    """Load-bearing, and it fails only on a fresh host.

    Docker seeds a new named volume from the image's /data, ownership
    included; the image creates it for UID 10001. A host bind would be
    root-owned and the service could not write its database or a single frame.
    """
    data_mount = [m for m in _service()["volumes"] if m.endswith(":/data")]
    assert data_mount == ["inkwake_data:/data"], data_mount
    assert "inkwake_data" in _compose()["volumes"]


def test_hardening() -> None:
    service = _service()
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    # mode=1777 is mandatory: the container runs as UID 10001, a default tmpfs
    # belongs to root, and every write ends in EACCES.
    assert any("mode=1777" in t for t in service["tmpfs"]), service["tmpfs"]


def test_logging_is_bounded() -> None:
    """An unwatched service must not fill the disk."""
    opts = _service()["logging"]["options"]
    assert opts["max-size"] and opts["max-file"]


def test_restart_policy_survives_a_reboot() -> None:
    assert _service()["restart"] == "unless-stopped"


def test_the_image_runs_unprivileged_and_owns_its_data() -> None:
    dockerfile = _read(DOCKERFILE)
    assert "USER 10001:10001" in dockerfile
    assert re.search(r"chown -R 10001:10001 /data", dockerfile)


def test_secrets_never_enter_the_build_context() -> None:
    ignored = _read(DOCKERIGNORE).splitlines()
    assert ".env" in ignored
    assert "*.sqlite3" in ignored
    assert "data/" in ignored


# --------------------------------------------------------------------------
# Firmware / server constants that are duplicated by hand
# --------------------------------------------------------------------------
#
# CLAUDE.md and firmware/src/config.h both say these two files are kept in
# step manually, which is fine right up until the pair encodes a rule that
# neither half can check alone. The offline backoff is exactly that rule: the
# ladder lives on the board, the slot spacing lives on the server, and only
# their sum decides whether panel rule 2 still holds.

FIRMWARE_CONFIG = APP / "firmware" / "src" / "config.h"


def _firmware_backoff_ladder_s() -> list[int]:
    """The rungs of SLEEP_OFFLINE_BACKOFF_S, in seconds."""
    text = FIRMWARE_CONFIG.read_text(encoding="utf-8")
    match = re.search(
        r"SLEEP_OFFLINE_BACKOFF_S\[\]\s*=\s*\{([^}]*)\}", text, re.DOTALL
    )
    assert match, "SLEEP_OFFLINE_BACKOFF_S not found in firmware/src/config.h"
    return [int(v) for v in re.findall(r"\d+", match.group(1))]


def _firmware_uint(name: str) -> int:
    text = FIRMWARE_CONFIG.read_text(encoding="utf-8")
    match = re.search(rf"{name}\s*=\s*(\d+)\s*;", text)
    assert match, f"{name} not found in firmware/src/config.h"
    return int(match.group(1))


def test_offline_backoff_reaches_the_notice_promptly_and_repeats() -> None:
    """The offline ladder must stay a ladder, not become a nap.

    Note what this deliberately does NOT assert: that the notice lands inside
    panel rule 2's 24 h. It cannot, and an earlier version of this test
    claimed it by computing a "head start" from the slot gaps alone. The
    counter the ladder is indexed by (failureStreak) is reset by a *successful*
    wake, and a successful wake need not paint - `should_repaint` returns
    False for unchanged content under MAX_IMAGE_AGE_S, and flatly refuses
    below MIN_REFRESH_TEMP_C. An unheated room in January produces no-paint
    successes indefinitely, so the age of the image when the router dies is
    unbounded and no arrangement of these rungs can bound it. Closing that gap
    needs the last-paint epoch in NVS, not a bigger constant here.

    What is checkable, and what this guards: the notice arrives promptly after
    connectivity is lost, and it keeps repeating on a sub-daily cadence so the
    pixels are exercised for as long as the outage lasts.
    """
    ladder = _firmware_backoff_ladder_s()
    notice_after = _firmware_uint("OFFLINE_NOTICE_AFTER")
    assert ladder, "the ladder must have at least one rung"
    assert notice_after >= 1

    def ladder_to_notice_h() -> float:
        return sum(
            ladder[min(streak - 1, len(ladder) - 1)] for streak in range(1, notice_after)
        ) / 3600.0

    to_notice_h = ladder_to_notice_h()
    assert to_notice_h <= 12.0, (
        f"the offline notice takes {to_notice_h:.1f} h from the first failed wake; "
        "beyond half a day the board is silent about an outage for too long"
    )

    # After the notice the streak resets, so the board sleeps the rung the
    # notice itself selected and then reruns the whole ladder.
    steady_h = ladder[min(notice_after - 1, len(ladder) - 1)] / 3600.0 + to_notice_h
    assert steady_h <= 24.0, (
        f"offline notices repeat every {steady_h:.1f} h; the panel needs its "
        "pixels exercised at least daily"
    )

    # Monotonic and bounded: a rung that shrinks would be a bug, and one longer
    # than the steady-state budget would starve the repeat above.
    assert ladder == sorted(ladder), "backoff rungs must not shrink"
    assert max(ladder) <= 24 * 3600


def test_the_compiled_server_url_matches_the_header_default() -> None:
    """Two places hold the same address, and the build flag wins.

    `platformio.ini` passes -DEINK_SERVER_URL, which overrides the #ifndef
    default in config.h. So changing the header alone leaves a file that reads
    correctly and an image that points somewhere else -- and the only way to
    notice is to grep the built binary, which is what happened here once.

    Also pinned: the default is a placeholder under a reserved domain, so a
    board flashed without editing it talks to nobody rather than to a stranger's
    server. The real address is entered in the setup portal and kept in NVS.
    """
    ini = _read(APP / "firmware" / "platformio.ini")
    header = _read(FIRMWARE_CONFIG)

    from_ini = re.search(r"-DEINK_SERVER_URL='\"([^\"]+)\"'", ini)
    assert from_ini, "platformio.ini no longer sets EINK_SERVER_URL"
    from_header = re.search(r'#define EINK_SERVER_URL "([^"]+)"', header)
    assert from_header, "config.h no longer carries a default"

    assert from_ini.group(1) == from_header.group(1), (
        f"the build flag says {from_ini.group(1)} and the header says "
        f"{from_header.group(1)}. The flag wins, so the header is decoration -- "
        "and the next person to read it will be misled."
    )
    assert from_ini.group(1).startswith("https://"), from_ini.group(1)
    host = from_ini.group(1).split("/")[2]
    assert host == "example.com" or host.endswith(".example.com"), (
        f"{host} is not a placeholder. The compiled default must point nowhere."
    )


def test_wake_watchdog_clears_the_sum_of_its_own_timeouts() -> None:
    """WAKE_WATCHDOG_S is a deadline on a healthy wake, not a hang detector.

    Nothing outside the setup portal calls esp_task_wdt_reset(), so the work
    has to finish inside this number. It therefore has to clear the sum of
    every sub-timeout on the longest path — and the TLS handshake is part of
    that sum, which is what the 300 s version missed: the arduino-esp32
    default is 120 s per handshake, and there are up to four handshakes on the
    first-wake path.
    """
    watchdog_s = _firmware_uint("WAKE_WATCHDOG_S")
    http_s = _firmware_uint("HTTP_TIMEOUT_MS") / 1000.0
    tls_s = _firmware_uint("TLS_HANDSHAKE_TIMEOUT_S")
    attempts = _firmware_uint("WIFI_MAX_ATTEMPTS")
    attempt_s = _firmware_uint("WIFI_ATTEMPT_TIMEOUT_MS") / 1000.0
    body_s = _firmware_uint("IMAGE_BODY_TIMEOUT_MS") / 1000.0

    # One request = TCP connect + TLS handshake + response header, each bounded
    # separately. Three on the display path: fetchPlan plus fetchImage over two
    # redirect hops. The firmware download has a fourth, and it is counted
    # inside ota_s below rather than here -- counting it twice would make this
    # assertion stricter than config.h's table by exactly one request, which is
    # the kind of drift that makes the two stop being checkable against each
    # other.
    request_s = http_s + tls_s + http_s
    # DNS has to be in here or the assertion is far weaker than the comment it
    # guards: it is the single largest term in config.h's table and is bounded
    # by none of the timeouts above, because WiFiClientSecure resolves through
    # WiFi.hostByName() before start_ssl_client. WiFiGeneric waits 16 s for
    # WIFI_DNS_IDLE_BIT and then 15 s for WIFI_DNS_DONE_BIT. Without this term
    # the test still passed at WAKE_WATCHDOG_S = 320, which is below the
    # documented worst case.
    #
    # FOUR uncached lookups, not three. In practice display, image and firmware
    # share one host and only the first resolution costs anything, but this
    # table is built from ceilings -- and counting three left the reserve
    # thinner than config.h's prose claimed.
    dns_s = 4 * (16 + 15)
    # The firmware download is its own path: a second association after the
    # panel rail is cut, one more request, and the transfer itself. It runs
    # AFTER armWake(), so an over-run there costs a wake and not a dark panel --
    # but the watchdog still has to clear it, because the watchdog is the only
    # thing that ends a hung transfer at all.
    ota_s = attempts * attempt_s + request_s + _firmware_uint("OTA_TRANSFER_TIMEOUT_S")
    network_s = attempts * attempt_s + dns_s + 3 * request_s + body_s + ota_s
    # Panel BUSY ceilings and the housekeeping either side of them, from the
    # table in config.h: M5.begin 40, present 80, finish 23, armWake 2, misc 2.
    panel_s = 40 + 80 + 23 + 2 + 2

    assert watchdog_s > network_s + panel_s, (
        f"WAKE_WATCHDOG_S is {watchdog_s} s but the sub-timeouts on the longest "
        f"path add up to {network_s + panel_s:.0f} s (network {network_s:.0f} "
        f"of which {dns_s} is DNS, panel {panel_s}). A healthy but slow wake "
        "would reboot, and the next boot skips its slot."
    )


# --------------------------------------------------------------------------
# The device palette is a contract with a third-party library
# --------------------------------------------------------------------------

#: M5GFX's own nearest-colour table. The whole two-palette trick rests on our
#: DEVICE_RGB being byte-identical to it: the server decides which ink a pixel
#: gets by matching in Lab against MEASURED_RGB, then paints that decision in
#: DEVICE_RGB so M5GFX's re-quantisation on arrival is an exact hit and becomes
#: a no-op. One changed value there and glyph edges reach the panel as noise,
#: silently - there is no error anywhere in the chain.
M5GFX_PANEL_DIR = (
    APP / "firmware" / ".pio" / "libdeps" / "papercolor" / "M5GFX" / "src" / "lgfx" / "v1" / "panel"
)
#: 0.2.28 kept the palette in the .cpp; by 0.2.31 it had moved into an .inl.
#: The move made this test skip silently until it was taught both names.
M5GFX_PANEL_FILES = ("Panel_ED2208.cpp", "Panel_ED2208.inl")


def _m5gfx_panel_source() -> Path | None:
    for name in M5GFX_PANEL_FILES:
        path = M5GFX_PANEL_DIR / name
        if path.is_file() and "epd_palette" in _read(path):
            return path
    return None


def test_device_palette_still_matches_m5gfx() -> None:
    """platformio.ini pins `^0.2.27`, so any future 0.2.x may be installed.

    Nothing else in the tree holds these two tables together. Verified by hand
    against 0.2.28 on 2026-09-09 and by this test against 0.2.31 on 2026-09-30.

    Skips without a firmware build, because the library is only there after
    `pio run`. CI sets INKWAKE_REQUIRE_M5GFX=1 after building, so a library
    that moved its palette somewhere this test does not look fails instead of
    skipping -- which is exactly what happened once.
    """
    source = _m5gfx_panel_source()
    if source is None:
        message = f"M5GFX palette not found under {M5GFX_PANEL_DIR}; run pio run -e papercolor"
        if os.environ.get("INKWAKE_REQUIRE_M5GFX") == "1":
            pytest.fail(message)
        pytest.skip(message)

    palette = pytest.importorskip("app.render.palette")
    text = _read(source)

    block = re.search(r"epd_palette\[\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
    assert block, f"epd_palette not found in {source.name} -- did the library restructure?"
    entries = re.findall(
        r"\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*EPD_([A-Z]+)\s*\}", block.group(1)
    )
    assert len(entries) == 6, f"expected 6 palette entries, parsed {len(entries)}"

    codes = {
        name: int(value, 0)
        for name, value in re.findall(
            r"EPD_([A-Z]+)\s*=\s*(0x[0-9a-fA-F]+)\s*;", text
        )
    }

    theirs_rgb = {name.lower(): (int(r), int(g), int(b)) for r, g, b, name in entries}
    theirs_nibble = {name.lower(): codes[name] for *_, name in entries}
    ours_rgb = dict(zip(palette.NAMES, palette.DEVICE_RGB))
    ours_nibble = dict(zip(palette.NAMES, palette.NATIVE_NIBBLE))

    assert theirs_rgb == ours_rgb, (
        "DEVICE_RGB no longer matches M5GFX's epd_palette. The frame would be "
        "re-quantised on arrival instead of passing through untouched, and "
        f"nothing would report it.\n  M5GFX: {theirs_rgb}\n  ours:  {ours_rgb}"
    )
    assert theirs_nibble == ours_nibble, (
        f"NATIVE_NIBBLE diverged.\n  M5GFX: {theirs_nibble}\n  ours:  {ours_nibble}"
    )


def test_firmware_asks_for_the_one_mode_that_does_not_dither() -> None:
    """`epd_fastest` is the single line the two-palette trick hangs on.

    Every other mode routes through a dithering row function. Simulated against
    M5GFX 0.2.28: swap the mode and a solid DEVICE_RGB blue field survives as
    blue only under `epd_fastest`; under the others the same field would break
    into a blue/white checkerboard once the palette drifts.
    """
    panel_cpp = _read(APP / "firmware" / "src" / "panel.cpp")
    assert "setEpdMode(m5gfx::epd_mode_t::epd_fastest)" in panel_cpp, (
        "panel.cpp no longer selects epd_fastest. Any other mode dithers the "
        "already-quantised frame a second time."
    )


def test_an_armed_wake_never_deep_sleeps_without_a_wake_source() -> None:
    """`M5.Power.powerOff()` is not a safe fallback on this board.

    Power_Class::_powerOff() ends in esp_deep_sleep_start() with no wake
    source on the S3 - the ext1 rescue below it is compiled out for anything
    but C5/C61, and board_M5PaperColor has no POWER_HOLD pin to pulse. The
    armed PMIC timer cannot recover that either: its action is POWERON, and
    the system never powered off. Result would be 5-10 mA (HARDWARE.md 6.3)
    with nothing to end it - a flat cell in about two days and a dark wall.

    So the branch that already has a wake armed must set the S3's own timer
    before sleeping, instead of falling through to M5Unified.
    """
    text = _read(APP / "firmware" / "src" / "power.cpp")

    assert "if (wake_armed && !give_up)" in text, (
        "the armed-wake fallback in powerOff() is gone. Without it a PMIC that "
        "stops answering between armWake() and shutdown leaves the board in a "
        "deep sleep with no wake source."
    )
    # One in that fallback, one in the no-wake-armed branch below it.
    assert text.count("esp_sleep_enable_timer_wakeup") >= 2, (
        "powerOff() has fewer S3 timer arms than the two paths that need one"
    )
