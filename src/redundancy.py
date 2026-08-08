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
from typing import TYPE_CHECKING, Collection, Sequence

from src.jurisdictions import files_per_state_book_id, republishes_state_licence

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
    ("draftkings", "vi_draftkings"),
    ("an_draftkings", "vi_draftkings"),
    ("hardrock", "an_hardrock"),
    ("hardrock", "vi_hardrock"),
    ("an_hardrock", "vi_hardrock"),
    ("caesars", "an_caesars"),
    ("caesars", "vi_caesars"),
    ("an_caesars", "vi_caesars"),
    ("an_fanatics", "vi_fanatics"),
    ("an_bet365", "vi_bet365"),
    ("an_circa", "vsin_circa"),
    # Newly registered VegasInsider columns for books that already have an
    # Action Network feed and, for three of them, a first-party adapter.  Every
    # combination has to be declared or ``src.coverage`` gains a second
    # observation path while :mod:`src.arb` gains a fake counterparty: BetMGM
    # priced by ``betmgm``, ``an_betmgm`` and ``vi_betmgm`` is one book, and two
    # of those three as opposite legs is the false arb this table exists to stop.
    ("betmgm", "vi_betmgm"),
    ("an_betmgm", "vi_betmgm"),
    ("fanduel", "vi_fanduel"),
    ("an_fanduel", "vi_fanduel"),
    ("betrivers_kambi", "vi_betrivers"),
    ("an_betrivers", "vi_betrivers"),
)

_PAIR_LOOKUP: dict[frozenset[str], tuple[str, str]] = {
    frozenset(pair): pair for pair in REDUNDANT_PAIRS
}


def is_redundant_pair(source_a: str, source_b: str) -> bool:
    return frozenset({source_a, source_b}) in _PAIR_LOOKUP


