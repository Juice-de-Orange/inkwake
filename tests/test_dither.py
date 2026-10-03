"""Tests for the photo dither pass.

No network and no filesystem: the one piece of real-world input is a wildlife
camera frame embedded below as base64, because the failure this module exists
to prevent - a flat night frame collapsing into one black rectangle - only
shows up on genuinely dark, genuinely low-contrast sensor data. A synthetic
gradient never reproduces it.
"""

from __future__ import annotations

import base64
import io
import math
import time

import numpy as np
import pytest
from PIL import Image

from app.render import dither, palette

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

#: 120x110 crop from a real infrared trail-camera night capture (foliage, no
#: people, no place you could recognise). Dark and flat on purpose: L* p2..p98 spans
#: only 0..116 out of 255. Straight quantisation turns this into 65 % black.
NIGHT_FRAME_JPEG_B64 = """
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
kD//2Q==
"""


@pytest.fixture(scope="module")
def night_frame() -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(NIGHT_FRAME_JPEG_B64)))


@pytest.fixture(scope="module")
def synthetic_photo() -> Image.Image:
    """A deterministic stand-in for a photograph: smooth, coloured, noisy."""
    h, w = 96, 128
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    rng = np.random.default_rng(11)
    r = 128 + 100 * np.sin(xx / 19.0)
    g = 128 + 100 * np.cos(yy / 13.0)
    b = 40 + 180 * (xx + yy) / (w + h)
    arr = np.stack((r, g, b), axis=-1) + rng.normal(0, 6, (h, w, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def _ink_shares(img: Image.Image) -> dict[str, float]:
    """Fraction of the frame each ink occupies, keyed by palette name."""
    idx = palette.rgb_to_indices(np.asarray(img))
    counts = np.bincount(idx.ravel(), minlength=len(palette.NAMES))
    return {n: float(c) / idx.size for n, c in zip(palette.NAMES, counts)}


# --------------------------------------------------------------------------
# The palette contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", dither.METHODS)
def test_output_holds_only_device_colours(synthetic_photo, method):
    out = dither.dither_photo(synthetic_photo, (80, 60), method)
    palette.assert_palette_exact(np.asarray(out))
    seen = {tuple(int(v) for v in c) for c in np.unique(np.asarray(out).reshape(-1, 3), axis=0)}
    assert seen <= set(palette.DEVICE_RGB)


@pytest.mark.parametrize("method", dither.METHODS)
def test_output_geometry_and_mode(synthetic_photo, method):
    out = dither.dither_photo(synthetic_photo, (61, 37), method)
    assert out.size == (61, 37)
    assert out.mode == "RGB"


@pytest.mark.parametrize("name,measured", list(zip(palette.NAMES, palette.MEASURED_RGB)))
def test_measured_inks_quantise_to_themselves(name, measured):
    """A field of an ink's own measured colour must come out as that ink alone.

    This is the whole Lab-against-MEASURED / paint-with-DEVICE split in one
    assertion: matching in plain RGB collapses the measured green and blue
    into black and this test goes red.
    """
    out = dither.dither_photo(Image.new("RGB", (48, 48), measured), (48, 48))
    expected = palette.DEVICE_RGB[palette.NAMES.index(name)]
    seen = {tuple(int(v) for v in c) for c in np.unique(np.asarray(out).reshape(-1, 3), axis=0)}
    assert seen == {expected}


@pytest.mark.parametrize("method", dither.METHODS)
def test_solid_red_stays_solid(method):
    """Red is inside the panel's reach, so it must not acquire a pattern."""
    out = dither.dither_photo(Image.new("RGB", (64, 64), palette.RED), (64, 64), method)
    assert _ink_shares(out)["red"] == 1.0


# --------------------------------------------------------------------------
# Tonal behaviour
# --------------------------------------------------------------------------


def _grey_ramp(w: int = 256, h: int = 64) -> Image.Image:
    ramp = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))
    return Image.fromarray(np.dstack([ramp] * 3), "RGB")


