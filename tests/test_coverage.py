"""No required book is trusted on one feed, or on an out-of-state one.

Two rules are pinned here, and they are separate claims.  The first is that a
book seen by exactly one republisher must not read as a book fetched
first-party.  The second is that only a feed carrying *this state's* licence
counts towards the two — a Las Vegas column is context, not corroboration.

The tests under "the rule itself" and "the locality label cannot be faked" fail
on the code that preceded their respective rules.  The rest are declaration and
wiring checks: they hold the table to the registry and the jurisdiction map, and
they pass on any code where those agree.
"""
from __future__ import annotations

import pytest

from src.coverage import (
    Access,
    Corroboration,
    REQUIRED_BOOKS,
    RequiredBook,
    _check_locality_declarations,
    check_book_coverage,
    coverage_for_state,
    locality_marking,
)
from src.jurisdictions import JURISDICTIONS, RouteStatus, jurisdiction
from src.schema import Selection
from src.sources.registry import BY_KEY, REPUBLISHED_SOURCE_KEYS, RETAIL_SOURCE_KEYS
from src.validation import Severity, ValidationReport
from tests.conftest import make_quote


def _ml(source: str, selection: Selection, decimal_odds: float, event: str = "a"):
    """One moneyline row, priced so a pair of them is a plausible market."""
    return make_quote(
        source=source,
        event_key=f"MLB-AWAY@MLB-HOME:2026-08-06-{event}",
        selection=selection,
        decimal_odds=decimal_odds,
        home_participant="MLB-HOME",
        away_participant="MLB-AWAY",
        home_team="Home",
        away_team="Away",
    )


def _book(source: str, *, events: int = 30, offset: float = 0.0):
    """Enough rows from one source to clear ``MIN_SHARED_SELECTIONS``."""
    rows = []
    for index in range(events):
        rows.append(_ml(source, Selection.HOME, 1.90 + offset, f"e{index}"))
        rows.append(_ml(source, Selection.AWAY, 2.05 + offset, f"e{index}"))
    return rows


def _find(rows, book: str):
    return next(entry for entry in rows if entry.book == book)


@pytest.fixture
def two_local_feeds(monkeypatch):
    """A substitute PA table for one book watched by two same-licence feeds.

    No shipped PA book has two, so the satisfied and disagreeing branches of the
    two-feed path are unreachable through the real table.  Testing them against
    a substitute keeps the shipped table honest — the alternative is relaxing the
    rule so the tests have something to pass on, which inverts what the tests are
    for.  ``an_caesars`` and ``an_fanatics`` are stand-ins chosen because both
    file a Pennsylvania book id, which is the property under test.
    """
    entry = RequiredBook(
        book="TwoLocalFeeds",
        direct=None,
        republishers={
            "an_caesars": Corroboration.SAME_LICENCE,
            "an_fanatics": Corroboration.SAME_LICENCE,
        },
    )
    monkeypatch.setitem(REQUIRED_BOOKS, "PA", (entry,))
    return entry


# ── the rule itself ──────────────────────────────────────────────────────────


def test_first_party_rows_satisfy_the_rule_alone():
    """A book fetched from the book needs no corroboration."""
    measured = _find(coverage_for_state(_book("fanduel"), "PA"), "FanDuel")
    assert measured.access is Access.DIRECT
    assert measured.satisfied
    assert measured.direct_rows == 60


def test_one_republisher_is_not_enough():
    """The defect this rule exists for: a single feed nothing can contradict.

    ``vi_hardrock`` alone produced the market that rendered as
    "Hard Rock Bet keeps -48.3%".  One feed is unfalsifiable, so it fails.
    """
    measured = _find(coverage_for_state(_book("an_fanatics"), "PA"), "Fanatics")
    assert measured.access is Access.SINGLE_SOURCE
    assert not measured.satisfied
    assert "nothing can contradict it" in measured.detail


def test_two_agreeing_local_republishers_satisfy_the_rule(two_local_feeds):
    """Two feeds carrying *this state's* licence, agreeing, is the second path.

    Declared through a substitute table because no PA book currently has two
    same-licence feeds — which is itself the finding recorded in
    ``docs/evidence/state-routing.md``, not a reason to weaken the rule here.
    """
    quotes = [*_book("an_caesars"), *_book("an_fanatics")]
    measured = _find(coverage_for_state(quotes, "PA"), "TwoLocalFeeds")
    assert measured.access is Access.CROSS_CHECKED
    assert measured.satisfied
    assert measured.local_republishers == ("an_caesars", "an_fanatics")
    # Also the guard for ``AGREEMENT_UNPROVEN``, which was carved out of this state
    # and must not swallow it: a thick overlap carries no "never compared" caveat.
    # It was a separate test building this same input under this same fixture.
    assert "never compared" not in measured.detail


def test_two_local_feeds_nobody_compared_do_not_satisfy_the_rule(two_local_feeds):
    """"Two feeds that agree" is not satisfied by two feeds and no comparison.

    Below ``MIN_SHARED_SELECTIONS`` the comparison returns ``UNDECIDED``, which
    ``_agreement`` reports as "no disagreement found".  Folded into
    ``CROSS_CHECKED`` that made the rule's second half satisfiable by two feeds
    nobody had compared, and the caveat explaining it was attached to a ``detail``
    string no surface renders — ``BookCoverage`` reaches no report or dashboard.

    A thin slate is the slate's fault, not the book's, so this is a WARNING and
    its own state rather than an error or a tick.
    """
    thin = [
        *_book("an_caesars", events=3),
        *_book("an_fanatics", events=3),
    ]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = _find(check_book_coverage(thin, "PA", report), "TwoLocalFeeds")

    assert measured.access is Access.AGREEMENT_UNPROVEN
    assert not measured.satisfied
    assert measured.local_republishers == ("an_caesars", "an_fanatics")
    assert "never compared" in measured.detail

    finding = next(
        f for f in report.findings if f.code == "required_book_agreement_unproven"
    )
    assert finding.severity is Severity.WARNING
    assert "fewer than the 20 needed to judge" in finding.message


def test_a_cross_licence_feed_does_not_count_towards_the_two():
    """The locality half of the rule, and the reason it is not a refinement.

    Fails on the pre-rule code, where ``an_fanatics`` + ``vi_fanatics`` read as
    ``CROSS_CHECKED``.  ``vi_fanatics`` is VegasInsider's ``/odds/las-vegas/``
    column: a different licence of the same brand, so it cannot confirm or
    contradict what Fanatics is pricing in Pennsylvania.
    """
    quotes = [*_book("an_fanatics"), *_book("vi_fanatics")]
    measured = _find(coverage_for_state(quotes, "PA"), "Fanatics")

    assert measured.access is Access.SINGLE_SOURCE
    assert not measured.satisfied
    assert measured.local_republishers == ("an_fanatics",)
    assert measured.non_local_republishers == ("vi_fanatics",)
    # Both are still shown — the out-of-state number is context, not evidence.
    assert measured.republishers == ("an_fanatics", "vi_fanatics")
    assert "context only" in measured.detail


