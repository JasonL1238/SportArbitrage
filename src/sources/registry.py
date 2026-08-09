"""Which sources exist, how to build one, and what each of them is.

A source used to be a *module*: one adapter class, one hard-coded key, one set of
credentials-free defaults baked into the file.  That is fine for three books and
wrong for ten, because the platform sources are one class serving many books —
Kambi alone fronts BetRivers, LeoVegas, Unibet and a long tail of others from the
same CDN, differing only in an operator token.

So a source is an **instance** here, described by a small record: which adapter
class builds it, what configuration distinguishes it from its siblings, what kind
of venue it is, and what it charges.  Adding a book that shares an existing
adapter is one entry in :data:`SOURCES`; adding a genuinely new one is that plus
the adapter.

Three invariants are checked at import, because each of them is silent when
broken and expensive afterwards:

1. **Keys are unique.**  ``source`` is the first element of ``dedup_key``, so two
   descriptors sharing a key produce rows that collide on the storage layer's
   UNIQUE constraint and abort the whole run's insert.
2. **Every source declares a commission and a settlement regime.**  An
   exchange whose charge defaulted quietly to zero would report a stream of
   false positives — exchanges quote tighter than sportsbooks, so their legs are
   exactly the ones that drag a cross-book sum below 1.0.  A prediction market
   whose settlement defaulted quietly to the sportsbook's would report a
   position as risk-free that stops being a hedge the moment the game is rained
   out.
3. **Every adapter accepts ``source_key``.**  Without it the instance's identity
   would come from a module constant and two instances of one class would be
   indistinguishable in the data.

Accidental mirrors (two Kambi licences of one book) are still measured against a
live slate by :mod:`src.distinctness`.  Intentional Action Network failover
pairs are declared in :mod:`src.redundancy` — agreement there is expected, and
disagreement is the finding.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from inspect import signature
from typing import Any, Callable, Mapping, Sequence

from src import settings
from src.commission import COMMISSIONS
from src.jurisdictions import (
    AN_BOOK_KEYS,
    JURISDICTIONS,
    Jurisdiction,
    RouteStatus,
    jurisdiction,
)
from src.settlement import SETTLEMENT
from src.sources.base import OddsSource
from src.sources.actionnetwork import LEGACY_V1_BASE_URL, ActionNetworkAdapter
from src.sources.betrivers_kambi import BetRiversKambiAdapter
from src.sources.betmgm import BetMgmAdapter
from src.sources.bovada import BovadaAdapter
from src.sources.caesars import CaesarsAdapter
from src.sources.cloudbet import CloudbetAdapter
from src.sources.draftkings import DraftKingsAdapter
from src.sources.fanduel import FanDuelAdapter
from src.sources.hardrock import HardRockAdapter
from src.sources.kalshi import KalshiAdapter
from src.sources.matchbook import MatchbookAdapter
from src.sources.onexbet import OneXBetAdapter
from src.sources.pinnacle import PinnacleAdapter
from src.sources.polymarket import PolymarketAdapter
from src.sources.smarkets import SmarketsAdapter
from src.sources.sxbet import SxBetAdapter
from src.sources.vegasinsider import VegasInsiderAdapter
from src.sources.vsin import VsinCircaAdapter


#: Sources that take much longer than the rest of the pass, because their own
#: published rate limit forces it.
#:
#: Collected **last**, so their length does not push everybody after them out of
#: the window where two prices can be taken together.  Smarkets paces itself at
#: 3.1 s and makes ~155 requests, so it runs about eight minutes; sitting in the
#: middle of the order it pushed Kalshi and Polymarket — four and ten requests
#: each — eight minutes past the fast books, and **847 of their shared markets
#: became uncomparable** for no reason but ordering.
SLOW_SOURCES: frozenset[str] = frozenset({"smarkets"})

#: Collected for the odds board only — never a counterparty you can bet against.
#:
#: Action Network's ``Open`` column (``book_id`` 30) is opening / consensus
#: lines, not a sportsbook.  It stays on the page for line context, but must not
#: win best-price highlighting, enter an arbitrage leg, or collapse two real
#: books into one counterparty via distinctness (a consensus feed that agrees
#: with A and with B would otherwise union-find A with B).
RETAIL_SOURCE_KEYS: frozenset[str] = frozenset(
    {"fanduel", "betrivers_kambi", "betmgm", "draftkings", "caesars", "hardrock"}
)

# Republished comparison surfaces identify gaps and stale/misaligned prices, but
# they are not a place at which this application can place a wager.  Keeping all
# of them view-only also prevents a first-party book and its republished copy
# from becoming the two legs of a false arbitrage.
REPUBLISHED_SOURCE_KEYS: frozenset[str] = frozenset(
    {"vsin_circa"}
) | frozenset(
    entry
    for entry in (
        "an_draftkings", "an_caesars", "vi_draftkings", "vi_caesars",
        "vi_hardrock", "vi_fanatics", "vi_bet365", "an_hardrock",
        "an_fanatics", "an_fliff", "an_circa", "an_superbook", "an_bally",
        "an_bet365", "an_open", "an_fanduel", "an_betrivers", "an_betmgm",
        "an_bovada", "an_onexbet", "an_parx", "an_unibet", "an_thescore",
        "vi_betmgm", "vi_fanduel", "vi_betrivers",
    )
)

# Venues at which somebody sitting in the United States cannot actually place the
# wager the price implies.  A *separate* axis from view-only: a republished mirror
# is unstakeable because it is a copy of somebody else's board, while these are
# genuine first-party order books that will not take a US customer.
#
# Deliberately **not** folded into ``VIEW_ONLY_SOURCES``.  These carry the sharpest
# lines on the page — Pinnacle in particular is the reference every other book is
# measured against — so dropping them from the data would cost more than it saves.
# The dashboard exposes the distinction as a toggle instead, defaulting to the
# US-only view so no arbitrage is presented as takeable unless it is.
#
# Why each one is here:
#   pinnacle        No US licence; geoblocks US traffic and has excluded US
#                   customers for years.
#   leovegas_kambi  The registered tenant is the Kambi ``leo`` GB market
#                   (``market: "GB"``) — a UK licence, not a US book at all.
#   bovada          Offshore (Curacao).  Takes US signups but holds no US state
#                   licence and self-blocks NJ/NY/NV/DE/MD.  Grouped here because
#                   reachable is not licensed, and a leg placed there carries
#                   counterparty risk a licensed book does not.
#   cloudbet        Offshore crypto book; does not accept US customers.
#   onexbet         Offshore; not US licensed, blocks US customers.
#   matchbook       UK/Malta licensed exchange; no US access.
#   smarkets        UK licensed exchange; no US access.
#   sxbet           Offshore crypto exchange.
#   an_bovada       Action Network mirrors of two of the above.  Already
#   an_onexbet      view-only, but they have to disappear *with* their book, or
#                   the US-only view still shows its price as context.
#   polymarket      **Two venues share this brand and only one is US-executable.**
#                   ``src.sources.polymarket`` reads ``gamma-api.polymarket.com``,
#                   the offshore platform.  The US venue is a different legal
#                   entity — QCX LLC, doing business as Polymarket US, a
#                   CFTC-designated contract market Polymarket acquired in July
#                   2025 — with its own order book, its own fee schedule and its
#                   own settlement rules.  A price quoted by the offshore book is
#                   not one a US customer can take, and pricing a hedge off it
#                   reports a position as risk-free that cannot be entered.  The
#                   US venue belongs here as its own source key, not as a config
#                   variant of this one: ``COMMISSIONS`` and ``SETTLEMENT`` are
#                   keyed by source key, so sharing one would read a fee schedule
#                   and a rain-out rule off the wrong venue's bytes.
#
# Kalshi is deliberately absent: it operates its own CFTC-regulated exchange and
# is executable from the US.
US_UNAVAILABLE_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "pinnacle",
        "leovegas_kambi",
        "bovada",
        "cloudbet",
        "onexbet",
        "matchbook",
        "smarkets",
        "sxbet",
        "an_bovada",
        "an_onexbet",
        "polymarket",
    }
)

#: Venues reachable from every state in :data:`~src.jurisdictions.JURISDICTIONS`
#: without holding a per-state sportsbook licence — the second half of
#: :func:`takeable_from_state`.
#:
#: **Enumerated, not derived.**  This used to be "every base source that is not
#: retail, not republished and not US-unavailable", which admitted each newly
#: registered source to all four states the moment it appeared, without it being
#: named in any table.  A first-party ``thescore`` added outside
#: :data:`RETAIL_SOURCE_KEYS` would have been takeable in DC, where theScore Bet
#: is not listed at all, and :func:`src.coverage._check_direct_route` would then
#: accept it as Pennsylvania's direct route.  Silence is the wrong default for a
#: question whose wrong answer is a leg sized into a position.
#:
#: :func:`_check_registry` requires every base source to be classified by exactly
#: one of the four sets, so *adding* a source without deciding this now fails at
#: import instead of granting nationwide reach by omission.
#:
#: Kalshi qualifies on the merits: a CFTC-regulated exchange with no state
#: sportsbook licence to hold, which is why it is also absent from
#: :data:`US_UNAVAILABLE_SOURCE_KEYS`.  A first-party sportsbook never belongs
#: here — it holds licences state by state, so it goes in
#: :data:`RETAIL_SOURCE_KEYS` with real per-state routes, where a missing route
#: fails loudly.
NATIONWIDE_SOURCE_KEYS: frozenset[str] = frozenset({"kalshi"})

#: Keys **reserved** for venues whose APIs demand per-account credentials, and
#: whose adapters therefore ship before their registration.
#:
#: A third axis beside view-only and US-unavailable, answering a different
#: question again: not "can you bet against this price" or "can a US customer
#: reach the venue" but "does fetching require a secret".  ProphetX and Novig
#: are both CFTC-regulated peer-to-peer exchanges with no anonymous market-data
#: surface — access is granted per account, so their adapters
#: (:mod:`src.sources.prophetx`, :mod:`src.sources.novig`) read
#: ``ODDS_PROPHETX_*`` / ``ODDS_NOVIG_*`` from the environment at fetch time
#: and refuse with ``login_required`` when unset.
#:
#: **Deliberately not registered in** :data:`_BASE_SOURCES` — which is why this
#: set is exempt from the stray-key check in
#: :func:`_check_reachability_is_declared`'s buckets: per the operator's
#: decision these venues stay out of the registry until credentials are
#: supplied *and* a genuine capture exists, because ``tests/conftest.py``
#: demands a committed fixture per registered key and no sanctioned path to
#: one exists before then (a response body carrying a partner or account id
#: has no sanctioned path at all, and that would be the finding).
#: ``tests/test_prophetx_novig.py`` pins both directions: the keys stay out of
#: :func:`keys` today, and this set names them so registering later is a
#: *decision* with a complete checklist rather than a drift.  Registration
#: takes all of the following — the list is deliberately identical to the one
#: in ``docs/SOURCE_FEASIBILITY.md``, and a step missing from either copy is
#: a defect, because the suite enforces most of these and the report renders
#: the rest:
#:
#: 1. the descriptor in :data:`_BASE_SOURCES`, classified in the four
#:    reachability sets (the cover check refuses anything less);
#: 2. ``COMMISSIONS`` and ``SETTLEMENT`` entries (``_check_registry``
#:    refuses their absence; Novig's must encode pregame-zero — see the
#:    adapter docstring);
#: 3. a genuine capture committed under ``tests/fixtures/raw/``
#:    (``conftest`` demands one per registered key);
#: 4. distinctness measured against every registered source, and any
#:    intentional failover pair declared in ``src.redundancy``;
#: 5. a ``src.betlinks`` entry — ``tests/test_betlinks.py`` asserts every
#:    registered non-consensus key resolves to a link;
#: 6. a ``src.report.SOURCE_NOTES`` entry with ``kind`` set to
#:    ``exchange`` — without one the sources page renders the venue as an
#:    unknown *sportsbook*, which for these two is affirmatively wrong;
#: 7. a ``src.report.SKIP_NOTES`` entry for every skip reason the adapter can
#:    emit that no existing note already covers —
#:    ``tests/test_report.py::test_every_real_skip_reason_has_an_explanation``
#:    is parametrised off the registry and goes red on an unexplained reason,
#:    and the page renders one as a generic shrug.  Prefer an existing
#:    spelling over a new one: both these adapters were rewritten onto the
#:    established vocabulary rather than keeping the names they were born
#:    with;
#: 8. flipping the stays-unregistered pin in ``tests/test_prophetx_novig.py``;
#: 9. the full acceptance bar (healthy collect, replay PASS, counted in
#:    ``comparable_group_count``).
CREDENTIALED_SOURCE_KEYS: frozenset[str] = frozenset({"prophetx", "novig"})

VIEW_ONLY_SOURCES: frozenset[str] = REPUBLISHED_SOURCE_KEYS | jurisdiction(
    settings.STATE
).view_only_sources


def view_only_for_state(state: str) -> frozenset[str]:
    return REPUBLISHED_SOURCE_KEYS | jurisdiction(state).view_only_sources


def view_only_for_run(state: str) -> frozenset[str]:
    """Which sources are not a counterparty, for a run recorded under *state*.

    One resolution of a mapping that had grown four hand-written copies —
    ``collect_once``, ``_cmd_arb``, ``report._arb_payload`` and
    ``store.cross_book_event_counts`` — and the last of them did not do it at all.
    It read the module-level :data:`VIEW_ONLY_SOURCES`, which is frozen at import
    from ``settings.STATE``: 27 keys under IL and 28 under PA (``hardrock``, which
    Pennsylvania cannot stake).  So ``runs`` and ``health`` printed a sport as
    ``usable`` or ``NO OVERLAP`` for the same stored run depending on which state
    the reading process was configured for.

    ``GLOBAL`` is not a jurisdiction and has no per-state view-only list; there,
    every republished mirror is the whole answer.

    An unrecognised or empty state gets that same answer rather than the ambient
    :data:`VIEW_ONLY_SOURCES`, and the difference is the whole point of the
    function.  Empty is not a rare case: ``jurisdiction`` migrates with
    ``DEFAULT ''``, so **every row predating that column** reads as empty, and those
    are exactly the stored runs ``runs``, ``health`` and ``report`` are asked
    about.  Falling back to the ambient constant left the defect above alive on
    precisely those rows — ``hardrock`` is the one key that differs between the IL
    and PA sets, and it is a real source — so the answer stayed a function of the
    reader's ``ODDS_STATE``.  The mirrors are what is knowable without a
    jurisdiction, and being deterministic and slightly generous is better than being
    exact about a state nobody recorded.
    """
    normalized = (state or "").strip().upper()
    if normalized in JURISDICTIONS:
        return view_only_for_state(normalized)
    return REPUBLISHED_SOURCE_KEYS


def is_view_only(key: str) -> bool:
    return key in VIEW_ONLY_SOURCES


def is_us_unavailable(key: str) -> bool:
    return key in US_UNAVAILABLE_SOURCE_KEYS


class SourceKind(StrEnum):
    """What kind of counterparty this is, which decides how a price behaves.

    Not decoration.  A sportsbook posts a price it will take the other side of;
    an exchange shows you somebody else's order, with a size and a commission; a
    prediction market shows a contract price with a fee and a bid/ask spread.
    The arbitrage engine has to treat the three differently, and the reports have
    to say which is which.
    """

    SPORTSBOOK = "sportsbook"
    EXCHANGE = "exchange"
    PREDICTION_MARKET = "prediction_market"

    @property
    def has_stated_liquidity(self) -> bool:
        """Does this kind publish how much money is behind a price?

        A sportsbook's limit is usually unstated; an exchange's top of book is a
        real number of currency units, and a position larger than it is not
        available at that price however good the arithmetic looks.
        """
        return self in (SourceKind.EXCHANGE, SourceKind.PREDICTION_MARKET)


@dataclass(frozen=True)
class SourceDescriptor:
    """One registered book, and everything the collector needs to build it."""

    key: str
    """Stable identity.  Goes into every row's ``source`` and into ``dedup_key``."""

    adapter: type
    """The class that fetches and parses this venue's payloads."""

    kind: SourceKind
    config: Mapping[str, Any] = field(default_factory=dict)
    """What distinguishes this instance from its siblings on the same class — the
    Kambi operator token, a FanDuel state, an exchange's currency."""
    route_state: str | None = None
    """Exact retail state whose proxy must carry this live descriptor."""

    def factory(self) -> Callable[..., OddsSource]:
        """A callable that builds this source, with its configuration bound.

        ``functools.partial`` rather than a lambda so that
        :func:`inspect.signature` still reports the adapter's own parameters —
        which is how :func:`accepts_leagues` can tell whether an instance can be
        narrowed to a league list without knowing anything about the class.
        """
        routed = {"proxy_state": self.route_state} if self.route_state else {}
        return partial(self.adapter, source_key=self.key, **self.config, **routed)

    def build(self, *, leagues: Sequence[str] | None = None, timeout: float | None = None) -> OddsSource:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if leagues is not None:
            kwargs["leagues"] = tuple(leagues)
        return self.factory()(**kwargs)

    def replay_instance(self) -> OddsSource:
        """An instance suitable for parsing stored bytes and nothing else.

        Built with no league configuration on purpose: replay must reproduce what
        was stored from the *bytes*, so anything a parse reads off its instance
        is a way for replay to disagree with collection.  ``source`` in
        particular comes from the envelope — see
        :func:`src.sources._common.envelope_source`.
        """
        return self.factory()()