@pytest.mark.parametrize("method", dither.METHODS)
def test_grey_ramp_is_dithered_not_flattened(method):
    """A grey wedge has to break into black and white, not pick one of them."""
    shares = _ink_shares(dither.dither_photo(_grey_ramp(), (256, 64), method))
    assert shares["black"] > 0.2
    assert shares["white"] > 0.2
    assert shares["black"] + shares["white"] > 0.75


@pytest.mark.parametrize("method", dither.METHODS)
def test_grey_ramp_keeps_its_tonal_order(method):
    """Dark end black, bright end white - error diffusion must not shuffle tone."""
    idx = palette.rgb_to_indices(np.asarray(dither.dither_photo(_grey_ramp(), (256, 64), method)))
    dark, bright = idx[:, :16], idx[:, -16:]
    assert (dark == palette.NAMES.index("black")).mean() > 0.9
    assert (bright == palette.NAMES.index("white")).mean() > 0.9


def test_atkinson_is_more_contrasty_than_floyd():
    """Atkinson drops a quarter of the residual, so midtones polarise further.

    Asked of _diffuse rather than dither_photo: a grey wedge is a neutral
    source, so the public path now sends it to black and white alone and both
    kernels would score exactly 1.00. The claim is about the kernel, so the
    kernel is what this measures - against all six inks, as before.
    """
    prepared = dither.prepare_photo(_grey_ramp(), (256, 64))
    linear = dither._srgb_to_linear(np.asarray(prepared, dtype=np.float64) / 255.0)

    def neutral_share(method: str) -> float:
        idx = dither._diffuse(linear, dither._kernel_for(method))
        return float(np.isin(idx, dither._NEUTRAL_INKS).mean())

    assert neutral_share("atkinson") > neutral_share("floyd")


# --------------------------------------------------------------------------
# The algorithm itself
# --------------------------------------------------------------------------

#: (dx in scan direction, dy, weight) straight out of the two definitions.
_REF_KERNELS = {
    "floyd": ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)),
    "atkinson": (
        (1, 0, 1 / 8),
        (2, 0, 1 / 8),
        (-1, 1, 1 / 8),
        (0, 1, 1 / 8),
        (1, 1, 1 / 8),
        (0, 2, 1 / 8),
    ),
}


def _reference_indices(img: Image.Image, method: str) -> np.ndarray:
    """Textbook serpentine error diffusion, written from the definition.

    Deliberately naive and deliberately not sharing code with dither._diffuse:
    it pins the weights, the scan order, the clamping and the fact that the
    accumulator lives in linear light. The ink decision is taken from the
    module's lookup table on purpose, so a disagreement here can only be a
    bookkeeping bug - the table itself is checked separately below.
    """
    lut = dither._ink_lut()
    n, nmax = dither._LUT_N, dither._LUT_MAX
    srgb = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0
    acc = dither._srgb_to_linear(srgb)
    h, w = acc.shape[:2]
    out = np.zeros((h, w), dtype=np.uint8)

    for y in range(h):
        step = 1 if y % 2 == 0 else -1
        order = range(w) if step == 1 else range(w - 1, -1, -1)
        for x in order:
            v = [min(1.0, max(0.0, acc[y, x, c])) for c in range(3)]
            key = 0
            for c in range(3):
                key = key * n + int(math.sqrt(v[c]) * nmax + 0.5)
            k = lut[key]
            out[y, x] = k
            err = [v[c] - dither._MEASURED_LINEAR[k][c] for c in range(3)]
            for dx, dy, weight in _REF_KERNELS[method]:
                nx, ny = x + dx * step, y + dy
                if 0 <= nx < w and ny < h:
                    for c in range(3):
                        acc[ny, nx, c] += err[c] * weight
    return out


@pytest.mark.parametrize("method", dither.METHODS)
def test_matches_reference_implementation(method):
    src = _grey_ramp(w=40, h=24).convert("RGB")
    arr = np.asarray(src, dtype=np.float64) / 255.0
    got = dither._diffuse(dither._srgb_to_linear(arr), dither._kernel_for(method))
    assert np.array_equal(got, _reference_indices(src, method))


