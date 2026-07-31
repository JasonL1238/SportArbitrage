"""Which promo scrapers exist, keyed to the same books the odds collector uses.

Promo sources are a **parallel** registry — never mixed into
:data:`src.sources.registry.SOURCES`.  Keys match the stakeable sportsbook
identity where possible (``fanduel``, ``draftkings``, …).  Books without a
stable logged-out promo API are covered by TheLines ``tl_*`` tenants (same
idea as Action Network ``an_*`` odds failovers).
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
from src.promos.leovegas import LeoVegasPromoAdapter
from src.promos.schema import PromoKind
from src.promos.thelines import TheLinesPromoAdapter


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
    empty_is_ok: bool = False,
) -> PromoSourceDescriptor:
    config: dict[str, Any] = {
        "targets": targets,
        "default_kind": default_kind,
        "empty_is_ok": empty_is_ok,
    }
    if headers:
        config["headers"] = dict(headers)
    return PromoSourceDescriptor(
        key=key,
        adapter=LandingPromoAdapter,
        config=config,
        odds_source_keys=odds_keys or (key,),
    )


def _thelines(
    key: str,
    brand_key: str,
    *,
    odds_keys: tuple[str, ...] = (),
) -> PromoSourceDescriptor:
    return PromoSourceDescriptor(
        key=key,
        adapter=TheLinesPromoAdapter,
        config={"brand_key": brand_key},
        odds_source_keys=odds_keys or (brand_key,),
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
    PromoSourceDescriptor(
        key="leovegas_kambi",
        adapter=LeoVegasPromoAdapter,
        odds_source_keys=("leovegas_kambi",),
    ),
    # US majors without a stable logged-out promo JSON from this egress —
    # covered by TheLines ``tl_*`` secondaries below (BetMGM / Caesars / bet365).
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
        empty_is_ok=True,
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
        empty_is_ok=True,
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
        empty_is_ok=True,
    ),
    # TheLines aggregator failover (Action Network–style secondaries).
    _thelines("tl_fanduel", "fanduel", odds_keys=("fanduel", "an_fanduel")),
    _thelines("tl_draftkings", "draftkings", odds_keys=("an_draftkings",)),
    _thelines("tl_betmgm", "betmgm", odds_keys=("betmgm", "an_betmgm")),
    _thelines("tl_caesars", "caesars", odds_keys=("an_caesars",)),
    _thelines("tl_bet365", "bet365", odds_keys=("an_bet365",)),
    _thelines("tl_hardrock", "hardrock", odds_keys=("hardrock", "an_hardrock")),
    _thelines("tl_fanatics", "fanatics", odds_keys=("an_fanatics",)),
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
