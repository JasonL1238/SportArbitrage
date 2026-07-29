"""Offline replay of the whole multi-sport slate, from stored bytes only.

``tests/fixtures/raw`` holds verbatim envelopes written by
:class:`src.raw_store.RawStore` during real live runs on 2026-07-28, so
everything here runs against bytes a sportsbook actually sent rather than against
a hand-written approximation of them.

**Scope.** Each adapter's own parsing rules are pinned in
``tests/test_{fanduel,pinnacle,kambi}_adapter.py``, and the clauses every adapter
must satisfy are in ``tests/test_source_contract.py``.  This file holds only what
neither of those can see: what emerges when all three books are parsed *together*
and run through the pipeline's own reconciliation.

It used to restate each adapter's thousandths scaling, period mapping and
pitcher-conditional handling.  Those are now asserted in the per-adapter files
against far more data, so keeping them here as well was three copies of one
assertion in three places, free to drift apart.  They were removed rather than
left to rot.

Nothing here is presented as live data.  These are captures, and the counts
printed below describe that capture, not today's slate.
"""
from __future__ import annotations

import collections

import pytest

from src.arb import find_opportunities
from src.events import reconcile_event_keys
from src.leagues import league
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Sport
from src.validation import Severity, validate
from src.vocab import CORE_MARKETS_BY_SPORT, SELECTIONS_BY_MARKET, draw_is_priced


@pytest.fixture(scope="module")
def replayed(all_fixture_quotes: list[Quote]) -> list[Quote]:
    """The captured slate after cross-source event reconciliation.

    Reconciliation belongs to the pipeline, not to any adapter, so the rows an
    adapter emits are not the rows anything downstream sees.  Replaying without it
    would exercise a shape that never reaches the store.
    """
    quotes, _ = reconcile_event_keys(all_fixture_quotes)
    return quotes


