"""Named sportsbook columns from VegasInsider's public odds comparison page.

This is a republisher, not a second counterparty.  Each configured instance
filters one named column and is declared redundant with that sportsbook.  The
HTML is server-rendered, credential-free, and carries event time, teams, market,
line, and price in the captured response itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

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
    parse_iso_time,
    refuse_mid_move_pairings,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

SOURCE_KEY = "vi_draftkings"
BASE_URL = "https://www.vegasinsider.com"
HOST_INTERVAL = 1.0

ROUTES: Mapping[str, str] = {
    "MLB": "/mlb/odds/las-vegas/",
    "NFL": "/nfl/odds/las-vegas/",
    "NBA": "/nba/odds/las-vegas/",
    "NHL": "/nhl/odds/las-vegas/",
    "WNBA": "/wnba/odds/las-vegas/",
}
BOOK_LABELS: Mapping[str, str] = {
    "draftkings": "DraftKings",
    "caesars": "Caesars",
    "bet365": "Bet365",
    "betmgm": "BetMGM",
    "fanduel": "FanDuel",
    "hardrock": "HardRock",
    "fanatics": "Fanatics",
    "betrivers": "RiversCasino",
}
MARKETS = frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL})
_EVENT_ID = re.compile(r"/events/(\d+)/")
_AMERICAN = re.compile(r"(?<![\d.])([+-]\d+|even)(?![\d.])", re.IGNORECASE)
_LINE = re.compile(r"^([ou])?([+-]?\d+(?:\.\d+)?)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class _Fixture:
    event_id: str
    competition: Any
    home: Participant
    away: Participant
    commence_time: datetime
    base_key: str


class VegasInsiderAdapter:
    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        book: str = "draftkings",
        base_url: str = BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        wanted = tuple(leagues) if leagues is not None else tuple(ROUTES)
        self._leagues = tuple(league for league in wanted if league in ROUTES)
        if not self._leagues:
            raise ValueError(f"VegasInsiderAdapter has no supported league in {wanted!r}")
        slug = book.strip().lower()
        if slug not in BOOK_LABELS:
            raise ValueError(f"unknown VegasInsider book {book!r}")
        self.book = slug
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
                    f"{self.base_url}{ROUTES[league]}",
                    endpoint=f"odds-{league.lower()}-{self.book}",
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    expect_json=False,
                )
            except SourceError as exc:
                tally.failed(league, exc)
                continue
            raws.append(raw)
            tally.produced(league, 1)
        tally.require_something(what="odds comparison page")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_vegasinsider(raws)

    def close(self) -> None:
        self._http.close()


def parse_vegasinsider(raws: Sequence[RawResponse]) -> ParseOutcome:
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    for raw in latest_per_endpoint(raws):
        parts = raw.endpoint.split("-")
        if len(parts) < 3 or parts[0] != "odds":
            continue
        league_key, book_slug = parts[1].upper(), parts[-1]
        if league_key not in ROUTES or book_slug not in BOOK_LABELS:
            raise FormatChangeError(f"{source}:{raw.endpoint}: unknown route metadata")
        competition = league_registry.league(league_key)
        soup = BeautifulSoup(raw.body, "html.parser")
        fixtures: dict[str, _Fixture] = {}
        pending: list[tuple[str, Market, Selection, float | None, int]] = []
        tables = soup.select("table.odds-table")
        if not tables:
            raise FormatChangeError(f"{source}:{raw.endpoint}: no odds table")
        for table in tables:
            _parse_table(
                table, raw, source, competition, BOOK_LABELS[book_slug], fixtures, pending, outcome
            )
        if not fixtures:
            # Same rule as the missing-column case above: a page that yielded no
            # fixture is a counted outcome, not a bare ``continue``.
            outcome.skipped[f"no_fixture_rows:{raw.endpoint}"] += 1
            continue
        resolved = resolve_doubleheaders(
            {event_id: (fixture.base_key, fixture.commence_time) for event_id, fixture in fixtures.items()}
        )
        for event_id, market, selection, line, american in pending:
            fixture = fixtures[event_id]
            decimal = american_to_decimal(american)
            outcome.quotes.append(
                Quote(
                    source=source, observed_at=raw.fetched_at, raw_ref=raw.ref,
                    sport=competition.sport, league=competition.key,
                    event_key=resolved[event_id], source_event_id=event_id,
                    home_participant=fixture.home.key, away_participant=fixture.away.key,
                    home_team=fixture.home.name, away_team=fixture.away.name,
                    commence_time=fixture.commence_time, market=market,
                    period=Period.FULL_GAME, selection=selection, line=line,
                    decimal_odds=decimal, american_odds=american,
                    implied_probability=implied_probability(decimal), status=QuoteStatus.ACTIVE,
                )
            )
    drop_duplicate_selections(source, outcome)
    # After dedup, so the judged group is what would otherwise publish.  A
    # tracker column read mid-move pairs one side's fresh price with the
    # other's stale one — see the helper's docstring for the measured case.
    refuse_mid_move_pairings(source, outcome)
    return outcome


def _parse_table(
    table: Tag,
    raw: RawResponse,
    source: str,
    competition: Any,
    book_label: str,
    fixtures: dict[str, _Fixture],
    pending: list[tuple[str, Market, Selection, float | None, int]],
    outcome: ParseOutcome,
) -> None:
    headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    try:
        column = headers.index(book_label)
    except ValueError:
        # The page rendered, the fixtures are on it, and this book has no column.
        #
        # Counted rather than returned silently, which is what this did and what
        # made a whole board disappear without a trace.  VegasInsider dropped
        # every per-book column from its MLB page between 01:54 and 07:50 on
        # 2026-08-14 — the earlier capture has eleven columns, the later one has
        # ``Time / Open / Consensus`` and **46 fixtures** — so all eight ``vi_*``
        # sources discarded that board on every run afterwards, reporting
        # ``parsed 0 quotes from 4 responses; skipped={}``.  An empty skip
        # dictionary on a parse that dropped everything is the one thing
        # ``docs/INPUT_CONTRACT.md`` says a drop may never be.
        #
        # The columns that *are* present are named, because which book vanished
        # and which survived is the whole diagnosis: ``Fanatics`` appeared only
        # ever on the MLB page, which is why ``vi_fanatics`` went 90 -> 12 -> 0
        # while its siblings merely shrank.
        outcome.skipped[f"book_column_absent:{book_label}"] += 1
        present = ", ".join(h for h in headers if h) or "no headers at all"
        outcome.skipped[f"columns_offered:{present}"] += 1
        return
    for market, marker in (
        (Market.MONEYLINE, "moneyline"), (Market.TOTAL, "total"), (Market.SPREAD, "spread")
    ):
        body = table.select_one(f'tbody[id^="odds-table-{marker}--0"]')
        if body is None:
            continue
        rows = body.find_all("tr", recursive=False)
        index = 0
        while index + 2 < len(rows):
            time_cell = rows[index].select_one("td.game-time")
            if time_cell is None:
                index += 1
                continue
            id_match = _EVENT_ID.search(str(time_cell.get("data-content") or ""))
            stamp = time_cell.select_one("[data-value]")
            commence = parse_iso_time(stamp.get("data-value") if stamp else None)
            away_row, home_row = rows[index + 1], rows[index + 2]
            away_name = _team_name(away_row)
            home_name = _team_name(home_row)
            if id_match is None or commence is None or not away_name or not home_name:
                outcome.skipped["incomplete_event_header"] += 1
                index += 3
                continue
            event_id = id_match.group(1)
            if commence <= raw.fetched_at:
                outcome.skipped["event_already_started"] += 1
                index += 3
                continue
            away = canonical_participant(away_name, competition)
            home = canonical_participant(home_name, competition)
            if away is None or home is None or away.key == home.key:
                outcome.reject(source, "unresolved_participants", f"{event_id}: {away_name!r}/{home_name!r}")
                index += 3
                continue
            away_side, home_side = orient(away, home, competition, home=home)
            fixture = _Fixture(
                event_id, competition, home_side, away_side, commence,
                build_event_key(away_side.key, home_side.key, commence, competition),
            )
            fixtures[event_id] = fixture
            for side_index, row in enumerate((away_row, home_row)):
                cells = row.find_all(["th", "td"], recursive=False)
                if column >= len(cells):
                    # A header wider than its own body row — the table is not the
                    # shape the header promised, which is worth counting rather
                    # than stepping over.
                    outcome.skipped[f"row_shorter_than_header:{market.value}"] += 1
                    continue
                text = cells[column].get_text(" ", strip=True)
                parsed = _price(text, market, side_index)
                if parsed is None:
                    # An empty cell is a book not offering this market; anything
                    # else is a price this parser could not read. They are
                    # different problems and were both silent.
                    outcome.skipped[
                        f"no_price_in_cell:{market.value}" if not text.strip()
                        else f"unreadable_price:{market.value}"
                    ] += 1
                    continue
                selection, line, american = parsed
                pending.append((event_id, market, selection, line, american))
            index += 3


def _team_name(row: Tag) -> str:
    link = row.select_one("a.team-name")
    return link.get_text(" ", strip=True) if link else ""


def _price(text: str, market: Market, side_index: int) -> tuple[Selection, float | None, int] | None:
    cleaned = " ".join(text.replace("−", "-").split())
    odds = _AMERICAN.findall(cleaned)
    if not odds:
        return None
    token = odds[-1].lower()
    american = 100 if token == "even" else int(token)
    if market is Market.MONEYLINE:
        return (Selection.AWAY if side_index == 0 else Selection.HOME, None, american)
    line_match = _LINE.search(cleaned)
    if line_match is None:
        return None
    line = float(line_match.group(2))
    if market is Market.TOTAL:
        selection = Selection.OVER if line_match.group(1).lower() == "o" else Selection.UNDER
    else:
        selection = Selection.AWAY if side_index == 0 else Selection.HOME
    return selection, line, american


__all__ = ["VegasInsiderAdapter", "parse_vegasinsider"]