#: Every source the collector can run.
#:
#: Order is the order they are collected in, and it is deliberate rather than
#: alphabetical: the three books with the longest history come first so that a
#: partial run still produces the comparison set that has been verified most.
_BASE_SOURCES: tuple[SourceDescriptor, ...] = (
    SourceDescriptor(
        key="fanduel",
        adapter=FanDuelAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="pinnacle",
        adapter=PinnacleAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="betrivers_kambi",
        adapter=BetRiversKambiAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"operator": "rsiusil", "market": "US-IL", "lang": "en_US"},
    ),
    # The second Kambi tenant, and the reason the registry exists in this shape.
    #
    # It is here on evidence rather than on the assumption that a different
    # operator token means a different book.  Most of them do not: `rsiusnj`,
    # `rsiuspa` and the plain `kambi` reference tenant answered the captured
    # slate with byte-identical prices to `rsiusil` — 30 of 30 shared moneylines
    # equal — and registering one of those would let the engine report an
    # arbitrage between BetRivers and BetRivers.  `leo` prices the same slate
    # differently (Cleveland at 2.28 where BetRivers had 2.38), and
    # `tests/test_distinctness.py` holds that against captured payloads from
    # both, so the claim is checked rather than asserted.
    SourceDescriptor(
        key="leovegas_kambi",
        adapter=BetRiversKambiAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"operator": "leo", "market": "GB", "lang": "en_GB"},
    ),
    # ── exchanges ────────────────────────────────────────────────────────────
    #
    # A different contract from a sportsbook's posted price, and the reason
    # `src.commission` exists: these quote *tighter* than a book, so their legs
    # are precisely the ones that drag a cross-book sum below 1.0. Priced gross,
    # they would produce a stream of false positives concentrated on exactly the
    # sources this expansion adds.
    SourceDescriptor(
        key="matchbook",
        adapter=MatchbookAdapter,
        kind=SourceKind.EXCHANGE,
    ),
    SourceDescriptor(
        key="bovada",
        adapter=BovadaAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="betmgm",
        adapter=BetMgmAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    # First-party DraftKings / Hard Rock.  From CA both need a licensed-state
    # ODDS_HTTP_PROXY for live odds (DK is Akamai 403; Hard Rock's GraphQL
    # returns an empty events list).  Parsers are pinned on captured fixtures.
    SourceDescriptor(
        key="draftkings",
        adapter=DraftKingsAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="hardrock",
        adapter=HardRockAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="caesars",
        adapter=CaesarsAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="cloudbet",
        adapter=CloudbetAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    SourceDescriptor(
        key="onexbet",
        adapter=OneXBetAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    # ── Action Network multi-book scoreboard ─────────────────────────────────
    #
    # One public JSON feed carrying many books.  Keys that pair with a
    # first-party adapter (FanDuel, BetRivers, BetMGM, Bovada, 1xBet) are
    # intentional failover feeds — see :mod:`src.redundancy`.  DraftKings /
    # Caesars / Bet365 have no first-party adapter from this egress; AN is the
    # only path until a licensed-state proxy lands.  LeoVegas Ontario on AN is
    # not paired with ``leovegas_kambi`` (Kambi GB) — different licence.
    SourceDescriptor(
        key="an_draftkings",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 68},
    ),
    # Caesars (and Bet365) are omitted unless bookIds names them.
    SourceDescriptor(
        key="an_caesars",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 123, "fetch_book_ids": "123"},
    ),
    # Independent public comparison surface.  These are the third observation
    # path for DK/Caesars, not separate counterparties; redundancy.py prevents
    # any combination of first-party, AN and VI rows from arbing against itself.
    SourceDescriptor(
        key="vi_draftkings",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "draftkings"},
    ),
    SourceDescriptor(
        key="vi_caesars",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "caesars"},
    ),
    SourceDescriptor(
        key="vi_hardrock",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "hardrock"},
    ),
    SourceDescriptor(
        key="vi_fanatics",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "fanatics"},
    ),
    SourceDescriptor(
        key="vi_bet365",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "bet365"},
    ),
    # VegasInsider already parses these three columns — they were simply never
    # registered.
    #
    # **They close no coverage gap, and the earlier note here claiming they did was
    # wrong.**  :mod:`src.coverage` declares every ``vi_*`` feed
    # ``Corroboration.OTHER_LICENCE``, which that module never counts towards the
    # two and never price-compares, so BetMGM / FanDuel / BetRivers read
    # ``SINGLE_SOURCE`` with these registered exactly as they did without them.
    # What they actually buy is *context*: a named out-of-state number beside a
    # book whose state feed is thin or dark, plus drift evidence through
    # ``REDUNDANT_PAIRS``.  That is worth three fetches a run; being able to
    # satisfy rule (c) is not what it is worth, and writing that down as though it
    # were is how a Las Vegas column ends up counted.
    SourceDescriptor(
        key="vi_betmgm",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "betmgm"},
    ),
    SourceDescriptor(
        key="vi_fanduel",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "fanduel"},
    ),
    SourceDescriptor(
        key="vi_betrivers",
        adapter=VegasInsiderAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book": "betrivers"},
    ),
    SourceDescriptor(
        key="vsin_circa",
        adapter=VsinCircaAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    # Hard Rock / Fanatics / Fliff / Circa / Westgate(SuperBook) / Bally —
    # Action Network catalog ids.  First-party adapters are the real path once
    # ODDS_HTTP_PROXY is set.  Fliff is sweepstakes-style in many states —
    # treat legs with extra caution (still a sportsbook kind for schema).
    #
    # Fliff (2292), Circa (78) and SuperBook (14) are absent from **both**
    # endpoint versions: asked alone on 2026-08-08, no response on either
    # carried the id requested, and none of the three appears in any capture in
    # this repository.  (The reply carried the defaults and nothing else on that
    # thin board; stored v1 captures show v1 answering an unknown id with an
    # assortment of other books instead, so "the defaults" is what happened that
    # minute rather than what v1 does in general.)  So the v2
    # switch is not the fix for them and no request shape here is.
    #
    # They are not "failing loudly": having no fixture, they take the
    # session-scoped ``registered_raws`` down with them, and roughly 1600
    # unrelated assertions — the whole shared source contract included — error at
    # setup instead of running.  One of those unrun assertions is
    # ``test_the_adapter_produced_rows_at_all``, which is precisely the check
    # that would have caught the Pennsylvania feeds returning nothing.  The
    # blast radius is the problem, not the redness; see docs/testing.md.
    SourceDescriptor(
        key="an_hardrock",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 2724,
            "fetch_book_ids": "2724,79,2988,4727,69,68,123,75,71",
        },
    ),
    SourceDescriptor(
        key="an_fanatics",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 2988,
            "fetch_book_ids": "2988,2990,79,4727,69,68,123,75,71",
        },
    ),
    SourceDescriptor(
        key="an_fliff",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 2292, "fetch_book_ids": "2292"},
    ),
    SourceDescriptor(
        key="an_circa",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 78, "fetch_book_ids": "78"},
    ),
    SourceDescriptor(
        key="an_superbook",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 14, "fetch_book_ids": "14"},
    ),
    SourceDescriptor(
        key="an_bally",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 4693,
            "fetch_book_ids": "4693,79,2988,4727,69,68,123,75,71",
        },
    ),
    # On v2 an id named alone comes back alone (measured 2026-08-08 for 79, 123,
    # 74, 4623 and 246), so bet365 no longer has to ride along on a Caesars
    # request — and asking for 123 alone while parsing 79 would now return
    # nothing, because v2's bookIds *selects* rather than expands.  This base
    # config is overwritten per state from ``Jurisdiction.republished`` on every
    # path that opens a socket, so it was never live; it is corrected rather than
    # left as a trap for the first caller who builds the base registry.
    SourceDescriptor(
        key="an_bet365",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 79, "fetch_book_ids": "79"},
    ),
    # State-licensed books with no first-party adapter here.  The ids below are
    # New Jersey's, which is what a GLOBAL run republishes: Action Network files
    # no national book for a state-licensed operator, so a run with no
    # jurisdiction has to name *some* licence, and NJ is the one the rest of this
    # block already used.  A state run replaces the id from
    # :attr:`Jurisdiction.republished`, where betPARX and Unibet are
    # ``UNAVAILABLE`` in Illinois and are not built there at all.
    SourceDescriptor(
        key="an_parx",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 1929, "fetch_book_ids": "1929"},
    ),
    SourceDescriptor(
        key="an_unibet",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 247, "fetch_book_ids": "247"},
    ),
    SourceDescriptor(
        key="an_thescore",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 4620, "fetch_book_ids": "4620"},
    ),
    # Consensus / opening lines on the AN scoreboard — view-only; see
    # :data:`VIEW_ONLY_SOURCES`.  Not a book you can stake at.
    #
    # ``fetch_book_ids`` is set even though 30 is one of v2's defaults, because
    # naming an id is the *measured* request shape: every capture on disk that
    # produced rows named its ids, and after the v2 flip this was the only
    # descriptor issuing a bookIds-less v2 request — a shape nothing had ever
    # exercised.  What an unnamed v2 request returns is still unmeasured; this
    # stops relying on it rather than settles it.
    SourceDescriptor(
        key="an_open",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 30, "fetch_book_ids": "30"},
    ),
    SourceDescriptor(
        key="an_fanduel",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 69},
    ),
    SourceDescriptor(
        key="an_betrivers",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 71},
    ),
    SourceDescriptor(
        key="an_betmgm",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 75},
    ),
    # Offshore shelf.  These two are the **only** sources that may set
    # ``base_url``, and they must: the offshore books exist on v1 and not on v2.
    # Measured 2026-08-08, both versions asked ``21,35,2495`` in the same
    # minute — v1 answered with Bovada on NFL and with Bovada and 1xBet on
    # soccer, v2 answered with neither on any of the five leagues.  Removing
    # these overrides silently empties two working sources, which is why the
    # reason is recorded here rather than in a commit message.
    #
    # The expand set stays offshore-only.  These are now the only descriptors
    # still on v1, and v1 is where the old trap was measured: asking for
    # BetRivers/BetMGM ids (71, 75) there could *remove* those books from the
    # payload.  That does not reproduce on v2, but nothing here runs on v2.
    SourceDescriptor(
        key="an_bovada",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 21,
            "fetch_book_ids": "21,35,2495",
            "base_url": LEGACY_V1_BASE_URL,
        },
    ),
    SourceDescriptor(
        key="an_onexbet",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 2495,
            "fetch_book_ids": "21,35,2495",
            "base_url": LEGACY_V1_BASE_URL,
        },
    ),
    SourceDescriptor(
        key="sxbet",
        adapter=SxBetAdapter,
        kind=SourceKind.EXCHANGE,
    ),
    SourceDescriptor(
        key="smarkets",
        adapter=SmarketsAdapter,
        kind=SourceKind.EXCHANGE,
    ),
    # ── prediction markets ───────────────────────────────────────────────────
    SourceDescriptor(
        key="kalshi",
        adapter=KalshiAdapter,
        kind=SourceKind.PREDICTION_MARKET,
    ),
    SourceDescriptor(
        key="polymarket",
        adapter=PolymarketAdapter,
        kind=SourceKind.PREDICTION_MARKET,
    ),
)


