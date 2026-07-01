from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from src import config
from src.models import Bookmaker, BookmakerMarket, Event, Outcome

log = logging.getLogger(__name__)

RATE_LIMIT_SLEEP: float = 1.5  # seconds between paid calls


@dataclass
class CreditTracker:
    """Tracks remaining API credits via response headers."""

    remaining: int | None = None
    used: int | None = None
    last_checked: datetime | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def update(self, headers: httpx.Headers) -> None:
        remaining = headers.get("x-requests-remaining")
        used = headers.get("x-requests-used")
        if remaining is not None:
            self.remaining = int(remaining)
        if used is not None:
            self.used = int(used)
        self.last_checked = datetime.now(UTC)
        self.history.append({"remaining": self.remaining, "used": self.used, "at": self.last_checked.isoformat()})

    @property
    def above_floor(self) -> bool:
        if self.remaining is None:
            return True
        return self.remaining >= config.CREDIT_FLOOR


class OddsClient:
    """Client for The Odds API v4 free tier."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or config.ODDS_API_KEY
        self.base_url = (base_url or config.ODDS_API_BASE_URL).rstrip("/")
        self.credits = CreditTracker()
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OddsClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        params = params or {}
        params["apiKey"] = self.api_key
        url = f"{self.base_url}{path}"
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        self.credits.update(resp.headers)
        return resp

    # ── Free endpoints ───────────────────────────────────────────────────

    def get_sports(self) -> list[dict[str, Any]]:
        """List in-season sports (free, no credit cost)."""
        resp = self._get("/sports/")
        return resp.json()

    def get_events(self, sport_key: str) -> list[dict[str, Any]]:
        """List upcoming events for a sport (free, no credit cost)."""
        resp = self._get(f"/sports/{sport_key}/events/")
        return resp.json()

    # ── Paid endpoint ────────────────────────────────────────────────────

    def get_odds(
        self,
        sport_key: str,
        regions: str | None = None,
        markets: str | None = None,
        odds_format: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Fetch odds for a sport.

        Returns ``(events_json, credits_remaining)``.
        Cost: ``len(markets.split(',')) * len(regions.split(','))`` credits.
        """
        if not self.credits.above_floor:
            log.warning(
                "Credits remaining (%s) below floor (%s) — skipping odds call for %s",
                self.credits.remaining, config.CREDIT_FLOOR, sport_key,
            )
            return [], self.credits.remaining

        params: dict[str, str] = {
            "regions": regions or config.REGIONS,
            "markets": markets or config.MARKETS,
            "oddsFormat": odds_format or config.ODDS_FORMAT,
        }

        time.sleep(RATE_LIMIT_SLEEP)
        resp = self._get(f"/sports/{sport_key}/odds/", params)
        data = resp.json()

        log.info(
            "Fetched odds for %s: %d events, credits remaining: %s",
            sport_key, len(data), self.credits.remaining,
        )
        return data, self.credits.remaining

    # ── Parsing helpers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def parse_events(raw: list[dict[str, Any]]) -> list[Event]:
        """Parse raw API JSON into typed Event models."""
        events: list[Event] = []
        for item in raw:
            bookmakers: list[Bookmaker] = []
            for bm in item.get("bookmakers", []):
                mkts: list[BookmakerMarket] = []
                for m in bm.get("markets", []):
                    outcomes: list[Outcome] = []
                    for o in m.get("outcomes", []):
                        try:
                            outcomes.append(
                                Outcome(
                                    name=o["name"],
                                    price=float(o["price"]),
                                    point=float(o["point"]) if o.get("point") is not None else None,
                                )
                            )
                        except (KeyError, TypeError, ValueError):
                            log.warning("Skipping malformed outcome in event %s: %s", item.get("id"), o)
                    if not outcomes:
                        continue
                    mkts.append(
                        BookmakerMarket(
                            key=m["key"],
                            last_update=OddsClient._parse_datetime(m["last_update"]),
                            outcomes=outcomes,
                        )
                    )
                if not mkts:
                    continue
                bookmakers.append(Bookmaker(key=bm["key"], title=bm["title"], markets=mkts))

            events.append(
                Event(
                    id=item["id"],
                    sport_key=item["sport_key"],
                    sport_title=item["sport_title"],
                    commence_time=OddsClient._parse_datetime(item["commence_time"]),
                    home_team=item["home_team"],
                    away_team=item["away_team"],
                    bookmakers=bookmakers,
                )
            )
        return events
