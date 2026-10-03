#!/usr/bin/env python3
"""Check a built firmware image before it is registered for OTA.

    python firmware/tools/verify-image.py firmware/.pio/build/papercolor/firmware.bin

Prints the version and the SHA-256 and exits non-zero on anything that would
reach the board as a problem. Run it every time; the checks are cheap and the
failure mode they guard against is a ladder and a USB cable.

The checks themselves, and why each one exists, are in `app/firmware_image.py`:
application image and chip, completeness (segments, checksum, appended
SHA-256), the version marker, size, and no device token. `python -m app.cli
firmware add` runs the very same code on the server and refuses what fails
here, so this script is the early warning, not the only gate.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# The checks live in the `app` package because the server image ships only
# that. Run from anywhere: the repository root is two levels up from here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.firmware_image import OTA_SLOT_BYTES, inspect  # noqa: E402


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
    inspection = inspect(blob)
    for problem in inspection.problems:
        fail(problem)

    sha = hashlib.sha256(blob).hexdigest()
    print(f"file      {path}")
    print(f"version   {inspection.version or '?'}")
    print(f"size      {len(blob)} bytes ({100 * len(blob) / OTA_SLOT_BYTES:.0f} % of a slot)")
    print(f"sha256    {sha}")

    if inspection.problems:
        print(f"\n{len(inspection.problems)} problem(s) -- do NOT register this image.")
        return 1
    print(
        "\nOK. Register it with: python -m app.cli firmware add <file> "
        f"--version {inspection.version}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