def test_a_book_seen_only_from_out_of_state_is_an_error_not_a_thin_feed():
    """``NON_LOCAL_ONLY`` exists so this cannot read as under-corroborated.

    Nothing carrying Pennsylvania's licence priced Fanatics at all, so there is
    no PA number on the page — only a Las Vegas one wearing the same brand.
    That is a worse condition than one local feed, and is reported as such.
    """
    measured = _find(coverage_for_state(_book("vi_fanatics"), "PA"), "Fanatics")
    assert measured.access is Access.NON_LOCAL_ONLY
    assert not measured.satisfied
    assert measured.local_republishers == ()
    assert measured.access.is_unobserved_locally

    # Reported alongside a live local feed on another book, so the slate-outage
    # collapse is not what is being measured here — see the pair of tests below.
    quotes = [*_book("vi_fanatics"), *_book("an_caesars")]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)
    finding = next(
        f for f in report.findings if f.code == "required_book_non_local_only"
    )
    assert finding.severity is Severity.ERROR


def test_live_out_of_state_columns_make_the_collapse_an_error_not_an_excuse():
    """Every local feed dark while Las Vegas prices a full slate.

    Two failure modes meet here and the guard has to refuse both.  Reported per
    book it is eleven findings for one condition, which is the nightly noise the
    collapse exists to stop.  Collapsed to the WARNING it asserted a slate with no
    pre-match games left while four out-of-state feeds sat on the same page holding
    sixty rows each — a confidently false cause, which is worse than noise because
    it tells the reader to ignore it.  (That wording is gone from the WARNING now,
    for the same reason: it could not establish it.)

    So: one finding, and it says what is true.  Fails both on the pre-rule guard
    (which counted out-of-state columns and emitted seven errors) and on the
    first fix for it (which emitted the WARNING and its false cause).
    """
    quotes = [
        row
        for key in ("vi_fanduel", "vi_betmgm", "vi_caesars", "vi_fanatics")
        for row in _book(key)
    ]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)

    codes = [f.code for f in report.findings]
    # One finding for the outage, plus PlaySugarHouse — which no feed watches at
    # all, so a feed outage does not explain it and it is not collapsed over.
    assert codes == ["book_coverage_local_feeds_dark", "required_book_missing"]
    finding = report.findings[0]
    assert finding.severity is Severity.ERROR
    assert "no pre-match games" not in finding.message
    assert "cannot stand in for them" in finding.message


def test_any_live_venue_in_the_run_refuses_the_no_games_left_excuse():
    """The collapse is falsified by *the run*, not by the declared columns.

    ``pinnacle`` is in no ``REQUIRED_BOOKS`` entry, so a guard that falsified
    itself only against the declared ``vi_*`` columns could not see it — and
    Pinnacle, Kalshi, Polymarket, Bovada and sxbet all list days ahead, which is
    precisely the overnight window the collapse was written for.  A total
    Pennsylvania blackout therefore printed "nothing else priced the slate
    either" beside sixty contradicting rows, and stayed a WARNING, so the run
    exited 0.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(_book("pinnacle"), "PA", report)

    finding = next(
        f for f in report.findings if f.code == "book_coverage_local_feeds_dark"
    )
    assert finding.severity is Severity.ERROR
    assert "no pre-match games" not in finding.message
    assert "pinnacle" in finding.message


def test_the_benign_collapse_survives_for_a_genuinely_empty_run():
    """Nothing priced anything — now the *only* case this WARNING covers.

    "Genuinely empty" has to mean the run itself is empty. It previously meant
    sixty Pinnacle rows, which is a live slate by any reading.

    Once the collapse is falsified against the run rather than against the declared
    columns, that narrowing has a consequence worth stating: the overnight window
    this branch was written for no longer reaches it, because Pinnacle and the
    exchanges list days ahead and so take the ERROR path. The message must claim
    only what is left — an empty run — and not the old guess about the slate.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage([], "PA", report)

    codes = [f.code for f in report.findings]
    assert codes == ["book_coverage_unobservable", "required_book_missing"]
    assert report.findings[0].severity is Severity.WARNING
    assert "the run is empty" in report.findings[0].message
    # The cause it can no longer establish must not be asserted.
    assert "no pre-match games left" not in report.findings[0].message


def test_first_party_rows_break_the_collapse_outright():
    """300 rows from five routes is proof the slate is live.

    The collapse's whole premise is that nothing could be priced.  First-party
    rows falsify it, so the books nothing watched are genuinely unobserved and
    each is reported.  Fails on the pre-fix guard, which consulted only the
    republishers and swallowed five real gaps behind one WARNING.
    """
    quotes = [
        row
        for key in ("fanduel", "betmgm", "caesars", "draftkings", "betrivers_kambi")
        for row in _book(key)
    ]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = check_book_coverage(quotes, "PA", report)
    codes = [f.code for f in report.findings]

    assert "book_coverage_unobservable" not in codes
    assert "book_coverage_local_feeds_dark" not in codes
    assert _find(measured, "FanDuel").access is Access.DIRECT
    assert codes.count("required_book_missing") == 6


def test_one_dark_local_feed_among_live_ones_still_raises_its_own_finding():
    """The collapse must not become a way for a real local gap to hide."""
    quotes = [*_book("an_caesars"), *_book("vi_fanatics")]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)
    codes = [f.code for f in report.findings]

    assert "book_coverage_unobservable" not in codes
    assert "required_book_non_local_only" in codes


def test_a_run_asking_only_for_out_of_state_context_is_not_faulted():
    """``--source vi_fanatics`` never asked to observe Fanatics locally.

    Judgeability keys on the feeds that could *satisfy* the rule.  Keyed on every
    named feed, this run was judged and — because its only feed is non-local —
    failed at ERROR, which sets ``report.ok`` False and exits non-zero on a scope
    the operator deliberately chose.  Fails on the pre-fix membership test.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = check_book_coverage(
        _book("vi_fanatics"), "PA", report, configured=["vi_fanatics"]
    )
    assert _find(measured, "Fanatics").access is Access.NOT_REQUESTED
    # Nothing is said about Fanatics.  PlaySugarHouse still reports, because it
    # declares no feed at all — see the test below.
    #
    # Asserted on the codes rather than on the absence of the substring
    # "Fanatics": the collapse message names the lowercase source key
    # ``vi_fanatics``, so a substring check passes while the run is being
    # collapsed over — it escaped the ``configured``-narrowing mutant on that
    # accident alone.
    assert [f.code for f in report.findings] == ["required_book_missing"]
    assert "PlaySugarHouse" in report.findings[0].message


def test_the_collapse_counts_only_feeds_the_run_asked_for():
    """``watched`` must be narrowed by ``configured``, or the collapse fires wrongly.

    A run asking only for two first-party books has *no* republisher in scope, so
    there is no local feed to have gone dark and nothing for the collapse to
    explain.  Without the ``configured`` filter, ``watched`` picked up all ten of
    Pennsylvania's declared republishers — none of which the run asked for — and
    the collapse fired, replacing two ERROR-level unobserved books with one
    WARNING that asserts a false cause and counts ten feeds that were never
    requested.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage([], "PA", report, configured=["fanduel", "betmgm"])

    codes = [f.code for f in report.findings]
    assert "book_coverage_unobservable" not in codes, codes
    assert "book_coverage_local_feeds_dark" not in codes, codes
    named = {
        book
        for book in ("FanDuel", "BetMGM", "PlaySugarHouse")
        for f in report.findings
        if book in f.message
    }
    assert named == {"FanDuel", "BetMGM", "PlaySugarHouse"}, named
    assert all(
        f.severity is Severity.ERROR
        for f in report.findings
        if f.code == "required_book_missing"
    )


