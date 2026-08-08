"""Intentional failover pairs stay registered and never arb against themselves."""
from __future__ import annotations

from src.arb import (
    EVERY_LEAGUE,
    _independent_source_count,
    counterparty_groups,
    find_opportunities,
    merge_counterparty_groups,
)
from src.collector import _check_distinctness
from src.redundancy import REDUNDANT_PAIRS, check_redundancy, is_redundant_pair
from src.schema import Selection
from src.validation import Severity, ValidationReport
from tests.conftest import make_quote


def _ml(
    source: str,
    event_key: str,
    selection: Selection,
    decimal_odds: float,
    *,
    league: str = "MLB",
):
    return make_quote(
        source=source,
        event_key=event_key,
        league=league,
        selection=selection,
        decimal_odds=decimal_odds,
        home_participant="MLB-HOME",
        away_participant="MLB-AWAY",
        home_team="Home",
        away_team="Away",
        source_event_id=event_key,
        raw_ref=f"{source}/raw",
    )


def _pair_slate(primary: str, secondary: str, *, identical: bool) -> list:
    quotes = []
    for i in range(25):
        event = f"MLB-AWAY{i}@MLB-HOME{i}:2026-08-01"
        quotes.append(_ml(primary, event, Selection.HOME, 1.91))
        quotes.append(_ml(primary, event, Selection.AWAY, 1.91))
        secondary_home = 1.91 if identical else 2.10
        quotes.append(_ml(secondary, event, Selection.HOME, secondary_home))
        quotes.append(
            _ml(secondary, event, Selection.AWAY, 1.91 if identical else 1.80)
        )
    return quotes


class TestRedundantPairsDeclared:
    def test_every_pair_names_registered_sources(self) -> None:
        from src.sources import registry

        for primary, secondary in REDUNDANT_PAIRS:
            assert primary in registry.BY_KEY, primary
            assert secondary in registry.BY_KEY, secondary
            assert secondary.startswith(("an_", "vi_", "vsin_")), secondary

    def test_lookup_is_symmetric(self) -> None:
        assert is_redundant_pair("fanduel", "an_fanduel")
        assert is_redundant_pair("an_fanduel", "fanduel")
        assert is_redundant_pair("an_draftkings", "vi_draftkings")
        assert not is_redundant_pair("fanduel", "pinnacle")


class TestDistinctnessExemptsFailover:
    def test_identical_failover_pair_is_not_a_registration_error(self) -> None:
        report = ValidationReport()
        _check_distinctness(_pair_slate("fanduel", "an_fanduel", identical=True), report)
        codes = {finding.code for finding in report.findings}
        assert "sources_are_one_counterparty" not in codes

    def test_accidental_mirror_is_still_an_error(self) -> None:
        report = ValidationReport()
        _check_distinctness(
            _pair_slate("betrivers_kambi", "leovegas_kambi", identical=True), report
        )
        codes = {finding.code for finding in report.findings}
        assert "sources_are_one_counterparty" in codes


class TestRedundancyFlags:
    def test_disagreement_is_a_warning(self) -> None:
        report = ValidationReport()
        check_redundancy(_pair_slate("bovada", "an_bovada", identical=False), report)
        codes = [finding.code for finding in report.findings]
        assert "redundant_sources_disagree" in codes
        assert all(f.severity is Severity.WARNING for f in report.findings)

    def test_silent_secondary_on_disjoint_slate_is_not_a_warning(self) -> None:
        report = ValidationReport()
        quotes = [
            _ml("bovada", f"MLB-A{i}@MLB-H{i}:2026-08-01", Selection.HOME, 1.91)
            for i in range(5)
        ]
        check_redundancy(quotes, report)
        assert report.findings == []


