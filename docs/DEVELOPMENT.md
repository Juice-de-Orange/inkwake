# Development

## Server

Python 3.13 (the version the image uses).

```bash
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest -q
python -m ruff check .
```

Run the suite more than once: `pytest-randomly` shuffles the order on every run,
and it has caught a real state leak between tests before. Nothing in the suite
touches the network or a database server; sources are served from fakes and the
device registry runs against a SQLite file in a temporary directory.

One test skips unless the M5GFX sources are present under
`firmware/.pio/libdeps/` (they appear after the first firmware build): it checks
that the server's palette still matches the colours M5GFX quantises to.

### Working on the layout without hardware

```bash
DATA_DIR=./data PUBLIC_BASE_URL=http://127.0.0.1:8099 \
  python -m uvicorn app.main:app --reload --port 8099
# then open http://127.0.0.1:8099/preview
```

`/preview` renders a frame on demand from the configured sources. Without a
device it uses empty telemetry. `?fresh=1` drops the source cache, `?raw=1`
returns the packed 4 bpp buffer the board would receive.

For layout changes, the tests in `tests/test_layout.py` check pixels rather than
return values, and write every frame they render to `tests/out/` so you can look
at it. `LONG_EVENTS` there holds titles as long as real calendar feeds produce —
measure against it, not only against short fixtures.

`python scripts/render_examples.py` regenerates `docs/example-frame.png` and
`docs/failure-frame.png` from made-up data.

### Device registry during development

```bash
DATA_DIR=./data python -m app.cli device add --label dev
DATA_DIR=./data python -m app.cli device list
```

## Firmware

See [firmware/README.md](../firmware/README.md). In short:

```bash
pip install platformio
cd firmware
pio run -e papercolor              # build
pio run -e papercolor -t upload    # flash over USB
pio device monitor -b 115200       # serial log
```

## Conventions

- Code, comments, commit messages and documentation in English; the text on the
  panel itself is German.
- Conventional Commits.
- Comments explain the trap the code avoids, not what the next line does.
- No real credentials, hostnames or personal data in code, tests or fixtures.
  `gitleaks` runs in CI and as a pre-commit hook (`pre-commit install`).
