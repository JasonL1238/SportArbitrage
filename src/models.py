from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Outcome(BaseModel):
    """A single priced outcome from one bookmaker."""

    name: str
    price: float  # decimal odds
    point: Optional[float] = None  # spread / total line
    source_market_id: Optional[str] = None
    source_selection_id: Optional[str] = None
    liquidity: Optional[float] = None
    status: str = "active"


class BookmakerMarket(BaseModel):
    key: str  # e.g. "h2h", "spreads", "totals"
    last_update: datetime
    outcomes: list[Outcome]


class Bookmaker(BaseModel):
    key: str
    title: str
    markets: list[BookmakerMarket]


class Event(BaseModel):
    id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: list[Bookmaker] = []


class BestOutcome(BaseModel):
    """The best available price for one outcome across all bookmakers."""

    outcome_name: str
    bookmaker_key: str
    bookmaker_title: str
    decimal_odds: float
    point: Optional[float] = None
    effective_decimal_odds: Optional[float] = None
    liquidity: Optional[float] = None


class ArbOpportunity(BaseModel):
    sport_key: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    market_key: str
    point: Optional[float] = None
    outcome_count: int
    margin: float  # positive = arb exists
    implied_prob_sum: float
    best_outcomes: list[BestOutcome]
    stakes: list[float] = []
    guaranteed_profit: float = 0.0
    total_stake: float = 0.0
    detected_at: datetime = datetime.min
    fees_applied: float = 0.0
    slippage_applied: float = 0.0
    max_executable_stake: Optional[float] = None

    @property
    def margin_pct(self) -> str:
        return f"{self.margin * 100:.2f}%"


PAPER_TRADE_STATUSES: list[str] = [
    "unchecked",
    "real",
    "stale",
    "odds_changed",
    "limit_issue",
    "not_available",
    "placed_manually",
    "ignored",
]


class PaperTrade(BaseModel):
    id: Optional[int] = None
    opportunity_id: int
    checked_at: Optional[datetime] = None
    status: str = "unchecked"
    still_available: Optional[bool] = None
    odds_at_check_json: Optional[str] = None
    would_have_profit: Optional[float] = None
    notes: str = ""


class PriceQuote(BaseModel):
    """Source-agnostic normalized market data row.

    This is the adapter boundary for sportsbooks, exchanges, and prediction
    markets.  Arbitrage logic can consume grouped quotes once event/market
    matching is confident enough.
    """

    source: str
    sport: str
    league: Optional[str] = None
    event_name: str
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    participant: Optional[str] = None
    market_type: str
    line: Optional[float] = None
    selection: str
    decimal_odds: Optional[float] = None
    price: Optional[float] = None
    implied_probability: Optional[float] = None
    timestamp: datetime
    event_start_time: Optional[datetime] = None
    source_event_id: str
    source_market_id: Optional[str] = None
    source_selection_id: Optional[str] = None
    liquidity: Optional[float] = None
    limit: Optional[float] = None
    status: str = "active"
    raw_payload_ref: Optional[str] = None