@pytest.mark.parametrize("method", dither.METHODS)
def test_matches_reference_on_a_colour_photo(synthetic_photo, method):
    src = dither.prepare_photo(synthetic_photo, (48, 36))
    arr = np.asarray(src, dtype=np.float64) / 255.0
    got = dither._diffuse(dither._srgb_to_linear(arr), dither._kernel_for(method))
    assert np.array_equal(got, _reference_indices(src, method))


def test_lookup_table_agrees_with_exact_lab_matching():
    """The LUT is an approximation; pin how good it has to stay."""
    rng = np.random.default_rng(7)
    srgb = rng.integers(0, 256, size=(120_000, 3)).astype(np.float64)
    lab = palette.srgb_to_lab(srgb)

    diff = lab[:, None, :] - palette.MEASURED_LAB
    dist = np.sqrt(np.einsum("...ij,...ij->...i", diff, diff))
    exact = dist.argmin(axis=1)

    lin = dither._srgb_to_linear(srgb / 255.0)
    q = np.rint(np.sqrt(lin) * dither._LUT_MAX).astype(np.int64)
    flat = (q[:, 0] * dither._LUT_N + q[:, 1]) * dither._LUT_N + q[:, 2]
    approx = np.frombuffer(dither._ink_lut(), dtype=np.uint8)[flat]

    rows = np.arange(len(srgb))
    penalty = dist[rows, approx] - dist[rows, exact]
    assert (approx == exact).mean() > 0.99
    # Every disagreement must be a near-tie, well under what the eye resolves.
    assert penalty.max() < 4.0


def test_shift_add_handles_both_edges():
    dst = np.zeros((4, 3))
    dither._shift_add(dst, np.arange(12, dtype=np.float64).reshape(4, 3), 1)
    assert dst[0].tolist() == [0.0, 0.0, 0.0]
    assert dst[1].tolist() == [0.0, 1.0, 2.0]

    dst = np.zeros((4, 3))
    dither._shift_add(dst, np.arange(12, dtype=np.float64).reshape(4, 3), -1)
    assert dst[0].tolist() == [3.0, 4.0, 5.0]
    assert dst[3].tolist() == [0.0, 0.0, 0.0]

    # Narrower than the kernel reaches: the residual falls off, nothing raises.
    dst = np.zeros((1, 3))
    dither._shift_add(dst, np.ones((1, 3)), 1)
    dither._shift_add(dst, np.ones((1, 3)), -2)
    assert dst.tolist() == [[0.0, 0.0, 0.0]]


@pytest.mark.parametrize("size", [(1, 1), (1, 8), (8, 1), (2, 3), (3, 2)])
@pytest.mark.parametrize("method", dither.METHODS)
def test_degenerate_sizes_survive(synthetic_photo, size, method):
    out = dither.dither_photo(synthetic_photo, size, method)
    assert out.size == size
    palette.assert_palette_exact(np.asarray(out))


def test_unknown_method_is_rejected(synthetic_photo):
    with pytest.raises(ValueError, match="unknown dither method"):
        dither.dither_photo(synthetic_photo, (16, 16), "stucki")


def test_method_aliases_resolve():
    assert dither._kernel_for("Floyd-Steinberg") is dither._kernel_for("floyd")
    assert dither._kernel_for(" ATKINSON ") is dither._kernel_for("atkinson")


# --------------------------------------------------------------------------
# prepare_photo
# --------------------------------------------------------------------------


def test_prepare_centre_crops_instead_of_squashing():
    """A 3:1 source cropped to 1:1 must keep only the middle third."""
    src = Image.new("RGB", (300, 100), palette.BLACK)
    src.paste(Image.new("RGB", (100, 100), palette.RED), (100, 0))
    out = dither.prepare_photo(src, (100, 100))
    assert out.size == (100, 100)
    assert _ink_shares(dither.dither_photo(src, (100, 100)))["red"] == 1.0


