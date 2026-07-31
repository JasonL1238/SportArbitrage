"""Normalized shape for sportsbook promotions and free-EV offers.

Deliberately separate from :class:`src.schema.Quote`.  A bonus bet, deposit
match, or odds boost is not a priced selection on a game market — folding it
into the odds schema is how phantom arbs get invented.  Downstream consumers
(alerts, dashboards) join on :attr:`PromoOffer.source`, which matches the odds
registry key where the book is the same counterparty.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PromoKind(StrEnum):
    """Coarse bucket for free-EV / bonus surfaces.

    Kept small and closed.  Unknown venue labels map through the adapter into
    one of these, or land as :attr:`OTHER` with the raw label preserved on
    :attr:`PromoOffer.raw_kind`.
    """

    SIGNUP_BONUS = "signup_bonus"
    DEPOSIT_MATCH = "deposit_match"
    BONUS_BET = "bonus_bet"
    FREE_BET = "free_bet"
    NO_SWEAT = "no_sweat"
    ODDS_BOOST = "odds_boost"
    PROFIT_BOOST = "profit_boost"
    PARLAY_BOOST = "parlay_boost"
    REFERRAL = "referral"
    LOYALTY = "loyalty"
    RISK_FREE = "risk_free"
    OTHER = "other"


class PromoOffer(BaseModel):
    """One publicly advertised promotion from one sportsbook."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)

    source: str
    """Registry key, e.g. ``fanduel`` or ``draftkings``."""

    offer_id: str
    """Stable-enough id within the source (venue id, slug, or content hash)."""

    kind: PromoKind
    title: str
    description: str = ""
    terms: str = ""
    """Truncated terms when the venue publishes them without login."""

    url: str | None = None
    """Public page a human can open to claim or read the offer."""

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    observed_at: datetime
    raw_ref: str = ""
    """Pointer into the raw-store envelope that produced this row."""

    raw_kind: str = ""
    """Venue's own category/label before mapping into :class:`PromoKind`."""

    product: str = ""
    """``sportsbook``, ``casino``, ``racing``, … when the venue says."""

    requires_login: bool = False
    """True when the public catalog says details need an account."""

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("title must be non-empty")
        return text

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.offer_id)


__all__ = ["PromoKind", "PromoOffer"]
