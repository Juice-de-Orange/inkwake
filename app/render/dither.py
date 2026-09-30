"""Error-diffusion dithering for photographs on the Spectra 6 panel.

Photos only. Text, icons, thin lines and flat UI fills never pass through
here - the layout draws those straight in DEVICE_RGB, because at ~180 PPI a
dithered glyph edge reads as noise rather than as a letter (HARDWARE.md 4.7).
Composite text *after* the diffusion pass, never before.

Three spaces are in play at once and mixing them up is the classic way to get
a picture that is merely subtly wrong:

  * the *decision* - which of the six inks a pixel gets - happens in Lab
    against MEASURED_RGB, so dark green and dark blue stop collapsing into
    black the way they do under plain RGB distance (palette.py, 4.6);
  * the *bookkeeping* - the residual pushed onto the neighbours - happens in
    linear sRGB. Lab is not additive: adding a Lab difference to a Lab colour
    lands somewhere the eye did not ask for, and across a large flat area the
    accumulated drift shows up as a colour cast;
  * the *painting* uses DEVICE_RGB, so M5GFX's own nearest-colour pass on
    arrival is an exact hit and becomes a no-op.

Serpentine scan order (every second row right-to-left) is not cosmetic: a
strictly left-to-right scan makes the residual travel in one direction only
and produces the diagonal "worm" trails that error diffusion is known for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from . import palette

# --------------------------------------------------------------------------
# sRGB transfer function
# --------------------------------------------------------------------------
#
# palette.srgb_to_lab() speaks 0..255 sRGB, but the error accumulator lives in
# linear light, so this module needs the transfer function in *both*
# directions. Defining the pair here keeps the round trip exact by
# construction: _linear_to_srgb is the algebraic inverse of _srgb_to_linear,
# so feeding an accumulator value back through palette.srgb_to_lab() loses
# nothing.


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """sRGB [0, 1] -> linear light [0, 1]."""
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """Linear light [0, 1] -> sRGB [0, 1]. Input must not be negative."""
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1.0 / 2.4) - 0.055)


#: The six measured inks in linear light, as plain Python floats. The inner
#: loop subtracts one of these per pixel; a numpy scalar lookup there costs
#: more than the whole rest of the iteration.
_MEASURED_LINEAR: tuple[tuple[float, ...], ...] = tuple(
    tuple(float(v) for v in row)
    for row in _srgb_to_linear(np.array(palette.MEASURED_RGB, dtype=np.float64) / 255.0)
)

# --------------------------------------------------------------------------
# Ink lookup table
# --------------------------------------------------------------------------
#
# Deciding an ink honestly means one Lab conversion plus six distance
# computations per pixel. Vectorised that is cheap, but error diffusion is
# serial along x, so it would run as 120 000 tiny numpy calls - seconds, not
# milliseconds. Instead the decision is baked once into a 3-D table and the
# inner loop does a single byte lookup.
#
# The table is indexed in *square-root* space, not in linear light. A linear
# grid spends nearly all its resolution on the highlights and lands its
# coarsest steps exactly where the six inks sit closest together (the four
# dark ones are all below 12 % luminance), which is where a wrong decision is
# most visible. sqrt() is a decent stand-in for the sRGB curve and costs one
# machine instruction.

# 96 steps per channel costs 864 KB and ~0.6 s once per process. It is the
# point where the table stops being the dominant error: the decisions it still
# gets "wrong" are ties, where the two candidate inks are under 1 dE apart and
# the worst case stays inside the ~3 dE that is visible at all - and diffusion
# absorbs even that, because the residual is measured against whichever ink
# was actually chosen.
_LUT_N = 96
_LUT_MAX = _LUT_N - 1
_LUT_CHUNK = 65536

# Keyed by ink subset, because the infrared path below needs a table of its
# own. None means all six. A second table costs the same ~0.6 s and 864 KB as
# the first, paid once per process and only if a neutral photo ever arrives.
_LUT_CACHE: dict[tuple[int, ...] | None, bytes] = {}


def _build_ink_lut(inks: tuple[int, ...] | None = None) -> bytes:
    """Precompute nearest_ink() over the sqrt-encoded RGB cube."""
    axis = (np.arange(_LUT_N, dtype=np.float64) / _LUT_MAX) ** 2  # linear light
    total = _LUT_N**3
    out = np.empty(total, dtype=np.uint8)

    # Chunked, and the coordinates are unpacked from the flat index rather than
    # meshgridded: nearest_ink() alone materialises an (n, 6, 3) float64
    # scratch array, so doing the whole cube at once would burn ~150 MB to
    # produce 864 KB.
    for start in range(0, total, _LUT_CHUNK):
        ids = np.arange(start, min(start + _LUT_CHUNK, total))
        block = np.stack(
            (
                axis[ids // (_LUT_N * _LUT_N)],
                axis[(ids // _LUT_N) % _LUT_N],
                axis[ids % _LUT_N],
            ),
            axis=-1,
        )
        lab = palette.srgb_to_lab(_linear_to_srgb(block) * 255.0)
        out[start : start + len(ids)] = palette.nearest_ink(lab, inks).astype(np.uint8)
    return out.tobytes()


def _ink_lut(inks: tuple[int, ...] | None = None) -> bytes:
    """The ink table for one subset, built on first use and kept for the process."""
    table = _LUT_CACHE.get(inks)
    if table is None:
        table = _build_ink_lut(inks)
        _LUT_CACHE[inks] = table
    return table


# --------------------------------------------------------------------------
# Kernels
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Kernel:
    """One error-diffusion kernel, split by how far ahead it reaches.

    Offsets are in *scan* order, so they stay valid on a right-to-left row:
    `ahead` is applied inside the serial loop, `below`/`below2` are applied to
    whole rows at once afterwards and get mirrored when the row ran backwards.
    """

    name: str
    ahead: tuple[tuple[int, float], ...]  # same row, not yet visited
    below: tuple[tuple[int, float], ...]  # row + 1
    below2: tuple[tuple[int, float], ...] = ()  # row + 2


_FLOYD = _Kernel(
    name="floyd",
    ahead=((1, 7 / 16),),
    below=((-1, 3 / 16), (0, 5 / 16), (1, 1 / 16)),
)

#: Atkinson hands on only 6/8 of the residual; the missing quarter is thrown
#: away on purpose. That is what crushes the muddy midtones this palette is
#: prone to and why it wins on faces and fur (4.7) - not a missing term.
_ATKINSON = _Kernel(
    name="atkinson",
    ahead=((1, 1 / 8), (2, 1 / 8)),
    below=((-1, 1 / 8), (0, 1 / 8), (1, 1 / 8)),
    below2=((0, 1 / 8),),
)

_KERNELS: dict[str, _Kernel] = {
    "floyd": _FLOYD,
    "floyd-steinberg": _FLOYD,
    "floyd_steinberg": _FLOYD,
    "fs": _FLOYD,
    "atkinson": _ATKINSON,
}

#: The method names worth putting in a config or an API. Aliases exist but are
#: not advertised.
METHODS: tuple[str, ...] = ("floyd", "atkinson")


def _kernel_for(method: str) -> _Kernel:
    try:
        return _KERNELS[method.strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown dither method {method!r}; expected one of {', '.join(METHODS)}"
        ) from None


# --------------------------------------------------------------------------
# The diffusion pass
# --------------------------------------------------------------------------


def _shift_add(dst: np.ndarray, src: np.ndarray, off: int) -> None:
    """dst[x + off] += src[x], discarding whatever falls off either edge."""
    n = dst.shape[0]
    if off == 0:
        dst += src
    elif 0 < off < n:
        dst[off:] += src[: n - off]
    elif -n < off < 0:
        dst[:off] += src[-off:]
    # Otherwise the image is narrower than the kernel reaches and the whole
    # row falls off the edge - legal, and the residual is simply lost.


def _scan_row(
    cur: np.ndarray,
    lut: bytes,
    w1: float,
    w2: float,
) -> tuple[list[int], np.ndarray]:
    """Quantise one row in scan order and return its ink indices and residual.

    This is the only genuinely serial part of the algorithm - pixel x + 1 must
    see the error from pixel x before it is quantised - so it runs as a plain
    Python loop over lists. Lists of floats beat numpy here by an order of
    magnitude: every access is a scalar, and a numpy scalar access costs more
    than the arithmetic around it.

    `cur` is linear light, already carrying the residual from the rows above.
    """
    rr = cur[:, 0].tolist()
    gg = cur[:, 1].tolist()
    bb = cur[:, 2].tolist()
    w = len(rr)

    idx = [0] * w
    er = [0.0] * w
    eg = [0.0] * w
    eb = [0.0] * w

    measured = _MEASURED_LINEAR
    sqrt = math.sqrt
    n = _LUT_N
    nmax = _LUT_MAX

    for x in range(w):
        # Clamp before quantising *and* before measuring the residual. An
        # unclamped accumulator runs away on any large flat area: the measured
        # white sits at only ~42 % linear, so a field of paper-white keeps
        # handing +0.58 to its neighbours forever. Clamping bounds it without
        # changing any decision, since nothing outside [0, 1] is displayable.
        r = rr[x]
        if r < 0.0:
            r = 0.0
        elif r > 1.0:
            r = 1.0
        g = gg[x]
        if g < 0.0:
            g = 0.0
        elif g > 1.0:
            g = 1.0
        b = bb[x]
        if b < 0.0:
            b = 0.0
        elif b > 1.0:
            b = 1.0

        k = lut[
            (int(sqrt(r) * nmax + 0.5) * n + int(sqrt(g) * nmax + 0.5)) * n
            + int(sqrt(b) * nmax + 0.5)
        ]
        idx[x] = k

        ink = measured[k]
        dr = r - ink[0]
        dg = g - ink[1]
        db = b - ink[2]
        er[x] = dr
        eg[x] = dg
        eb[x] = db

        nx = x + 1
        if nx < w:
            rr[nx] += dr * w1
            gg[nx] += dg * w1
            bb[nx] += db * w1
            if w2:
                nx += 1
                if nx < w:
                    rr[nx] += dr * w2
                    gg[nx] += dg * w2
                    bb[nx] += db * w2

    err = np.empty((w, 3), dtype=np.float64)
    err[:, 0] = er
    err[:, 1] = eg
    err[:, 2] = eb
    return idx, err


def _diffuse(
    linear: np.ndarray,
    kernel: _Kernel,
    inks: tuple[int, ...] | None = None,
) -> np.ndarray:
    """Error-diffuse a linear-light (h, w, 3) image into ink indices (h, w).

    `inks` restricts which inks may be spent; None means all six.
    """
    h, w = linear.shape[:2]
    lut = _ink_lut(inks)
    out = np.empty((h, w), dtype=np.uint8)

    # The serial loop is unrolled for exactly two forward neighbours, so say so
    # here rather than letting a third one be silently dropped by a future
    # kernel (Jarvis and Stucki both reach three).
    ahead = dict(kernel.ahead)
    w1 = ahead.pop(1, 0.0)
    w2 = ahead.pop(2, 0.0)
    if ahead:
        raise ValueError(
            f"kernel {kernel.name!r} diffuses to x+{sorted(ahead)}; "
            "_scan_row only unrolls x+1 and x+2"
        )

    # Residual owed to the rows we have not reached yet. Only two are ever
    # outstanding (Atkinson reaches two rows down), so this is O(w), not O(h*w).
    pend1 = np.zeros((w, 3), dtype=np.float64)
    pend2 = np.zeros((w, 3), dtype=np.float64)

    for y in range(h):
        cur = linear[y] + pend1
        pend1, pend2 = pend2, np.zeros((w, 3), dtype=np.float64)

        reverse = bool(y & 1)
        if reverse:
            cur = cur[::-1]

        idx, err = _scan_row(cur, lut, w1, w2)

        if reverse:
            # Back to natural order. The kernel is mirrored with it, which is
            # exactly what serpentine means: the residual leans the way the
            # scan was travelling.
            out[y] = idx[::-1]
            err = err[::-1]
            sign = -1
        else:
            out[y] = idx
            sign = 1

        for off, weight in kernel.below:
            _shift_add(pend1, err * weight, sign * off)
        for off, weight in kernel.below2:
            _shift_add(pend2, err * weight, sign * off)

    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def prepare_photo(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Crop, scale and tone-correct a photo so it is worth dithering.

    Centre-crops to the target aspect ratio (never squashes - a stretched
    animal is more obviously wrong than a cropped one), resamples with
    LANCZOS, stretches the tonal range and finishes with a light unsharp mask.

    The autocontrast step is not optional polish. Wildlife-camera frames are
    routinely dark and flat, and the measured palette spans only L* 11..72 -
    feeding a flat night frame straight into the quantiser turns it into one
    black rectangle, because every input tone lands nearer to the black ink
    than to anything else. Stretching first is what keeps the animal visible.

    Sharpening comes last: an unsharp mask overshoots, and running
    autocontrast afterwards would let those overshoots define the black and
    white points.
    """
    w, h = size
    if w <= 0 or h <= 0:
        raise ValueError(f"target size must be positive, got {size}")

    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        # Flatten onto white rather than onto black: the panel's paper is the
        # closest thing it has to "nothing here".
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[3])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        raise ValueError(f"source image is empty: {img.size}")

    # Centre crop to the target aspect ratio.
    if sw * h > sh * w:  # source is wider than the target
        cw = max(1, round(sh * w / h))
        left = (sw - cw) // 2
        box = (left, 0, left + cw, sh)
    else:
        ch = max(1, round(sw * h / w))
        top = (sh - ch) // 2
        box = (0, top, sw, top + ch)
    img = img.resize(size, Image.Resampling.LANCZOS, box=box)

    # preserve_tone drives all three channels from the luminance histogram.
    # Per-channel autocontrast would double as a white balance and is happy to
    # turn an infrared night frame magenta.
    img = ImageOps.autocontrast(img, cutoff=2, preserve_tone=True)
    return img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=2))