def test_no_feed_at_all_is_missing_not_merely_unconfirmed():
    """PlaySugarHouse has no feed, and must not read the same as a thin one."""
    measured = _find(coverage_for_state(_book("fanduel"), "PA"), "PlaySugarHouse")
    assert measured.access is Access.MISSING
    assert measured.republishers == ()


def test_two_disagreeing_local_republishers_fail_the_book(two_local_feeds):
    """Two same-licence feeds that differ is a real finding, not an average.

    Built from two *local* keys, because only those are compared: this is the
    case the agreement test exists for.
    """
    quotes = [*_book("an_caesars"), *_book("an_fanatics", offset=0.60)]
    measured = _find(coverage_for_state(quotes, "PA"), "TwoLocalFeeds")
    assert measured.access is Access.DISAGREEING
    assert not measured.satisfied

    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)
    finding = next(
        f
        for f in report.findings
        if f.code == "required_book_observations_disagree"
    )
    assert finding.severity is Severity.ERROR


def test_a_cross_licence_price_gap_never_fails_the_book():
    """A Las Vegas column differing from the state licence is not a defect.

    Holding ``vi_*`` to same-licence agreement would fault every book forever,
    which is why it is excluded from the price comparison as well as from the
    count.  Excluded from *both*, or the rule contradicts itself.
    """
    quotes = [*_book("an_caesars"), *_book("vi_caesars", offset=0.60)]
    measured = _find(coverage_for_state(quotes, "PA"), "Caesars")
    assert measured.access is Access.SINGLE_SOURCE

    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)
    assert not [
        finding
        for finding in report.findings
        if finding.code == "required_book_observations_disagree"
    ]


def test_cross_licence_corroborator_is_recorded_but_never_counted():
    """VegasInsider publishes a Las Vegas column, not the state licence."""
    entry = next(b for b in REQUIRED_BOOKS["PA"] if b.book == "Fanatics")
    assert entry.republishers["an_fanatics"] is Corroboration.SAME_LICENCE
    assert entry.republishers["an_fanatics"].is_local
    assert entry.republishers["vi_fanatics"] is Corroboration.OTHER_LICENCE
    assert not entry.republishers["vi_fanatics"].is_local


# ── the locality label cannot be faked ───────────────────────────────────────


def test_a_national_feed_cannot_be_declared_local():
    """The one-word edit this invariant exists to refuse.

    Relabelling ``vi_fanatics`` SAME_LICENCE would not make VegasInsider serve a
    Pennsylvania page; it would only let its Las Vegas column count towards the
    two-feed requirement, which is exactly the substitution the rule forbids.
    """
    faked = RequiredBook(
        book="Fanatics",
        direct=None,
        republishers={"vi_fanatics": Corroboration.SAME_LICENCE},
    )
    with pytest.raises(RuntimeError, match="files no per-state book id"):
        _check_locality_declarations({"PA": (faked,)})


def test_a_state_scoped_feed_cannot_be_discarded_as_non_local():
    """The mirror error: throwing away a real local observation."""
    discarded = RequiredBook(
        book="Fanatics",
        direct=None,
        republishers={"an_fanatics": Corroboration.OTHER_LICENCE},
    )
    with pytest.raises(RuntimeError, match="republishes PA's own licence"):
        _check_locality_declarations({"PA": (discarded,)})


def test_a_feed_with_no_licence_in_this_state_cannot_be_declared_local():
    """``AN_BOOK_KEYS`` is a weaker property than the one being claimed.

    ``an_bally`` files per-state book ids, but only a New Jersey one.  Declaring
    it local in Pennsylvania asserts a licence that does not exist, and the
    membership-only check accepted it.
    """
    from src.jurisdictions import RouteStatus, jurisdiction

    assert jurisdiction("PA").republished["an_bally"].status is RouteStatus.UNAVAILABLE
    overreaching = RequiredBook(
        book="Bally Bet",
        direct=None,
        republishers={"an_bally": Corroboration.SAME_LICENCE},
    )
    with pytest.raises(RuntimeError, match="republishes no PA licence"):
        _check_locality_declarations({"PA": (overreaching,)})


def test_a_table_keyed_in_the_wrong_case_is_refused():
    """``normalize_state`` in the invariant, the raw key at run time.

    Checking only the normalized form let ``"pa"`` pass every consistency check
    here and then match nothing in ``coverage_for_state``, which looks the table
    up by the normalized state — Pennsylvania would have reported
    ``book_coverage_not_declared`` while this invariant had just read its entries
    and pronounced them sound.
    """
    entry = RequiredBook(book="betPARX", direct=None, republishers={})
    with pytest.raises(RuntimeError, match="keyed exactly"):
        _check_locality_declarations({"pa": (entry,)})


