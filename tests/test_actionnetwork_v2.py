"""Which Action Network endpoint each republisher asks, and why it matters.

The two versions publish different book catalogues.  Pennsylvania's whole set —
74, 122, 246, 255, 280, 1534, 1906, 2791, 3547, 4623 — comes back on ``web/v2``;
on ``web/v1`` the MLB board carries none of it, and only 74 and 122 appear at all,
on two leagues.  The offshore shelf (21, 35, 2495) is the other way round:
``web/v1`` only.  Both were measured in the same minute on 2026-08-08.

That makes the base URL a correctness property rather than a preference, and a
silent one: v1 answers a Pennsylvania MLB request with HTTP 200, a full slate and
prices belonging to books nobody asked for.  Nine of Pennsylvania's ten fetchable
republished feeds were on v1 that way while reporting healthy — seven returning
nothing of their own on any league — because the *default* was v1 and only three
descriptors had thought to override it.

So the rule these tests hold is directional: **the default is the endpoint that
knows state licences**, and leaving it is the thing that has to be spelled out.
A descriptor that forgets to choose gets the working one.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from src.jurisdictions import JURISDICTIONS, RouteStatus
from src.raw_store import RawStore
from src.schema import Period
from src.sources import registry
from src.sources.actionnetwork import (
    DEFAULT_BASE_URL,
    LEGACY_V1_BASE_URL,
    REQUESTED_PERIODS,
    ActionNetworkAdapter,
    parse_actionnetwork,
)
from src.sources.guards import FormatChangeError, SourceError

FIXTURE_RAW_DIR = Path(__file__).parent / "fixtures" / "raw"

#: Spelled out rather than imported.  Asserting a descriptor equals
#: ``DEFAULT_BASE_URL`` is a tautology — move the constant to v1 and every such
#: comparison stays true while every live request goes to the wrong catalogue.
#: Measured, so it is a literal.
V2_URL = "https://api.actionnetwork.com/web/v2/scoreboard"
V1_URL = "https://api.actionnetwork.com/web/v1/scoreboard"

#: The two sources whose books exist on v1 and not on v2.  Any other key here
#: would be a source silently reading last-generation ids.
OFFSHORE_KEYS = frozenset({"an_bovada", "an_onexbet"})


def _an_descriptors(state: str | None) -> dict[str, registry.SourceDescriptor]:
    entries = (
        registry.sources_for_state(state) if state else registry.SOURCES
    )
    return {
        entry.key: entry
        for entry in entries
        if entry.adapter is ActionNetworkAdapter
    }


def test_the_default_endpoint_is_the_one_that_knows_state_licences() -> None:
    assert DEFAULT_BASE_URL == V2_URL
    assert LEGACY_V1_BASE_URL == V1_URL


@pytest.mark.parametrize("source", ("an_hardrock", "an_fanatics", "an_bally"))
def test_current_v2_capture_produces_real_quotes_without_rejections(source: str) -> None:
    paths = sorted(FIXTURE_RAW_DIR.glob(f"{source}__*.json"))
    assert paths
    raws = [RawStore(FIXTURE_RAW_DIR).read(path) for path in paths]
    adapter = registry.descriptor(source).replay_instance()
    try:
        outcome = adapter.parse(raws)
    finally:
        adapter.close()
    assert outcome.quotes
    assert not outcome.rejections
    assert {quote.source for quote in outcome.quotes} == {source}


@pytest.mark.parametrize("source", ("an_hardrock", "an_fanatics", "an_bally"))
def test_the_committed_v2_captures_are_full_game_only_because_none_asked(
    source: str,
) -> None:
    """The coverage this endpoint drops when the period set is not named.

    v2 answers with ``event`` alone unless ``periods`` names the windows, and
    every committed v2 capture predates that discovery — so all three parse to
    full game only, while every v1 capture beside them that parses to rows at all
    carries period rows for 35-57% of its total (40-65% of its baseball rows).
    ``an_open`` is 40 of 70 pregame MLB rows.

    This test states the gap rather than hiding it.  It is expected to be
    rewritten — not deleted — when these fixtures are recaptured with
    :data:`REQUESTED_PERIODS`, and it will fail loudly at that point, which is
    the only reliable reminder that the recapture actually changed something.
    """
    paths = sorted(FIXTURE_RAW_DIR.glob(f"{source}__*.json"))
    raws = [RawStore(FIXTURE_RAW_DIR).read(path) for path in paths]
    assert all("periods=" not in raw.url for raw in raws), (
        "a capture asked for periods; rewrite this test to assert they arrived"
    )
    adapter = registry.descriptor(source).replay_instance()
    try:
        outcome = adapter.parse(raws)
    finally:
        adapter.close()
    assert {quote.period for quote in outcome.quotes} == {Period.FULL_GAME}


def test_every_request_asks_for_the_period_windows() -> None:
    """The parameter without which v2 silently returns a third of the rows.

    Named ``periods``, plural, and the near misses are all quiet: singular
    ``period`` is ignored, and a comma-space list is accepted and answered with
    full game only.  So this pins the literal, not just its presence.
    """
    assert REQUESTED_PERIODS == "event,firstfiveinnings,firstinning"
    assert " " not in REQUESTED_PERIODS

    sent: list[dict[str, str] | None] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(dict(request.url.params))
        return httpx.Response(200, json={"games": []})

    for key in ("an_parx", "an_open", "an_bovada"):
        entry = registry.descriptor(key)
        adapter = entry.factory()(
            leagues=["MLB"],
            client=httpx.Client(transport=httpx.MockTransport(record)),
        )
        try:
            with pytest.raises(SourceError):
                adapter.fetch_raw()  # an empty board is a refusal, not a result
        finally:
            adapter.close()

    assert sent, "no request was issued"
    for params in sent:
        assert params is not None
        assert params.get("periods") == REQUESTED_PERIODS


@pytest.mark.parametrize("state", [None, *sorted(JURISDICTIONS)])
def test_every_republisher_asks_v2_except_the_offshore_two(state: str | None) -> None:
    """The check that makes descriptor #19 impossible.

    A new Action Network source is added by copying a neighbour and changing the
    book id.  Nothing in ``_check_registry`` can catch a *missing* config key, so
    the only thing that keeps the next one off the wrong endpoint is that
    omitting the key now means v2.  This asserts that across every jurisdiction,
    not just the base registry, because a state run rebuilds these descriptors
    from ``Jurisdiction.republished`` and could reintroduce one there.
    """
    descriptors = _an_descriptors(state)
    assert descriptors, "no Action Network sources built"
    if state:
        # The lookup a live state run actually fetches through, which rebuilds
        # these descriptors from the jurisdiction rather than reading them off
        # the base registry.  Sweeping only the enumeration lookup would miss a
        # v1 URL reintroduced there.
        descriptors |= {
            entry.key: entry
            for entry in registry.republished_sources_for_state(state)
            if entry.adapter is ActionNetworkAdapter
        }

    for key, entry in sorted(descriptors.items()):
        base = entry.config.get("base_url", DEFAULT_BASE_URL)
        if key in OFFSHORE_KEYS:
            assert base == V1_URL, (
                f"{key} carries the offshore shelf, which v2 does not publish"
            )
            continue
        assert base == V2_URL, (
            f"{key} would ask web/v1, which answers a state request with a full "
            "slate of books nobody asked for rather than with an error"
        )


def test_the_offshore_exception_is_two_sources_and_stays_that_way() -> None:
    """A widening exception should cost somebody a deliberate edit.

    ``base_url`` is the one config key that can quietly point a source at last
    generation's catalogue, so the set allowed to set it is pinned rather than
    merely conventional.
    """
    setters = {
        entry.key
        for entry in registry.SOURCES
        if entry.adapter is ActionNetworkAdapter and "base_url" in entry.config
    }
    assert setters == OFFSHORE_KEYS


def test_the_endpoint_is_a_fetch_concern_and_never_reaches_parse() -> None:
    """The claim the v2 switch rests on, in executable form.

    Committed captures are v1 bytes.  They must keep parsing identically after
    the default moves, which holds only because ``base_url`` is read in
    ``fetch_raw`` while ``parse`` is a module-level function reading the schema
    off the payload and the book id off the envelope label.  If that ever stops
    being true, every stored fixture starts disagreeing with the live run and
    this is the test that says so.
    """
    store = RawStore(FIXTURE_RAW_DIR)
    paths = sorted(FIXTURE_RAW_DIR.glob("an_bovada__*.json"))
    assert paths, "an_bovada is the committed v1 capture this test reads"
    raws = [store.read(path) for path in paths]
    assert any("/web/v1/" in raw.url for raw in raws), (
        "this fixture is meant to be v1 bytes; the test is vacuous otherwise"
    )

    on_v2 = ActionNetworkAdapter(
        source_key="an_bovada", book_id=21, base_url=DEFAULT_BASE_URL
    )
    on_v1 = ActionNetworkAdapter(
        source_key="an_bovada", book_id=21, base_url=LEGACY_V1_BASE_URL
    )
    try:
        assert on_v2.parse(raws).quotes == on_v1.parse(raws).quotes
        assert on_v2.parse(raws).quotes == parse_actionnetwork(raws).quotes
    finally:
        on_v2.close()
        on_v1.close()


def test_parse_reads_the_book_from_the_envelope_not_the_instance() -> None:
    """Why relabelling a capture is enough to test another tenant's id.

    Also the reason the endpoint cannot leak into parse: the tenant is carried by
    the stored envelope, so replay does not need to know how the adapter was
    configured.  ``replay_run`` calls ``descriptor.factory()`` with no arguments —
    the descriptor's own config is bound into the partial, so what replay omits is
    the *caller's* configuration, not the registry's.
    """
    store = RawStore(FIXTURE_RAW_DIR)
    paths = sorted(FIXTURE_RAW_DIR.glob("an_bovada__*.json"))
    raws = [store.read(path) for path in paths]
    assert parse_actionnetwork(raws).quotes, "fixture parses to rows as book 21"

    relabelled = [
        dataclasses.replace(raw, endpoint=raw.endpoint.replace(":21:", ":2495:"))
        for raw in raws
    ]
    as_other_book = parse_actionnetwork(relabelled)
    assert all(quote.source == "an_bovada" for quote in as_other_book.quotes)
    assert as_other_book.quotes != parse_actionnetwork(raws).quotes


def test_a_second_licences_capture_is_refused_not_quietly_preferred() -> None:
    """The recapture hazard: two states' book ids in one source's store.

    The same brand is a different id per licence, so re-capturing a republisher
    from Pennsylvania writes a *new* endpoint label beside the surviving
    out-of-state one — ``latest_per_endpoint`` groups by label, so age never
    enters into it and both are parsed.  De-duplication is per ``path:event_id``
    and iteration is over ``sorted(labels)``, so for any game both list, the
    lexically smaller id wins: ``scoreboard:21:`` sorts before
    ``scoreboard:2495:`` exactly as 123 sorts before 1906 for ``an_caesars``,
    and the stale licence's prices go out under the new state's run.

    Rehearsed here with the committed capture as the week-old survivor and a
    relabelled copy as today's — the newer capture, and the loser.
    """
    store = RawStore(FIXTURE_RAW_DIR)
    paths = sorted(FIXTURE_RAW_DIR.glob("an_bovada__*.json"))
    assert paths
    captured = [store.read(path) for path in paths]
    assert all(":21:" in raw.endpoint for raw in captured)

    stale = [
        dataclasses.replace(raw, fetched_at=raw.fetched_at - timedelta(days=7))
        for raw in captured
    ]
    fresh = [
        dataclasses.replace(raw, endpoint=raw.endpoint.replace(":21:", ":2495:"))
        for raw in captured
    ]
    assert "scoreboard:21:mlb" < "scoreboard:2495:mlb", (
        "the trap is that the older capture sorts first; if that stopped being "
        "true this test would pass for the wrong reason"
    )
    assert parse_actionnetwork(stale).quotes, "each half parses on its own"
    assert parse_actionnetwork(fresh).quotes

    with pytest.raises(FormatChangeError) as caught:
        parse_actionnetwork(stale + fresh)
    message = str(caught.value)
    assert "21" in message and "2495" in message, (
        f"the refusal has to name both ids to be actionable: {message}"
    )


@pytest.mark.parametrize(
    "source,book_id",
    (("an_parx", 74), ("an_thescore", 4601), ("an_unibet", 246)),
)
def test_the_unproven_books_are_captured_on_their_own_id_and_parse(
    source: str, book_id: int
) -> None:
    """The three feeds the v2 work existed to unblock, held to what they produced.

    Each of these shipped a fixture that asked a **New Jersey** id through v1 —
    1929, 247, 4620 — and parsed to zero rows, so every contract test over them
    passed by having nothing to check. Replaced 2026-08-08 with Pennsylvania
    captures on v2 against a live pregame slate.

    The id pinned here is the licence the key's fixture store currently holds,
    because the store is **single-licence by the parser's own guard** — one
    pass writes one book id, and mixing two states' captures under one key is
    refused as a format change.  ``an_parx``/``an_unibet`` hold their
    2026-08-08 Pennsylvania captures (74, 246).  ``an_thescore`` holds its
    2026-08-10 Illinois capture (4601, the id's first-ever request — 74 soccer
    rows), taken for the Illinois campaign; the 4623 Pennsylvania proof (225
    rows) is recorded in ``docs/evidence/action-network.md`` and its store returns
    with the next PA-egress recapture.

    The properties held per key: the fixture asks the key's own id, rides v2,
    asked for the period windows, and parses to rows.  The windows-present
    property is provable only where a **baseball** board is in the store — a
    soccer board has no innings — so it binds exactly the keys that hold one.
    """
    paths = sorted(FIXTURE_RAW_DIR.glob(f"{source}__*.json"))
    assert paths, f"{source} lost its capture"
    raws = [RawStore(FIXTURE_RAW_DIR).read(path) for path in paths]

    assert all(f":{book_id}:" in raw.endpoint for raw in raws), (
        f"{source} must be captured on its own id {book_id}"
    )
    assert all("/web/v2/" in raw.url for raw in raws), "captured on v2"
    assert all("periods=" in raw.url for raw in raws), (
        "the capture has to have asked, or full-game-only is all it can hold"
    )

    adapter = registry.descriptor(source).replay_instance()
    try:
        outcome = adapter.parse(raws)
    finally:
        adapter.close()

    assert outcome.quotes, f"{source} parses to nothing — an empty fixture again"
    assert not outcome.rejections, [str(r) for r in outcome.rejections[:3]]
    if any(":mlb" in raw.endpoint for raw in raws):
        assert {quote.period for quote in outcome.quotes} > {Period.FULL_GAME}, (
            "a v2 capture that asked for periods and came back full-game-only "
            "means the parameter stopped working, which is silent in production"
        )


def test_the_periods_evidence_cannot_quietly_leave_the_fixture_pool() -> None:
    """At least one committed AN baseball board must parse to sub-game windows.

    The windows-present assertion above is conditional on an ``:mlb`` board
    being in the key's store — a soccer board has no innings to show — which
    makes it *self-disabling*: a recapture pass that happens to store no
    baseball board (any pass in the MLB offseason) would silently retire the
    suite's only committed-bytes proof that ``periods=`` yields F5/F1 windows,
    with zero failing tests.  This floor pin makes that loss loud.  It binds
    the pool, not one key, so the evidence may move between keys — today it
    lives in ``an_parx``'s and ``an_unibet``'s 2026-08-08 Pennsylvania MLB
    boards.  If MLB itself is dark at recapture time, the honest moves are a
    capture of another innings-bearing board or a deliberate, recorded change
    to this pin — not a quiet conditional.
    """
    store = RawStore(FIXTURE_RAW_DIR)
    mlb_paths = sorted(FIXTURE_RAW_DIR.glob("an_*__*-mlb_*.json"))
    assert mlb_paths, "no Action Network baseball board is committed at all"
    windowed = set()
    for path in mlb_paths:
        raw = store.read(path)
        if "periods=event%2Cfirstfiveinnings%2Cfirstinning" not in raw.url:
            continue
        source_key = path.name.split("__", 1)[0]
        adapter = registry.descriptor(source_key).replay_instance()
        try:
            outcome = adapter.parse([raw])
        finally:
            adapter.close()
        windowed |= {q.period for q in outcome.quotes} - {Period.FULL_GAME}
    assert windowed, (
        "no committed AN baseball board parses to a sub-game window; the "
        "periods= proof has left the fixture pool"
    )


def test_every_live_action_network_request_names_its_book_ids() -> None:
    """No request relies on v2's *unnamed* default, which nobody has measured.

    Every capture on disk that produced rows named its ids; asking for ids v2
    does not carry narrows the answer to the defaults 15/30, and what a request
    with **no** ``bookIds`` returns has never been observed on v2 at all.
    After the endpoint flip ``an_open`` was the one descriptor issuing that
    shape — on every batch's global pass — so a change in the unnamed default
    would have landed as a silent board change on a source with no evidence
    trail.  Named ids are the measured shape; this keeps every live request on
    it, across the global list and all four states' live builders.
    """
    live: dict[str, registry.SourceDescriptor] = {}
    for entry in registry.global_sources():
        live.setdefault(entry.key, entry)
    for state in sorted(JURISDICTIONS):
        for entry in registry.republished_sources_for_state(state):
            live.setdefault(f"{state}:{entry.key}", entry)

    an_live = {
        label: entry
        for label, entry in live.items()
        if entry.adapter is ActionNetworkAdapter
    }
    assert an_live, "the sweep found no Action Network descriptors — vacuous"
    for label, entry in sorted(an_live.items()):
        assert entry.config.get("fetch_book_ids"), (
            f"{label} would issue a bookIds-less request — an unmeasured shape"
        )


def test_asking_for_baseball_windows_does_not_empty_another_sports_board() -> None:
    """``periods=`` names baseball windows and is sent on *every* path.

    The quiet failure would be v2 treating unknown window names on a
    non-baseball board as "match nothing" — every Action Network tenant's
    NBA/NFL/NHL board emptying the day the parameter shipped, with each request
    still answering 200.  The committed theScore Bet Illinois **soccer** capture
    (2026-08-10, the first-ever request of id 4601) is the evidence it does
    not: same request shape, ``periods=`` in the URL, and the board parses to
    74 full-game rows (the baseball windows are simply absent, as they should
    be for a sport that has no innings).  The evidence used to be the betPARX
    Pennsylvania NFL board; that file was retired 2026-08-09 when its stale
    preseason line clashed with the fresh-vintage fixture pool — see
    ``docs/evidence/action-network.md`` § "One stale board retired".
    """
    store = RawStore(FIXTURE_RAW_DIR)
    paths = sorted(FIXTURE_RAW_DIR.glob("an_thescore__*-soccer_*.json"))
    assert paths, "the soccer capture is this test's entire evidence"
    raws = [store.read(path) for path in paths]
    # The full encoded window list, not the bare parameter name: a recapture
    # that asked ``periods=event`` alone would prove nothing about unknown
    # window names and still contain the substring.
    assert all(
        "periods=event%2Cfirstfiveinnings%2Cfirstinning" in raw.url for raw in raws
    )

    outcome = parse_actionnetwork(raws)
    assert not outcome.rejections
    assert len(outcome.quotes) >= 50, (
        f"a soccer board answering {len(outcome.quotes)} rows is the emptying "
        "this test exists to catch"
    )
    assert {quote.period for quote in outcome.quotes} == {Period.FULL_GAME}


@pytest.mark.parametrize("state", sorted(JURISDICTIONS))
def test_no_republished_route_claims_to_have_been_validated(state: str) -> None:
    """A status nothing can earn is worse than no status.

    ``_republished_for`` builds these routes from a table of book ids.  It runs
    no probe, reads no capture, and cannot know whether a feed answers — so
    ``VALIDATED``, which it used to stamp on every route it built, was assigned
    by the act of construction.  Pennsylvania's betPARX, Mohegan and theScore
    routes read "validated" while returning zero rows.

    ``TEMPLATE`` is the honest word here and ``UNAVAILABLE`` is the licensing
    fact.  Whether the book was seen is ``src.coverage``'s claim, not this one.

    **No output surface reads this difference today** — every consumer tests
    only ``is UNAVAILABLE``, and ``RetailRoute.warning`` is a different class.
    That is the argument for pinning it rather than against: a field nothing
    validates is exactly the one that drifts, and the next reader to reach for
    a route status will find a word that means what it says.
    """
    for key, route in sorted(JURISDICTIONS[state].republished.items()):
        assert route.status is not RouteStatus.VALIDATED, (
            f"{state}/{key}: nothing in this construction path can validate a "
            "route; src.coverage owns the observed-or-not claim"
        )
        assert route.status in (RouteStatus.TEMPLATE, RouteStatus.UNAVAILABLE)


def test_the_state_layer_names_pennsylvanias_own_ids() -> None:
    """The ids v1 could not answer for, resolved per state rather than inherited.

    Pinned alongside the endpoint because the two together are what makes a PA
    run a PA run: v2 supplies the catalogue and ``Jurisdiction.republished``
    supplies the licence.  Either one wrong and the run reports another state's
    prices under Pennsylvania's name.
    """
    built = _an_descriptors("PA")
    assert built["an_parx"].config["book_id"] == 74
    assert built["an_unibet"].config["book_id"] == 246
    assert built["an_thescore"].config["book_id"] == 4623
    assert built["an_betrivers"].config["book_id"] == 122

    # The fetch-path lookup refuses the two books with no PA licence.  The
    # enumeration lookup above deliberately keeps them registered on their own
    # New Jersey ids so replay and coverage still see a stable key set — which
    # is exactly why the endpoint sweep has to run over *both*.
    fetched = {entry.key for entry in registry.republished_sources_for_state("PA")}
    for key in ("an_hardrock", "an_bally"):
        assert key in built
        assert key not in fetched, f"{key} holds no PA licence and must not be fetched"
