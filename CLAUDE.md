# inkwake — working notes for Claude Code (and humans)

A colour e-ink wall dashboard: an **M5Stack PaperColor (C151)** wakes three times a
day, fetches a finished frame from this server, paints it and switches itself off.
The frame shows indoor climate and battery, weather and sun times, optional
bookings, upcoming calendar events and an optional wildlife-camera sighting.

## Read these first

- **`HARDWARE.md`** — the board's technical reference. It is the truth about the
  hardware and justifies almost every decision here. Where it marks something
  ❌ UNVERIFIED, do not guess.
- **`app/models.py`** — the data contracts. Every source returns these types and
  the renderer consumes only these types. Nothing else crosses that boundary.

## Commands

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q                      # run it more than once, see below
python -m ruff check .
python -m uvicorn app.main:app --reload --port 8099
python -m app.cli --help                 # device registry and firmware
python scripts/render_examples.py        # regenerate docs/*.png
cd firmware && pio run -e papercolor     # firmware build
```

**pytest-randomly is on.** Test order changes on every run; that is deliberate
and has caught a real state leak between tests. One green run is not evidence.

`GET /preview` renders live and returns the PNG — the way to check layout without
hardware. `?raw=1` returns the 120 000 bytes of packed 4 bpp.

## Rules

- Code, identifiers, comments, commits and docs in English. The **panel text** is
  German (headings, weekdays, setup screens); keep new panel strings consistent.
- Never write real credentials, hostnames, IP addresses, coordinates of a home or
  personal data into any file, test or fixture. Secrets go into `.env`; test data
  is made up; example hosts use `example.com` / `192.0.2.x`.
- Conventional Commits (`feat:`, `fix:`, `docs:` …).
- Comments explain which trap the code avoids, not what the next line does.

## Architecture: thin client

The board renders **nothing**. It holds no fonts, knows no time zone and runs no
template engine. It fetches a PNG and blits it. On this panel that is necessity,
not convenience: quantising onto six Spectra 6 inks needs Lab matching against
*measured* panel colours plus error diffusion over the whole image (HARDWARE.md
§9.1).

The protocol is **TRMNL BYOS** — an open, documented contract instead of a home-made
one (§9.2). Telemetry travels in request headers so the server can render the
board's own sensor values into the frame it returns in the same response.

## The decisions you need to know

**1. No ESP32 deep sleep.** On this board it draws 5–10 mA because the rails stay
up. The M5PM1 PMIC switches everything off instead: 92 µA, ~70× less (§6.3). So
the device **cold-boots** on every wake; nothing survives except NVS, 32 bytes of
PMIC RTC RAM and the picture on the panel.

**2. The two-palette trick.** The board re-quantises incoming images itself. The
server therefore decides in Lab against `MEASURED_RGB` which ink a pixel gets, but
paints the result in `DEVICE_RGB`, so M5GFX's nearest-colour pass is an exact hit
and a no-op. Details in `app/render/palette.py` — the most error-prone part of the
project. `assert_palette_exact()` runs on every finished frame: an antialiased
glyph edge would otherwise reach the panel as colour noise with no error anywhere.

**2b. Two clocks, not one.** `last_painted_at` means "we told the board to paint"
and is set *before* the image is requested; `last_image_fetched_at` means "the
board collected the bytes". Keeping them apart is the only way to see the failure
where `image_url` points somewhere the board cannot reach (a loopback
`PUBLIC_BASE_URL`): the board never loads an image while the status looks
healthy. The 180 s floor counts from the **later** of the two — with
`last_painted_at` alone, anyone reaching `/api/display` could keep it fresh and
freeze the panel.

**3. Time belongs to the server.** The board gets a countdown in seconds, never a
time of day. Daylight saving time is only `app/schedule.py`'s problem, and NTP
(up to 4 s of radio per wake) is almost never needed (§9.6).

**4. Every wake is time-boxed.** `finishWake()` is the only place that arms the
PMIC wake timer, so a hang would leave the device dark until someone plugs in USB.
See "Firmware" below.

## Who bids against whom in the frame body

Below the header, three things share the same paper: the event list, the sighting
photo and the second line of each event title. It is decided in **one** place in
`render_dashboard`, and the order is the statement — the tuple is just notation:

1. `EVENT_HARD_FLOOR` (3) events. Fewer is not a list any more.
2. A photo at all, on any rung of the tile ladder.
3. `EVENT_FLOOR` (5) events — the number the layout is designed for.
4. Two-line titles, i.e. as few shortened titles as possible.
5. The bigger tile.

Terms 1 and 3 are **the same number, capped twice**, and the capping is the whole
mechanism: above a threshold the count stops paying, so the next term decides.
Without term 1 the scoring degenerates exactly when it matters: with real,
two-line titles no rung reaches five events, all rungs tie on the first term, the
tile area is never compared and the photo is dropped every time. The tests were
green when that happened, because every fixture title fit on one line. Measure
changes against `LONG_EVENTS` in `tests/test_layout.py`; a test keeps those
titles actually wrapping.

Trap when checking: without a tile, `_tile_rows()` hits the caption, which then
starts at the left margin. "Are there rows?" is **not** a photo check —
`_assert_tile_at_least()` with a minimum height is.

The bookings section is left out entirely when `SOURCES_DSN` is empty.

## The time budget belongs to the board

`_source_budget_s()` is 30 s by default. The firmware hangs up after
`HTTP_TIMEOUT_MS` (8 s, `firmware/src/config.h`), counts the wake as failed and
only comes back at the next slot — the other 22 s buy a frame nobody receives.
`/api/display` therefore uses `assemble.device_budget_s()` (three quarters of
`DEVICE_HTTP_TIMEOUT_S`); `/preview` and `prewarm()` keep the generous budget.
With the prewarm working this costs nothing — the cache is minutes old. It is for
the morning the prewarm failed, where a frame saying "ohne Termine" in the footer
beats a timeout the board turns into a lost slot.

`DEVICE_HTTP_TIMEOUT_S` is not a preference but a fact about the device on the
wall. Changing it means flashing, not restarting.

## Panel care is not optional

The server enforces it too instead of trusting the firmware
(`schedule.should_repaint`). The numbers live per device in the `devices` table and
are guarded by CHECK constraints there — the 180 s floor is the only lower bound in
the schema that comes from a datasheet rather than experience:

- **≥ 180 s between two refreshes.** The documented consequence of ignoring it is
  permanent damage.
- **At least one refresh per 24 h**, or burn-in.
- **No refresh below ~15 °C** — colour casts that take hours at room temperature
  to clear.
- Every refresh takes **15–30 s** and flashes. A seconds display is impossible on
  this panel.

## Data sources

| Source | How | Notes |
|---|---|---|
| Events | ICS feeds (`EVENTS_ICS_URLS`) | RRULE/EXDATE/RECURRENCE-ID, per-feed disk cache |
| Bookings, sightings | two Postgres views (`SOURCES_DSN`), optional | contract in `app/sources/database.py`, examples in `docs/data-sources.md` |
| Weather | Open-Meteo | no key |
| Sun times | computed locally (NOAA) | no network |

**Failure policy.** Sources report errors by *returning normally* with an empty
value plus a failure name, never by raising into the frame. When everything a
panel shows comes only from a cache, the footer says so (`Dashboard.failures`). A
*partial* outage stays in the log on purpose: "ohne Termine" under six visible
events teaches people to ignore hints.

**A failed fetch must not evict a good value.** A source that returns `None` plus
a failure name must not be cached as fresh; with prewarming, a 30-second blip
would otherwise paint the frame without weather for an hour.

**Private calendar links are credentials.** They are never logged (httpx's own
request log is held at WARNING in `main.py`, and `events._describe()` strips URLs
from errors) and never used in file names (the cache is keyed by a hash).

**Sources are pre-warmed before every wake time** (`PREWARM_LEAD_S`, 5 min). A
battery setting, not a freshness setting: the board keeps its radio up at ~120 mA
while the server waits on upstreams. Tied to the wake times of all **active**
devices rather than a fixed interval. The prewarm fills **only** the source cache:
no frame, no store access, and it does **not** run under `_render_lock` — sharing
it would queue a device request behind a slow prewarm. The source pool is sized
for two concurrent collections, and `events.py` writes its disk cache via a unique
temp name for the same reason.

**Events come in two halves, and the seam is load-bearing.** `collect_events`
talks to the network and only knows calendar days; `trim_events` knows the time of
day and touches nothing. The first is cached, the second runs on **every** frame.
The prewarm fills the cache five minutes before the slot — with a different clock
than the wake. Anything time-dependent in the cached value or its key would make
the 14:50 frame show 14:45's view or miss the cache entirely.

The cut has two rules, each preventing one bug:

- **It runs BEFORE `_collapse_runs`, but run detection judges the UNCUT list**
  (`run_days_from`). Cut afterwards, a series whose earliest day was this morning
  disappears instead of moving to tomorrow. Classified on the cut list, a three-day
  series shrinks below `MAX_RUN_DAYS` in the evening and prints **twice**.
- **`start = None` always survives**: an all-day entry, or a multi-day entry that
  started earlier and is still going. Cutting either would invent information.

## Device registry

`app/store.py` keeps devices, the device-log ring and the firmware catalogue in
one SQLite file in the data volume. Devices exist only through the CLI:

- `python -m app.cli device add` creates the row and prints the token
  `<id>.<secret>` **once**; the database stores only the SHA-256 of the full token.
  There is no enrolment endpoint, and no service statement (`SQL_*`) inserts into
  `devices` — a test holds that line.
- The service writes telemetry and state, never identity or settings. Wake times,
  panel-care thresholds, `active`, the token hash and the firmware assignment are
  operator columns (`ADMIN_SQL_*`, `ADMIN_SETTABLE`).
- A store error raises `DbUnavailable` and the caller answers **503**, never 401:
  "no such device" and "could not ask" must not look alike, or a broken disk turns
  into boards that need re-provisioning.
- 401 means "your identity is wrong", 403 means "you are known but disabled". The
  firmware opens the setup portal after repeated 401/403, never on 503.

**Operational values live in the row, not the environment.** Wake times, panel
floor, minimum temperature and battery thresholds come from `devices`; `config.py`
only holds the defaults a new row gets. Everything that needs wake times — the
countdown, the prewarm, the "next refresh" printed on the panel — reads the rows
(the prewarm takes the union over active devices). **One daily slot less saves
about 430 mAh a year**, more than all code optimisations together — a freshness
decision that belongs to the operator.

## Firmware

A wake in `firmware/src/main.cpp` — the order is not stylistic: measure the battery
**before** the radio (TX spikes pull the reading down), Wi-Fi **off** before the
panel is powered (panel current plus TX spikes on a 1250 mAh cell is a real
brownout risk).

**Time limits.** `writeToStream()` has no wall-clock bound on the body, and
`WiFiClient::connected()` reports a half-open socket as connected forever.
Transfers with `Content-Length` are read by hand against an overall and a stall
deadline. Chunked responses still go through `writeToStream()` because only that
path strips chunk sizes; it is bounded through the sink.

**Wake watchdog**, `WAKE_WATCHDOG_S` — and it is *not* hang detection. Outside the
setup portal nothing calls `esp_task_wdt_reset()`, so the number is a deadline on
the elapsed time of *every* healthy wake. It must exceed the sum of all partial
timeouts, not the expected duration; the calculation is in the comment above the
constant and `test_wake_watchdog_clears_the_sum_of_its_own_timeouts` recomputes
it. The expensive item is not obvious: `HTTP_TIMEOUT_MS` does **not** bound the
TLS handshake, whose Arduino default is 120 s per connection — hence
`TLS_HANDSHAKE_TIMEOUT_S`.

**Offline backoff.** `SLEEP_OFFLINE_BACKOFF_S` stretches retries 1 h / 2 h / 4 h /
4 h instead of retrying hourly. It does not implement panel rule 2: the ladder
hangs off `failureStreak`, which every *successful* wake resets, including a 304
that paints nothing.

After a failure reboot (watchdog, panic, brownout) the next slot is **skipped**,
or a reproducible fault becomes a reboot loop. Never on `ESP_RST_POWERON`: that is
the reset reason of every normal wake.

**The wake timer is read back, not trusted.** `power::armWake()` verifies the PMIC
registers. If nothing could be armed the board does **not** power off
(`M5.Power.powerOff()` ends in `esp_deep_sleep_start()` without a wake source on
this chip); it takes a short ESP32 deep sleep instead, counted in NVS and given up
after `PMIC_FALLBACK_MAX` attempts.

The captive portal is the one place where the watchdog does what its name says: it
runs non-blocking, feeds the watchdog each loop and keeps its own absolute
deadline. WiFiManager's own timeout is useless here — every page request extends it.

**The server URL lives in NVS**, set in the setup portal, with the compile-time
define as fallback (a placeholder under `example.com`). Moving the server must not
mean taking the board off the wall.

**Build traps** (§10): OPI PSRAM must be on or `M5.Display` silently does nothing.
There is no PlatformIO board definition — `esp32s3box` plus
`board_build.arduino.memory_type = qio_opi`.

## Deployment

`docker-compose.yml` runs one hardened container (read-only root, no
capabilities, UID 10001) with a named volume for `/data`. The volume holds the
registry — token hashes included — so it is the thing to back up. The port is
published on loopback by default for a reverse proxy; `PUBLIC_BASE_URL` has no
default on purpose. `tests/test_deploy.py` guards compose, Dockerfile and the
firmware/server constants that are duplicated by hand.