class TestTheDirectRouteIsCheckedToo:
    """``direct`` is the stronger claim, and it used to be the unchecked one.

    ``coverage_for_state`` awards ``Access.DIRECT`` — which satisfies the rule
    outright — on ``direct_rows > 0`` alone: no second feed, no locality test.
    So whatever key sits in this field is believed, while the invariant read only
    the republishers beside it.

    Two adversarial reviews demonstrated the cost. The first: naming
    ``vi_fanatics`` as Fanatics' direct route turned ``single_source`` into
    ``direct``, and ``vi_bet365`` as theScore Bet's turned ``missing`` into
    ``direct`` — grading Pennsylvania coverage of one brand off a different
    brand's Las Vegas column. The second, after only republishers were refused:
    ``pinnacle`` as theScore Bet's route was accepted and graded ``DIRECT`` off
    an offshore book. The cases below cover both shapes, plus the boundary that
    a real route must still pass.
    """

    def _entry(self, key: str) -> RequiredBook:
        return RequiredBook(book="theScore Bet", direct=key, republishers={})

    def test_a_republisher_cannot_be_a_direct_route(self) -> None:
        """The demonstrated one. A republished board is somebody else watching."""
        with pytest.raises(RuntimeError, match="is a republisher"):
            _check_locality_declarations({"PA": (self._entry("vi_bet365"),)})
        with pytest.raises(RuntimeError, match="is a republisher"):
            _check_locality_declarations({"PA": (self._entry("an_thescore"),)})

    def test_an_unregistered_key_cannot_be_a_direct_route(self) -> None:
        """Nothing will ever store a row under it, so the book reports MISSING
        forever with no message saying why.

        The placeholder is deliberately a key no adapter will ever claim.  This
        test used ``thescore`` — the very book its sibling cases are written
        about — until that adapter was registered on 2026-08-13, at which point
        the assertion said the opposite of the truth and failed.  A name that
        could become real is not a stand-in for one that cannot.
        """
        with pytest.raises(RuntimeError, match="not a registered source"):
            _check_locality_declarations({"PA": (self._entry("no_such_book"),)})

    def test_a_venue_unreachable_from_this_state_cannot_be_a_direct_route(self) -> None:
        """The clause a first pass at this invariant missed entirely.

        Rejecting republishers left every *first-party* wrong answer open, and a
        review demonstrated the consequence: ``direct="pinnacle"`` for theScore
        Bet in PA was accepted, and ``coverage_for_state`` then reported
        ``access=direct satisfied=True`` off an offshore book with no findings.

        Three different reasons, one classifier — offshore (``pinnacle``), a real
        first-party book holding no PA licence (``hardrock``), and an offshore
        exchange (``smarkets``).
        """
        from src.sources import registry

        for key in ("pinnacle", "hardrock", "smarkets", "bovada"):
            assert key not in registry.takeable_from_state("PA")
            with pytest.raises(RuntimeError, match="not reachable from PA"):
                _check_locality_declarations({"PA": (self._entry(key),)})

    def test_a_reachable_venue_from_another_state_is_still_refused(self) -> None:
        """``hardrock`` is licensed in Illinois and not in Pennsylvania.

        The check has to be per state rather than "is this a real book", or it
        answers the question the locality rule exists to ask with a global yes.
        """
        from src.sources import registry

        assert "hardrock" in registry.takeable_from_state("IL")
        with pytest.raises(RuntimeError, match="not reachable from PA"):
            _check_locality_declarations({"PA": (self._entry("hardrock"),)})

    def test_the_real_direct_routes_still_pass(self) -> None:
        """The check must not be so strict that the shipped table cannot express
        a first-party route — eight of PA's eleven books have one.

        ``thescore`` joined on 2026-08-13, ``bet365`` on 2026-08-15 and
        ``betparx_kambi`` on 2026-08-23 (the Kambi tenant PA's own web app
        names, answered from a Philadelphia egress the same day).  All three PA
        routes were ``TEMPLATE`` when first named here, and naming an unproven
        route is safe: the table says *which* key would be the direct route,
        and ``DIRECT`` is graded only on ``direct_rows > 0``, so a route that
        has never produced cannot lift the grade on its own.  bet365 adds a
        second guarantee — its adapter refuses unless the host itself reports the
        routed state's licence, so a PA route asked from the wrong egress raises
        rather than producing rows Pennsylvania would then be graded on.
        """
        declared = {
            entry.direct for entry in REQUIRED_BOOKS["PA"] if entry.direct is not None
        }
        assert declared == {
            "fanduel", "betrivers_kambi", "draftkings", "betmgm", "caesars",
            "thescore", "bet365", "betparx_kambi",
        }
        for key in sorted(declared):
            _check_locality_declarations(
                {"PA": (RequiredBook(book="x", direct=key, republishers={}),)}
            )

    def test_a_first_party_route_the_operator_plans_to_add_would_pass(self) -> None:
        """Guarding against the opposite failure: an invariant so strict that the
        next real adapter cannot be declared.

        Written when theScore Bet and betPARX were both still due a first-party
        Pennsylvania route; both have one now (``thescore`` since 2026-08-13,
        ``betparx_kambi`` since 2026-08-23).  The shape is still checked with the
        longest-registered Kambi book, because the invariant is about the
        declaration's shape, not about which adapter happens to be newest.
        """
        _check_locality_declarations(
            {
                "PA": (
                    RequiredBook(
                        book="BetRivers", direct="betrivers_kambi", republishers={}
                    ),
                )
            }
        )


def test_the_shipped_table_satisfies_the_invariant():
    """Import already ran this; pin it so a later edit cannot pass silently."""
    _check_locality_declarations(REQUIRED_BOOKS)


def test_no_pa_book_yet_has_two_same_licence_feeds():
    """Guards the written record, not the rule.

    ``docs/evidence/state-routing.md`` records that the two-feed path is currently
    unreachable for every Pennsylvania book — one Action Network id each, and
    VegasInsider's Las Vegas column is a different licence.  ``two_local_feeds``
    exists as a substitute table precisely because of that.

    The day a real second PA-licensed feed is added this fails, which is the
    point: the doc, the substitute fixture, and this test all become stale in the
    same commit and the failure says so rather than letting the record rot.
    """
    doubled = {
        entry.book: sorted(
            key
            for key, kind in entry.republishers.items()
            if kind is Corroboration.SAME_LICENCE
        )
        for entry in REQUIRED_BOOKS["PA"]
    }
    assert doubled, "the PA table went empty; this test would pass vacuously"
    with_two = {book: feeds for book, feeds in doubled.items() if len(feeds) >= 2}
    assert not with_two, (
        "a PA book now has two same-licence feeds: update the Pennsylvania "
        "section of docs/evidence/state-routing.md and reconsider whether the "
        f"two_local_feeds substitute table is still needed — {with_two}"
    )


# ── how failures surface ─────────────────────────────────────────────────────


def test_missing_book_is_an_error_and_single_source_is_a_warning():
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(_book("an_fanatics"), "PA", report)
    by_code = {finding.code: finding for finding in report.findings}

    assert by_code["required_book_single_source"].severity is Severity.WARNING
    assert by_code["required_book_missing"].severity is Severity.ERROR
    # The message has to name the rule, or a reader cannot tell what to fix.
    assert "two agreeing republishers" in by_code["required_book_missing"].message


def test_the_cross_licence_clause_only_appears_where_one_was_seen():
    """A book nothing reached must not read as one whose out-of-state number was weighed.

    The clause is the point of the rule where a Las Vegas column *did* answer,
    and misdirection where none did: appended unconditionally, every
    ``required_book_missing`` row invited the reader to look for a cross-licence
    feed that had never been in the picture.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage([*_book("an_fanatics"), *_book("vi_bet365")], "PA", report)
    by_code = {finding.code: finding for finding in report.findings}

    # BetMGM: no feed of any licence produced a row.
    missing = by_code["required_book_missing"].message
    assert "two agreeing republishers" in missing
    assert "another state's licence" not in missing

    # bet365: seen, but only from outside Pennsylvania — the clause explains it.
    non_local = by_code["required_book_non_local_only"].message
    assert "another state's licence never counts towards the two" in non_local

    # And the same holds for the thin-feed warning, where the one feed is local.
    single = by_code["required_book_single_source"].message
    assert "another state's licence" not in single


def test_undeclared_state_warns_rather_than_passing_silently():
    """An empty checklist must not render as a clean one."""
    assert "NJ" not in REQUIRED_BOOKS
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    assert check_book_coverage(_book("fanduel"), "NJ", report) == ()
    codes = [finding.code for finding in report.findings]
    assert codes == ["book_coverage_not_declared"]


def test_no_state_checks_nothing():
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    assert check_book_coverage(_book("fanduel"), None, report) == ()
    assert report.findings == []


# ── the declaration is wired to something real ───────────────────────────────


@pytest.mark.parametrize("state", sorted(REQUIRED_BOOKS))
def test_every_declared_state_is_a_known_jurisdiction(state):
    assert state in JURISDICTIONS


@pytest.mark.parametrize(
    ("book", "source_key"),
    [
        (book.book, key)
        for entry in REQUIRED_BOOKS.values()
        for book in entry
        for key in (*( (book.direct,) if book.direct else () ), *book.republishers)
    ],
)
def test_every_named_source_is_registered(book, source_key):
    """A typo'd key would otherwise read as a permanently missing book."""
    assert source_key in BY_KEY, f"{book} names unregistered source {source_key}"


