"""Typed routing for US retail jurisdictions selected in a collection batch.

Retail sportsbooks expose the same product through state-specific public hosts,
tenant ids, and request parameters.  Those values belong here rather than in
the registry or report so collection, promotions, diagnostics, and presentation
all describe the same jurisdiction.

Routes marked ``template`` are structurally known but still need a successful
probe from that state's egress. Unavailable routes keep the source registered
without inventing an endpoint for a state where the book is not licensed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


class RouteStatus(StrEnum):
    VALIDATED = "validated"
    TEMPLATE = "template"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RetailRoute:
    source_key: str
    host: str
    config: Mapping[str, Any]
    status: RouteStatus
    routed_state: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))

    @property
    def warning(self) -> str | None:
        if self.status is RouteStatus.VALIDATED:
            return None
        if self.status is RouteStatus.TEMPLATE:
            return f"{self.source_key}: {self.routed_state} route is template-only"
        if self.status is RouteStatus.UNAVAILABLE:
            return f"{self.source_key}: no licensed route is available in {self.routed_state}"
        raise AssertionError(f"unhandled route status: {self.status}")


@dataclass(frozen=True)
class PromoRoute:
    fanduel_region: str
    betrivers_url: str | None
    betrivers_label: str


@dataclass(frozen=True)
class Jurisdiction:
    state: str
    label: str
    routes: Mapping[str, RetailRoute]
    promos: PromoRoute
    live_validated: bool
    view_only_sources: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))


_MGM_ACCESS_ID = "ZTg4YWEwMTgtZTlhYy00MWRkLWIzYWYtZjMzODI5ZDE0Mjc5"


def _route(
    source_key: str,
    url: str,
    config: Mapping[str, Any],
    status: RouteStatus,
    routed_state: str,
    detail: str = "",
) -> RetailRoute:
    return RetailRoute(
        source_key=source_key,
        host=urlsplit(url).netloc,
        config=config,
        status=status,
        routed_state=routed_state,
        detail=detail,
    )


IL = Jurisdiction(
    state="IL",
    label="Illinois",
    live_validated=True,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.il.sportsbook.fanduel.com",
            {"state": "il"},
            RouteStatus.VALIDATED,
            "IL",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiusil", "market": "US-IL", "lang": "en_US"},
            RouteStatus.VALIDATED,
            "IL",
        ),
        "betmgm": _route(
            "betmgm",
            "https://www.il.betmgm.com",
            {
                "base_url": "https://www.il.betmgm.com",
                "subdivision": "US-Illinois",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.VALIDATED,
            "IL",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.VALIDATED,
            "IL",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/il/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "IL",
            "exact Illinois route still requires matching-egress validation",
        ),
        "hardrock": _route(
            "hardrock",
            "https://api.hardrocksportsbook.com",
            {"segment": "il"},
            RouteStatus.TEMPLATE,
            "IL",
            "exact Illinois route still requires matching-egress validation",
        ),
    },
    promos=PromoRoute(
        fanduel_region="IL",
        betrivers_url="https://il.betrivers.com/",
        betrivers_label="BetRivers IL",
    ),
)


PA = Jurisdiction(
    state="PA",
    label="Pennsylvania",
    live_validated=False,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.pa.sportsbook.fanduel.com",
            {"state": "pa"},
            RouteStatus.TEMPLATE,
            "PA",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiuspa", "market": "US-PA", "lang": "en_US"},
            RouteStatus.TEMPLATE,
            "PA",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.pa.betmgm.com",
            {
                "base_url": "https://sports.pa.betmgm.com",
                "subdivision": "US-Pennsylvania",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "PA",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-PA-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-PA-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.TEMPLATE,
            "PA",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/pa/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "PA",
        ),
        # PGCB's current authorized-online list has no Hard Rock book.  Retain
        # the adapter and secondary observations globally, but never invent a
        # PA segment and call it a PA route.
        "hardrock": RetailRoute(
            source_key="hardrock",
            host="api.hardrocksportsbook.com",
            config={"segment": "nj"},
            status=RouteStatus.UNAVAILABLE,
            routed_state="PA",
            detail="Hard Rock is not a Pennsylvania online sportsbook",
        ),
    },
    promos=PromoRoute(
        fanduel_region="PA",
        betrivers_url="https://pa.betrivers.com/",
        betrivers_label="BetRivers PA",
    ),
    # Retain observations and health history, but they cannot become PA arb
    # legs because the underlying books are not licensed PA online operators.
    view_only_sources=frozenset(
        {
            "hardrock",
            "vi_hardrock",
            "an_hardrock",
            "vsin_circa",
            "an_circa",
            "an_fliff",
            "an_superbook",
            "an_bally",
        }
    ),
)


NJ = Jurisdiction(
    state="NJ",
    label="New Jersey",
    live_validated=False,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.nj.sportsbook.fanduel.com",
            {"state": "nj"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiusnj", "market": "US-NJ", "lang": "en_US"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.nj.betmgm.com",
            {
                "base_url": "https://sports.nj.betmgm.com",
                "subdivision": "US-New Jersey",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-NJ-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-NJ-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/nj/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "hardrock": _route(
            "hardrock",
            "https://api.hardrocksportsbook.com",
            {"segment": "nj"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
    },
    promos=PromoRoute(
        fanduel_region="NJ",
        betrivers_url="https://nj.betrivers.com/",
        betrivers_label="BetRivers NJ",
    ),
)


DC = Jurisdiction(
    state="DC",
    label="District of Columbia",
    live_validated=False,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.dc.sportsbook.fanduel.com",
            {"state": "dc"},
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "betrivers_kambi": RetailRoute(
            source_key="betrivers_kambi",
            host="eu-offering-api.kambicdn.com",
            config={},
            status=RouteStatus.UNAVAILABLE,
            routed_state="DC",
            detail="BetRivers is not a District of Columbia online sportsbook",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.dc.betmgm.com",
            {
                "base_url": "https://sports.dc.betmgm.com",
                "subdivision": "US-District of Columbia",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-DC-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-DC-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/dc/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "hardrock": RetailRoute(
            source_key="hardrock",
            host="api.hardrocksportsbook.com",
            config={},
            status=RouteStatus.UNAVAILABLE,
            routed_state="DC",
            detail="Hard Rock is not a District of Columbia online sportsbook",
        ),
    },
    promos=PromoRoute(
        fanduel_region="DC",
        betrivers_url=None,
        betrivers_label="BetRivers unavailable in DC",
    ),
    view_only_sources=frozenset(
        {"betrivers_kambi", "hardrock", "vi_hardrock", "an_hardrock"}
    ),
)


JURISDICTIONS: Mapping[str, Jurisdiction] = MappingProxyType(
    {"IL": IL, "PA": PA, "NJ": NJ, "DC": DC}
)


def normalize_state(value: str) -> str:
    return value.strip().upper()


def jurisdiction(state: str) -> Jurisdiction:
    key = normalize_state(state)
    try:
        return JURISDICTIONS[key]
    except KeyError:
        raise KeyError(
            f"unknown jurisdiction {state!r}; configured: {', '.join(JURISDICTIONS)}"
        ) from None


def source_host(state: str, source_key: str) -> str | None:
    route = jurisdiction(state).routes.get(source_key)
    return route.host if route is not None else None


def route_warnings(state: str) -> tuple[str, ...]:
    configured = jurisdiction(state)
    return tuple(
        warning
        for route in configured.routes.values()
        if (warning := route.warning) is not None
    )


__all__ = [
    "IL",
    "DC",
    "JURISDICTIONS",
    "NJ",
    "PA",
    "Jurisdiction",
    "PromoRoute",
    "RetailRoute",
    "RouteStatus",
    "jurisdiction",
    "normalize_state",
    "route_warnings",
    "source_host",
]
