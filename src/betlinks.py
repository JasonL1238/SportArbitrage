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
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit


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
    "an_draftkings": "draftkings",
    "an_fanatics": "fanatics",
    "an_fanduel": "fanduel",
    "an_hardrock": "hardrock",
    # betPARX gained a first-party Kambi route on 2026-08-23, so the brand
    # key is that source key, as it is for BetRivers, DraftKings and theScore.
    "an_parx": "betparx_kambi",
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
    # betPARX is partitioned by licence the same way: ``pa.betparx.com`` and
    # ``nj.betparx.com`` each mount their own Kambi widget at ``/kambi``.
    # No IL or DC door, because no licence — the routes say the same.
    "betparx_kambi": {
        "PA": "https://pa.betparx.com/kambi",
        "NJ": "https://nj.betparx.com/kambi",
    },
    "caesars": {
        "IL": "https://sportsbook.caesars.com/us/il/bet",
        "PA": "https://sportsbook.caesars.com/us/pa/bet",
        "NJ": "https://sportsbook.caesars.com/us/nj/bet",
        "DC": "https://sportsbook.caesars.com/us/dc/bet",
    },
    "bet365": {
        # The stateless www.bet365.com serves no board at all, so the site-level
        # fallback in SITE is a door onto nothing.  These are the hosts that
        # actually carry a licensed board.
        "IL": "https://www.il.bet365.com",
        "PA": "https://www.pa.bet365.com",
        "NJ": "https://www.nj.bet365.com",
        # No DC entry: bet365 holds no DC licence, which src.sources.research
        # records as an unavailable state rather than a missing route.
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
    "hardrock": "https://app.hardrock.bet",
    "kalshi": "https://kalshi.com",
    "leovegas_kambi": "https://www.leovegas.com/en-ca/sport",
    "matchbook": "https://www.matchbook.com",
    "onexbet": "https://1xbet.com/en",
    "pinnacle": "https://www.pinnacle.com/en",
    # Site-level only, and deliberately so.  The collected events carry a ``slug``
    # and a ``ticker``, so an event URL looks derivable — but no pattern has been
    # requested and confirmed, and this table's rule is evidence rather than a
    # plausible guess.  Deriving one belongs with a ``scripts/verify_betlinks.py``
    # pass, not here.
    "polymarket_us": "https://polymarket.us",
    "smarkets": "https://smarkets.com",
    "betparx_kambi": "https://www.betparx.com",
    "thescore": "https://www.thescore.bet",
    # Deliberately Pennsylvania's door, in knowing tension with the "stateless
    # fallbacks" rule above: Unibet has no US brand chooser — ``unibet.com`` is
    # the global gambling site, worse than any US state's page.  The protection
    # is the *inventory*, not this resolver: ``an_unibet``, the only feed that
    # produces a unibet leg, is built for PA and NJ runs alone (both resolved
    # by STATE_SITE) and is view-only everywhere, so no money surface reaches
    # this entry.  A governed IL or DC run asking would receive PA's door — it
    # cannot ask, because no such run can hold the feed.
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


def state_host(book: str, state: str) -> str | None:
    """The netloc this book serves *this* state's board on, or None.

    Derived from :data:`STATE_SITE` rather than from a table of its own.  The
    per-state door is already spelled once there and pinned by test against
    :attr:`src.jurisdictions.PromoRoute.betrivers_url`; a second table of hosts
    would be a fourth spelling of the same fact, and the defect this module
    exists to prevent is precisely two spellings of one door drifting apart.
    """
    door = STATE_SITE.get(book, {}).get(state.strip().upper())
    return urlsplit(door).netloc if door else None


@dataclass(frozen=True)
class _Event:
    """One book's candidate event-URL grammar.

    ``template`` is formatted with ``id`` (the row's ``source_event_id``),
    ``slug`` (dashed team names, away first), and — for a book in
    :data:`STATE_SITE` — ``host``, that state's own netloc.

    **Verification is per state for a state-partitioned book.**  A probe runs
    from one egress, so proving Illinois' grammar says nothing about
    Pennsylvania's: the host differs, and so may the routing behind it.  Those
    books therefore carry ``verified_states`` and may not set the flat
    ``verified`` at all — :func:`_check_link_tables` refuses it at import.
    Books served from one domain everywhere keep the plain flag.
    """

    template: str
    verified: bool = False
    verified_states: frozenset[str] = frozenset()
    note: str = ""

    def verified_for(self, state: str | None) -> bool:
        """Is this grammar proven for the jurisdiction governing the run?"""
        if self.verified_states:
            return state is not None and state.strip().upper() in self.verified_states
        return self.verified

    def build(self, *, event_id: str, slug: str, host: str = "") -> str:
        return self.template.format(id=event_id, slug=slug, host=host)


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
        "https://{host}/?page=sportsbook#event/{id}",
        note=(
            "2026-08-04: client-rendered; the id is in the fragment, never sent. "
            "Was spelled il.betrivers.com until 2026-08-15, when _Event learned "
            "states: the Illinois host was the one the probe happened to run "
            "from, not a property of the grammar"
        ),
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
    # No bet365 entry, and that is a measurement rather than an omission.  Its
    # address bar mirrors the app's own navigation token segment for segment —
    # captured 2026-08-16 as ``#/AC/B16/C20525425/D48/E1096/F10/`` — and a
    # fixture row's token is the same shape:
    # ``#AC#B16#C20525425#D19#E26475424#F19#I0#P951933#H1#``.  The event id is
    # one segment of several; class, competition, template and group are equally
    # required, and a ``Quote`` keeps none of them.  So no ``{id}`` template can
    # address a bet365 event: reaching event precision here is a schema change
    # (carry the token) and not a link-table addition.  Note also that ``#E``
    # means *two* things in that grammar — the event on a fixture row, a layout
    # template on the league link — so the obvious pattern-match is wrong twice.
}

