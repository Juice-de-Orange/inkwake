"""What makes a file an inkwake firmware image, checked without a board.

One set of checks with two callers: `python -m app.cli firmware add`, which
refuses to register anything that fails them, and
`firmware/tools/verify-image.py`, which runs them on a workstation right after
the build. They live here, inside the `app` package, because the server image
ships only `app/` -- the tool imports this module, not the other way round.
Standard library only, so the tool needs no virtualenv.

Why each check exists, because none of them is hypothetical:

**It is an application image.** Byte 0 must be 0xE9 and there must be an
`esp_app_desc_t` magic word at offset 0x20. This separates `firmware.bin` from
`firmware.elf`, from an image built for another chip, and -- the expensive one --
from a MERGED flash image, which starts at 0x0 with the bootloader. Flashed at
the OTA offset that produces a partition that cannot boot, on a device whose
BOOT button sits behind a wall mount. The firmware checks the same two values
before it erases anything, so this is the second of two nets; the point of
having it here is that it fails on a workstation instead of on the wall.

**It is complete.** The header says how many segments follow and each segment
says how long it is, so the end of the image is computable: after the last
segment comes padding to a 16-byte boundary whose last byte is an XOR checksum
over all segment data, then -- when the header says so, and PlatformIO's builds
do -- the SHA-256 of everything before it. A download that stopped halfway
still starts with 0xE9 and still carries the version marker, which is exactly
how a truncated image used to be registered without a word. The board's
bootloader would refuse it, after the download and the flash write were paid.

**The version is read out of the image, not typed.** The device refuses to
download a digest it has already seen, so a mismatch between the registered
version and the version in the binary can no longer cause the download-flash-loop
it caused on the sibling device. It can still cause confusion -- `firmware list`
would name a release the board never reports -- so the number is read from the
marker symbol, and `firmware add --version` is compared with it.

**Size.** Against one OTA slot (6400 kB with `default_16MB.csv`) and against
the window the board itself accepts for a download (`OTA_MIN_IMAGE_BYTES` and
`OTA_MAX_IMAGE_BYTES` in firmware/src/config.h). The current image is about a
fifth of a slot, so this does not catch a tight fit; it catches a file that is
not what somebody thought it was.

**No device token.** This project has no `build_opt.h`, so there is nothing
routine to leak -- but an image lands in the data volume, which goes into every
backup, and it is served to the device over the network. A regression
that compiled a token in would be worth catching before either happens. The
finding is reported by shape and offset, never by value.

Only the token shape, deliberately: the WiFi-password heuristic that used to be
here is gone, and the note beside TOKEN_RE says why.

What this does NOT establish: that the image boots. The checksum and the digest
prove the file is the one the build wrote, not that the code in it is right, and
nothing here looks at the partition table, the bootloader or a signature.
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections import Counter
from dataclasses import dataclass
from typing import Optional

ESP_IMAGE_MAGIC = 0xE9
ESP_IMAGE_HEADER_BYTES = 24
ESP_SEGMENT_HEADER_BYTES = 8
ESP_CHECKSUM_SEED = 0xEF
#: esp_image_header_t caps the segment count at ESP_IMAGE_MAX_SEGMENTS.
ESP_MAX_SEGMENTS = 16
#: `chip_id` in esp_image_header_t. The board is an ESP32-S3 (platformio.ini).
ESP_CHIP_ID_ESP32S3 = 0x0009

APP_DESC_OFFSET = ESP_IMAGE_HEADER_BYTES + ESP_SEGMENT_HEADER_BYTES  # 0x20
APP_DESC_MAGIC = 0xABCD5432

#: default_16MB.csv: app0 and app1 are 0x640000 each.
OTA_SLOT_BYTES = 6_553_600

#: The board's own window on a download (firmware/src/config.h). An image
#: outside it can be registered and assigned and is then refused on every wake.
OTA_MIN_IMAGE_BYTES = 512 * 1024
OTA_MAX_IMAGE_BYTES = 4 * 1024 * 1024

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


@dataclass(frozen=True)
class Inspection:
    """What the checks found. An empty `problems` means: register it."""

    version: Optional[str]
    problems: tuple[str, ...]


def _xor(data: bytes) -> int:
    """XOR of all bytes. Counted rather than looped: a byte value that occurs
    an even number of times cancels itself, and Counter runs in C."""
    result = 0
    for value, count in Counter(data).items():
        if count & 1:
            result ^= value
    return result


def _structure_problems(blob: bytes) -> list[str]:
    """Walk the ESP image: header, segments, checksum, appended digest."""
    if not blob or blob[0] != ESP_IMAGE_MAGIC:
        first = f"0x{blob[0]:02X}" if blob else "missing"
        return [
            f"first byte is {first} instead of 0x{ESP_IMAGE_MAGIC:02X} -- this is "
            "not an ESP32 application image. A merged flash image or an .elf bricks "
            "every board that receives it over the air."
        ]
    if len(blob) < APP_DESC_OFFSET + 4:
        return ["file is too short for an app descriptor"]

    problems: list[str] = []
    (magic,) = struct.unpack_from("<I", blob, APP_DESC_OFFSET)
    if magic != APP_DESC_MAGIC:
        problems.append(
            f"app descriptor at 0x{APP_DESC_OFFSET:02X} has magic 0x{magic:08X} "
            f"instead of 0x{APP_DESC_MAGIC:08X}"
        )
    (chip_id,) = struct.unpack_from("<H", blob, 12)
    if chip_id != ESP_CHIP_ID_ESP32S3:
        problems.append(
            f"image header names chip id 0x{chip_id:04X}, the board is an ESP32-S3 "
            f"(0x{ESP_CHIP_ID_ESP32S3:04X})"
        )

    segments = blob[1]
    if not 1 <= segments <= ESP_MAX_SEGMENTS:
        problems.append(f"image header claims {segments} segments, expected 1 to {ESP_MAX_SEGMENTS}")
        return problems

    offset = ESP_IMAGE_HEADER_BYTES
    checksum = ESP_CHECKSUM_SEED
    for number in range(1, segments + 1):
        if offset + ESP_SEGMENT_HEADER_BYTES > len(blob):
            problems.append(
                f"the file ends at {len(blob)} bytes, before the header of segment "
                f"{number} of {segments} -- truncated"
            )
            return problems
        (_, length) = struct.unpack_from("<II", blob, offset)
        offset += ESP_SEGMENT_HEADER_BYTES
        if offset + length > len(blob):
            problems.append(
                f"segment {number} of {segments} needs {length} bytes at offset {offset}, "
                f"the file ends at {len(blob)} -- truncated"
            )
            return problems
        checksum ^= _xor(blob[offset : offset + length])
        offset += length

    # Padding up to a 16-byte boundary; its last byte is the checksum.
    end = (offset | 15) + 1
    hash_appended = blob[23] == 1
    expected_len = end + (32 if hash_appended else 0)
    if len(blob) < expected_len:
        problems.append(
            f"the image is {expected_len} bytes by its own headers, the file has "
            f"{len(blob)} -- truncated"
        )
        return problems
    if blob[end - 1] != checksum:
        problems.append(
            f"segment checksum is 0x{checksum:02X}, the image says 0x{blob[end - 1]:02X} "
            "-- the file was altered or damaged after the build"
        )
    if hash_appended and hashlib.sha256(blob[:end]).digest() != blob[end:expected_len]:
        problems.append(
            "the SHA-256 appended to the image does not match its contents -- the "
            "file was altered or damaged after the build"
        )
    if len(blob) > expected_len:
        problems.append(
            f"{len(blob) - expected_len} bytes follow the end of the image "
            f"(at {expected_len}) -- this is not the plain firmware.bin of a build"
        )
    return problems


def _version(blob: bytes, problems: list[str]) -> Optional[str]:
    hits = [m.end() for m in re.finditer(re.escape(MARKER), blob)]
    if not hits:
        problems.append(
            "version marker not found. kFwMarker in main.cpp is missing or the "
            "linker dropped it -- without the marker nobody can tell from outside "
            "which version this file holds."
        )
        return None
    if len(hits) > 1:
        problems.append(f"version marker occurs {len(hits)} times in the image, expected exactly once")
        return None
    end = blob.find(b"\x00", hits[0])
    version = blob[hits[0] : end if end >= 0 else hits[0] + 33].decode("ascii", "replace")
    if not re.fullmatch(r"[0-9A-Za-z.+_-]{1,32}", version):
        problems.append(f"version string does not look like a version: {version!r}")
    return version


def inspect(blob: bytes) -> Inspection:
    """Run every check. Nothing is raised: the caller decides what a problem costs."""
    problems = _structure_problems(blob)
    version = _version(blob, problems)

    if len(blob) > OTA_SLOT_BYTES:
        problems.append(
            f"{len(blob)} bytes do not fit one OTA slot ({OTA_SLOT_BYTES} bytes, "
            "default_16MB.csv)"
        )
    if not OTA_MIN_IMAGE_BYTES <= len(blob) <= OTA_MAX_IMAGE_BYTES:
        problems.append(
            f"{len(blob)} bytes is outside the {OTA_MIN_IMAGE_BYTES} to {OTA_MAX_IMAGE_BYTES} "
            "bytes the board accepts for a download (firmware/src/config.h)"
        )

    for match in TOKEN_RE.finditer(blob):
        problems.append(
            f"something shaped like a device token at offset 0x{match.start():X} "
            f"({len(match.group(0))} characters). Value deliberately not printed."
        )

    return Inspection(version=version, problems=tuple(problems))
