"""Source capability metadata for odds acquisition planning."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceCapability:
    key: str
    category: str
    official_api: bool
    websocket: bool
    auth_required: str
    sports_markets: str
    order_book: bool
    liquidity: bool
    adapter_status: str
    docs_url: str
    notes: str = ""
    priority: int = 100
    structured_fields: tuple[str, ...] = field(default_factory=tuple)


SOURCE_CATALOG: dict[str, SourceCapability] = {
    "kalshi": SourceCapability(
        key="kalshi",
        category="prediction_exchange",
        official_api=True,
        websocket=True,
        auth_required="public market data; RSA API key for trading/private endpoints",
        sports_markets="Sports events exist in Kalshi docs/search filters; availability varies by jurisdiction and listing cycle",
        order_book=True,
        liquidity=True,
        adapter_status="planned_public_market_data",
        docs_url="https://docs.kalshi.com/",
        notes="REST, WebSocket, and FIX; order books expose yes/no bid depth.",
        priority=1,
        structured_fields=("ticker", "event_ticker", "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest"),
    ),
    "polymarket": SourceCapability(
        key="polymarket",
        category="prediction_exchange",
        official_api=True,
        websocket=True,
        auth_required="public market data; wallet/L2 auth for trading and user data",
        sports_markets="Sports markets and sports metadata endpoints are documented",
        order_book=True,
        liquidity=True,
        adapter_status="planned_public_market_data",
        docs_url="https://docs.polymarket.com/",
        notes="Public CLOB market-data endpoints and public market WebSocket; sports scores have a separate WebSocket.",
        priority=2,
        structured_fields=("condition_id", "token_id", "outcome", "bid", "ask", "spread", "volume", "liquidity"),
    ),
    "betfair": SourceCapability(
        key="betfair",
        category="betting_exchange",
        official_api=True,
        websocket=True,
        auth_required="app key and session token",
        sports_markets="Broad exchange sports coverage where Betfair is available",
        order_book=True,
        liquidity=True,
        adapter_status="research_only",
        docs_url="https://docs.developer.betfair.com/",
        notes="API-NG plus Exchange Stream API; best candidate for exchange-style sports odds if account access is available.",
        priority=3,
    ),
    "matchbook": SourceCapability(
        key="matchbook",
        category="betting_exchange",
        official_api=True,
        websocket=False,
        auth_required="account session for most useful endpoints",
        sports_markets="Sports exchange markets in supported jurisdictions",
        order_book=True,
        liquidity=True,
        adapter_status="research_only",
        docs_url="https://api.matchbook.com/",
        notes="REST exchange API; confirm current public docs/access before implementing.",
        priority=4,
    ),
    "the_odds_api": SourceCapability(
        key="the_odds_api",
        category="sportsbook_aggregator",
        official_api=True,
        websocket=False,
        auth_required="API key",
        sports_markets="Broad pre-match sportsbook odds coverage",
        order_book=False,
        liquidity=False,
        adapter_status="implemented",
        docs_url="https://the-odds-api.com/liveapi/guides/v4/",
        priority=5,
    ),
    "espn_odds": SourceCapability(
        key="espn_odds",
        category="media_affiliate_public_json",
        official_api=False,
        websocket=False,
        auth_required="none",
        sports_markets="Limited public odds, often one provider",
        order_book=False,
        liquidity=False,
        adapter_status="implemented",
        docs_url="https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
        notes="Good free sanity-check source; not enough book coverage for serious arb detection.",
        priority=20,
    ),
}


def ranked_sources(category: str | None = None) -> list[SourceCapability]:
    sources = SOURCE_CATALOG.values()
    if category:
        sources = [s for s in sources if s.category == category]
    return sorted(sources, key=lambda s: s.priority)