@pytest.mark.parametrize(
    ("book", "key"),
    [
        (book.book, key)
        for entry in REQUIRED_BOOKS.values()
        for book in entry
        for key in book.republishers
    ],
)
def test_republishers_are_republishers_and_direct_routes_are_not(book, key):
    assert key in REPUBLISHED_SOURCE_KEYS, f"{book}: {key} is not a republisher"
    assert key not in RETAIL_SOURCE_KEYS


def test_pennsylvania_declares_every_book_the_operator_named():
    """The eleven PA books, so quietly dropping one is a failing test."""
    assert {book.book for book in REQUIRED_BOOKS["PA"]} == {
        "bet365",
        "BetMGM",
        "betPARX",
        "BetRivers",
        "Caesars",
        "DraftKings",
        "FanDuel",
        "Fanatics",
        "Mohegan Pennsylvania",
        "PlaySugarHouse",
        "theScore Bet",
    }


def test_illinois_declares_every_book_the_operator_named():
    """The ten IGB online operators, so quietly dropping one is a failing test."""
    assert {book.book for book in REQUIRED_BOOKS["IL"]} == {
        "bet365",
        "BetMGM",
        "BetRivers",
        "Caesars",
        "Circa",
        "DraftKings",
        "FanDuel",
        "Fanatics",
        "Hard Rock Bet",
        "theScore Bet",
    }


def test_illinois_circa_entry_is_shaped_to_fail_loudly():
    """Circa has no Illinois-licence observation path, and the entry says so.

    Action Network's only Circa book is the stateless id 78 (zero rows in every
    league, re-measured 2026-08-03) and the 2026-08-08 catalogue lists no
    ``Circa IL``.  ``vsin_circa`` is the Las Vegas line tracker, so it may only
    ever be ``OTHER_LICENCE`` context — grading ``NON_LOCAL_ONLY`` when VSiN
    answers and ``MISSING`` when it does not, ERROR severity either way.
    Anyone upgrading this entry — a direct key, a same-licence feed — is
    claiming an Illinois price source that has been measured not to exist, and
    this pin makes that claim a failing test rather than a quietly green
    checklist.
    """
    circa = next(book for book in REQUIRED_BOOKS["IL"] if book.book == "Circa")
    assert circa.direct is None
    assert circa.republishers == {"vsin_circa": Corroboration.OTHER_LICENCE}


def test_unlicensed_republisher_explains_itself_rather_than_reading_as_a_gap():
    """``UNAVAILABLE`` is a licensing fact, not a broken feed.

    betPARX has no Illinois book to republish.  Were Illinois to declare it
    required, the message must say that rather than "no feed produced a row".
    """
    configured = jurisdiction("IL")
    assert configured.republished["an_parx"].status is RouteStatus.UNAVAILABLE
    assert jurisdiction("PA").republished["an_parx"].status is not RouteStatus.UNAVAILABLE


def test_a_narrowed_run_does_not_fault_books_it_never_asked_for():
    """``--source an_caesars`` must not report the other ten books missing.

    It did: a run naming three sources produced eleven ``required_book_missing``
    errors, which is the same fault ``validation``'s *capabilities* argument
    exists to prevent — blaming a book for a scope the operator chose.
    """
    quotes = _book("fanduel")
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = check_book_coverage(quotes, "PA", report, configured=["fanduel"])

    assert _find(measured, "FanDuel").access is Access.DIRECT
    assert _find(measured, "Caesars").access is Access.NOT_REQUESTED
    assert not _find(measured, "Caesars").access.is_judgeable
    # One finding, and it is the book that has no feed in any scope.
    assert [finding.code for finding in report.findings] == ["required_book_missing"]
    assert "PlaySugarHouse" in report.findings[0].message


def test_a_book_with_no_feed_at_all_is_never_exempted_as_unrequested():
    """``PlaySugarHouse`` declares no direct route and no republisher.

    An empty feed set intersects nothing, so the ``NOT_REQUESTED`` exemption
    swallowed it on every run that passes a ``configured`` list — which is every
    production run, since the collector always passes one.  The single book in
    the table whose whole purpose is to fail loudly was the single book that
    could not.  Fails on the pre-fix membership test.
    """
    for configured in (["fanduel"], ["vi_fanatics"], ["an_caesars", "caesars"]):
        report = ValidationReport(quote_count=0, event_count=0, source_count=0)
        measured = check_book_coverage(
            _book("fanduel"), "PA", report, configured=configured
        )
        entry = _find(measured, "PlaySugarHouse")
        assert entry.access is Access.MISSING, configured
        assert entry.access.is_judgeable


def test_a_requested_book_that_produced_nothing_is_recorded_as_missing():
    """The not-requested exemption is scoped to what was never asked for.

    A book whose feed *was* requested and produced nothing is recorded
    ``MISSING`` — never ``NOT_REQUESTED``.  It is reported rather than collapsed,
    because FanDuel's sixty first-party rows are proof this slate had something
    to price: the collapse only speaks for a run that observed nothing at all.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = check_book_coverage(
        _book("fanduel"), "PA", report, configured=["fanduel", "an_caesars"]
    )
    assert _find(measured, "Caesars").access is Access.MISSING
    codes = [f.code for f in report.findings]
    assert "book_coverage_unobservable" not in codes
    assert {"required_book_missing"} == set(codes)


def test_omitting_configured_judges_every_book():
    """A full pass passes no source list, and must still hold every book."""
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    measured = check_book_coverage(_book("fanduel"), "PA", report)
    assert not any(entry.access is Access.NOT_REQUESTED for entry in measured)


def test_a_slate_wide_outage_is_one_finding_not_eleven():
    """Overnight, every game on the republisher's board is already under way.

    Measured at 02:20 ET: all ten PA book ids still returned prices and all
    eighteen games were ``complete`` or ``inprogress``, so every adapter
    correctly parsed nothing.  Eleven errors for one condition is noise, and a
    nightly false alarm is a check that stops being read.

    Collapsing is what is under test, so the run holds nothing else — a live
    venue in it is a different case, and now a different finding.  What must not
    return is one error per book.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage([], "PA", report)
    codes = [finding.code for finding in report.findings]
    # The outage, and the one book no feed watches. Not eleven.
    assert codes == ["book_coverage_unobservable", "required_book_missing"]