class TestCoverage:
    def test_every_sport_is_present_in_the_capture(self, replayed) -> None:
        """A sport dropping out of the fixtures is either a lost capture or an
        adapter regression, and either way every cross-book claim below becomes
        vacuously true for it."""
        found = {quote.sport for quote in replayed}
        missing = sorted(s.value for s in set(Sport) - found)
        assert not missing, f"no rows for {missing}"

    def test_every_registered_source_contributed(self, replayed) -> None:
        """Set equality against the **registry**, not against a list of names.

        A literal here says "these three books" and goes stale the moment a
        fourth is added — quietly, because a set of three still compares equal to
        itself.  Derived from the registry, adding a source that produces nothing
        fails here instead of looking like a source that was never asked for.
        """
        from src.sources import registry

        assert {quote.source for quote in replayed} == set(registry.keys())

    def test_each_sport_has_its_core_markets_from_some_book(self, replayed) -> None:
        """No single book prices everything, but the slate as a whole must cover a
        sport's core markets or the sport is not really collected."""
        seen: dict[Sport, set[Market]] = collections.defaultdict(set)
        for quote in replayed:
            if quote.period is Period.FULL_GAME:
                seen[quote.sport].add(quote.market)
        for sport, expected in CORE_MARKETS_BY_SPORT.items():
            missing = sorted(m.value for m in expected - seen[sport])
            assert not missing, f"{sport.value} is missing {missing}"

    def test_the_per_sport_cross_book_count_is_recorded(self, replayed) -> None:
        """A record, not a threshold.

        How many books price the same event is a property of the calendar as much
        as of the code — in late July the NHL has barely any cross-book slate and
        the NBA has none at all.  Printing the numbers keeps "this sport is
        supported" an evidenced claim rather than an assumed one.
        """
        per_sport: dict[Sport, dict[str, set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        for quote in replayed:
            per_sport[quote.sport][quote.event_key].add(quote.source)
        for sport, events in sorted(per_sport.items(), key=lambda kv: kv[0].value):
            shared = [e for e, books in events.items() if len(books) >= 2]
            books = sorted({b for bs in events.values() for b in bs})
            print(
                f"{sport.value:<11} {len(events):>4} events, "
                f"{len(shared):>4} priced by 2+ books, from {books}"
            )


class TestCrossBookIdentity:
    def test_books_sharing_an_event_agree_on_the_participants(self, replayed) -> None:
        """The join only means something if the books mean the same fixture.

        Where home advantage is real they must agree on which side is home; in
        tennis the ordering is imposed by ``src.events.orient`` and the books have
        no opinion, so only the pair is compared.
        """
        by_event: dict[str, dict[str, tuple[str, ...]]] = collections.defaultdict(dict)
        for quote in replayed:
            if league(quote.league).has_home_away:
                identity: tuple[str, ...] = (quote.home_participant, quote.away_participant)
            else:
                identity = tuple(sorted((quote.home_participant, quote.away_participant)))
            by_event[quote.event_key][quote.source] = identity
        for event_key, per_source in by_event.items():
            assert len(set(per_source.values())) == 1, f"{event_key}: {per_source}"

    def test_books_sharing_an_event_agree_on_the_sport(self, replayed) -> None:
        by_event: dict[str, set[Sport]] = collections.defaultdict(set)
        for quote in replayed:
            by_event[quote.event_key].add(quote.sport)
        for event_key, sports in by_event.items():
            assert len(sports) == 1, f"{event_key} spans {sports}"

    def test_an_event_key_holds_one_start_time_window(self, replayed) -> None:
        """Two different fixtures under one key is the doubleheader mis-join, and
        it is silent — every row still looks individually valid."""
        by_event: dict[str, list[Quote]] = collections.defaultdict(list)
        for quote in replayed:
            by_event[quote.event_key].append(quote)
        for event_key, rows in by_event.items():
            times = {q.commence_time for q in rows}
            tolerance = max(league(q.league).same_event_tolerance for q in rows)
            assert max(times) - min(times) <= tolerance, (
                f"{event_key} spans {max(times) - min(times)}, over the {tolerance} tolerance"
            )

    def test_books_sharing_an_event_agree_on_the_favourite(self, replayed) -> None:
        """The check that caught a real phantom-arbitrage generator.

        Two books differ on the *size* of a price constantly; they do not disagree
        about which competitor is favoured.  A mirrored pair means one book's
        prices are attached to the wrong participant — which is what Pinnacle's
        tennis rows did on 5 of 8 matches, because the book's own home/away label
        was trusted where our ordering is imposed instead.  Every row looked
        individually valid, so only a cross-book comparison could see it.
        """
        by_event: dict[str, dict[str, dict[Selection, float]]] = collections.defaultdict(
            lambda: collections.defaultdict(dict)
        )
        for quote in replayed:
            if quote.market is not Market.MONEYLINE or quote.period is not Period.FULL_GAME:
                continue
            if quote.selection in (Selection.HOME, Selection.AWAY):
                by_event[quote.event_key][quote.source][quote.selection] = quote.decimal_odds

        # Only events with a *clear* favourite can be compared this way.  On a
        # pick'em the two books genuinely disagree about which side is a hair
        # shorter — the captured HOU@LAA has FanDuel at 1.909/1.943 and Pinnacle at
        # 1.962/1.943 — and that disagreement carries no information.  Requiring a
        # gap in implied probability keeps the check pointed at the failure it
        # exists for: a mirrored pair, where the tennis rows differed by 1.24
        # against 4.12.
        clear_favourite = 0.05

        def edge(prices: dict[Selection, float]) -> float:
            return 1 / prices[Selection.HOME] - 1 / prices[Selection.AWAY]

        compared = 0
        for event_key, per_source in by_event.items():
            complete = {
                src: prices
                for src, prices in per_source.items()
                if {Selection.HOME, Selection.AWAY} <= set(prices)
                and abs(edge(prices)) > clear_favourite
            }
            if len(complete) < 2:
                continue
            verdicts = {src: edge(prices) > 0 for src, prices in complete.items()}
            assert len(set(verdicts.values())) == 1, (
                f"{event_key}: books disagree on the favourite — {complete}"
            )
            compared += 1
        assert compared > 10, f"only {compared} events compared — too few to mean anything"

    def test_reconciliation_is_idempotent_on_the_capture(self, replayed) -> None:
        again, changes = reconcile_event_keys(replayed)
        assert changes == []
        assert [q.event_key for q in again] == [q.event_key for q in replayed]


class TestDeterminism:
    def test_replaying_the_same_bytes_twice_is_identical(
        self, fanduel_raw, pinnacle_raw, kambi_raw
    ) -> None:
        """Replay, regression testing and reproducing a live failure offline all
        rest on this and nothing else."""
        from src.sources.betrivers_kambi import BetRiversKambiAdapter
        from src.sources.fanduel import FanDuelAdapter
        from src.sources.pinnacle import PinnacleAdapter

        for cls, raws in (
            (FanDuelAdapter, fanduel_raw),
            (PinnacleAdapter, pinnacle_raw),
            (BetRiversKambiAdapter, kambi_raw),
        ):
            adapter = cls()
            try:
                first = [q.model_dump() for q in adapter.parse(raws).quotes]
                second = [q.model_dump() for q in adapter.parse(raws).quotes]
            finally:
                adapter.close()
            assert first == second, cls.__name__

    def test_every_row_is_traceable_to_stored_bytes(
        self, all_fixture_quotes, registered_raws
    ) -> None:
        """Both provenance refs must resolve by plain membership, so an orphaned
        row is a failure rather than something a reader has to go and check."""
        stored = {raw.ref for raws in registered_raws.values() for raw in raws}
        for quote in all_fixture_quotes:
            assert quote.raw_ref in stored, quote.raw_ref
            if quote.identity_raw_ref is not None:
                assert quote.identity_raw_ref in stored, quote.identity_raw_ref


@pytest.fixture(scope="module")
def declared_markets() -> dict[tuple[str, str], frozenset]:
    """What each registered source says it prices, as the collector supplies it.

    Validation has to be given this or it holds every source to its sport's full
    core market set — which is right for a sportsbook and wrong for a venue that
    prices one market by design.  Smarkets is the live case: its published rate
    limit will not support fetching the totals and handicaps, so it collects the
    moneyline alone and *says so*, and without the claim its missing spreads are
    reported as "a source label has probably changed".
    """
    from src.sources import registry

    claims: dict[tuple[str, str], frozenset] = {}
    for descriptor in registry.SOURCES:
        adapter = descriptor.replay_instance()
        try:
            for league, markets in adapter.capabilities().items():
                claims[(descriptor.key, league)] = markets
        finally:
            adapter.close()
    return claims


class TestValidationOnTheRealSlate:
    def test_the_captured_slate_has_no_validation_errors(
        self, replayed, declared_markets
    ) -> None:
        """The end-to-end assertion this file exists for.

        An ERROR means the data is wrong or unusable, so a real capture producing
        one is a parser fault rather than something to tune away.  Warnings are
        allowed: a book legitimately prices markets another does not.
        """
        report = validate(replayed, capabilities=declared_markets)
        errors = [f for f in report.findings if f.severity is Severity.ERROR]
        assert errors == [], "\n".join(str(f) for f in errors[:10])

    def test_no_book_prices_a_complete_market_to_lose(self, replayed) -> None:
        """Implied probabilities summing below 1.0 at one book means the prices or
        the lines were mispaired.  It is the signature of a parser fault, and it is
        also exactly what a phantom arbitrage looks like."""
        grouped: dict[tuple[str, str, str], list[Quote]] = collections.defaultdict(list)
        for quote in replayed:
            grouped[quote.market_key].append(quote)

        checked = 0
        for market_key, rows in grouped.items():
            if any(r.status is not QuoteStatus.ACTIVE for r in rows):
                continue
            expected = set(SELECTIONS_BY_MARKET[rows[0].market])
            if rows[0].market is Market.MONEYLINE and not draw_is_priced(
                rows[0].sport, rows[0].period
            ):
                expected.discard(Selection.DRAW)
            if {r.selection for r in rows} != expected:
                continue
            overround = sum(r.implied_probability for r in rows)
            assert overround >= 1.0 - 1e-9, f"{market_key} sums to {overround:.4f}"
            checked += 1
        assert checked > 100, f"only {checked} complete markets checked — too few to mean anything"


class TestArbitrageOnTheRealSlate:
    def test_the_captured_slate_is_reported_honestly(self, replayed) -> None:
        """Three correctly-priced books produce no arbitrage, and that is the
        expected result rather than a bug.

        What matters is *why*.  "No opportunities" because nothing was mispriced
        and "no opportunities" because nothing was comparable are entirely
        different claims, and only the first one says the pipeline works — so the
        comparable count is asserted, not just the opportunity count.
        """
        report = find_opportunities(replayed)
        print(
            f"comparable cross-book markets: {report.comparable_group_count}, "
            f"opportunities: {len(report.opportunities)}, "
            # ``ArbReport`` has no ``refusals``; this was guarded on
            # ``hasattr`` and so printed a constant 0 forever.  The diagnostics
            # are the thing that records why a market was turned down.
            f"refused: {len(report.diagnostics)}"
        )
        assert report.comparable_group_count > 0, (
            "no market was comparable across books, so 'no arbitrage' means nothing"
        )