class TestArbTreatsFailoverAsOneBook:
    def test_three_paths_for_one_book_never_form_arb_legs(self) -> None:
        event = "MLB-AWAY@MLB-HOME:2026-08-01"
        quotes = [
            _ml(source, event, selection, odds)
            for source, selection, odds in (
                ("draftkings", Selection.HOME, 1.80),
                ("draftkings", Selection.AWAY, 2.05),
                ("an_draftkings", Selection.HOME, 2.30),
                ("an_draftkings", Selection.AWAY, 1.70),
                ("vi_draftkings", Selection.HOME, 2.20),
                ("vi_draftkings", Selection.AWAY, 1.75),
            )
        ]
        report = find_opportunities(quotes)
        assert report.opportunities == []

    def test_declared_pair_is_not_in_measured_union_find(self) -> None:
        # Thick agreeing slate: distinctness would call them MIRROR, but they
        # must still stay out of the transitive counterparty graph.
        quotes = _pair_slate("fanduel", "an_fanduel", identical=True)
        groups = counterparty_groups(quotes).get(EVERY_LEAGUE, [])
        assert frozenset({"fanduel", "an_fanduel"}) not in groups

    def test_secondary_is_ignored_when_primary_covers_the_contract(self) -> None:
        quotes = _pair_slate("fanduel", "an_fanduel", identical=False)
        # Disagreement would look like edge if both stayed eligible; demotion
        # keeps only the primary, so no fanduel/an_fanduel opportunity.
        report = find_opportunities(quotes)
        for opp in report.opportunities:
            assert "an_fanduel" not in {leg.source for leg in opp.legs}

    def test_partial_primary_still_finds_primary_vs_third_with_an_present(self) -> None:
        # FanDuel away-only; AN both sides (and treated as pinnacle's mirror);
        # must still recover FanDuel/Pinnacle, not return None from the switch.
        event = "MLB-AWAY@MLB-HOME:2026-08-01"
        quotes = [
            _ml("fanduel", event, Selection.AWAY, 2.10),
            _ml("an_fanduel", event, Selection.HOME, 2.20),
            _ml("an_fanduel", event, Selection.AWAY, 1.75),
            _ml("pinnacle", event, Selection.HOME, 1.91),
            _ml("pinnacle", event, Selection.AWAY, 2.00),
        ]
        report = find_opportunities(
            quotes,
            one_counterparty={
                EVERY_LEAGUE: [frozenset({"an_fanduel", "pinnacle"})],
            },
        )
        assert report.opportunities, "partial primary must still arb vs third book"
        sources = {leg.source for opp in report.opportunities for leg in opp.legs}
        assert "fanduel" in sources and "pinnacle" in sources

    def test_republished_three_way_secondary_remains_diagnostic_only(self) -> None:
        from src.schema import Sport

        # Healthy overrounds per book; AN home + Pinnacle draw/away has edge.
        # FanDuel is two-way only (same shape demotion must not apply).
        event = "EPL-A@EPL-B:2026-08-01"
        quotes = []
        for source, priced in (
            ("fanduel", ((Selection.HOME, 2.05), (Selection.AWAY, 1.90))),
            (
                "an_fanduel",
                (
                    (Selection.HOME, 2.30),
                    (Selection.AWAY, 3.40),
                    (Selection.DRAW, 3.50),
                ),
            ),
            (
                "pinnacle",
                (
                    (Selection.HOME, 2.10),
                    (Selection.AWAY, 3.90),
                    (Selection.DRAW, 3.50),
                ),
            ),
        ):
            for selection, odds in priced:
                quotes.append(
                    make_quote(
                        source=source,
                        event_key=event,
                        league="EPL",
                        sport=Sport.SOCCER,
                        selection=selection,
                        decimal_odds=odds,
                        home_participant="EPL-B",
                        away_participant="EPL-A",
                        home_team="Home",
                        away_team="Away",
                        source_event_id=event,
                        raw_ref=f"{source}/raw",
                    )
                )
        report = find_opportunities(quotes)
        assert not any(
            "an_fanduel" in {leg.source for leg in opp.legs}
            for opp in report.opportunities
        ), "republished prices must never become executable arb legs"

    def test_no_arb_between_book_and_its_republisher(self) -> None:
        quotes = _pair_slate("fanduel", "an_fanduel", identical=False)
        report = find_opportunities(quotes)
        for opp in report.opportunities:
            sources = {leg.source for leg in opp.legs}
            assert sources != {"fanduel", "an_fanduel"}

    def test_declared_pair_does_not_transitively_collapse_third_book(self) -> None:
        # Measured mirror only links AN BetRivers with LeoVegas.
        behind = {"an_betrivers": "an_betrivers", "leovegas_kambi": "an_betrivers"}
        assert (
            _independent_source_count(("betrivers_kambi", "leovegas_kambi"), behind)
            == 2
        )
        assert (
            _independent_source_count(("betrivers_kambi", "an_betrivers"), behind) == 1
        )

    def test_assignment_cannot_stitch_both_failover_feeds(self) -> None:
        # Incomplete primary 3-way leaves AN up; stitching FD home + AN draw +
        # third away would monetize feed disagreement.
        from src.schema import Sport

        event = "EPL-A@EPL-B:2026-08-01"
        quotes = []
        for source, priced in (
            ("fanduel", ((Selection.HOME, 2.20), (Selection.AWAY, 3.40))),
            (
                "an_fanduel",
                (
                    (Selection.HOME, 2.10),
                    (Selection.AWAY, 3.40),
                    (Selection.DRAW, 4.20),
                ),
            ),
            (
                "pinnacle",
                (
                    (Selection.HOME, 2.10),
                    (Selection.AWAY, 4.00),
                    (Selection.DRAW, 3.50),
                ),
            ),
        ):
            for selection, odds in priced:
                quotes.append(
                    make_quote(
                        source=source,
                        event_key=event,
                        league="EPL",
                        sport=Sport.SOCCER,
                        selection=selection,
                        decimal_odds=odds,
                        home_participant="EPL-B",
                        away_participant="EPL-A",
                        home_team="Home",
                        away_team="Away",
                        source_event_id=event,
                        raw_ref=f"{source}/raw",
                    )
                )
        report = find_opportunities(quotes)
        for opp in report.opportunities:
            legs = {leg.source for leg in opp.legs}
            assert not ({"fanduel", "an_fanduel"} <= legs)

    def test_merge_strips_stale_recorded_failover_edges(self) -> None:
        # Pre-fix runs may have recorded {fanduel, an_fanduel}. Merging that
        # with a measured AN–third mirror must not collapse fanduel with third.
        recorded = {
            EVERY_LEAGUE: [frozenset({"fanduel", "an_fanduel"})],
        }
        measured = {
            EVERY_LEAGUE: [frozenset({"an_fanduel", "leovegas_kambi"})],
        }
        merged = merge_counterparty_groups(measured, recorded)
        groups = merged[EVERY_LEAGUE]
        assert frozenset({"fanduel", "an_fanduel"}) not in groups
        assert frozenset({"an_fanduel", "leovegas_kambi"}) in groups