# --------------------------------------------------------------------------
# Neutral sources
# --------------------------------------------------------------------------
#
# The wildlife camera switches to infrared after dark, and an infrared frame
# has no colour at all: R == G == B in every pixel. Sent through the six-ink
# quantiser it still comes out speckled - measured over the camera's own
# archive, real captures spend 14 % of their pixels on green and red on
# average and over a quarter at worst - because black and white are the only
# inks without a hue, and every midtone in between gets assembled out of
# coloured ones.
# That is the "mud" the animal disappears into.
#
# Restricting such a frame to the two neutral inks costs nothing that was
# carrying information and buys a clean black-and-white raster.

#: The inks that carry no hue. Nothing else in this palette is close: measured
#: black and white are the only entries whose own channel spread stays under
#: 20/255.
_NEUTRAL_INKS: tuple[int, ...] = (
    palette.NAMES.index("black"),
    palette.NAMES.index("white"),
)

#: A source counts as neutral below these. Measured on real material: an
#: infrared capture comes in at exactly 0.000, a daylight frame from the same
#: camera at 23.6, and the nearest non-neutral case - a field of measured
#: white - at 14.0. These are a fence in open country, not a judgement call.
_NEUTRAL_MEAN_SPREAD = 2.0 / 255.0
_NEUTRAL_P99_SPREAD = 8.0 / 255.0


