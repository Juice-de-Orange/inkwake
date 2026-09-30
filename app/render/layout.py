"""Dashboard layout: one Dashboard object in, one 400x600 RGB frame out.

Everything here obeys three rules that come straight from HARDWARE.md and are
not stylistic preferences:

  1. No antialiasing, ever. ImageDraw's default fontmode is "L", which renders
     grey glyph edges. Those greys get quantised to whichever of six inks is
     nearest, so an edge pixel becomes yellow or blue and body text turns into
     coloured fringe at 180 PPI (4.7, gotcha 7). `draw.fontmode = "1"` is the
     single most important line in this file.
  2. Only DEVICE_RGB values reach the frame. assert_palette_exact() runs on the
     finished image; it is a programming-error tripwire, not a data check, so it
     is allowed to raise - unlike anything that touches a content source.
  3. Every text/background pair goes through assert_readable(), because
     yellow-on-white is invisible on this panel and no amount of proofreading
     catches that reliably.

Header and footer are fixed bands. Between them the body is measured whole
before any of it is drawn, because two sections want the same paper: the event
list and the sighting photo. The split is decided in one place, by one rule -
a bigger tile may cost the sixth event, never the fifth (EVENT_FLOOR) - and
what the fitter cannot spend is given back by pulling the sighting up under
the last event rather than leaving a hole above it. When content does not fit
we drop whole entries rather than clip them, because a half-drawn line on a
bistable panel stays wrong for eight hours.

Content failures never propagate. A missing photo, a broken dither module or a
None weather block degrade to a quieter frame plus a line in the footer; only
palette and font violations are fatal, and both are bugs in this repo rather
than in the world.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont

from ..config import Settings
from ..config import settings as default_settings
from ..models import Booking, Dashboard, Event, Sighting, Telemetry
from . import palette
from .palette import BLACK, BLUE, GREEN, RED, WHITE

# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

MARGIN = 12
#: Header is a fixed band: the date, the battery and the ambient readings are
#: the one part of the frame whose position must not move between refreshes.
HEADER_H = 96
FOOTER_H = 20
SECTION_GAP = 5

#: Ink for the battery glyph flips to red below this. Deliberately well above
#: the firmware's charge prompt (config.battery_low_mv) - this is a warning,
#: not the cutoff.
LOW_BATTERY_PCT = 20

MAX_BOOKINGS = 3
#: Space the events section needs before it is worth drawing at all: heading
#: plus one entry. Below that the section is dropped whole.
MIN_EVENTS_H = 66

#: How many events the body owes the reader before the photo may claim space.
#: The whole point of the panel is the next few days, and 400x600 has room for
#: exactly this many next to three stays and a usable tile - see the tile
#: ladder below for the trade the fitter is allowed to make.
EVENT_FLOOR = 5

#: Below this the list has stopped being a list, and no picture is worth that.
#: It exists because EVENT_FLOOR alone produced the opposite failure: when no
#: rung of the ladder could reach five entries, every rung tied at a loss, the
#: tile area was never compared at all, and the photo was dropped whole - on
#: 16.09.2026 the real panel showed a caption and no cat for exactly this
#: reason. The two floors together say it properly: five entries outrank any
#: picture, a picture outranks the fourth, and three outrank the picture again.
EVENT_HARD_FLOOR = 3

_TITLE_SIZE = 24
_HEADING_SIZE = 18
_BODY_SIZE = 16
_META_SIZE = 14

#: Both sections are lists of short rows, so the gap only has to separate two
#: lines, not two paragraphs; the blue kicker does most of the grouping work.
_BOOKING_GAP = 3
_EVENT_GAP = 3

#: Tile sizes the sighting photo may claim, largest first. 130x100 was the old
#: fixed size and it was a mistake: at ~180 PPI with six inks and no greys a
#: tile that small shows a blur where the animal was. The ladder bottoms out
#: instead of shrinking indefinitely - below the last rung a photo is not worth
#: the space and the caption goes alone.
_TILE_SIZES = ((180, 135), (160, 120), (144, 108), (128, 96))
_TILE_GAP = 10
#: The caption column next to the tile has to hold real words, not one
#: hyphenated fragment per line.
_MIN_CAPTION_W = 90

#: backend/assets/fonts - shipped with the code so a container without
#: fontconfig renders identically to a laptop.
FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_FONT_FILES = {False: "DejaVuSans.ttf", True: "DejaVuSans-Bold.ttf"}


class LayoutError(RuntimeError):
    """The frame could not be built at all."""


class FontMissingError(LayoutError):
    """A required font file is not on disk.

    Raised instead of falling back to ImageFont.load_default(): a silently
    substituted font still renders, just at the wrong metrics, which is the
    documented #1 source of invisible layout bugs (HARDWARE.md 9.3).
    """


# --------------------------------------------------------------------------
# Fonts and text measurement
# --------------------------------------------------------------------------


@lru_cache(maxsize=32)
def load_font(bold: bool, size: int) -> FreeTypeFont:
    path = FONT_DIR / _FONT_FILES[bool(bold)]
    if not path.is_file():
        raise FontMissingError(
            f"font {path} is missing; put DejaVuSans.ttf and DejaVuSans-Bold.ttf "
            f"in {FONT_DIR} (they are not optional - a substituted font renders "
            "a wrong layout without erroring)"
        )
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:  # unreadable or corrupt file
        raise FontMissingError(f"font {path} could not be loaded: {exc}") from exc


def text_width(text: str, font: FreeTypeFont) -> int:
    """Advance width in whole pixels, rounded up.

    getlength() rather than getbbox(): we place the *next* element, so the pen
    advance is the right measure, and rounding up keeps a one-pixel rounding
    error from pushing ink past the margin.
    """
    return int(math.ceil(font.getlength(text)))


def line_height(font: FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def flatten(text: str) -> str:
    """Collapse every whitespace run to one space.

    Scraped event titles and AI captions arrive with newlines and tabs in them.
    A newline reaching draw.text() silently becomes a second line that the
    height calculation never accounted for, i.e. text on top of text.
    """
    return " ".join(text.split())


def ellipsize(text: str, font: FreeTypeFont, max_width: int) -> str:
    """Shorten `text` until it fits, ending in a single-glyph ellipsis."""
    text = flatten(text)
    if max_width <= 0:
        return ""
    if text_width(text, font) <= max_width:
        return text
    if text_width("…", font) > max_width:
        return ""
    for cut in range(len(text) - 1, 0, -1):
        candidate = text[:cut].rstrip() + "…"
        if text_width(candidate, font) <= max_width:
            return candidate
    return "…"


def _split_long_word(word: str, font: FreeTypeFont, max_width: int) -> list[str]:
    """Hard-break a word that cannot fit on any line.

    Event titles arrive from scrapers, so "Sonderausstellungseroeffnung..." with
    no space in 90 characters is a real input, not a hypothetical.
    """
    if text_width(word, font) <= max_width:
        return [word]
    chunks: list[str] = []
    piece = ""
    for ch in word:
        if piece and text_width(piece + ch, font) > max_width:
            chunks.append(piece)
            piece = ch
        else:
            piece += ch
    if piece:
        chunks.append(piece)
    return chunks


def wrap_text(
    text: str,
    font: FreeTypeFont,
    max_width: int,
    max_lines: Optional[int] = None,
) -> list[str]:
    """Greedy word wrap, measured with the real font.

    With max_lines set, the surplus is folded back into the last kept line and
    ellipsised there, so the reader sees where the title was cut instead of a
    line that just stops.
    """
    if max_width <= 0 or not text.strip():
        return []

    words: list[str] = []
    for raw in text.split():
        words.extend(_split_long_word(raw, font, max_width))

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if not current or text_width(candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    if max_lines is not None and len(lines) > max_lines:
        if max_lines <= 0:
            return []
        tail = " ".join(lines[max_lines - 1 :])
        lines = lines[: max_lines - 1] + [ellipsize(tail, font, max_width)]
    return lines


# --------------------------------------------------------------------------
# German formatting
# --------------------------------------------------------------------------

# Hard-coded rather than locale-driven: the container has no de_AT locale and
# setlocale() is process-global, so a locale-formatted date is a bug that only
# shows up in production.
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)
MONTHS_SHORT = (
    "Jan.", "Feb.", "März", "Apr.", "Mai", "Juni",
    "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez.",
)

#: Source names as the fetchers report them -> what a human reads in the footer.
FAILURE_LABELS = {
    "weather": "Wetter",
    "sun": "Sonnenzeiten",
    "bookings": "Buchungen",
    "events": "Termine",
    "sighting": "Sichtung",
    "telemetry": "Sensorwerte",
}


def _weekday(day: date) -> str:
    return WEEKDAYS_SHORT[day.weekday()]


def format_long_date(day: date) -> str:
    return f"{_weekday(day)} {day.day:02d}. {MONTHS[day.month - 1]}"


def format_short_date(day: date) -> str:
    return f"{_weekday(day)} {day.day:02d}. {MONTHS_SHORT[day.month - 1]}"


def format_stay(arrival: date, departure: date) -> str:
    """Render a stay as "Mi 07. – Do 08. Okt." - one month name when shared.

    `departure` is the day the flat is free again (models.Booking), so both
    ends are printed as given; no arithmetic happens here.
    """
    if arrival.year == departure.year and arrival.month == departure.month:
        return (
            f"{_weekday(arrival)} {arrival.day:02d}. – "
            f"{_weekday(departure)} {departure.day:02d}. {MONTHS_SHORT[departure.month - 1]}"
        )
    return f"{format_short_date(arrival)} – {format_short_date(departure)}"


def _guests(count: int) -> str:
    return "1 Gast" if count == 1 else f"{count} Gäste"


def _hhmm(value: datetime) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


# --------------------------------------------------------------------------
# Painter
# --------------------------------------------------------------------------


class _Painter:
    """Thin wrapper that makes the two panel rules unforgettable.

    Every text call goes through here so fontmode and the readability check
    cannot be forgotten at one call site out of forty.
    """

    def __init__(self, width: int, height: int) -> None:
        self.img = Image.new("RGB", (width, height), WHITE)
        self.draw = ImageDraw.Draw(self.img)
        # See module docstring rule 1. Without this the frame is unreadable.
        self.draw.fontmode = "1"
        self.w = width
        self.h = height

    def text(
        self,
        xy: tuple[int, int],
        text: str,
        font: FreeTypeFont,
        fill: tuple[int, int, int],
        bg: tuple[int, int, int] = WHITE,
        anchor: str = "la",
    ) -> None:
        if not text:
            return
        palette.assert_readable(fill, bg)
        # flatten() again as a last line of defence: one stray newline in a
        # scraped title would draw a second, unbudgeted line.
        self.draw.text(xy, flatten(text), font=font, fill=fill, anchor=anchor)

    def rect(self, x: int, y: int, w: int, h: int, fill: tuple[int, int, int]) -> None:
        if w <= 0 or h <= 0:
            return
        self.draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)


@dataclass(frozen=True)
class _Line:
    dx: int
    dy: int
    text: str
    font: FreeTypeFont
    colour: tuple[int, int, int]
    bg: tuple[int, int, int] = WHITE
    #: "ra" right-aligns at dx. Used for the trailing metadata of a row (guest
    #: count, venue), which reads as a second column instead of as more of the
    #: same sentence.
    anchor: str = "la"


@dataclass
class _Block:
    """A measured, position-independent piece of content.

    Measuring and drawing are split because the fitter has to know a block's
    height before it decides whether the block is drawn at all.
    """

    height: int
    lines: list[_Line] = field(default_factory=list)
    rects: list[tuple[int, int, int, int, tuple[int, int, int]]] = field(default_factory=list)

    def draw(self, p: _Painter, x: int, y: int) -> None:
        for rx, ry, rw, rh, colour in self.rects:
            p.rect(x + rx, y + ry, rw, rh, colour)
        for line in self.lines:
            p.text((x + line.dx, y + line.dy), line.text, line.font, line.colour, line.bg,
                   anchor=line.anchor)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

#: Lightning bolt in a unit box, drawn by inverting the pixels it covers so it
#: stays visible over both the filled and the empty part of the battery.
_BOLT = ((0.60, 0.00), (0.16, 0.56), (0.44, 0.56), (0.34, 1.00), (0.84, 0.40), (0.56, 0.40))


def _invert_region(img: Image.Image, box: tuple[int, int, int, int], accent: tuple[int, int, int],
                   mask: Image.Image) -> None:
    """Swap white and `accent` inside `mask`. Result stays inside DEVICE_RGB."""
    x0, y0, x1, y1 = box
    tile = np.array(img.crop((x0, y0, x1 + 1, y1 + 1)), dtype=np.uint8)
    m = np.array(mask, dtype=bool)
    if m.shape != tile.shape[:2]:
        return
    is_accent = (tile == np.array(accent, dtype=np.uint8)).all(axis=-1)
    tile[m & is_accent] = np.array(WHITE, dtype=np.uint8)
    tile[m & ~is_accent] = np.array(accent, dtype=np.uint8)
    img.paste(Image.fromarray(tile), (x0, y0))


def _draw_battery(p: _Painter, tel: Telemetry, right: int, top: int) -> int:
    """Battery glyph plus percentage, right-aligned. Returns its left edge.

    An unknown level draws an empty cell and a "?" - printing "0 %" for a board
    that simply did not report would be a lie the reader cannot detect.
    """
    pct = tel.battery_percent
    if pct is not None and not 0 <= pct <= 100:
        # A reading outside 0..100 is a broken gauge, not a battery state.
        # "?" is the truthful rendering and it keeps "999 %" from shoving the
        # date off the header.
        pct = None
    charging = bool(tel.is_charging)
    accent = RED if (pct is not None and pct < LOW_BATTERY_PCT) else BLACK
    label = f"{pct} %" if pct is not None else "?"

    font = load_font(True, _BODY_SIZE)
    label_w = text_width(label, font)
    body_w, body_h, nub_w, nub_h, gap = 38, 20, 3, 9, 6
    left = right - (body_w + nub_w + gap + label_w)

    bx1, by1 = left + body_w - 1, top + body_h - 1
    p.draw.rectangle([left, top, bx1, by1], outline=accent, width=2)
    nub_y = top + (body_h - nub_h) // 2
    p.draw.rectangle([bx1 + 1, nub_y, bx1 + nub_w, nub_y + nub_h - 1], fill=accent)

    ix0, iy0, ix1, iy1 = left + 4, top + 4, bx1 - 4, by1 - 4
    inner_w, inner_h = ix1 - ix0 + 1, iy1 - iy0 + 1
    if pct is not None and inner_w > 0:
        filled = int(round(inner_w * min(100, max(0, pct)) / 100))
        p.rect(ix0, iy0, filled, inner_h, accent)
    if charging and inner_w > 4 and inner_h > 4:
        mask = Image.new("1", (inner_w, inner_h), 0)
        ImageDraw.Draw(mask).polygon(
            [(int(fx * (inner_w - 1)), int(fy * (inner_h - 1))) for fx, fy in _BOLT], fill=1
        )
        _invert_region(p.img, (ix0, iy0, ix1, iy1), accent, mask)

    p.text((left + body_w + nub_w + gap, top + body_h // 2), label, font, accent, anchor="lm")
    return left


def _indoor_line(tel: Telemetry) -> str:
    parts: list[str] = []
    if tel.temperature_c is not None:
        parts.append(f"{tel.temperature_c:.1f} °C")
    if tel.humidity_pct is not None:
        parts.append(f"{tel.humidity_pct:.0f} % rF")
    if not parts:
        return ""
    return "Innen " + " · ".join(parts)


def _draw_header(p: _Painter, data: Dashboard) -> None:
    x0, x1 = MARGIN, p.w - MARGIN
    f_title = load_font(True, _TITLE_SIZE)
    f_body = load_font(False, _BODY_SIZE)
    f_meta = load_font(False, _META_SIZE)

    # The battery claims the right edge first; the title is then ellipsised
    # into what is left, so a long month name can never overwrite it.
    bat_left = _draw_battery(p, data.telemetry, right=x1, top=9)
    y = 8
    p.text((x0, y), ellipsize(format_long_date(data.now.date()), f_title, bat_left - 10 - x0),
           f_title, BLACK)
    y += line_height(f_title)

    indoor = _indoor_line(data.telemetry)
    if indoor:
        p.text((x0, y), ellipsize(indoor, f_body, x1 - x0), f_body, BLACK)
    else:
        p.text((x0, y), "Innen unbekannt", f_body, BLACK)
    y += line_height(f_body)

    width = x1 - x0
    w = data.weather
    if w is None:
        p.text((x0, y), "Wetter unbekannt", f_body, BLACK)
    else:
        head = f"{w.temp_c:.1f} °C"
        tail = ""
        if w.temp_min_c is not None and w.temp_max_c is not None:
            tail = f"{w.temp_min_c:.0f}/{w.temp_max_c:.0f} °C"
        spent = text_width(f"{head} · ", f_body)
        if tail:
            spent += text_width(f" · {tail}", f_body)
        desc = ellipsize(w.description or "", f_body, width - spent)
        # Joined from the non-empty parts, so a description that had to be
        # dropped entirely does not leave a stranded " ·  · " behind.
        p.text((x0, y), " · ".join(part for part in (head, desc, tail) if part),
               f_body, BLACK)
    y += line_height(f_body)

    meta: list[str] = []
    if w is not None:
        meta.append(f"Regen {w.precip_prob_pct} %")
    if data.sun is not None:
        meta.append(
            f"Sonne {data.sun.sunrise:%H:%M}–{data.sun.sunset:%H:%M}"
        )
    if data.telemetry.rssi_dbm is not None:
        meta.append(f"WLAN −{abs(data.telemetry.rssi_dbm)} dBm")
    while meta and text_width(" · ".join(meta), f_meta) > width:
        # Dropped from the left, so the WLAN reading is the last thing to go:
        # a missing sunset is cosmetic, a missing RSSI hides a real problem.
        meta.pop(0)
    if meta:
        p.text((x0, y), " · ".join(meta), f_meta, BLACK)

    p.rect(x0, HEADER_H - 3, x1 - x0, 1, BLACK)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _heading_height() -> int:
    """Label, rule, and four pixels of air. Three headings are three per cent
    of the frame each, so the padding here is bought back in event rows."""
    return line_height(load_font(True, _HEADING_SIZE)) + 6


def _draw_heading(p: _Painter, y: int, text: str, colour: tuple[int, int, int],
                  note: str = "") -> int:
    font = load_font(True, _HEADING_SIZE)
    p.text((MARGIN, y), text, font, colour)
    if note:
        f_note = load_font(False, _META_SIZE)
        room = p.w - MARGIN - (MARGIN + text_width(text, font) + 8)
        p.text((p.w - MARGIN, y + line_height(font) - line_height(f_note) - 1),
               ellipsize(note, f_note, room), f_note, BLACK, anchor="ra")
    rule_y = y + line_height(font) + 2
    p.rect(MARGIN, rule_y, p.w - 2 * MARGIN, 2, colour)
    return y + _heading_height()


def _prepare_booking(booking: Booking, today: date, width: int) -> _Block:
    """One stay, one line: the range on the left, who and how many on the right.

    This used to be two lines, and with real data the second one read
    "1 Nacht · <name> · 1 Gast" three times almost verbatim - sixty pixels
    spent on a sentence the date range already tells you. The nights are gone
    for that reason (they are the range, restated); the guest count is the
    first thing dropped when a long name needs the room, the range never is.
    """
    f_date = load_font(True, _BODY_SIZE)
    f_body = load_font(False, _BODY_SIZE)
    lines: list[_Line] = []
    rects: list[tuple[int, int, int, int, tuple[int, int, int]]] = []

    # Comparison, not arithmetic: `nights` is precomputed upstream precisely so
    # the renderer never adds days to a date (models.Booking).
    running = booking.arrival <= today < booking.departure
    right = width
    if running:
        f_badge = load_font(True, 13)
        badge_w = text_width("jetzt", f_badge) + 12
        badge_h = line_height(f_badge) + 2
        rects.append((width - badge_w, 1, badge_w, badge_h, RED))
        lines.append(_Line(width - badge_w + 6, 2, "jetzt", f_badge, WHITE, RED))
        right = width - badge_w - 8

    stay = ellipsize(format_stay(booking.arrival, booking.departure), f_date, right)
    lines.append(_Line(0, 0, stay, f_date, BLACK))

    room = right - text_width(stay, f_date) - 12
    guests = f" · {_guests(booking.guests)}"
    # A name cut down to two glyphs plus an ellipsis identifies nobody, so
    # below that the guest count gives up its space rather than the name.
    if room - text_width(guests, f_body) < text_width("Mm…", f_body):
        guests = ""
    tail = ellipsize(booking.name or "—", f_body, room - text_width(guests, f_body))
    if tail:
        lines.append(_Line(right, 0, tail + guests, f_body, BLACK, anchor="ra"))
    return _Block(height=line_height(f_body) + _BOOKING_GAP, lines=lines, rects=rects)


def _prepare_event(event: Event, today: date, width: int, max_title_lines: int = 2) -> _Block:
    """Two lines: when and where in small type, then the title in body type.

    The venue used to sit directly behind the title in the same row of text,
    which read as one run-on phrase - "Der Menschenfeind  Stadttheater". It
    now shares the kicker line but is right-aligned and black against the blue
    day, so the two are separate columns rather than one sentence, and the
    title gets the full width back.
    """
    f_meta = load_font(False, _META_SIZE)
    f_title = load_font(False, _BODY_SIZE)
    lines: list[_Line] = []

    day_txt = "Heute" if event.day == today else format_short_date(event.day)
    kicker = ellipsize(f"{day_txt} · {event.start}" if event.start else day_txt, f_meta, width)
    lines.append(_Line(0, 0, kicker, f_meta, BLUE))
    if event.location:
        # 16 px of clear paper between the two columns, so they never read as
        # one string even when both run long.
        venue = ellipsize(event.location, f_meta, width - text_width(kicker, f_meta) - 16)
        if venue:
            lines.append(_Line(width, 0, venue, f_meta, BLACK, anchor="ra"))

    # One pixel inside the kicker's descender space, which leaves the kicker
    # and its title closer together than _EVENT_GAP puts two entries. Without
    # that difference a list of six entries reads as twelve loose lines.
    y = line_height(f_meta) - 1
    title_lines = wrap_text(event.title or "—", f_title, width, max_lines=max_title_lines)
    if not title_lines:
        title_lines = ["—"]
    for text in title_lines:
        lines.append(_Line(0, y, text, f_title, BLACK))
        y += line_height(f_title)

    return _Block(height=y + _EVENT_GAP, lines=lines)


def _fit_events(events: Sequence[Event], today: date, width: int, top: int,
                limit: int, title_lines: int = 2) -> tuple[list[_Block], int]:
    """Measure as many whole events as fit between `top` and `limit`.

    Returns the blocks and how many were left over. Nothing is clipped: on a
    bistable panel a half-drawn row stays wrong for eight hours, so an event
    is either fully there or counted in the heading's overflow note.

    `title_lines` is a budget for the whole list, not a property of an entry.
    Two is what a title deserves; one is what the fitter may buy the photo with
    (see the bidding in render_dashboard). It is applied to every entry rather
    than to some of them, because a list where entry three is cut and entry
    four is not reads as a rendering fault rather than as a decision.
    """
    fitted: list[_Block] = []
    cursor = top
    for event in events:
        block = _prepare_event(event, today, width, max_title_lines=title_lines)
        if cursor + block.height > limit:
            # A second title line is usually what does not fit. Cutting the
            # title of the last entry beats dropping the entry entirely.
            if title_lines > 1:
                block = _prepare_event(event, today, width, max_title_lines=1)
            if cursor + block.height > limit:
                break
        fitted.append(block)
        cursor += block.height
    return fitted, len(events) - len(fitted)


def _trim_letterbox(photo: Image.Image) -> Image.Image:
    """Drop the dead black bands the camera bakes into its frames.

    The Blink stills arrive as 1280x720 with about thirty pure-black rows top
    and bottom. They cost eight per cent of a tile that is already too small,
    and they anchor the black point of the autocontrast pass in dither.py, so
    what is left comes out flatter than it needs to be. Only edge bands that
    are essentially unlit are removed, and never more than a third of a side -
    a genuinely dark night capture must survive this untouched.
    """
    grey = np.asarray(photo.convert("L"), dtype=np.float32)
    if grey.size == 0:
        return photo

    def band(profile: np.ndarray) -> tuple[int, int]:
        lo, hi = 0, len(profile)
        while lo < hi and profile[lo] < 8.0:
            lo += 1
        while hi > lo and profile[hi - 1] < 8.0:
            hi -= 1
        keep = len(profile) - lo - (len(profile) - hi)
        if keep < len(profile) * 2 // 3:
            return 0, len(profile)
        return lo, hi

    top, bottom = band(grey.mean(axis=1))
    left, right = band(grey.mean(axis=0))
    if (left, top, right, bottom) == (0, 0, photo.width, photo.height):
        return photo
    return photo.crop((left, top, right, bottom))


def _open_photo(sighting: Sighting) -> Optional[Image.Image]:
    """Decode the capture once, or give up quietly.

    Deliberately split from the dithering. The fitter has to know whether a
    photo *exists* before it reserves a tile-sized rung for it, and decoding is
    the expensive half, so doing it here lets four tile sizes be measured for
    the price of none: only the size that wins is ever dithered.
    """
    if not sighting.image_path:
        return None
    try:
        with Image.open(sighting.image_path) as src:
            src.load()
            return _trim_letterbox(src.convert("RGB"))
    except Exception:
        return None


def _render_tile(photo: Image.Image, size: tuple[int, int]) -> Optional[Image.Image]:
    """Dither an already decoded capture to the six inks.

    Every failure path returns None: a dither module that is not there yet, or
    one that has regressed, costs the photo and never the frame.
    """
    tile: Optional[Image.Image] = None
    try:
        from .dither import dither_photo  # local import: sibling module, optional

        tile = dither_photo(photo, size)
    except Exception:
        tile = None
    if tile is None:
        try:
            tile = _cover_crop(photo, size)
        except Exception:
            return None
    if tile.size != size:
        # NEAREST only - any interpolating filter invents colours between inks.
        tile = tile.resize(size, Image.Resampling.NEAREST)
    return _force_palette(tile)


def _cover_crop(photo: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Undithered fallback: scale to cover, crop centred. Still full-colour -
    _force_palette() does the ink matching right after."""
    src = photo.convert("RGB")
    scale = max(size[0] / src.width, size[1] / src.height)
    scaled = src.resize(
        (max(1, int(round(src.width * scale))), max(1, int(round(src.height * scale)))),
        Image.Resampling.LANCZOS,
    )
    x = (scaled.width - size[0]) // 2
    y = (scaled.height - size[1]) // 2
    return scaled.crop((x, y, x + size[0], y + size[1]))


