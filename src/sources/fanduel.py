"""FanDuel MLB game markets from the public content-managed-page endpoint.

``sbapi.{state}.sportsbook.fanduel.com`` serves the same JSON the public
sportsbook page renders from.  No authentication is involved: ``_ak`` is a
static application key baked into the public page, not a user credential, and
no account, session, or geolocation workaround is used.

The page mixes real games with futures containers ("MLB Futures", "MLB Player
Awards").  Those are counted as out of scope rather than coerced into the game
schema — which is what produced market types like
``american_league_cy_young_2026`` and teams like ``MLB Futures`` before.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Sequence

import httpx

from src.events import build_event_key, resolve_doubleheaders
from src.normalize import decimal_to_american, implied_probability
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    BaseballQuote,
    Market,
    Period,
    QuoteStatus,
    Selection,
)
from src.sources.base import ParseOutcome
from src.sources.guards import check_http_response, require_keys, require_mapping, require_nonempty
from src.teams import canonical_team

log = logging.getLogger(__name__)

SOURCE_KEY = "fanduel"
DEFAULT_STATE = "il"
DEFAULT_APP_KEY = "FhMFpcPWXMeyZxOx"
MLB_PAGE_ID = "mlb"

#: FanDuel ``marketType`` -> canonical market.  Only values observed in live
#: payloads are listed; anything else is reported as out of scope instead of
#: being guessed at from its display name.
MARKET_TYPES: dict[str, tuple[Market, Period]] = {
    "MONEY_LINE": (Market.MONEYLINE, Period.FULL_GAME),
    "MATCH_HANDICAP_(2-WAY)": (Market.RUN_LINE, Period.FULL_GAME),
    "TOTAL_POINTS_(OVER/UNDER)": (Market.TOTAL_RUNS, Period.FULL_GAME),
}

#: ``result.type`` -> canonical selection.
RESULT_TYPES: dict[str, Selection] = {
    "HOME": Selection.HOME,
    "AWAY": Selection.AWAY,
    "OVER": Selection.OVER,
    "UNDER": Selection.UNDER,
    "DRAW": Selection.DRAW,
}

_ALTERNATE_PREFIX = "ALTERNATE_"


class FanDuelAdapter:
    """Collects FanDuel MLB game markets."""

    def __init__(
        self,
        state: str = DEFAULT_STATE,
        app_key: str = DEFAULT_APP_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.state = state
        self.app_key = app_key
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def source_key(self) -> str:
        return SOURCE_KEY

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self) -> list[RawResponse]:
        url = f"https://sbapi.{self.state}.sportsbook.fanduel.com/api/content-managed-page"
        params = {"page": "CUSTOM", "customPageId": MLB_PAGE_ID, "_ak": self.app_key}
        response = self._client.get(url, params=params)
        raw = RawResponse(
            source=self.source_key,
            endpoint="content-managed-page-mlb",
            url=str(response.request.url),
            status_code=response.status_code,
            body=response.text,
            fetched_at=datetime.now(UTC),
            content_type=response.headers.get("content-type"),
            headers=RawResponse.clean_headers(response.headers),
            request_params={k: v for k, v in params.items() if k != "_ak"},
        )
        # Raises on blocked / CAPTCHA / login / HTML / empty / non-JSON.
        payload = check_http_response(
            source=self.source_key,
            endpoint=raw.endpoint,
            status_code=raw.status_code,
            body=raw.body,
            content_type=raw.content_type,
            url=raw.url,
        )
        envelope = require_mapping(payload, source=self.source_key, endpoint=raw.endpoint)
        require_keys(envelope, ("attachments",), source=self.source_key, endpoint=raw.endpoint)
        attachments = require_mapping(
            envelope["attachments"], source=self.source_key, endpoint=raw.endpoint
        )
        require_keys(attachments, ("events", "markets"), source=self.source_key, endpoint=raw.endpoint)
        require_nonempty(
            attachments["events"], source=self.source_key, endpoint=raw.endpoint, what="events"
        )
        require_nonempty(
            attachments["markets"], source=self.source_key, endpoint=raw.endpoint, what="markets"
        )
        return [raw]

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        outcome = ParseOutcome()
        for raw in raws:
            outcome.extend(parse_fanduel(raw))
        return outcome

    def close(self) -> None:
        self._client.close()


# ── parsing (pure) ───────────────────────────────────────────────────────────


def parse_fanduel(raw: RawResponse) -> ParseOutcome:
    """Parse one captured FanDuel response into normalized quotes."""
    outcome = ParseOutcome()
    attachments = raw.json().get("attachments") or {}
    events: dict[str, Any] = attachments.get("events") or {}
    markets: dict[str, Any] = attachments.get("markets") or {}

    games = _accepted_games(events, outcome)
    if not games:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (game["base_key"], game["commence_time"]) for event_id, game in games.items()}
    )

    for market_id, market in markets.items():
        event_id = str(market.get("eventId", ""))
        game = games.get(event_id)
        if game is None:
            # Belongs to a futures/props container, already counted above.
            continue

        market_type_raw = str(market.get("marketType") or "")
        resolved = _resolve_market(market_type_raw)
        if resolved is None:
            outcome.skipped[f"market_type:{market_type_raw or 'missing'}"] += 1
            continue
        market_type, period, is_alternate = resolved

        market_open = str(market.get("marketStatus") or "OPEN").upper() == "OPEN"
        runners = market.get("runners") or []
        if not runners:
            outcome.reject(
                SOURCE_KEY,
                "market_without_runners",
                f"{market_type_raw} on event {event_id} has no runners",
                market_id=market_id,
            )
            continue

        for runner in runners:
            quote = _build_quote(
                raw=raw,
                runner=runner,
                market=market,
                market_id=str(market_id),
                market_type=market_type,
                period=period,
                is_alternate=is_alternate,
                market_open=market_open,
                event_id=event_id,
                event_key=event_keys[event_id],
                game=game,
                outcome=outcome,
            )
            if quote is not None:
                outcome.quotes.append(quote)

    return outcome


def _accepted_games(events: dict[str, Any], outcome: ParseOutcome) -> dict[str, dict[str, Any]]:
    """Select real two-team games, counting everything else as out of scope."""
    games: dict[str, dict[str, Any]] = {}
    for event_id, event in events.items():
        name = str(event.get("name") or "")
        teams = _teams_from_name(name)
        if teams is None:
            outcome.skipped["non_game_event"] += 1
            continue
        away, home = teams
        commence_time = _parse_time(event.get("openDate"))
        if commence_time is None:
            outcome.reject(
                SOURCE_KEY,
                "missing_commence_time",
                f"event {event_id} ({name!r}) has unparseable openDate {event.get('openDate')!r}",
                event_id=event_id,
            )
            continue
        games[str(event_id)] = {
            "home_team": home.name,
            "away_team": away.name,
            "commence_time": commence_time,
            "base_key": build_event_key(away.abbr, home.abbr, commence_time),
        }
    return games


def _teams_from_name(name: str):
    """``"Phillies (A Nola) @ Marlins (S Alcantara)"`` -> (away, home) teams.

    Returns ``None`` unless both sides resolve to distinct MLB clubs, which is
    what keeps futures containers out of the game schema.  The probable-pitcher
    suffix is dropped by :func:`src.teams.canonical_team`.
    """
    if " @ " not in name:
        return None
    away_raw, home_raw = name.split(" @ ", 1)
    away = canonical_team(away_raw)
    home = canonical_team(home_raw)
    if away is None or home is None or away.abbr == home.abbr:
        return None
    return away, home


def _resolve_market(market_type_raw: str) -> tuple[Market, Period, bool] | None:
    direct = MARKET_TYPES.get(market_type_raw)
    if direct is not None:
        return direct[0], direct[1], False
    if market_type_raw.startswith(_ALTERNATE_PREFIX):
        base = MARKET_TYPES.get(market_type_raw[len(_ALTERNATE_PREFIX) :])
        if base is not None:
            return base[0], base[1], True
    return None


def _build_quote(
    *,
    raw: RawResponse,
    runner: dict[str, Any],
    market: dict[str, Any],
    market_id: str,
    market_type: Market,
    period: Period,
    is_alternate: bool,
    market_open: bool,
    event_id: str,
    event_key: str,
    game: dict[str, Any],
    outcome: ParseOutcome,
) -> BaseballQuote | None:
    result_type = str((runner.get("result") or {}).get("type") or "")
    selection = RESULT_TYPES.get(result_type)
    if selection is None:
        outcome.reject(
            SOURCE_KEY,
            "unknown_selection",
            f"result.type {result_type!r} on {market_type} for event {event_id}",
            market_id=market_id,
            runner=runner.get("runnerName"),
        )
        return None

    odds_block = (runner.get("winRunnerOdds") or {})
    decimal_odds = ((odds_block.get("trueOdds") or {}).get("decimalOdds") or {}).get("decimalOdds")
    if decimal_odds is None:
        # A suspended runner legitimately carries no price.
        outcome.skipped["runner_without_price"] += 1
        return None

    american = (odds_block.get("americanDisplayOdds") or {}).get("americanOdds")
    line = runner.get("handicap") if market_type in MARKETS_REQUIRING_LINE else None
    runner_active = str(runner.get("runnerStatus") or "ACTIVE").upper() == "ACTIVE"

    try:
        decimal_odds = float(decimal_odds)
        return BaseballQuote(
            source=SOURCE_KEY,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            event_key=event_key,
            source_event_id=event_id,
            home_team=game["home_team"],
            away_team=game["away_team"],
            commence_time=game["commence_time"],
            market=market_type,
            period=period,
            selection=selection,
            line=None if line is None else float(line),
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=int(american) if american is not None else decimal_to_american(decimal_odds),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=_selection_id(runner),
            status=QuoteStatus.ACTIVE if (market_open and runner_active) else QuoteStatus.SUSPENDED,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            SOURCE_KEY,
            "invalid_quote",
            f"{market_type}/{selection} on event {event_id}: {exc}",
            market_id=market_id,
        )
        return None


def _selection_id(runner: dict[str, Any]) -> str | None:
    value = runner.get("selectionId")
    return None if value is None else str(value)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
