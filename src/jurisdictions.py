"""Typed routing for US retail jurisdictions selected in a collection batch.

Retail sportsbooks expose the same product through state-specific public hosts,
tenant ids, and request parameters.  Those values belong here rather than in
the registry or report so collection, promotions, diagnostics, and presentation
all describe the same jurisdiction.

Routes marked ``template`` are structurally known but still need a successful
probe from that state's egress. Unavailable routes keep the source registered
without inventing an endpoint for a state where the book is not licensed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


class RouteStatus(StrEnum):
    VALIDATED = "validated"
    TEMPLATE = "template"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RetailRoute:
    source_key: str
    host: str
    config: Mapping[str, Any]
    status: RouteStatus
    routed_state: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))

    @property
    def warning(self) -> str | None:
        if self.status is RouteStatus.VALIDATED:
            return None
        if self.status is RouteStatus.TEMPLATE:
            return f"{self.source_key}: {self.routed_state} route is template-only"
        if self.status is RouteStatus.UNAVAILABLE:
            return f"{self.source_key}: no licensed route is available in {self.routed_state}"
        raise AssertionError(f"unhandled route status: {self.status}")


@dataclass(frozen=True)
class RepublishedRoute:
    """Which book a republisher must ask for when collecting in this state.

    A republisher is not state-neutral just because its own host is.  Action
    Network files **one book id per state licence** — ``FanDuel PA`` is 255 and
    ``FanDuel NJ`` is 69, they carry different prices, and asking for the wrong
    one is silently answered rather than refused.  The registry used to hold a
    single id per republisher key, all of them New Jersey's, so *every*
    republished row collected in Pennsylvania described a New Jersey book while
    being labelled and validated as the local one: ``an_caesars`` disagreed with
    the rest of the slate on 12 of 94 shared markets because it was quoting
    Caesars NJ against PA books, which reads as a parser fault and is not one.

    ``UNAVAILABLE`` is a real state: Hard Rock has no Pennsylvania licence and
    Bally Bet has no Illinois one, so there is no id to ask for and the source
    is not instantiated there rather than being pointed at another state's book.
    """

    config: Mapping[str, Any]
    status: RouteStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


@dataclass(frozen=True)
class PromoRoute:
    fanduel_region: str
    betrivers_url: str | None
    betrivers_label: str


@dataclass(frozen=True)
class Jurisdiction:
    state: str
    label: str
    routes: Mapping[str, RetailRoute]
    promos: PromoRoute
    live_validated: bool
    view_only_sources: frozenset[str] = frozenset()
    republished: Mapping[str, RepublishedRoute] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))
        object.__setattr__(self, "republished", MappingProxyType(dict(self.republished)))


_MGM_ACCESS_ID = "ZTg4YWEwMTgtZTlhYy00MWRkLWIzYWYtZjMzODI5ZDE0Mjc5"


def _route(
    source_key: str,
    url: str,
    config: Mapping[str, Any],
    status: RouteStatus,
    routed_state: str,
    detail: str = "",
) -> RetailRoute:
    return RetailRoute(
        source_key=source_key,
        host=urlsplit(url).netloc,
        config=config,
        status=status,
        routed_state=routed_state,
        detail=detail,
    )


#: Action Network's book id for each state licence, keyed by republisher source
#: key then state.  A missing state means the operator has no online licence
#: there to republish, which is an ``UNAVAILABLE`` route rather than a fallback.
#:
#: The ids for the nine books above were confirmed to return moneyline prices on
#: 2026-08-06 by naming the whole set on one request and counting rows per book.
#: That check is why the table is trusted and why ``an_superbook`` (Westgate,
#: id 14) never entered it: a Nevada book that never appeared in this payload.
#: Registering ids unverified is what bought three permanently empty sources
#: (``an_fliff``/``an_circa``/``an_superbook``, deregistered 2026-08-09).
#:
#: **Which endpoint version these ids answer on is now settled: ``web/v2``.**  An
#: earlier note here could not say, because the adapter defaulted to ``web/v1``
#: while the 2026-08-06 check was recorded against v2.  Both were asked the same
#: question on 2026-08-07: naming Pennsylvania's whole set,
#: ``bookIds=74,122,246,255,280,1534,1906,2791,3547,4623``, v2 answered keyed by
#: all ten on MLB, while v1's MLB board carried none of them and answered with an
#: unrelated set — 200, eleven games, plausible prices, and not one of the
#: licences asked for.  v1 is not uniformly blind to them (the same request
#: returned 74 and 122 on WNBA and NFL), which makes it worse rather than better:
#: a partial answer is one that looks like it worked.
#: :data:`ActionNetworkAdapter.DEFAULT_BASE_URL` is v2 for this reason, and only
#: the two offshore sources may leave it.
_AN_BOOK_IDS: Mapping[str, Mapping[str, int]] = MappingProxyType(
    {
        "an_fanduel": {"IL": 270, "PA": 255, "NJ": 69},
        "an_betmgm": {"IL": 282, "PA": 280, "NJ": 75, "DC": 369},
        "an_draftkings": {"IL": 1538, "PA": 1534, "NJ": 68},
        "an_caesars": {"IL": 279, "PA": 1906, "NJ": 123, "DC": 3585},
        "an_betrivers": {"IL": 262, "PA": 122, "NJ": 71},
        "an_bet365": {"IL": 3915, "PA": 3547, "NJ": 79},
        "an_fanatics": {"IL": 2990, "PA": 2791, "NJ": 2988, "DC": 3679},
        "an_hardrock": {"IL": 3646, "NJ": 2724},
        "an_bally": {"NJ": 4693},
        # State-licensed books with no first-party adapter here, so the
        # republished feed is the *only* observation path.  betPARX and Unibet
        # have no Illinois licence to republish; theScore Bet has one in all
        # three.
        #
        # **All three now parse.**  Captured 2026-08-08T16:38Z from a Pennsylvania
        # egress against a live pregame slate: 74 → 255 rows, 4623 → 225, 246 →
        # 195, no rejections.  74 and 4623 carry all three period windows; **246
        # publishes no first-inning market** — its board is full-game and
        # first-five only, so a missing F1 row for Unibet PA is that book's
        # shelf, not a fetch defect.  The 74 and 246 captures are the committed
        # fixtures; 4623's was retired 2026-08-09 when the Illinois campaign
        # swapped ``an_thescore``'s single-licence store to its IL capture
        # (4601) — the measurement above stands as record, the bytes return
        # with the next PA-egress pass (docs/evidence/action-network.md
        # § "theScore Bet's Illinois id answers").
        #
        # Two things had to be true at once and neither was, which is why this
        # note used to be long.  The endpoint: the ids are on ``web/v2`` and the
        # nine v1 tenants asking for them got other books back, because that is
        # what v1 does with an id it does not know.  The clock: the 2026-08-07
        # runs hit a dead slate — every game ``complete`` or ``inprogress`` — so
        # even the rows that came back produced nothing after the pregame filter.
        # A capture taken then was an empty fixture however healthy the run
        # looked.
        #
        # The brands are settled too, and not by inference:
        # ``api.actionnetwork.com/web/v1/books`` is Action Network's own
        # catalogue and names 74 ``'Parx'``/``paparx``, 4623 ``'theScore Bet
        # PA'`` and 246 ``'UnibetPA'``.  So this table's betPARX and theScore Bet
        # entries are confirmed, and 246 is **Unibet Pennsylvania** — the
        # catalogue holds no book named Mohegan at all.  See ``src.coverage``,
        # where that distinction decides what may be claimed.
        "an_parx": {"PA": 74, "NJ": 1929},
        "an_unibet": {"PA": 246, "NJ": 247},
        "an_thescore": {"IL": 4601, "PA": 4623, "NJ": 4620},
    }
)

#: The republisher keys whose book is a state licence, in table order.
AN_BOOK_KEYS: tuple[str, ...] = tuple(_AN_BOOK_IDS)


def files_per_state_book_id(source_key: str) -> bool:
    """Is this **republisher** asked for a different book id per state?

    The single definition of "local" *among the republishers*, because more than
    one module needs it and two spellings of it would drift.  :mod:`src.coverage`
    uses it to decide what may corroborate a state licence; :mod:`src.redundancy`
    uses it to decide whether a feed may be described as failover for a state book.

    False for VegasInsider (``/odds/las-vegas/``, no state parameter), for VSiN's
    Vegas line tracker, and for an Action Network feed pinned to a fixed id such
    as Circa or Fliff — each publishes one number for the whole country.

    **It answers about republishers only, and False does not mean nationwide.**
    The table it reads is :data:`_AN_BOOK_IDS`, so ``fanduel``, ``betmgm`` and
    ``betrivers_kambi`` are all False while being the most state-scoped sources in
    the repository — a first-party retail route carries a per-state host, tenant
    or segment and is pinned by :data:`Jurisdiction.routes`, not by a book id.  The
    earlier name and docstring said False meant "publishes one number for the whole
    country", which was untrue of exactly those three, and :mod:`src.redundancy`
    already had to write ``primary in RETAIL_SOURCE_KEYS or is_local(primary)`` to
    work around it.  A caller asking "is this source state-scoped at all" wants
    that union, not this function.
    """
    return source_key in _AN_BOOK_IDS


def republishes_state_licence(state: str, source_key: str) -> bool:
    """Does this feed carry ``state``'s own licence for its book?

    Stricter than :func:`files_per_state_book_id`, and the distinction is load
    bearing: ``an_bally`` files per-state ids but holds only a New Jersey book,
    so it is state-scoped in general and carries no Pennsylvania price at all.
    """
    if not files_per_state_book_id(source_key):
        return False
    route = jurisdiction(state).republished.get(source_key)
    return route is not None and route.status is not RouteStatus.UNAVAILABLE


def _republished_for(state: str) -> Mapping[str, RepublishedRoute]:
    """Every Action Network republisher route for one state.

    ``fetch_book_ids`` names the state's **whole** set on every request rather
    than each source naming only itself.  On ``web/v2`` an id named alone does
    come back alone, so this is no longer needed to make a book present; it is
    kept because it makes every source in a state issue the identical URL, so
    the raw store sees one payload shape instead of fifteen.

    **Every route here is ``TEMPLATE`` or ``UNAVAILABLE``, never ``VALIDATED``.**
    This function knows one thing — whether the operator holds a licence in this
    state to republish — and that is what the status may say.  It used to stamp
    ``VALIDATED`` on everything it built, which is a status no code path here can
    earn: nothing is probed, no capture is consulted, and the word was assigned
    by the act of construction.  Pennsylvania's ``an_parx``, ``an_unibet`` and
    ``an_thescore`` read "validated" while returning zero rows.

    Whether a book was actually *observed* is a different question with a
    different owner: :mod:`src.coverage` answers it per book from the run's own
    quotes, and says ``SINGLE_SOURCE`` or ``MISSING`` when the answer is no.  One
    claim, one owner — a status that guesses at the other one can only disagree
    with it.
    """
    ids = {
        key: books[state] for key, books in _AN_BOOK_IDS.items() if state in books
    }
    fetch = ",".join(str(book_id) for book_id in sorted(ids.values()))
    routes: dict[str, RepublishedRoute] = {}
    for key in _AN_BOOK_IDS:
        book_id = ids.get(key)
        if book_id is None:
            routes[key] = RepublishedRoute(
                config={},
                status=RouteStatus.UNAVAILABLE,
            )
            continue
        routes[key] = RepublishedRoute(
            config={"book_id": book_id, "fetch_book_ids": fetch},
            status=RouteStatus.TEMPLATE,
        )
    return routes


IL = Jurisdiction(
    state="IL",
    label="Illinois",
    live_validated=True,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.il.sportsbook.fanduel.com",
            {"state": "il"},
            RouteStatus.VALIDATED,
            "IL",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiusil", "market": "US-IL", "lang": "en_US"},
            RouteStatus.VALIDATED,
            "IL",
        ),
        "betmgm": _route(
            "betmgm",
            "https://www.il.betmgm.com",
            {
                "base_url": "https://www.il.betmgm.com",
                "subdivision": "US-Illinois",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.VALIDATED,
            "IL",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.VALIDATED,
            "IL",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/il/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "IL",
            "exact Illinois route still requires matching-egress validation",
        ),
        "hardrock": _route(
            "hardrock",
            "https://api.hardrocksportsbook.com",
            {"segment": "il", "channel": "ILLINOIS_ONLINE"},
            RouteStatus.VALIDATED,
            "IL",
            "validated from matching Illinois egress on 2026-08-03",
        ),
        "thescore": _route(
            "thescore",
            "https://sportsbook.us-il.thescore.bet",
            {"api_base": "https://sportsbook.us-il.thescore.bet"},
            RouteStatus.TEMPLATE,
            "IL",
            "anonymous board read from matching Illinois egress on 2026-08-13; "
            "awaiting two clean runs for VALIDATED",
        ),
    },
    promos=PromoRoute(
        fanduel_region="IL",
        betrivers_url="https://il.betrivers.com/",
        betrivers_label="BetRivers IL",
    ),
    republished=_republished_for("IL"),
)


PA = Jurisdiction(
    state="PA",
    label="Pennsylvania",
    # Same standard IL met: routes promoted on live matching-egress evidence.
    # Not a claim about every route — betmgm and caesars are still TEMPLATE,
    # exactly as IL's caesars is.
    live_validated=True,
    routes={
        # The three VALIDATED routes below earned it the same way on 2026-08-08:
        # two parser-clean runs each from a detected-PA egress, runs 29/30/31
        # replaying PASS offline.  FanDuel runs 29/30 (1579 and 1967 quotes, 0
        # rejections), BetRivers runs 28/29 (3699 and 3698, 0 rejections),
        # DraftKings runs 29/31 (130 each, 0 rejections — both after the
        # trueOdds fix).  Run 28 as a whole replays FAIL, and only on
        # DraftKings' pre-fix rows — replay has no per-source filter, so
        # BetRivers' run-28 evidence is its own drift-free rows inside a run
        # whose verdict belongs to another book's since-fixed defect.
        "fanduel": _route(
            "fanduel",
            "https://sbapi.pa.sportsbook.fanduel.com",
            {"state": "pa"},
            RouteStatus.VALIDATED,
            "PA",
            "validated from matching Pennsylvania egress on 2026-08-08",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiuspa", "market": "US-PA", "lang": "en_US"},
            RouteStatus.VALIDATED,
            "PA",
            "validated from matching Pennsylvania egress on 2026-08-08",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.pa.betmgm.com",
            {
                "base_url": "https://sports.pa.betmgm.com",
                "subdivision": "US-Pennsylvania",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "PA",
            # 2026-08-08, PA egress: HTTP 400 "Access id not allowed for
            # application" — the id above is another state's.  The PA web app
            # has to supply its own before this route can be exercised.
            "PA access_id unknown; the configured id is refused with HTTP 400",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-PA-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-PA-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.VALIDATED,
            "PA",
            # Validated for what it answers, which is less than it is asked:
            # eventgroups 94682, 42133 and 40253 return "access denied" from
            # this egress, so MLB and tennis produce and WNBA/NFL/NHL do not.
            # scopes_refused keeps that failing loudly per run; a status can
            # say the route is real, not that the shelf is whole.
            "validated from matching Pennsylvania egress on 2026-08-08; "
            "3 of 5 eventgroups access-denied",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/pa/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "PA",
            # 2026-08-08, PA egress: still blocked at the edge ("request
            # blocked" marker) before any competition id is reached, so the PA
            # egress alone did not clear the CloudFront refusal seen earlier.
            "blocked at the CDN edge from PA egress on 2026-08-08",
        ),
        # PGCB's current authorized-online list has no Hard Rock book.  Retain
        # the adapter and secondary observations globally, but never invent a
        # PA segment and call it a PA route.
        "hardrock": RetailRoute(
            source_key="hardrock",
            host="api.hardrocksportsbook.com",
            config={"segment": "nj"},
            status=RouteStatus.UNAVAILABLE,
            routed_state="PA",
            detail="Hard Rock is not a Pennsylvania online sportsbook",
        ),
        # TEMPLATE in its exact sense — structurally known and unproven — and
        # honest here rather than optimistic: the host is DNS-confirmed against
        # a negative control (`sportsbook.us-pa` resolves, `us-zz` is NXDOMAIN,
        # 2026-08-13), the path grammar is Illinois' with one segment changed,
        # and theScore holds a PA licence (Action Network files it as id 4623).
        # What makes it safe to carry unproven is that the adapter compares the
        # edge's own `currentRegionCode` to this routed state on every fetch, so
        # from anywhere but Pennsylvania it refuses instead of pricing.  That is
        # the whole point of entering it now: the route works on arrival rather
        # than needing a session.
        "thescore": _route(
            "thescore",
            "https://sportsbook.us-pa.thescore.bet",
            {"api_base": "https://sportsbook.us-pa.thescore.bet"},
            RouteStatus.TEMPLATE,
            "PA",
            "PA edge confirmed by DNS with a negative control on 2026-08-13; "
            "never asked over HTTP",
        ),
    },
    promos=PromoRoute(
        fanduel_region="PA",
        betrivers_url="https://pa.betrivers.com/",
        betrivers_label="BetRivers PA",
    ),
    # Retain observations and health history, but they cannot become PA arb
    # legs because the underlying books are not licensed PA online operators.
    view_only_sources=frozenset(
        {
            "hardrock",
            "vi_hardrock",
            "an_hardrock",
            "vsin_circa",
            "an_bally",
        }
    ),
    republished=_republished_for("PA"),
)


NJ = Jurisdiction(
    state="NJ",
    label="New Jersey",
    live_validated=False,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.nj.sportsbook.fanduel.com",
            {"state": "nj"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "betrivers_kambi": _route(
            "betrivers_kambi",
            "https://eu-offering-api.kambicdn.com",
            {"operator": "rsiusnj", "market": "US-NJ", "lang": "en_US"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.nj.betmgm.com",
            {
                "base_url": "https://sports.nj.betmgm.com",
                "subdivision": "US-New Jersey",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-NJ-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-NJ-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/nj/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "NJ",
        ),
        "hardrock": _route(
            "hardrock",
            "https://api.hardrocksportsbook.com",
            {"segment": "nj", "channel": "NEW_JERSEY_ONLINE"},
            RouteStatus.TEMPLATE,
            "NJ",
        ),
    },
    promos=PromoRoute(
        fanduel_region="NJ",
        betrivers_url="https://nj.betrivers.com/",
        betrivers_label="BetRivers NJ",
    ),
    republished=_republished_for("NJ"),
)


DC = Jurisdiction(
    state="DC",
    label="District of Columbia",
    live_validated=False,
    routes={
        "fanduel": _route(
            "fanduel",
            "https://sbapi.dc.sportsbook.fanduel.com",
            {"state": "dc"},
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "betrivers_kambi": RetailRoute(
            source_key="betrivers_kambi",
            host="eu-offering-api.kambicdn.com",
            config={},
            status=RouteStatus.UNAVAILABLE,
            routed_state="DC",
            detail="BetRivers is not a District of Columbia online sportsbook",
        ),
        "betmgm": _route(
            "betmgm",
            "https://sports.dc.betmgm.com",
            {
                "base_url": "https://sports.dc.betmgm.com",
                "subdivision": "US-District of Columbia",
                "access_id": _MGM_ACCESS_ID,
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "draftkings": _route(
            "draftkings",
            "https://sportsbook-nash.draftkings.com",
            {
                "base_url": "https://sportsbook-nash.draftkings.com/sites/US-DC-SB/api/v5",
                "content_base_url": (
                    "https://sportsbook-nash.draftkings.com/sites/US-DC-SB/api/"
                    "sportscontent/controldata/league/leagueSubcategory/v1"
                ),
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "caesars": _route(
            "caesars",
            "https://api.americanwagering.com",
            {
                "base_url": (
                    "https://api.americanwagering.com/regions/us/locations/dc/"
                    "brands/czr/sb/v3"
                )
            },
            RouteStatus.TEMPLATE,
            "DC",
        ),
        "hardrock": RetailRoute(
            source_key="hardrock",
            host="api.hardrocksportsbook.com",
            config={},
            status=RouteStatus.UNAVAILABLE,
            routed_state="DC",
            detail="Hard Rock is not a District of Columbia online sportsbook",
        ),
    },
    promos=PromoRoute(
        fanduel_region="DC",
        betrivers_url=None,
        betrivers_label="BetRivers unavailable in DC",
    ),
    republished=_republished_for("DC"),
    view_only_sources=frozenset(
        {"betrivers_kambi", "hardrock", "vi_hardrock", "an_hardrock"}
    ),
)


JURISDICTIONS: Mapping[str, Jurisdiction] = MappingProxyType(
    {"IL": IL, "PA": PA, "NJ": NJ, "DC": DC}
)


def normalize_state(value: str) -> str:
    return value.strip().upper()


def jurisdiction(state: str) -> Jurisdiction:
    key = normalize_state(state)
    try:
        return JURISDICTIONS[key]
    except KeyError:
        raise KeyError(
            f"unknown jurisdiction {state!r}; configured: {', '.join(JURISDICTIONS)}"
        ) from None


def source_host(state: str, source_key: str) -> str | None:
    route = jurisdiction(state).routes.get(source_key)
    return route.host if route is not None else None


def route_warnings(state: str) -> tuple[str, ...]:
    configured = jurisdiction(state)
    return tuple(
        warning
        for route in configured.routes.values()
        if (warning := route.warning) is not None
    )


__all__ = [
    "AN_BOOK_KEYS",
    "IL",
    "DC",
    "JURISDICTIONS",
    "NJ",
    "PA",
    "Jurisdiction",
    "PromoRoute",
    "RepublishedRoute",
    "RetailRoute",
    "RouteStatus",
    "files_per_state_book_id",
    "jurisdiction",
    "normalize_state",
    "republishes_state_licence",
    "route_warnings",
    "source_host",
]
