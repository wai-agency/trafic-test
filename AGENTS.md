# AGENTS.md

## Cursor Cloud specific instructions

This repository is a single, self-contained **Python CLI** (`traffic-monitor`, brand
"BuzimLine") that monitors live traffic/border data for a Stuttgart → Bužim road trip.
There is **no database, message broker, or companion backend service** — the CLI only makes
outbound HTTPS calls to public third-party APIs and (optionally) serves a static HTML
dashboard. See `README.md` (German) for the full feature/command reference.

### Environment / setup
- Requires Python >= 3.11 (CI/Docker pin 3.12; the VM has 3.12). Work inside the `.venv`
  created by the update script: `source .venv/bin/activate`.
- The startup update script creates `.venv` and runs `pip install -e ".[dev]"` (the `dev`
  extra adds `pytest`). Dependencies come from `pyproject.toml`; there is no lockfile.
- `cp .env.example .env` for local runs. All env vars are **optional** — the app runs
  without any of them. Credential-gated features that stay off unless secrets are set:
  `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (push alerts) and `NAKORDONI_API_KEY`
  (live HR↔BA border wait times).

### Running / testing (activate `.venv` first)
- Tests: `python -m pytest` (9 tests, all offline — they parse sample data, no network).
- Lint: **no linter is configured** (no ruff/flake8/black config and no CI lint step),
  so there is no lint command to run for this repo.
- Core commands: `traffic-monitor recommend` (fully offline), `traffic-monitor check`
  (polls live sources once), `traffic-monitor watch` (loop; used by Docker), and
  `traffic-monitor dashboard --out site` / `--serve --port 8080` (static dashboard).

### Non-obvious caveats
- `check`, `watch`, and `dashboard` need **outbound internet (HTTPS/443)**. Every data
  source is wrapped in try/except, so an unreachable source becomes a soft `info` alert
  rather than a failure. `promet.si` commonly returns 401/blocked in cloud networks — this
  is expected and non-fatal.
- The `dashboard --serve` HTTP listener defaults to host `0.0.0.0`; when testing in the VM
  bind to `127.0.0.1` and curl/browse `http://127.0.0.1:8080/`.
