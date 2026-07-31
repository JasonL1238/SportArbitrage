"""What each venue does when the contest does not happen as scheduled.

Two prices on the same fixture are only a hedge if the two contracts *void
together*.  A cross-book arbitrage is built on the assumption that whatever
happens, both legs settle from the same event — and the case where that
assumption fails is not exotic: rain-outs, postponements and abandonments are
routine, and their handling is the one term of a bet that varies most between
venues.

:mod:`src.arb` already knows this for the derived markets.  A full-game total
needs nine innings for action and books disagree about a rain-shortened game, so
a spread or total position carries a void-risk note.  The moneyline was treated
as the safe case, on the reasoning that every US book gives it action after five
innings — true, and irrelevant to the failure that matters, which is the game
that is *never completed as scheduled*:

* A **sportsbook** voids a cancelled or long-postponed game and refunds the
  stake.  Both legs of a two-book position void, and the position is flat.
* **Kalshi** states, in the ``rules_secondary`` of all 82 captured moneyline
  markets (``KXMLBGAME``): *"If this game is postponed or delayed, the market
  will remain open and close after the rescheduled game has finished (within two
  days). If the game is cancelled or rescheduled to over two days away, the
  market will resolve to a fair price."*  So it settles from the make-up game the
  book refunded, or at a price the venue chooses.  Its spread and total series
  (``KXMLBSPREAD``, ``KXMLBTOTAL``, 272 markets) carry no such clause in the
  captures, so for those the regime is **inferred from the venue** rather than
  read from the payload.
* **Polymarket** states: *"If the game is postponed, this market will remain
  open until the game has been completed. If the game is canceled entirely, with
  no make-up game, this market will resolve 50-50."*  A contract bought at 0.75
  returns 0.50 while the book returns the whole stake.

Either way the hedge is gone and what remains is a one-sided bet for the whole
stake on that leg — on a position that was reported as risk-free with no note at
all, because the note was suppressed for the moneyline specifically.

This module does not try to model those rules; modelling them would mean
claiming to know how each venue exercises its discretion, which none of them
publish precisely enough to encode.  It records the one fact the arbitrage
engine needs — **whose settlement is compatible with whose** — and supplies the
sentence to print when it is not.  Sources in one regime void together; sources
in two regimes do not, and every position spanning two says so out loud.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence


class SettlementRegime(StrEnum):
    """How a venue disposes of a contest that is not completed as scheduled."""

    #: Voids and refunds a cancelled or long-postponed contest, and settles a
    #: rescheduled one as a new event.  Every sportsbook here.
    VOID_AND_REFUND = "void_and_refund"

    #: Keeps the market open across a postponement and settles from the make-up
    #: contest; on a full cancellation resolves at a venue-chosen "fair price"
    #: rather than refunding.  Kalshi.
    SETTLE_MAKE_UP_GAME = "settle_make_up_game"

    #: Keeps the market open across a postponement and, on a full cancellation,
    #: resolves every contract at 0.50 regardless of the price paid.  Polymarket.
    RESOLVE_FIFTY_FIFTY = "resolve_fifty_fifty"


#: How each venue is described in the note, in the venue's own terms.
_DESCRIPTIONS: dict[SettlementRegime, str] = {
    SettlementRegime.VOID_AND_REFUND: "voids and refunds a cancelled game",
    SettlementRegime.SETTLE_MAKE_UP_GAME: (
        "keeps the market open through a postponement and settles from the "
        "rescheduled game, or resolves a cancellation at a price it chooses"
    ),
    SettlementRegime.RESOLVE_FIFTY_FIFTY: (
        "keeps the market open through a postponement and resolves a cancelled "
        "game at 0.50 per contract whatever you paid"
    ),
}

#: Per-source regime, keyed by ``source_key``.
#:
#: An exchange is placed in the sportsbook's regime because its market voids with
#: the underlying event in the same way: Matchbook, Smarkets and SX Bet each void
#: a cancelled fixture and return the stake.
#:
#: Said plainly, because the strength of the evidence is not the same for every
#: row here: Polymarket's rule is quoted verbatim from its own market
#: descriptions and Kalshi's from its moneyline ``rules_secondary``, while the
#: three exchanges publish **no** settlement text in anything this pipeline
#: captures — grepping every stored envelope from them for "postpone", "cancel",
#: "abandon", "void" or "refund" returns nothing.  Their entries are taken from
#: each venue's published rules, not from bytes on disk, and are the reading that
#: refuses fewer positions.  SX Bet deserves the most suspicion of the three: it
#: settles from an on-chain oracle rather than from a bookmaker's rulebook, and
#: if that oracle resolves a cancelled fixture rather than voiding it, its true
#: regime is Kalshi's.  Worth re-reading against a live cancellation before this
#: line is trusted further than it is written.
#:
#: :mod:`src.sources.registry` enforces that every registered source appears
#: here, for the same reason it enforces a declared commission: the silent
#: default is the dangerous one.
SETTLEMENT: dict[str, SettlementRegime] = {
    "fanduel": SettlementRegime.VOID_AND_REFUND,
    "pinnacle": SettlementRegime.VOID_AND_REFUND,
    "betrivers_kambi": SettlementRegime.VOID_AND_REFUND,
    "leovegas_kambi": SettlementRegime.VOID_AND_REFUND,
    "bovada": SettlementRegime.VOID_AND_REFUND,
    "betmgm": SettlementRegime.VOID_AND_REFUND,
    "draftkings": SettlementRegime.VOID_AND_REFUND,
    "hardrock": SettlementRegime.VOID_AND_REFUND,
    "caesars": SettlementRegime.VOID_AND_REFUND,
    "cloudbet": SettlementRegime.VOID_AND_REFUND,
    "onexbet": SettlementRegime.VOID_AND_REFUND,
    "an_draftkings": SettlementRegime.VOID_AND_REFUND,
    "an_caesars": SettlementRegime.VOID_AND_REFUND,
    "an_bet365": SettlementRegime.VOID_AND_REFUND,
    "an_open": SettlementRegime.VOID_AND_REFUND,
    "an_fanduel": SettlementRegime.VOID_AND_REFUND,
    "an_betrivers": SettlementRegime.VOID_AND_REFUND,
    "an_betmgm": SettlementRegime.VOID_AND_REFUND,
    "an_bovada": SettlementRegime.VOID_AND_REFUND,
    "an_onexbet": SettlementRegime.VOID_AND_REFUND,
    "an_hardrock": SettlementRegime.VOID_AND_REFUND,
    "an_fanatics": SettlementRegime.VOID_AND_REFUND,
    "an_fliff": SettlementRegime.VOID_AND_REFUND,
    "an_circa": SettlementRegime.VOID_AND_REFUND,
    "an_superbook": SettlementRegime.VOID_AND_REFUND,
    "an_bally": SettlementRegime.VOID_AND_REFUND,
    "matchbook": SettlementRegime.VOID_AND_REFUND,
    "smarkets": SettlementRegime.VOID_AND_REFUND,
    "sxbet": SettlementRegime.VOID_AND_REFUND,
    "kalshi": SettlementRegime.SETTLE_MAKE_UP_GAME,
    "polymarket": SettlementRegime.RESOLVE_FIFTY_FIFTY,
}


@dataclass(frozen=True)
class SettlementMismatch:
    """Two or more regimes in one position, and the sentence describing it."""

    sources: tuple[str, ...]
    note: str


def regime_for(
    source_key: str, table: Mapping[str, SettlementRegime] | None = None
) -> SettlementRegime:
    """The regime for one source, defaulting to the sportsbook one.

    Defaulting *here* is right because this is also asked about historical rows
    whose source has since been removed; the guarantee that a live source cannot
    be added without a declared regime lives in the registry, where adding one is
    the thing that happens.
    """
    entries = table if table is not None else SETTLEMENT
    return entries.get(source_key, SettlementRegime.VOID_AND_REFUND)


def mismatch(
    source_keys: Sequence[str], table: Mapping[str, SettlementRegime] | None = None
) -> SettlementMismatch | None:
    """``None`` if every source settles a non-event the same way, else the note.

    Deliberately blunt: it reports that the legs may not void together, not what
    the net position would be.  Working that out needs to know which of several
    discretionary outcomes occurred, and a caveat that overstates its own
    precision is worse than one that says plainly what is unknown.
    """
    regimes = {key: regime_for(key, table) for key in source_keys}
    if len(set(regimes.values())) <= 1:
        return None
    parts = ", ".join(
        f"{key} {_DESCRIPTIONS[regime]}"
        for key, regime in sorted(regimes.items())
    )
    return SettlementMismatch(
        sources=tuple(sorted(regimes)),
        note=(
            "the legs do not void together if the game is postponed or cancelled: "
            f"{parts} — the hedge disappears and the remaining leg is a one-sided "
            "bet for its whole stake, so this is not risk-free on a game that is "
            "not played as scheduled"
        ),
    )
