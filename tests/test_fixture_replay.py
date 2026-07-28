"""Regression tests over real captured payloads.

Each of these locks a specific defect shut.  Every expected number was read off
the captured fixtures and cross-checked against the raw JSON by hand, so a
change in parser behaviour shows up as a failure here rather than as a silent
change in what gets collected.
"""
from __future__ import annotations

import statistics

import pytest

from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection
from src.sources.betrivers_kambi import BetRiversKambiAdapter, parse_kambi
from src.sources.fanduel import FanDuelAdapter, parse_fanduel
from src.sources.pinnacle import PinnacleAdapter, parse_pinnacle
from src.validation import validate
from tests.conftest import FIXTURE_DATE

# ── FanDuel ──────────────────────────────────────────────────────────────────


def test_fanduel_parses_only_real_games(fanduel_raw: list[RawResponse]) -> None:
    outcome = parse_fanduel(fanduel_raw[0])
    assert len(outcome.quotes) == 96
    assert len(outcome.event_keys) == 16
    assert outcome.rejections == []
    # The page carries 20 "events"; four are futures containers ("MLB Futures",
    # "MLB Player Awards", "MLB Player Markets") and must not become games.
    assert outcome.skipped["non_game_event"] == 4


def test_fanduel_never_emits_a_free_text_market_type(fanduel_raw: list[RawResponse]) -> None:
    """Unmapped market names used to pass through as the market type itself,
    producing values like ``american_league_cy_young_2026``."""
    outcome = parse_fanduel(fanduel_raw[0])
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE,
        Market.RUN_LINE,
        Market.TOTAL_RUNS,
    }
    assert {q.period for q in outcome.quotes} == {Period.FULL_GAME}


def test_fanduel_strips_probable_pitcher_from_team_names(fanduel_raw: list[RawResponse]) -> None:
    """Event names are ``"Phillies (A Nola) @ Marlins (S Alcantara)"``; the
    pitcher suffix must not end up in the team fields or the event key."""
    outcome = parse_fanduel(fanduel_raw[0])
    rows = [q for q in outcome.quotes if q.event_key == f"PHI@MIA:{FIXTURE_DATE}"]
    assert rows, "expected the Phillies/Marlins game"
    assert {q.home_team for q in rows} == {"Miami Marlins"}
    assert {q.away_team for q in rows} == {"Philadelphia Phillies"}
    assert all("(" not in q.home_team and "(" not in q.away_team for q in outcome.quotes)


def test_fanduel_golden_values(fanduel_raw: list[RawResponse]) -> None:
    """Hand-verified against the captured JSON for one game."""
    outcome = parse_fanduel(fanduel_raw[0])
    rows = {
        (q.market, q.selection): q
        for q in outcome.quotes
        if q.event_key == f"PHI@MIA:{FIXTURE_DATE}"
    }

    moneyline_away = rows[(Market.MONEYLINE, Selection.AWAY)]
    assert moneyline_away.decimal_odds == pytest.approx(1.943396226415094)
    assert moneyline_away.american_odds == -106
    assert moneyline_away.line is None

    run_line_away = rows[(Market.RUN_LINE, Selection.AWAY)]
    run_line_home = rows[(Market.RUN_LINE, Selection.HOME)]
    assert run_line_away.line == -1.5
    assert run_line_home.line == 1.5
    assert run_line_away.decimal_odds == pytest.approx(2.62)
    assert run_line_away.american_odds == 162

    total_over = rows[(Market.TOTAL_RUNS, Selection.OVER)]
    assert total_over.line == 8.5
    assert total_over.decimal_odds == pytest.approx(1.925925925925926)
    assert total_over.american_odds == -108


# ── Pinnacle ─────────────────────────────────────────────────────────────────


def test_pinnacle_excludes_special_matchups(pinnacle_raw: list[RawResponse]) -> None:
    """528 matchups, only 11 of which are games.  Treating the other 517 as
    events produced 528 "events" and selections like ``"5+"``."""
    matchups = next(r for r in pinnacle_raw if r.endpoint == "matchups")
    markets = next(r for r in pinnacle_raw if r.endpoint == "markets-straight")
    outcome = parse_pinnacle(matchups, markets)

    assert len(outcome.event_keys) == 11
    assert outcome.skipped["matchup_type:special"] == 521
    assert outcome.rejections == []
    assert len(outcome.quotes) == 803
    # Every event key is a real matchup key, never a prop description.
    assert all(
        "@" in key and key.split(":")[1].startswith("2026-") for key in outcome.event_keys
    )


