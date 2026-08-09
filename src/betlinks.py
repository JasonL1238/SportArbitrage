"""Where to actually place each leg of a position.

A detected arbitrage is only takeable if you can reach both bet slips before the
prices move, so every leg carries a link.  Three things make that harder than
formatting a template:

**A mirrored row's event id is not the book's.**  ``an_fanduel``,
``vi_draftkings`` and ``vsin_circa`` read FanDuel / DraftKings / Circa prices out
of an aggregator's payload, so their ``source_event_id`` is *the aggregator's*
id.  Interpolating it into the book's own event URL yields a link that resolves
to somebody else's game, which is worse than no link: it is a wrong link that
looks right.  Mirrors therefore resolve to the book they mirror and stop at that
book's league page.

**An unverified template is a guess.**  Books change URL grammars without
notice, several never accepted a bare event id, and a 404 on a live slate is
indistinguishable to a reader from "this arb went away".  So every event
template here carries :attr:`_Event.verified`, and :func:`bet_link` refuses to
emit an event URL until a live probe has set it.  ``scripts/verify_betlinks.py``
is that probe; it reports what resolved and what did not, and the flags below
are its output rather than anyone's expectation.  A template pinned to one
state's domain must be made state-aware **before** it may verify — the guard
test on :data:`STATE_SITE` books refuses the flip — because event precision
outranks the state door.

**A league page is always a true statement.**  When there is no verified event
template the link degrades to the book's page for that league, and
:attr:`BetLink.precision` says so, so the dashboard and the SMS can tell the
reader whether they are one click from the slip or several.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol


class Precision(StrEnum):
    """How close to the bet slip a link actually lands."""

    EVENT = "event"
    """The game's own page on the book."""
    LEAGUE = "league"
    """The book's page for that league — find the game from there."""
    SITE = "site"
    """The book's front door, when even its league grammar is unknown."""


@dataclass(frozen=True)
class BetLink:
    url: str
    precision: Precision
    book: str
    """The venue the bet is actually placed at.

    Differs from the row's ``source`` for every mirrored feed: an ``an_fanduel``
    price is placed at FanDuel, and a reader told to go to Action Network would
    be sent somewhere they cannot bet.
    """

    mirrored: bool = False
    """True when the price was read off an aggregator rather than the book."""


class _Priced(Protocol):
    """The part of :class:`src.schema.Quote` a link is built from."""

    source: str
    source_event_id: str
    league: str
    home_team: str
    away_team: str


#: Mirror feed → the book whose prices it republishes.  ``an_open`` is the
#: aggregator's own consensus line rather than one venue's, so it has no book.
MIRROR_BOOK: Mapping[str, str] = {
    "an_bally": "bally",
    "an_bet365": "bet365",
    "an_betmgm": "betmgm",
    "an_betrivers": "betrivers_kambi",
    "an_bovada": "bovada",
    "an_caesars": "caesars",
    "an_circa": "circa",
    "an_draftkings": "draftkings",
    "an_fanatics": "fanatics",
    "an_fanduel": "fanduel",
    "an_fliff": "fliff",
    "an_hardrock": "hardrock",
    "an_onexbet": "onexbet",
    "an_superbook": "superbook",
    "an_parx": "parx",
    "an_thescore": "thescore",
    # Action Network files this book as ``UnibetPA``; the Pennsylvania online
    # skin ran on Unibet's licence, so the front door is Unibet's.  Recorded
    # here rather than under a Mohegan URL because that mapping is unverified.
    "an_unibet": "unibet",
    "vi_bet365": "bet365",
    "vi_caesars": "caesars",
    "vi_draftkings": "draftkings",
    "vi_fanatics": "fanatics",
    "vi_hardrock": "hardrock",
    "vi_betmgm": "betmgm",
    "vi_betrivers": "betrivers_kambi",
    "vi_fanduel": "fanduel",
    "vsin_circa": "circa",
}

#: Mirror feeds that do not name a single venue, so no bet slip exists.
CONSENSUS_FEEDS: frozenset[str] = frozenset({"an_open"})

