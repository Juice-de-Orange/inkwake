# Deploying the server

The server is one container with one volume. It needs outbound HTTPS (weather,
calendar feeds) and has to be reachable by the board.

## 1. Configure

```bash
cp .env.example .env
```

At minimum set `PUBLIC_BASE_URL` — the address the **board** uses to reach the
server, without a trailing slash. Compose refuses to start without it: a loopback
default would hand the board an image URL on its own loopback, and it would never
load a picture while the server believes it painted one.

Set `ADMIN_API_KEY` as well (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
It guards `/preview` and `/api/internal/status`, which render or describe a frame.

Then say where the board hangs: `TZ_NAME`, `WEATHER_LAT` and `WEATHER_LON`
(decimal degrees). The values in `.env.example` are an example — Vienna city
centre — and the weather and sun times on the panel are for that place until you
change them. `PANEL_LANGUAGE` picks the panel text, `en` (default) or `de`.

Everything else is explained in `.env.example`; the data sources are described in
[data-sources.md](data-sources.md).

## 2. Start

```bash
docker compose up -d --build
docker compose logs -f inkwake          # "no devices registered yet" is expected on a fresh start
curl -fsS http://127.0.0.1:8099/healthz
```

The container runs as UID 10001 with a read-only root filesystem, no capabilities
and `no-new-privileges`. All state lives in the named volume `inkwake_data`
mounted at `/data`: the device registry (`inkwake.sqlite3`), rendered frames,
registered firmware images and the calendar cache.

## 3. Make it reachable

Two setups work:

**Behind a reverse proxy (recommended).** Keep the default
`INKWAKE_BIND=127.0.0.1` and let Caddy, nginx or Traefik terminate TLS. The
firmware verifies server certificates against the ESP-IDF CA bundle, so the
certificate must be publicly trusted (Let's Encrypt is fine; self-signed is not).
Example with Caddy:

```caddyfile
eink.example.com {
    reverse_proxy 127.0.0.1:8099
}
```

and `PUBLIC_BASE_URL=https://eink.example.com`. A path prefix works too
(`https://example.com/eink` proxied to the container's `/`): the firmware keeps
the path and appends `/api/display`.

Two things a proxy must not break: firmware downloads need `Content-Length`
(don't turn them into chunked responses), and the board sends its telemetry in
hyphenated headers (`Battery-Voltage`, …), which every proxy passes through.

**On a trusted LAN without TLS.** Set `INKWAKE_BIND=0.0.0.0` and
`PUBLIC_BASE_URL=http://<server-ip>:8099`. The device token then travels in clear
text on your network, and `/preview` is reachable by everyone on it — set
`ADMIN_API_KEY`.

## 4. Add a device

```bash
docker compose exec inkwake python -m app.cli device add --label "Hallway" --mac AA:BB:CC:DD:EE:FF
```

The token is printed once and only its hash is stored. Enter it in the board's
setup portal together with Wi-Fi and `PUBLIC_BASE_URL` (see
[firmware/README.md](../firmware/README.md)). A lost token cannot be recovered,
but it can be replaced:

```bash
docker compose exec inkwake python -m app.cli device rotate-token <device-id>
```

prints a new token once and the old one stops working at once. The device keeps
its id, settings, log and firmware assignment; enter the new token in the board's
setup portal.

**Moving an existing board to this server.** A board that already holds a
token from another inkwake server (or from the system inkwake was derived from)
does not need to be re-provisioned. The row needs the board's device id — the
part of its token in front of the dot — and the SHA-256 hex of its full token,
which is what the old database stores. The clear token is never needed and is
refused:

```bash
# hash.txt holds one line: the 64 hex characters from the old registry.
docker compose exec -T inkwake python -m app.cli device import \
  --id <device-id> --label "Hallway" --slots 06:50,14:50,19:50 --token-hash - < hash.txt
```

Panel-care and battery thresholds start at the defaults; copy the old values
with `device set`. Telemetry and the log fill in from the next wake.

Per-device settings (wake times, panel-care and battery thresholds) are changed
with `device set`; `python -m app.cli device set --help` lists them. The ranges
are the same the database enforces — the 180 s minimum between refreshes comes
from the panel's datasheet and cannot be lowered.

## 5. Over-the-air firmware updates

```bash
python firmware/tools/verify-image.py firmware/.pio/build/papercolor/firmware.bin
# /tmp in the container is a tmpfs, which `docker cp` cannot write to -- pipe it in:
docker compose exec -T inkwake sh -c 'cat > /tmp/firmware.bin' < firmware/.pio/build/papercolor/firmware.bin
# --version must be the FW_VERSION compiled into the image (firmware/src/config.h); the CLI
# reads the marker in the image and refuses a mismatch.
docker compose exec inkwake python -m app.cli firmware add /tmp/firmware.bin --version 1.0.1
docker compose exec inkwake python -m app.cli device set <device-id> --firmware <firmware-id>
```

`--version` must equal `FW_VERSION` compiled into the image (`verify-image.py`
prints it). `firmware add` runs the same checks as `verify-image.py` and
registers nothing when one fails:

- the file is an ESP32-S3 application image (magic byte, chip id, app descriptor),
- it is complete: every segment is there, the checksum and the SHA-256 the build
  appended match, and nothing follows them — a download that stopped halfway
  fails here,
- it carries the `INKWAKE-FW-VERSION:` marker exactly once, and the marker
  equals `--version`,
- its size is inside what the board accepts for a download (512 KiB to 4 MiB),
- nothing in it is shaped like a device token.

That establishes that the file is the one the build wrote for this board, not
that it boots. `--force` registers a file in spite of failed checks (a version
that contradicts the marker stays refused); the board then has only its own.

**When an image is offered.** On a wake, the server offers the image assigned to
the device if the board reports a version in that request and the version
**differs** from the assigned one. Versions are compared, never ordered: assign
an older image and the board is offered the downgrade. `device set <id>
--firmware none` stops the offer.

**Battery.** The offer is held back while the charge the board reports in that
wake is below the device's `--ota-min-battery-pct` (default 50). A wake that
carries no battery reading — the firmware omits it when the PMIC read fails —
is *not* held back, so a board with a broken reading can still be updated. The
board applies its own floor on top (3600 mV, `OTA_MIN_BATTERY_MV`), with the
same rule for a missing reading.

The board never downloads in the same wake as a repaint and never fetches the
same digest twice. OTA has not yet been exercised on real hardware; see *Known
gaps* in the firmware README.

## Backups

Everything the server keeps lives in the volume `inkwake_data` (`/data` in the
container). Two things in it matter:

- `inkwake.sqlite3` — the device registry including the token hashes, the device
  log and the firmware catalogue. Losing it means re-provisioning every board.
- `firmware/` — the OTA images registered with `firmware add`.

Frames (`frames/`) and the calendar cache are rebuilt automatically. Keep a copy
of your `.env` as well; it holds the admin key and possibly a database password
and private calendar links, so store it like a password.

### The whole data directory

The simplest complete backup. The server is stopped for the few seconds it takes,
so nothing writes while the archive is made; do it away from a wake time (a wake
that finds the server down counts as failed and the board retries an hour later).

```bash
docker compose stop inkwake
docker compose run --rm --no-deps -T inkwake tar -C /data -cf - . > inkwake-data.tar
docker compose start inkwake
```

### Only the database, while the server runs

SQLite's online backup, so a wake writing at the same moment cannot tear it.
Firmware images are not included.

```bash
docker compose exec -T inkwake python -c \
  "import sqlite3; s=sqlite3.connect('/data/inkwake.sqlite3'); d=sqlite3.connect('/tmp/b.sqlite3'); s.backup(d); d.close()"
docker compose exec -T inkwake cat /tmp/b.sqlite3 > inkwake-backup.sqlite3
```

### Restore

**The whole data directory.** Stop the server, empty the volume, unpack the
archive into it, start again. This works on the existing volume and on a fresh
one (after `docker compose down -v`, or on a new host: `docker compose run`
creates the volume).

```bash
docker compose stop inkwake
# A one-off container on the same volume, as the same user the server runs as.
docker compose run --rm --no-deps -T inkwake sh -c \
  'find /data -mindepth 1 -delete && tar -C /data -xf -' < inkwake-data.tar
docker compose up -d
docker compose exec inkwake python -m app.cli device list
```

**Only the database.** Stop the server, write the file back into the volume,
start again:

```bash
docker compose stop inkwake
# The -wal/-shm files belong to the database being replaced and must go with it.
docker compose run --rm -T --no-deps inkwake sh -c \
  'cat > /data/inkwake.sqlite3 && rm -f /data/inkwake.sqlite3-wal /data/inkwake.sqlite3-shm' \
  < inkwake-backup.sqlite3
docker compose up -d
docker compose exec inkwake python -m app.cli device list
```

Either way the devices, their settings and their tokens are back as they were at
backup time, and the boards authenticate as before; a device added after the
backup is gone and needs `device add` again. A database-only restore brings the
firmware *catalogue* back but not the image files: on a fresh volume, remove and
re-add each image (`firmware remove`, `firmware add`) — until then the server
does not offer those updates and logs `file … is missing from the firmware dir`
on each wake.

## Updating

Back up first (above). Then, in the checkout:

```bash
git pull                      # or a release: git fetch --tags && git checkout vX.Y.Z
docker compose up -d --build
docker compose logs inkwake   # "database schema version … is newer" means: wrong checkout
docker compose exec inkwake python -m app.cli device list
```

The data volume stays; the registry schema is migrated at startup. A database
written by a *newer* version is refused rather than guessed at, so going back to
an older version needs the backup taken before the update. Read the
[CHANGELOG](../CHANGELOG.md) for settings that changed.

### From 0.1.0

- **The panel is English by default now.** To keep the German panel, add
  `PANEL_LANGUAGE=de` to `.env`. A `.env` copied from 0.1.0's `.env.example`
  sets `EVENTS_TITLE=Termine`, `BOOKINGS_TITLE=Buchungen` and
  `SIGHTING_TITLE=Sichtung`; with `PANEL_LANGUAGE=en` empty those three lines,
  or the headings stay German over English text.
- The database needs nothing: the schema is unchanged (`PRAGMA user_version` 1),
  devices keep their tokens and settings, and nothing has to be re-flashed.
- New commands: `device rotate-token` and `device import` (see *Add a device*).

### What the database holds

For scripts and migrations that read or write the file directly. Stop the server
first if you write.

| Table | What |
|---|---|
| `devices` | One row per board: identity (`id`, `label`, `token_hash`), operator settings, last telemetry and state. |
| `device_logs` | The last 500 log lines per device (`payload` is JSON). |
| `firmware` | Registered OTA images; `file_path` is relative to `/data/firmware`. |

- **Timestamps** (`created_at`, `updated_at`, `first_seen_at`, `last_seen_at`,
  `last_painted_at`, `last_image_fetched_at`, `device_logs.at`, `firmware.created_at`)
  are TEXT in **UTC, ISO 8601 with an explicit offset**, exactly as Python's
  `datetime.isoformat()` writes them: `2026-10-12T06:50:03.123456+00:00`, or
  `2026-10-12T06:50:00+00:00` when the microseconds are zero. The reader also
  accepts `Z` and a space instead of `T`, and takes a value without offset as UTC —
  but write the `+00:00` form: the device list is ordered by comparing this text,
  and mixed spellings sort wrongly.
- `token_hash`: the SHA-256 of the full token `<id>.<secret>`, 64 lowercase hex
  characters. The token itself is stored nowhere.
- `slots`: a JSON array of wake times in minutes after local midnight, sorted,
  1 to 8 entries: `[410,890,1190]` is 06:50, 14:50, 19:50.
- Flags (`active`, `low_battery`, `is_charging`) are `0`/`1`. CHECK constraints
  enforce the panel-care ranges; a row outside them is refused, not clamped.

## Endpoints

| Endpoint | Caller | Auth |
|---|---|---|
| `GET /api/display` | board | device token |
| `GET /api/image/{name}` | board | device token |
| `GET /api/firmware/{id}` | board | device token |
| `POST /api/log` | board | device token |
| `GET /preview` | you | `X-Admin-Key` or `Authorization: Bearer` (if `ADMIN_API_KEY` is set) |
| `GET /api/internal/status` | you | same |
| `GET /healthz` | container healthcheck | none, says nothing about content |
