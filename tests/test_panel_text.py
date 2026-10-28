"""The panel text catalog: every language complete, and the frame really uses it.

The dataclass already refuses a language with a missing key. What it cannot
see is a template whose placeholders differ (a KeyError at 07:00, on the
board's wake), a weather code one language forgot, and -- the reason this
file exists -- a string the layout still hard-codes. The last one is checked
on rendered frames: every text the painter draws is recorded, and a frame in
one language must not carry a word that only the other language has.
"""

from __future__ import annotations

import datetime as dt
import re
import string
from dataclasses import fields, replace
from typing import Any

import pytest

from app.config import PANEL_LANGUAGES, Settings
from app.models import Booking, Dashboard, Event, Sighting, SunTimes, Telemetry, Weather
from app.panel_text import CATALOG, ENGLISH, GERMAN, PanelText, plural
from app.render import layout
from app.sources.weather import describe

NOW = dt.datetime(2026, 8, 25, 15, 0)
NEXT = dt.datetime(2026, 8, 25, 20, 0)


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, tuple):
        return [s for v in value for s in _strings(v)]
    raise TypeError(type(value))


def _words(texts: PanelText) -> set[str]:
    out: set[str] = set()
    for f in fields(PanelText):
        for text in _strings(getattr(texts, f.name)):
            literal = re.sub(r"\{[^}]*\}", " ", text)
            out.update(w.lower() for w in re.findall(r"[^\W\d_]{2,}", literal))
    return out


# --------------------------------------------------------------------------
# Parity
# --------------------------------------------------------------------------


def test_the_catalog_has_exactly_the_configurable_languages() -> None:
    assert tuple(CATALOG) == PANEL_LANGUAGES
    assert PANEL_LANGUAGES[0] == "en", "English is the default for a new installation"


@pytest.mark.parametrize("language", [lang for lang in PANEL_LANGUAGES if lang != "en"])
def test_every_language_matches_english_field_by_field(language: str) -> None:
    other = CATALOG[language]
    for f in fields(PanelText):
        mine, theirs = getattr(ENGLISH, f.name), getattr(other, f.name)
        assert type(mine) is type(theirs), f.name
        if isinstance(mine, str):
            assert _placeholders(mine) == _placeholders(theirs), f.name
        elif isinstance(mine, tuple):
            assert len(mine) == len(theirs), f.name
            for a, b in zip(mine, theirs):
                assert _placeholders(a) == _placeholders(b), f.name
        else:
            assert set(mine) == set(theirs), f"{f.name}: keys differ"


@pytest.mark.parametrize("language", PANEL_LANGUAGES)
def test_no_text_is_empty_and_the_calendar_is_complete(language: str) -> None:
    texts = CATALOG[language]
    for f in fields(PanelText):
        for text in _strings(getattr(texts, f.name)):
            assert text.strip(), f"{language}.{f.name} has an empty text"
    assert len(texts.weekdays_short) == 7
    assert len(texts.months) == len(texts.months_short) == 12


@pytest.mark.parametrize("language", PANEL_LANGUAGES)
def test_every_template_formats_with_its_documented_fields(language: str) -> None:
    """A wrong field name is a KeyError on the wake, not in a review."""
    t = CATALOG[language]
    day = dict(weekday="x", day=7, month="y")
    for template in (t.long_date, t.short_date):
        template.format(**day)
    t.stay_same_month.format(weekday1="x", day1=7, weekday2="y", day2=8, month="z")
    t.indoor.format(readings="r")
    t.humidity.format(value=47.0)
    t.rain.format(pct=20)
    t.sun.format(rise="06:51", set="19:13")
    t.wifi.format(dbm=63)
    t.footer_long.format(now="14:50", next="19:50")
    t.footer_short.format(now="14:50", next="19:50")
    t.missing.format(names="a, b")
    for forms in (t.guests, t.more_bookings, t.bookings_count, t.more_events, t.events_count):
        assert "1" in plural(forms, 1) and "4" in plural(forms, 4)


# --------------------------------------------------------------------------
# The frame uses the catalog, and only the catalog
# --------------------------------------------------------------------------


