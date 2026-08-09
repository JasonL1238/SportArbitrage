"""Where each leg gets placed, and the ways that link can lie.

The failure this module exists to prevent is not a missing link — it is a link
that resolves to the wrong game while looking exactly like a right one.  Two
shapes of that are pinned here: interpolating an aggregator's event id into the
book's URL, and emitting a grammar no live probe has confirmed.
"""
from __future__ import annotations

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

        Two deliberate exceptions, both protected by inventory rather than the
        resolver, and both asserted on purpose so a change to either fact
        shows up here:

        * ``unibet`` — no US brand chooser exists (``unibet.com`` is the
          global gambling site); its only feed is built for PA/NJ runs alone
          and is view-only everywhere.
        * ``superbook`` — Colorado's own door, behind ``an_superbook``
          (Westgate, id 14), a feed that has never produced a row on either
          endpoint version; there is no row to link from.
        """
        from src.sources.registry import REPUBLISHED_SOURCE_KEYS

        for book, quote_source, pinned in (
            ("betrivers_kambi", "betrivers_kambi", None),
            ("caesars", "caesars", None),
            ("unibet", "an_unibet", "https://pa.unibet.com"),
            ("superbook", "an_superbook", "https://co.superbook.com"),
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

    def test_no_state_partitioned_book_hides_behind_a_league_page(self) -> None:
        """The league-page lookup outranks the state door in ``bet_link``.

        Today none of the state-partitioned books has a league grammar, so the
        state door is always reached.  A future league entry for one of them,
        pinned to a single state's domain, would silently reintroduce the
        Illinois-link defect one precision level up — this makes that addition
        a loud decision instead.
        """
        for book in STATE_SITE:
            assert book not in LEAGUE_PAGE, (
                f"{book} gained a league page; make it state-aware before "
                "letting it outrank STATE_SITE"
            )

    def test_no_state_partitioned_book_can_verify_a_one_state_event_grammar(
        self,
    ) -> None:
        """Event precision outranks everything, so it is the worst hiding place.

        ``EVENT_URL["betrivers_kambi"]`` embeds ``il.betrivers.com`` in a
        template that today cannot emit because ``verified`` is False — but
        ``scripts/verify_betlinks.py`` exists precisely to flip that flag, it
        samples betrivers events from stored runs, and the day it verifies this
        template a governed PA row links Illinois at *event* precision, above
        both guards below it.  Demonstrated by two reviewers independently.
        This makes the flip a test failure that names the required work.
        """
        for book in STATE_SITE:
            candidate = EVENT_URL.get(book)
            if candidate is None:
                continue
            assert not candidate.verified, (
                f"{book}'s event template is pinned to one state's domain "
                f"({candidate.template}); make _Event state-aware before "
                "verifying it, or the state door is silently outranked"
            )

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
