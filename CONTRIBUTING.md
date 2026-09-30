# Contributing to topdeck

Welcome. Small PRs beat big ones here.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . pytest ruff
pytest
ruff check .
ruff format --check .
```

CI runs the same four steps on Python 3.10 through 3.13.

## Ground rules

- Branch off main, open a PR, keep it focused. One change per PR.
- Add tests for behavior you add. A handful of real tests beats a coverage badge.
- Run ruff before you push.
- No telemetry, ever. No network calls without a clear reason stated in the PR. No new dependencies without saying why.
- Write like a person. Short sentences. Say what changed and why.

## What good looks like

A PR that fixes one thing, proves it with a test, and explains itself in a few lines gets merged fast. If you are unsure about an approach, open the PR anyway and ask in the description.
