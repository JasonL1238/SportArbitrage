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
from src.jurisdictions import jurisdiction, source_config
from src.settlement import SETTLEMENT
from src.sources.base import OddsSource
from src.sources.actionnetwork import ActionNetworkAdapter
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
VIEW_ONLY_SOURCES: frozenset[str] = frozenset({"an_open"}) | jurisdiction(
    settings.STATE
).view_only_sources


def view_only_for_state(state: str) -> frozenset[str]:
    return frozenset({"an_open"}) | jurisdiction(state).view_only_sources


def is_view_only(key: str) -> bool:
    return key in VIEW_ONLY_SOURCES


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

    def factory(self) -> Callable[..., OddsSource]:
        """A callable that builds this source, with its configuration bound.

        ``functools.partial`` rather than a lambda so that
        :func:`inspect.signature` still reports the adapter's own parameters —
        which is how :func:`accepts_leagues` can tell whether an instance can be
        narrowed to a league list without knowing anything about the class.
        """
        return partial(self.adapter, source_key=self.key, **self.config)

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
    SourceDescriptor(
        key="vsin_circa",
        adapter=VsinCircaAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    # Hard Rock / Fanatics / Fliff / Circa / Westgate(SuperBook) / Bally —
    # Action Network catalog ids.  From CA the scoreboard often omits prices
    # even when bookIds are named; first-party adapters are the real path once
    # ODDS_HTTP_PROXY is set.  Fliff is sweepstakes-style in many states —
    # treat legs with extra caution (still a sportsbook kind for schema).
    SourceDescriptor(
        key="an_hardrock",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 2724,
            "fetch_book_ids": "2724,79,2988,4727,69,68,123,75,71",
            "base_url": "https://api.actionnetwork.com/web/v2/scoreboard",
        },
    ),
    SourceDescriptor(
        key="an_fanatics",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={
            "book_id": 2988,
            "fetch_book_ids": "2988,2990,79,4727,69,68,123,75,71",
            "base_url": "https://api.actionnetwork.com/web/v2/scoreboard",
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
            "base_url": "https://api.actionnetwork.com/web/v2/scoreboard",
        },
    ),
    # Bet365 only appears when Caesars is named on the request; parse still
    # filters to book_id 79.
    SourceDescriptor(
        key="an_bet365",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 79, "fetch_book_ids": "123"},
    ),
    # Consensus / opening lines on the AN scoreboard — view-only; see
    # :data:`VIEW_ONLY_SOURCES`.  Not a book you can stake at.
    SourceDescriptor(
        key="an_open",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 30},
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
    # Offshore shelf: these book ids are absent from the default US payload and
    # only appear when named.  Asking for BetRivers/BetMGM ids here would strip
    # those books, so the expand set stays offshore-only.
    SourceDescriptor(
        key="an_bovada",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 21, "fetch_book_ids": "21,35,2495"},
    ),
    SourceDescriptor(
        key="an_onexbet",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 2495, "fetch_book_ids": "21,35,2495"},
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


def sources_for_state(state: str) -> tuple[SourceDescriptor, ...]:
    """Build the stable registry with active-state retail constructor values.

    Only first-party retail descriptors receive overrides.  Action Network,
    VegasInsider, exchanges, prediction markets, and offshore books retain the
    exact configuration they had before jurisdiction routing.
    """
    configured = jurisdiction(state)
    built: list[SourceDescriptor] = []
    for entry in _BASE_SOURCES:
        override = source_config(configured.state, entry.key)
        built.append(
            replace(entry, config={**entry.config, **override})
            if override
            else entry
        )
    return tuple(built)


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


_check_registry()


__all__ = [
    "BY_KEY",
    "SOURCES",
    "SLOW_SOURCES",
    "VIEW_ONLY_SOURCES",
    "SourceDescriptor",
    "SourceKind",
    "accepts_leagues",
    "descriptor",
    "is_view_only",
    "keys",
    "sources_for_state",
    "view_only_for_state",
]