class TestOfflinePrimaryNamesOnlyALocalFailover:
    """A national board is context for a dark state book, never its stand-in.

    ``primary_source_offline`` used to say "Using the registered republisher as
    failover for betmgm" whichever republisher had rows.  On a state run whose
    first-party route was geo-blocked, that put the substitution the coverage
    rule refuses — a Las Vegas column standing in for the state licence — into
    the validation report, in the voice of the module a reader trusts to explain
    the gap.  Both tests fail on the pre-fix single-branch message.

    These two pass no ``state``, so they exercise the weaker
    ``files_per_state_book_id`` fallback — "does this feed file per-state ids
    anywhere" — which is the strongest question available when the caller does not
    know the jurisdiction.  The state-aware predicate is covered by
    ``TestOneOfflineBookGetsOneAnswer``, which passes ``state="PA"``.
    """

    def _quotes(self, source: str):
        return [
            _ml(source, f"MLB-A@MLB-H:2026-08-06-{index}", selection, price)
            for index in range(30)
            for selection, price in (
                (Selection.HOME, 1.90),
                (Selection.AWAY, 2.05),
            )
        ]

    def _codes(self, source: str) -> list[str]:
        report = ValidationReport(quote_count=0, event_count=0, source_count=0)
        check_redundancy(self._quotes(source), report)
        return [finding.code for finding in report.findings]

    def test_a_state_licence_republisher_is_named_as_failover(self) -> None:
        """``an_betmgm`` is asked for a per-state book id, so it is failover."""
        assert "primary_source_offline" in self._codes("an_betmgm")

    def test_a_national_board_is_refused_as_failover(self) -> None:
        """``vi_betmgm`` is VegasInsider's Las Vegas column, so it is not."""
        codes = self._codes("vi_betmgm")
        assert "primary_source_offline_no_local_failover" in codes
        assert "primary_source_offline" not in codes


class TestOneOfflineBookGetsOneAnswer:
    """Six books have two mirrors, and the per-pair loop answered twice.

    With the first-party route geo-blocked and both republishers up — the case the
    locality branch was written for — the report carried, adjacent:

        primary_source_offline: draftkings produced no rows while an_draftkings
            priced the slate. Using the registered republisher as failover
        primary_source_offline_no_local_failover: draftkings produced no rows.
            vi_draftkings ... is context and not failover — draftkings is unobserved

    The second sentence is false in exactly the situation that produces it, and
    both were addressed to the same book. Whether a book has failover is a
    property of the book, so it is decided once.
    """

    def _quotes(self, *sources: str):
        return [
            _ml(source, f"MLB-A@MLB-H:2026-08-06-{index}", selection, price)
            for source in sources
            for index in range(30)
            for selection, price in (
                (Selection.HOME, 1.90),
                (Selection.AWAY, 2.05),
            )
        ]

    def _findings(self, *sources: str, state: str | None = "PA"):
        report = ValidationReport(quote_count=0, event_count=0, source_count=0)
        check_redundancy(self._quotes(*sources), report, state=state)
        return [f for f in report.findings if f.source == "draftkings"]

    def test_both_mirrors_live_yields_one_finding_and_it_is_the_true_one(self):
        findings = self._findings("an_draftkings", "vi_draftkings")
        codes = [f.code for f in findings]

        assert codes == ["primary_source_offline"], codes
        # And it names the feed that actually stands in, not the Las Vegas column
        # beside it — naming that one here is the substitution being refused.
        assert "an_draftkings" in findings[0].message
        assert "vi_draftkings" not in findings[0].message
        assert "is unobserved" not in findings[0].message

    def test_only_the_national_mirror_live_still_refuses_it(self):
        """The regrouping must not turn the locality branch off."""
        findings = self._findings("vi_draftkings")
        assert [f.code for f in findings] == [
            "primary_source_offline_no_local_failover"
        ]
        # It says the mirror is not failover, and stops there. "is unobserved" was
        # ``src.coverage``'s sentence, reported there at ERROR; both fired on one
        # book in one run, a word apart and a severity apart, so the exit status
        # turned on which module spoke. One claim, one owner.
        assert "context and not failover" in findings[0].message
        assert "vi_draftkings (60 quotes)" in findings[0].message
        assert "unobserved" not in findings[0].message

    def test_only_the_local_mirror_live_is_failover(self):
        findings = self._findings("an_draftkings")
        assert [f.code for f in findings] == ["primary_source_offline"]
