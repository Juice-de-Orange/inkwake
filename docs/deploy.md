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
[firmware/README.md](../firmware/README.md)). A lost token cannot be recovered:
`device remove`, then `device add` again.

Per-device settings (wake times, panel-care and battery thresholds) are changed
with `device set`; `python -m app.cli device set --help` lists them. The ranges
are the same the database enforces — the 180 s minimum between refreshes comes
from the panel's datasheet and cannot be lowered.

## 5. Over-the-air firmware updates

```bash
python firmware/tools/verify-image.py firmware/.pio/build/papercolor/firmware.bin
# /tmp in the container is a tmpfs, which `docker cp` cannot write to -- pipe it in:
docker compose exec -T inkwake sh -c 'cat > /tmp/firmware.bin' < firmware/.pio/build/papercolor/firmware.bin
docker compose exec inkwake python -m app.cli firmware add /tmp/firmware.bin --version 1.0.2
docker compose exec inkwake python -m app.cli device set <device-id> --firmware <firmware-id>
```

`--version` must equal `FW_VERSION` compiled into the image (`verify-image.py`
prints it). The board downloads an offered image only when its battery allows,
never in the same wake as a repaint, and never the same digest twice. OTA has not
yet been exercised on real hardware; see *Known gaps* in the firmware README.

## Backups

Back up the `inkwake_data` volume, or at least `inkwake.sqlite3` in it. It holds
the device registry including the token hashes; losing it means re-provisioning
every board. Frames and the calendar cache are rebuilt automatically.

```bash
# SQLite's online backup, so a wake writing at the same moment cannot tear it.
docker compose exec -T inkwake python -c \
  "import sqlite3; s=sqlite3.connect('/data/inkwake.sqlite3'); d=sqlite3.connect('/tmp/b.sqlite3'); s.backup(d); d.close()"
docker compose exec -T inkwake cat /tmp/b.sqlite3 > inkwake-backup.sqlite3
```

## Updating

```bash
git pull
docker compose up -d --build
```

The schema is migrated at startup. A database written by a *newer* version is
refused rather than guessed at, so a rollback needs the matching backup.

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