_HOST_PLACEHOLDER = "{host}"


def _names_a_state(template: str) -> str | None:
    """Does the constant part of this template name a jurisdiction?

    The host checks below compare *where* a URL points, and a template can
    satisfy them while still asking the wrong licence a question:
    ``https://{host}/?page=sportsbook&market=US-IL#event/{id}`` resolves to
    Pennsylvania's own host and then names Illinois' market in the query.  Only
    the host varies per state, so every distinctness and prefix test passes.

    A state-partitioned template must take its jurisdiction from ``{host}`` and
    from nowhere else, so any state code in the surrounding literal is a
    contradiction.  Matched as a whole token, which is why ``sportsbook`` and
    ``il.betrivers.com`` (inside the placeholder's own expansion, not the
    literal) do not trip it.
    """
    literal = template.replace(_HOST_PLACEHOLDER, "")
    for state in {s for doors in STATE_SITE.values() for s in doors}:
        if re.search(rf"(?<![A-Za-z0-9]){state}(?![A-Za-z0-9])", literal, re.I):
            return state
    return None


def _collapses_across_states(
    book: str, build: Callable[[str], str]
) -> str | None:
    """Does this template give two different licences the same URL?

    ``{host}`` is a netloc, and **not every state-partitioned book is
    partitioned by netloc**.  Caesars' four doors are one host with four paths
    (``sportsbook.caesars.com/us/il/bet`` … ``/us/dc/bet``), so a ``{host}``
    template resolves all four to the same URL — the Illinois-link defect
    exactly, wearing the placeholder that was supposed to prevent it.  Worse,
    a netloc-to-netloc assertion *passes* it, because the netloc really does
    match in every state.

    So the rule is distinctness rather than shape: states with different doors
    must end up with different URLs.  Returns a message when they do not.
    """
    doors = STATE_SITE.get(book, {})
    built = {state: build(state) for state in doors}
    if len(set(built.values())) < len(set(doors.values())):
        return (
            f"resolves to {sorted(set(built.values()))} across {sorted(doors)}, "
            f"which is fewer distinct URLs than {book} has distinct doors — "
            "{host} is a netloc, and this book is partitioned by something else "
            "(a path, a segment), so every state would share one link"
        )
    # Distinctness alone is not enough, and neither is a prefix.  Two more
    # things have to hold, and each catches what the other misses:
    #
    # * the **netloc must match** that state's door.  ``startswith`` is a
    #   string test, not a host test, and the doors of a host-partitioned book
    #   carry no path — so ``https://www.il.bet365.com.elsewhere.example/mlb``
    #   begins with Illinois' whole door string while pointing at a host that
    #   is not bet365 at all.  ``…@elsewhere.example`` is the same trick made
    #   to read as bet365 to a human.
    # * the URL must **sit under** the door, which is what catches a template
    #   hardcoding one state's domain while carrying ``{host}`` somewhere
    #   decorative, and any book partitioned by path rather than by host.
    astray = {
        state: url
        for state, url in built.items()
        if urlsplit(url).netloc != urlsplit(doors[state]).netloc
        or not url.startswith(doors[state])
    }
    if astray:
        return (
            f"builds {sorted(astray.values())} for {sorted(astray)}, which is "
            "not on those states' own doors "
            f"({sorted(doors[s] for s in astray)}) — a link at this precision "
            "outranks the state door, so it must be that state's licence"
        )
    return None


