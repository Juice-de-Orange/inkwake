"""Render the example frames in docs/ from made-up data.

    python scripts/render_examples.py

The README shows what the panel shows, so these frames go through the same
renderer and PNG encoder as a real wake. Every name, event and caption below
is invented, and the "photo" is drawn here -- nothing in docs/ comes from a
real calendar, booking or camera.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.models import (  # noqa: E402
    Booking,
    Dashboard,
    Event,
    Sighting,
    SunTimes,
    Telemetry,
    Weather,
)
from app.render.encode import encode_png  # noqa: E402
from app.render.layout import render_dashboard  # noqa: E402

DOCS = ROOT / "docs"
NOW = dt.datetime(2026, 9, 18, 14, 50)
NEXT = dt.datetime(2026, 9, 18, 19, 50)


def _meadow(path: Path) -> Path:
    """A drawn stand-in for a trail-camera capture: sky, hills, a fox."""
    w, h = 640, 480
    yy, xx = np.mgrid[0:h, 0:w]
    sky = np.dstack([
        (150 + yy * 60 // h).astype(np.uint8),
        (190 + yy * 40 // h).astype(np.uint8),
        np.full((h, w), 235, dtype=np.uint8),
    ])
    img = Image.fromarray(sky)
    draw = ImageDraw.Draw(img)
    draw.ellipse((470, 40, 560, 130), fill=(255, 230, 150))
    draw.polygon([(0, 260), (180, 190), (360, 250), (520, 200), (640, 240), (640, 480), (0, 480)],
                 fill=(70, 130, 60))
    draw.polygon([(0, 330), (260, 290), (640, 340), (640, 480), (0, 480)], fill=(95, 155, 70))
    # The fox: body, head, ears, tail.
    draw.ellipse((250, 330, 390, 400), fill=(200, 90, 30))
    draw.ellipse((360, 300, 430, 360), fill=(205, 95, 35))
    draw.polygon([(372, 308), (382, 278), (394, 306)], fill=(150, 60, 20))
    draw.polygon([(400, 304), (414, 276), (422, 308)], fill=(150, 60, 20))
    draw.ellipse((180, 340, 280, 380), fill=(210, 110, 45))
    draw.ellipse((178, 348, 205, 372), fill=(245, 240, 230))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    img.save(path, quality=92)
    return path


def _dashboard(photo: Path, **overrides: object) -> Dashboard:
    today = NOW.date()
    base: dict[str, object] = dict(
        now=NOW,
        next_refresh=NEXT,
        telemetry=Telemetry(
            device_id="AA:BB:CC:DD:EE:01",
            battery_percent=81,
            is_charging=False,
            temperature_c=21.8,
            humidity_pct=47.0,
            rssi_dbm=-63,
        ),
        weather=Weather(17.4, 2, "Teilweise bewölkt", 20, 11.0, 19.0),
        sun=SunTimes(dt.time(6, 51), dt.time(19, 13)),
        bookings=[
            Booking(today - dt.timedelta(days=1), today + dt.timedelta(days=2), 3, "Alex", 2),
            Booking(dt.date(2026, 9, 26), dt.date(2026, 9, 28), 2, "Familie Berg", 4),
            Booking(dt.date(2026, 10, 9), dt.date(2026, 10, 10), 1, "Sam", 1),
        ],
        events=[
            Event(today, "19:30", "Sommerkonzert im Park", "Stadtpark"),
            Event(today + dt.timedelta(days=1), "10:00", "Bauernmarkt", "Marktplatz"),
            Event(today + dt.timedelta(days=2), None, "Kunstwoche", "Galerie am Fluss"),
            Event(today + dt.timedelta(days=2), "18:00", "Lesung: Neue Erzählungen", "Stadtbibliothek"),
            Event(today + dt.timedelta(days=3), "14:00", "Stadtfest", "Hauptplatz"),
            Event(today + dt.timedelta(days=4), "20:00", "Theater im Hof", "Schlosshof"),
        ],
        sighting=Sighting(
            captured_at=dt.datetime(2026, 9, 18, 6, 12),
            caption="Rusty ist wieder da: der Fuchs quert frühmorgens die Wiese hinter dem Haus.",
            species="Fuchs",
            individual="Rusty",
            camera="Wiesenkamera",
            image_path=str(photo),
        ),
        failures=[],
    )
    base.update(overrides)
    return Dashboard(**base)  # type: ignore[arg-type]


def main() -> int:
    settings = replace(Settings(), sources_dsn="postgresql://example")
    with tempfile.TemporaryDirectory() as tmp:
        photo = _meadow(Path(tmp) / "meadow.jpg")
        frames = {
            "example-frame.png": _dashboard(photo),
            # Two sources down: the panel keeps what it has and says so in the
            # footer instead of throwing the frame away.
            "failure-frame.png": _dashboard(photo, weather=None, events=[], failures=["weather", "events"]),
        }
        for name, data in frames.items():
            img = render_dashboard(data, settings)
            (DOCS / name).write_bytes(encode_png(img))
            print(f"wrote docs/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
