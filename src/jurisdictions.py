"""Typed routing for the one US retail jurisdiction active in a run.

Retail sportsbooks expose the same product through state-specific public hosts,
tenant ids, and request parameters.  Those values belong here rather than in
the registry or report so collection, promotions, diagnostics, and presentation
all describe the same jurisdiction.

Routes marked ``template`` are structurally known but still need a successful
probe from that state's egress.  ``legacy`` routes intentionally preserve an
older working route until the active-state replacement is verified.  They are
reported as warnings; they are never presented as active-state proof.
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
    LEGACY = "legacy"
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
        return (
            f"{self.source_key}: using legacy {self.routed_state} routing"
            + (f" ({self.detail})" if self.detail else "")
        )


@dataclass(frozen=True)
class PromoRoute:
    fanduel_region: str
    betrivers_url: str
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
        # Kept exactly as the pre-jurisdiction implementation behaved.  These
        # warnings stay visible until an IL response parses into usable quotes.
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/nj/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.LEGACY,
            "NJ",
            "Illinois replacement has not passed the parser",
        ),
        "hardrock": _route(
            "hardrock",
            "https://api.hardrocksportsbook.com",
            {"segment": "nj"},
            RouteStatus.LEGACY,
            "NJ",
            "Illinois replacement has not passed the parser",
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


JURISDICTIONS: Mapping[str, Jurisdiction] = MappingProxyType({"IL": IL, "PA": PA})


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


def source_config(state: str, source_key: str) -> dict[str, Any]:
    route = jurisdiction(state).routes.get(source_key)
    return dict(route.config) if route is not None else {}


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
    "JURISDICTIONS",
    "PA",
    "Jurisdiction",
    "PromoRoute",
    "RetailRoute",
    "RouteStatus",
    "jurisdiction",
    "normalize_state",
    "route_warnings",
    "source_config",
    "source_host",
]
