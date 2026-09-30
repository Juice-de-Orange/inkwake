## What and why

<!-- What does this change and which problem does it solve? Link the issue: Closes #123 -->

## How it was tested

<!-- Commands you ran and what they showed. A bug fix comes with a test that fails without it.
     Firmware: did it run on a real board? -->

## Checklist

- [ ] Commits follow Conventional Commits and are signed off (`git commit -s`)
- [ ] `python -m pytest -q` (twice) and `python -m ruff check .` pass
- [ ] Firmware touched? `pio run -e papercolor` builds
- [ ] Docs, `README.md` or `.env.example` updated for user-facing changes
- [ ] No real credentials, calendar links, hostnames, IP addresses, coordinates or personal data in code, tests or screenshots
