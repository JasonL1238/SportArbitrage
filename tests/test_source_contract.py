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
from src.events import build_event_key
from src.leagues import is_known, league
from src.participants import canonical_participant
from src.sources import registry
from src.schema import (
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    SELECTIONS_BY_MARKET,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Sport,
    draw_is_priced,
)

#: The largest line magnitude each sport plausibly publishes.  A total of 8.0
#: arriving as 8000 is a units error, not a line, and books that send lines in
#: thousandths make that a live risk rather than a hypothetical one.
MAX_ABS_LINE: dict[Sport, float] = {
    Sport.BASEBALL: 15.0,
    Sport.HOCKEY: 12.0,
    Sport.SOCCER: 12.0,
    Sport.TENNIS: 15.0,
    Sport.BASKETBALL: 300.0,
    Sport.FOOTBALL: 90.0,
}

#: The increment a sport's lines land on.  Half-point lines everywhere except
#: soccer and tennis, where Asian handicaps and totals legitimately use quarter
#: lines (-0.25, +0.75) that split the stake across two numbers.
LINE_INCREMENT: dict[Sport, float] = {
    Sport.BASEBALL: 0.5,
    Sport.BASKETBALL: 0.5,
    Sport.FOOTBALL: 0.5,
    Sport.HOCKEY: 0.5,
    Sport.SOCCER: 0.25,
    Sport.TENNIS: 0.25,
}

#: Every adapter under contract — **read off the registry**, never listed here.
#:
#: This file's own docstring says an adapter is finished when this passes with it
#: added, and for three books the only thing making that true was somebody
#: remembering to edit a literal.  A source added to the registry and forgotten
#: here would escape every clause below while looking, from the outside, exactly
#: as finished as the others.  ``tests/conftest.py`` raises if a registered
#: source has no captured payloads, so "no fixture" is a loud failure rather than
#: a silent skip.
ADAPTERS = list(registry.keys())