def _force_palette(tile: Image.Image) -> Image.Image:
    """Guarantee the tile is exact DEVICE_RGB before it touches the frame.

    The dither module is written by someone else and may regress; catching that
    here turns a whole-frame PaletteError into a slightly uglier photo.
    """
    arr = np.array(tile.convert("RGB"), dtype=np.uint8)
    try:
        palette.assert_palette_exact(arr)
        return Image.fromarray(arr)
    except palette.PaletteError:
        idx = palette.nearest_ink(palette.srgb_to_lab(arr))
        return Image.fromarray(palette.DEVICE_LUT[idx])


def _tile_ladder(width: int) -> list[tuple[int, int]]:
    """Tile sizes that still leave a readable caption column, largest first."""
    return [s for s in _TILE_SIZES if width - s[0] - _TILE_GAP >= _MIN_CAPTION_W]


def _measure_sighting(sighting: Sighting, width: int,
                      tile: Optional[tuple[int, int]]) -> _Block:
    """Caption block for a given tile size - no image is touched.

    Measuring without loading is what lets the fitter try four tile sizes
    against the event list for the price of none: the photo is dithered once,
    after the size has been decided.
    """
    f_body = load_font(False, _BODY_SIZE)
    lh = line_height(f_body)

    if tile is not None:
        caption_x = tile[0] + _TILE_GAP
        caption_w = width - caption_x
        # The caption wraps beside the tile, so it may never grow past it.
        max_lines = max(1, tile[1] // lh)
        floor_h = tile[1]
    else:
        caption_x, caption_w, max_lines, floor_h = 0, width, 3, 0

    caption = sighting.caption or sighting.species or "Sichtung"
    text_lines = wrap_text(caption, f_body, caption_w, max_lines=max_lines)
    lines = [_Line(caption_x, i * lh, text, f_body, BLACK)
             for i, text in enumerate(text_lines)]
    return _Block(height=max(floor_h, len(text_lines) * lh), lines=lines)


def _sighting_note(sighting: Sighting) -> str:
    parts = [f"{_weekday(sighting.captured_at.date())} {sighting.captured_at:%H:%M}"]
    if sighting.individual:
        parts.append(sighting.individual)
    elif sighting.species:
        parts.append(sighting.species)
    if sighting.camera:
        parts.append(sighting.camera)
    return " · ".join(parts)


# --------------------------------------------------------------------------
# Footer
# --------------------------------------------------------------------------


def _failure_hint(failures: Sequence[str]) -> str:
    names = [FAILURE_LABELS.get(f, f) for f in failures if f]
    if not names:
        return ""
    return "ohne " + ", ".join(names)


def _draw_footer(p: _Painter, data: Dashboard) -> None:
    font = load_font(False, _META_SIZE)
    width = p.w - 2 * MARGIN
    y = p.h - FOOTER_H

    long_form = (
        f"Stand {_hhmm(data.now)} · nächste Aktualisierung "
        f"{_hhmm(data.next_refresh)}"
    )
    short_form = f"Stand {_hhmm(data.now)} · nächste {_hhmm(data.next_refresh)}"
    hint = _failure_hint(data.failures)

    if not hint:
        text = long_form
    elif text_width(f"{long_form} · {hint}", font) <= width:
        text = f"{long_form} · {hint}"
    else:
        # The stale-source hint outranks the word "Aktualisierung": knowing what
        # is missing matters more than the polite long form (see models.Dashboard).
        text = f"{short_form} · {hint}"
    p.text((MARGIN, y), ellipsize(text, font, width), font, BLACK)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def render_dashboard(data: Dashboard, settings: Optional[Settings] = None) -> Image.Image:
    """Build one complete frame. Returns a 400x600 RGB image in DEVICE_RGB.

    Everything below the header is measured before anything below the header is
    drawn. That order is what lets the photo and the event list bid against
    each other for the same band of paper, and what lets the events heading
    know how many entries were left over before it is painted.
    """
    cfg = settings or default_settings
    width, height = cfg.panel_width, cfg.panel_height
    if width < 320 or height < 400:
        raise LayoutError(
            f"panel {width}x{height} is below the smallest layout this file "
            "supports; the section minimums assume the native 400x600"
        )

    p = _Painter(width, height)
    _draw_header(p, data)
    _draw_footer(p, data)

    x0 = MARGIN
    content_w = width - 2 * MARGIN
    today = data.now.date()
    body_top = HEADER_H + 4
    body_bottom = height - FOOTER_H - 6
    heading_h = _heading_height()
    f_body = load_font(False, _BODY_SIZE)
    f_meta = load_font(False, _META_SIZE)
    note_h = line_height(f_meta)

    # -- bookings ----------------------------------------------------------
    # One line each and a hard cap of MAX_BOOKINGS, so this section's height is
    # known before anything below it is measured. Only a panel too short for
    # the whole body can push a stay out.
    # Skipped entirely when no bookings source is configured: a heading over
    # "Keine Buchungen" would spend a fifth of the panel saying nothing.
    y = body_top
    if cfg.sources_dsn:
        y = _draw_heading(p, body_top, cfg.bookings_title, RED)
        booking_limit = max(y, body_bottom - MIN_EVENTS_H)
        booking_blocks = [_prepare_booking(b, today, content_w) for b in data.bookings[:MAX_BOOKINGS]]
        shown, cursor = 0, y
        for block in booking_blocks:
            if cursor + block.height > booking_limit:
                break
            cursor += block.height
            shown += 1
        hidden = len(data.bookings) - shown
        if hidden and shown and cursor + note_h > booking_limit:
            shown -= 1
            cursor -= booking_blocks[shown].height
            hidden += 1

        for block in booking_blocks[:shown]:
            block.draw(p, x0, y)
            y += block.height
        if not data.bookings:
            # Only when there really are none. "Keine Buchungen" under a section
            # that was merely too short would be a lie the reader cannot check.
            p.text((x0, y), "Keine Buchungen", f_body, BLACK)
            y += line_height(f_body) + _BOOKING_GAP
        elif hidden:
            # "weitere" only when something is actually shown above it - a lone
            # "3 weitere Buchungen" under an empty heading reads like a bug.
            word = "Buchung" if hidden == 1 else "Buchungen"
            p.text((x0, y), f"{hidden} weitere {word}" if shown else f"{hidden} {word}",
                   f_meta, BLACK)
            y += note_h

    # -- how the rest of the band is split ---------------------------------
    # The photo and the event list want the same paper, and so does the second
    # line of every event title. All three bid here, once, rather than being
    # settled by whichever section happens to draw first.
    #
    # The order below IS the decision; the tuple is only how it is written
    # down. Each candidate is scored on, in this order:
    #
    #   1. EVENT_HARD_FLOOR entries. Fewer than three is not a list any more,
    #      and no picture buys its way past that.
    #   2. A picture at all, on any rung of the ladder.
    #   3. EVENT_FLOOR entries - the five this layout is sized for.
    #   4. Two-line titles, i.e. as few cut titles as possible.
    #   5. The bigger tile.
    #
    # Terms 1 and 3 are the same count clamped twice, and the clamping is the
    # whole mechanism: above a floor the count stops paying, so the next term
    # decides. Without term 1 the old two-term score degenerated exactly where
    # it mattered - with real, two-line event titles no rung reaches five, so
    # every rung tied at a loss, the tile area was never compared, and the
    # photo was dropped every single time (16.09.2026, the frame that prompted
    # this). Without term 4 the only currency left is the picture itself, and a
    # cut title is a far smaller loss to the reader than a missing animal.
    events_top = y + SECTION_GAP + heading_h
    floor = min(EVENT_FLOOR, len(data.events))
    hard_floor = min(EVENT_HARD_FLOOR, len(data.events))
    sighting_block: Optional[_Block] = None
    tile_size: Optional[tuple[int, int]] = None
    event_blocks: list[_Block] = []
    dropped = 0

    # Decoded before the ladder is offered, not when the tile is pasted. A path
    # that turns out to be unreadable would otherwise still reserve a rung, and
    # the reader would lose the photo *and* the event the rung cost, and get
    # blank paper where both had been.
    photo = _open_photo(data.sighting) if data.sighting is not None else None

    if data.sighting is None:
        event_blocks, dropped = _fit_events(data.events, today, content_w, events_top, body_bottom)
    else:
        rungs: list[Optional[tuple[int, int]]] = []
        if photo is not None:
            rungs.extend(_tile_ladder(content_w))
        rungs.append(None)  # caption alone, the rung below the smallest tile

        # Ten measurements at the very most, and every one of them is cheap:
        # _measure_sighting never touches the file and _prepare_event never
        # draws. Only the combination that wins is ever dithered.
        best: Optional[tuple[int, int, int, int, int]] = None
        for size in rungs:
            block = _measure_sighting(data.sighting, content_w, size)
            need = heading_h + block.height + SECTION_GAP
            for title_lines in (2, 1):
                fitted, drop = _fit_events(data.events, today, content_w, events_top,
                                           body_bottom - need, title_lines)
                score = (
                    min(len(fitted), hard_floor),
                    1 if size else 0,
                    min(len(fitted), floor),
                    title_lines,
                    size[0] * size[1] if size else 0,
                )
                if best is None or score > best:
                    best = score
                    sighting_block, tile_size = block, size
                    event_blocks, dropped = fitted, drop

        assert sighting_block is not None  # rungs always ends in one
        if data.events and not event_blocks:
            # Not one entry fits beside the sighting, on any rung - and since
            # entries outrank the picture in the score above, that means no rung
            # could have managed it. The events are the reason the thing is on
            # the wall, so the sighting is what goes.
            #
            # Asked of the winning combination rather than of the geometry: the
            # old form measured from body_top, which ignores the stays above and
            # is three rows too optimistic the moment the flat is booked.
            sighting_block, tile_size = None, None
            event_blocks, dropped = _fit_events(data.events, today, content_w,
                                                events_top, body_bottom)

    sighting_h = heading_h + sighting_block.height + SECTION_GAP if sighting_block else 0
    events_limit = body_bottom - sighting_h

    # -- events ------------------------------------------------------------
    events_end = y
    if events_top + note_h <= events_limit:
        note = ""
        if dropped:
            # In the heading rather than under the last entry: the note used to
            # cost a body line, which on this panel is most of an event.
            word = "weiterer Termin" if dropped == 1 else "weitere Termine"
            if not event_blocks:
                word = "Termin" if dropped == 1 else "Termine"
            note = f"{dropped} {word}"
        _draw_heading(p, y + SECTION_GAP, cfg.events_title, BLUE, note=note)
        cursor = events_top
        for block in event_blocks:
            block.draw(p, x0, cursor)
            cursor += block.height
        if not data.events and cursor + line_height(f_body) <= events_limit:
            p.text((x0, cursor), "Keine Termine", f_body, BLACK)
            cursor += line_height(f_body)
        events_end = cursor

    # -- sighting ----------------------------------------------------------
    if sighting_block is not None and data.sighting is not None:
        tile = _render_tile(photo, tile_size) if (photo is not None and tile_size) else None
        if tile_size is not None and tile is None:
            # The file was there when the size was picked and is not readable
            # now, or the dither module gave up: the caption takes the full
            # width instead. The block only ever shrinks, so nothing overflows.
            sighting_block = _measure_sighting(data.sighting, content_w, None)
        # Anchored to the events, not to the bottom edge: whatever the fitter
        # could not spend on an event would otherwise sit here as a visible
        # hole between the last title and the green rule.
        sy = min(events_end + SECTION_GAP, body_bottom - heading_h - sighting_block.height)
        sy = _draw_heading(p, sy, cfg.sighting_title, GREEN, note=_sighting_note(data.sighting))
        if tile is not None:
            p.img.paste(tile, (x0, sy))
        sighting_block.draw(p, x0, sy)

    palette.assert_palette_exact(np.asarray(p.img))
    return p.img