def global_sources() -> tuple[SourceDescriptor, ...]:
    """Genuinely state-neutral venues and republished diagnostics, registry order.

    Excludes the state-licensed republishers as well as the first-party retail
    books.  Both are state-scoped for the same reason — the book they describe
    depends on the jurisdiction — and ``collect_batch`` fetches this list once and
    shares the instances across every run in the batch, which is exactly what a
    per-state book id cannot survive.  See
    :data:`STATE_LICENSED_REPUBLISHER_KEYS`.
    """
    excluded = RETAIL_SOURCE_KEYS | STATE_LICENSED_REPUBLISHER_KEYS
    return tuple(entry for entry in _BASE_SOURCES if entry.key not in excluded)


def state_sources_for_state(state: str) -> tuple[SourceDescriptor, ...]:
    """Exact-state first-party retail descriptors only.

    Unavailable books retain their stable registry entries globally, but are not
    instantiated for a state in which no licensed route exists.  Any route
    tagged for another state is a configuration error, never a fallback.
    """
    configured = jurisdiction(state)
    built: list[SourceDescriptor] = []
    for entry in _BASE_SOURCES:
        if entry.key not in RETAIL_SOURCE_KEYS:
            continue
        resolved = _state_retail_descriptor(entry, configured)
        if resolved is not None:
            built.append(resolved)
    return tuple(built)


