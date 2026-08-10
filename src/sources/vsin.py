"""Circa prices from VSiN's public Las Vegas line tracker.

Circa's own betting-menu page points readers to VSiN as an odds aggregator
while reserving its complete real-time menu for the mobile app.  This adapter
therefore supplies an independent, named Circa observation path; it is a
republisher, not a second counterparty.  It is also Circa's **only** feed:
``an_circa`` was deregistered on 2026-08-09 after Action Network's id 78
returned zero rows in every league on both endpoint versions, so there is no
redundancy pair to declare and no state licence behind these numbers — VSiN
tracks the Las Vegas board, which is why ``coverage`` may only ever count it
as ``OTHER_LICENCE`` context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import american_to_decimal, implied_probability
from src.participants import Participant, canonical_participant
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection
from src.sources._common import (
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    refuse_mid_move_pairings,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

SOURCE_KEY = "vsin_circa"
BASE_URL = "https://data.vsin.com"
HOST_INTERVAL = 1.0

ROUTES: Mapping[str, str] = {
    "MLB": "MLB",
    "NFL": "NFL",
    "NBA": "NBA",
    "NHL": "NHL",
    "WNBA": "WNBA",
}
MARKETS = frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL})
_EVENT_ID = re.compile(r"openGameDetail\('([^']+)'\)")
_CLOCK = re.compile(r"(\d{1,2}:\d{2}\s+[AP]M)\s+ET", re.IGNORECASE)
_PAGE_DATE = re.compile(r"[A-Z][a-z]{2},\s+([A-Z][a-z]{2})\s+(\d{1,2})")
_AMERICAN = re.compile(r"^[+-]\d+$")
_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _Fixture:
    event_id: str
    competition: Any
    home: Participant
    away: Participant
    commence_time: datetime
    base_key: str


class VsinCircaAdapter:
    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        wanted = tuple(leagues) if leagues is not None else tuple(ROUTES)
        self._leagues = tuple(league for league in wanted if league in ROUTES)
        if not self._leagues:
            raise ValueError(f"VsinCircaAdapter has no supported league in {wanted!r}")
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=HOST_INTERVAL
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from({league: MARKETS for league in self.leagues}, self.leagues)

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for league in self.leagues:
            tally.requested(league)
            try:
                raw = self._http.get(
                    f"{self.base_url}/vegas-odds-linetracker/",
                    endpoint=f"odds-{league.lower()}-circa",
                    params={"sportid": ROUTES[league]},
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    expect_json=False,
                )
            except SourceError as exc:
                tally.failed(league, exc)
                continue
            raws.append(raw)
            tally.produced(league, 1)
        tally.require_something(what="Circa line-tracker page")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_vsin_circa(raws)

    def close(self) -> None:
        self._http.close()


def parse_vsin_circa(raws: Sequence[RawResponse]) -> ParseOutcome:
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    for raw in latest_per_endpoint(raws):
        parts = raw.endpoint.split("-")
        if len(parts) != 3 or parts[0] != "odds" or parts[2] != "circa":
            continue
        league_key = parts[1].upper()
        if league_key not in ROUTES:
            raise FormatChangeError(f"{source}:{raw.endpoint}: unknown route metadata")
        competition = league_registry.league(league_key)
        soup = BeautifulSoup(raw.body, "html.parser")
        table = soup.select_one("table.sp-table")
        body = table.select_one("tbody.sp-tbody-fg") if table else None
        if table is None or body is None:
            raise FormatChangeError(f"{source}:{raw.endpoint}: no full-game line table")
        if "Circa" not in [cell.get_text(" ", strip=True) for cell in table.select("thead th")]:
            raise FormatChangeError(f"{source}:{raw.endpoint}: no named Circa column")
        page_date = _page_date(table, raw)
        fixtures: dict[str, _Fixture] = {}
        pending: list[tuple[str, Market, Selection, float | None, int]] = []
        rows = body.find_all("tr", recursive=False)
        index = 0
        while index + 2 < len(rows):
            opener = rows[index]
            if "sp-row-open" not in (opener.get("class") or []):
                index += 1
                continue
            away_row, home_row = rows[index + 1], rows[index + 2]
            event_id = _event_id(opener)
            commence = _commence_time(opener, page_date)
            away_name = _team_name(away_row)
            home_name = _team_name(home_row)
            if not event_id or commence is None or not away_name or not home_name:
                outcome.skipped["incomplete_event_header"] += 1
                index += 3
                continue
            if commence <= raw.fetched_at:
                outcome.skipped["event_already_started"] += 1
                index += 3
                continue
            away = canonical_participant(away_name, competition)
            home = canonical_participant(home_name, competition)
            if away is None or home is None or away.key == home.key:
                outcome.reject(
                    source,
                    "unresolved_participants",
                    f"{event_id}: {away_name!r}/{home_name!r}",
                )
                index += 3
                continue
            away_side, home_side = orient(away, home, competition, home=home)
            fixture = _Fixture(
                event_id,
                competition,
                home_side,
                away_side,
                commence,
                build_event_key(away_side.key, home_side.key, commence, competition),
            )
            fixtures[event_id] = fixture
            for side_index, row in enumerate((away_row, home_row)):
                cells = row.select("td.sp-book-set-a")[:3]
                if len(cells) != 3:
                    outcome.reject(source, "missing_circa_columns", event_id)
                    continue
                for market, cell in zip(
                    (Market.SPREAD, Market.MONEYLINE, Market.TOTAL), cells, strict=True
                ):
                    parsed = _price(cell, market, side_index)
                    if parsed is not None:
                        selection, line, american = parsed
                        pending.append((event_id, market, selection, line, american))
            index += 3
        resolved = resolve_doubleheaders(
            {event_id: (fixture.base_key, fixture.commence_time) for event_id, fixture in fixtures.items()}
        )
        for event_id, market, selection, line, american in pending:
            fixture = fixtures[event_id]
            decimal = american_to_decimal(american)
            outcome.quotes.append(
                Quote(
                    source=source,
                    observed_at=raw.fetched_at,
                    raw_ref=raw.ref,
                    sport=competition.sport,
                    league=competition.key,
                    event_key=resolved[event_id],
                    source_event_id=event_id,
                    home_participant=fixture.home.key,
                    away_participant=fixture.away.key,
                    home_team=fixture.home.name,
                    away_team=fixture.away.name,
                    commence_time=fixture.commence_time,
                    market=market,
                    period=Period.FULL_GAME,
                    selection=selection,
                    line=line,
                    decimal_odds=decimal,
                    american_odds=american,
                    implied_probability=implied_probability(decimal),
                    status=QuoteStatus.ACTIVE,
                )
            )
    drop_duplicate_selections(source, outcome)
    # Same medium, same hazard as VegasInsider: VSiN's cells update one at a
    # time, so a column read mid-move can pair prices the book never offered
    # together.  Judged after dedup, on what would otherwise publish.
    refuse_mid_move_pairings(source, outcome)
    return outcome


def _page_date(table: Tag, raw: RawResponse) -> datetime:
    header = table.select_one("thead th.sp-game-th")
    match = _PAGE_DATE.search(header.get_text(" ", strip=True) if header else "")
    if match is None:
        raise FormatChangeError(f"{raw.source}:{raw.endpoint}: missing slate date")
    month, day = match.groups()
    local_fetched = raw.fetched_at.astimezone(_EASTERN)
    parsed = datetime.strptime(f"{month} {day} {local_fetched.year}", "%b %d %Y")
    if (parsed.date() - local_fetched.date()).days > 180:
        parsed = parsed.replace(year=parsed.year - 1)
    elif (local_fetched.date() - parsed.date()).days > 180:
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def _event_id(row: Tag) -> str | None:
    button = row.select_one("button.sp-detail-btn")
    match = _EVENT_ID.search(str(button.get("onclick") or "")) if button else None
    return match.group(1) if match else None


def _commence_time(row: Tag, page_date: datetime) -> datetime | None:
    match = _CLOCK.search(row.get_text(" ", strip=True))
    if match is None:
        return None
    clock = datetime.strptime(match.group(1).upper(), "%I:%M %p")
    return page_date.replace(hour=clock.hour, minute=clock.minute, tzinfo=_EASTERN)


def _team_name(row: Tag) -> str:
    link = row.select_one("a.sp-team-link")
    return link.get_text(" ", strip=True) if link else ""


def _price(
    cell: Tag, market: Market, side_index: int
) -> tuple[Selection, float | None, int] | None:
    if "sp-val-na" in (cell.select_one(".sp-val-na") or cell).get("class", []):
        return None
    if market is Market.MONEYLINE:
        token = cell.get_text("", strip=True).replace("\u2212", "-")
        if not _AMERICAN.match(token):
            return None
        return (Selection.AWAY if side_index == 0 else Selection.HOME, None, int(token))
    number = cell.select_one(".sp-s-num")
    juice = cell.select_one(".sp-s-juice")
    if number is None or juice is None:
        return None
    line_token = number.get_text("", strip=True).replace("\u2212", "-")
    juice_token = juice.get_text("", strip=True).replace("\u2212", "-")
    line_match = re.search(r"[+-]?\d+(?:\.\d+)?", line_token)
    odds_match = re.search(r"[+-]\d+", juice_token)
    if line_match is None or odds_match is None:
        return None
    line = float(line_match.group())
    american = int(odds_match.group())
    if market is Market.TOTAL:
        marker = cell.select_one(".sp-total-o")
        selection = Selection.OVER if marker and marker.get_text(strip=True).lower() == "o" else Selection.UNDER
    else:
        selection = Selection.AWAY if side_index == 0 else Selection.HOME
    return selection, line, american


__all__ = ["VsinCircaAdapter", "parse_vsin_circa"]
