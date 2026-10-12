# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-11

### Added

- First public release. Server: TRMNL BYOS endpoints for the M5Stack PaperColor,
  six-ink colour pipeline, server-side panel care, SQLite device registry with a
  CLI, weather and sun times, iCalendar events, optional bookings and sightings
  from Postgres views, source prewarming, OTA offers. Firmware 1.0.1 for the
  PaperColor with PMIC power-off, wake watchdog and setup portal.
- `firmware add` checks that a file is a complete inkwake image for this board
  (ESP32-S3 segments, checksum, appended SHA-256, size window) and that its
  compiled-in version matches `--version`; `--force` overrides the image checks
  but never a contradicting version. An update is offered only while its image
  file is present on the server.
- Deployment guide covers a LAN setup without a reverse proxy, getting an
  image into the container for `firmware add`, and restoring a backup.