def _check_link_tables() -> None:
    """Refuse, at import, a link table that outranks the state door wrongly.

    Two tests used to enforce this by forbidding the entries outright — no
    :data:`STATE_SITE` book could hold a :data:`LEAGUE_PAGE` entry, and none
    could set ``verified``.  That was the right call while nothing could spell
    a per-state URL; now that :class:`_Event` can, the ban becomes a shape rule,
    and it moves here so it holds for anyone importing the module rather than
    only for the suite.

    What the checks are actually protecting: :func:`bet_link` reaches event and
    league precision *before* the state door, so a template carrying one state's
    literal domain sends a Pennsylvania bettor to Illinois at the precision that
    wins.  That defect shipped once — run 32, two real opportunities whose
    ``betrivers_kambi`` legs read ``il.betrivers.com`` under a "PLACE BOTH NOW"
    while the prices came from ``rsiuspa``.
    """
    errors: list[str] = []
    for book, candidate in EVENT_URL.items():
        partitioned = book in STATE_SITE
        if not partitioned:
            if candidate.verified_states:
                errors.append(
                    f"{book}: verified_states is for books whose host differs "
                    "by licence; this book serves one domain, so the plain "
                    "verified flag is the honest claim"
                )
            if _HOST_PLACEHOLDER in candidate.template:
                errors.append(
                    f"{book}: template takes {{host}} but the book is not in "
                    "STATE_SITE, so nothing can fill it"
                )
            continue
        if candidate.verified:
            errors.append(
                f"{book}: the flat verified flag claims every state at once, "
                "but a probe runs from one egress — use verified_states"
            )
        if _HOST_PLACEHOLDER not in candidate.template:
            errors.append(
                f"{book}: event template {candidate.template!r} hardcodes a "
                "host for a book whose board is partitioned by licence; event "
                "precision outranks the state door, so this links one state's "
                "site from every state's run"
            )
        unknown = set(candidate.verified_states) - set(STATE_SITE[book])
        if unknown:
            errors.append(
                f"{book}: verified_states names {sorted(unknown)}, which "
                "STATE_SITE has no door for — nothing can resolve their host"
            )
        if _HOST_PLACEHOLDER in candidate.template:
            named = _names_a_state(candidate.template)
            if named:
                errors.append(
                    f"{book}: event template {candidate.template!r} names "
                    f"{named} in its literal text; a state-partitioned grammar "
                    "must take its jurisdiction from {host} alone"
                )
            collapsed = _collapses_across_states(
                book,
                lambda state, _b=book, _c=candidate: _c.build(
                    event_id="1", slug="a-b", host=state_host(_b, state) or ""
                ),
            )
            if collapsed:
                errors.append(f"{book}: event template {collapsed}")
    for book, pages in LEAGUE_PAGE.items():
        if book not in STATE_SITE:
            # Nothing fills ``{host}`` for a book with no per-state doors, and
            # ``bet_link`` deliberately does not call ``.format`` on these, so
            # the placeholder would reach the reader as literal ``{host}`` in
            # the URL.  The EVENT_URL loop above already refuses this shape;
            # skipping the whole book here left the same hole one table over.
            for league, url in pages.items():
                if _HOST_PLACEHOLDER in url:
                    errors.append(
                        f"{book}/{league}: league page {url!r} takes {{host}} "
                        "but the book has no per-state doors to fill it from, "
                        "so the placeholder ships to the reader verbatim"
                    )
            continue
        for league, url in pages.items():
            if _HOST_PLACEHOLDER not in url:
                errors.append(
                    f"{book}/{league}: league page {url!r} hardcodes a host for "
                    "a book partitioned by licence, and league precision "
                    "outranks the state door"
                )
                continue
            named = _names_a_state(url)
            if named:
                errors.append(
                    f"{book}/{league}: league page {url!r} names {named} in "
                    "its literal text; a state-partitioned grammar must take "
                    "its jurisdiction from {host} alone"
                )
            collapsed = _collapses_across_states(
                book,
                lambda state, _b=book, _u=url: _u.format(
                    host=state_host(_b, state) or ""
                ),
            )
            if collapsed:
                errors.append(f"{book}/{league}: league page {collapsed}")
    if errors:
        raise RuntimeError(
            "state-partitioned link templates are unsound:\n- " + "\n- ".join(errors)
        )


_check_link_tables()

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
    stateless landing rather than to some other state's, at *every* precision:
    an unresolvable host skips the event and league grammars outright instead
    of filling the placeholder with a guess.
    """
    source = priced.source
    book = book_for(source)
    if book is None:
        return None
    mirrored = source in MIRROR_BOOK

    # For a state-partitioned book, both branches below outrank the state door,
    # so an ungoverned run must not enter them: every template they hold spells
    # a host, and with no jurisdiction to spell it from the only available
    # answer is some *other* state's.  Falling through to SITE is the honest
    # one — less precise, and not a link to a licence this run cannot bet on.
    host = state_host(book, state) if state else None
    partitioned = book in STATE_SITE
    resolvable = host is not None if partitioned else True

    # A mirrored row never reaches an event page: the id belongs to the
    # aggregator that published the price, not to the book that posted it.
    if not mirrored and resolvable:
        candidate = EVENT_URL.get(book)
        if candidate is not None and candidate.verified_for(state):
            slug = team_slug(priced.away_team, priced.home_team)
            url = candidate.build(
                event_id=priced.source_event_id, slug=slug, host=host or ""
            )
            return BetLink(url, Precision.EVENT, book, mirrored=False)

    if resolvable:
        league_pages = LEAGUE_PAGE.get(book) or {}
        league_url = league_pages.get(priced.league)
        if league_url:
            # Only a partitioned book's entry is a template; the rest are plain
            # URLs and are returned untouched.  Formatting them anyway would be
            # a no-op today and a crash the day one carries a literal brace.
            if partitioned:
                league_url = league_url.format(host=host)
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