def test_the_collapse_does_not_swallow_a_book_nothing_watches():
    """A feed outage cannot explain a book that has no feed to lose.

    ``PlaySugarHouse`` is declared required and watched by nothing — no
    first-party route, no republisher.  It is the one entry whose entire purpose
    is to stay loud, and the slate-outage collapse returned before reporting it,
    so the nightly window was also the window in which a permanent configuration
    gap disappeared from the report.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage([], "PA", report)
    missing = [f for f in report.findings if f.code == "required_book_missing"]

    assert len(missing) == 1, [f.code for f in report.findings]
    assert "PlaySugarHouse" in missing[0].message
    assert missing[0].severity is Severity.ERROR
    # And the collapse still fired, so this is not just "the collapse broke".
    assert any(f.code == "book_coverage_unobservable" for f in report.findings)


def test_one_dark_book_among_healthy_ones_is_still_its_own_error():
    """The collapse must not become a way for a real gap to hide."""
    quotes = [*_book("fanduel"), *_book("an_caesars"), *_book("vi_caesars")]
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    check_book_coverage(quotes, "PA", report)
    codes = [finding.code for finding in report.findings]
    assert "book_coverage_unobservable" not in codes
    assert "required_book_missing" in codes


# ── which positions are takeable from the state ───────────────────────────────


class _Position:
    """Only ``sources`` is read, which is all the marking needs."""

    def __init__(self, *sources: str) -> None:
        self.sources = frozenset(sources)


def test_an_unlicensed_route_is_not_a_local_leg():
    """``JURISDICTIONS[state].routes`` was the wrong table to ask.

    It holds every route the registry knows for the state *including the
    ``UNAVAILABLE`` ones*.  Hard Rock is an ``UNAVAILABLE`` Pennsylvania route —
    the operator holds no PA licence for it — and :mod:`src.report` counted it as
    the local leg that made a position takeable from PA, on the dashboard, while
    ``collector arb`` withheld the same position.
    """
    assert jurisdiction("PA").routes["hardrock"].status is RouteStatus.UNAVAILABLE
    assert "hardrock" in JURISDICTIONS["PA"].routes      # the old, wrong answer

    marking = locality_marking("PA", route_scope="state")
    position = _Position("hardrock", "pinnacle")
    assert not marking.has_local_leg(position)
    assert marking.non_local_sources(position) == ("hardrock", "pinnacle")
    assert marking.count_without_local_leg([position]) == 1


def test_a_licensed_route_is_a_local_leg():
    """The marking must not simply flag everything."""
    marking = locality_marking("PA", route_scope="state")
    position = _Position("fanduel", "pinnacle")
    assert marking.has_local_leg(position)
    assert marking.leg_is_local("fanduel")
    assert not marking.leg_is_local("pinnacle")
    assert marking.non_local_sources(position) == ("pinnacle",)
    assert marking.count_without_local_leg([position]) == 0


def test_the_label_names_the_state_and_reachability():
    """One phrase for every surface — "reachable", never "licensed"."""
    marking = locality_marking("PA", route_scope="state")
    assert marking.label() == "not reachable from PA"


def test_leg_origin_separates_the_three_ways_a_leg_can_be_foreign():
    """"Not reachable from PA" is one phrase over three different situations.

    A book licensed one state over, a federally regulated venue reachable from
    everywhere, and an exchange no US customer can open an account with are not
    the same news, and the reachability bit alone cannot tell them apart.
    """
    marking = locality_marking("PA", route_scope="state")

    here = marking.leg_origin("fanduel")
    assert (here.kind, here.label, here.local) == ("in_state", "PA", True)

    # Reachable but *not* licensed here, and saying "PA" would state a licence
    # that does not exist.
    national = marking.leg_origin("kalshi")
    assert (national.kind, national.label, national.local) == (
        "national", "national", True,
    )

    elsewhere = marking.leg_origin("hardrock")
    assert elsewhere.kind == "other_states"
    assert elsewhere.label == "IL, NJ"
    assert elsewhere.local is False

    offshore = marking.leg_origin("pinnacle")
    assert (offshore.kind, offshore.label, offshore.local) == (
        "offshore", "offshore", False,
    )

    # A mirror is not a counterparty at all, and its reason differs again.
    assert marking.leg_origin("an_fanduel").kind == "republished"


def test_leg_origin_never_contradicts_the_reachability_bit():
    """The pill and the position-level flag are computed from one answer.

    Two derivations of "can I take this leg" is how a page ends up labelling a
    leg reachable inside a position it has already counted as unreachable.
    """
    from src.sources import registry

    marking = locality_marking("IL", route_scope="state")
    for source in sorted(registry.keys()):
        assert marking.leg_origin(source).local == marking.leg_is_local(source)


def test_leg_origin_still_places_a_venue_on_an_ungoverned_run():
    """A ``GLOBAL`` run has no state to be inside, and venues still have places.

    ``locality_applies`` is about whether a *state's* claim is being made. Where
    Pinnacle can be reached from does not depend on that, so the origin stays
    answerable — it simply never says ``in_state``.
    """
    marking = locality_marking("GLOBAL", route_scope="global")
    assert marking.reachable is None
    assert marking.leg_origin("pinnacle").kind == "offshore"
    assert marking.leg_origin("kalshi").kind == "national"
    assert marking.leg_origin("fanduel").kind == "other_states"
    assert all(
        marking.leg_origin(source).kind != "in_state"
        for source in ("fanduel", "kalshi", "pinnacle")
    )


def test_a_state_with_no_licence_at_all_flags_rather_than_skipping(monkeypatch):
    """An empty licensed set means "nothing here is takeable", never "no marking".

    "No licence" is the direction the mistake goes in: the live collector shipped
    one round with this rule gated on the keys a run had *built*, so the run that
    built no state book skipped the rule entirely and reported the position
    unlabelled instead of flagging it.

    Reached through a substitute because every shipped jurisdiction has at least
    four licensed retail routes — asserted below, so this stops being a substitute
    the day one does not.
    """
    from src.sources import registry

    assert all(
        registry.state_licensed_keys(state) for state in JURISDICTIONS
    ), "a shipped state now has no licensed route; test it directly instead"

    monkeypatch.setattr(registry, "state_licensed_keys", lambda state: frozenset())
    marking = locality_marking("PA", route_scope="state")
    assert marking.count_without_local_leg([_Position("pinnacle", "bovada")]) == 1


def test_a_us_regulated_venue_is_reachable_from_every_state():
    """The rule was inverted for Kalshi, and it cost real positions.

    Kalshi is a federally regulated US venue with no per-state sportsbook licence
    to hold — this registry says so itself, in the note explaining why it is absent
    from ``US_UNAVAILABLE_SOURCE_KEYS``. Built on ``state_licensed_keys`` alone, the
    locality rule called it out-of-state in *every* jurisdiction, so a legal
    Kalshi arbitrage on a Pennsylvania run was withheld from the report,
    the dashboard and the SMS — under a warning saying "every leg was a global or
    offshore venue".

    Rule (a) stops an unreachable price being presented as the state's own. It was
    never a reason to hide — or label — a reachable one.
    """
    marking = locality_marking("PA", route_scope="state")
    position = _Position("kalshi", "fanduel")
    assert marking.has_local_leg(position), "a legal US position flagged on a PA run"
    assert marking.non_local_sources(position) == ()
    assert marking.count_without_local_leg([position]) == 0


def test_the_us_regulated_prediction_markets_are_takeable_and_offshore_books_are_not():
    """Two CFTC venues are reachable; the offshore order books are not.

    This used to pin the offshore Polymarket as the unreachable half against
    Kalshi's reachable one. That venue was deregistered on 2026-08-13 once
    ``polymarket_us`` — QCX LLC, a separate CFTC-designated contract market with
    its own book, fees and settlement — was collecting, so the unreachable half
    is now carried by a venue that is still registered. The claim is unchanged:
    treating an unreachable price as takeable is how a hedge that cannot be
    entered gets reported as risk-free.

    The inverse mistake is the one the test above guards, and both are live: this
    must not sweep up either CFTC venue.
    """
    from src.sources import registry

    marking = locality_marking("PA", route_scope="state")
    offshore_only = _Position("smarkets", "pinnacle")
    assert not marking.has_local_leg(offshore_only)
    assert marking.non_local_sources(offshore_only) == ("pinnacle", "smarkets")

    for state in JURISDICTIONS:
        assert "smarkets" not in registry.takeable_from_state(state), state
        assert "kalshi" in registry.takeable_from_state(state), state
        assert "polymarket_us" in registry.takeable_from_state(state), state


def test_reachability_is_not_the_same_question_as_a_retail_licence():
    """Three sets, and conflating any two of them broke something.

    ``hardrock`` is retail and US-executable, and holds no PA licence — so it is
    reachable from IL and NJ and not from PA. ``kalshi`` holds no retail licence
    anywhere and is reachable from all four. ``pinnacle`` is reachable from none.
    """
    from src.sources import registry

    assert "hardrock" in registry.takeable_from_state("IL")
    assert "hardrock" not in registry.takeable_from_state("PA")
    for state in JURISDICTIONS:
        assert "kalshi" in registry.takeable_from_state(state), state
        assert "pinnacle" not in registry.takeable_from_state(state), state
        # A republished mirror is never somewhere you can place a bet.
        assert not registry.takeable_from_state(state) & REPUBLISHED_SOURCE_KEYS


def test_a_source_that_declares_no_reachability_fails_the_import():
    """Nationwide reach has to be claimed, because it used to be inherited.

    The nationwide half of ``takeable_from_state`` was "every base source that is
    not retail, not republished and not US-unavailable", so a source registered
    without anyone deciding the question became placeable in all four states
    while appearing in no table. A first-party ``thescore`` added that way is
    takeable in DC — where theScore Bet is not listed at all — and
    ``_check_direct_route`` then accepts it as PA's direct route, which is the
    whole locality rule failing in the direction that looks like coverage.

    That example is now history rather than hypothesis — ``thescore`` was
    registered into ``RETAIL_SOURCE_KEYS`` on 2026-08-13, with per-state routes,
    which is exactly what the docstring above prescribes.  So the *unclassified*
    key here has to be one nothing will ever claim; using a plausible book name
    means this check silently stops checking the day somebody adds it.
    """
    import dataclasses

    from src.sources import registry
    from src.sources.kalshi import KalshiAdapter

    invented = dataclasses.replace(registry.descriptor("kalshi"), key="no_such_book")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry, "_BASE_SOURCES", registry._BASE_SOURCES + (invented,)
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "no_such_book" in str(caught.value)
    assert invented.adapter is KalshiAdapter, "descriptor copy kept a real adapter"


def test_nationwide_reach_and_a_state_licence_cannot_both_be_claimed():
    """The two halves of ``takeable_from_state`` mean opposite things.

    Nationwide means "no per-state licence is needed"; retail, view-only and
    US-unavailable each mean a per-state answer exists. A key in both would make
    the union true everywhere while the per-state table said otherwise — the
    shape of the bug this set was introduced to close, arriving through the set
    itself.
    """
    from src.sources import registry

    # Pinned by content, not by size: both members are CFTC-designated venues
    # holding no state sportsbook licence, and that is the only ground for
    # membership.  ``polymarket_us`` is here while the offshore ``polymarket`` is
    # in US_UNAVAILABLE_SOURCE_KEYS — one brand, two legal entities, opposite
    # answers — so this assertion is also what stops the two being confused.
    assert registry.NATIONWIDE_SOURCE_KEYS == frozenset({"kalshi", "polymarket_us"})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "NATIONWIDE_SOURCE_KEYS",
            registry.NATIONWIDE_SOURCE_KEYS | {"fanduel"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "fanduel" in str(caught.value)


def test_retail_cannot_also_be_view_only_or_unreachable():
    """The overlap the first cut of the guard never looked at.

    Adding ``hardrock`` to US_UNAVAILABLE passed the invariant while
    ``takeable_from_state("IL")`` still served it while view-only and
    US-unavailable filtering disowned it — a self-contradiction the guard's own error message describes,
    arriving through a RETAIL overlap rather than a NATIONWIDE one.
    """
    from src.sources import registry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "US_UNAVAILABLE_SOURCE_KEYS",
            registry.US_UNAVAILABLE_SOURCE_KEYS | {"hardrock"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "hardrock" in str(caught.value)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "REPUBLISHED_SOURCE_KEYS",
            registry.REPUBLISHED_SOURCE_KEYS | {"fanduel"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "fanduel" in str(caught.value)


def test_the_one_legitimate_overlap_is_exactly_the_offshore_mirrors():
    """The four sets are a cover, not a partition, and this is the whole gap.

    A republished mirror of an offshore book is honestly both view-only and
    unstakeable, so the import-time guard permits REPUBLISHED ∩ US_UNAVAILABLE.
    Pinned to its exact membership here so a key drifting into both sets is a
    loud test failure demanding a decision, rather than a quiet third state.
    """
    from src.sources import registry

    assert registry.REPUBLISHED_SOURCE_KEYS & registry.US_UNAVAILABLE_SOURCE_KEYS == {
        "an_bovada",
    }
    assert not registry.RETAIL_SOURCE_KEYS & registry.REPUBLISHED_SOURCE_KEYS
    assert not registry.RETAIL_SOURCE_KEYS & registry.US_UNAVAILABLE_SOURCE_KEYS


def test_a_reachability_set_cannot_name_a_source_that_does_not_exist():
    """A set that drifts from the registry stops describing it.

    The three older sets are hand-written lists of keys, and a key kept after its
    source was renamed or dropped reads as a deliberate classification while
    covering nothing — the quiet half of the same problem as a source covered by
    no set at all.
    """
    from src.sources import registry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "NATIONWIDE_SOURCE_KEYS",
            registry.NATIONWIDE_SOURCE_KEYS | {"a_book_that_never_existed"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "a_book_that_never_existed" in str(caught.value)


def test_the_an_id_table_cannot_promise_a_feed_that_is_not_registered():
    """A key in ``_AN_BOOK_IDS`` without a registry descriptor is a fetch
    promise nothing can keep.

    The id table sat outside the four reachability buckets, so a key added
    there without registering passed every import check while
    ``republished_sources_for_state`` could never build it: the book it was
    meant to corroborate graded SINGLE_SOURCE or MISSING forever, its
    coverage detail naming a feed that does not exist.  Found latent by an
    adversarial round (2026-08-09) — no instance existed, and this keeps it
    that way.
    """
    from src.sources import registry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "STATE_LICENSED_REPUBLISHER_KEYS",
            registry.STATE_LICENSED_REPUBLISHER_KEYS | {"an_ghost_book"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "an_ghost_book" in str(caught.value)
    assert "_AN_BOOK_IDS" in str(caught.value)


def test_a_stored_key_the_registry_forgot_is_view_only_for_its_run():
    """Rows outlive registrations, and a ghost key is never a counterparty.

    ``unibet_au`` left 68 rows in runs 2–4 when it was dropped; re-analysing
    those runs formed it into full alert-eligible legs — unlabelled ones,
    because the runs predate the jurisdiction column — with no commission
    schedule, no settlement rule and no bet link behind them.  Stored-row
    call sites now pass their keys, and ``view_only_for_run`` folds every
    key the registry no longer knows into the view-only answer, under
    governed and ungoverned states alike.
    """
    from src.sources import registry

    legacy = registry.view_only_for_run("", ("unibet_au", "fanduel", "kalshi"))
    assert "unibet_au" in legacy
    assert "fanduel" not in legacy
    assert "kalshi" not in legacy
    assert "unibet_au" in registry.view_only_for_run("IL", ("unibet_au",))


def test_a_ghost_key_reads_view_only_on_the_source_badge():
    """The dashboard's source badge agrees with the leg rule on every state."""
    from src.report import _source_entry

    for state in ("GLOBAL", "IL", "PA", ""):
        assert _source_entry("unibet_au", state=state)["view_only"], state
    assert not _source_entry("fanduel", state="IL")["view_only"]


