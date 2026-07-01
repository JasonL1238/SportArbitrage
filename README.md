# SportArbitrage

A cloud-ready sports arbitrage research scanner. Pulls odds from [The Odds API](https://the-odds-api.com/), detects pre-match arbitrage opportunities, stores results in Postgres, sends Discord alerts with deduplication, and provides a Streamlit dashboard with paper-trading verification and analytics.

## Disclaimer

This tool is for **educational research only**. It does not automate betting, scrape sportsbook websites, bypass geolocation, CAPTCHA, or anti-bot systems, or violate any sportsbook terms of service. It uses only legal odds API data. Betting execution remains manual. In Illinois, sports wagering is overseen by the [Illinois Gaming Board](https://igb.illinois.gov/) — only place bets manually through licensed sportsbooks.

## Quick Start (Local)

```bash
# 1. Clone and install
git clone https://github.com/JasonL1238/SportArbitrage.git
cd SportArbitrage
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env. For local ESPN testing, no key or database is required.
# For The Odds API + Postgres, set ODDS_API_KEY and DATABASE_URL.

# 3. Run the scanner
python -m src.scanner --source espn_odds --sport baseball_mlb
python -m src.scanner              # scan all target sports
python -m src.scanner --dry-run    # check events only
python -m src.scanner --sport basketball_nba  # scan one sport
python -m src.scanner --watch --interval 300  # continuous local monitor

# 4. Open the dashboard
streamlit run src/dashboard.py
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ODDS_API_KEY` | No | — | API key from [The Odds API](https://the-odds-api.com/) when using `odds_api` |
| `DATABASE_URL` | No | — | Postgres connection string. If omitted, scanner writes local JSONL records to `LOCAL_DATA_DIR` |
| `LOCAL_DATA_DIR` | No | `.local_data` | Local append-only scan output directory |
| `ODDS_SOURCES` | No | `odds_api` if keyed, else `espn_odds` | Comma-separated source adapters to run |
| `DISCORD_WEBHOOK_URL` | No | — | Discord webhook for alerts |
| `ENABLE_ALERTS` | No | `true` | Enable/disable alert dispatch |
| `MIN_ARB_MARGIN` | No | `0.01` | Minimum arb margin (1%) |
| `MAX_ODDS_AGE_SECONDS` | No | `300` | Max age for bookmaker odds |
| `DEFAULT_STAKE` | No | `100` | Default total stake |
| `TARGET_SPORTS` | No | NBA, MLB, NFL, NHL | Comma-separated sport keys |
| `MARKETS` | No | `h2h,spreads,totals` | Markets to scan |
| `REGIONS` | No | `us` | Regions to fetch |
| `CREDIT_FLOOR` | No | `50` | Stop scanning below this credit level |
| `SCAN_INTERVAL_SECONDS` | No | `300` | Delay between scans in `--watch` mode |
| `REQUIRE_DISTINCT_BOOKS` | No | `true` | Require at least two books for executable arbs |
| `REQUIRE_COMPLETE_OUTCOMES` | No | `true` | Skip incomplete markets |

## Deployment Guide

### 1. Create a Neon Postgres Database

1. Sign up at [neon.tech](https://neon.tech) (free tier available)
2. Create a new project
3. Copy the **pooled connection string** from the dashboard — it looks like:
   ```
   postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/sportarbitrage?sslmode=require
   ```
4. Run the scanner once locally to create all tables automatically:
   ```bash
   DATABASE_URL="postgresql://..." python -m src.scanner --dry-run
   ```

### 2. Set Up Discord Webhook

1. Open your Discord server
2. Go to **Server Settings → Integrations → Webhooks**
3. Click **New Webhook**, choose a channel, and copy the URL
4. Set `DISCORD_WEBHOOK_URL` in your `.env` or GitHub Secrets

### 3. Add GitHub Repository Secrets

Go to your repo on GitHub → **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret | Value |
|--------|-------|
| `ODDS_API_KEY` | Your Odds API key |
| `DATABASE_URL` | Your Neon Postgres connection string |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |

### 4. Deploy Scheduled Scans

The repository includes a GitHub Actions workflow at `.github/workflows/scan.yml` that:

- Runs every 2 hours on a cron schedule (adjustable)
- Can be triggered manually via `workflow_dispatch`
- Installs dependencies and runs tests
- Executes a full scan with your secrets

To adjust the schedule, edit the `cron` value in the workflow file. GitHub Actions cron uses UTC.

### 5. Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env with your DATABASE_URL, ODDS_API_KEY, etc.

# Run a scan
python -m src.scanner

# Open the dashboard
streamlit run src/dashboard.py
```

### 6. Checking Logs

- **GitHub Actions**: Go to your repo → Actions tab → click on a workflow run to see logs
- **Database**: Use the Neon SQL Editor to query `scan_runs`, `alert_logs`, or `arb_opportunities`
- **Dashboard**: The Analytics tab shows rolling summaries of scans, alerts, and verified arbs

## Credit Budget

The Odds API free tier gives **500 credits/month**. Each odds call costs `markets × regions` credits.

| Setting | Default | Credits |
|---------|---------|---------|
| Region | `us` | 1 |
| Markets | `h2h,spreads,totals` | 3 |
| **Per sport scan** | | **3 credits** |

That gives you roughly **166 sport-scans/month** (~5/day). The scanner tracks remaining credits via the `x-requests-remaining` header and will stop scanning when credits drop below a configurable floor (default 50).

## Project Structure

```
SportArbitrage/
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt
├── .github/workflows/
│   └── scan.yml          # GitHub Actions cron workflow
├── src/
│   ├── config.py         # environment variables and defaults
│   ├── models.py         # pydantic data models
│   ├── normalize.py      # odds format conversion
│   ├── arb.py            # arbitrage detection + stale odds filter
│   ├── stake.py          # stake split and profit calculation
│   ├── db.py             # Postgres persistence (6 tables)
│   ├── dedup.py          # alert deduplication logic
│   ├── odds_client.py    # The Odds API client
│   ├── scanner.py        # CLI scan orchestrator
│   ├── alerts.py         # terminal + Discord notifications
│   └── dashboard.py      # Streamlit dashboard
└── tests/
    ├── conftest.py
    ├── test_odds_conversion.py
    ├── test_arb.py
    ├── test_stake.py
    ├── test_dedup.py
    └── test_alerts.py
```

## Database Schema

| Table | Purpose |
|-------|---------|
| `scan_runs` | Scan session metadata, status, and error tracking |
| `raw_snapshots` | Raw API JSON per sport fetch |
| `normalized_odds` | One row per bookmaker-outcome for historical analysis |
| `arb_opportunities` | Detected arbs with dedup keys |
| `alert_logs` | Alert dispatch history with dedup tracking |
| `paper_trade_checks` | Manual verification log |

## Arbitrage Detection

For each event and market, the scanner picks the best decimal odds per outcome across all bookmakers and checks:

```
2-way:  1/best_odds_A + 1/best_odds_B < 1
3-way:  1/best_home + 1/best_draw + 1/best_away < 1
```

If the sum is less than 1, the margin (1 − sum) is the theoretical profit percentage. The scanner then computes the optimal stake split so every outcome returns the same payout.

Bookmaker odds older than `MAX_ODDS_AGE_SECONDS` (default 5 minutes) are filtered before detection.

## Alert Deduplication

Alerts are deduplicated by a fingerprint of the event, market, line, and outcome/bookmaker combination. An alert is suppressed if:

- The same fingerprint was alerted within the last 10 minutes, **unless**
- The margin has improved by at least 0.5 percentage points

Opportunities are always stored regardless of alert status.

## Dashboard

Run `streamlit run src/dashboard.py` to open the dashboard with six tabs:

1. **Current Opportunities** — latest arbs filtered by sport, market, margin, bookmaker
2. **Alerted Opportunities** — opportunities that triggered Discord alerts
3. **Stake Calculator** — input a total stake to see the split and guaranteed profit
4. **History** — all logged opportunities with timestamps
5. **Paper Trading** — log manual verification status per opportunity
6. **Analytics** — rolling metrics: theoretical arbs, alerts sent, verified executable %, average margin, projected monthly EV

## Paper-Trading Workflow

After each scan:
1. Check the dashboard for new candidates
2. Manually verify on each sportsbook whether the odds are still available
3. Log the result in the Paper Trading tab with one of:
   `unchecked`, `real`, `stale`, `odds_changed`, `limit_issue`, `not_available`, `placed_manually`, `ignored`
4. After 2–4 weeks, check the Analytics tab

| Metric | Good sign |
|--------|-----------|
| Candidate arbs per week | 5+ |
| Real usable arbs per week | 1–3+ |
| Average margin | 1%+ |
| Verified executable % | 50%+ |

If these are weak after 4 weeks, do **not** upgrade to paid infrastructure.

## Running Tests

```bash
python -m pytest tests/ -v
```

## Swapping to a Paid API

The `OddsClient` class in `src/odds_client.py` is the only module that touches the network. To use a different data source, implement the same `get_sports()`, `get_events()`, `get_odds()` interface and swap it into `scanner.py`. The arb math, database, and dashboard are API-agnostic.

See `docs/odds_sources.md` for the current source-acquisition plan, including exchanges, prediction markets, sportsbook aggregators, and direct sportsbook risks.
