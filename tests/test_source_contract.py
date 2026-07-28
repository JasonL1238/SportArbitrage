"""The source input contract, as an executable test.

`docs/INPUT_CONTRACT.md` states what a scraper must deliver.  This file asserts
every mechanically checkable clause of it against each registered adapter, using
its captured payloads.  A new adapter is finished when this passes with it added
to ``ADAPTERS`` — not when it returns rows.

Each test names the clause it enforces and why the clause exists, because a
contract test that only says "assert x == y" gets deleted the first time it is
inconvenient.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from src.arb import canonical_line
from src.normalize import (
    MAX_DECIMAL_ODDS,
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    is_plausible_decimal_odds,
)
from src.schema import (
    LEAGUE_MLB,
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    SELECTIONS_BY_MARKET,
    SPORT_BASEBALL,
    Market,
    QuoteStatus,
    Selection,
)
from src.teams import canonical_team

#: Every adapter under contract, with the fixture that feeds it.
ADAPTERS = ["fanduel", "pinnacle", "betrivers_kambi"]


@pytest.fixture(scope="session")
def parsed(fanduel_raw, pinnacle_raw, kambi_raw):
    """Each adapter's full ParseOutcome from its captured responses."""
    from src.sources.betrivers_kambi import BetRiversKambiAdapter
    from src.sources.fanduel import FanDuelAdapter
    from src.sources.pinnacle import PinnacleAdapter

    outcomes = {}
    for key, cls, raws in (
        ("fanduel", FanDuelAdapter, fanduel_raw),
        ("pinnacle", PinnacleAdapter, pinnacle_raw),
        ("betrivers_kambi", BetRiversKambiAdapter, kambi_raw),
    ):
        adapter = cls()
        try:
            outcomes[key] = (adapter.parse(raws), raws)
        finally:
            adapter.close()
    return outcomes


@pytest.fixture(params=ADAPTERS)
def adapter_case(request, parsed):
    outcome, raws = parsed[request.param]
    return request.param, outcome, raws


class TestInterface:
    def test_the_adapter_declares_a_stable_lowercase_key(self, adapter_case) -> None:
        key, _, _ = adapter_case
        assert key == key.lower()
        assert key.strip() == key and key

    def test_parsing_is_deterministic(self, adapter_case, parsed) -> None:
        """Replay, regression testing and offline reproduction all rest on this
        and nothing else."""
        key, _, raws = adapter_case
        from src.sources.betrivers_kambi import BetRiversKambiAdapter
        from src.sources.fanduel import FanDuelAdapter
        from src.sources.pinnacle import PinnacleAdapter

        cls = {
            "fanduel": FanDuelAdapter,
            "pinnacle": PinnacleAdapter,
            "betrivers_kambi": BetRiversKambiAdapter,
        }[key]
        adapter = cls()
        try:
            first = [q.model_dump() for q in adapter.parse(raws).quotes]
            second = [q.model_dump() for q in adapter.parse(raws).quotes]
        finally:
            adapter.close()
        assert first == second

    def test_the_adapter_produced_rows_at_all(self, adapter_case) -> None:
        key, outcome, _ = adapter_case
        assert outcome.quotes, f"{key} parsed nothing from its captured payloads"

    def test_nothing_in_scope_was_rejected(self, adapter_case) -> None:
        """A rejection means the source offered something in scope that could not
        be represented.  Zero is the passing state."""
        key, outcome, _ = adapter_case
        assert outcome.rejections == [], (
            f"{key}: "
            + "; ".join(f"{r.reason}: {r.detail}" for r in outcome.rejections[:5])
        )

    def test_out_of_scope_input_is_counted_rather_than_ignored(self, adapter_case) -> None:
        """Every skip carries a reason, so "we collected everything" stays
        falsifiable.  Reasons must be non-empty labels, not blanks."""
        _, outcome, _ = adapter_case
        for reason, count in outcome.skipped.items():
            assert reason and reason.strip(), "a skip reason must be a stable label"
            assert count > 0


