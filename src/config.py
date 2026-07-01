from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"

DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
LOCAL_DATA_DIR: Path = Path(os.getenv("LOCAL_DATA_DIR", ".local_data"))

ENABLE_ALERTS: bool = os.getenv("ENABLE_ALERTS", "true").lower() in ("true", "1", "yes")

_default_sports = "basketball_nba,baseball_mlb,americanfootball_nfl,icehockey_nhl"
TARGET_SPORTS: list[str] = [
    s.strip() for s in os.getenv("TARGET_SPORTS", _default_sports).split(",") if s.strip()
]

REGIONS: str = os.getenv("REGIONS", "us")
MARKETS: str = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "decimal"

_default_sources = "odds_api" if ODDS_API_KEY else "espn_odds"
ODDS_SOURCES: list[str] = [
    s.strip() for s in os.getenv("ODDS_SOURCES", _default_sources).split(",") if s.strip()
]

MIN_ARB_MARGIN: float = float(os.getenv("MIN_ARB_MARGIN", "0.01"))
DEFAULT_STAKE: float = float(os.getenv("DEFAULT_STAKE", "100.0"))
MAX_ODDS_AGE_SECONDS: int = int(os.getenv("MAX_ODDS_AGE_SECONDS", "300"))
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
REQUIRE_DISTINCT_BOOKS: bool = os.getenv("REQUIRE_DISTINCT_BOOKS", "true").lower() in ("true", "1", "yes")
REQUIRE_COMPLETE_OUTCOMES: bool = os.getenv("REQUIRE_COMPLETE_OUTCOMES", "true").lower() in ("true", "1", "yes")
DEFAULT_FEE_RATE: float = float(os.getenv("DEFAULT_FEE_RATE", "0.0"))
DEFAULT_SLIPPAGE_BPS: float = float(os.getenv("DEFAULT_SLIPPAGE_BPS", "0.0"))

# Backward-compat aliases used by existing code
MIN_MARGIN: float = MIN_ARB_MARGIN
DEFAULT_TOTAL_STAKE: float = DEFAULT_STAKE

CREDIT_FLOOR: int = int(os.getenv("CREDIT_FLOOR", "50"))

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