def test_prepare_crops_tall_sources_vertically():
    src = Image.new("RGB", (100, 300), palette.BLACK)
    src.paste(Image.new("RGB", (100, 100), palette.RED), (0, 100))
    assert _ink_shares(dither.dither_photo(src, (100, 100)))["red"] == 1.0


def test_prepare_flattens_alpha_onto_white():
    """Transparent areas become paper, not ink - black would read as content."""
    src = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    out = dither.dither_photo(src, (32, 32))
    assert _ink_shares(out)["white"] == 1.0


@pytest.mark.parametrize("mode", ["L", "P", "CMYK"])
def test_non_rgb_sources_are_converted(synthetic_photo, mode):
    out = dither.dither_photo(synthetic_photo.convert(mode), (32, 24))
    palette.assert_palette_exact(np.asarray(out))


def test_prepare_rejects_a_degenerate_target(synthetic_photo):
    with pytest.raises(ValueError, match="positive"):
        dither.prepare_photo(synthetic_photo, (0, 10))


# --------------------------------------------------------------------------
# Real sensor data
# --------------------------------------------------------------------------


def test_prepare_lifts_a_flat_night_frame(night_frame):
    """The tonal stretch has to actually happen on a real dark capture."""
    before = np.asarray(night_frame.convert("L"), dtype=np.float64)
    after = np.asarray(dither.prepare_photo(night_frame, (120, 110)).convert("L"), dtype=np.float64)

    span_before = np.percentile(before, 98) - np.percentile(before, 2)
    span_after = np.percentile(after, 98) - np.percentile(after, 2)
    assert span_after > span_before + 40
    assert after.mean() > before.mean() + 20


@pytest.mark.parametrize("method", dither.METHODS)
def test_real_night_frame_is_not_a_black_field(night_frame, method):
    """The regression this module exists for.

    Without the tonal stretch this frame quantises to 65 % black and the
    animal disappears. Anything above ~half the frame in one ink means the
    picture stopped carrying information.
    """
    shares = _ink_shares(dither.dither_photo(night_frame, (120, 110), method))
    assert max(shares.values()) < 0.7
    assert shares["black"] < 0.5
    assert shares["white"] > 0.2


# --------------------------------------------------------------------------
# Neutral sources
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", dither.METHODS)
def test_an_infrared_frame_spends_no_coloured_ink(night_frame, method):
    """The mud this path exists to remove.

    A night capture is infrared: R == G == B everywhere, so every coloured
    pixel in the output is an artefact of assembling greys out of inks that
    have a hue. Against all six, this frame used to come out with a fifth of
    its pixels red and green - confetti on an animal that has none.
    """
    shares = _ink_shares(dither.dither_photo(night_frame, (120, 110), method))
    coloured = {name: v for name, v in shares.items() if name not in ("black", "white")}
    assert sum(coloured.values()) == 0.0, coloured
    assert shares["black"] > 0.0 and shares["white"] > 0.0


def test_a_colour_photo_still_reaches_for_colour(synthetic_photo):
    """The guard in the other direction: this must not swallow ordinary photos."""
    shares = _ink_shares(dither.dither_photo(synthetic_photo, (96, 72)))
    coloured = sum(v for name, v in shares.items() if name not in ("black", "white"))
    assert coloured > 0.1


def test_the_method_argument_still_reaches_the_kernel(synthetic_photo):
    """Guards the public path, which the neutral tests no longer can.

    They parametrise over METHODS but assert only ink shares that both kernels
    satisfy, and the contrast test above now asks _diffuse directly. Without
    this, dither_photo could stop threading `method` into _kernel_for and every
    other test would still pass. A coloured source, because a neutral one is
    routed to two inks where the kernels barely differ.
    """
    floyd = dither.dither_photo(synthetic_photo, (96, 72), "floyd")
    atkinson = dither.dither_photo(synthetic_photo, (96, 72), "atkinson")
    assert np.asarray(floyd).tobytes() != np.asarray(atkinson).tobytes()


