"""Kalshi public market-data adapter.

This is intentionally read-only.  Kalshi trading, portfolio, and authenticated
order-book endpoints are out of scope for local public-source validation.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.models import PriceQuote
from src.sources.base import SourceHealth

log = logging.getLogger(__name__)

DEFAULT_KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiQuoteAdapter:
    def __init__(self, base_url: str = DEFAULT_KALSHI_URL, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._failure_count = 0

    @property
    def source_key(self) -> str:
        return "kalshi"

    def fetch_quotes(
        self,
        *,
        max_markets: int = 100,
        query: str | None = "sports",
    ) -> list[PriceQuote]:
        self._fetch_count += 1
        try:
            markets = self.fetch_markets(max_markets=max_markets, query=query)
            quotes = parse_kalshi_quotes(markets)
            self._record_success()
            return quotes
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def fetch_markets(self, *, max_markets: int, query: str | None = "sports") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(max_markets, 100), "status": "open"}
        if query:
            params["query"] = query
        resp = self._client.get(f"{self.base_url}/markets", params=params)
        resp.raise_for_status()
        payload = resp.json()
        markets = payload.get("markets", []) if isinstance(payload, dict) else []
        return [m for m in markets if m.get("status") == "active"][:max_markets]

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

    def _record_success(self) -> None:
        self._last_success = datetime.now(UTC)

    def _record_failure(self, msg: str) -> None:
        self._last_failure = datetime.now(UTC)
        self._last_error = msg
        self._failure_count += 1


def parse_kalshi_quotes(markets: list[dict[str, Any]]) -> list[PriceQuote]:
    quotes: list[PriceQuote] = []
    for market in markets:
        if market.get("status") != "active":
            continue
        timestamp = _parse_datetime(market.get("updated_time")) or datetime.now(UTC)
        event_start = _parse_datetime(
            market.get("expected_expiration_time")
            or market.get("latest_expiration_time")
            or market.get("close_time")
        )
        for selection, price_key, size_key in [
            ("YES", "yes_ask_dollars", "yes_ask_size_fp"),
            ("NO", "no_ask_dollars", "no_ask_size_fp"),
        ]:
            price = _float_or_none(market.get(price_key))
            if price is None or price <= 0 or price > 1:
                continue
            quotes.append(
                PriceQuote(
                    source="kalshi",
                    sport=_infer_sport(market),
                    league=None,
                    event_name=str(market.get("title") or market.get("ticker") or ""),
                    participant=_subtitle_for_selection(market, selection),
                    market_type="prediction_binary",
                    selection=selection,
                    decimal_odds=1 / price,
                    price=price,
                    implied_probability=price,
                    timestamp=timestamp,
                    event_start_time=event_start,
                    source_event_id=str(market.get("event_ticker") or market.get("ticker") or ""),
                    source_market_id=str(market.get("ticker") or ""),
                    source_selection_id=f"{market.get('ticker', '')}:{selection.lower()}",
                    liquidity=_float_or_none(market.get(size_key))
                    or _float_or_none(market.get("liquidity_dollars")),
                    status="active",
                    raw_payload_ref=str(market.get("ticker") or ""),
                )
            )
    return quotes


def _subtitle_for_selection(market: dict[str, Any], selection: str) -> str | None:
    key = "yes_sub_title" if selection == "YES" else "no_sub_title"
    value = market.get(key)
    return str(value) if value else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _infer_sport(market: dict[str, Any]) -> str:
    haystack = " ".join(
        str(x).lower()
        for x in [
            market.get("ticker", ""),
            market.get("event_ticker", ""),
            market.get("title", ""),
            market.get("yes_sub_title", ""),
            market.get("no_sub_title", ""),
        ]
    )
    for token, sport in [
        ("xmlb", "baseball_mlb"),
        ("mlb", "baseball_mlb"),
        ("xnfl", "americanfootball_nfl"),
        ("nfl", "americanfootball_nfl"),
        ("xnba", "basketball_nba"),
        ("nba", "basketball_nba"),
        ("xnhl", "icehockey_nhl"),
        ("nhl", "icehockey_nhl"),
        ("xwc", "soccer"),
        ("soccer", "soccer"),
    ]:
        if token in haystack:
            return sport
    return "prediction_market"
