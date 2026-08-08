"""The mirror gate, against payloads captured from real platform tenants.

A mirror is invisible to every other check in the pipeline.  It satisfies the
source contract, emits valid rows, raises the cross-source coverage counts, and
clears ``require_distinct_sources`` — because that compares source *keys*, and
two keys is exactly what a mirror has.  What it does not have is two
counterparties, so an "arbitrage" between the two is a position nobody can hold.

The fixtures under ``tests/fixtures/mirrors`` are three Kambi tenants captured
within two seconds of each other on 2026-07-28: BetRivers Illinois, BetRivers
New Jersey, and LeoVegas.  The first two returned a **byte-identical** listView —
same sha256 — which is what a licence of one book looks like from outside.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.distinctness import (
    MIN_SHARED_SELECTIONS,
    Agreement,
    Verdict,
    compare_all,
    compare_sources,
    find_mirrors,
    screen_candidate,
)
from src.events import reconcile_event_keys
from src.raw_store import RawStore
from src.schema import Market, Selection
from src.sources.betrivers_kambi import BetRiversKambiAdapter
from tests.conftest import FIXTURE_DATE, FIXTURE_RAW_DIR, make_quote

MIRROR_DIR = Path(__file__).parent / "fixtures" / "mirrors"

#: The three tenants, and where each one's capture lives.
TENANTS = {
    "betrivers_kambi_il_sample": MIRROR_DIR,
    "betrivers_kambi_nj": MIRROR_DIR,
    "leovegas_kambi": FIXTURE_RAW_DIR,
}


@pytest.fixture(scope="module")
def tenant_quotes():
    """Rows from all three tenants, reconciled as one run would be."""
    quotes = []
    for source, directory in TENANTS.items():
        store = RawStore(directory)
        paths = sorted(directory.glob(f"{source}__*.json"))
        assert paths, f"no captured payloads for {source} in {directory}"
        adapter = BetRiversKambiAdapter()
        try:
            outcome = adapter.parse([store.read(path) for path in paths])
        finally:
            adapter.close()
        assert outcome.rejections == [], f"{source}: {outcome.rejections[:2]}"
        quotes.extend(outcome.quotes)
    reconciled, _ = reconcile_event_keys(quotes)
    return reconciled


class TestAgainstRealTenants:
    def test_two_licences_of_one_book_are_flagged_as_a_mirror(self, tenant_quotes) -> None:
        """BetRivers Illinois against BetRivers New Jersey.

        Registering both would let the arbitrage engine report a position between
        BetRivers and BetRivers, at the top of the report, indistinguishable from
        a real one.
        """
        pair = compare_sources(
            tenant_quotes, "betrivers_kambi_il_sample", "betrivers_kambi_nj"
        )
        assert pair.compared >= MIN_SHARED_SELECTIONS
        assert pair.verdict is Verdict.MIRROR
        assert pair.rate == pytest.approx(1.0)
        assert pair.verdict.blocks_registration

    def test_a_genuinely_different_tenant_is_not(self, tenant_quotes) -> None:
        """LeoVegas is the same platform and a different book: same fixtures,
        its own prices.  A gate that refused it would leave the source list at
        three books forever."""
        for other in ("betrivers_kambi_il_sample", "betrivers_kambi_nj"):
            pair = compare_sources(tenant_quotes, "leovegas_kambi", other)
            assert pair.compared >= MIN_SHARED_SELECTIONS
            assert pair.verdict is Verdict.DISTINCT, pair.summary()
            assert not pair.verdict.blocks_registration

    def test_the_sweep_finds_exactly_the_one_mirrored_pair(self, tenant_quotes) -> None:
        mirrors = find_mirrors(tenant_quotes)
        assert [
            tuple(sorted((pair.source_a, pair.source_b))) for pair in mirrors
        ] == [("betrivers_kambi_il_sample", "betrivers_kambi_nj")]

    def test_screening_a_candidate_names_the_source_it_mirrors(self, tenant_quotes) -> None:
        """A rejected candidate is worth writing down *with its partner*, or it
        gets rediscovered and re-added six months later."""
        candidate = [q for q in tenant_quotes if q.source == "betrivers_kambi_nj"]
        registered = [q for q in tenant_quotes if q.source != "betrivers_kambi_nj"]
        results = screen_candidate(candidate, registered)
        assert results[0].verdict is Verdict.MIRROR
        assert results[0].source_b == "betrivers_kambi_il_sample"

    def test_comparison_is_symmetric_in_its_verdict(self, tenant_quotes) -> None:
        forward = compare_sources(tenant_quotes, "leovegas_kambi", "betrivers_kambi_nj")
        backward = compare_sources(tenant_quotes, "betrivers_kambi_nj", "leovegas_kambi")
        assert forward.rate == backward.rate
        assert forward.compared == backward.compared


class TestTheRuleItself:
    def _book(self, source: str, prices: dict[Selection, float], event: str = "e1"):
        return [
            make_quote(
                source=source,
                selection=selection,
                decimal_odds=odds,
                market=Market.MONEYLINE,
                event_key=f"MLB-PHI@MLB-MIA:2026-07-2{event[-1]}",
                source_event_id=f"{source}-{event}",
                source_market_id=f"{source}-{event}-m",
            )
            for selection, odds in prices.items()
        ]

    def test_a_thin_overlap_yields_no_verdict_rather_than_a_pass(self) -> None:
        """Three selections agreeing three times is what a one-fixture overlap
        looks like, not evidence of anything.  Calling it a mirror would refuse a
        real book on a quiet day; calling it distinct would wave one through."""
        quotes = [
            *self._book("a", {Selection.HOME: 1.9, Selection.AWAY: 2.0}),
            *self._book("b", {Selection.HOME: 1.9, Selection.AWAY: 2.0}),
        ]
        pair = compare_sources(quotes, "a", "b")
        assert pair.rate == 1.0
        assert pair.verdict is Verdict.UNDECIDED
        assert not pair.verdict.blocks_registration
        assert "no verdict" in pair.summary()

    def test_undecided_is_not_treated_as_a_mirror_by_the_sweep(self) -> None:
        quotes = [
            *self._book("a", {Selection.HOME: 1.9, Selection.AWAY: 2.0}),
            *self._book("b", {Selection.HOME: 1.9, Selection.AWAY: 2.0}),
        ]
        assert find_mirrors(quotes) == []

    def test_occasional_agreement_is_not_a_mirror(self) -> None:
        """-110 is the most common number in the industry; two books landing on
        it together is not evidence of one counterparty."""
        agreement = Agreement(
            source_a="a", source_b="b", compared=100, identical=60
        )
        assert agreement.verdict is Verdict.DISTINCT

    def test_near_total_agreement_is(self) -> None:
        """Not 1.0: one tenant of a platform can differ on a handful of prices —
        a stale cache, a market suspended in one jurisdiction — and still be the
        same book."""
        assert Agreement("a", "b", compared=100, identical=96).verdict is Verdict.MIRROR
        assert Agreement("a", "b", compared=100, identical=94).verdict is Verdict.DISTINCT

    def test_differences_are_reported_so_a_human_can_check(self) -> None:
        quotes = [
            *self._book("a", {Selection.HOME: 1.90, Selection.AWAY: 2.00}),
            *self._book("b", {Selection.HOME: 1.95, Selection.AWAY: 2.00}),
        ]
        pair = compare_sources(quotes, "a", "b")
        assert pair.examples and pair.examples[0][1:] == (1.90, 1.95)

    def test_screening_refuses_a_mixed_candidate(self) -> None:
        """The caller is asking about *one* source; two would silently average
        two different answers into one verdict."""
        quotes = [*self._book("a", {Selection.HOME: 1.9}), *self._book("b", {Selection.HOME: 1.9})]
        with pytest.raises(ValueError):
            screen_candidate(quotes, [])

    def test_only_the_moneyline_is_compared(self) -> None:
        """Adding markets only some books offer would let the *overlap* rather
        than the *agreement* decide the answer."""
        spreads = [
            make_quote(source=source, selection=selection, decimal_odds=odds,
                       market=Market.SPREAD, line=line, source_market_id=f"{source}-s")
            for source, odds in (("a", 1.9), ("b", 2.4))
            for selection, line in ((Selection.HOME, -1.5), (Selection.AWAY, 1.5))
        ]
        assert compare_sources(spreads, "a", "b").compared == 0


def test_every_registered_source_is_compared_against_every_other(tenant_quotes) -> None:
    """The sweep is a full pairwise pass, not a chain: a mirror can enter the
    registry at any point, so every pair has to be looked at."""
    pairs = compare_all(tenant_quotes)
    assert len(pairs) == 3  # three tenants -> three unordered pairs
    assert len({tuple(sorted((p.source_a, p.source_b))) for p in pairs}) == 3


class TestFrontEndsOfARegisteredBook:
    """Venues that resell an order book already collected here.

    Named as Pennsylvania sources to acquire, and rejected — not because they are
    unreachable, but because each one is a second window onto a book this
    repository already has.  Two windows are not two counterparties.

    The exclusion is pinned by name rather than measured, because **the
    measurement points the wrong way**.  ``compare_sources`` tests exact
    ``decimal_odds`` equality, so a front-end that adds its own fee to the
    displayed price makes every price differ: agreement 0%, verdict
    ``DISTINCT``, ``blocks_registration`` False.  The gate that exists to catch
    mirrors would wave these through with its strongest possible endorsement.
    """

    #: Each candidate with the registered source it fronts.
    FRONT_ENDS = {
        "robinhood": "kalshi",
        "coinbase_predictions": "kalshi",
        "playsugarhouse": "betrivers_kambi",
    }

    def test_none_of_them_is_registered(self) -> None:
        from src.sources import registry

        keys = set(registry.keys())
        for candidate, fronts in sorted(self.FRONT_ENDS.items()):
            assert candidate not in keys, (
                f"{candidate} resells {fronts}'s order book; registering it lets "
                "the engine report an arbitrage between two front-ends of one book"
            )
            assert fronts in keys, (
                f"{fronts} is the book {candidate} is excluded in favour of; if it "
                "ever leaves the registry this exclusion needs revisiting, not "
                "silently outliving its reason"
            )

    def test_a_marked_up_front_end_would_pass_the_mirror_gate(self) -> None:
        """The reason this class exists, stated in executable form.

        Same book underneath, two cents per contract on top.  Every price
        differs, so the gate reports two distinct venues.
        """
        underlying = {Selection.HOME: 1.90, Selection.AWAY: 2.10}

        def slate(source: str, markup: float) -> list:
            # A distinct event per row: the comparison keys on the event, so one
            # fixture repeated would collapse to two shared selections and the
            # verdict would be UNDECIDED for the wrong reason.
            return [
                make_quote(
                    source=source,
                    selection=selection,
                    decimal_odds=round(odds - markup, 2),
                    event_key=f"MLB-PHI@MLB-MIA-{index}:{FIXTURE_DATE}",
                    source_event_id=f"evt-{index}",
                    source_market_id=f"m{index}",
                )
                for index in range(MIN_SHARED_SELECTIONS)
                for selection, odds in underlying.items()
            ]

        book = slate("kalshi", 0.0)
        resold = slate("a_front_end", 0.02)

        measured = compare_sources([*book, *resold], "kalshi", "a_front_end")
        assert measured.compared >= MIN_SHARED_SELECTIONS
        assert measured.rate == 0.0
        assert measured.verdict is Verdict.DISTINCT
        assert not measured.verdict.blocks_registration
