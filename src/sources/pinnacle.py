"""Pinnacle MLB game markets from the public guest API.

``guest.api.arcadia.pinnacle.com`` is the unauthenticated endpoint the public
odds page reads from.  Two calls are needed: ``matchups`` supplies event
identity and ``markets/straight`` supplies prices, joined locally on
``matchupId``.

Pinnacle returns far more matchups than there are games — for one MLB slate,
517 of 528 were "special" matchups (player total bases, exact-score props).
Only ``type == "matchup"`` entries are real games; the rest are counted as out
of scope.  Treating them as events is what previously produced 528 "events"
and moneyline rows whose selection was ``"5+"``.

Periods are numeric and undocumented, so the mapping below was verified against
the data rather than assumed: see :class:`src.schema.Period`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Sequence

import httpx

from src.events import build_event_key, resolve_doubleheaders
from src.normalize import american_to_decimal, implied_probability
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    BaseballQuote,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Side,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    FormatChangeError,
    check_http_response,
    require_list,
    require_nonempty,
)
from src.teams import canonical_team

log = logging.getLogger(__name__)

SOURCE_KEY = "pinnacle"
DEFAULT_BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
MLB_LEAGUE_ID = 246

ENDPOINT_MATCHUPS = "matchups"
ENDPOINT_MARKETS = "markets-straight"

#: Pinnacle ``type`` -> canonical market.
MARKET_TYPES: dict[str, Market] = {
    "moneyline": Market.MONEYLINE,
    "spread": Market.RUN_LINE,
    "total": Market.TOTAL_RUNS,
    "team_total": Market.TEAM_TOTAL_RUNS,
}

#: Pinnacle numeric period -> canonical period.  Verified empirically against
#: one slate: period 0 totals had median 8.5 (full game), period 1 had median
#: 4.5 and matched Kambi's explicitly labelled "First 5 Innings" markets, and
#: period 3 had a 0.5 total priced at -109 (~52% for at least one run) plus a
#: three-way moneyline, which is a single inning and not three.
PERIODS: dict[int, Period] = {
    0: Period.FULL_GAME,
    1: Period.FIRST_5_INNINGS,
    3: Period.FIRST_1_INNING,
}

#: Pinnacle ``designation`` -> canonical selection.
DESIGNATIONS: dict[str, Selection] = {
    "home": Selection.HOME,
    "away": Selection.AWAY,
    "draw": Selection.DRAW,
    "over": Selection.OVER,
    "under": Selection.UNDER,
}

_CLOSED_STATUSES = frozenset({"closed", "suspended", "cancelled", "canceled", "settled"})


class PinnacleAdapter:
    """Collects Pinnacle MLB game markets."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        league_id: int = MLB_LEAGUE_ID,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.league_id = league_id
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def source_key(self) -> str:
        return SOURCE_KEY

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self) -> list[RawResponse]:
        matchups = self._get(f"/leagues/{self.league_id}/matchups", ENDPOINT_MATCHUPS, "matchups")
        markets = self._get(
            f"/leagues/{self.league_id}/markets/straight", ENDPOINT_MARKETS, "markets"
        )
        return [matchups, markets]

    def _get(self, path: str, endpoint: str, what: str) -> RawResponse:
        response = self._client.get(f"{self.base_url}{path}")
        raw = RawResponse(
            source=self.source_key,
            endpoint=endpoint,
            url=str(response.request.url),
            status_code=response.status_code,
            body=response.text,
            fetched_at=datetime.now(UTC),
            content_type=response.headers.get("content-type"),
            headers=RawResponse.clean_headers(response.headers),
        )
        payload = check_http_response(
            source=self.source_key,
            endpoint=endpoint,
            status_code=raw.status_code,
            body=raw.body,
            content_type=raw.content_type,
            url=raw.url,
        )
        items = require_list(payload, source=self.source_key, endpoint=endpoint)
        require_nonempty(items, source=self.source_key, endpoint=endpoint, what=what)
        return raw

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        matchups = _find(raws, ENDPOINT_MATCHUPS)
        markets = _find(raws, ENDPOINT_MARKETS)
        if matchups is None or markets is None:
            missing = ENDPOINT_MATCHUPS if matchups is None else ENDPOINT_MARKETS
            raise FormatChangeError(f"{SOURCE_KEY}: replay is missing the {missing!r} response")
        return parse_pinnacle(matchups, markets)

    def close(self) -> None:
        self._client.close()


def _find(raws: Sequence[RawResponse], endpoint: str) -> RawResponse | None:
    matches = [raw for raw in raws if raw.endpoint == endpoint]
    return max(matches, key=lambda raw: raw.fetched_at) if matches else None


# ── parsing (pure) ───────────────────────────────────────────────────────────


