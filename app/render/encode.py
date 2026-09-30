"""Wire formats: the two byte streams the board may receive, plus their ETag.

Two encodings, because the two hardware situations are genuinely different
(HARDWARE.md 9.4):

  encode_png   Indexed PNG, six palette entries, bit depth 4. The default.
               This board has 8 MB PSRAM, so inflating costs almost nothing,
               and the ~110 kB saved is one to two seconds less radio-on -
               the dominant energy post of a wake.
  encode_raw   The packed 4 bpp framebuffer, socket straight into memory,
               exactly 120 000 bytes at 400x600. No decoder, and a fixed
               radio window. The fallback for hardware without the RAM.

The ETag rule
-------------
frame_etag hashes the *packed panel pixels* - the output of encode_raw - and
nothing else.

Hashing the source data instead looks equivalent and is not: every frame is
assembled with a fresh timestamp, so that hash would differ on every wake
even when the picture is identical. The board would then download and
repaint, and a Spectra 6 repaint is 15-30 s of exactly the energy the cache
existed to save (9.5).

Hashing the PNG has the mirror problem: a different Pillow release, another
compression level or one added metadata chunk moves the hash while the
picture on the wall stays the same. The packed nibbles are the only
canonical form - they are literally what the panel ends up holding.

Both encoders refuse a frame containing a colour outside DEVICE_RGB rather
than rounding it. Rounding is what M5GFX would do anyway, silently, in a
flat 400 km away; a rejected frame at least reaches the log.
"""

from __future__ import annotations

import hashlib
import io
from typing import Union

import numpy as np
from PIL import Image

from . import palette

#: Either accepted input. The layout draws with Pillow, so a PIL Image is the
#: normal case; arrays exist because the dither path works in numpy and
#: round-tripping it through Pillow just to encode would be pointless.
ImageLike = Union[Image.Image, np.ndarray]

PNG_MEDIA_TYPE = "image/png"
RAW_MEDIA_TYPE = "application/octet-stream"

#: DEVICE_RGB flattened for putpalette. Six entries and no padding, which is
#: also what makes Pillow choose bit depth 4 for the PNG - see encode_png.
_PNG_PALETTE = [component for ink in palette.DEVICE_RGB for component in ink]


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------


def _as_rgb_array(img: ImageLike) -> np.ndarray:
    """Normalise either accepted input into a contiguous (H, W, 3) uint8 array.

    A float array is rejected instead of cast. Float RGB in [0, 1] would
    truncate to all zeros, and all-zero is a *valid* all-black frame that
    every guard downstream happily accepts - the mistake would only surface
    as a black panel. This is the one place it is still visible.
    """
    if isinstance(img, Image.Image):
        # convert() on P or RGBA is lossless enough for our purposes; anything
        # it invents outside the palette is caught by assert_palette_exact.
        arr = np.asarray(img if img.mode == "RGB" else img.convert("RGB"))
    elif isinstance(img, np.ndarray):
        if img.dtype != np.uint8:
            raise TypeError(
                f"expected a uint8 RGB array, got dtype {img.dtype}; "
                "float pixel values would truncate to a silently black frame"
            )
        arr = img
    else:
        raise TypeError(
            f"expected a PIL Image or a uint8 RGB array, got {type(img).__name__}"
        )

    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected an (H, W, 3) RGB image, got shape {arr.shape}")
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError(f"refusing to encode an empty frame, shape {arr.shape}")

    return np.ascontiguousarray(arr, dtype=np.uint8)


# --------------------------------------------------------------------------
# Encoders
# --------------------------------------------------------------------------


def encode_png(img: ImageLike) -> bytes:
    """Encode a finished frame as a palette PNG holding exactly six colours.

    The image is built from ink indices rather than quantised from RGB.
    Image.quantize() would re-derive the palette mapping and dither by
    default, i.e. undo the decision the render pipeline already made; going
    through rgb_to_indices keeps the mapping exact and raises on anything
    unexpected instead.

    The six-entry palette is deliberate beyond size: Pillow picks the PNG bit
    depth from the palette length, so <= 16 entries yield a 4 bpp image -
    the panel's own depth, at no extra cost.
    """
    arr = _as_rgb_array(img)
    palette.assert_palette_exact(arr)
    indices = palette.rgb_to_indices(arr)

    height, width = indices.shape
    out = Image.frombytes("P", (width, height), indices.tobytes())
    out.putpalette(_PNG_PALETTE)

    buf = io.BytesIO()
    # No pnginfo is passed, so the file is IHDR/PLTE/IDAT/IEND and nothing
    # else - byte-identical across runs, which keeps frame diffing honest.
    out.save(buf, format="PNG", optimize=True, compress_level=9)
    return buf.getvalue()


def encode_raw(img: ImageLike) -> bytes:
    """Encode a finished frame as the panel's packed 4 bpp framebuffer.

    Two pixels per byte, high nibble = the left pixel, row-major, no padding
    (4.2). At the native 400x600 this is exactly 120 000 bytes; the caller is
    responsible for having handed us native geometry, since this module has
    no opinion about layout.
    """
    arr = _as_rgb_array(img)
    # Ahead of rgb_to_indices only for the error message: this one names the
    # offending colours, rgb_to_indices only reports that some exist.
    palette.assert_palette_exact(arr)
    return palette.pack_nibbles(palette.rgb_to_indices(arr))


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def frame_etag(payload: bytes) -> str:
    """Strong ETag over finished pixel bytes. Pass encode_raw(img), nothing else.

    Returns the quoted form, e.g. '"3ac1..."'. That is the complete header
    value - assign it to ETag as-is and compare it against If-None-Match as-is.
    An unquoted token is not a valid entity-tag, and quoting it a second time
    produces an ETag that can never match, which shows up as a board that
    re-downloads and repaints on every single wake.
    """
    return f'"{hashlib.sha256(payload).hexdigest()}"'
