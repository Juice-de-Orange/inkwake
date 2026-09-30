# Contributing to inkwake

Thanks for taking the time to contribute! Bug reports, ideas and pull requests
are welcome.

Good places to start: issues labelled
[`good first issue`](https://github.com/Juice-de-Orange/inkwake/labels/good%20first%20issue)
and [`help wanted`](https://github.com/Juice-de-Orange/inkwake/labels/help%20wanted).
Larger changes are best discussed in an issue first.

## Before you start

- Read [CLAUDE.md](CLAUDE.md). It lists the design decisions and the traps they
  avoid — the panel-care rules, the two-palette trick, the split between the
  cached and the per-frame half of the event source. A change that breaks one of
  them usually still passes a naive test.
- Hardware facts come from [HARDWARE.md](HARDWARE.md). Where it marks something
  as unverified, measure or ask; do not guess.

## Development setup

The full walkthrough is in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # run it twice: the order is randomised
python -m ruff check .
```

Firmware: `pip install platformio && cd firmware && pio run -e papercolor`.

### Secret guard

The repository ships a [pre-commit](https://pre-commit.com/) hook that runs
[gitleaks](https://github.com/gitleaks/gitleaks) on every commit:

```bash
pip install pre-commit
pre-commit install
```

CI runs the same scanner over the full history. Never put real credentials,
calendar links, hostnames, coordinates or personal data into any file, test or
screenshot — use `.env` (see `.env.example`) and documentation placeholders
(`example.com`, `192.0.2.x`).

## Branch and commit conventions

- Fork, then branch from `main`; name the branch `<kind>/<short-slug>`
  (e.g. `feat/caldav-source`, `fix/ics-timezone`).
- Commits follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `chore:`.
- Sign off your commits with the
  [Developer Certificate of Origin](https://developercertificate.org/):
  `git commit -s`. There is no CLA.

## Pull requests

- A bug fix comes with a test that fails without it.
- Layout changes: attach the frame (`/preview` or `tests/out/`) and run the tests
  in `tests/test_layout.py` — they check pixels, not return values.
- Firmware changes: say whether they ran on real hardware. Timing, power and
  panel behaviour cannot be verified any other way.
- User-facing changes update `README.md`, `.env.example` or the docs.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