#: Book → jurisdiction → that state's own front door, for the books whose site
#: is partitioned by licence rather than geolocated.  Consulted before
#: :data:`SITE`, and only when the caller states the run's jurisdiction.
#:
#: This table exists because the state-blind one below sent a Pennsylvania
#: bettor to Illinois: run 32 — the run PA's routes were promoted on — produced
#: two real opportunities whose ``betrivers_kambi`` legs each carried
#: ``il.betrivers.com`` under a "PLACE BOTH NOW", while the prices had come
#: from ``rsiuspa``/``market=US-PA``.  A link to another licence's site does
#: not show this state's slip, which is rule (a)'s exact failure worn by a URL;
#: the seconds it wastes are the seconds the edge is made of.  The BetRivers
#: doors agree with :attr:`src.jurisdictions.PromoRoute.betrivers_url` per
#: state — pinned by test, because two spellings of one door will drift.
STATE_SITE: Mapping[str, Mapping[str, str]] = {
    "betrivers_kambi": {
        "IL": "https://il.betrivers.com/?page=sportsbook",
        "PA": "https://pa.betrivers.com/?page=sportsbook",
        "NJ": "https://nj.betrivers.com/?page=sportsbook",
        # No DC entry: BetRivers holds no DC licence, and no DC run can carry
        # a takeable betrivers_kambi leg to link.
    },
    "caesars": {
        "IL": "https://sportsbook.caesars.com/us/il/bet",
        "PA": "https://sportsbook.caesars.com/us/pa/bet",
        "NJ": "https://sportsbook.caesars.com/us/nj/bet",
        "DC": "https://sportsbook.caesars.com/us/dc/bet",
    },
    "unibet": {
        "PA": "https://pa.unibet.com",
        "NJ": "https://nj.unibet.com",
        # The only feed that produces a unibet leg is ``an_unibet``, whose book
        # ids exist for PA (246) and NJ (247) alone.
    },
}

#: Book → front door.  Every key reachable from :data:`MIRROR_BOOK` or the
#: registry has an entry, because :func:`bet_link` must always return something.
#: For the books in :data:`STATE_SITE` these are the *stateless* fallbacks — a
#: brand chooser or geolocating landing — used only when no jurisdiction
#: governs the run.
SITE: Mapping[str, str] = {
    "bally": "https://play.ballybet.com/sports",
    "bet365": "https://www.bet365.com",
    "betmgm": "https://sports.betmgm.com/en/sports",
    "betrivers_kambi": "https://www.betrivers.com",
    "bovada": "https://www.bovada.lv/sports",
    "caesars": "https://sportsbook.caesars.com",
    "circa": "https://www.circasports.com",
    "cloudbet": "https://www.cloudbet.com/en/sports",
    "draftkings": "https://sportsbook.draftkings.com",
    "fanatics": "https://sportsbook.fanatics.com",
    "fanduel": "https://sportsbook.fanduel.com",
    "fliff": "https://www.getfliff.com",
    "hardrock": "https://app.hardrock.bet",
    "kalshi": "https://kalshi.com",
    "leovegas_kambi": "https://www.leovegas.com/en-ca/sport",
    "matchbook": "https://www.matchbook.com",
    "onexbet": "https://1xbet.com/en",
    "pinnacle": "https://www.pinnacle.com/en",
    "polymarket": "https://polymarket.com",
    "smarkets": "https://smarkets.com",
    "superbook": "https://co.superbook.com",
    "parx": "https://www.betparx.com",
    "thescore": "https://www.thescore.bet",
    # Deliberately Pennsylvania's door, in knowing tension with the "stateless
    # fallbacks" rule above: Unibet has no US brand chooser — ``unibet.com`` is
    # the global gambling site, worse than any US state's page.  The protection
    # is the *inventory*, not this resolver: ``an_unibet``, the only feed that
    # produces a unibet leg, is built for PA and NJ runs alone (both resolved
    # by STATE_SITE) and is view-only everywhere, so no money surface reaches
    # this entry.  A governed IL or DC run asking would receive PA's door — it
    # cannot ask, because no such run can hold the feed.  ``superbook`` two
    # entries up is the same shape: Colorado's own door, behind a feed
    # (``an_superbook``, Westgate id 14) that has never produced a row.
    "unibet": "https://pa.unibet.com",
    "sxbet": "https://sx.bet",
}