def check_redundancy(
    quotes: Sequence[Quote],
    report: ValidationReport,
    *,
    state: str | None = None,
    configured: Collection[str] | None = None,
) -> None:
    """Flag price drift between intentional dual feeds on overlapping leagues.

    Offline warnings require a shared league: Action Network often omits a book
    on MLB while the first-party adapter is full, and that is thin upstream
    coverage rather than a dead failover path.

    *configured* names the sources this run actually asked for.  A pair whose
    primary was never collected is not an outage, and saying so was a live false
    positive rather than a hypothetical one: ``global_sources()`` deliberately
    excludes the state-licensed republishers, so a ``GLOBAL`` run reported
    fourteen dual feeds "offline" — including ``an_fanduel``, which that run is
    designed not to collect — every one of them on a completely healthy pass.

    *state* selects the stronger locality predicate.  Without it this can only
    ask whether a feed files per-state book ids *somewhere*; with it, whether it
    files one *here*, which is the question the failover claim actually turns on.
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

    # Imported here rather than at module scope for the reason in the module
    # docstring: this file sits on :mod:`src.arb`'s hot path, and the registry
    # pulls in every adapter.
    from src.sources.registry import RETAIL_SOURCE_KEYS

    asked = None if configured is None else frozenset(configured)
    licence = f"{state}'s" if state else "this state's"

    def is_local(source_key: str) -> bool:
        """Does this feed carry the collected state's licence?

        Falls back to the weaker "files per-state ids anywhere" question only
        when no state was supplied, because that is the strongest claim the data
        supports there.
        """
        if state is None:
            return files_per_state_book_id(source_key)
        return republishes_state_licence(state, source_key)

    # Whether an offline book has usable failover is a question about the **book**,
    # not about one pair, and it has to be answered once.  Six *primaries* here have
    # two mirrors each — eight books carry both an ``an_*`` and a ``vi_*`` feed, but
    # Fanatics and bet365 have no first-party adapter, so their two feeds are a
    # single pair rather than two pairs sharing a primary.  For the other six the
    # per-pair loop answered the question twice: with
    # the first-party route dark and both republishers up — the geo-blocked-retail
    # case this branch exists for — the report carried "Using the registered
    # republisher as failover for draftkings" and "…draftkings is unobserved"
    # adjacent, and the second sentence was false in exactly the case that
    # produced it.
    secondaries: dict[str, list[str]] = defaultdict(list)
    for primary, secondary in REDUNDANT_PAIRS:
        secondaries[primary].append(secondary)

    for primary, mirrors in secondaries.items():
        # A pair whose primary was not collected is out of scope, not offline.
        if asked is not None and primary not in asked:
            continue
        if counts.get(primary, 0) > 0:
            continue
        live = [mirror for mirror in mirrors if counts.get(mirror, 0) > 0]
        if not live:
            continue
        # A feed that publishes one number for the whole country is not failover
        # for a **state-licensed** book, and must not be described as one: the
        # message read "Using the registered republisher as failover for betmgm"
        # whichever republisher it was, so on a state run whose first-party route
        # was geo-blocked the report asserted in writing that a Las Vegas column
        # was standing in for the state licence — the substitution the coverage
        # rule exists to refuse, from the module a reader trusts to explain the gap.
        #
        # Scoped to a retail primary, because demanding a state licence of the
        # others is a category error rather than a finding.  ``bovada`` and
        # ``an_bovada`` are two views of an offshore book that holds no US state
        # licence anywhere, and ``an_circa`` / ``vsin_circa`` are two views of one
        # Las Vegas board; for those, the mirror genuinely is the failover and the
        # original message is the true one.
        watches_a_state_licence = primary in RETAIL_SOURCE_KEYS or is_local(primary)
        local = [mirror for mirror in live if is_local(mirror)]
        if watches_a_state_licence and not local:
            named = ", ".join(
                f"{mirror} ({counts[mirror]} quotes)" for mirror in live
            )
            # This finding is about **failover**, and stops there.  It used to end
            # "— {primary} is unobserved", which is :mod:`src.coverage`'s sentence,
            # reported there at ``ERROR`` (``required_book_non_local_only``) on the
            # grounds that a book watched only from out of state is not thin but
            # unseen.  Both fired on the same book in the same run, one word apart
            # and two severities apart, so the run's exit status turned on which
            # module happened to speak.  One claim, one owner: the required-book rule
            # decides whether the jurisdiction observed the book, and this says only
            # that what is up cannot stand in for what is down.
            report.add(
                Severity.WARNING,
                "primary_source_offline_no_local_failover",
                f"{primary} produced no rows. {named} priced the slate but "
                f"publish one board for the whole country rather than {licence} "
                f"licence, so that is context and not failover. Whether anything "
                f"local saw {primary} is the required-book rule's finding",
                source=primary,
            )
        else:
            # Named on the feed that actually stands in.  A national column beside
            # it is not part of the failover claim and saying so here would
            # re-introduce the substitution the branch above refuses.
            standing_in = local or live
            named = ", ".join(
                f"{mirror} ({counts[mirror]} quotes)" for mirror in standing_in
            )
            report.add(
                Severity.WARNING,
                "primary_source_offline",
                f"{primary} produced no rows while {named} priced the slate. "
                f"Using the registered republisher as failover for {primary}",
                source=primary,
            )

    for primary, secondary in REDUNDANT_PAIRS:
        # A pair whose primary was not collected is out of scope, not offline.
        if asked is not None and primary not in asked:
            continue
        primary_leagues = by_source_league.get(primary, set())
        secondary_leagues = by_source_league.get(secondary, set())
        shared = primary_leagues & secondary_leagues
        primary_n = counts.get(primary, 0)
        secondary_n = counts.get(secondary, 0)

        if primary_n > 0 and secondary_n == 0:
            continue
        if primary_n == 0:
            # A dark primary is the grouped pass's business, above, and it decided
            # once for the whole book.  Stated as ``primary_n == 0`` rather than
            # ``primary in offline``: the ``offline`` membership is only ever a
            # subset of this condition, so testing it read as though a dark primary
            # might still be handled here — and drift needs two live feeds anyway.
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
                "are two books — check for stale republished prices or a "
                "parser/jurisdiction mismatch before trusting either leg",
                source=secondary,
            )


__all__ = [
    "REDUNDANT_PAIRS",
    "check_redundancy",
    "is_redundant_pair",
]