class TestRowIdentity:
    def test_sport_and_league_are_the_declared_constants(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert quote.sport == SPORT_BASEBALL
            assert quote.league == LEAGUE_MLB

    def test_source_matches_the_adapter_key(self, adapter_case) -> None:
        key, outcome, _ = adapter_case
        assert {q.source for q in outcome.quotes} == {key}

    def test_team_names_resolve_to_real_clubs(self, adapter_case) -> None:
        """A passthrough feed string here breaks every cross-source join, and a
        market label reaching this field is how futures become games."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            for role, name in (("home", quote.home_team), ("away", quote.away_team)):
                assert canonical_team(name) is not None, f"{key}: {role} {name!r}"

    def test_team_names_are_the_canonical_spelling(self, adapter_case) -> None:
        """Emitting the canonical name rather than the feed's spelling is what
        makes rows comparable without re-normalising at every read."""
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert canonical_team(quote.home_team).name == quote.home_team
            assert canonical_team(quote.away_team).name == quote.away_team

    def test_event_keys_are_well_formed(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for key in outcome.event_keys:
            matchup, _, rest = key.partition(":")
            away, _, home = matchup.partition("@")
            assert away and home and away != home
            assert rest.startswith("20"), f"{key} does not carry a scheduling date"

    def test_an_event_key_maps_to_one_matchup_and_one_start_time(
        self, adapter_case
    ) -> None:
        """Two different games under one key is the doubleheader mis-join, and it
        is silent — the rows all look individually valid."""
        key, outcome, _ = adapter_case
        by_event: dict[str, set] = defaultdict(set)
        for quote in outcome.quotes:
            by_event[quote.event_key].add(
                (quote.home_team, quote.away_team, quote.commence_time)
            )
        for event_key, identities in by_event.items():
            assert len(identities) == 1, f"{key}: {event_key} holds {identities}"

    def test_a_source_event_id_maps_to_one_event_key(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        mapping: dict[str, set[str]] = defaultdict(set)
        for quote in outcome.quotes:
            mapping[quote.source_event_id].add(quote.event_key)
        for source_event_id, keys in mapping.items():
            assert len(keys) == 1, f"{source_event_id} spans {keys}"

    def test_doubleheader_games_have_distinct_source_event_ids(
        self, adapter_case
    ) -> None:
        """Downstream reconciliation needs the two games told apart at the source."""
        _, outcome, _ = adapter_case
        by_id: dict[str, set] = defaultdict(set)
        for quote in outcome.quotes:
            by_id[quote.source_event_id].add(quote.commence_time)
        for source_event_id, times in by_id.items():
            assert len(times) == 1, f"{source_event_id} carries several start times"


class TestMarketVocabulary:
    def test_selections_are_legal_for_their_market(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert quote.selection in SELECTIONS_BY_MARKET[quote.market]

    def test_lines_are_present_exactly_where_they_are_meaningful(
        self, adapter_case
    ) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.market in MARKETS_REQUIRING_LINE:
                assert quote.line is not None
            else:
                assert quote.line is None

    def test_side_is_present_exactly_for_team_totals(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.market in MARKETS_REQUIRING_SIDE:
                assert quote.side is not None
            else:
                assert quote.side is None

    def test_lines_are_on_a_baseball_scale(self, adapter_case) -> None:
        """A total of 8.0 arriving as 8000 is a units error, not a line.  Books
        that send lines in thousandths make this a live risk."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.line is None:
                continue
            assert abs(quote.line) <= 30.0, f"{key}: line {quote.line}"

    def test_lines_land_on_half_run_increments(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.line is None:
                continue
            assert abs(quote.line * 2 - round(quote.line * 2)) < 1e-9, quote.line

    def test_two_sided_markets_mirror_their_lines(self, adapter_case) -> None:
        """The pairing rule the arbitrage engine depends on: a run line's two
        halves must be exact opposites, and a total's two sides must share one
        number."""
        key, outcome, _ = adapter_case
        by_market: dict[tuple, dict[Selection, float | None]] = defaultdict(dict)
        for quote in outcome.quotes:
            by_market[quote.market_key][quote.selection] = quote.line

        for market_key, lines in by_market.items():
            if Selection.HOME in lines and Selection.AWAY in lines:
                home, away = lines[Selection.HOME], lines[Selection.AWAY]
                if home is not None and away is not None:
                    assert abs(home + away) < 1e-9, f"{key}: {market_key} {home}/{away}"
            if Selection.OVER in lines and Selection.UNDER in lines:
                over, under = lines[Selection.OVER], lines[Selection.UNDER]
                if over is not None and under is not None:
                    assert abs(over - under) < 1e-9, f"{key}: {market_key} {over}/{under}"

    def test_a_market_id_identifies_exactly_one_market(self, adapter_case) -> None:
        """Several lines, both teams' totals, or two periods under one id fuses
        distinct markets, after which every market-level check reads an
        arbitrary row.  Two mirrored run-line rows are the legitimate case and
        are allowed."""
        key, outcome, _ = adapter_case
        grouped: dict[tuple, list] = defaultdict(list)
        for quote in outcome.quotes:
            grouped[quote.market_key].append(quote)

        for market_key, rows in grouped.items():
            assert len({r.market for r in rows}) == 1, f"{key}: {market_key} spans markets"
            assert len({r.period for r in rows}) == 1, f"{key}: {market_key} spans periods"
            assert len({r.side for r in rows}) == 1, f"{key}: {market_key} spans sides"
            assert len({r.is_alternate for r in rows}) == 1, (
                f"{key}: {market_key} mixes primary and alternate rows"
            )
            # One canonical line per market: run lines mirror onto a single value.
            assert len({canonical_line(r) for r in rows}) == 1, (
                f"{key}: {market_key} groups several lines"
            )

    def test_no_two_rows_share_a_dedup_key(self, adapter_case) -> None:
        """Storage enforces this with a UNIQUE constraint, so a collision does
        not merely warn — it aborts the run's insert."""
        key, outcome, _ = adapter_case
        counts = Counter(q.dedup_key for q in outcome.quotes)
        collisions = {k: n for k, n in counts.items() if n > 1}
        assert not collisions, f"{key}: {list(collisions)[:3]}"


class TestPrices:
    def test_all_three_price_formats_agree(self, adapter_case) -> None:
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert quote.implied_probability == pytest.approx(
                1 / quote.decimal_odds, abs=1e-9
            )
            payout = quote.decimal_odds - 1.0
            from_american = american_to_decimal(quote.american_odds) - 1.0
            assert abs(from_american - payout) / payout <= 0.01, (
                f"{key}: {quote.american_odds:+d} vs {quote.decimal_odds}"
            )

    def test_prices_are_in_a_range_a_book_publishes(self, adapter_case) -> None:
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert is_plausible_decimal_odds(quote.decimal_odds), (
                f"{key}: {quote.decimal_odds} outside "
                f"{MIN_DECIMAL_ODDS}-{MAX_DECIMAL_ODDS}"
            )

    def test_american_odds_are_actual_prices(self, adapter_case) -> None:
        """Values strictly between -100 and +100 are not prices; -50 converts to
        a plausible 3.00 and would be undetectable downstream."""
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert not (-100 < quote.american_odds < 100), quote.american_odds

    def test_a_complete_market_never_prices_the_book_to_lose(
        self, adapter_case
    ) -> None:
        """Implied probabilities summing below 1.0 at one book means the prices
        or lines are mispaired — it is the signature of a parser fault, and it is
        also exactly what a phantom arbitrage looks like."""
        key, outcome, _ = adapter_case
        grouped: dict[tuple, list] = defaultdict(list)
        for quote in outcome.quotes:
            grouped[quote.market_key].append(quote)

        for market_key, rows in grouped.items():
            if len(rows) < 2 or any(r.status is not QuoteStatus.ACTIVE for r in rows):
                continue
            required = SELECTIONS_BY_MARKET[rows[0].market]
            present = {r.selection for r in rows}
            if rows[0].market is Market.MONEYLINE:
                if not {Selection.HOME, Selection.AWAY} <= present:
                    continue
            elif not (required & present) == required:
                continue
            overround = sum(r.implied_probability for r in rows)
            assert overround >= 1.0 - 1e-9, f"{key}: {market_key} sums to {overround:.4f}"


class TestProvenance:
    def test_observed_at_comes_from_a_captured_response(self, adapter_case) -> None:
        """Never a source-supplied clock: a betting cutoff is in the future, and
        using it makes fresh rows look future-dated."""
        key, outcome, raws = adapter_case
        fetched = {raw.fetched_at for raw in raws}
        for quote in outcome.quotes:
            assert quote.observed_at in fetched, f"{key}: {quote.observed_at}"

    def test_raw_ref_points_at_a_captured_response(self, adapter_case) -> None:
        key, outcome, raws = adapter_case
        refs = {raw.ref for raw in raws}
        for quote in outcome.quotes:
            assert quote.raw_ref in refs, f"{key}: orphaned raw_ref {quote.raw_ref}"

    def test_prices_are_observed_before_the_game_starts(self, adapter_case) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert quote.observed_at < quote.commence_time

    def test_a_reported_change_time_is_never_in_the_future(
        self, adapter_case
    ) -> None:
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.last_change_at is not None:
                assert quote.last_change_at <= quote.observed_at

    def test_stated_limits_are_positive(self, adapter_case) -> None:
        """``None`` means unknown; a zero or negative limit would silently cap
        every position at nothing."""
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.limit_amount is not None:
                assert quote.limit_amount > 0


class TestCrossAdapterConsistency:
    """Clauses that only mean anything with more than one adapter present."""

    def test_all_adapters_agree_on_event_key_construction(self, parsed) -> None:
        """A key built differently by one adapter joins to nothing, and the
        symptom is an absence rather than an error."""
        from src.events import build_event_key

        for key, (outcome, _) in parsed.items():
            for quote in outcome.quotes:
                away = canonical_team(quote.away_team).abbr
                home = canonical_team(quote.home_team).abbr
                expected = build_event_key(away, home, quote.commence_time)
                # A doubleheader ordinal is the only permitted difference.
                assert quote.event_key.split("#")[0] == expected, (
                    f"{key}: {quote.event_key} != {expected}"
                )

    def test_sources_that_share_an_event_agree_on_the_teams(self, parsed) -> None:
        by_event: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        for key, (outcome, _) in parsed.items():
            for quote in outcome.quotes:
                by_event[quote.event_key][key] = (quote.home_team, quote.away_team)
        for event_key, per_source in by_event.items():
            assert len(set(per_source.values())) == 1, f"{event_key}: {per_source}"

    def test_shared_events_exist_at_all(self, parsed) -> None:
        """Without an event priced by two books there is nothing to cross-check,
        and every downstream comparison is vacuously clean."""
        counts: Counter[str] = Counter()
        for outcome, _ in parsed.values():
            counts.update(outcome.event_keys)
        assert [event for event, n in counts.items() if n >= 2]
