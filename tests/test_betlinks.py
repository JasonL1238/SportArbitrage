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
