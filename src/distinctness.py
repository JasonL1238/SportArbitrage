"""Whether two sources are two books or one book twice.

This is the check that makes a longer source list mean anything.  Nothing else in
the pipeline can see the difference: a mirror passes the source contract, emits
perfectly valid rows, raises the cross-source coverage counts, and clears
``require_distinct_sources`` in :mod:`src.arb` — because that compares *source
keys*, and two keys is what a mirror has.  What it does not have is two
counterparties.

The failure is not academic.  Kambi fronts a long tail of operators from one
CDN, and on the captured slate ``rsiusil`` (BetRivers Illinois), ``rsiusnj``
(BetRivers New Jersey) and the plain ``kambi`` reference tenant answered with the
same sixteen fixtures at **byte-identical prices** — 30 of 30 shared moneylines
equal.  Registering two of them would let the engine report an arbitrage between
BetRivers and BetRivers: a position that cannot be held, at the top of the
report, indistinguishable from a real one.

So distinctness is measured, not assumed, and it is measured on **prices** rather
than on configuration — a table saying "these two are different" is a claim that
goes stale the first time an operator changes platform.

Two design points:

**An agreement rate, not exact equality.**  Two genuinely independent books do
land on the same price sometimes; -110 is the most common number in the industry.
What separates a mirror is that it agrees *almost always*.

**A minimum sample.**  Three shared selections agreeing three times is not
evidence of anything, and on a thin slate that is all there is.  Below
:data:`MIN_SHARED_SELECTIONS` the answer is "not enough overlap to say", which is
a third verdict and not a pass.

**Per competition, not only overall.**  Two venues can be one feed in some
markets and two in others, and one rate over everything averages that away.  It
is not hypothetical — it is what the live slate does.  BetRivers and LeoVegas
score 64% together and pass as DISTINCT, and underneath that number they agree
on **132 of 132** ITF prices, **46 of 46** ATP Challenger, **40 of 40** ATP,
**34 of 34** WTA and **27 of 27** Bundesliga: for all of tennis they are provably
one price feed.  An "arbitrage" between them in tennis is a position nobody can
hold, and the overall rate is exactly the statistic that hides it.  So each
competition with enough overlap is judged on its own, and a mirror anywhere is a
mirror.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from src.schema import Market, Quote

#: Agreement at or above this is a mirror.  Not 1.0: two tenants of one platform
#: can differ on a handful of prices — a stale cache, a market suspended in one
#: jurisdiction and not another — while still being one counterparty.  Set from
#: the observed spread: the live mirrors agreed on 30 of 30, and the genuinely
#: distinct pair (``leo`` against ``rsiusil``) agreed on well under half.
#: Anything in between is not something the live data produces, so the exact
#: cut-off is not load-bearing; what matters is that it sits far from both.
MIRROR_AGREEMENT_RATE = 0.95

#: Fewer shared selections than this and no verdict is given.  A tiny sample
#: agreeing perfectly is what a one-fixture overlap looks like, and calling that
#: a mirror would reject a real book on a quiet day.
MIN_SHARED_SELECTIONS = 20

#: Markets compared.  The moneyline alone, deliberately: it is the one market
#: every source prices, it needs no line to match on, and adding markets that
#: only some books offer would let the *overlap* rather than the *agreement*
#: decide the answer.
COMPARED_MARKETS: frozenset[Market] = frozenset({Market.MONEYLINE})


class Verdict(StrEnum):
    DISTINCT = "distinct"
    MIRROR = "mirror"
    UNDECIDED = "undecided"

    @property
    def blocks_registration(self) -> bool:
        """Should this stop the source being registered?

        ``UNDECIDED`` does not.  "We could not tell" is a reason to look again on
        a busier slate, not a reason to refuse a book — and treating it as one
        would make every out-of-season sport unaddable.
        """
        return self is Verdict.MIRROR


def _selection_key(quote: Quote) -> tuple:
    return (
        quote.event_key,
        quote.market.value,
        quote.period.value,
        quote.side.value if quote.side else "",
        quote.selection.value,
        "" if quote.line is None else f"{quote.line:g}",
    )


@dataclass(frozen=True)
class LeagueAgreement:
    """The same measurement, narrowed to one competition."""

    league: str
    compared: int
    identical: int

    @property
    def rate(self) -> float:
        return self.identical / self.compared if self.compared else 0.0

    @property
    def is_mirror(self) -> bool:
        """Enough overlap to judge, and agreement above the cut."""
        return (
            self.compared >= MIN_SHARED_SELECTIONS
            and self.rate >= MIRROR_AGREEMENT_RATE
        )


@dataclass(frozen=True)
class Agreement:
    """How often two sources published exactly the same price."""

    source_a: str
    source_b: str
    compared: int
    identical: int
    examples: tuple[tuple[str, float, float], ...] = ()
    """Up to a few ``(selection key, price a, price b)`` for a human to check."""
    by_league: tuple[LeagueAgreement, ...] = ()
    """The same measurement per competition, most-compared first."""

    @property
    def rate(self) -> float:
        return self.identical / self.compared if self.compared else 0.0

    @property
    def mirrored_leagues(self) -> tuple[LeagueAgreement, ...]:
        """Competitions in which these two are one price feed."""
        return tuple(entry for entry in self.by_league if entry.is_mirror)

    @property
    def verdict(self) -> Verdict:
        """MIRROR if they are one feed overall **or** in any one competition.

        The second half is what the live slate needs.  Judging only the overall
        rate lets a pair that is byte-identical across all of tennis pass as
        distinct because it disagrees about hockey, and the tennis prices are
        the ones that would be reported as an arbitrage nobody can hold.

        Note the asymmetry with UNDECIDED: a league too thin to judge cannot
        make a pair a mirror, but it cannot rescue one either — a mirror found
        anywhere stands, because the evidence for it does not stop being
        evidence when other markets disagree.
        """
        if self.mirrored_leagues:
            return Verdict.MIRROR
        if self.compared < MIN_SHARED_SELECTIONS:
            return Verdict.UNDECIDED
        return Verdict.MIRROR if self.rate >= MIRROR_AGREEMENT_RATE else Verdict.DISTINCT

    def summary(self) -> str:
        if self.verdict is Verdict.UNDECIDED:
            return (
                f"{self.source_a} vs {self.source_b}: only {self.compared} shared "
                f"selection(s), fewer than the {MIN_SHARED_SELECTIONS} needed to judge — "
                "no verdict"
            )
        line = (
            f"{self.source_a} vs {self.source_b}: {self.identical}/{self.compared} "
            f"shared moneyline prices identical ({self.rate * 100:.1f}%) — "
            f"{self.verdict.value.upper()}"
        )
        mirrored = self.mirrored_leagues
        if mirrored and self.rate < MIRROR_AGREEMENT_RATE:
            # The overall number does not show why, so it is spelled out: this
            # is the case the overall number was hiding.
            detail = ", ".join(
                f"{entry.league} {entry.identical}/{entry.compared}" for entry in mirrored
            )
            line += f"; one feed in {detail}"
        return line


def _priced(quotes: Iterable[Quote]) -> dict[str, dict[tuple, float]]:
    """Best price per source per selection, over the compared markets.

    "Best" rather than "first" so the comparison does not depend on row order,
    and so an alternate-line duplicate cannot make two identical books look
    different.
    """
    prices: dict[str, dict[tuple, float]] = defaultdict(dict)
    for quote in quotes:
        if quote.market not in COMPARED_MARKETS:
            continue
        key = _selection_key(quote)
        current = prices[quote.source].get(key)
        if current is None or quote.decimal_odds > current:
            prices[quote.source][key] = quote.decimal_odds
    return prices


def _leagues(quotes: Iterable[Quote]) -> dict[tuple, str]:
    """Which competition each compared selection belongs to.

    Keyed on the selection rather than on the source, because the two sources
    have to be counted under one competition to be compared under it.  Where they
    disagree about the league — which they do constantly, and which is already
    reported as ``league_disagreement`` — the **modal** spelling wins, matching
    :func:`src.arb._counterparties`, which reads this table back.

    The two used to disagree, and the mirror gate fell through the gap.  Filing
    by the lexicographically first spelling let a single dissenting source decide
    the bucket: Matchbook labels 22 tennis fixtures ``ATP`` where the rest of the
    slate says ``ATP_CHALLENGER``, ``ATP`` sorts first, and 44 Challenger
    selections on which the two Kambi tenants agree **44 of 44** were filed under
    ATP.  ``ATP_CHALLENGER`` therefore never appeared in the gate at all, and a
    position between one book and itself was reportable on exactly those
    fixtures.  Ties break on the name, so the answer stays deterministic.
    """
    votes: dict[tuple, Counter[str]] = defaultdict(Counter)
    for quote in quotes:
        if quote.market not in COMPARED_MARKETS:
            continue
        votes[_selection_key(quote)][quote.league] += 1
    return {
        key: min(counts.most_common(), key=lambda item: (-item[1], item[0]))[0]
        for key, counts in votes.items()
    }


def compare_sources(
    quotes: Sequence[Quote],
    source_a: str,
    source_b: str,
    *,
    prices: Mapping[str, Mapping[tuple, float]] | None = None,
    league_of: Mapping[tuple, str] | None = None,
) -> Agreement:
    """How closely two sources agree on the selections they both price.

    *prices* and *league_of* are the two derived tables, passed in when a caller
    is comparing many pairs over one slate.  Recomputing them per pair is a full
    scan of every quote inside a loop over ``sources²``: at ten sources the
    constant was small enough not to matter, and at thirty it added 28 seconds to
    a detection pass that is otherwise under a second.
    """
    prices = _priced(quotes) if prices is None else prices
    league_of = _leagues(quotes) if league_of is None else league_of
    left, right = prices.get(source_a, {}), prices.get(source_b, {})
    shared = sorted(set(left) & set(right))
    identical = 0
    differences: list[tuple[str, float, float]] = []
    per_league: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key in shared:
        bucket = per_league[league_of.get(key, "")]
        bucket[0] += 1
        if left[key] == right[key]:
            identical += 1
            bucket[1] += 1
        elif len(differences) < 5:
            differences.append(("|".join(str(part) for part in key), left[key], right[key]))
    return Agreement(
        source_a=source_a,
        source_b=source_b,
        compared=len(shared),
        identical=identical,
        examples=tuple(differences),
        by_league=tuple(
            LeagueAgreement(league=league, compared=count, identical=same)
            for league, (count, same) in sorted(
                per_league.items(), key=lambda item: (-item[1][0], item[0])
            )
        ),
    )


def find_mirrors(quotes: Sequence[Quote]) -> list[Agreement]:
    """Every pair of sources in *quotes* that looks like one counterparty."""
    return [pair for pair in compare_all(quotes) if pair.verdict is Verdict.MIRROR]


def compare_all(quotes: Sequence[Quote]) -> list[Agreement]:
    """Every source pair's agreement, ordered most-agreeing first."""
    sources = sorted({quote.source for quote in quotes})
    # Derived once and shared: the pair loop is quadratic in sources, and a
    # rescan of every row inside it makes the whole thing quadratic in rows too.
    prices = _priced(quotes)
    league_of = _leagues(quotes)
    pairs = [
        compare_sources(quotes, first, second, prices=prices, league_of=league_of)
        for index, first in enumerate(sources)
        for second in sources[index + 1 :]
    ]
    return sorted(pairs, key=lambda pair: (-pair.rate, pair.source_a, pair.source_b))


def screen_candidate(
    candidate_quotes: Sequence[Quote], registered_quotes: Sequence[Quote]
) -> list[Agreement]:
    """Judge one candidate source against every source already registered.

    The gate a new source has to pass before it is added.  Returns one
    :class:`Agreement` per registered source, worst first, so the caller can
    refuse on the first :attr:`Verdict.MIRROR` and record the partner it mirrors
    — a rejected candidate is worth writing down with its reason, or it gets
    rediscovered and re-added six months later.
    """
    combined = [*candidate_quotes, *registered_quotes]
    candidates = {quote.source for quote in candidate_quotes}
    if len(candidates) != 1:
        raise ValueError(
            f"expected rows from exactly one candidate source, got {sorted(candidates)}"
        )
    candidate = next(iter(candidates))
    others = sorted({quote.source for quote in registered_quotes} - candidates)
    results = [compare_sources(combined, candidate, other) for other in others]
    return sorted(results, key=lambda pair: -pair.rate)