def _state_retail_descriptor(
    entry: SourceDescriptor, configured: Jurisdiction
) -> SourceDescriptor | None:
    """One retail book's exact-state descriptor, or ``None`` if unlicensed here.

    Factored out so a caller asking about a single book is answered about *that*
    book.  ``descriptor_for_state`` used to build the whole retail set, so one
    mis-tagged route raised for whoever was asking: ``probe_sources.py`` turns the
    refusal into an ``UNLICENSED`` verdict and caches it under the key it was
    probing, which would have filed every retail book in the state as unlicensed
    on the strength of one bad entry.

    Raises ``RuntimeError`` for a route tagged for another state — never a
    fallback, which is the whole point of the exact-state rule.
    """
    route = configured.routes.get(entry.key)
    if route is None or route.status is RouteStatus.UNAVAILABLE:
        return None
    if route.routed_state != configured.state:
        raise RuntimeError(
            f"{entry.key} route is tagged {route.routed_state}, not "
            f"requested state {configured.state}; cross-state fallback refused"
        )
    return replace(
        entry,
        config={**entry.config, **route.config},
        route_state=configured.state,
    )


def state_licensed_keys(state: str) -> frozenset[str]:
    """Which source keys hold a first-party licence in *state*.

    One definition, because there were three and they disagreed.  ``collect_once``
    used the keys it had actually *built* — so a run narrowed to
    ``--source pinnacle`` built no retail book, the set came back empty, and the
    exact-state leg requirement was skipped rather than failing everything.
    :mod:`src.report` used ``JURISDICTIONS[state].routes`` directly, which still
    contains the books the operator holds **no** licence for: Hard Rock is an
    ``UNAVAILABLE`` PA route, and it counted as Pennsylvania's local leg.

    Derived from :func:`state_sources_for_state` so the answer cannot drift from
    what that function would agree to build.

    This is a question about **retail licences**, and it is *not* the same question
    as "can the operator bet here from that state" — see
    :func:`takeable_from_state`, which is what a locality filter must ask.
    """
    return frozenset(entry.key for entry in state_sources_for_state(state))


