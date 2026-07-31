"""Which promo scrapers exist, keyed to the same books the odds collector uses.

Promo sources are a **parallel** registry — never mixed into
:data:`src.sources.registry.SOURCES`.  Keys match the stakeable sportsbook
identity where possible (``fanduel``, ``betrivers_kambi``, …).  DraftKings /
Caesars / Bet365 have no first-party odds adapter from this egress, so their
promo keys are the brand names (``draftkings``, ``caesars``, ``bet365``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Mapping

from src.promos.base import PromoSource
from src.promos.bovada import BovadaPromoAdapter
from src.promos.cloudbet import CloudbetPromoAdapter
from src.promos.draftkings import DraftKingsPromoAdapter
from src.promos.fanduel import FanDuelPromoAdapter
from src.promos.landing import LandingPromoAdapter, LandingTarget
from src.promos.schema import PromoKind


@dataclass(frozen=True)
class PromoSourceDescriptor:
    key: str
    adapter: type
    config: Mapping[str, Any] = field(default_factory=dict)
    """Extra constructor kwargs (region, operator-specific URLs, …)."""

    odds_source_keys: tuple[str, ...] = ()
    """Odds-registry keys this promo feed belongs to (for cross-linking)."""

    def factory(self) -> Callable[..., PromoSource]:
        return partial(self.adapter, source_key=self.key, **self.config)


def _landing(
    key: str,
    targets: tuple[LandingTarget, ...],
    *,
    odds_keys: tuple[str, ...] = (),
    default_kind: PromoKind = PromoKind.OTHER,
    headers: Mapping[str, str] | None = None,
) -> PromoSourceDescriptor:
    config: dict[str, Any] = {"targets": targets, "default_kind": default_kind}
    if headers:
        config["headers"] = dict(headers)
    return PromoSourceDescriptor(
        key=key,
        adapter=LandingPromoAdapter,
        config=config,
        odds_source_keys=odds_keys or (key,),
    )


#: Every large sportsbook (and promo-bearing exchange) paired to the odds slate.
PROMO_SOURCES: tuple[PromoSourceDescriptor, ...] = (
    PromoSourceDescriptor(
        key="fanduel",
        adapter=FanDuelPromoAdapter,
        odds_source_keys=("fanduel", "an_fanduel"),
    ),
    PromoSourceDescriptor(
        key="draftkings",
        adapter=DraftKingsPromoAdapter,
        odds_source_keys=("an_draftkings",),
    ),
    PromoSourceDescriptor(
        key="bovada",
        adapter=BovadaPromoAdapter,
        odds_source_keys=("bovada", "an_bovada"),
    ),
    PromoSourceDescriptor(
        key="cloudbet",
        adapter=CloudbetPromoAdapter,
        odds_source_keys=("cloudbet",),
    ),
    _landing(
        "betmgm",
        (
            LandingTarget(
                "https://www.il.betmgm.com/en/mobileportal/promotions",
                "promotions-portal",
                "BetMGM Promotions",
            ),
            LandingTarget(
                "https://www.betmgm.com/en/sports",
                "marketing-sports",
                "BetMGM Sports",
            ),
        ),
        odds_keys=("betmgm", "an_betmgm"),
        default_kind=PromoKind.SIGNUP_BONUS,
        headers={"x-bwin-accessid": "ZTg4YWEwMTgtZTlhYy00MWRkLWIzYWYtZjMzODI5ZDE0Mjc5"},
    ),
    _landing(
        "betrivers_kambi",
        (
            LandingTarget(
                "https://il.betrivers.com/",
                "landing",
                "BetRivers Illinois",
            ),
        ),
        odds_keys=("betrivers_kambi", "an_betrivers"),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "leovegas_kambi",
        (
            LandingTarget(
                "https://www.leovegas.com/en-row/promotions",
                "promotions",
                "LeoVegas Promotions",
            ),
            LandingTarget(
                "https://www.leovegas.co.uk/promotions",
                "promotions-uk",
                "LeoVegas UK Promotions",
            ),
        ),
        odds_keys=("leovegas_kambi",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "caesars",
        (
            LandingTarget(
                "https://www.caesars.com/sportsbook-and-casino",
                "sportsbook-home",
                "Caesars Sportsbook",
            ),
        ),
        odds_keys=("an_caesars",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "bet365",
        (
            LandingTarget(
                "https://www.bet365.com/",
                "home",
                "bet365",
            ),
        ),
        odds_keys=("an_bet365",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "onexbet",
        (
            LandingTarget(
                "https://1xbet.com/en/bonus",
                "bonus",
                "1xBet Bonus",
            ),
        ),
        odds_keys=("onexbet", "an_onexbet"),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "pinnacle",
        (
            LandingTarget(
                "https://www.pinnacle.com/en/",
                "home",
                "Pinnacle",
            ),
        ),
        odds_keys=("pinnacle",),
        default_kind=PromoKind.OTHER,
    ),
    _landing(
        "smarkets",
        (
            LandingTarget(
                "https://smarkets.com/en/promotions",
                "promotions",
                "Smarkets Promotions",
            ),
        ),
        odds_keys=("smarkets",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "matchbook",
        (
            LandingTarget(
                "https://www.matchbook.com/",
                "home",
                "Matchbook",
            ),
        ),
        odds_keys=("matchbook",),
        default_kind=PromoKind.OTHER,
    ),
)

BY_KEY: dict[str, PromoSourceDescriptor] = {entry.key: entry for entry in PROMO_SOURCES}


def keys() -> tuple[str, ...]:
    return tuple(entry.key for entry in PROMO_SOURCES)


def descriptor(key: str) -> PromoSourceDescriptor:
    try:
        return BY_KEY[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown promo source {key!r}; registered: {sorted(BY_KEY)}"
        ) from exc


def _check() -> None:
    seen: set[str] = set()
    for entry in PROMO_SOURCES:
        if entry.key in seen:
            raise RuntimeError(f"promo source key {entry.key!r} registered twice")
        seen.add(entry.key)


_check()


__all__ = [
    "BY_KEY",
    "PROMO_SOURCES",
    "PromoSourceDescriptor",
    "descriptor",
    "keys",
]