def parse_pinnacle(matchups_raw: RawResponse, markets_raw: RawResponse) -> ParseOutcome:
    """Join captured matchup and market responses into normalized quotes."""
    outcome = ParseOutcome()
    matchups = matchups_raw.json()
    markets = markets_raw.json()

    games = _accepted_games(matchups, outcome)
    if not games:
        return outcome

    event_keys = resolve_doubleheaders(
        {mid: (game["base_key"], game["commence_time"]) for mid, game in games.items()}
    )

    for market in markets:
        matchup_id = str(market.get("matchupId", ""))
        game = games.get(matchup_id)
        if game is None:
            # A market on a special/prop matchup, already counted.
            continue

        market_type = MARKET_TYPES.get(str(market.get("type") or ""))
        if market_type is None:
            outcome.skipped[f"market_type:{market.get('type')}"] += 1
            continue

        period_raw = market.get("period")
        period = PERIODS.get(period_raw) if isinstance(period_raw, int) else None
        if period is None:
            # In scope but unmappable: a new period would silently become
            # full-game data if coerced, so it is a rejection, not a skip.
            outcome.reject(
                SOURCE_KEY,
                "unknown_period",
                f"period {period_raw!r} on {market_type} for matchup {matchup_id}",
                matchup_id=matchup_id,
                market_key=market.get("key"),
            )
            continue

        side = None
        if market_type in MARKETS_REQUIRING_SIDE:
            side_raw = str(market.get("side") or "")
            if side_raw not in ("home", "away"):
                outcome.reject(
                    SOURCE_KEY,
                    "missing_side",
                    f"{market_type} without a home/away side on matchup {matchup_id}",
                    matchup_id=matchup_id,
                    side=market.get("side"),
                )
                continue
            side = Side(side_raw)

        is_alternate = bool(market.get("isAlternate"))
        status = _status(game["status"], market.get("status"))
        limit_amount = _max_risk(market.get("limits") or [])
        market_id = str(market.get("key") or f"{matchup_id}:{market.get('type')}:{period_raw}")

        for price in market.get("prices") or []:
            quote = _build_quote(
                raw=markets_raw,
                price=price,
                market_type=market_type,
                period=period,
                side=side,
                is_alternate=is_alternate,
                status=status,
                limit_amount=limit_amount,
                market_id=market_id,
                matchup_id=matchup_id,
                event_key=event_keys[matchup_id],
                game=game,
                outcome=outcome,
            )
            if quote is not None:
                outcome.quotes.append(quote)

    return outcome


def _accepted_games(matchups: list[dict[str, Any]], outcome: ParseOutcome) -> dict[str, dict[str, Any]]:
    games: dict[str, dict[str, Any]] = {}
    for matchup in matchups:
        matchup_id = str(matchup.get("id", ""))
        if str(matchup.get("type")) != "matchup":
            outcome.skipped[f"matchup_type:{matchup.get('type')}"] += 1
            continue

        teams = _teams(matchup)
        if teams is None:
            outcome.reject(
                SOURCE_KEY,
                "unknown_team",
                f"matchup {matchup_id} participants did not resolve to two MLB clubs: "
                f"{[p.get('name') for p in matchup.get('participants') or []]}",
                matchup_id=matchup_id,
            )
            continue
        away, home = teams

        commence_time = _parse_time(matchup.get("startTime"))
        if commence_time is None:
            outcome.reject(
                SOURCE_KEY,
                "missing_commence_time",
                f"matchup {matchup_id} has unparseable startTime {matchup.get('startTime')!r}",
                matchup_id=matchup_id,
            )
            continue

        games[matchup_id] = {
            "home_team": home.name,
            "away_team": away.name,
            "commence_time": commence_time,
            "base_key": build_event_key(away.abbr, home.abbr, commence_time),
            "status": matchup.get("status"),
        }
    return games


def _teams(matchup: dict[str, Any]):
    resolved: dict[str, Any] = {}
    for participant in matchup.get("participants") or []:
        alignment = participant.get("alignment")
        if alignment not in ("home", "away"):
            continue
        team = canonical_team(participant.get("name"))
        if team is None:
            return None
        resolved[alignment] = team
    if "home" not in resolved or "away" not in resolved:
        return None
    if resolved["home"].abbr == resolved["away"].abbr:
        return None
    return resolved["away"], resolved["home"]


def _build_quote(
    *,
    raw: RawResponse,
    price: dict[str, Any],
    market_type: Market,
    period: Period,
    side: Side | None,
    is_alternate: bool,
    status: QuoteStatus,
    limit_amount: float | None,
    market_id: str,
    matchup_id: str,
    event_key: str,
    game: dict[str, Any],
    outcome: ParseOutcome,
) -> BaseballQuote | None:
    designation = str(price.get("designation") or "")
    selection = DESIGNATIONS.get(designation)
    if selection is None:
        outcome.reject(
            SOURCE_KEY,
            "unknown_selection",
            f"designation {designation!r} on {market_type} for matchup {matchup_id}",
            market_id=market_id,
        )
        return None

    american = price.get("price")
    if american is None:
        outcome.skipped["price_without_odds"] += 1
        return None

    points = price.get("points")
    if market_type in MARKETS_REQUIRING_LINE and points is None:
        outcome.reject(
            SOURCE_KEY,
            "missing_line",
            f"{market_type} price without points on matchup {matchup_id}",
            market_id=market_id,
        )
        return None

    try:
        decimal_odds = american_to_decimal(american)
        # -0.0 is real in Pinnacle payloads (a pick'em run line); normalize the
        # sign so a line and its mirror compare and serialize consistently.
        line = None if market_type not in MARKETS_REQUIRING_LINE else float(points) + 0.0
        return BaseballQuote(
            source=SOURCE_KEY,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            event_key=event_key,
            source_event_id=matchup_id,
            home_team=game["home_team"],
            away_team=game["away_team"],
            commence_time=game["commence_time"],
            market=market_type,
            period=period,
            selection=selection,
            side=side,
            line=line,
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=int(american),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=designation,
            limit_amount=limit_amount,
            status=status,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            SOURCE_KEY,
            "invalid_quote",
            f"{market_type}/{selection} on matchup {matchup_id}: {exc}",
            market_id=market_id,
        )
        return None


def _status(matchup_status: Any, market_status: Any) -> QuoteStatus:
    for value in (market_status, matchup_status):
        if str(value or "").lower() in _CLOSED_STATUSES:
            return QuoteStatus.SUSPENDED
    return QuoteStatus.ACTIVE


def _max_risk(limits: list[dict[str, Any]]) -> float | None:
    for limit in limits:
        if limit.get("type") == "maxRiskStake":
            try:
                return float(limit["amount"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
