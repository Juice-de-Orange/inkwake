"""Spectra 6 colour handling: the six inks, Lab matching, and nibble packing.

Read this before touching any rendering code. Getting the colour path wrong is
the single most error-prone detail on this panel (HARDWARE.md 4.2) and the
failure is silent - you get a picture, it is just the wrong colours.

Three palettes exist and they are NOT interchangeable:

  NATIVE_NIBBLE   the 4-bit codes the panel itself speaks. 0x4 and 0x7 are
                  undefined and must never be emitted.

  DEVICE_RGB      the RGB values M5GFX uses on-device. This is what we write
                  into the PNG.

  MEASURED_RGB    what the ink actually looks like under reflected light.
                  This is what we dither against.

Why two of them, and why that split matters
-------------------------------------------
The device is not a dumb framebuffer. M5GFX takes whatever RGB we send and
quantises it *again* on arrival, mapping each pixel to its nearest entry in
DEVICE_RGB. So if we dithered against MEASURED_RGB and shipped those muddy
values, M5GFX would re-quantise them and our carefully computed error
diffusion would be silently mangled a second time.

The fix is to separate the two jobs:

  * decide *which ink* each pixel gets by matching in Lab against
    MEASURED_RGB - the real appearance, so dark green stops collapsing into
    black the way it does under plain RGB distance (4.6);
  * then paint that decision using DEVICE_RGB, so M5GFX's nearest-colour pass
    is an exact hit and becomes a no-op.

Pair this with setEpdMode(epd_fastest) on the firmware side. The mode names
mislead: epd_fastest means *no* on-device dithering, which is precisely what
we want once the server has already done the work (4.7).

Text never goes through any of this. The layout draws directly in DEVICE_RGB
and assert_palette_exact() enforces it, so glyph edges reach the panel
untouched.
"""

from __future__ import annotations

import numpy as np

# Index order is fixed: [black, white, yellow, red, blue, green].
# Every array in this module uses it. Do not reorder - NATIVE_NIBBLE maps
# positionally onto it, and 0x4 is deliberately skipped.
NAMES = ("black", "white", "yellow", "red", "blue", "green")

#: The panel's own 4-bit codes, in NAMES order. Note the gap: blue is 0x5, not
#: 0x4. Verified identically in Waveshare's epd4in0e.py (branch Development),
#: M5GFX Panel_ED2208.cpp and ESPHome epaper_spi_spectra_e6.cpp (4.2).
NATIVE_NIBBLE = (0x0, 0x1, 0x2, 0x3, 0x5, 0x6)

#: What M5GFX renders these inks as. The PNG we ship must contain exactly
#: these values and nothing else (4.6 palette (b)).
DEVICE_RGB = (
    (0, 0, 0),        # black
    (255, 255, 255),  # white
    (255, 243, 56),   # yellow
    (191, 0, 0),      # red
    (100, 64, 255),   # blue
    (67, 138, 28),    # green
)

#: Measured reflective appearance, from the epaper-dithering package
#: (SPECTRA_7_3_6COLOR_V2). Used only to decide which ink a photo pixel wants.
#: Note how far "white" is from #FFFFFF - quantising against idealised RGB is
#: the #1 cause of muddy output (4.6 palette (c)).
MEASURED_RGB = (
    (31, 24, 41),     # black    #1F1829
    (168, 180, 182),  # white    #A8B4B6
    (180, 173, 0),    # yellow   #B4AD00
    (113, 24, 19),    # red      #711813
    (36, 70, 139),    # blue     #24468B
    (50, 84, 60),     # green    #32543C
)

# Convenience aliases so layout code reads in colour names, never in indices.
BLACK, WHITE, YELLOW, RED, BLUE, GREEN = DEVICE_RGB

#: Pairs that are unreadable on this panel, from the measured luminances
#: (4.7). Yellow on white is the notorious one: near-identical luminance.
#: assert_readable() refuses them so a layout tweak cannot quietly produce
#: an invisible line of text.
UNSAFE_PAIRS = frozenset(
    {
        frozenset({YELLOW, WHITE}),
        frozenset({GREEN, BLACK}),
        frozenset({BLUE, BLACK}),
        frozenset({GREEN, BLUE}),
    }
)


# --------------------------------------------------------------------------
# sRGB -> CIE Lab (D65)
# --------------------------------------------------------------------------


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """Undo the sRGB transfer function. Input and output are float in [0, 1]."""
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


