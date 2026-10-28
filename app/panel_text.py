"""Every word the server prints on the panel, per language (`PANEL_LANGUAGE`).

One dataclass, one instance per language. A field is a catalog key, so a
language that forgets a text does not construct -- the parity a dictionary
would need a test for is a TypeError here. The test that remains
(`tests/test_panel_text.py`) checks what the type cannot: the same
placeholders in every template, the same weather codes, and that a frame
rendered in English carries no German word.

Hard-coded rather than locale-driven: the container has no de_AT locale and
setlocale() is process-global, so a locale-formatted date is a bug that only
shows up in production.

What is NOT in here: content (event titles, names, captions come from the
sources as they are), units (°C, dBm, %), and the firmware's own screens
(setup portal, empty battery), which the board draws without the server.

Templates use str.format() with named fields. Plurals are a pair
(one, other) picked by `plural()`; both languages here need no more forms.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelText:
    # -- calendar -----------------------------------------------------------
    weekdays_short: tuple[str, ...]  # Monday first, as date.weekday()
    months: tuple[str, ...]
    months_short: tuple[str, ...]
    #: Header date. Fields: weekday, day, month.
    long_date: str
    #: Event kicker and the two ends of a stay. Fields: weekday, day, month.
    short_date: str
    #: A stay inside one month. Fields: weekday1, day1, weekday2, day2, month.
    stay_same_month: str
    today: str

    # -- header -------------------------------------------------------------
    #: Fields: readings ("21.8 °C · 47 % RH").
    indoor: str
    #: Fields: value (float).
    humidity: str
    indoor_unknown: str
    weather_unknown: str
    #: Fields: pct.
    rain: str
    #: Fields: rise, set ("06:51").
    sun: str
    #: Fields: dbm (positive number; the minus sign is part of the template).
    wifi: str

    # -- sections -----------------------------------------------------------
    bookings_title: str
    events_title: str
    sighting_title: str
    now_badge: str
    guests: tuple[str, str]
    bookings_unavailable: str
    no_bookings: str
    #: "3 more bookings" under the shown ones; "3 bookings" when none fit.
    more_bookings: tuple[str, str]
    bookings_count: tuple[str, str]
    more_events: tuple[str, str]
    events_count: tuple[str, str]
    events_unavailable: str
    no_events: str

    # -- footer -------------------------------------------------------------
    #: Fields: now, next ("14:50").
    footer_long: str
    footer_short: str
    #: Fields: names (already joined).
    missing: str
    #: Source names as the fetchers report them (Dashboard.failures).
    failure_labels: dict[str, str]

    # -- weather ------------------------------------------------------------
    #: WMO 4677 codes. Short enough for a narrow column: a description longer
    #: than ~18 characters gets ellipsised by the layout.
    weather: dict[int, str]
    weather_unknown_code: str


def plural(forms: tuple[str, str], n: int) -> str:
    return (forms[0] if n == 1 else forms[1]).format(n=n)


ENGLISH = PanelText(
    weekdays_short=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    months=(
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    months_short=(
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ),
    long_date="{weekday} {day} {month}",
    short_date="{weekday} {day} {month}",
    stay_same_month="{weekday1} {day1} – {weekday2} {day2} {month}",
    today="Today",
    indoor="Indoor {readings}",
    humidity="{value:.0f} % RH",
    indoor_unknown="Indoor unknown",
    weather_unknown="Weather unknown",
    rain="Rain {pct} %",
    sun="Sun {rise}–{set}",
    wifi="Wi-Fi −{dbm} dBm",
    bookings_title="Bookings",
    events_title="Events",
    sighting_title="Sighting",
    now_badge="now",
    guests=("{n} guest", "{n} guests"),
    bookings_unavailable="Bookings unavailable",
    no_bookings="No bookings",
    more_bookings=("{n} more booking", "{n} more bookings"),
    bookings_count=("{n} booking", "{n} bookings"),
    more_events=("{n} more event", "{n} more events"),
    events_count=("{n} event", "{n} events"),
    events_unavailable="Events unavailable",
    no_events="No events",
    footer_long="As of {now} · next update {next}",
    footer_short="As of {now} · next {next}",
    missing="no {names}",
    failure_labels={
        "weather": "weather",
        "sun": "sun times",
        "bookings": "bookings",
        "events": "events",
        "sighting": "sighting",
        "telemetry": "sensor readings",
    },
    weather={
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        56: "Freezing drizzle",
        57: "Freezing drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Freezing rain",
        67: "Freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Rain showers",
        81: "Showers",
        82: "Heavy showers",
        85: "Snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunder+hail",
        99: "Thunder+hail",
    },
    weather_unknown_code="Unknown",
)

#: The texts of 0.1.0, unchanged: PANEL_LANGUAGE=de renders the same bytes.
GERMAN = PanelText(
    weekdays_short=("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
    months=(
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
    months_short=(
        "Jan.", "Feb.", "März", "Apr.", "Mai", "Juni",
        "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez.",
    ),
    long_date="{weekday} {day:02d}. {month}",
    short_date="{weekday} {day:02d}. {month}",
    stay_same_month="{weekday1} {day1:02d}. – {weekday2} {day2:02d}. {month}",
    today="Heute",
    indoor="Innen {readings}",
    humidity="{value:.0f} % rF",
    indoor_unknown="Innen unbekannt",
    weather_unknown="Wetter unbekannt",
    rain="Regen {pct} %",
    sun="Sonne {rise}–{set}",
    wifi="WLAN −{dbm} dBm",
    bookings_title="Buchungen",
    events_title="Termine",
    sighting_title="Sichtung",
    now_badge="jetzt",
    guests=("{n} Gast", "{n} Gäste"),
    bookings_unavailable="Buchungen nicht abrufbar",
    no_bookings="Keine Buchungen",
    more_bookings=("{n} weitere Buchung", "{n} weitere Buchungen"),
    bookings_count=("{n} Buchung", "{n} Buchungen"),
    more_events=("{n} weiterer Termin", "{n} weitere Termine"),
    events_count=("{n} Termin", "{n} Termine"),
    events_unavailable="Termine nicht abrufbar",
    no_events="Keine Termine",
    footer_long="Stand {now} · nächste Aktualisierung {next}",
    footer_short="Stand {now} · nächste {next}",
    missing="ohne {names}",
    failure_labels={
        "weather": "Wetter",
        "sun": "Sonnenzeiten",
        "bookings": "Buchungen",
        "events": "Termine",
        "sighting": "Sichtung",
        "telemetry": "Sensorwerte",
    },
    # The umlauts stay: an LED matrix without an "ö" glyph might have to write
    # "Bewoelkt", this panel renders real type and a reader would notice.
    weather={
        0: "Klar",
        1: "Meist klar",
        2: "Teilw. bewölkt",
        3: "Bewölkt",
        45: "Nebel",
        48: "Nebel",
        51: "Leichter Niesel",
        53: "Niesel",
        55: "Starker Niesel",
        56: "Eisniesel",
        57: "Eisniesel",
        61: "Leichter Regen",
        63: "Regen",
        65: "Starkregen",
        66: "Eisregen",
        67: "Eisregen",
        71: "Leichter Schnee",
        73: "Schnee",
        75: "Starker Schnee",
        77: "Schneekorn",
        80: "Regenschauer",
        81: "Schauer",
        82: "Starke Schauer",
        85: "Schneeschauer",
        86: "Starke Schneeschauer",
        95: "Gewitter",
        96: "Gewitter+Hagel",
        99: "Gewitter+Hagel",
    },
    weather_unknown_code="Unbekannt",
)

#: Keys are config.PANEL_LANGUAGES; a test holds the two together.
CATALOG: dict[str, PanelText] = {"en": ENGLISH, "de": GERMAN}


def texts_for(language: str) -> PanelText:
    """The texts for a language config.py has already validated."""
    return CATALOG[language]