@pytest.fixture(scope="session")
def parsed(registered_raws):
    """Each registered adapter's full ParseOutcome from its captured responses."""
    outcomes = {}
    for key, raws in registered_raws.items():
        adapter = registry.descriptor(key).replay_instance()
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
        """Same bytes, same instance, same rows — twice.

        Necessary but **not** sufficient for the purity clause, which is why the
        test below exists: this one passes just as happily when ``parse`` reads
        configuration off the instance, because both calls read the same
        configuration.
        """
        key, _, raws = adapter_case
        adapter = registry.descriptor(key).replay_instance()
        try:
            first = [q.model_dump() for q in adapter.parse(raws).quotes]
            second = [q.model_dump() for q in adapter.parse(raws).quotes]
        finally:
            adapter.close()
        assert first == second

    def test_parsing_does_not_depend_on_how_the_instance_was_configured(
        self, adapter_case
    ) -> None:
        """The clause replay actually rests on, tested where it can fail.

        ``src.collector.replay_run`` builds an adapter with **no arguments** and
        re-parses the stored bytes, so any configuration ``parse`` reads off its
        instance is a way for replay to disagree with collection — silently, and
        reported as rows the parser lost or invented.

        FanDuel did exactly this: narrowed to one league, the same soccer capture
        parsed to 30 rows on collection and 381 on replay.  Determinism alone did
        not catch it, because a single instance is self-consistent.  Two
        differently-configured instances over one set of bytes is what does.
        """
        key, _, raws = adapter_case
        descriptor = registry.descriptor(key)
        default = descriptor.replay_instance()
        try:
            expected = [q.model_dump() for q in default.parse(raws).quotes]
            declared = list(default.leagues)
        finally:
            default.close()
        if len(declared) < 2:
            pytest.skip(f"{key} collects one league, so there is nothing to narrow")

        narrowed = descriptor.build(leagues=declared[:1])
        try:
            assert [q.model_dump() for q in narrowed.parse(raws).quotes] == expected
        finally:
            narrowed.close()

    def test_the_rows_carry_the_registered_source_key_not_the_modules(
        self, adapter_case
    ) -> None:
        """Two instances of one adapter class must be distinguishable in the data.

        ``source`` is the first element of ``dedup_key``, so a parser that read
        its identity off a module constant would file the LeoVegas tenant's
        prices under BetRivers and collide the two on insert — and replay, which
        builds an adapter with no arguments at all, could never reproduce the
        right key.  It comes off the envelope instead.
        """
        key, outcome, raws = adapter_case
        assert {raw.source for raw in raws} == {key}
        assert {quote.source for quote in outcome.quotes} == {key}

    def test_the_adapter_declares_what_it_prices(self, adapter_case) -> None:
        """Coverage reporting must be able to say "this source has no totals for
        soccer" instead of inferring it from an absence — and validation must not
        fault a book for a market it never claimed."""
        key, outcome, _ = adapter_case
        adapter = registry.descriptor(key).replay_instance()
        try:
            claims = adapter.capabilities()
        finally:
            adapter.close()
        assert claims, f"{key} declares no capabilities"
        for league_key, markets in claims.items():
            assert is_known(league_key), f"{key} claims unregistered league {league_key!r}"
            assert markets, f"{key} claims league {league_key} with no markets"
        # Nothing it produced may be outside what it claimed for that league.
        for quote in outcome.quotes:
            declared = claims.get(quote.league)
            if declared is None or quote.period is not Period.FULL_GAME:
                continue
            assert quote.market in declared, (
                f"{key} produced {quote.market.value} for {quote.league} without "
                "claiming it"
            )

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

    def test_a_zero_increment_records_no_skip_reason_at_all(self) -> None:
        """``skipped[reason] += 0`` must leave the map untouched.

        A plain ``Counter`` materializes the key at zero, and the v2 Action
        Network parser shipped exactly that (``odds_not_an_object: 0`` on every
        capture whose rows were all objects) — a label asserting out-of-scope
        input was seen when none was.  The invariant the test above states is
        made structural by ``ReasonCounter``; this pin fails if ``ParseOutcome``
        ever reverts to a plain ``Counter``.
        """
        from src.sources.base import ParseOutcome

        outcome = ParseOutcome()
        outcome.skipped["odds_not_an_object"] += 0
        assert dict(outcome.skipped) == {}
        outcome.skipped["odds_not_an_object"] += 2
        outcome.skipped["odds_not_an_object"] += 0
        assert dict(outcome.skipped) == {"odds_not_an_object": 2}

    def test_every_demonstrated_door_into_a_zero_count_reason_is_closed(self) -> None:
        """The ``+=`` idiom is one of five doors, and an adversarial pass walked
        through the other four: ``Counter.update`` (and so the constructor and
        ``copy``) bypasses ``__setitem__`` at the dict level when the counter is
        empty; an increment undone by a decrement leaves the key at zero; and
        ``setdefault`` inserts at the C level.  ``extend`` composes from
        ``update``, so a zero held by one outcome must not propagate either.
        """
        from src.sources.base import ParseOutcome, ReasonCounter

        assert dict(ReasonCounter({"r": 0})) == {}
        counter = ReasonCounter()
        counter.update({"r": 0})
        assert dict(counter) == {}
        counter["r"] += 2
        counter["r"] -= 2
        assert dict(counter) == {}
        counter["r"] += 2
        counter.subtract({"r": 2})
        assert dict(counter) == {}
        assert counter.setdefault("r", 0) == 0
        assert dict(counter) == {}
        counter["kept"] += 3
        assert dict(counter.copy()) == {"kept": 3}
        receiving = ParseOutcome()
        holding = ParseOutcome()
        holding.skipped.update({"r": 0, "kept": 1})
        receiving.extend(holding)
        assert dict(receiving.skipped) == {"kept": 1}


