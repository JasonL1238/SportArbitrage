"""Intentional dual feeds for the same venue.

:mod:`src.distinctness` treats two sources that agree on prices as a
registration error — correct for accidental Kambi licence mirrors.  It is the
wrong reaction for Action Network secondaries kept on purpose: a blocked
first-party edge still needs odds, and when both feeds are up their disagreement
is the signal worth flagging, not their agreement.

Pairs listed here:

* never raise ``sources_are_one_counterparty`` when they mirror
* cannot be two legs of one arb position (enforced non-transitively in
  :func:`src.arb._independent_source_count` — they are *not* filed into the
  measured counterparty union-find, which would collapse unrelated books)
* raise a warning when both price a thick overlapping sample and disagree

This module deliberately imports nothing from :mod:`src.arb` or
:mod:`src.validation` at load time — :mod:`src.arb` needs the pair table on
its hot path, and :mod:`src.validation` already imports :mod:`src.arb`.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from src.schema import Quote
    from src.validation import ValidationReport

#: ``(primary, secondary)`` — primary is the first-party adapter; secondary is
#: the Action Network republisher.  Order is documentation, not load-bearing:
#: lookup is symmetric.
#:
#: LeoVegas is omitted: AN ``book_id`` 1242 is LeoVegas Ontario, while
#: ``leovegas_kambi`` is the Kambi ``leo`` / GB tenant — pairing them as
#: failover would constantly disagree and stake the wrong licence.
REDUNDANT_PAIRS: tuple[tuple[str, str], ...] = (
    ("fanduel", "an_fanduel"),
    ("betrivers_kambi", "an_betrivers"),
    ("betmgm", "an_betmgm"),
    ("bovada", "an_bovada"),
    ("onexbet", "an_onexbet"),
    ("draftkings", "an_draftkings"),
    ("hardrock", "an_hardrock"),
    ("caesars", "an_caesars"),
)

_PAIR_LOOKUP: dict[frozenset[str], tuple[str, str]] = {
    frozenset(pair): pair for pair in REDUNDANT_PAIRS
}


def is_redundant_pair(source_a: str, source_b: str) -> bool:
    return frozenset({source_a, source_b}) in _PAIR_LOOKUP


def check_redundancy(
    quotes: Sequence[Quote], report: ValidationReport
) -> None:
    """Flag price drift between intentional dual feeds on overlapping leagues.

    Offline warnings require a shared league: Action Network often omits a book
    on MLB while the first-party adapter is full, and that is thin upstream
    coverage rather than a dead failover path.
    """
    from src.distinctness import (
        MIRROR_AGREEMENT_RATE,
        MIN_SHARED_SELECTIONS,
        Verdict,
        compare_sources,
    )
    from src.validation import Severity

    by_source_league: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for quote in quotes:
        by_source_league[quote.source].add(quote.league)
        counts[quote.source] += 1

    for primary, secondary in REDUNDANT_PAIRS:
        primary_leagues = by_source_league.get(primary, set())
        secondary_leagues = by_source_league.get(secondary, set())
        shared = primary_leagues & secondary_leagues
        primary_n = counts.get(primary, 0)
        secondary_n = counts.get(secondary, 0)

        if primary_n > 0 and secondary_n == 0:
            continue
        if secondary_n > 0 and primary_n == 0:
            report.add(
                Severity.WARNING,
                "primary_source_offline",
                f"{primary} produced no rows while {secondary} priced the slate "
                f"({secondary_n} quotes). Using the Action Network republisher as "
                f"failover for {primary}",
                source=primary,
            )
            continue
        if not shared:
            continue
        agreement = compare_sources(quotes, primary, secondary)
        if agreement.verdict is Verdict.UNDECIDED:
            continue
        if (
            agreement.verdict is Verdict.DISTINCT
            and agreement.compared >= MIN_SHARED_SELECTIONS
            and agreement.rate < MIRROR_AGREEMENT_RATE
        ):
            report.add(
                Severity.WARNING,
                "redundant_sources_disagree",
                f"{agreement.summary()}. These are an intentional failover pair, "
                "so disagreement is a data-quality signal rather than proof they "
                "are two books — check for stale Action Network prices or a "
                "parser/jurisdiction mismatch before trusting either leg",
                source=secondary,
            )


__all__ = [
    "REDUNDANT_PAIRS",
    "check_redundancy",
    "is_redundant_pair",
]
