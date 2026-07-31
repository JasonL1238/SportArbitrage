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

What is deliberately *not* here: whether a source is a mirror of another. That is
a fact about prices, not about configuration, and it is measured against a live
slate by :mod:`src.distinctness` rather than asserted in a table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from inspect import signature
from typing import Any, Callable, Mapping, Sequence

from src.commission import COMMISSIONS
from src.settlement import SETTLEMENT
from src.sources.base import OddsSource
from src.sources.actionnetwork import ActionNetworkAdapter
from src.sources.betrivers_kambi import BetRiversKambiAdapter
from src.sources.betmgm import BetMgmAdapter
from src.sources.bovada import BovadaAdapter
from src.sources.cloudbet import CloudbetAdapter
from src.sources.fanduel import FanDuelAdapter
from src.sources.kalshi import KalshiAdapter
from src.sources.matchbook import MatchbookAdapter
from src.sources.onexbet import OneXBetAdapter
from src.sources.pinnacle import PinnacleAdapter
from src.sources.polymarket import PolymarketAdapter
from src.sources.smarkets import SmarketsAdapter
from src.sources.sxbet import SxBetAdapter
from src.sources.unibet_au import UnibetAuAdapter


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
SOURCES: tuple[SourceDescriptor, ...] = (
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
    SourceDescriptor(
        key="unibet_au",
        adapter=UnibetAuAdapter,
        kind=SourceKind.SPORTSBOOK,
    ),
    # ── Action Network multi-book scoreboard ─────────────────────────────────
    #
    # One public JSON feed carrying many books.  Redundant keys (FanDuel /
    # BetRivers / BetMGM) are intentional secondary feeds for when a book's own
    # edge is unreachable; distinctness still has to clear them against the
    # primary adapter on a live slate.
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
    # Bet365 only appears when Caesars is named on the request; parse still
    # filters to book_id 79.
    SourceDescriptor(
        key="an_bet365",
        adapter=ActionNetworkAdapter,
        kind=SourceKind.SPORTSBOOK,
        config={"book_id": 79, "fetch_book_ids": "123"},
    ),
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
    "SourceDescriptor",
    "SourceKind",
    "accepts_leagues",
    "descriptor",
    "keys",
]
