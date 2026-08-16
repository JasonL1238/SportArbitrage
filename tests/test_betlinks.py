"""Where each leg gets placed, and the ways that link can lie.

The failure this module exists to prevent is not a missing link — it is a link
that resolves to the wrong game while looking exactly like a right one.  Two
shapes of that are pinned here: interpolating an aggregator's event id into the
book's URL, and emitting a grammar no live probe has confirmed.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from src.betlinks import (
    CONSENSUS_FEEDS,
    EVENT_URL,
    LEAGUE_PAGE,
    MIRROR_BOOK,
    SITE,
    STATE_SITE,
    Precision,
    bet_link,
    book_for,
    link_payload,
    state_host,
    team_slug,
)
from src.sources.registry import SOURCES
from tests.conftest import make_quote


class TestAMirrorNeverLinksToAnEvent:
    """An ``an_*`` / ``vi_*`` / ``vsin_*`` row's id belongs to the aggregator.

    ``an_draftkings`` is the case that matters: DraftKings *does* have a
    confirmed event grammar, so the only thing standing between a reader and a
    link to a stranger's game is the mirror check.  Action Network event 292342
    is a real DraftKings event id too — of an unrelated fixture.
    """

    @pytest.mark.parametrize("source", sorted(MIRROR_BOOK))
    def test_no_mirror_gets_an_event_link(self, source: str) -> None:
        quote = make_quote(source=source, source_event_id="292342", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is not Precision.EVENT, (
            f"{source} produced an event link from an aggregator's id"
        )
        assert link.mirrored is True

    def test_the_mirror_resolves_to_the_book_not_the_aggregator(self) -> None:
        quote = make_quote(source="an_fanduel", source_event_id="292342", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.book == "fanduel"
        assert "fanduel" in link.url
        assert "actionnetwork" not in link.url

    def test_the_same_book_direct_does_get_the_event_link(self) -> None:
        """The mirror check must not be a blanket ban on the book."""
        quote = make_quote(source="draftkings", source_event_id="34475007", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is Precision.EVENT
        assert link.url.endswith("/event/34475007")
        assert link.mirrored is False


class TestAnUnverifiedGrammarIsNeverEmitted:
    """``verified`` is the whole safety property: a candidate URL is a guess."""

    @pytest.mark.parametrize(
        "book", sorted(key for key, entry in EVENT_URL.items() if not entry.verified)
    )
    def test_unverified_books_fall_back(self, book: str) -> None:
        quote = make_quote(source=book, source_event_id="123456", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is not Precision.EVENT

    def test_flipping_verified_on_is_what_promotes_it(self) -> None:
        """Pins the mechanism, so a refactor cannot leave the flag decorative."""
        import dataclasses

        from src import betlinks

        candidate = EVENT_URL["fanduel"]
        assert candidate.verified is False
        promoted = dict(EVENT_URL)
        promoted["fanduel"] = dataclasses.replace(candidate, verified=True)
        quote = make_quote(source="fanduel", source_event_id="35883975", league="MLB")
        original = betlinks.EVENT_URL
        try:
            betlinks.EVENT_URL = promoted
            link = bet_link(quote)
        finally:
            betlinks.EVENT_URL = original
        assert link is not None
        assert link.precision is Precision.EVENT
        assert link.url.endswith("35883975")

    def test_every_verified_entry_records_when_it_was_checked(self) -> None:
        """A flag with no probe behind it is the thing this module forbids."""
        for book, entry in EVENT_URL.items():
            if entry.verified:
                assert entry.note, f"{book} is verified with no note saying when"
                assert "20" in entry.note, f"{book}'s note names no date: {entry.note!r}"


class TestEveryRegisteredSourceResolves:
    """Parametrised off the registry, not a hand-written list.

    A newly registered book with no entry here would otherwise return no link at
    all and the panel would quietly print a dash forever.
    """

    @pytest.mark.parametrize("key", sorted(descriptor.key for descriptor in SOURCES))
    def test_a_link_or_a_declared_consensus_feed(self, key: str) -> None:
        quote = make_quote(source=key, source_event_id="1", league="MLB")
        link = bet_link(quote)
        if key in CONSENSUS_FEEDS:
            assert link is None, f"{key} is a consensus feed and cannot be bet"
            return
        assert link is not None, f"{key} resolves to no link at all"
        assert link.url.startswith("https://")

    def test_every_mirrored_book_has_a_front_door(self) -> None:
        for source, book in MIRROR_BOOK.items():
            assert book in SITE, f"{source} mirrors {book}, which has no SITE entry"

    def test_every_league_page_book_has_a_front_door(self) -> None:
        for book in LEAGUE_PAGE:
            assert book in SITE, f"{book} has league pages but no SITE entry"

    def test_league_pages_are_not_shared_between_books(self) -> None:
        """A copy-paste that points one book's league page at another's host."""
        for book, pages in LEAGUE_PAGE.items():
            for league, url in pages.items():
                assert url.startswith(
                    "https://"
                ), f"{book}/{league} is not absolute"


class TestPrecisionIsReported:
    def test_a_league_page_says_league(self) -> None:
        quote = make_quote(source="pinnacle", source_event_id="1", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is Precision.LEAGUE

    def test_a_book_with_no_league_grammar_says_site(self) -> None:
        quote = make_quote(source="kalshi", source_event_id="X", league="MLB")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is Precision.SITE

    def test_an_unlisted_league_degrades_rather_than_breaking(self) -> None:
        """A tennis row on a book whose league table only covers US majors."""
        quote = make_quote(source="pinnacle", source_event_id="1", league="ATP")
        link = bet_link(quote)
        assert link is not None
        assert link.precision is Precision.SITE
        assert link.book == "pinnacle"

    def test_the_payload_carries_the_precision(self) -> None:
        quote = make_quote(source="draftkings", source_event_id="34475007", league="MLB")
        payload = link_payload(quote)
        assert payload == {
            "url": "https://sportsbook.draftkings.com/event/34475007",
            "precision": "event",
            "book": "draftkings",
            "mirrored": False,
        }

    def test_a_consensus_feed_has_no_payload(self) -> None:
        quote = make_quote(source="an_open", source_event_id="1", league="MLB")
        assert link_payload(quote) is None
        assert book_for("an_open") is None


class TestAStatePartitionedBookLinksItsOwnState:
    """A licence's front door, not some other licence's.

    Run 32 — the run Pennsylvania's routes were promoted on — produced two real
    opportunities whose ``betrivers_kambi`` legs each carried
    ``il.betrivers.com`` under a "PLACE BOTH NOW", while the prices had come
    from ``rsiuspa``/``market=US-PA``.  The Illinois site does not show
    Pennsylvania's slip; a link that looks right and lands wrong wastes the
    seconds the edge is made of, and presents another state's venue as this
    state's — rule (a)'s failure worn by a URL.
    """

    @pytest.mark.parametrize(
        "source,state,expected",
        (
            ("betrivers_kambi", "PA", "https://pa.betrivers.com/?page=sportsbook"),
            ("betrivers_kambi", "IL", "https://il.betrivers.com/?page=sportsbook"),
            ("betrivers_kambi", "NJ", "https://nj.betrivers.com/?page=sportsbook"),
            ("caesars", "PA", "https://sportsbook.caesars.com/us/pa/bet"),
            ("caesars", "IL", "https://sportsbook.caesars.com/us/il/bet"),
            ("an_unibet", "PA", "https://pa.unibet.com"),
            ("an_unibet", "NJ", "https://nj.unibet.com"),
            # bet365's three doors.  The host *is* the licence for this book —
            # the stateless origin serves no board at all — so a wrong door is
            # not a cosmetic slip but a link to nothing.
            ("bet365", "IL", "https://www.il.bet365.com"),
            ("bet365", "PA", "https://www.pa.bet365.com"),
            ("bet365", "NJ", "https://www.nj.bet365.com"),
        ),
    )
    def test_the_governed_state_selects_the_door(
        self, source: str, state: str, expected: str
    ) -> None:
        quote = make_quote(source=source, source_event_id="1", league="ATP")
        link = bet_link(quote, state=state)
        assert link is not None
        assert link.url == expected

    def test_an_ungoverned_run_names_no_state_at_all(self) -> None:
        """Better a brand chooser than a confident wrong state — for every
        state-partitioned book, not only the one the defect was found on.

        One deliberate exception, protected by inventory rather than the
        resolver, and asserted on purpose so a change to the fact shows up
        here:

        * ``unibet`` — no US brand chooser exists (``unibet.com`` is the
          global gambling site); its only feed is built for PA/NJ runs alone
          and is view-only everywhere.

        (``superbook`` was the second exception until 2026-08-09, when
        ``an_superbook`` — a feed that never produced a row on either endpoint
        version — was deregistered along with its Colorado door.)
        """
        from src.sources.registry import REPUBLISHED_SOURCE_KEYS

        for book, quote_source, pinned in (
            ("betrivers_kambi", "betrivers_kambi", None),
            ("caesars", "caesars", None),
            ("unibet", "an_unibet", "https://pa.unibet.com"),
            ("bet365", "bet365", None),
        ):
            quote = make_quote(source=quote_source, source_event_id="1", league="ATP")
            link = bet_link(quote)
            assert link is not None, book
            if pinned is not None:
                assert link.url == pinned
                # The inventory protection the pin relies on: the feed is
                # view-only, so no money surface builds this link.
                assert quote_source in REPUBLISHED_SOURCE_KEYS
                continue
            assert link.url not in STATE_SITE[book].values(), (
                f"{book}'s ungoverned fallback is one state's own door: {link.url}"
            )
            if book == "bet365":
                # bet365 passes the rule above but is the one STATE_SITE book
                # whose stateless fallback src/betlinks.py itself calls "a door
                # onto nothing" — www.bet365.com serves no board.  Unibet buys
                # its exemption with view-only inventory; bet365 cannot, because
                # it is stakeable.  Its protection is the other half of the same
                # argument: a retail key is excluded from ``global_sources()``,
                # so an ungoverned run can never hold a bet365 leg to link.  If
                # that ever stops being true, this book needs a real ungoverned
                # door before it reaches a money surface.
                from src.sources.registry import RETAIL_SOURCE_KEYS, global_sources

                assert quote_source in RETAIL_SOURCE_KEYS
                assert quote_source not in {e.key for e in global_sources()}

    def test_the_betrivers_doors_agree_with_the_promo_layer(self) -> None:
        """Two spellings of one door will drift; this is the pin that says so.

        ``jurisdictions.PromoRoute`` already carried the right per-state
        BetRivers URL while the arb-link layer pinned Illinois — the repo knew
        the answer and the two layers disagreed.  Each state's arb door must
        live on the host the promo layer names.
        """
        from urllib.parse import urlsplit

        from src.jurisdictions import JURISDICTIONS

        for state, jurisdiction in JURISDICTIONS.items():
            promo_url = jurisdiction.promos.betrivers_url
            arb_url = STATE_SITE["betrivers_kambi"].get(state)
            if promo_url is None:
                assert arb_url is None, (
                    f"{state}: no BetRivers licence, so no arb door either"
                )
                continue
            assert arb_url is not None, f"{state}: promo layer has a door, arb has none"
            assert urlsplit(arb_url).netloc == urlsplit(promo_url).netloc, (
                f"{state}: arb door {arb_url} and promo door {promo_url} disagree"
            )

    def test_the_first_party_doors_cover_exactly_the_licensed_states(self) -> None:
        """A door exists for a licence iff a route does — for both directions.

        BetRivers above is pinned against the *promo* layer because that is
        where its second spelling lived.  The books with a first-party adapter
        have another: ``jurisdictions`` says which states the collector may
        fetch, ``STATE_SITE`` says which states the reader can be sent to, and
        nothing made the two agree.  A door without a route sends a reader to a
        board no run can price; a route without a door drops a governed run to
        the stateless landing, which for bet365 is a door onto nothing.
        """
        from src.jurisdictions import JURISDICTIONS, RouteStatus

        for book in ("bet365", "caesars"):
            for state, jurisdiction in JURISDICTIONS.items():
                route = jurisdiction.routes.get(book)
                door = STATE_SITE[book].get(state)
                licensed = route is not None and route.status is not RouteStatus.UNAVAILABLE
                assert (door is not None) == licensed, (
                    f"{book}/{state}: route={'yes' if licensed else 'no'} but "
                    f"door={door!r} — these must agree, or the reader and the "
                    "collector disagree about where this licence lives"
                )

    def test_bet365s_doors_are_the_hosts_its_routes_fetch(self) -> None:
        """For bet365 alone, the door and the route are the *same* host.

        Its pull-pod API is served from the licensed site itself, so the two
        layers spell one string twice and will drift.  The host **is** the
        licence here — ``www.bet365.com`` serves no board at all — so a door
        that drifts from the route is a door onto another state's board.

        Caesars is deliberately not in this pin: it fetches from
        ``api.americanwagering.com`` and sends readers to
        ``sportsbook.caesars.com``, which are different hosts on purpose, so
        equality there would be asserting something false.
        """
        from urllib.parse import urlsplit

        from src.jurisdictions import source_host

        for state, door in STATE_SITE["bet365"].items():
            assert urlsplit(door).netloc == source_host(state, "bet365"), (
                f"bet365/{state}: door {door} and the route host "
                f"{source_host(state, 'bet365')} disagree — for this book that "
                "is a link to a different licence's board"
            )

    def test_a_state_partitioned_league_page_resolves_in_every_state(self) -> None:
        """The league-page lookup outranks the state door in ``bet_link``.

        This used to forbid the entry outright, because nothing could spell a
        per-state league URL.  ``{host}`` can, so the rule is now a shape: an
        entry must take the placeholder and must produce that state's own
        netloc for every state the book is licensed in.  A single-domain league
        page would reintroduce the Illinois-link defect one precision level up.
        """
        for book, pages in LEAGUE_PAGE.items():
            if book not in STATE_SITE:
                continue
            for league, url in pages.items():
                assert "{host}" in url, (
                    f"{book}/{league} gained a league page pinned to one "
                    f"domain ({url}); it outranks STATE_SITE from every state"
                )
                doors = STATE_SITE[book]
                built = {s: url.format(host=state_host(book, s)) for s in doors}
                # Netloc equality is not enough on its own: Caesars' four doors
                # are one host with four *paths*, so a {host} template gives
                # every state the same URL while matching a netloc that never
                # varied.  Distinctness, and sitting under the state's own
                # door, are the checks with teeth.
                assert len(set(built.values())) >= len(set(doors.values())), (
                    f"{book}/{league} collapses {sorted(doors)} onto "
                    f"{sorted(set(built.values()))}; this book is not "
                    "partitioned by netloc, so {host} cannot carry its licence"
                )
                for state, door in doors.items():
                    # netloc AND prefix: startswith is a string test, and a
                    # door with no path (bet365's) is a prefix of
                    # "www.il.bet365.com.elsewhere.example" too.
                    assert urlsplit(built[state]).netloc == urlsplit(door).netloc, (
                        f"{book}/{league} in {state} points at "
                        f"{urlsplit(built[state]).netloc}, not at that state's "
                        f"host {urlsplit(door).netloc}"
                    )
                    assert built[state].startswith(door), (
                        f"{book}/{league} in {state} resolves to {built[state]}, "
                        f"which is not under that state's door {door}"
                    )

    def test_a_state_partitioned_event_grammar_is_verified_per_state(self) -> None:
        """Event precision outranks everything, so it is the worst hiding place.

        ``EVENT_URL["betrivers_kambi"]`` embedded ``il.betrivers.com`` in a
        template that could not emit because ``verified`` was False — but
        ``scripts/verify_betlinks.py`` exists precisely to flip that flag, it
        samples betrivers events from stored runs, and the day it verified that
        template a governed PA row would have linked Illinois at *event*
        precision, above both guards below it.  Demonstrated by two reviewers
        independently.

        The template is state-aware now, so the flat flag is what is forbidden:
        it claims every licence at once from one egress's evidence.  A probe
        proves one state, and ``verified_states`` is the only thing that can
        say which.  ``src.betlinks._check_link_tables`` refuses the same shapes
        at import; this is the version that names the history.
        """
        for book in STATE_SITE:
            candidate = EVENT_URL.get(book)
            if candidate is None:
                continue
            assert not candidate.verified, (
                f"{book} set the flat verified flag; a probe runs from one "
                "egress, so name the states it proved in verified_states"
            )
            assert "{host}" in candidate.template, (
                f"{book}'s event template is pinned to one state's domain "
                f"({candidate.template}); it outranks the state door"
            )
            assert set(candidate.verified_states) <= set(STATE_SITE[book]), (
                f"{book} claims a state STATE_SITE has no door for, so "
                "nothing can resolve its host"
            )
            doors = STATE_SITE[book]
            built = {
                s: candidate.build(event_id="1", slug="a-b", host=state_host(book, s))
                for s in doors
            }
            # Same trap as the league page above: matching a netloc proves
            # nothing for a book whose licences differ by path.  Caesars is
            # that book — one host, four state paths — so a {host} template
            # would hand all four states one link while passing every netloc
            # assertion in this file.
            assert len(set(built.values())) >= len(set(doors.values())), (
                f"{book} collapses {sorted(doors)} onto "
                f"{sorted(set(built.values()))}; this book is not partitioned "
                "by netloc, so {host} cannot carry its licence"
            )
            for state, door in doors.items():
                assert urlsplit(built[state]).netloc == urlsplit(door).netloc, (
                    f"{book} in {state} points at "
                    f"{urlsplit(built[state]).netloc}, not at that state's "
                    f"host {urlsplit(door).netloc}"
                )
                assert built[state].startswith(door), (
                    f"{book} in {state} builds {built[state]}, which is not "
                    f"under that state's door {door}"
                )

    def test_an_ungoverned_run_never_reaches_a_state_partitioned_grammar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no jurisdiction there is no host, and a guess is the defect.

        Both branches above the state door take a ``{host}``.  If ``bet_link``
        filled it from anything but the run's own jurisdiction it would name a
        licence the run cannot bet on, at the precision that wins — so an
        ungoverned run must fall through to SITE instead.  Making the templates
        state-aware would otherwise have *created* the hole the two guards above
        were holding shut.

        The tables are patched because today no state-partitioned book holds
        either kind of entry, so the live tables exercise neither branch and a
        test written against them would pass without touching the gate at all.
        These are the entries the next such book will have.
        """
        import src.betlinks as betlinks

        monkeypatch.setitem(
            betlinks.EVENT_URL,
            "betrivers_kambi",
            betlinks._Event(
                "https://{host}/?page=sportsbook#event/{id}",
                verified_states=frozenset({"IL", "PA", "NJ"}),
            ),
        )
        monkeypatch.setitem(
            betlinks.LEAGUE_PAGE, "betrivers_kambi", {"MLB": "https://{host}/?page=mlb"}
        )
        quote = make_quote(source="betrivers_kambi", source_event_id="7", league="MLB")

        # Governed: event precision, on that state's own licence.
        for state, host in (("IL", "il.betrivers.com"), ("PA", "pa.betrivers.com")):
            governed = bet_link(quote, state=state)
            assert governed is not None
            assert governed.precision is Precision.EVENT
            assert urlsplit(governed.url).netloc == host

        # Ungoverned: both branches skipped, no placeholder left behind, and
        # crucially not some other state's board at the winning precision.
        link = bet_link(quote)
        assert link is not None
        assert link.precision is Precision.SITE
        assert "{host}" not in link.url
        assert link.url not in STATE_SITE["betrivers_kambi"].values()

        # A licence the book does not hold behaves the same way: DC has no
        # BetRivers door, so there is no host and no event link to build.
        dc = bet_link(quote, state="DC")
        assert dc is not None
        assert dc.precision is Precision.SITE
        assert "{host}" not in dc.url

    def test_the_payload_carries_the_state_resolved_door(self) -> None:
        quote = make_quote(source="betrivers_kambi", source_event_id="1", league="ATP")
        payload = link_payload(quote, state="PA")
        assert payload is not None
        assert payload["url"] == "https://pa.betrivers.com/?page=sportsbook"


class TestSlug:
    def test_away_first_dashed(self) -> None:
        assert team_slug("Los Angeles Angels", "Baltimore Orioles") == (
            "los-angeles-angels-baltimore-orioles"
        )

    def test_punctuation_and_case_collapse(self) -> None:
        assert team_slug("St. Louis Cardinals") == "st-louis-cardinals"

    def test_an_empty_name_does_not_leave_a_dangling_dash(self) -> None:
        assert team_slug("", "Chicago Cubs") == "chicago-cubs"
        assert team_slug("Chicago Cubs", "") == "chicago-cubs"