def _dashboards(language: str) -> list[Dashboard]:
    """Content without a word of either language, covering every text branch:
    the running-stay badge, hidden stays, today, a dropped event, every failure
    label, unknown readings, no events, unreachable calendars, and the sighting
    fallback caption."""
    today = NOW.date()
    weather = Weather(17.4, 61, describe(61, language), 20, 11.0, 19.0)
    full = Dashboard(
        now=NOW,
        next_refresh=NEXT,
        telemetry=Telemetry(device_id="x", battery_percent=81, temperature_c=21.8,
                            humidity_pct=47.0, rssi_dbm=-63),
        weather=weather,
        sun=SunTimes(dt.time(6, 51), dt.time(19, 13)),
        bookings=[Booking(today - dt.timedelta(days=1), today + dt.timedelta(days=1), 2, "Alex", 2)]
        + [Booking(today + dt.timedelta(days=d), today + dt.timedelta(days=d + 1), 1, "Sam", 1)
           for d in range(3, 8)],
        events=[Event(today + dt.timedelta(days=d // 2), "19:30", f"Xyzzy {d}", "Plugh")
                for d in range(14)],
        sighting=Sighting(captured_at=NOW - dt.timedelta(hours=3), caption=""),
        failures=["weather", "sun", "bookings", "events", "sighting", "telemetry"],
    )
    empty = Dashboard(now=NOW, next_refresh=NEXT, telemetry=Telemetry(device_id="x"),
                      failures=["bookings"])
    quiet = Dashboard(now=NOW, next_refresh=NEXT, telemetry=Telemetry(device_id="x"))
    no_calendar = Dashboard(now=NOW, next_refresh=NEXT, telemetry=Telemetry(device_id="x"),
                            failures=["events"])
    return [full, empty, quiet, no_calendar]


def _drawn_texts(monkeypatch: pytest.MonkeyPatch, language: str) -> list[str]:
    drawn: list[str] = []
    original = layout._Painter.text

    def record(self: layout._Painter, xy: Any, text: str, *args: Any, **kwargs: Any) -> None:
        drawn.append(text)
        original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(layout._Painter, "text", record)
    settings = replace(Settings(sources_dsn="postgresql://reader@db.example.invalid/example"),
                       panel_language=language)
    for dash in _dashboards(language):
        layout.render_dashboard(dash, settings)
    return drawn


@pytest.mark.parametrize("language, other", [("en", "de"), ("de", "en")])
def test_a_frame_carries_no_word_of_the_other_language(
    monkeypatch: pytest.MonkeyPatch, language: str, other: str
) -> None:
    foreign = _words(CATALOG[other]) - _words(CATALOG[language])
    drawn = _drawn_texts(monkeypatch, language)

    words = {w.lower() for text in drawn for w in re.findall(r"[^\W\d_]{2,}", text)}
    assert not words & foreign, f"{language} frame shows {sorted(words & foreign)}"
    # And the frame is not mute: its own language is on it.
    assert words & (_words(CATALOG[language]) - _words(CATALOG[other]))


def test_english_frame_says_what_it_should(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = " | ".join(_drawn_texts(monkeypatch, "en"))
    for expected in ("Tue 25 August", "Indoor 21.8 °C · 47 % RH", "Light rain", "Rain 20 %",
                     "Wi-Fi −63 dBm", "Bookings", "now", "Today", "Events", "more events",
                     "As of 15:00", "no weather", "Sighting", "Bookings unavailable",
                     "No events", "Events unavailable", "Indoor unknown", "Weather unknown",
                     "No bookings"):
        assert expected in drawn, expected


def test_german_frame_keeps_the_0_1_0_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = " | ".join(_drawn_texts(monkeypatch, "de"))
    for expected in ("Di 25. August", "Innen 21.8 °C · 47 % rF", "Leichter Regen", "Regen 20 %",
                     "WLAN −63 dBm", "Buchungen", "jetzt", "Heute", "Termine",
                     "weitere Termine", "Stand 15:00", "ohne Wetter", "Sichtung",
                     "Buchungen nicht abrufbar", "Keine Termine", "Termine nicht abrufbar",
                     "Innen unbekannt",
                     "Wetter unbekannt", "Keine Buchungen"):
        assert expected in drawn, expected


def test_german_is_the_text_of_0_1_0() -> None:
    """Spot checks against the strings 0.1.0 hard-coded in layout.py."""
    assert GERMAN.footer_long.format(now="14:50", next="19:50") == (
        "Stand 14:50 · nächste Aktualisierung 19:50"
    )
    assert plural(GERMAN.more_bookings, 1) == "1 weitere Buchung"
    assert plural(GERMAN.more_events, 1) == "1 weiterer Termin"
    assert GERMAN.weather[2] == "Teilw. bewölkt"