def takeable_from_state(state: str) -> frozenset[str]:
    """Every venue the operator can actually place a bet at from *state*.

    A retail licence is one way to be reachable, not the only one.  Kalshi is a
    federally regulated US venue with no per-state sportsbook licence to hold —
    this registry says so itself, in the note explaining why it is absent from
    :data:`US_UNAVAILABLE_SOURCE_KEYS` — so a filter built on
    :func:`state_licensed_keys` alone called it out-of-state everywhere.

    That inverted the rule it was written to serve: a Kalshi position on
    a Pennsylvania run was **withheld from the report, the dashboard and the SMS**,
    under a warning saying "every leg was a global or offshore venue", about a
    venue legal in Pennsylvania.  Rule (a) exists to stop an unreachable price
    being presented as the state's own; it was never a reason to hide a reachable
    one.

    Retail keys still have to earn their place per state — ``hardrock`` is
    US-executable in general and ``UNAVAILABLE`` in Pennsylvania, so it is takeable
    in IL and NJ and not here.  The other half is
    :data:`NATIONWIDE_SOURCE_KEYS`, which is **named rather than inferred**: a
    venue reaches every state by being written down as doing so, not by failing to
    match three exclusions.

    **The nationwide half is state-invariant, and that is a real limit of this
    function rather than a claim about the world.**  The union below never reads
    *state* on that side: the contribution is ``{kalshi}`` identically in IL, PA,
    NJ and DC, because no per-state table mentions it.  Kalshi's sports contracts
    are the one venue where per-state availability is genuinely contested, so if
    that ever has to be answered per state it needs a table in
    :mod:`src.jurisdictions` first — do not read a per-state answer out of this
    name until one exists.

    ``polymarket`` used to be the second name in that set and is not any more: the
    registered adapter reads the offshore platform, which is a different legal
    entity from the CFTC-designated Polymarket US.  Its legs are now labelled
    "not reachable from {ST}" everywhere rather than counted as a local hedge.
    """
    return state_licensed_keys(state) | NATIONWIDE_SOURCE_KEYS