def test_pinnacle_period_mapping(pinnacle_raw: list[RawResponse]) -> None:
    """Numeric periods are undocumented; these mappings were verified against
    line values and against Kambi's explicitly labelled markets."""
    matchups = next(r for r in pinnacle_raw if r.endpoint == "matchups")
    markets = next(r for r in pinnacle_raw if r.endpoint == "markets-straight")
    quotes = parse_pinnacle(matchups, markets).quotes

    full_game_totals = [
        q.line for q in quotes if q.market is Market.TOTAL_RUNS and q.period is Period.FULL_GAME
    ]
    first_five_totals = [
        q.line
        for q in quotes
        if q.market is Market.TOTAL_RUNS and q.period is Period.FIRST_5_INNINGS
    ]
    first_inning_totals = [
        q.line
        for q in quotes
        if q.market is Market.TOTAL_RUNS and q.period is Period.FIRST_1_INNING
    ]

    # Alternate lines widen each range, so compare the centres: a full game is
    # priced around 8.5 runs and its first five innings around half that.
    assert statistics.median(full_game_totals) == pytest.approx(8.5, abs=0.5)
    assert statistics.median(first_five_totals) == pytest.approx(4.5, abs=0.5)
    # A single inning is priced at 0.5 runs; three innings never would be.
    assert set(first_inning_totals) == {0.5}


def test_pinnacle_does_not_use_source_clock_as_observation_time(
    pinnacle_raw: list[RawResponse],
) -> None:
    """``cutoffAt`` is when betting closes, in the future.  It was previously
    written into the observation timestamp, making fresh rows look future-dated."""
    matchups = next(r for r in pinnacle_raw if r.endpoint == "matchups")
    markets = next(r for r in pinnacle_raw if r.endpoint == "markets-straight")
    quotes = parse_pinnacle(matchups, markets).quotes

    assert {q.observed_at for q in quotes} == {markets.fetched_at}
    assert all(q.observed_at < q.commence_time for q in quotes)


def test_pinnacle_run_lines_are_mirrored(pinnacle_raw: list[RawResponse]) -> None:
    matchups = next(r for r in pinnacle_raw if r.endpoint == "matchups")
    markets = next(r for r in pinnacle_raw if r.endpoint == "markets-straight")
    quotes = parse_pinnacle(matchups, markets).quotes

    groups: dict[tuple, dict] = {}
    for quote in quotes:
        if quote.market is Market.RUN_LINE:
            groups.setdefault(quote.market_key, {})[quote.selection] = quote
    assert groups
    for sides in groups.values():
        if Selection.HOME in sides and Selection.AWAY in sides:
            assert sides[Selection.HOME].line == pytest.approx(-sides[Selection.AWAY].line)


# ── BetRivers / Kambi ────────────────────────────────────────────────────────


def test_kambi_scales_lines_out_of_thousandths(kambi_raw: list[RawResponse]) -> None:
    """Kambi sends both odds and lines in thousandths.  Only the odds were
    divided, so a total of 8.0 was emitted as a line of 8000."""
    outcome = _parse_kambi(kambi_raw)
    lines = [q.line for q in outcome.quotes if q.line is not None]
    assert lines
    assert max(abs(line) for line in lines) <= 20.0

    totals = [
        q.line
        for q in outcome.quotes
        if q.market is Market.TOTAL_RUNS and q.period is Period.FULL_GAME
    ]
    assert 5.0 <= min(totals) and max(totals) <= 14.0


def test_kambi_collects_full_market_set_not_just_the_headline(
    kambi_raw: list[RawResponse],
) -> None:
    """listView carries one bet offer per event; the per-event endpoint carries
    the rest.  Collecting only listView yielded 2 rows per game and no lines."""
    outcome = _parse_kambi(kambi_raw)
    assert len(outcome.event_keys) == 16
    assert len(outcome.quotes) == 755
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE,
        Market.RUN_LINE,
        Market.TOTAL_RUNS,
        Market.TEAM_TOTAL_RUNS,
    }
    assert outcome.rejections == []


def test_kambi_excludes_pitcher_conditional_moneylines(kambi_raw: list[RawResponse]) -> None:
    """"Match Odds (X must start)" is a different market from the moneyline.
    Collecting both would put several conflicting prices on one selection."""
    outcome = _parse_kambi(kambi_raw)
    assert any(key.startswith("criterion:Match Odds (") for key in outcome.skipped)

    seen: set[tuple] = set()
    for quote in outcome.quotes:
        assert quote.dedup_key not in seen, f"duplicate selection {quote.dedup_key}"
        seen.add(quote.dedup_key)


def test_kambi_resolves_city_abbreviated_team_names(kambi_raw: list[RawResponse]) -> None:
    """Kambi sends "CHI White Sox", "WAS Nationals", "NY Mets".  These failed to
    resolve, rejecting 14 of 16 games."""
    outcome = _parse_kambi(kambi_raw)
    assert outcome.rejections == []
    assert len(outcome.event_keys) == 16


def test_kambi_records_source_price_change_time_separately(
    kambi_raw: list[RawResponse],
) -> None:
    outcome = _parse_kambi(kambi_raw)
    with_change = [q for q in outcome.quotes if q.last_change_at is not None]
    assert with_change
    assert all(q.last_change_at <= q.observed_at for q in with_change)


