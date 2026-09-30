"""Tests for the wire encoders.

The fixtures are built the way the real renderer builds a frame: Pillow
drawing primitives in DEVICE_RGB only, no antialiasing anywhere, so the
result is palette-exact by construction. test_fixtures_are_palette_exact
guards that assumption - if a future Pillow starts antialiasing rectangles or
the default bitmap font, every other test here would fail with a confusing
PaletteError instead.
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.render import palette
from app.render.encode import encode_png, encode_raw, frame_etag

WIDTH, HEIGHT = 400, 600
RAW_BYTES = WIDTH * HEIGHT // 2


# --------------------------------------------------------------------------
# Fixtures: images that look like what the renderer actually produces
# --------------------------------------------------------------------------


FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A real TTF at a real size, so the measured PNG size means something.

    Falls back to the bundled bitmap font if the assets are missing. Note the
    fallback is load_default_imagefont(), not load_default(): since Pillow
    10.1 the latter hands back an antialiasing FreeType font, whose grey glyph
    edges are exactly what assert_palette_exact refuses.
    """
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    if (FONT_DIR / name).is_file():
        return ImageFont.truetype(str(FONT_DIR / name), size)
    loader = getattr(ImageFont, "load_default_imagefont", ImageFont.load_default)
    return loader()


def _text_dashboard() -> Image.Image:
    """A text-only frame: header, weather, bookings, events, footer.

    Deliberately dense - about thirty lines at 15-22 px plus rules and badges -
    so the measured PNG size is an upper bound for a realistic text dashboard
    rather than for a mostly empty page.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), palette.WHITE)
    draw = ImageDraw.Draw(img)
    # Bilevel glyph rendering. Pillow antialiases by default and there is no
    # grey ink on this panel; this single line is what keeps text exact.
    draw.fontmode = "1"
    head, body, small = _font(22, bold=True), _font(15), _font(13)

    draw.rectangle((0, 0, WIDTH - 1, 44), fill=palette.RED)
    draw.text((12, 10), "Montag, 25. August", fill=palette.WHITE, font=head)
    draw.text((320, 14), "07:00", fill=palette.WHITE, font=body)

    draw.text((12, 56), "Innen 21 C  heiter", fill=palette.BLACK, font=body)
    draw.text((12, 76), "Regen 10 %   Sonne 06:05 - 20:11", fill=palette.BLUE, font=small)
    draw.line((12, 98, WIDTH - 12, 98), fill=palette.BLACK, width=1)

    draw.text((12, 106), "Buchungen", fill=palette.GREEN, font=body)
    for row in range(3):
        top = 130 + row * 42
        draw.rectangle((12, top, WIDTH - 12, top + 36), outline=palette.BLACK)
        draw.text((18, top + 3), "26.08. - 29.08.   3 Naechte", fill=palette.BLACK, font=small)
        draw.text((18, top + 19), f"Familie Beispiel {row}, 4 Gaeste", fill=palette.BLACK, font=small)

    draw.text((12, 264), "Veranstaltungen", fill=palette.GREEN, font=body)
    for row in range(6):
        top = 288 + row * 22
        draw.text((12, top), "26.08.", fill=palette.RED, font=small)
        draw.text((72, top), "19:30", fill=palette.BLACK, font=small)
        draw.text((130, top), "Jazz im Hof, Kollegienkirche", fill=palette.BLACK, font=small)

    draw.line((12, 428, WIDTH - 12, 428), fill=palette.BLACK, width=1)
    draw.text((12, 436), "Wildkamera", fill=palette.GREEN, font=body)
    for row in range(6):
        draw.text(
            (12, 462 + row * 18),
            "Ein Reh aeugt am Waldrand, 05:42, Kamera Nord",
            fill=palette.BLACK,
            font=small,
        )

    draw.rectangle((0, HEIGHT - 28, WIDTH - 1, HEIGHT - 1), fill=palette.YELLOW)
    draw.text((12, HEIGHT - 22), "Akku 78 %   4021 mV   -68 dBm", fill=palette.BLACK, font=small)
    return img


def _dithered_photo(width: int, height: int, seed: int = 7) -> np.ndarray:
    """A stand-in for a wildlife still after dithering: structured plus noise.

    Flat gradients compress far too well to say anything useful about a real
    photo panel, so noise is added before the ink decision. The result is the
    pessimistic end of what a photo region costs in the PNG.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = np.stack(
        [
            (xx * 255 // max(width - 1, 1)),
            (yy * 255 // max(height - 1, 1)),
            ((xx + yy) * 255 // max(width + height - 2, 1)),
        ],
        axis=-1,
    ).astype(np.int32)
    base += rng.integers(-40, 40, size=base.shape)
    noisy = np.clip(base, 0, 255).astype(np.uint8)

    indices = palette.nearest_ink(palette.srgb_to_lab(noisy))
    return palette.DEVICE_LUT[indices]


def _photo_dashboard() -> Image.Image:
    """The text dashboard with a dithered photo strip pasted over the bottom."""
    img = _text_dashboard()
    strip = Image.fromarray(_dithered_photo(WIDTH, 220))
    img.paste(strip, (0, 360))
    return img


def _swatch(*inks: tuple[int, int, int]) -> np.ndarray:
    """One row of the given inks, as a (1, N, 3) uint8 array."""
    return np.array([list(inks)], dtype=np.uint8)


# --------------------------------------------------------------------------
# Fixture sanity
# --------------------------------------------------------------------------


def test_fixtures_are_palette_exact() -> None:
    palette.assert_palette_exact(np.asarray(_text_dashboard()))
    palette.assert_palette_exact(np.asarray(_photo_dashboard()))


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


def test_png_roundtrip_is_lossless() -> None:
    img = _photo_dashboard()
    reloaded = Image.open(io.BytesIO(encode_png(img)))
    assert np.array_equal(np.asarray(reloaded.convert("RGB")), np.asarray(img))


def test_png_is_four_bit_indexed_with_six_colours() -> None:
    data = encode_png(_text_dashboard())
    width, height, bit_depth, colour_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (WIDTH, HEIGHT)
    assert bit_depth == 4  # the panel's own depth; Pillow derives it from PLTE
    assert colour_type == 3  # indexed

    reloaded = Image.open(io.BytesIO(data))
    assert reloaded.mode == "P"
    assert bytes(reloaded.getpalette()) == bytes(
        c for ink in palette.DEVICE_RGB for c in ink
    )


def test_png_carries_no_metadata_and_is_deterministic() -> None:
    """No timestamps, no ancillary chunks - the same frame must encode identically."""
    data = encode_png(_text_dashboard())
    chunks = []
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunks.append(data[pos + 4 : pos + 8].decode("ascii"))
        pos += 12 + length
    assert chunks == ["IHDR", "PLTE", "IDAT", "IEND"]
    assert encode_png(_text_dashboard()) == data


def _decode_png_indices(data: bytes) -> np.ndarray:
    """Inflate and unfilter a PNG by hand, returning the raw palette indices.

    Pillow reading back its own output proves very little - a shared bug would
    cancel out. The board decodes with a completely different library, so this
    walks the format the way that library will: chunk split, zlib, per-scanline
    filter reconstruction, then nibble unpacking.
    """
    (width, height, bit_depth, colour_type) = struct.unpack(">IIBB", data[16:26])
    assert (bit_depth, colour_type) == (4, 3)

    idat = b""
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length

    stream = zlib.decompress(idat)
    stride = width // 2  # 4 bpp, two pixels per byte, no padding at even widths
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        ftype = stream[offset]
        row = bytearray(stream[offset + 1 : offset + 1 + stride])
        for x in range(stride):
            # Sub-byte depths filter on whole bytes, so the left neighbour is
            # one byte back regardless of how many pixels that byte holds.
            a = row[x - 1] if x else 0
            b = prev[x]
            c = prev[x - 1] if x else 0
            if ftype == 1:
                row[x] = (row[x] + a) & 0xFF
            elif ftype == 2:
                row[x] = (row[x] + b) & 0xFF
            elif ftype == 3:
                row[x] = (row[x] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[x] = (row[x] + pred) & 0xFF
            elif ftype != 0:
                raise AssertionError(f"unknown PNG filter type {ftype}")
        rows.append(row)
        prev = row

    packed = np.frombuffer(b"".join(bytes(r) for r in rows), dtype=np.uint8)
    packed = packed.reshape(height, stride)
    out = np.empty((height, width), dtype=np.uint8)
    out[:, 0::2] = packed >> 4  # PNG packs MSB-first, same as the panel
    out[:, 1::2] = packed & 0x0F
    return out


def test_png_decodes_without_pillow_to_the_same_indices() -> None:
    img = _photo_dashboard()
    expected = palette.rgb_to_indices(np.asarray(img))
    assert np.array_equal(_decode_png_indices(encode_png(img)), expected)


def test_png_beats_raw_by_a_wide_margin(capsys: pytest.CaptureFixture[str]) -> None:
    """Measured sizes. Radio-on time is the dominant energy cost (9.4)."""
    text_png = len(encode_png(_text_dashboard()))
    photo_png = len(encode_png(_photo_dashboard()))
    with capsys.disabled():
        print(
            f"\n  PNG size, text dashboard : {text_png:>7,} B"
            f"\n  PNG size, with photo strip: {photo_png:>7,} B"
            f"\n  raw packed 4 bpp          : {RAW_BYTES:>7,} B"
        )
    assert text_png < 30_000
    # A dithered photo is the worst case; PNG must still beat the raw frame,
    # otherwise the negotiation should prefer raw for photo-heavy layouts.
    assert photo_png < RAW_BYTES


def test_png_accepts_pil_and_numpy_alike() -> None:
    img = _text_dashboard()
    assert encode_png(img) == encode_png(np.asarray(img))


# --------------------------------------------------------------------------
# Raw
# --------------------------------------------------------------------------


def test_raw_is_exactly_120000_bytes_at_native_size() -> None:
    assert len(encode_raw(_text_dashboard())) == 120_000


def test_raw_uses_native_nibble_codes_high_nibble_left() -> None:
    """Blue is 0x5, not 0x4 - the gap in the code table is the classic bug (4.2)."""
    row = _swatch(palette.BLACK, palette.WHITE, palette.YELLOW, palette.RED,
                  palette.BLUE, palette.GREEN)
    assert encode_raw(row) == bytes([0x01, 0x23, 0x56])


def test_raw_is_row_major() -> None:
    img = np.array(
        [
            [palette.RED, palette.WHITE],
            [palette.GREEN, palette.BLACK],
        ],
        dtype=np.uint8,
    )
    assert encode_raw(img) == bytes([0x31, 0x60])


def test_raw_rejects_odd_width() -> None:
    with pytest.raises(ValueError):
        encode_raw(_swatch(palette.BLACK, palette.WHITE, palette.RED))


# --------------------------------------------------------------------------
# Palette guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("encoder", [encode_png, encode_raw])
@pytest.mark.parametrize("stray", [(128, 128, 128), (254, 255, 255), (255, 243, 57)])
def test_foreign_colour_is_rejected_not_rounded(encoder, stray) -> None:
    """Near-misses matter most: #FEFFFF is invisible in review, not on the panel."""
    arr = np.asarray(_text_dashboard()).copy()
    arr[300, 200] = stray
    with pytest.raises(palette.PaletteError):
        encoder(arr)


def test_float_array_is_rejected() -> None:
    """[0, 1] floats would truncate to an all-black frame that passes every guard."""
    arr = np.asarray(_text_dashboard()).astype(np.float32) / 255.0
    with pytest.raises(TypeError):
        encode_raw(arr)


@pytest.mark.parametrize("bad", [np.zeros((4, 4), dtype=np.uint8), "not an image", None])
def test_non_image_input_is_rejected(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_raw(bad)


def test_empty_frame_is_rejected() -> None:
    with pytest.raises(ValueError):
        encode_raw(np.zeros((0, 4, 3), dtype=np.uint8))


# --------------------------------------------------------------------------
# ETag
# --------------------------------------------------------------------------


def test_etag_is_a_quoted_sha256() -> None:
    tag = frame_etag(encode_raw(_text_dashboard()))
    assert tag.startswith('"') and tag.endswith('"')
    body = tag[1:-1]
    assert len(body) == 64 and all(c in "0123456789abcdef" for c in body)


def test_identical_render_yields_identical_etag() -> None:
    """The whole point: a re-render with the same result must answer 304."""
    assert frame_etag(encode_raw(_text_dashboard())) == frame_etag(
        encode_raw(_text_dashboard())
    )


def test_one_changed_pixel_changes_the_etag() -> None:
    before = np.asarray(_text_dashboard()).copy()
    after = before.copy()
    after[300, 200] = palette.RED if tuple(before[300, 200]) != palette.RED else palette.BLACK
    assert frame_etag(encode_raw(before)) != frame_etag(encode_raw(after))


def test_etag_ignores_how_the_frame_was_handed_over() -> None:
    """Same pixels via PIL, via numpy and via a palette image - one ETag."""
    img = _text_dashboard()
    as_array = np.asarray(img)

    indices = palette.rgb_to_indices(as_array)
    as_palette = Image.frombytes("P", (WIDTH, HEIGHT), indices.tobytes())
    as_palette.putpalette([c for ink in palette.DEVICE_RGB for c in ink])

    tags = {
        frame_etag(encode_raw(img)),
        frame_etag(encode_raw(as_array)),
        frame_etag(encode_raw(as_palette)),
    }
    assert len(tags) == 1


def test_etag_differs_from_a_hash_of_the_png() -> None:
    """Documents the contract: hash the packed pixels, never the container."""
    img = _text_dashboard()
    assert frame_etag(encode_raw(img)) != frame_etag(encode_png(img))
