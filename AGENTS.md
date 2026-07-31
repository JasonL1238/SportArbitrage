# AGENTS.md

## Cursor Cloud specific instructions

This is a single Python package (a multi-sport odds collector CLI + a static
HTML dashboard). There is no separate frontend/backend service split; everything
runs from the `src` package.

### Environment / commands
- Use `python3` — there is no bare `python` on the VM.
- Dependencies install to the user site via `pip install -r requirements-dev.txt`
  (the update script). No virtualenv is used; do not expect one.
- `python -m playwright install chromium` is **only** needed for
  `ODDS_FETCH_MODE=browser`. The default fetch path is `curl_cffi` Chrome
  impersonation and the test suite is fully offline, so the browser download is
  not part of routine setup.
- There is **no lint tooling** configured (no ruff/flake8/black). CI (`.github/workflows/tests.yml`)
  only runs `pytest`, so "lint" == the test suite here.

### Tests
- Run with `python3 -m pytest tests/ -q`. The full suite takes ~5 minutes and
  runs entirely offline against real captured payloads in `tests/fixtures/raw`.
- `tests/test_arb_scaling.py::TestScale::test_thirty_sources_on_a_full_soccer_slate_stays_fast`
  is a wall-clock performance assertion (`< 1.0s`) and can fail on a shared/slow
  VM even though the algorithm is fine — treat a lone failure there as
  environmental, not a code defect.

### Running the app
- Live collection works from this VM over the network:
  `python3 -m src.collector collect --tier core` (add `--sport baseball` to narrow).
  Some venues are geo-/bot-blocked from a datacenter IP (e.g. `betmgm` → HTTP 403,
  `onexbet` → CAPTCHA, the `an_*` Action Network sources → empty). This is expected:
  a blocked source does not abort the run, and validation may print `FAIL`
  without the process crashing. Set `ODDS_HTTP_PROXY` for a residential exit if needed.
- Dashboard: `python3 -m src.report --serve 8765` serves the **data directory**, so
  the root URL shows a file listing — the dashboard itself is at
  `http://localhost:8765/dashboard.html`, and that served page has a working
  "Scrape now" button that triggers a fresh collection and reloads on the new run.
  `python3 -m src.report` (no `--serve`) just writes a standalone `data/dashboard.html`.
- All artifacts land under `data/` (gitignored): raw envelopes in `data/raw/`,
  normalized rows in `data/collector.sqlite3`. Override paths with `ODDS_DATA_DIR`,
  `ODDS_RAW_DIR`, `ODDS_DB_PATH`.