def _is_neutral(srgb: np.ndarray) -> bool:
    """True when an sRGB [0, 1] image carries no hue worth spending ink on.

    Both bounds are needed. The mean alone would pass a grey frame with a
    small bright colour subject in it; the percentile alone would pass an
    evenly, faintly tinted one.
    """
    if srgb.size == 0:
        return False
    spread = srgb.max(axis=-1) - srgb.min(axis=-1)
    return bool(
        spread.mean() < _NEUTRAL_MEAN_SPREAD
        and np.percentile(spread, 99) < _NEUTRAL_P99_SPREAD
    )


def dither_photo(
    img: Image.Image,
    size: tuple[int, int],
    method: str = "floyd",
) -> Image.Image:
    """Turn a photo into an RGB image made only of DEVICE_RGB pixels.

    The full pipeline: prepare_photo() then error diffusion. Callers hand in
    the raw file and get back something the layout can paste directly.

    `method` is "floyd" (default, general photography) or "atkinson"
    (portraits, faces, fur - higher contrast, less mud).

    A source with no colour in it - an infrared night capture - is diffused
    against black and white alone. See "Neutral sources" above.
    """
    kernel = _kernel_for(method)
    prepared = prepare_photo(img, size)

    srgb = np.asarray(prepared, dtype=np.float64) / 255.0
    inks = _NEUTRAL_INKS if _is_neutral(srgb) else None
    indices = _diffuse(_srgb_to_linear(srgb), kernel, inks)

    rgb = palette.DEVICE_LUT[indices]
    # Cheap next to the diffusion pass, and the one guard that catches a
    # palette or lookup-table mistake here instead of 400 km away.
    palette.assert_palette_exact(rgb)
    return Image.fromarray(rgb, mode="RGB")
