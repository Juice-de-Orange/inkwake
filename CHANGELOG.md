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
- First public release. Server: TRMNL BYOS endpoints for the M5Stack PaperColor,
  six-ink colour pipeline, server-side panel care, SQLite device registry with a
  CLI, weather and sun times, iCalendar events, optional bookings and sightings
  from Postgres views, source prewarming, OTA offers. Firmware 1.0.1 for the
  PaperColor with PMIC power-off, wake watchdog and setup portal.
