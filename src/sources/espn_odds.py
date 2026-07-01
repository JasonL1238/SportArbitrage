"""ESPN public odds adapter.

ESPN exposes odds pages powered by DraftKings. This adapter fetches
publicly visible JSON from ESPN's odds endpoints and normalises the
data into the standard Event model.

This is a media/affiliate source — useful for fixture discovery,
sanity checks, and cross-source validation.  Per the brief, media
odds pages are rated low-to-medium risk.

The adapter is designed to work with replay fixtures for offline
testing before any live polling is enabled.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.models import Bookmaker, BookmakerMarket, Event, Outcome
from src.sources.base import SourceHealth, SourceSnapshot

log = logging.getLogger(__name__)

_SPORT_PATHS: dict[str, tuple[str, str]] = {
    "basketball_nba": ("basketball", "nba"),
    "americanfootball_nfl": ("football", "nfl"),
    "baseball_mlb": ("baseball", "mlb"),
    "icehockey_nhl": ("hockey", "nhl"),
}

_EVENTS_URLS: dict[str, str] = {
    key: f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
    for key, (sport, league) in _SPORT_PATHS.items()
}

_SUMMARY_URLS: dict[str, str] = {
    key: f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
    for key, (sport, league) in _SPORT_PATHS.items()
}

PARSER_VERSION = "1"


class EspnOddsAdapter:
    """Adapter for ESPN's publicly visible odds JSON endpoints."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=15.0)
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._failure_count = 0

    @property
    def source_key(self) -> str:
        return "espn_odds"

    def discover_events(self, sport_key: str) -> list[dict[str, Any]]:
        url = _EVENTS_URLS.get(sport_key)
        if not url:
            return []
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            events = [e for e in data.get("events", []) if _is_upcoming_event(e)]
            for event in events:
                event["_sport_key"] = sport_key
            self._record_success()
            return events
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def fetch_odds(self, sport_key: str) -> SourceSnapshot:
        events_url = _EVENTS_URLS.get(sport_key)
        summary_url = _SUMMARY_URLS.get(sport_key)
        if not events_url or not summary_url:
            return SourceSnapshot(
                source_key=self.source_key,
                sport_key=sport_key,
                raw_payload="[]",
                fetched_at=datetime.now(UTC),
                parse_error=f"No ESPN odds URL configured for {sport_key}",
            )

        self._fetch_count += 1
        try:
            resp = self._client.get(events_url)
            resp.raise_for_status()
            data = resp.json()
            events = [e for e in data.get("events", []) if _is_upcoming_event(e)]

            for event in events:
                event["_sport_key"] = sport_key
                event_id = event.get("id")
                if not event_id:
                    continue
                summary_resp = self._client.get(summary_url, params={"event": event_id})
                summary_resp.raise_for_status()
                summary = summary_resp.json()
                odds_payload = summary.get("pickcenter") or summary.get("odds") or []
                if isinstance(odds_payload, dict):
                    odds_payload = [odds_payload]
                if not odds_payload:
                    continue
                comps = event.get("competitions") or []
                if comps:
                    comps[0]["odds"] = odds_payload

            payload = json.dumps(events)
            self._record_success()
            return SourceSnapshot(
                source_key=self.source_key,
                sport_key=sport_key,
                raw_payload=payload,
                fetched_at=datetime.now(UTC),
                url=events_url,
                status_code=resp.status_code,
                content_type=resp.headers.get("content-type"),
                parser_version=PARSER_VERSION,
            )
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def parse_events(self, raw: list[dict[str, Any]]) -> list[Event]:
        """Parse ESPN odds JSON into normalised Event models.

        ESPN's odds endpoint returns a list of event objects, each
        containing competitions with odds data.
        """
        return _parse_espn_odds(raw)

    def healthcheck(self) -> SourceHealth:
        return SourceHealth(
            source_key=self.source_key,
            is_healthy=self._last_failure is None or (
                self._last_success is not None
                and self._last_success > self._last_failure
            ),
            last_success=self._last_success,
            last_failure=self._last_failure,
            error_message=self._last_error,
            fetch_count=self._fetch_count,
            failure_count=self._failure_count,
        )

    def supports_delta(self) -> bool:
        return False

    def confidence_score(self) -> float:
        return 0.70

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EspnOddsAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _record_success(self) -> None:
        self._last_success = datetime.now(UTC)

    def _record_failure(self, msg: str) -> None:
        self._last_failure = datetime.now(UTC)
        self._last_error = msg
        self._failure_count += 1


