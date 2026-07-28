"""BetRivers MLB game markets from the public Kambi offering API.

BetRivers' web sportsbook is served by Kambi, whose offering API at
``eu-offering-api.kambicdn.com`` is the unauthenticated endpoint the public
page reads from.  Two stages are needed:

1. ``listView`` enumerates the slate but carries only the headline market —
   one bet offer per event.  Collecting only this is why the previous version
   produced 32 rows for a 16-game slate and no run lines or totals at all.
2. ``betoffer/event/{ids}`` returns the full market set per event and accepts
   comma-separated ids, so the whole slate costs a couple of requests rather
   than one per game.

Two field traps live in this payload.  Odds *and lines* are returned in
thousandths: ``1640`` is 1.640 decimal and a ``line`` of ``8000`` is a total of
8.0 — dividing the odds but not the line emits totals of 8000 runs.  And
``closed`` is a cutoff *timestamp* (``"2026-07-28T22:40:00Z"``), not a boolean,
so treating it as truthy marks every open pre-match offer as suspended.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

import httpx

from src.events import build_event_key, resolve_doubleheaders
from src.normalize import decimal_to_american, implied_probability
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
    require_keys,
    require_mapping,
    require_nonempty,
)
from src.teams import canonical_team

log = logging.getLogger(__name__)

SOURCE_KEY = "betrivers_kambi"
DEFAULT_BASE_URL = "https://eu-offering-api.kambicdn.com/offering/v2018"
DEFAULT_OPERATOR = "rsiusil"
DEFAULT_MARKET = "US-IL"
MLB_PATH = "baseball/mlb/all/all"

ENDPOINT_LISTVIEW = "listview"
ENDPOINT_BETOFFER_PREFIX = "betoffer-batch-"

#: Kambi thousandths divisor for both odds and handicap lines.
THOUSANDTHS = 1000.0

#: Batch size for the per-event market call.  Small enough to keep URLs short
#: and each stored payload readable.
BATCH_SIZE = 8

#: Exact Kambi criterion label -> canonical market and period.  Matching is
#: exact by design: ``"Match Odds (Chase Burns must start)"`` is a
#: pitcher-conditional market that must not be collected as the moneyline, and
#: substring matching would collect it as one.
CRITERIA: dict[str, tuple[Market, Period]] = {
    "Moneyline": (Market.MONEYLINE, Period.FULL_GAME),
    "Run Line": (Market.RUN_LINE, Period.FULL_GAME),
    "Total Runs": (Market.TOTAL_RUNS, Period.FULL_GAME),
    "Handicap - First 5 Innings": (Market.RUN_LINE, Period.FIRST_5_INNINGS),
    "Total Runs - First 5 Innings": (Market.TOTAL_RUNS, Period.FIRST_5_INNINGS),
    "Total Runs - Inning 1": (Market.TOTAL_RUNS, Period.FIRST_1_INNING),
    "Inning 1": (Market.MONEYLINE, Period.FIRST_1_INNING),
}

#: Prefix for team totals, completed by a team name: "Total Runs by CIN Reds".
TEAM_TOTAL_PREFIX = "Total Runs by "

#: Kambi outcome type -> canonical selection, for outcomes that are not a team.
OUTCOME_TYPES: dict[str, Selection] = {
    "OT_OVER": Selection.OVER,
    "OT_UNDER": Selection.UNDER,
    "OT_CROSS": Selection.DRAW,
}


class BetRiversKambiAdapter:
    """Collects BetRivers MLB game markets via the Kambi offering API."""

    def __init__(
        self,
        operator: str = DEFAULT_OPERATOR,
        base_url: str = DEFAULT_BASE_URL,
        market: str = DEFAULT_MARKET,
        timeout: float = 20.0,
        batch_size: int = BATCH_SIZE,
        client: httpx.Client | None = None,
    ) -> None:
        self.operator = operator
        self.base_url = base_url.rstrip("/")
        self.market = market
        self.batch_size = batch_size
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def source_key(self) -> str:
        return SOURCE_KEY

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self) -> list[RawResponse]:
        listview = self._get(
            f"/{self.operator}/listView/{MLB_PATH}.json", ENDPOINT_LISTVIEW
        )
        payload = require_mapping(listview.json(), source=self.source_key, endpoint=ENDPOINT_LISTVIEW)
        require_keys(payload, ("events",), source=self.source_key, endpoint=ENDPOINT_LISTVIEW)
        events = payload["events"]
        require_nonempty(events, source=self.source_key, endpoint=ENDPOINT_LISTVIEW, what="events")

        event_ids = [
            str(entry["event"]["id"])
            for entry in events
            if isinstance(entry, dict) and (entry.get("event") or {}).get("id") is not None
        ]
        if not event_ids:
            raise FormatChangeError(
                f"{self.source_key}:{ENDPOINT_LISTVIEW}: {len(events)} events but none carried an id"
            )

        raws = [listview]
        for index, batch in enumerate(_batched(event_ids, self.batch_size), start=1):
            endpoint = f"{ENDPOINT_BETOFFER_PREFIX}{index:02d}"
            raws.append(
                self._get(
                    f"/{self.operator}/betoffer/event/{','.join(batch)}.json",
                    endpoint,
                    extra_params={"event_ids": ",".join(batch)},
                )
            )
        return raws

    def _get(
        self, path: str, endpoint: str, extra_params: dict[str, str] | None = None
    ) -> RawResponse:
        params = {"lang": "en_US", "market": self.market}
        response = self._client.get(f"{self.base_url}{path}", params=params)
        raw = RawResponse(
            source=self.source_key,
            endpoint=endpoint,
            url=str(response.request.url),
            status_code=response.status_code,
            body=response.text,
            fetched_at=datetime.now(UTC),
            content_type=response.headers.get("content-type"),
            headers=RawResponse.clean_headers(response.headers),
            request_params={**params, **(extra_params or {})},
        )
        check_http_response(
            source=self.source_key,
            endpoint=endpoint,
            status_code=raw.status_code,
            body=raw.body,
            content_type=raw.content_type,
            url=raw.url,
        )
        return raw

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        listview = next((raw for raw in raws if raw.endpoint == ENDPOINT_LISTVIEW), None)
        if listview is None:
            raise FormatChangeError(
                f"{SOURCE_KEY}: replay is missing the {ENDPOINT_LISTVIEW!r} response"
            )
        betoffers = [raw for raw in raws if raw.endpoint.startswith(ENDPOINT_BETOFFER_PREFIX)]
        if not betoffers:
            raise FormatChangeError(f"{SOURCE_KEY}: replay has no betoffer responses")
        return parse_kambi(listview, betoffers)

    def close(self) -> None:
        self._client.close()


def _batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ── parsing (pure) ───────────────────────────────────────────────────────────


def parse_kambi(listview: RawResponse, betoffers: Sequence[RawResponse]) -> ParseOutcome:
    """Join a captured listView with captured per-event market responses."""
    outcome = ParseOutcome()
    games = _accepted_games(listview.json().get("events") or [], outcome)
    if not games:
        return outcome

    event_keys = resolve_doubleheaders(
        {eid: (game["base_key"], game["commence_time"]) for eid, game in games.items()}
    )

    for raw in betoffers:
        payload = raw.json()
        for offer in payload.get("betOffers") or []:
            event_id = str(offer.get("eventId", ""))
            game = games.get(event_id)
            if game is None:
                outcome.skipped["betoffer_for_unknown_event"] += 1
                continue

            label = str((offer.get("criterion") or {}).get("englishLabel") or "")
            resolved = _resolve_criterion(label, game)
            if resolved is None:
                outcome.skipped[f"criterion:{label or 'missing'}"] += 1
                continue
            market_type, period, side = resolved

            # ``closed`` is the betting *cutoff timestamp*, not a boolean flag.
            # Reading it as truthy marks every normal pre-match offer suspended.
            offer_closes_at = _parse_time(offer.get("closed"))
            offer_id = str(offer.get("id", ""))

            for raw_outcome in offer.get("outcomes") or []:
                quote = _build_quote(
                    raw=raw,
                    raw_outcome=raw_outcome,
                    market_type=market_type,
                    period=period,
                    side=side,
                    offer_closes_at=offer_closes_at,
                    offer_id=offer_id,
                    event_id=event_id,
                    event_key=event_keys[event_id],
                    game=game,
                    label=label,
                    outcome=outcome,
                )
                if quote is not None:
                    outcome.quotes.append(quote)

    return outcome


def _accepted_games(events: list[Any], outcome: ParseOutcome) -> dict[str, dict[str, Any]]:
    games: dict[str, dict[str, Any]] = {}
    for entry in events:
        event = (entry or {}).get("event") or {}
        event_id = str(event.get("id", ""))
        if not event_id:
            outcome.skipped["event_without_id"] += 1
            continue

        home = canonical_team(event.get("homeName"))
        away = canonical_team(event.get("awayName"))
        if home is None or away is None or home.abbr == away.abbr:
            outcome.reject(
                SOURCE_KEY,
                "unknown_team",
                f"event {event_id} ({event.get('englishName')!r}) did not resolve to two MLB "
                f"clubs: home={event.get('homeName')!r} away={event.get('awayName')!r}",
                event_id=event_id,
            )
            continue

        commence_time = _parse_time(event.get("start"))
        if commence_time is None:
            outcome.reject(
                SOURCE_KEY,
                "missing_commence_time",
                f"event {event_id} has unparseable start {event.get('start')!r}",
                event_id=event_id,
            )
            continue

        games[event_id] = {
            "home": home,
            "away": away,
            "home_team": home.name,
            "away_team": away.name,
            "commence_time": commence_time,
            "base_key": build_event_key(away.abbr, home.abbr, commence_time),
            "state": event.get("state"),
        }
    return games


def _resolve_criterion(
    label: str, game: dict[str, Any]
) -> tuple[Market, Period, Side | None] | None:
    direct = CRITERIA.get(label)
    if direct is not None:
        return direct[0], direct[1], None
    if label.startswith(TEAM_TOTAL_PREFIX):
        team = canonical_team(label[len(TEAM_TOTAL_PREFIX) :])
        if team is None:
            return None
        if team.abbr == game["home"].abbr:
            return Market.TEAM_TOTAL_RUNS, Period.FULL_GAME, Side.HOME
        if team.abbr == game["away"].abbr:
            return Market.TEAM_TOTAL_RUNS, Period.FULL_GAME, Side.AWAY
    return None


def _build_quote(
    *,
    raw: RawResponse,
    raw_outcome: dict[str, Any],
    market_type: Market,
    period: Period,
    side: Side | None,
    offer_closes_at: datetime | None,
    offer_id: str,
    event_id: str,
    event_key: str,
    game: dict[str, Any],
    label: str,
    outcome: ParseOutcome,
) -> BaseballQuote | None:
    selection = _selection(raw_outcome, market_type, game)
    if selection is None:
        outcome.reject(
            SOURCE_KEY,
            "unknown_selection",
            f"outcome type {raw_outcome.get('type')!r} / participant "
            f"{raw_outcome.get('participant')!r} on {label!r} for event {event_id}",
            offer_id=offer_id,
        )
        return None

    kambi_odds = raw_outcome.get("odds")
    if kambi_odds is None:
        outcome.skipped["outcome_without_odds"] += 1
        return None

    kambi_line = raw_outcome.get("line")
    if market_type in MARKETS_REQUIRING_LINE and kambi_line is None:
        outcome.reject(
            SOURCE_KEY,
            "missing_line",
            f"{market_type} outcome without a line on {label!r} for event {event_id}",
            offer_id=offer_id,
        )
        return None
    if market_type in MARKETS_REQUIRING_SIDE and side is None:
        outcome.reject(
            SOURCE_KEY,
            "missing_side",
            f"{market_type} without a resolvable side on {label!r} for event {event_id}",
            offer_id=offer_id,
        )
        return None

    # Suspended means the source says so, or the offer's cutoff has already
    # passed as of the moment we observed it — never merely that a cutoff exists.
    suspended = str(raw_outcome.get("status") or "OPEN").upper() != "OPEN" or (
        offer_closes_at is not None and offer_closes_at <= raw.fetched_at
    )

    try:
        decimal_odds = float(kambi_odds) / THOUSANDTHS
        line = None
        if market_type in MARKETS_REQUIRING_LINE:
            line = float(kambi_line) / THOUSANDTHS + 0.0
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
            side=side,
            line=line,
            decimal_odds=decimal_odds,
            american_odds=decimal_to_american(decimal_odds),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=offer_id,
            source_selection_id=_selection_id(raw_outcome),
            status=QuoteStatus.SUSPENDED if suspended else QuoteStatus.ACTIVE,
            last_change_at=_parse_time(raw_outcome.get("changedDate")),
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            SOURCE_KEY,
            "invalid_quote",
            f"{market_type}/{selection} on event {event_id}: {exc}",
            offer_id=offer_id,
        )
        return None


def _selection(
    raw_outcome: dict[str, Any], market_type: Market, game: dict[str, Any]
) -> Selection | None:
    """Resolve a Kambi outcome to a canonical selection.

    Team sides are resolved by matching the outcome's participant to this
    event's clubs rather than by trusting ``OT_ONE``/``OT_TWO`` ordering, and
    three-way markets label their outcomes ``"1"``/``"X"``/``"2"``, so the
    participant field is the only reliable signal.
    """
    outcome_type = str(raw_outcome.get("type") or "")
    mapped = OUTCOME_TYPES.get(outcome_type)
    if mapped is not None:
        return mapped

    if outcome_type in ("OT_ONE", "OT_TWO"):
        team = canonical_team(raw_outcome.get("participant")) or canonical_team(
            raw_outcome.get("englishLabel")
        )
        if team is None:
            return None
        if team.abbr == game["home"].abbr:
            return Selection.HOME
        if team.abbr == game["away"].abbr:
            return Selection.AWAY
    return None


def _selection_id(raw_outcome: dict[str, Any]) -> str | None:
    value = raw_outcome.get("id")
    return None if value is None else str(value)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
