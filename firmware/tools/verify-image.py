#!/usr/bin/env python3
"""Check a built firmware image before it is registered for OTA.

    python firmware/tools/verify-image.py firmware/.pio/build/papercolor/firmware.bin

Prints the version and the SHA-256 and exits non-zero on anything that would
reach the board as a problem. Run it every time; the checks are cheap and the
failure mode they guard against is a ladder and a USB cable.

Why each check exists, because none of them is hypothetical:

**It is an application image.** Byte 0 must be 0xE9 and there must be an
`esp_app_desc_t` magic word at offset 0x20. This separates `firmware.bin` from
`firmware.elf`, from an image built for another chip, and -- the expensive one --
from a MERGED flash image, which starts at 0x0 with the bootloader. Flashed at
the OTA offset that produces a partition that cannot boot, on a device whose
BOOT button sits behind a wall mount. The firmware checks the same two values
before it erases anything, so this is the second of two nets; the point of
having it here is that it fails on a workstation instead of on the wall.

**The version is read out of the image, not typed.** The device refuses to
download a digest it has already seen, so a mismatch between the registered
version and the version in the binary can no longer cause the download-flash-loop
it caused on the sibling device. It can still cause confusion -- `firmware list`
would name a release the board never reports -- so the number is read from the
marker symbol and printed, and `app.cli firmware add --version` should get that.

**Size against one OTA slot.** 6400 kB with `default_16MB.csv`. The current
image is about a fifth of that, so this does not catch a tight fit; it catches a
file that is not what somebody thought it was.

**No device token.** This project has no `build_opt.h`, so there is nothing
routine to leak -- but an image lands in the data volume, which goes into every
backup, and it is served to the device over the network. A regression
that compiled a token in would be worth catching before either happens. The
finding is reported by shape and offset, never by value.

Only the token shape, deliberately: the WiFi-password heuristic that used to be
here is gone, and the note beside TOKEN_RE says why.
"""

from __future__ import annotations

import hashlib
import re
import struct
import sys
from pathlib import Path

ESP_IMAGE_MAGIC = 0xE9
APP_DESC_OFFSET = 0x20
APP_DESC_MAGIC = 0xABCD5432

#: default_16MB.csv: app0 and app1 are 0x640000 each.
OTA_SLOT_BYTES = 6_553_600

MARKER = b"INKWAKE-FW-VERSION:"

#: `<hex id>.<base64url secret>` -- the device token shape. Deliberately narrow:
#: a looser pattern matches base64 blobs in the certificate bundle and cries
#: wolf on every build.
TOKEN_RE = re.compile(rb"[0-9a-f]{16,64}\.[A-Za-z0-9_-]{30,60}")

# There is deliberately NO WiFi-password heuristic here, and the attempt is
# worth recording. "A password-shaped string next to a password-shaped label"
# matched four times on a clean build -- every hit a name in ESP-IDF's Wi-Fi
# authmode table (the WPA, WPA2, WAPI and WPA3 variants). A check that fails on every honest
# image is worse than no check, because it teaches whoever runs it to skip the
# output. The token pattern above is narrow enough to mean something; a WiFi
# password compiled into this firmware would have to be caught by review.


def fail(message: str) -> None:
    print(f"ERROR: {message}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        fail(f"{path} does not exist")
        return 2

    blob = path.read_bytes()
    problems = 0

    # -- is it an app image ------------------------------------------------
    if not blob or blob[0] != ESP_IMAGE_MAGIC:
        fail(
            f"first byte is 0x{blob[0]:02X} instead of 0x{ESP_IMAGE_MAGIC:02X} -- this is "
            "not an ESP32 application image. A merged flash image or an .elf bricks "
            "every board that receives it over the air."
        )
        problems += 1
    elif len(blob) < APP_DESC_OFFSET + 4:
        fail("file is too short for an app descriptor")
        problems += 1
    else:
        (magic,) = struct.unpack_from("<I", blob, APP_DESC_OFFSET)
        if magic != APP_DESC_MAGIC:
            fail(
                f"app descriptor at 0x{APP_DESC_OFFSET:02X} has magic 0x{magic:08X} "
                f"instead of 0x{APP_DESC_MAGIC:08X}"
            )
            problems += 1

    # -- version, read out of the image ------------------------------------
    hits = [m.end() for m in re.finditer(re.escape(MARKER), blob)]
    version = None
    if not hits:
        fail(
            "version marker not found. kFwMarker in main.cpp is missing or the "
            "linker dropped it -- without the marker nobody can tell from outside "
            "which version this file holds."
        )
        problems += 1
    elif len(hits) > 1:
        fail(f"version marker occurs {len(hits)} times in the image, expected exactly once")
        problems += 1
    else:
        end = blob.index(b"\x00", hits[0])
        version = blob[hits[0] : end].decode("ascii", "replace")
        if not re.fullmatch(r"[0-9A-Za-z.+_-]{1,32}", version):
            fail(f"version string does not look like a version: {version!r}")
            problems += 1

    # -- size ---------------------------------------------------------------
    if len(blob) > OTA_SLOT_BYTES:
        fail(
            f"{len(blob)} bytes do not fit one OTA slot ({OTA_SLOT_BYTES} bytes, "
            "default_16MB.csv)"
        )
        problems += 1

    # -- credentials --------------------------------------------------------
    for match in TOKEN_RE.finditer(blob):
        fail(
            f"something shaped like a device token at offset 0x{match.start():X} "
            f"({len(match.group(0))} characters). Value deliberately not printed."
        )
        problems += 1

    sha = hashlib.sha256(blob).hexdigest()
    print(f"file      {path}")
    print(f"version   {version or '?'}")
    print(f"size      {len(blob)} bytes ({100 * len(blob) / OTA_SLOT_BYTES:.0f} % of a slot)")
    print(f"sha256    {sha}")

    if problems:
        print(f"\n{problems} problem(s) -- do NOT register this image.")
        return 1
    print(f"\nOK. Register it with: python -m app.cli firmware add <file> --version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