#: Republishers whose book is a **state licence**, so which book they publish
#: depends on the jurisdiction being collected.
#:
#: These cannot ride along with the cached global fetch.  ``collect_batch``
#: fetches globals once and reuses those instances for the ``GLOBAL`` run and
#: every state run, which is right for Pinnacle and the exchanges and wrong here:
#: one shared ``an_caesars`` can only have asked for one book id, so a
#: Pennsylvania run reusing it stored ``bookIds=123`` — Caesars **NJ** — under a
#: PA key.  Measured on run 22 before this split, where every republished row in
#: the PA run came from the New Jersey licence.
#:
#: They are therefore state-scoped like the first-party retail books, and absent
#: from :func:`global_sources`: a state-licensed book in a run labelled ``GLOBAL``
#: is the same mislabelling in a different place.  Read off the jurisdiction
#: table so the two cannot drift.
STATE_LICENSED_REPUBLISHER_KEYS: frozenset[str] = frozenset(AN_BOOK_KEYS)

#: The unconfigured base descriptors by key.  Distinct from :data:`BY_KEY`, which
#: is resolved for ``settings.STATE`` — a lookup that needs to apply *another*
#: state's route has to start from the unresolved entry or it inherits this
#: process's state.
BY_BASE_KEY: dict[str, SourceDescriptor] = {entry.key: entry for entry in _BASE_SOURCES}


def republished_sources_for_state(state: str) -> tuple[SourceDescriptor, ...]:
    """State-licensed republishers, carrying this state's book ids.

    Built per state and never cached across runs, for the reason recorded on
    :data:`STATE_LICENSED_REPUBLISHER_KEYS`.  A book with no licence in this
    state is omitted rather than pointed at another state's id.

    ``route_state`` is deliberately **not** set: it becomes the adapter's
    ``proxy_state`` argument, which is a first-party retail concern.  Action
    Network answers the same host from any egress; only the requested id changes.
    """
    configured = jurisdiction(state)
    built: list[SourceDescriptor] = []
    for entry in _BASE_SOURCES:
        if entry.key not in STATE_LICENSED_REPUBLISHER_KEYS:
            continue
        route = configured.republished.get(entry.key)
        if route is None or route.status is RouteStatus.UNAVAILABLE:
            continue
        built.append(replace(entry, config={**entry.config, **route.config}))
    return tuple(built)


