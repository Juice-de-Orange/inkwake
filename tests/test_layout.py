"""Layout tests.

The interesting failures on this panel are all silent: an antialiased glyph
edge, a line of text that runs under the footer, a booking name that reaches
past the margin. None of them raise, and none of them are visible from 400 km
away, so every test here checks pixels rather than return values.

The sample content is made up but shaped like production data: short booking
names, a 120-character German caption as a captioning model writes it, and
event titles as long as real calendar feeds deliver them -- because that is
what actually has to fit.

Rendered frames land in tests/out/ so a human can look at them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.models import (
    Booking,
    Dashboard,
    Event,
    Sighting,
    SunTimes,
    Telemetry,
    Weather,
)
from app.render import layout, palette
from app.render.layout import MARGIN, render_dashboard

OUT_DIR = Path(__file__).parent / "out"
#: With a bookings source configured, as on a board that shows stays. Without
#: one the bookings section is left out entirely (see its own test).
SETTINGS = Settings(sources_dsn="postgresql://reader@db.example.invalid/example")

NOW = dt.datetime(2026, 8, 25, 15, 0)
NEXT = dt.datetime(2026, 8, 25, 20, 0)

# -- sample rows, shaped like production data ------------------------------
SAMPLE_BOOKINGS = [
    Booking(dt.date(2026, 10, 7), dt.date(2026, 10, 8), 1, "Alex", 1),
    Booking(dt.date(2026, 10, 22), dt.date(2026, 10, 23), 1, "Alex", 1),
    Booking(dt.date(2026, 11, 10), dt.date(2026, 11, 11), 1, "Alex Beispiel", 2),
]
SAMPLE_CAPTION = (
    "Ein Wiedersehensfreude: Rusty ist wieder am Waldrand zu sehen! "
    "Bereits heute zum 6. Mal begegnet uns dieser treue Fuchs."
)
SAMPLE_SIGHTING = Sighting(
    captured_at=dt.datetime(2026, 8, 25, 10, 31, 14),
    caption=SAMPLE_CAPTION,
    species="Fuchs",
    individual="Rusty",
    camera="Wiesenkamera",
)

#: The same six days with titles as long as real event feeds publish them.
#: Every one of these wraps to two lines at 376 px, and that is the whole point:
#: the short fixture above wraps to one, so for months it was the only shape the
#: fitter was ever measured against. On 16.09.2026 the real board showed a
#: caption and no animal, and nothing here went red.
LONG_EVENTS = [
    Event(dt.date(2026, 8, 25), "10:30",
          "Sonderausstellung: Die Sammlung Hartmann im Stadtmuseum", "Stadtmuseum"),
    Event(dt.date(2026, 8, 26), "11:30",
          "Jazz im Theater: Quartett von Johanna Lindberger", "Stadttheater"),
    Event(dt.date(2026, 8, 27), "12:30",
          "Führung durch die Ausgrabungen am Rathausplatz", "Rathausplatz"),
    Event(dt.date(2026, 8, 28), "13:30",
          "Herbstfest der Freiwilligen Feuerwehr Nordviertel", "Nordviertel"),
    Event(dt.date(2026, 8, 29), "14:30",
          "Lesung: Paula Brenner liest aus ihrem neuen Roman", "Literaturhaus"),
    Event(dt.date(2026, 8, 30), "15:30",
          "Bauernherbst im Umland – Almabtrieb in Oberwiesen", "Oberwiesen"),
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _telemetry(**kw) -> Telemetry:
    base = dict(
        device_id="m5-01",
        battery_percent=76,
        is_charging=False,
        temperature_c=24.7,
        humidity_pct=51.0,
        rssi_dbm=-67,
    )
    base.update(kw)
    return Telemetry(**base)


def _dashboard(**kw) -> Dashboard:
    base = dict(
        now=NOW,
        next_refresh=NEXT,
        telemetry=_telemetry(),
        weather=Weather(26.3, 2, "Teilweise bewölkt", 15, 14.0, 28.0),
        sun=SunTimes(dt.time(6, 5), dt.time(20, 5)),
        bookings=list(SAMPLE_BOOKINGS),
        events=[
            Event(dt.date(2026, 8, 25), "19:30", "Sommerszene: Tanzabend", "Kulturhalle"),
            Event(dt.date(2026, 8, 26), "20:00", "Theater im Hof", "Schlosshof"),
            Event(dt.date(2026, 8, 27), None, "Bauernmarkt", "Marktplatz"),
            Event(dt.date(2026, 8, 28), "18:00", "Lesung im Literaturhaus", "Literaturhaus"),
            Event(dt.date(2026, 8, 29), "10:00", "Flohmarkt", "Schlossgarten"),
            Event(dt.date(2026, 8, 30), "21:00", "Sternenführung", "Naturkundemuseum"),
        ],
        sighting=None,
        failures=[],
    )
    base.update(kw)
    return Dashboard(**base)


def _save(img: Image.Image, name: str) -> Image.Image:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    img.save(OUT_DIR / f"layout_{name}.png")
    return img


def _arr(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.uint8)


def _is_ink(img: Image.Image) -> np.ndarray:
    """True where a pixel is anything other than paper white."""
    return (_arr(img) != np.array(palette.WHITE, dtype=np.uint8)).any(axis=-1)


def _colours(img: Image.Image, box: tuple[int, int, int, int]) -> set[tuple[int, int, int]]:
    region = _arr(img.crop(box)).reshape(-1, 3)
    return {tuple(int(c) for c in row) for row in np.unique(region, axis=0)}


def _assert_frame_sane(img: Image.Image, name: str) -> None:
    """The four invariants every frame must hold, whatever the content."""
    _save(img, name)
    assert img.size == (SETTINGS.panel_width, SETTINGS.panel_height)
    assert img.mode == "RGB"
    palette.assert_palette_exact(_arr(img))

    ink = _is_ink(img)
    w, h = img.size
    # One pixel of slack on each side: a hinted glyph can put ink one pixel
    # left of the pen position (DejaVu's "J" and "w" both do), which is
    # typography, not overflow. Anything that really ran away lands near x=0.
    assert not ink[:, : MARGIN - 1].any(), "ink in the left margin"
    assert not ink[:, w - MARGIN + 1 :].any(), "ink in the right margin"
    assert not ink[:8, :].any(), "ink above the header's top margin"
    # The band between the last body row and the footer must stay clear, or a
    # long caption has crept into the footer line.
    gap_top = h - layout.FOOTER_H - 5
    assert not ink[gap_top : h - layout.FOOTER_H, :].any(), "content overruns into the footer"
    # The footer itself must be there, and must not touch the bottom edge.
    assert ink[h - layout.FOOTER_H :, :].any(), "footer line is missing"
    assert not ink[h - 1 :, :].any(), "ink on the last row"


def _photo(path: Path, size: tuple[int, int] = (640, 480)) -> Path:
    """A deterministic, photo-like source image (smooth gradients, one blob)."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    r = (xx * 255 // w).astype(np.uint8)
    g = (yy * 255 // h).astype(np.uint8)
    b = ((xx + yy) * 255 // (w + h)).astype(np.uint8)
    arr = np.dstack([r, g, b])
    blob = ((xx - w // 2) ** 2 + (yy - h // 2) ** 2) < (min(w, h) // 4) ** 2
    arr[blob] = (30, 40, 30)
    Image.fromarray(arr).save(path, quality=90)
    return path


# --------------------------------------------------------------------------
# Reading the frame back
# --------------------------------------------------------------------------
#
# The space a section got is not a return value anywhere - render_dashboard
# hands back pixels and nothing else - so the tests below count what a reader
# counts: coloured rules, blue kicker lines, the rectangle the photo covers.
# That also means a refactor cannot make them pass by accident.


def _rule_rows(img: Image.Image, colour: tuple[int, int, int]) -> list[int]:
    """Rows holding a section rule: the only near-full-width run of one ink."""
    match = (_arr(img) == np.array(colour, dtype=np.uint8)).all(axis=-1)
    return np.where(match.sum(axis=1) > img.width // 2)[0].tolist()


def _row_bands(rows: np.ndarray) -> list[tuple[int, int]]:
    """Group a boolean per-row mask into runs of consecutive rows."""
    out: list[tuple[int, int]] = []
    for r in np.where(rows)[0]:
        if out and r == out[-1][1] + 1:
            out[-1] = (out[-1][0], int(r))
        else:
            out.append((int(r), int(r)))
    return out


def _event_bands(img: Image.Image) -> list[tuple[int, int]]:
    """Row range of every event kicker painted on the frame.

    One blue line per event, searched between the events rule and the
    sighting heading, so neither the heading itself nor a blue pixel in the
    dithered photo below can be counted as an entry. Counting ink rather than
    return values is the point: the fitter can believe it placed five entries
    and still draw two.
    """
    rules = _rule_rows(img, palette.BLUE)
    if not rules:
        return []
    blue = (_arr(img) == np.array(palette.BLUE, dtype=np.uint8)).all(axis=-1)
    green = (_arr(img) == np.array(palette.GREEN, dtype=np.uint8)).all(axis=-1)
    green_rows = np.where(green.any(axis=1))[0]
    top = rules[-1] + 1
    bottom = int(green_rows.min()) if green_rows.size else img.height
    mask = np.zeros(img.height, dtype=bool)
    mask[top:bottom] = blue[top:bottom].any(axis=1)
    return _row_bands(mask)


def _visible_events(img: Image.Image) -> int:
    return len(_event_bands(img))


def _tile_rows(img: Image.Image) -> list[int]:
    """Rows the sighting photo covers, probed at the left margin.

    Only the tile is painted there: when a photo is present the caption starts
    a tile width plus a gap further right, and every row of a dithered photo
    carries ink somewhere in its first twenty columns.
    """
    rules = _rule_rows(img, palette.GREEN)
    if not rules:
        return []
    ink = _is_ink(img)
    return [y for y in range(rules[-1] + 1, img.height - layout.FOOTER_H - 5)
            if ink[y, MARGIN : MARGIN + 20].any()]


def _assert_tile_at_least(img: Image.Image, size: tuple[int, int]) -> None:
    """The pasted photo covers at least `size` pixels of paper.

    Width is measured as "every column is touched somewhere down the tile",
    not as ink density: error diffusion leaves bare paper wherever the capture
    is bright, so a corner of sky would otherwise read as a smaller tile.
    """
    want_w, want_h = size
    rows = _tile_rows(img)
    assert rows, "no photo under the Sichtung heading"
    height = rows[-1] - rows[0] + 1
    assert height >= want_h, f"photo is only {height} px tall, wanted {want_h}"
    span = _is_ink(img)[rows[0] : rows[-1] + 1]
    covered = span[:, MARGIN : MARGIN + want_w].any(axis=0)
    assert covered.all(), f"photo is only {int(covered.argmin())} px wide, wanted {want_w}"


# --------------------------------------------------------------------------
# Text utilities
# --------------------------------------------------------------------------


def test_wrap_text_respects_width_and_line_limit():
    font = layout.load_font(False, 16)
    text = "Sommerszene Tanzabend in der Kulturhalle mit anschliessender Diskussion"
    lines = layout.wrap_text(text, font, 200)
    assert len(lines) > 1
    assert all(layout.text_width(line, font) <= 200 for line in lines)
    assert " ".join(lines) == text

    capped = layout.wrap_text(text, font, 200, max_lines=2)
    assert len(capped) == 2
    assert capped[-1].endswith("…")
    assert all(layout.text_width(line, font) <= 200 for line in capped)


def test_wrap_text_hard_breaks_a_word_that_cannot_fit():
    font = layout.load_font(False, 16)
    word = "Sonderausstellungseroeffnungspodiumsdiskussionsveranstaltung"
    lines = layout.wrap_text(word, font, 120)
    assert len(lines) > 1
    assert all(layout.text_width(line, font) <= 120 for line in lines)
    assert "".join(lines) == word


def test_ellipsize_never_exceeds_the_budget():
    font = layout.load_font(False, 16)
    assert layout.ellipsize("kurz", font, 200) == "kurz"
    cut = layout.ellipsize("Grosses Konzerthaus am Theaterplatz", font, 90)
    assert cut.endswith("…")
    assert layout.text_width(cut, font) <= 90
    assert layout.ellipsize("egal", font, 0) == ""


def test_german_date_formatting():
    assert layout.format_long_date(dt.date(2026, 8, 25)) == "Di 25. August"
    # Same month: the month is named once, on the departure date.
    assert layout.format_stay(dt.date(2026, 10, 7), dt.date(2026, 10, 8)) == (
        "Mi 07. – Do 08. Okt."
    )
    # Across the month boundary both ends carry their month.
    assert layout.format_stay(dt.date(2026, 9, 30), dt.date(2026, 10, 2)) == (
        "Mi 30. Sep. – Fr 02. Okt."
    )
    assert layout._guests(1) == "1 Gast"
    assert layout._guests(4) == "4 Gäste"


def test_failure_hint_uses_german_source_names():
    assert layout._failure_hint([]) == ""
    assert layout._failure_hint(["weather"]) == "ohne Wetter"
    assert layout._failure_hint(["weather", "events"]) == "ohne Wetter, Termine"
    # An unknown source name still reaches the reader rather than vanishing.
    assert layout._failure_hint(["bus"]) == "ohne bus"


# --------------------------------------------------------------------------
# The no-antialiasing rule
# --------------------------------------------------------------------------


def test_painter_disables_font_antialiasing():
    p = layout._Painter(40, 40)
    assert p.draw.fontmode == "1", "fontmode must be '1'; 'L' renders grey glyph edges"


def test_painter_refuses_an_unreadable_ink_pair():
    """The readability guard sits at the one place every string passes through."""
    p = layout._Painter(60, 40)
    with pytest.raises(palette.PaletteError):
        p.text((0, 0), "unsichtbar", layout.load_font(False, 16), palette.YELLOW)
    # The same words in a safe pair are fine.
    p.text((0, 0), "lesbar", layout.load_font(False, 16), palette.BLACK)


def test_text_regions_contain_only_two_pure_inks():
    """The strongest available proof that no glyph edge was smoothed.

    assert_palette_exact would already fail on a grey edge, but this pins the
    intent: a black headline on white paper must be exactly two colours, not
    two colours plus whatever the nearest ink to grey happens to be.
    """
    img = render_dashboard(_dashboard(), SETTINGS)
    _save(img, "aa_probe")
    title = _colours(img, (MARGIN, 8, 260, 8 + 30))
    assert title == {palette.BLACK, palette.WHITE}, title
    # The bookings heading is red on white for the same reason.
    heading = _colours(img, (MARGIN, 100, 260, 126))
    assert heading <= {palette.RED, palette.WHITE}, heading


# --------------------------------------------------------------------------
# Full frames
# --------------------------------------------------------------------------


def test_typical_frame(tmp_path):
    sighting = Sighting(
        captured_at=SAMPLE_SIGHTING.captured_at,
        caption=SAMPLE_CAPTION,
        species="Fuchs",
        individual="Rusty",
        camera="Wiesenkamera",
        image_path=str(_photo(tmp_path / "cam.jpg")),
    )
    img = render_dashboard(_dashboard(sighting=sighting), SETTINGS)
    _assert_frame_sane(img, "typical")

    # The photo must actually be there: the tile sits at the left margin under
    # the Sichtung heading and cannot be all-white paper.
    assert _is_ink(img)[440:570, MARGIN : MARGIN + 130].any()


# --------------------------------------------------------------------------
# What the body is worth: entries, photo, and the paper between them
# --------------------------------------------------------------------------
#
# The measuring stick for the space split. The frame that prompted these showed
# three stays, two events and "5 weitere Termine", with a 40 px band of blank
# paper above the sighting and a photo too small to make out an animal in. All
# four of those render happily and raise nothing, so each number below is one a
# human read off a real frame.


def _typical_with_photo(tmp_path, **kw) -> Image.Image:
    """A normal day: three stays, six dates, one capture with a picture."""
    sighting = Sighting(
        captured_at=SAMPLE_SIGHTING.captured_at,
        caption=SAMPLE_CAPTION,
        species="Fuchs",
        individual="Rusty",
        camera="Wiesenkamera",
        image_path=str(_photo(tmp_path / "cam.jpg")),
    )
    return render_dashboard(_dashboard(sighting=sighting, **kw), SETTINGS)


def test_three_bookings_and_a_photo_still_leave_five_events(tmp_path):
    """The one number this layout owes the reader.

    It is pinned here rather than left to the fitter's arithmetic because every
    constant in the file - a heading's padding, a booking's second line, ten
    pixels of tile - is paid for out of this section and out of nothing else.
    """
    img = _typical_with_photo(tmp_path)
    _assert_frame_sane(img, "five_events")
    assert _visible_events(img) >= 5, (
        f"only {_visible_events(img)} events survived next to three stays and "
        "a photo; five is the floor this layout is sized for"
    )


def test_the_photo_is_big_enough_to_show_an_animal(tmp_path):
    """130x100 was a blur at 180 PPI on six inks. 160x120 is the floor."""
    img = _typical_with_photo(tmp_path)
    _assert_tile_at_least(img, (160, 120))


def test_the_photo_never_costs_the_fifth_event(tmp_path):
    """Where the two compete the entry wins and the tile gives up a rung.

    Dropping the stays frees three rows of paper, and that surplus goes into
    the photo rather than into a sixth entry - but only because the fifth is
    already safe: with the stays back the tile takes the smaller size instead.
    """
    roomy = _typical_with_photo(tmp_path, bookings=[])
    tight = _typical_with_photo(tmp_path)
    _save(roomy, "photo_roomy")

    assert _visible_events(roomy) >= 5 and _visible_events(tight) >= 5
    _assert_tile_at_least(roomy, (180, 135))
    assert len(_tile_rows(tight)) < len(_tile_rows(roomy))


def test_the_long_fixture_really_wraps():
    """LONG_EVENTS is only worth something while every title takes two lines.

    The fixture was rewritten once already (to made-up titles); a rewrite that
    shortened them would leave the tests below green and measuring nothing.
    """
    for event in LONG_EVENTS:
        block = layout._prepare_event(event, NOW.date(), 376)
        rows = {line.dy for line in block.lines}
        assert len(rows) >= 3, f"{event.title!r} no longer wraps (kicker + two title lines)"


def test_long_event_titles_keep_the_photo_and_the_fifth_event(tmp_path):
    """The frame of 16.09.2026: caption, no cat, and every test still green.

    Nothing about that day was unusual - the titles were simply the ones
    the feed publishes, so every entry wrapped to two lines. With two-line
    entries no rung of the tile ladder reaches EVENT_FLOOR, the score's first
    term ties every rung at a loss, and the tile area is then never looked at
    at all: the picture is what goes, every single time.
    """
    img = _typical_with_photo(tmp_path, events=LONG_EVENTS)
    _assert_frame_sane(img, "long_titles")
    assert _visible_events(img) >= 5, (
        f"only {_visible_events(img)} of six long-titled events survived"
    )
    _assert_tile_at_least(img, (128, 96))


def test_a_title_is_cut_before_the_picture_is_dropped(tmp_path):
    """The order the fitter owes the reader, pinned against a flood of entries.

    A cut title still names its event; a dropped photo is the whole Sichtung
    section reduced to one sentence. So the line budget gives way first, and
    the picture only when even the smallest rung cannot be paid for.
    """
    img = _typical_with_photo(tmp_path, events=LONG_EVENTS * 3)
    _assert_frame_sane(img, "long_titles_many")
    # Height, not "are there rows": with no tile the caption starts at the left
    # margin itself, so _tile_rows() is happily non-empty for a frame that has
    # no picture on it at all. This assertion was green that way once already.
    _assert_tile_at_least(img, (128, 96))
    assert _visible_events(img) >= 5


def test_no_blank_band_between_the_events_and_the_sighting(tmp_path):
    """The sighting follows the last event instead of hugging the bottom edge.

    It used to be anchored to the footer, so whatever the fitter could not
    spend on an entry stayed on the frame as a hole.
    """
    img = _typical_with_photo(tmp_path)
    ink = _is_ink(img)
    green = (_arr(img) == np.array(palette.GREEN, dtype=np.uint8)).all(axis=-1)
    heading_top = int(np.where(green.any(axis=1))[0].min())
    last_event_row = int(np.where(ink[:heading_top].any(axis=1))[0].max())
    gap = heading_top - last_event_row - 1
    # The section gap plus the leading the two fonts do not use. Past that it
    # is paper nobody asked for.
    assert gap <= 16, f"{gap} px of blank paper above the Sichtung heading"


def test_everything_empty():
    img = render_dashboard(
        Dashboard(
            now=NOW,
            next_refresh=NEXT,
            telemetry=Telemetry(device_id="m5-01"),
            failures=["weather", "events"],
        ),
        SETTINGS,
    )
    _assert_frame_sane(img, "empty")
    # A frame with nothing in it still has to say something in both sections;
    # an empty section would read as a rendering failure.
    assert _is_ink(img)[100:230, :].any()


def test_extreme_content_never_overflows():
    long_title = (
        "Sonderausstellungseröffnung mit anschliessender Podiumsdiskussion zum "
        "Thema Nachhaltigkeit im alpinen Raum, danach Buffet und Live-Musik"
    )
    long_place = (
        "Grosses Konzerthaus am Johann-Sebastian-Bach-Platz 9, 1234 Beispielstadt, "
        "Eingang Hofgasse gegenüber der Stadtpfarrkirche"
    )
    events = [
        Event(dt.date(2026, 8, 25) + dt.timedelta(days=i % 7), "19:30",
              f"{long_title} (Teil {i})", long_place)
        for i in range(20)
    ]
    long_booking = Booking(
        dt.date(2026, 8, 24), dt.date(2026, 8, 27), 3,
        "Familie Mustermann-Beispielhofer aus Musterhausen", 12,
    )
    img = render_dashboard(
        _dashboard(
            bookings=[long_booking] * 3,
            events=events,
            sighting=Sighting(
                captured_at=SAMPLE_SIGHTING.captured_at,
                caption=SAMPLE_CAPTION * 3,
                species="Fuchs",
                individual="Rusty",
                camera="Wildkamera Hecke Nordseite unten",
            ),
            failures=["weather", "events", "sighting", "bookings"],
        ),
        SETTINGS,
    )
    _assert_frame_sane(img, "extreme")


def test_running_booking_is_marked():
    """A stay that covers `now` gets the red badge; the same stay later does not."""
    running = Booking(dt.date(2026, 8, 24), dt.date(2026, 8, 27), 3, "Alex", 1)
    future = Booking(dt.date(2026, 9, 24), dt.date(2026, 9, 27), 3, "Alex", 1)
    # Below the red section rule, right-hand side: only the badge lives here.
    band = (280, 130, 390, 190)

    now_img = render_dashboard(_dashboard(bookings=[running]), SETTINGS)
    later_img = render_dashboard(_dashboard(bookings=[future]), SETTINGS)
    _save(now_img, "booking_running")

    assert palette.RED in _colours(now_img, band)
    assert palette.RED not in _colours(later_img, band)


def test_frames_are_deterministic():
    """Same input, identical bytes - the caching layer hashes the frame."""
    data = _dashboard()
    a = _arr(render_dashboard(data, SETTINGS))
    b = _arr(render_dashboard(data, SETTINGS))
    assert np.array_equal(a, b)


# --------------------------------------------------------------------------
# Battery
# --------------------------------------------------------------------------

# The glyph plus its label live in the top right corner.
BATTERY_BOX = (250, 0, 400, 40)


@pytest.mark.parametrize(
    "kwargs, name, expect_red",
    [
        ({"battery_percent": 76}, "bat_ok", False),
        ({"battery_percent": 5}, "bat_low", True),
        ({"battery_percent": 19}, "bat_edge", True),
        ({"battery_percent": None}, "bat_unknown", False),
        ({"battery_percent": 64, "is_charging": True}, "bat_charging", False),
        ({"battery_percent": 8, "is_charging": True}, "bat_charging_low", True),
        # A broken gauge must not be able to push the date out of the header.
        ({"battery_percent": 999}, "bat_bogus", False),
        ({"battery_percent": -5}, "bat_negative", False),
    ],
)
def test_battery_states(kwargs, name, expect_red):
    img = render_dashboard(_dashboard(telemetry=_telemetry(**kwargs)), SETTINGS)
    _assert_frame_sane(img, name)
    colours = _colours(img, BATTERY_BOX)
    assert (palette.RED in colours) is expect_red
    # Something is always drawn up there, even with no reading at all.
    assert _is_ink(img)[0:40, 250:400].any()
    # ...and the battery never eats the date it sits next to.
    assert _is_ink(img)[8:37, MARGIN:120].any(), "the headline date went missing"


def test_unknown_battery_draws_an_empty_cell():
    """No reading must not be rendered as 0 %, and must not fill the cell."""
    unknown = render_dashboard(_dashboard(telemetry=_telemetry(battery_percent=None)), SETTINGS)
    full = render_dashboard(_dashboard(telemetry=_telemetry(battery_percent=100)), SETTINGS)
    ink_unknown = _is_ink(unknown)[0:40, 250:400].sum()
    ink_full = _is_ink(full)[0:40, 250:400].sum()
    assert ink_unknown < ink_full


def test_charging_bolt_is_visible_over_the_fill():
    """The bolt inverts the pixels it covers, so it shows on both halves."""
    charging = _arr(render_dashboard(
        _dashboard(telemetry=_telemetry(battery_percent=64, is_charging=True)), SETTINGS))
    idle = _arr(render_dashboard(
        _dashboard(telemetry=_telemetry(battery_percent=64, is_charging=False)), SETTINGS))
    assert not np.array_equal(charging[0:40, 250:400], idle[0:40, 250:400])


# --------------------------------------------------------------------------
# Sighting / photo path
# --------------------------------------------------------------------------


def test_sighting_without_image_uses_the_full_width(tmp_path):
    img = render_dashboard(_dashboard(sighting=SAMPLE_SIGHTING), SETTINGS)
    _assert_frame_sane(img, "sighting_no_image")
    # Caption starts at the left margin because no tile is reserving that space.
    assert _is_ink(img)[500:575, MARGIN : MARGIN + 40].any()


def test_missing_photo_file_does_not_kill_the_frame():
    broken = Sighting(
        captured_at=SAMPLE_SIGHTING.captured_at,
        caption=SAMPLE_CAPTION,
        camera="Wiesenkamera",
        image_path="/nonexistent/does-not-exist.jpg",
    )
    img = render_dashboard(_dashboard(sighting=broken), SETTINGS)
    _assert_frame_sane(img, "sighting_broken_path")


def test_broken_dither_module_degrades_to_a_flat_photo(tmp_path, monkeypatch):
    """A regression in the neighbouring module costs the dithering, not the frame."""
    import app.render.dither as dither

    def explode(*_args, **_kwargs):
        raise RuntimeError("dither module went sideways")

    monkeypatch.setattr(dither, "dither_photo", explode)
    sighting = Sighting(
        captured_at=SAMPLE_SIGHTING.captured_at,
        caption=SAMPLE_CAPTION,
        camera="Wiesenkamera",
        image_path=str(_photo(tmp_path / "cam.jpg")),
    )
    img = render_dashboard(_dashboard(sighting=sighting), SETTINGS)
    _assert_frame_sane(img, "sighting_dither_failed")
    assert _is_ink(img)[440:570, MARGIN : MARGIN + 130].any()


def test_out_of_palette_tile_is_repaired_not_pasted(tmp_path, monkeypatch):
    """A dither module that returns greys must never reach the frame."""
    import app.render.dither as dither

    def grey(_img, size, method="floyd"):
        return Image.new("RGB", size, (128, 128, 128))

    monkeypatch.setattr(dither, "dither_photo", grey)
    sighting = Sighting(
        captured_at=SAMPLE_SIGHTING.captured_at,
        caption=SAMPLE_CAPTION,
        camera="Wiesenkamera",
        image_path=str(_photo(tmp_path / "cam.jpg")),
    )
    img = render_dashboard(_dashboard(sighting=sighting), SETTINGS)
    palette.assert_palette_exact(_arr(img))


def test_the_cameras_black_bands_are_trimmed_off(tmp_path):
    """A typical trail-camera capture is 1280x720 with thirty dead rows top and bottom.

    That is eight per cent of a tile that is already the smallest thing on the
    frame, and pure black in the source also holds the autocontrast pass in
    dither.py down to a black point that is nowhere in the scene.
    """
    w, h, band = 640, 480, 30
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.dstack([
        (xx * 255 // w).astype(np.uint8),
        np.full((h, w), 90, dtype=np.uint8),
        np.full((h, w), 140, dtype=np.uint8),
    ])
    arr[:band] = 0
    arr[h - band :] = 0
    path = tmp_path / "letterboxed.png"
    Image.fromarray(arr).save(path)

    assert layout._open_photo(
        Sighting(captured_at=NOW, caption="x", image_path=str(path))
    ).size == (w, h - 2 * band)

    # A capture that is dark all over is a night frame, not a letterboxed one,
    # and must come through untouched rather than be cropped away.
    night = Image.fromarray(np.full((h, w, 3), 3, dtype=np.uint8))
    assert layout._trim_letterbox(night).size == (w, h)


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------


def test_bundled_fonts_are_present():
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        assert (layout.FONT_DIR / name).is_file(), (
            f"{name} must ship in backend/assets/fonts so the container does not "
            "depend on system fonts"
        )


def test_missing_font_raises_instead_of_falling_back(tmp_path, monkeypatch):
    """A substituted font renders a wrong layout without erroring - refuse it."""
    monkeypatch.setattr(layout, "FONT_DIR", tmp_path)
    layout.load_font.cache_clear()
    try:
        with pytest.raises(layout.FontMissingError):
            layout.load_font(False, 16)
    finally:
        layout.load_font.cache_clear()


def test_tiny_panel_is_refused():
    with pytest.raises(layout.LayoutError):
        render_dashboard(_dashboard(), Settings(panel_width=128, panel_height=64))


# --------------------------------------------------------------------------
# Hostile input
# --------------------------------------------------------------------------


def test_newlines_in_scraped_text_cannot_add_unbudgeted_lines():
    """A "\\n" reaching draw.text() would draw a line the fitter never measured."""
    assert layout.flatten("Konzert\nim\tKeller  ") == "Konzert im Keller"
    img = render_dashboard(
        _dashboard(
            bookings=[Booking(dt.date(2026, 10, 7), dt.date(2026, 10, 8), 1,
                              "Alex\nBeispiel", 1)],
            events=[Event(dt.date(2026, 8, 25), "19:30",
                          "Zeile eins\nZeile zwei\nZeile drei", "Ort\nmit\nUmbruch")],
            sighting=Sighting(captured_at=SAMPLE_SIGHTING.captured_at,
                              caption="Erste Zeile\n\nZweite Zeile", camera="Wiesenkamera"),
        ),
        SETTINGS,
    )
    _assert_frame_sane(img, "newlines")


def test_a_smaller_panel_still_produces_a_sane_frame():
    """Geometry is derived from the settings, not from a second set of constants."""
    small = Settings(panel_width=320, panel_height=400)
    img = render_dashboard(_dashboard(sighting=SAMPLE_SIGHTING), small)
    _save(img, "small_panel")
    assert img.size == (320, 400)
    palette.assert_palette_exact(_arr(img))
    ink = _is_ink(img)
    assert not ink[:, : MARGIN - 1].any()
    assert not ink[:, 320 - MARGIN + 1 :].any()
    assert not ink[399:, :].any()


# --------------------------------------------------------------------------
# Compact rows, and the space they buy back
# --------------------------------------------------------------------------
#
# The five-event floor and the tile size are pinned further up, next to the
# frames they were measured on. What is left here is the other half of that
# budget: the rows themselves had to get smaller for it to be payable, and the
# sighting was reserving a rung for a photo that did not always exist.

#: Two lines and the gap under them - what one event entry costs. A blank run
#: this tall is a hole: another entry would have fitted in it.
EVENT_ROW_H = 38


def _tallest_blank_run(img: Image.Image) -> int:
    """The tallest run of empty rows inside the body, ignoring the tail.

    Paper under the last section is the end of the page. A run in the middle is
    space the fitter took away from one section and then gave to nobody.
    """
    ink = _is_ink(img)
    top, bottom = layout.HEADER_H + 4, img.height - layout.FOOTER_H - 6
    rows = ink[top:bottom].any(axis=1)
    if not rows.any():
        return 0
    run = best = 0
    for filled in rows[: int(np.where(rows)[0].max()) + 1]:
        run = 0 if filled else run + 1
        best = max(best, run)
    return best


def test_five_events_survive_a_long_event_list(tmp_path):
    """The floor holds under pressure, not only on a day with six entries."""
    img = _typical_with_photo(tmp_path, events=_dashboard().events * 3)
    _assert_frame_sane(img, "many_events")
    assert _visible_events(img) >= 5


def test_a_photo_that_cannot_be_read_gives_its_space_back(tmp_path):
    """A dead path costs the photo and nothing else.

    Reserving a tile-sized rung on the strength of a path that turns out to be
    unreadable is worse than having no photo at all: the reader loses the
    picture *and* the event that rung cost, and gets blank paper where both
    used to be. Measured against a sighting that never had an image.
    """
    broken = render_dashboard(
        _dashboard(sighting=Sighting(
            captured_at=SAMPLE_SIGHTING.captured_at, caption=SAMPLE_CAPTION,
            species="Fuchs", individual="Rusty", camera="Wiesenkamera",
            image_path=str(tmp_path / "never-written.jpg"))),
        SETTINGS,
    )
    _assert_frame_sane(broken, "broken_photo_path")
    without = render_dashboard(_dashboard(sighting=SAMPLE_SIGHTING), SETTINGS)

    assert _visible_events(broken) == _visible_events(without)
    blank = _tallest_blank_run(broken)
    assert blank < EVENT_ROW_H, f"{blank} px held for a photo that never arrived"


def test_a_booking_is_one_line():
    """The stay reads as one row: the range, then who, then how many.

    The second line used to say "1 Nacht . Alex . 1 Gast" under each of three
    near-identical stays - three lines restating the range printed above them.
    """
    block = layout._prepare_booking(SAMPLE_BOOKINGS[0], NOW.date(), 376)
    body = layout.load_font(False, 16)
    assert block.height <= layout.line_height(body) + 8
    assert {line.dy for line in block.lines} == {0}, "the stay spilled onto a second line"
    # The range is the headline and the name has to survive beside it; the
    # guest count is the only part allowed to go when the room runs out.
    assert any("Okt." in line.text for line in block.lines)
    assert any("Alex" in line.text for line in block.lines)


def test_without_a_bookings_source_the_section_is_left_out():
    """No SOURCES_DSN: no red heading, no "Keine Buchungen", and the events move up.

    A heading over an empty list would spend a fifth of the panel on a feature
    the operator never switched on.
    """
    without = render_dashboard(_dashboard(bookings=[]), Settings(sources_dsn=""))
    with_source = render_dashboard(_dashboard(bookings=[]), SETTINGS)
    _assert_frame_sane(without, "no_bookings_source")

    assert not _rule_rows(without, palette.RED)
    assert _rule_rows(with_source, palette.RED)
    assert min(_rule_rows(without, palette.BLUE)) < min(_rule_rows(with_source, palette.BLUE))


def test_section_titles_come_from_the_settings():
    custom = Settings(
        sources_dsn=SETTINGS.sources_dsn,
        bookings_title="Gäste",
        events_title="Kalender",
        sighting_title="Wildkamera",
    )
    default = render_dashboard(_dashboard(sighting=SAMPLE_SIGHTING), SETTINGS)
    renamed = render_dashboard(_dashboard(sighting=SAMPLE_SIGHTING), custom)
    _assert_frame_sane(renamed, "custom_titles")
    # Same layout, different words: the headings are where the frames differ.
    assert not np.array_equal(_arr(default), _arr(renamed))


def test_three_bookings_cost_three_rows_not_six():
    """Measured on the frame: the distance between the two section rules."""
    img = render_dashboard(_dashboard(), SETTINGS)
    red, blue = max(_rule_rows(img, palette.RED)), min(_rule_rows(img, palette.BLUE))
    body = layout.line_height(layout.load_font(False, 16))
    assert blue - red < 6 * body, "three stays are taking two lines each again"


def test_the_venue_reads_as_a_separate_fact():
    """"Der Menschenfeind  Stadttheater" used to be one run-on phrase.

    Either the venue sits on a line of its own or it is a column with real
    white space in front of it. Both are legible; one string is not.
    """
    block = layout._prepare_event(
        Event(dt.date(2026, 8, 28), "18:00", "Der Menschenfeind", "Stadttheater"),
        NOW.date(), 376,
    )
    title = next(line for line in block.lines if line.text == "Der Menschenfeind")
    venue = next(line for line in block.lines if line.text == "Stadttheater")

    # One assertion that holds whichever way the layout separates them. The
    # earlier version hid its only check behind the same-line branch, so once
    # the venue moved onto a line of its own the test stopped verifying
    # anything and would have passed a run-on phrase again.
    if venue.dy == title.dy:
        title_end = title.dx + layout.text_width(title.text, title.font)
        starts_at = venue.dx - (layout.text_width(venue.text, venue.font)
                                if venue.anchor.startswith("r") else 0)
        gap = starts_at - title_end
        separated = gap >= 12
        why = f"venue shares the title's line with only {gap}px between them"
    else:
        separated = True
        why = ""

    assert separated, why


def test_an_event_without_a_start_time_prints_only_the_day(tmp_path):
    """About one scraped entry in twenty has no usable time.

    No time is an answer, not a missing value: the day goes out alone rather
    than with a separator and nothing behind it, or an invented "00:00".
    """
    block = layout._prepare_event(
        Event(dt.date(2026, 8, 27), None, "Bauernmarkt", "Marktplatz"),
        NOW.date(), 376,
    )
    kicker = block.lines[0]
    assert kicker.text == "Do 27. Aug."
    assert "·" not in kicker.text and ":" not in kicker.text

    img = _typical_with_photo(
        tmp_path,
        events=[Event(dt.date(2026, 8, 27) + dt.timedelta(days=i), None,
                      f"Termin ohne Uhrzeit {i}", "Marktplatz")
                for i in range(6)],
    )
    _assert_frame_sane(img, "no_start_times")
    assert _visible_events(img) >= 5