# sRGB primaries under a D65 white point.
_M_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_D65 = np.array([0.95047, 1.00000, 1.08883])


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert uint8 sRGB to CIE Lab.

    Accepts any shape ending in 3; returns the same shape. Lab is what makes
    "which ink is closest" agree with human vision - in plain RGB, dark green
    and dark blue both read as "nearly black" and collapse together.
    """
    arr = np.asarray(rgb, dtype=np.float64)
    if arr.shape[-1] != 3:
        raise ValueError(f"expected trailing axis of 3, got {arr.shape}")

    flat = arr.reshape(-1, 3) / 255.0
    xyz = _srgb_to_linear(flat) @ _M_RGB_TO_XYZ.T / _D65

    # The CIE f() companding, with the linear segment near black.
    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0)

    lab = np.empty_like(f)
    lab[:, 0] = 116.0 * f[:, 1] - 16.0
    lab[:, 1] = 500.0 * (f[:, 0] - f[:, 1])
    lab[:, 2] = 200.0 * (f[:, 1] - f[:, 2])
    return lab.reshape(arr.shape)


#: Lab coordinates of the six measured inks. Precomputed once; the dither
#: inner loop is hot enough that recomputing per pixel is wasteful.
MEASURED_LAB = srgb_to_lab(np.array(MEASURED_RGB, dtype=np.uint8))

#: Same six inks as lookup tables for painting decisions back out.
DEVICE_LUT = np.array(DEVICE_RGB, dtype=np.uint8)
MEASURED_LUT = np.array(MEASURED_RGB, dtype=np.float64)


def nearest_ink(lab: np.ndarray, inks: tuple[int, ...] | None = None) -> np.ndarray:
    """Index of the perceptually closest ink for each Lab colour.

    lab has shape (..., 3); the result drops the trailing axis.

    `inks` narrows the choice to a subset, given as palette indices - the
    infrared path in dither.py uses it to reach for black and white only. The
    result stays a palette index either way, so DEVICE_LUT, NATIVE_NIBBLE and
    dither's _MEASURED_LINEAR keep indexing correctly downstream.
    """
    if inks is None:
        diff = lab[..., None, :] - MEASURED_LAB  # (..., 6, 3)
        return np.argmin(np.einsum("...ij,...ij->...i", diff, diff), axis=-1)

    chosen = np.asarray(inks, dtype=np.intp)
    diff = lab[..., None, :] - MEASURED_LAB[chosen]
    return chosen[np.argmin(np.einsum("...ij,...ij->...i", diff, diff), axis=-1)]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


class PaletteError(AssertionError):
    """A frame contained a colour the panel cannot show."""


def _pack_rgb(arr: np.ndarray) -> np.ndarray:
    """Pack an (..., 3) uint8 array into one uint32 per pixel for set logic."""
    a = np.asarray(arr, dtype=np.uint8)
    return (
        (a[..., 0].astype(np.uint32) << 16)
        | (a[..., 1].astype(np.uint32) << 8)
        | a[..., 2].astype(np.uint32)
    )


def assert_palette_exact(rgb: np.ndarray) -> None:
    """Fail loudly if rgb holds anything outside DEVICE_RGB.

    This runs on every finished frame. It is the guard that catches the whole
    family of silent colour bugs: an anti-aliased glyph edge, a stray #808080
    separator, a JPEG artefact that survived compositing. Any of those reach
    the panel as *something* - M5GFX will happily map grey to the nearest ink
    - so without this check the failure never surfaces as an error, only as a
    picture that looks slightly wrong 400 km away.
    """
    packed = _pack_rgb(np.asarray(rgb, dtype=np.uint8).reshape(-1, 3))
    allowed = {(r << 16) | (g << 8) | b for r, g, b in DEVICE_RGB}
    stray = set(np.unique(packed).tolist()) - allowed
    if stray:
        raise PaletteError(
            "frame contains %d colour(s) outside the Spectra 6 palette: %s"
            % (len(stray), ", ".join(f"#{c:06X}" for c in sorted(stray)[:8]))
        )


def assert_readable(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> None:
    """Refuse ink pairs that are unreadable on this panel (4.7)."""
    if frozenset({fg, bg}) in UNSAFE_PAIRS:
        raise PaletteError(
            f"unreadable colour pair on Spectra 6: {fg} on {bg}. "
            "Safe pairs are black/white, red/white, blue/white, green/white, "
            "black/yellow."
        )


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


def pack_nibbles(indices: np.ndarray) -> bytes:
    """Pack ink indices into the panel's 4 bpp format.

    indices is a (H, W) array of 0..5 in NAMES order. Two pixels per byte,
    high nibble = the left (even-x) pixel, row-major, no padding (4.2).

    At the native 400x600 this yields exactly 120 000 bytes.
    """
    idx = np.asarray(indices)
    if idx.ndim != 2:
        raise ValueError(f"expected a 2-D index array, got shape {idx.shape}")
    h, w = idx.shape
    if w % 2:
        raise ValueError(f"width must be even to pack 2 px/byte, got {w}")
    if idx.size and (idx.min() < 0 or idx.max() >= len(NATIVE_NIBBLE)):
        raise ValueError("index out of range for the 6-ink palette")

    codes = np.array(NATIVE_NIBBLE, dtype=np.uint8)[idx]
    return ((codes[:, 0::2] << 4) | codes[:, 1::2]).astype(np.uint8).tobytes()


def rgb_to_indices(rgb: np.ndarray) -> np.ndarray:
    """Map an exact-DEVICE_RGB image back to ink indices.

    Only valid on a frame that has passed assert_palette_exact; it matches
    exactly rather than approximately, so an unexpected colour raises instead
    of being quietly rounded to its neighbour.
    """
    arr = np.asarray(rgb, dtype=np.uint8)
    packed = _pack_rgb(arr)

    out = np.full(arr.shape[:2], -1, dtype=np.int16)
    for i, (r, g, b) in enumerate(DEVICE_RGB):
        out[packed == ((r << 16) | (g << 8) | b)] = i
    if (out < 0).any():
        raise PaletteError(
            "image holds colours outside DEVICE_RGB; run assert_palette_exact first"
        )
    return out.astype(np.uint8)