class TestRowIdentity:
    def test_league_is_registered_and_matches_the_sport(self, adapter_case) -> None:
        """A league the registry does not know cannot be validated, scheduled, or
        joined — reconciliation skips it and every per-league check reads a
        default."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert is_known(quote.league), f"{key}: unregistered league {quote.league!r}"
            assert league(quote.league).sport is quote.sport, (
                f"{key}: {quote.league} is not a {quote.sport.value} league"
            )

    def test_the_period_has_recorded_settlement_rules(self, adapter_case) -> None:
        """Collecting a window whose tie semantics are unknown means guessing
        whether a tie voids the bet, and that guess is worth the whole stake."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            assert quote.settlement is not None, f"{key}: {quote.sport}/{quote.period}"

    def test_a_draw_appears_only_where_the_books_price_one(self, adapter_case) -> None:
        """A draw on a full-game baseball or hockey moneyline is a misparsed third
        runner; on a soccer 90-minute or hockey regulation market it is correct."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.selection is Selection.DRAW:
                assert draw_is_priced(quote.sport, quote.period), (
                    f"{key}: draw on {quote.sport.value}/{quote.period.value}"
                )

    def test_source_matches_the_adapter_key(self, adapter_case) -> None:
        key, outcome, _ = adapter_case
        assert {q.source for q in outcome.quotes} == {key}

    def test_participants_resolve_within_their_league(self, adapter_case) -> None:
        """A passthrough feed string here breaks every cross-source join, and a
        market label reaching this field is how futures become games."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            competition = league(quote.league)
            for role, name in (("home", quote.home_team), ("away", quote.away_team)):
                assert canonical_participant(name, competition) is not None, (
                    f"{key}: {role} {name!r} does not resolve in {quote.league}"
                )

    def test_participant_keys_match_the_display_names(self, adapter_case) -> None:
        """The row carries both an identity and a display name; if they disagree,
        the thing everything joins on is not the thing a human is reading."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            competition = league(quote.league)
            home = canonical_participant(quote.home_team, competition)
            away = canonical_participant(quote.away_team, competition)
            assert home.key == quote.home_participant, f"{key}: {quote.home_team!r}"
            assert away.key == quote.away_participant, f"{key}: {quote.away_team!r}"

    def test_roster_participants_use_the_canonical_spelling(self, adapter_case) -> None:
        """For a closed roster there is one correct spelling, and emitting it
        rather than the feed's makes rows comparable without re-normalising at
        every read.  Open rosters have no canonical form to demand."""
        _, outcome, _ = adapter_case
        for quote in outcome.quotes:
            competition = league(quote.league)
            if competition.roster is None:
                continue
            assert canonical_participant(quote.home_team, competition).name == quote.home_team
            assert canonical_participant(quote.away_team, competition).name == quote.away_team

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
                (quote.home_participant, quote.away_participant, quote.commence_time)
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

    def test_lines_are_on_their_sports_scale(self, adapter_case) -> None:
        """A total of 8.0 arriving as 8000 is a units error, not a line.  Books
        that send lines in thousandths make this a live risk."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.line is None:
                continue
            limit = MAX_ABS_LINE[quote.sport]
            assert abs(quote.line) <= limit, (
                f"{key}: {quote.sport.value} line {quote.line} exceeds {limit}"
            )

    def test_totals_are_plausible_for_their_league(self, adapter_case) -> None:
        """A 2.5-point NFL total and a 165-goal soccer total are both a market
        mapped to the wrong sport, and neither is caught by a units check alone.

        Judged against the **ladder** bound rather than the main-line band: a
        venue's alternate totals run well past its headline number, and Kalshi
        quotes MLB totals from 1.5 to 13.5 against a main-line floor of 4.
        """
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.market is not Market.TOTAL or quote.period is not Period.FULL_GAME:
                continue
            low, high = league(quote.league).plausible_line_range
            assert low <= quote.line <= high, (
                f"{key}: {quote.league} full-game total {quote.line} outside {low}-{high}"
            )

    def test_lines_land_on_their_sports_increment(self, adapter_case) -> None:
        """Half-point lines everywhere except soccer and tennis, where quarter
        lines are real and split the stake across two numbers."""
        key, outcome, _ = adapter_case
        for quote in outcome.quotes:
            if quote.line is None:
                continue
            step = LINE_INCREMENT[quote.sport]
            scaled = quote.line / step
            assert abs(scaled - round(scaled)) < 1e-9, (
                f"{key}: {quote.sport.value} line {quote.line} is not a multiple of {step}"
            )

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
            present = {r.selection for r in rows}
            if rows[0].market is Market.MONEYLINE:
                # The expected outcome count is sport-dependent: a soccer
                # 90-minute moneyline and a hockey regulation one have three
                # legs, everything else has two.  A two-way market missing a leg
                # and a genuine two-way market are otherwise indistinguishable,
                # and only the complete one can be checked for overround.
                needed = {Selection.HOME, Selection.AWAY}
                if draw_is_priced(rows[0].sport, rows[0].period):
                    needed = needed | {Selection.DRAW}
                if not needed <= present:
                    continue
            else:
                required = SELECTIONS_BY_MARKET[rows[0].market]
                if not (required & present) == required:
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
        for key, (outcome, _) in parsed.items():
            for quote in outcome.quotes:
                expected = build_event_key(
                    quote.away_participant,
                    quote.home_participant,
                    quote.commence_time,
                    league(quote.league),
                )
                # A doubleheader ordinal is the only permitted difference.
                assert quote.event_key.split("#")[0] == expected, (
                    f"{key}: {quote.event_key} != {expected}"
                )

    def test_sources_that_share_an_event_agree_on_the_participants(self, parsed) -> None:
        """Books must agree on who is playing.  Where home advantage is real they
        must also agree on which side is home; in tennis the ordering is imposed
        by src.events.orient and the books have no opinion, so only the pair is
        compared there."""
        by_event: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        for key, (outcome, _) in parsed.items():
            for quote in outcome.quotes:
                competition = league(quote.league)
                if competition.has_home_away:
                    identity = (quote.home_participant, quote.away_participant)
                else:
                    identity = tuple(
                        sorted((quote.home_participant, quote.away_participant))
                    )
                by_event[quote.event_key][key] = identity
        for event_key, per_source in by_event.items():
            assert len(set(per_source.values())) == 1, f"{event_key}: {per_source}"

    def test_shared_events_exist_at_all(self, parsed) -> None:
        """Without an event priced by two books there is nothing to cross-check,
        and every downstream comparison is vacuously clean."""
        counts: Counter[str] = Counter()
        for outcome, _ in parsed.values():
            counts.update(outcome.event_keys)
        assert [event for event, n in counts.items() if n >= 2]

    def test_every_sport_reports_how_many_books_cover_it(self, parsed) -> None:
        """Not an assertion about coverage but a record of it.

        A sport is only usable when at least two books price the same event, and
        that is a property of the day rather than of the code — the NHL has no
        cross-book slate in July.  Printing the count keeps "this sport is
        supported" an evidenced claim instead of an assumed one.
        """
        per_sport: dict[Sport, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for key, (outcome, _) in parsed.items():
            for quote in outcome.quotes:
                per_sport[quote.sport][quote.event_key].add(key)
        for sport, events in sorted(per_sport.items(), key=lambda kv: kv[0].value):
            shared = [e for e, books in events.items() if len(books) >= 2]
            print(
                f"{sport.value}: {len(events)} events, {len(shared)} priced by 2+ books"
            )


@pytest.mark.parametrize("key", [entry.key for entry in registry.SOURCES])
def test_every_registered_source_declares_at_least_one_league(key: str) -> None:
    """An adapter that declares nothing removes itself from the health denominator.

    ``league_coverage`` builds its entries from ``set(configured) | set(rows)``,
    so a source with an empty ``leagues`` **and** no rows contributes none — and
    ``_check_source_health`` then grades the collapse against a denominator that
    silently excludes it.  Measured: two producing sources plus two silent ones
    is a 50% share and a warning; add three silent sources that declare nothing
    and the message honestly prints "2 of 7" while the *grade* comes from a
    hidden 2-of-4.  A pipeline that has lost five of seven books reads as a
    warning.

    Every adapter satisfies this today and ``build_sources`` refuses to construct
    an instance with no serviceable league — but nothing asserted it, and the
    check that depends on it is the one that says whether a run is usable.
    """
    source = registry.descriptor(key).replay_instance()
    assert source.leagues, f"{key} declares no leagues"
    assert all(str(league).strip() for league in source.leagues), key