def test_every_required_book_feed_combination_is_declared_redundant():
    """Rule 8, enforced by enumeration instead of checklist prose.

    Every pair of feeds declared for one required book must be in
    ``REDUNDANT_PAIRS`` — re-registering a feed (the ``an_circa`` /
    ``vsin_circa`` pair died with the 2026-08-09 deregistration) without
    re-declaring its pairs would otherwise be caught by nothing mechanical.
    """
    from itertools import combinations

    from src.coverage import REQUIRED_BOOKS
    from src.redundancy import is_redundant_pair

    missing = []
    for state, books in REQUIRED_BOOKS.items():
        for book in books:
            feeds = ([book.direct] if book.direct else []) + list(book.republishers)
            for a, b in combinations(sorted(feeds), 2):
                if not is_redundant_pair(a, b):
                    missing.append((state, book.book, a, b))
    assert not missing, missing


def test_an_id_table_key_must_be_classified_as_republished():
    """An entry in the AN id table IS somebody else's board.

    Any other classification contradicts the table: retail would make a
    mirror placeable, nationwide would free a state-licensed book's copy
    from its licence.  The key is parked in NATIONWIDE here because leaving
    it unclassified trips the older cover check first — this pin is about
    the contradiction, not the omission.
    """
    from src.sources import registry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            registry,
            "REPUBLISHED_SOURCE_KEYS",
            registry.REPUBLISHED_SOURCE_KEYS - {"an_fanduel"},
        )
        patch.setattr(
            registry,
            "NATIONWIDE_SOURCE_KEYS",
            registry.NATIONWIDE_SOURCE_KEYS | {"an_fanduel"},
        )
        with pytest.raises(RuntimeError) as caught:
            registry._check_reachability_is_declared()
    assert "an_fanduel" in str(caught.value)
    assert "id table" in str(caught.value)


