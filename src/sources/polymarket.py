"""Polymarket public CLOB market-data adapter.

This adapter is read-only and uses public market-data endpoints.  Trading and
user data require wallet/API authentication and are intentionally out of scope.
"""
from __future__ import annotations

import logging
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from src.models import PriceQuote
from src.sources.base import SourceHealth

log = logging.getLogger(__name__)

DEFAULT_CLOB_URL = "https://clob.polymarket.com"
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"


class PolymarketQuoteAdapter:
    def __init__(self, base_url: str = DEFAULT_CLOB_URL, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.gamma_url = DEFAULT_GAMMA_URL
        self._client = httpx.Client(timeout=timeout)
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._failure_count = 0

    @property
    def source_key(self) -> str:
        return "polymarket"

    def fetch_quotes(
        self,
        *,
        max_markets: int = 100,
        query: str | None = None,
    ) -> list[PriceQuote]:
        """Fetch active Polymarket markets and top-of-book quotes."""
        self._fetch_count += 1
        try:
            markets = self._fetch_markets(max_markets=max_markets, query=query)
            token_ids = [
                str(token["token_id"])
                for market in markets
                for token in market.get("tokens", [])
                if token.get("token_id")
            ]
            books = self._fetch_books(token_ids)
            quotes = parse_polymarket_quotes(markets, books)
            self._record_success()
            return quotes
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def fetch_sports_quotes(self, *, max_events: int = 25) -> list[PriceQuote]:
        """Fetch sports-tagged Polymarket quotes from the public Gamma API."""
        self._fetch_count += 1
        try:
            events = self.fetch_sports_events(max_events=max_events)
            quotes = parse_polymarket_gamma_quotes(events)
            self._record_success()
            return quotes
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def fetch_sports_events(self, *, max_events: int = 25) -> list[dict[str, Any]]:
        resp = self._client.get(
            f"{self.gamma_url}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": max_events,
                "tag_slug": "sports",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            return []
        return payload[:max_events]

    def healthcheck(self) -> SourceHealth:
        return SourceHealth(
            source_key=self.source_key,
            is_healthy=self._last_failure is None or (
                self._last_success is not None and self._last_success > self._last_failure
            ),
            last_success=self._last_success,
            last_failure=self._last_failure,
            error_message=self._last_error,
            fetch_count=self._fetch_count,
            failure_count=self._failure_count,
        )

    def close(self) -> None:
        self._client.close()

    def _fetch_markets(self, *, max_markets: int, query: str | None) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(markets) < max_markets:
            params: dict[str, Any] = {}
            if cursor:
                params["next_cursor"] = cursor
            if query:
                params["q"] = query
            resp = self._client.get(f"{self.base_url}/sampling-markets", params=params)
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            for market in data:
                if market.get("active") and not market.get("closed") and market.get("accepting_orders", True):
                    markets.append(market)
                if len(markets) >= max_markets:
                    break
            cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
            if not cursor:
                break
        return markets

    def _fetch_books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        books: list[dict[str, Any]] = []
        for i in range(0, len(token_ids), 100):
            batch = token_ids[i:i + 100]
            if not batch:
                continue
            resp = self._client.post(
                f"{self.base_url}/books",
                json=[{"token_id": token_id} for token_id in batch],
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list):
                books.extend(payload)
        return books

    def _record_success(self) -> None:
        self._last_success = datetime.now(UTC)

    def _record_failure(self, msg: str) -> None:
        self._last_failure = datetime.now(UTC)
        self._last_error = msg
        self._failure_count += 1


def parse_polymarket_quotes(
    markets: list[dict[str, Any]],
    books: list[dict[str, Any]],
) -> list[PriceQuote]:
    markets_by_token: dict[str, dict[str, Any]] = {}
    for market in markets:
        for token in market.get("tokens", []):
            token_id = str(token.get("token_id", ""))
            if token_id:
                markets_by_token[token_id] = market

    quotes: list[PriceQuote] = []
    for book in books:
        token_id = str(book.get("asset_id") or "")
        if not token_id:
            continue
        market = markets_by_token.get(token_id, {})
        token = _token_for_id(market, token_id)
        best_ask = _best_level(book.get("asks", []), best_low=True)
        if best_ask is None:
            continue

        ask_price = best_ask[0]
        if ask_price <= 0:
            continue

        timestamp = _parse_book_timestamp(book.get("timestamp"))
        condition_id = str(market.get("condition_id") or book.get("market") or "")
        question = str(market.get("question") or market.get("market_slug") or condition_id)
        event_start = _parse_datetime(market.get("game_start_time") or market.get("end_date_iso"))

        quotes.append(
            PriceQuote(
                source="polymarket",
                sport=_infer_sport(market),
                league=None,
                event_name=question,
                market_type="prediction_binary",
                selection=str(token.get("outcome") or token_id),
                decimal_odds=1 / ask_price,
                price=ask_price,
                implied_probability=ask_price,
                timestamp=timestamp,
                event_start_time=event_start,
                source_event_id=str(market.get("event_id") or market.get("event_slug") or condition_id),
                source_market_id=condition_id,
                source_selection_id=token_id,
                liquidity=best_ask[1],
                status="active" if market.get("active", True) and not market.get("closed", False) else "inactive",
                raw_payload_ref=condition_id,
            )
        )
    return quotes


def parse_polymarket_gamma_quotes(events: list[dict[str, Any]]) -> list[PriceQuote]:
    """Parse public Gamma sports event payloads into normalized quotes.

    Gamma carries event/tag metadata and lightweight market prices.  Full CLOB
    depth remains available through ``parse_polymarket_quotes`` when token IDs
    are sent to the CLOB book endpoints.
    """
    quotes: list[PriceQuote] = []
    for event in events:
        if not event.get("active", True) or event.get("closed", False):
            continue
        event_name = str(event.get("title") or event.get("slug") or event.get("id") or "")
        event_start = _parse_datetime(event.get("endDate") or event.get("endDateIso"))
        for market in event.get("markets", []) or []:
            if not market.get("active", True) or market.get("closed", False):
                continue
            if market.get("acceptingOrders") is False:
                continue
            outcomes = _json_list(market.get("outcomes"))
            prices = _json_list(market.get("outcomePrices"))
            token_ids = _json_list(market.get("clobTokenIds"))
            if len(outcomes) != len(prices):
                continue
            timestamp = _parse_datetime(market.get("updatedAt") or event.get("updatedAt")) or datetime.now(UTC)
            market_id = str(market.get("conditionId") or market.get("id") or "")
            for idx, outcome in enumerate(outcomes):
                price = _float_or_none(prices[idx])
                if price is None or price <= 0 or price > 1:
                    continue
                quotes.append(
                    PriceQuote(
                        source="polymarket",
                        sport=_infer_sport({**event, **market}),
                        league=None,
                        event_name=event_name,
                        market_type="prediction_binary",
                        selection=str(outcome),
                        decimal_odds=1 / price,
                        price=price,
                        implied_probability=price,
                        timestamp=timestamp,
                        event_start_time=event_start or _parse_datetime(market.get("endDate") or market.get("endDateIso")),
                        source_event_id=str(event.get("id") or event.get("slug") or market_id),
                        source_market_id=market_id,
                        source_selection_id=str(token_ids[idx]) if idx < len(token_ids) else None,
                        liquidity=_float_or_none(market.get("liquidityNum") or market.get("liquidity")),
                        status="active",
                        raw_payload_ref=market_id,
                    )
                )
    return quotes


def _token_for_id(market: dict[str, Any], token_id: str) -> dict[str, Any]:
    for token in market.get("tokens", []):
        if str(token.get("token_id")) == token_id:
            return token
    return {}


def _best_level(levels: list[dict[str, Any]], *, best_low: bool) -> tuple[float, float] | None:
    parsed: list[tuple[float, float]] = []
    for level in levels:
        try:
            parsed.append((float(level["price"]), float(level.get("size", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed:
        return None
    return min(parsed, key=lambda x: x[0]) if best_low else max(parsed, key=lambda x: x[0])


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_book_timestamp(value: Any) -> datetime:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, tz=UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _infer_sport(market: dict[str, Any]) -> str:
    tags = []
    for tag in market.get("tags", []) or []:
        if isinstance(tag, dict):
            tags.extend([str(tag.get("label", "")), str(tag.get("slug", ""))])
        else:
            tags.append(str(tag))
    haystack = " ".join(str(x).lower() for x in [
        market.get("question", ""),
        market.get("title", ""),
        market.get("market_slug", ""),
        market.get("slug", ""),
        market.get("event_slug", ""),
        " ".join(tags),
    ])
    for token, sport in [
        ("nba", "basketball_nba"),
        ("wnba", "basketball_wnba"),
        ("nfl", "americanfootball_nfl"),
        ("mlb", "baseball_mlb"),
        ("nhl", "icehockey_nhl"),
        ("soccer", "soccer"),
        ("tennis", "tennis"),
    ]:
        if token in haystack:
            return sport
    return "prediction_market"
