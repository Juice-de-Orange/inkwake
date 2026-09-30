# Security Policy

## Supported versions

inkwake is developed on `main`; fixes land there. Please update to the latest
commit before reporting.

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for security
problems.

Report privately through GitHub's private vulnerability reporting on this
repository: **Security → Report a vulnerability**. Include a description, the
commit or firmware version you tested and steps to reproduce.

You will receive an acknowledgement within **7 days**. A fix or workaround is
aimed for within **90 days** of triage, followed by a GitHub security advisory.

## Scope

In scope: the server (device authentication, the admin-key endpoints, the
firmware download path, source parsing, the container setup) and the firmware
(token handling, the setup portal, OTA verification, TLS).

Worth knowing up front, and not a vulnerability by itself:

- On a LAN setup without TLS (`http://`), the device token travels in clear text.
  The documentation recommends HTTPS behind a reverse proxy.
- The setup portal is an open Wi-Fi network for up to 15 minutes, by design of
  the WiFiManager flow; the token field is never pre-filled.
- With `ADMIN_API_KEY` empty, `/preview` and `/api/internal/status` are open.
  The server logs a warning at startup.