def test_the_nationwide_half_of_takeable_is_exactly_the_named_set():
    """What ``takeable_from_state`` adds beyond the state's licences.

    Worth stating because the named set and the old derived-by-exclusion
    comprehension return the *same* answer as long as the four sets partition the
    registry — the invariant above is what fixed the bug, not the expression. So
    this pins the direction: the state-invariant half is read from
    ``NATIONWIDE_SOURCE_KEYS``, and a fifth classification appearing later cannot
    quietly re-open it.
    """
    from src.sources import registry

    for state in JURISDICTIONS:
        beyond_licences = registry.takeable_from_state(state) - registry.state_licensed_keys(
            state
        )
        assert beyond_licences == registry.NATIONWIDE_SOURCE_KEYS, state


# ── which runs the rule governs ──────────────────────────────────────────────


def test_a_legacy_scope_run_with_a_real_jurisdiction_is_governed():
    """The scope test was inverted, and ``legacy`` is the value it mattered for.

    ``jurisdiction`` and ``route_scope`` were added to the schema in *different*
    commits, so every row a database migrated across that gap holds carries a real
    jurisdiction and ``route_scope='legacy'`` — and those are exactly the rows
    ``arb --run <old>`` and the dashboard read.  Requiring ``route_scope ==
    "state"`` therefore turned the rule off on the historical half of the store: a
    PA run with a ``pinnacle``/``onexbet`` position printed it **and texted it**,
    two legs neither of which a Pennsylvania operator can reach, with nothing
    saying so.

    Fails on the previous predicate, which left the position unmarked.
    """
    marking = locality_marking("PA", route_scope="legacy")
    assert marking.marking
    assert marking.count_without_local_leg([_Position("pinnacle", "onexbet")]) == 1


@pytest.mark.parametrize("scope", ["global", "all"])
def test_a_scope_the_operator_widened_on_purpose_is_exempt(scope):
    """``--scope all`` from Pennsylvania is a request to see the wider board.

    The one direction that must stay exempt: marking it against one state answers
    a question nobody asked, which is what broke four integration tests the first
    time the decision was keyed on the jurisdiction alone.
    """
    marking = locality_marking("PA", route_scope=scope)
    position = _Position("pinnacle", "onexbet")
    assert marking.reachable is None
    assert not marking.marking
    assert marking.leg_is_local("pinnacle")
    assert marking.has_local_leg(position)
    assert marking.non_local_sources(position) == ()
    assert marking.count_without_local_leg([position]) == 0


def test_an_unrecognised_scope_gets_the_marking_rather_than_a_pass():
    """A deny-list, so the money-safe answer is the default.

    A scope value this module has never heard of is governed.  Being wrong that way
    costs a labelled position with its count reported; being wrong the other way
    sends an unlabelled text message naming a book nobody can reach.
    """
    marking = locality_marking("PA", route_scope="some_future_scope")
    assert marking.marking
    assert marking.count_without_local_leg([_Position("pinnacle", "onexbet")]) == 1


@pytest.mark.parametrize("state", ["GLOBAL", "", "XX"])
def test_a_run_with_no_jurisdiction_is_never_marked(state):
    """Nothing is out of state when there is no state, and ``XX`` must not raise.

    The jurisdiction test is also what stops an unrecognised one reaching
    ``jurisdiction("XX")`` and aborting a whole pass with a ``KeyError``.
    """
    marking = locality_marking(state, route_scope="state")
    position = _Position("pinnacle", "onexbet")
    assert not marking.marking
    assert marking.has_local_leg(position)
    assert marking.count_without_local_leg([position]) == 0


def test_every_surface_asks_one_function_whether_the_rule_applies():
    """The condition is a function, not a sentence three callers each spell.

    ``lines`` grew a fourth spelling — the jurisdiction alone, with no scope test —
    and on a widened-scope run ``arb`` offered a position that ``lines``, one
    command later, marked as having no leg reachable from the state.
    """
    from src.coverage import locality_applies

    assert locality_applies("PA", "state")
    assert locality_applies("PA", "legacy")
    assert locality_applies("pa", " STATE ")          # normalised, both arguments
    assert not locality_applies("PA", "global")
    assert not locality_applies("GLOBAL", "state")
    assert not locality_applies(None, "state")


def test_global_is_not_asked_to_declare_a_required_book_list():
    """``GLOBAL`` licenses nothing, so there is no checklist it could declare.

    A missing declaration is a warning on purpose — an unchecked jurisdiction is
    not a clean one — but ``GLOBAL`` is not a jurisdiction, and ``collect_once``
    passed it straight through, so every global run in every batch carried
    ``book_coverage_not_declared`` telling the operator to declare books for a
    non-state.  ``check_redundancy`` next to it already had the special case.
    """
    report = ValidationReport(quote_count=0, event_count=0, source_count=0)
    assert check_book_coverage(_book("pinnacle"), "GLOBAL", report) == ()
    assert report.findings == []