def _parse_kambi(kambi_raw: list[RawResponse]):
    listview = next(r for r in kambi_raw if r.endpoint == "listview")
    betoffers = [r for r in kambi_raw if r.endpoint.startswith("betoffer-batch-")]
    return parse_kambi(listview, betoffers)


# ── whole-slate properties ───────────────────────────────────────────────────


def test_fixture_slate_passes_validation(all_fixture_quotes) -> None:
    report = validate(all_fixture_quotes)
    assert report.ok, "\n".join(str(f) for f in report.errors[:20])
    assert report.warnings == [], "\n".join(str(f) for f in report.warnings[:20])
    assert report.quote_count == 1654
    assert report.event_count == 16
    assert report.source_count == 3


def test_every_event_is_priced_by_at_least_two_books(all_fixture_quotes) -> None:
    by_event: dict[str, set[str]] = {}
    for quote in all_fixture_quotes:
        by_event.setdefault(quote.event_key, set()).add(quote.source)
    assert by_event
    assert all(len(sources) >= 2 for sources in by_event.values())


def test_doubleheader_gets_distinct_keys_consistently(all_fixture_quotes) -> None:
    """The captured slate contains a Cleveland/Cincinnati doubleheader.  Both
    books must agree on which game is which, or the join is wrong."""
    keys = {q.event_key for q in all_fixture_quotes if q.event_key.startswith("CLE@CIN")}
    assert keys == {f"CLE@CIN:{FIXTURE_DATE}", f"CLE@CIN:{FIXTURE_DATE}#2"}

    for key in sorted(keys):
        starts = {q.source: q.commence_time for q in all_fixture_quotes if q.event_key == key}
        assert len(starts) >= 2
        spread = max(starts.values()) - min(starts.values())
        assert spread.total_seconds() <= 120


def test_parsing_is_deterministic(fanduel_raw, pinnacle_raw, kambi_raw) -> None:
    """Replay depends on parsing being a pure function of the captured bytes."""
    for adapter_cls, raws in (
        (FanDuelAdapter, fanduel_raw),
        (PinnacleAdapter, pinnacle_raw),
        (BetRiversKambiAdapter, kambi_raw),
    ):
        adapter = adapter_cls()
        try:
            first = adapter.parse(raws).quotes
            second = adapter.parse(raws).quotes
        finally:
            adapter.close()
        assert [q.model_dump() for q in first] == [q.model_dump() for q in second]


def test_no_quote_is_priced_at_or_below_evens_boundary(all_fixture_quotes) -> None:
    assert all(1.0 < q.decimal_odds <= 1000.0 for q in all_fixture_quotes)
    assert all(q.american_odds != 0 for q in all_fixture_quotes)


def test_all_rows_carry_a_raw_reference(all_fixture_quotes) -> None:
    assert all(q.raw_ref for q in all_fixture_quotes)
    assert all(q.status in (QuoteStatus.ACTIVE, QuoteStatus.SUSPENDED) for q in all_fixture_quotes)


def test_kambi_offer_close_time_is_not_read_as_a_suspension_flag(
    kambi_raw: list[RawResponse],
) -> None:
    """Kambi's ``closed`` is the betting cutoff *timestamp*, not a boolean.

    Treating any non-empty value as truthy marked 741 of 757 perfectly open
    offers as suspended — while every outcome in the payload reported ``OPEN``.
    That also silenced the overround check, which skips markets that are not
    fully active, so the strongest correctness test stopped covering this book.
    """
    outcome = _parse_kambi(kambi_raw)
    active = [q for q in outcome.quotes if q.status is QuoteStatus.ACTIVE]
    # Every cutoff in the fixture is hours after the 07:03Z capture.
    assert len(active) == len(outcome.quotes)


def test_kambi_marks_a_past_cutoff_as_suspended(kambi_raw: list[RawResponse]) -> None:
    """The cutoff still has to be honoured once it has passed."""
    import json

    from src.raw_store import RawResponse as _Raw

    listview = next(r for r in kambi_raw if r.endpoint == "listview")
    betoffer = next(r for r in kambi_raw if r.endpoint.startswith("betoffer-batch-"))
    payload = betoffer.json()
    for offer in payload["betOffers"]:
        offer["closed"] = "2026-07-28T06:00:00Z"  # before the 07:03Z capture
    rewritten = _Raw(
        source=betoffer.source,
        endpoint=betoffer.endpoint,
        url=betoffer.url,
        status_code=betoffer.status_code,
        body=json.dumps(payload),
        fetched_at=betoffer.fetched_at,
        content_type=betoffer.content_type,
    )
    quotes = parse_kambi(listview, [rewritten]).quotes
    assert quotes
    assert all(q.status is QuoteStatus.SUSPENDED for q in quotes)
