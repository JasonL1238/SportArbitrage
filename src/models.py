from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Outcome(BaseModel):
    """A single priced outcome from one bookmaker."""

    name: str
    price: float  # decimal odds
    point: Optional[float] = None  # spread / total line


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
