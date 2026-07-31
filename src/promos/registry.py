"""Which promo scrapers exist, keyed to the same books the odds collector uses.

Promo sources are a **parallel** registry — never mixed into
:data:`src.sources.registry.SOURCES`.  Keys match the stakeable sportsbook
identity where possible (``fanduel``, ``draftkings``, …).  Books without a
stable logged-out promo API are covered by HTML catalog adapters and TheLines
``tl_*`` tenants (same idea as Action Network ``an_*`` odds failovers).
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
from src.promos.html_catalog import HtmlCatalogPromoAdapter
from src.promos.landing import LandingPromoAdapter, LandingTarget
from src.promos.leovegas import LeoVegasPromoAdapter
from src.promos.schema import PromoKind
from src.promos.thelines import TheLinesPromoAdapter

_BETRIVERS_STATES: tuple[tuple[str, str], ...] = (
    ("il", "IL"),
    ("nj", "NJ"),
    ("pa", "PA"),
    ("co", "CO"),
    ("mi", "MI"),
    ("in", "IN"),
    ("va", "VA"),
    ("oh", "OH"),
    ("az", "AZ"),
    ("ny", "NY"),
    ("la", "LA"),
    ("md", "MD"),
    ("wv", "WV"),
)


@dataclass(frozen=True)
class PromoSourceDescriptor:
    key: str
    adapter: type
    config: Mapping[str, Any] = field(default_factory=dict)
    """Extra constructor kwargs (region, operator-specific URLs, …)."""

    def factory(self) -> Callable[..., PromoSource]:
        return partial(self.adapter, source_key=self.key, **self.config)


def _landing(
    key: str,
    targets: tuple[LandingTarget, ...],
    *,
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
    )


def _html_catalog(
    key: str,
    index_url: str,
    *,
    default_kind: PromoKind = PromoKind.SIGNUP_BONUS,
    eligible_regions: tuple[str, ...] = (),
    max_details: int = 10,
) -> PromoSourceDescriptor:
    return PromoSourceDescriptor(
        key=key,
        adapter=HtmlCatalogPromoAdapter,
        config={
            "index_url": index_url,
            "default_kind": default_kind,
            "eligible_regions": eligible_regions,
            "max_details": max_details,
        },
    )


def _thelines(key: str, brand_key: str) -> PromoSourceDescriptor:
    return PromoSourceDescriptor(
        key=key,
        adapter=TheLinesPromoAdapter,
        config={"brand_key": brand_key},
    )


def _betrivers_targets() -> tuple[LandingTarget, ...]:
    return tuple(
        LandingTarget(
            f"https://{slug}.betrivers.com/",
            f"landing-{code.lower()}",
            f"BetRivers {code}",
            region=code,
        )
        for slug, code in _BETRIVERS_STATES
    )


#: Every large sportsbook (and promo-bearing exchange) paired to the odds slate.
PROMO_SOURCES: tuple[PromoSourceDescriptor, ...] = (
    PromoSourceDescriptor(
        key="fanduel",
        adapter=FanDuelPromoAdapter,
    ),
    PromoSourceDescriptor(
        key="draftkings",
        adapter=DraftKingsPromoAdapter,
    ),
    PromoSourceDescriptor(
        key="bovada",
        adapter=BovadaPromoAdapter,
    ),
    PromoSourceDescriptor(
        key="cloudbet",
        adapter=CloudbetPromoAdapter,
    ),
    PromoSourceDescriptor(
        key="leovegas_kambi",
        adapter=LeoVegasPromoAdapter,
    ),
    # First-party HTML catalogs for US majors (TheLines remains failover).
    # Do not stamp a static nationwide footprint — eligibility comes from terms
    # / enrich so state diffs stay honest.
    _html_catalog(
        "betmgm",
        "https://sports.betmgm.com/en/blog/promotions",
    ),
    _html_catalog(
        "caesars",
        "https://www.caesars.com/sportsbook-and-casino/promotions",
    ),
    _html_catalog(
        "fanatics",
        "https://sportsbook.fanatics.com/promotions",
    ),
    _html_catalog(
        "hardrock",
        "https://www.hardrock.bet/promotions",
    ),
    _html_catalog(
        "bet365",
        "https://www.bet365.com/en/o-hub/welcome-offer",
        default_kind=PromoKind.SIGNUP_BONUS,
        max_details=4,
    ),
    # Canada / Ontario public surfaces (host itself is ON-scoped).
    _html_catalog(
        "leovegas_on",
        "https://www.leovegas.com/en-ca/promotions",
        eligible_regions=("ON",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _html_catalog(
        "betmgm_on",
        "https://www.betmgm.ca/en/promotions",
        eligible_regions=("ON",),
        default_kind=PromoKind.SIGNUP_BONUS,
    ),
    _landing(
        "betrivers_kambi",
        _betrivers_targets(),
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
        default_kind=PromoKind.OTHER,
        empty_is_ok=True,
    ),
    # TheLines aggregator failover (Action Network–style secondaries).
    _thelines("tl_fanduel", "fanduel"),
    _thelines("tl_draftkings", "draftkings"),
    _thelines("tl_betmgm", "betmgm"),
    _thelines("tl_caesars", "caesars"),
    _thelines("tl_bet365", "bet365"),
    _thelines("tl_hardrock", "hardrock"),
    _thelines("tl_fanatics", "fanatics"),
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