def sources_for_state(state: str) -> tuple[SourceDescriptor, ...]:
    """Build the complete stable registry with available state overrides.

    Unavailable descriptors remain registered for contract coverage and
    historical offline replay, but :func:`state_sources_for_state` and
    :func:`republished_sources_for_state` are the live builders and omit them.
    Exchanges, prediction markets, and offshore books are genuinely
    state-neutral and pass through untouched.
    """
    configured = jurisdiction(state)
    state_entries = {entry.key: entry for entry in state_sources_for_state(configured.state)}
    built: list[SourceDescriptor] = []
    for entry in _BASE_SOURCES:
        if entry.key in RETAIL_SOURCE_KEYS:
            if entry.key in state_entries:
                built.append(state_entries[entry.key])
            else:
                built.append(entry)
            continue
        route = configured.republished.get(entry.key)
        if route is not None and route.status is not RouteStatus.UNAVAILABLE:
            built.append(replace(entry, config={**entry.config, **route.config}))
            continue
        built.append(entry)
    return tuple(built)


def replay_descriptor_for_state(state: str, key: str) -> SourceDescriptor:
    """Registry-order lookup, **including** the replay fallbacks.

    Backed by :func:`sources_for_state`, so a retail book with no route in this
    state answers with the base descriptor rather than raising.  That is right
    for replay and for anything enumerating the stable registry, and wrong for
    anything that is about to send a request — see :func:`descriptor_for_state`.

    The name carries the warning, because a docstring could not.  This pair used
    to be ``descriptor_for_state`` (this one) and ``live_descriptor_for_state``
    (the strict one), which gave the obvious name to the lookup that silently
    answers with *another state's* configuration, and left the safe one to be
    reached for on purpose.  Two fetch paths had already reached for the wrong one.
    ``replay`` is the only thing a fallback like that is honestly for.
    """
    entries = {entry.key: entry for entry in sources_for_state(state)}
    try:
        return entries[key]
    except KeyError:
        raise KeyError(
            f"source {key!r} is unavailable for {jurisdiction(state).state}"
        ) from None


def descriptor_for_state(state: str, key: str) -> SourceDescriptor:
    """The descriptor to *fetch* with, or a refusal — never a fallback.

    :func:`sources_for_state` keeps every key registered so replay and coverage
    can enumerate the stable set, and fills the gaps with base descriptors —
    which for an unlicensed state means another state's configuration:

        sources_for_state("DC")["betrivers_kambi"] → operator rsiusil, US-IL
        sources_for_state("PA")["hardrock"]        → {} → DEFAULT_SEGMENT "nj"
        sources_for_state("IL")["an_parx"]         → book_id 1929 (New Jersey)

    That is right for replay — :func:`replay_descriptor_for_state`, which is where
    that behaviour now says its own name — and wrong for a caller about to open a
    socket, and ``probe_sources.py`` and ``recon_sources.py`` were both reaching
    for the lax lookup while doing exactly that.  **Neither is known to have
    produced a false
    validation**: ``state_candidates`` skips ``UNAVAILABLE`` routes before it
    builds a candidate, and ``recon_sources`` calls ``profile()`` first, which
    raises for an unlicensed book.  So this is a second lock on a door whose
    first lock currently holds, rather than a fix for an observed incident.

    It is worth having as a *lookup* rather than as care at each call site
    because of what the failure would cost if a guard upstream were relaxed: a
    probe that validates another state's route does not merely fail to help, it
    writes ``ProbeStatus.OK`` under this state's key and manufactures the
    evidence the jurisdiction rules are supposed to rest on.  A refusal is
    cheap; a false ``ok`` row is believed for as long as it is cached.
    """
    configured = jurisdiction(state)
    if key in RETAIL_SOURCE_KEYS:
        # Resolved for **this book alone**.  Building the whole retail set here
        # meant one mis-tagged route in the state answered for every book:
        # ``probe_sources.py`` turns the refusal into an ``UNLICENSED`` verdict for
        # whichever candidate it happened to be probing and caches
        # ``ProbeStatus.UNLICENSED`` under *that* key, so a single bad entry would
        # have filed every retail book in the state as unlicensed.
        route = configured.routes.get(key)
        resolved = _state_retail_descriptor(BY_BASE_KEY[key], configured)
        if resolved is None:
            reason = (
                route.detail or f"no licensed route is available in {configured.state}"
                if route is not None
                else f"{configured.state} declares no route for this book"
            )
            raise KeyError(
                f"{key!r} has no live {configured.state} route: {reason}. Refusing "
                "to fall back to another state's configuration — a probe that "
                "validates the wrong licence manufactures false evidence"
            )
        return resolved
    if key in STATE_LICENSED_REPUBLISHER_KEYS:
        built = {entry.key: entry for entry in republished_sources_for_state(configured.state)}
        if key not in built:
            raise KeyError(
                f"{key!r} republishes no {configured.state} licence, so there is "
                "no book id to ask for. Refusing to fall back to another state's id"
            )
        return built[key]
    return replay_descriptor_for_state(configured.state, key)


# Active-state compatibility for existing callers.  Processes that need to
# inspect another state without re-importing use ``sources_for_state``.
SOURCES: tuple[SourceDescriptor, ...] = sources_for_state(settings.STATE)
BY_KEY: dict[str, SourceDescriptor] = {entry.key: entry for entry in SOURCES}


def keys() -> tuple[str, ...]:
    return tuple(entry.key for entry in SOURCES)


def descriptor(key: str) -> SourceDescriptor:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown source {key!r}; registered: {sorted(BY_KEY)}"
        ) from None


def accepts_leagues(factory: Callable[..., Any]) -> bool:
    """Can this source be narrowed to a league list?

    Works on the ``partial`` a descriptor hands back, which is the reason
    :meth:`SourceDescriptor.factory` uses one.
    """
    try:
        return "leagues" in signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False


