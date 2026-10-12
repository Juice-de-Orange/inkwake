# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `device import` registers a board that already holds a token, from the
  token's SHA-256 hash (`--token-hash`, or `-` to read it from stdin). For
  moving a running installation without re-provisioning the boards. A clear
  token is refused and never echoed.
- `device rotate-token <id>` gives a device a new token, printed once; the old
  token stops working immediately. Settings, telemetry, logs and the firmware
  assignment stay (#2).
- Panel text in English and German: `PANEL_LANGUAGE=en|de`. Dates, headings,
  weather, notes and footer come from one catalog (`app/panel_text.py`); `de`
  renders the same frames as 0.1.0 (#4).

### Changed

- **The panel speaks English by default.** An installation updated from 0.1.0
  that wants to keep its German panel sets `PANEL_LANGUAGE=de`.
- `BOOKINGS_TITLE`, `EVENTS_TITLE` and `SIGHTING_TITLE` default to empty, which
  means the heading of the panel language. A `.env` copied from 0.1.0's
  `.env.example` still sets the German headings explicitly; empty them for
  English headings.
- `.env.example`, the compose file and the deploy guide say plainly that the
  default location (Vienna city centre) and time zone are an example to replace.
- First public release. Server: TRMNL BYOS endpoints for the M5Stack PaperColor,
  six-ink colour pipeline, server-side panel care, SQLite device registry with a
  CLI, weather and sun times, iCalendar events, optional bookings and sightings
  from Postgres views, source prewarming, OTA offers. Firmware 1.0.1 for the
  PaperColor with PMIC power-off, wake watchdog and setup portal.