#: Book → league key → that league's page.  Only entries whose grammar is known
#: are listed; anything missing degrades to :data:`SITE`.
LEAGUE_PAGE: Mapping[str, Mapping[str, str]] = {
    "draftkings": {
        "MLB": "https://sportsbook.draftkings.com/leagues/baseball/mlb",
        "NBA": "https://sportsbook.draftkings.com/leagues/basketball/nba",
        "WNBA": "https://sportsbook.draftkings.com/leagues/basketball/wnba",
        "NHL": "https://sportsbook.draftkings.com/leagues/hockey/nhl",
        "NFL": "https://sportsbook.draftkings.com/leagues/football/nfl",
        "EPL": "https://sportsbook.draftkings.com/leagues/soccer/england---premier-league",
        "MLS": "https://sportsbook.draftkings.com/leagues/soccer/mls",
    },
    "fanduel": {
        "MLB": "https://sportsbook.fanduel.com/navigation/mlb",
        "NBA": "https://sportsbook.fanduel.com/navigation/nba",
        "WNBA": "https://sportsbook.fanduel.com/navigation/wnba",
        "NHL": "https://sportsbook.fanduel.com/navigation/nhl",
        "NFL": "https://sportsbook.fanduel.com/navigation/nfl",
    },
    "bovada": {
        "MLB": "https://www.bovada.lv/sports/baseball/mlb",
        "NBA": "https://www.bovada.lv/sports/basketball/nba",
        "WNBA": "https://www.bovada.lv/sports/basketball/wnba",
        "NHL": "https://www.bovada.lv/sports/hockey/nhl",
        "NFL": "https://www.bovada.lv/sports/football/nfl",
        "EPL": "https://www.bovada.lv/sports/soccer/england-premier-league",
        "MLS": "https://www.bovada.lv/sports/soccer/usa-mls",
    },
    "pinnacle": {
        "MLB": "https://www.pinnacle.com/en/baseball/mlb/matchups/",
        "NBA": "https://www.pinnacle.com/en/basketball/nba/matchups/",
        "WNBA": "https://www.pinnacle.com/en/basketball/wnba/matchups/",
        "NHL": "https://www.pinnacle.com/en/hockey/nhl/matchups/",
        "NFL": "https://www.pinnacle.com/en/football/nfl/matchups/",
        "EPL": "https://www.pinnacle.com/en/soccer/england-premier-league/matchups/",
    },
    "betmgm": {
        "MLB": "https://sports.betmgm.com/en/sports/baseball-23/betting/usa-9/mlb-75",
        "NBA": "https://sports.betmgm.com/en/sports/basketball-7/betting/usa-9/nba-6004",
        "NFL": "https://sports.betmgm.com/en/sports/football-11/betting/usa-9/nfl-35",
        "NHL": "https://sports.betmgm.com/en/sports/ice-hockey-12/betting/usa-9/nhl-34",
    },
    "smarkets": {
        "MLB": "https://smarkets.com/sport/baseball/mlb",
        "NBA": "https://smarkets.com/sport/basketball/nba",
        "WNBA": "https://smarkets.com/sport/basketball/wnba",
        "NFL": "https://smarkets.com/sport/american-football/nfl",
        "EPL": "https://smarkets.com/sport/football/english-premier-league",
    },
    "matchbook": {
        "MLB": "https://www.matchbook.com/sport/baseball",
        "NBA": "https://www.matchbook.com/sport/basketball",
        "NFL": "https://www.matchbook.com/sport/american-football",
        "EPL": "https://www.matchbook.com/sport/soccer",
    },
    "cloudbet": {
        "MLB": "https://www.cloudbet.com/en/sports/baseball/usa-mlb",
        "NBA": "https://www.cloudbet.com/en/sports/basketball/usa-nba",
        "NFL": "https://www.cloudbet.com/en/sports/american-football/usa-nfl",
    },
}


@dataclass(frozen=True)
class _Event:
    """One book's candidate event-URL grammar.

    ``template`` is formatted with ``id`` (the row's ``source_event_id``) and
    ``slug`` (dashed team names, away first).  ``verified`` is set only from a
    live ``verify`` run — see the module docstring for why it defaults to False.
    """

    template: str
    verified: bool = False
    note: str = ""

    def build(self, *, event_id: str, slug: str) -> str:
        return self.template.format(id=event_id, slug=slug)


