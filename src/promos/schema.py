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

    summary: str = ""
    """Concrete one-line mechanics, e.g. ``Bet $5, get $150 in bonus bets``."""

    eligible_regions: list[str] = Field(default_factory=list)
    """US state / CA province codes where the offer was observed or stated."""

    ineligible_regions: list[str] = Field(default_factory=list)
    """Codes explicitly excluded by the venue copy."""

    eligibility_notes: str = ""
    """New customer, min deposit, age, opt-in, and similar constraints."""

    bonus_amount: float | None = None
    """Primary reward dollars when parseable (bonus bets, free bet, match cap)."""

    min_deposit: float | None = None
    min_odds: str | None = None
    """Minimum odds constraint as published (e.g. ``-200``, ``1.5``)."""

    wagering_requirement: str | None = None
    """Rollover / playthrough when stated (e.g. ``1x``, ``10x deposit``)."""

    reward_type: str = ""
    """``bonus_bets``, ``free_bet``, ``site_credit``, ``cash``, ``boost``, …"""

    usage_guidance: str = ""
    """Generated max-profit playbook for the dashboard detail panel."""

    is_specific: bool = False
    """True once enrichment found concrete mechanics ($/%, reward type, etc.)."""

    state_confirmed: bool = False
    """True when the collected state was affirmatively named by the offer's own copy.

    Stamped by the collector against the run's jurisdiction; the default is
    False so an unstamped row keeps the fail-closed reading.  An unconfirmed
    offer is **stored and labeled** rather than dropped — the first live run
    filtered 51 of 54 offers to no audit trail at all, and the one survivor's
    "confirmation" came from another offer's state list sharing its description
    (see docs/evidence/promos.md, 2026-08-16).  A stored False is the honest
    verdict: it shows the operator the offer *and* the fact that its copy never
    named the state, which is exactly the label-don't-withhold rule the odds
    side adopted for out-of-state prices.
    """

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("title must be non-empty")
        return text

    @field_validator("url")
    @classmethod
    def _url_empty_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("eligible_regions", "ineligible_regions")
    @classmethod
    def _normalize_regions(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            code = str(item or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.offer_id)


__all__ = ["PromoKind", "PromoOffer"]