def _parse_espn_odds(raw: list[dict[str, Any]]) -> list[Event]:
    """Parse ESPN-format odds data into Event models.

    ESPN odds JSON has the structure:
    [
      {
        "id": "...",
        "competitions": [
          {
            "id": "...",
            "competitors": [...],
            "odds": [
              {
                "provider": {"name": "DraftKings"},
                "homeTeamOdds": {"moneyLine": -150, "spreadOdds": -110, "spread": "-3.5"},
                "awayTeamOdds": {"moneyLine": +130, "spreadOdds": -110, "spread": "+3.5"},
                "overUnder": 215.5,
                "overOdds": -110,
                "underOdds": -110,
              }
            ]
          }
        ]
      }
    ]
    """
    events: list[Event] = []
    now = datetime.now(UTC)

    for item in raw:
        comps = item.get("competitions", [])
        if not comps:
            continue

        comp = comps[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

        home_name = home.get("team", {}).get("displayName", home.get("team", {}).get("name", "Home"))
        away_name = away.get("team", {}).get("displayName", away.get("team", {}).get("name", "Away"))

        sport_key = _extract_sport_key(item)
        commence_str = item.get("date", comp.get("date", ""))
        try:
            commence_time = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            commence_time = now

        odds_list = comp.get("odds", [])
        bookmakers: list[Bookmaker] = []

        for odds_entry in odds_list:
            provider = odds_entry.get("provider", {})
            provider_name = provider.get("name", "Unknown")
            provider_key = provider_name.lower().replace(" ", "_")

            markets: list[BookmakerMarket] = []

            h2h_outcomes = _parse_moneyline(odds_entry, home_name, away_name)
            if h2h_outcomes:
                markets.append(BookmakerMarket(key="h2h", last_update=now, outcomes=h2h_outcomes))

            spread_outcomes = _parse_spread(odds_entry, home_name, away_name)
            if spread_outcomes:
                markets.append(BookmakerMarket(key="spreads", last_update=now, outcomes=spread_outcomes))

            total_outcomes = _parse_totals(odds_entry)
            if total_outcomes:
                markets.append(BookmakerMarket(key="totals", last_update=now, outcomes=total_outcomes))

            if markets:
                bookmakers.append(Bookmaker(key=provider_key, title=provider_name, markets=markets))

        events.append(Event(
            id=str(item.get("id", comp.get("id", ""))),
            sport_key=sport_key,
            sport_title=item.get("shortName", sport_key),
            commence_time=commence_time,
            home_team=home_name,
            away_team=away_name,
            bookmakers=bookmakers,
        ))

    return events


def _is_upcoming_event(item: dict[str, Any]) -> bool:
    comps = item.get("competitions") or []
    if not comps:
        return False

    status_type = (comps[0].get("status") or {}).get("type") or {}
    if status_type.get("completed") is True:
        return False

    commence_str = item.get("date", comps[0].get("date", ""))
    try:
        commence_time = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return commence_time > datetime.now(UTC)


def _extract_sport_key(item: dict[str, Any]) -> str:
    if item.get("_sport_key"):
        return str(item["_sport_key"])

    league = item.get("league", {})
    slug = league.get("slug", "")
    if "nba" in slug.lower():
        return "basketball_nba"
    if "nfl" in slug.lower():
        return "americanfootball_nfl"
    if "mlb" in slug.lower():
        return "baseball_mlb"
    if "nhl" in slug.lower():
        return "icehockey_nhl"
    return slug or "unknown"


def _american_to_decimal(odds: int | float) -> float:
    odds = float(odds)
    if odds > 0:
        return 1 + odds / 100
    return 1 + 100 / abs(odds)


def _parse_moneyline(odds: dict, home_name: str, away_name: str) -> list[Outcome]:
    results: list[Outcome] = []
    home_ml = odds.get("homeTeamOdds", {}).get("moneyLine")
    away_ml = odds.get("awayTeamOdds", {}).get("moneyLine")
    if home_ml is not None and away_ml is not None:
        try:
            results.append(Outcome(name=home_name, price=_american_to_decimal(home_ml)))
            results.append(Outcome(name=away_name, price=_american_to_decimal(away_ml)))
        except (ValueError, ZeroDivisionError):
            pass
    return results


def _parse_spread(odds: dict, home_name: str, away_name: str) -> list[Outcome]:
    results: list[Outcome] = []
    home_spread = odds.get("homeTeamOdds", {}).get("spread")
    away_spread = odds.get("awayTeamOdds", {}).get("spread")
    home_spread_odds = odds.get("homeTeamOdds", {}).get("spreadOdds")
    away_spread_odds = odds.get("awayTeamOdds", {}).get("spreadOdds")

    if all(v is not None for v in [home_spread, away_spread, home_spread_odds, away_spread_odds]):
        try:
            results.append(Outcome(
                name=home_name, price=_american_to_decimal(home_spread_odds),
                point=float(home_spread),
            ))
            results.append(Outcome(
                name=away_name, price=_american_to_decimal(away_spread_odds),
                point=float(away_spread),
            ))
        except (ValueError, ZeroDivisionError):
            pass
    return results


def _parse_totals(odds: dict) -> list[Outcome]:
    results: list[Outcome] = []
    ou = odds.get("overUnder")
    over_odds = odds.get("overOdds")
    under_odds = odds.get("underOdds")

    if all(v is not None for v in [ou, over_odds, under_odds]):
        try:
            point = float(ou)
            results.append(Outcome(name="Over", price=_american_to_decimal(over_odds), point=point))
            results.append(Outcome(name="Under", price=_american_to_decimal(under_odds), point=point))
        except (ValueError, ZeroDivisionError):
            pass
    return results