def _check_registry() -> None:
    """Refuse to import a registry that would fail silently at runtime."""
    seen: set[str] = set()
    for entry in SOURCES:
        if entry.key in seen:
            raise RuntimeError(
                f"source key {entry.key!r} is registered twice; `source` is the first "
                "element of dedup_key, so two sources sharing one would collide on the "
                "storage layer's UNIQUE constraint and abort the run's insert"
            )
        seen.add(entry.key)
        if entry.key not in COMMISSIONS:
            raise RuntimeError(
                f"source {entry.key!r} has no entry in src.commission.COMMISSIONS. "
                "Declare one — NO_COMMISSION for a sportsbook, whose margin is already "
                "in the price — because a venue that charges and is assumed not to "
                "produces false arbitrage rather than merely inaccurate margins"
            )
        if entry.key not in SETTLEMENT:
            raise RuntimeError(
                f"source {entry.key!r} has no entry in src.settlement.SETTLEMENT. "
                "Declare one — VOID_AND_REFUND for a book or an exchange, whose "
                "market voids with the fixture — because a venue that settles a "
                "cancelled game differently and is assumed not to reports a "
                "one-sided bet as a hedge"
            )
        parameters = signature(entry.adapter).parameters
        if "source_key" not in parameters:
            raise RuntimeError(
                f"{entry.adapter.__name__} does not accept source_key, so two instances "
                "of it could not be told apart in the data"
            )
        if entry.config:
            unknown = sorted(set(entry.config) - set(parameters))
            if unknown:
                raise RuntimeError(
                    f"source {entry.key!r} configures {unknown}, which "
                    f"{entry.adapter.__name__} does not accept"
                )
    _check_reachability_is_declared()


def _check_reachability_is_declared() -> None:
    """Every base source says how it is reachable, or the import fails.

    :func:`takeable_from_state` answers "can the operator place this bet from
    here", and its nationwide half used to be whatever was left after three
    exclusions.  Left-over is not a decision: a source registered without a
    thought about licensing became takeable in all four states, and nothing named
    it anywhere for a reviewer to check.  Coverage grades a book ``DIRECT`` off
    such a key, so the failure surfaces as a state's board looking complete.

    So the four sets **cover** :data:`_BASE_SOURCES` — not partition it, because
    one overlap is real: a republished mirror of an offshore book is both
    view-only and unstakeable (``an_bovada``, ``an_onexbet`` today, pinned in
    ``tests/test_coverage.py``).  Every other overlap is a contradiction and is
    refused below: retail means "you can bet here, per state licence", which no
    other classification can simultaneously be true of, and nationwide means "no
    per-state licence needed", which none of the rest can.  The first cut
    checked only the nationwide conflicts; adding ``hardrock`` to
    US_UNAVAILABLE passed while ``takeable_from_state("IL")`` still served it —
    the exact self-contradiction the check's own message describes, arriving
    through an overlap it never looked at.

    ``_BASE_SOURCES`` and not :data:`SOURCES`, because the state-scoped
    republisher variants are generated from the base list and inherit its
    classification.
    """
    buckets = {
        "RETAIL_SOURCE_KEYS": RETAIL_SOURCE_KEYS,
        "REPUBLISHED_SOURCE_KEYS": REPUBLISHED_SOURCE_KEYS,
        "US_UNAVAILABLE_SOURCE_KEYS": US_UNAVAILABLE_SOURCE_KEYS,
        "NATIONWIDE_SOURCE_KEYS": NATIONWIDE_SOURCE_KEYS,
    }
    base_keys = {entry.key for entry in _BASE_SOURCES}

    unclassified = sorted(base_keys - set().union(*buckets.values()))
    if unclassified:
        raise RuntimeError(
            f"source(s) {unclassified} are in no reachability set. Add each to "
            "RETAIL_SOURCE_KEYS (a state-licensed book, with per-state routes in "
            "src.jurisdictions), REPUBLISHED_SOURCE_KEYS (somebody else's board), "
            "US_UNAVAILABLE_SOURCE_KEYS (no US access), or NATIONWIDE_SOURCE_KEYS "
            "(reachable from every state without a state licence). Leaving one out "
            "used to mean nationwide reach by omission, which is how an "
            "unreachable venue becomes a state's 'direct' route"
        )

    for name, keys in buckets.items():
        stray = sorted(keys - base_keys)
        if stray:
            raise RuntimeError(
                f"{name} names {stray}, which no source registers; a set that can "
                "drift from the registry stops being a statement about it"
            )

    nationwide_conflict = sorted(
        NATIONWIDE_SOURCE_KEYS
        & (RETAIL_SOURCE_KEYS | REPUBLISHED_SOURCE_KEYS | US_UNAVAILABLE_SOURCE_KEYS)
    )
    if nationwide_conflict:
        raise RuntimeError(
            f"{nationwide_conflict} are declared reachable from every state and "
            "also state-licensed, view-only or US-unavailable. Nationwide reach "
            "means no per-state licence is needed; the other three each mean the "
            "opposite, so takeable_from_state would contradict itself"
        )

    retail_conflict = sorted(
        RETAIL_SOURCE_KEYS & (REPUBLISHED_SOURCE_KEYS | US_UNAVAILABLE_SOURCE_KEYS)
    )
    if retail_conflict:
        raise RuntimeError(
            f"{retail_conflict} are declared retail and also view-only or "
            "US-unavailable. Retail means a first-party book the operator can "
            "bet at, per state routes; a copy of somebody else's board or a "
            "venue no US customer can reach cannot be that. With both set, "
            "takeable_from_state would serve the key while is_us_unavailable "
            "or view-only filtering disowned it, and which one a surface "
            "believed would depend on which it asked first"
        )


_check_registry()


__all__ = [
    # Public by use, so public by export.  Three names in this list were reachable
    # from four modules and two test files while being absent from it, which makes
    # ``__all__`` a record of what somebody remembered rather than of the surface.
    "BY_BASE_KEY",
    "BY_KEY",
    "SOURCES",
    "SLOW_SOURCES",
    "CREDENTIALED_SOURCE_KEYS",
    "NATIONWIDE_SOURCE_KEYS",
    "REPUBLISHED_SOURCE_KEYS",
    "RETAIL_SOURCE_KEYS",
    "STATE_LICENSED_REPUBLISHER_KEYS",
    "US_UNAVAILABLE_SOURCE_KEYS",
    "VIEW_ONLY_SOURCES",
    "SourceDescriptor",
    "SourceKind",
    "accepts_leagues",
    "descriptor",
    "descriptor_for_state",
    "global_sources",
    "is_us_unavailable",
    "is_view_only",
    "keys",
    "replay_descriptor_for_state",
    "republished_sources_for_state",
    "sources_for_state",
    "state_licensed_keys",
    "takeable_from_state",
    "state_sources_for_state",
    "view_only_for_run",
    "view_only_for_state",
]