@pytest.mark.parametrize("name,measured", list(zip(palette.NAMES, palette.MEASURED_RGB)))
def test_measured_inks_are_never_read_as_neutral(name, measured):
    """The nearest miss.

    Measured white spreads 14/255 across its channels and black 17/255 - the
    smallest gaps in the palette. If the threshold ever drifts up to meet
    them, a field of ink would quantise through the two-ink path and the
    colour ones would silently stop reproducing themselves.
    """
    srgb = np.full((8, 8, 3), measured, dtype=np.float64) / 255.0
    assert dither._is_neutral(srgb) is False


def test_both_bounds_are_load_bearing():
    """Mean and percentile each refuse what the other would wave through.

    Both cases are built to sit inside the *other* bound on purpose, so a
    single-bound implementation fails here rather than in front of a badger.
    """
    # Grey with a coloured subject in 2 % of it. Mean spread is 20*0.3/1024 =
    # 0.0059, comfortably under the 2/255 = 0.0078 mean bound - so only the
    # percentile can refuse this. The patch has to exceed 1 % of the frame or
    # the 99th percentile never sees it.
    speckled = np.full((32, 32, 3), 0.5)
    speckled.reshape(-1, 3)[:20] = (0.8, 0.5, 0.5)
    assert dither._is_neutral(speckled) is False

    # Evenly tinted by 5/255. Every pixel spreads 0.0196, which is under the
    # 8/255 = 0.0314 percentile bound - so only the mean can refuse it.
    tinted = np.full((32, 32, 3), 0.5)
    tinted[..., 0] += 5.0 / 255.0
    assert dither._is_neutral(tinted) is False

    # Actually neutral.
    assert dither._is_neutral(np.full((32, 32, 3), 0.5)) is True
    assert dither._is_neutral(np.zeros((0, 0, 3))) is False


def test_the_two_ink_table_is_cached_separately():
    """Both tables have to coexist; one must not evict the other."""
    dither._ink_lut()
    dither._ink_lut(dither._NEUTRAL_INKS)
    assert set(dither._LUT_CACHE) >= {None, dither._NEUTRAL_INKS}

    mono = np.frombuffer(dither._ink_lut(dither._NEUTRAL_INKS), dtype=np.uint8)
    assert set(np.unique(mono).tolist()) <= set(dither._NEUTRAL_INKS)
    # The six-ink table must still be the six-ink table.
    full = np.frombuffer(dither._ink_lut(), dtype=np.uint8)
    assert len(set(np.unique(full).tolist())) == len(palette.NAMES)


def test_real_night_frame_survives_the_full_panel_path(night_frame):
    """End to end: dither, map back to indices, pack to the 4 bpp wire format."""
    out = dither.dither_photo(night_frame, (400, 300))
    packed = palette.pack_nibbles(palette.rgb_to_indices(np.asarray(out)))
    assert len(packed) == 400 * 300 // 2


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_400x300_stays_under_two_seconds(night_frame, monkeypatch):
    """Includes building the lookup table, which is what a cold worker pays."""
    # monkeypatch rather than a bare assignment: the table is a process-wide
    # cache, and an empty one left behind by a test that died here would make
    # the *next* photo in the session pay the 0.4 s build inside its own
    # budget. night_frame is infrared, so what gets built here is the
    # two-ink table - the same cost as the six-ink one.
    monkeypatch.setattr(dither, "_LUT_CACHE", {})

    start = time.process_time()
    dither.dither_photo(night_frame, (400, 300), "floyd")
    cold = time.process_time() - start

    start = time.process_time()
    dither.dither_photo(night_frame, (400, 300), "atkinson")
    warm = time.process_time() - start

    print(f"\n400x300: cold (with LUT build) {cold:.3f} s, warm atkinson {warm:.3f} s")
    assert cold < 2.0
    assert warm < 2.0