#: Book → candidate event grammar, with what the last live probe measured.
#: ``scripts/verify_betlinks.py`` on 2026-08-04, Chrome-impersonated, from an
#: Illinois egress, against future-dated events out of stored run 18.
#:
#: Only two books can be confirmed from HTML.  Eight serve a client-rendered
#: shell that is byte-identical for a real id and a fabricated one, so their
#: grammar is neither proved nor disproved and the flag stays False — a link
#: that *might* be right is exactly what this module refuses to emit.  Re-run the
#: script from a browser that executes JS, or from a region that is not blocked,
#: to settle the rest.
EVENT_URL: Mapping[str, _Event] = {
    "draftkings": _Event(
        "https://sportsbook.draftkings.com/event/{id}",
        verified=True,
        note="2026-08-04: resolved and named the game on every sample",
    ),
    "smarkets": _Event(
        "https://smarkets.com/event/{id}",
        verified=True,
        note="2026-08-04: resolved and named the game on every sample",
    ),
    "fanduel": _Event(
        "https://sportsbook.fanduel.com/event/{id}",
        note="2026-08-04: client-rendered, a bogus id returns the same shell",
    ),
    "pinnacle": _Event(
        "https://www.pinnacle.com/en/betting/matchup/{id}",
        note="2026-08-04: client-rendered, a bogus id returns the same shell",
    ),
    "betmgm": _Event(
        "https://sports.betmgm.com/en/sports/events/{id}",
        note=(
            "2026-08-04: client-rendered; it also redirects the id to a slug of "
            "its own, and on two samples that slug was a different event, so "
            "source_event_id is probably not the id this URL takes"
        ),
    ),
    "matchbook": _Event(
        "https://www.matchbook.com/events/{id}",
        note="2026-08-04: client-rendered, a bogus id returns the same shell",
    ),
    "betrivers_kambi": _Event(
        "https://il.betrivers.com/?page=sportsbook#event/{id}",
        note="2026-08-04: client-rendered; the id is in the fragment, never sent",
    ),
    "kalshi": _Event(
        "https://kalshi.com/markets/{id}",
        note="2026-08-04: client-rendered; earlier probe also rate-limited (429)",
    ),
    "sxbet": _Event(
        "https://sx.bet/markets/{id}",
        note="2026-08-04: client-rendered, a bogus id returns the same shell",
    ),
    "onexbet": _Event(
        "https://1xbet.com/en/line/{id}",
        note="2026-08-04: redirected to /en/block — geo-blocked, grammar untested",
    ),
    "cloudbet": _Event(
        "https://www.cloudbet.com/en/sports/event/{id}",
        note="2026-08-04: HTTP 500 on every sample — this grammar is wrong",
    ),
    "leovegas_kambi": _Event(
        "https://www.leovegas.com/en-ca/sport#event/{id}",
        note="2026-08-04: HTTP 404 on every sample — this grammar is wrong",
    ),
    "polymarket": _Event(
        "https://polymarket.com/event/{slug}",
        note=(
            "2026-08-04: HTTP 404 — Polymarket slugs are market questions, not "
            "team names, and nothing we store reconstructs one"
        ),
    ),
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def team_slug(*names: str) -> str:
    """Dashed slug from display names, in the order given.

    Books that name an event in its URL spell it away-first, so callers pass
    ``away, home``.
    """
    parts = [_SLUG_STRIP.sub("-", (name or "").lower()).strip("-") for name in names]
    return "-".join(part for part in parts if part)


def book_for(source: str) -> str | None:
    """The venue a row's price is placed at, or None for a consensus feed."""
    if source in CONSENSUS_FEEDS:
        return None
    return MIRROR_BOOK.get(source, source)


def bet_link(priced: _Priced, *, state: str | None = None) -> BetLink | None:
    """The closest link to this row's bet slip that is a true statement.

    Returns None only for a consensus feed, where no single venue takes the bet.
    *state* is the run's governed jurisdiction, when one governs: for the books
    in :data:`STATE_SITE` it selects that licence's own front door, because a
    state-partitioned book's Illinois site does not show Pennsylvania's slip.
    Left ``None`` — a legacy or global run — those books degrade to their
    stateless landing rather than to some other state's.
    """
    source = priced.source
    book = book_for(source)
    if book is None:
        return None
    mirrored = source in MIRROR_BOOK

    # A mirrored row never reaches an event page: the id belongs to the
    # aggregator that published the price, not to the book that posted it.
    if not mirrored:
        candidate = EVENT_URL.get(book)
        if candidate is not None and candidate.verified:
            slug = team_slug(priced.away_team, priced.home_team)
            url = candidate.build(event_id=priced.source_event_id, slug=slug)
            return BetLink(url, Precision.EVENT, book, mirrored=False)

    league_pages = LEAGUE_PAGE.get(book) or {}
    league_url = league_pages.get(priced.league)
    if league_url:
        return BetLink(league_url, Precision.LEAGUE, book, mirrored=mirrored)

    if state:
        state_site = STATE_SITE.get(book, {}).get(state.strip().upper())
        if state_site:
            return BetLink(state_site, Precision.SITE, book, mirrored=mirrored)
    site = SITE.get(book)
    if site:
        return BetLink(site, Precision.SITE, book, mirrored=mirrored)
    return None


def link_payload(priced: _Priced, *, state: str | None = None) -> dict[str, object] | None:
    """JSON shape for the dashboard and the SMS formatter."""
    link = bet_link(priced, state=state)
    if link is None:
        return None
    return {
        "url": link.url,
        "precision": link.precision.value,
        "book": link.book,
        "mirrored": link.mirrored,
    }


__all__ = [
    "CONSENSUS_FEEDS",
    "EVENT_URL",
    "LEAGUE_PAGE",
    "MIRROR_BOOK",
    "SITE",
    "STATE_SITE",
    "BetLink",
    "Precision",
    "bet_link",
    "book_for",
    "link_payload",
    "team_slug",
]
