"""Concrete promo-usage plans built from the same slate the arb detector reads.

:mod:`src.promos.strategy` writes a generic playbook ("bet a liquid market,
hedge elsewhere").  This module replaces the generic advice with the actual
best executions available **right now**: for each scraped offer it scans the
games collected from that same book and pairs them with hedge prices from the
rest of the slate, producing named legs with stakes and a per-outcome profit
table.

Ground rules, in order of importance:

* **A promo is only usable at the book that issued it.**  The promo-side leg of
  every plan is drawn exclusively from that brand's own odds feed (first-party
  adapter, or its Action Network failover view of the same book).  A better
  price for the same selection at another book is a *hedge* candidate, never a
  promo leg.
* **Hedges must be genuinely elsewhere.**  A hedge at the same counterparty —
  the same brand's failover feed, a Kambi licence mirror, anything the measured
  counterparty gate has folded together — is not a hedge, it is doubling down
  at one house.  Separately, and across every leg rather than only against the
  promo book, no position may take both feeds of one failover pair: those are
  one book seen twice, so :func:`src.arb._uses_both_failover_feeds` refuses the
  stitch here exactly as it does for an arb position.
* **No joins across sport, period, market, line, or settlement rule.**  Groups
  come from :func:`src.arb.group_key`; books with mismatched contract shapes,
  ambiguous tie settlement, or a different postponement regime are counted out,
  exactly as the arb detector counts them out.
* **Plans are computed, never stored.**  Odds move; a plan written into the
  promo database would outlive the prices it quotes.  The planner is a pure
  function of (offers, quotes, as_of) and runs at page-build time.

Dollar figures are conversion arithmetic on the venue's published prices, the
same category of number as the arb detector's guaranteed-profit column.  The
floor reported for a plan is the minimum over every enumerated settlement
outcome, computed from the rounded stakes actually printed.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from src.arb import (
    HALF_WIN,
    LINE_UNSUPPORTED,
    LOSE,
    MAX_OBSERVATION_SPREAD,
    PUSH,
    REFUSE_MARGIN,
    WIN,
    MarketGroup,
    _counterparties,
    _fixture_outliers,
    _order_driven_sources,
    _return_multiplier,
    _uses_both_failover_feeds,
    contract_shape,
    crossed_against_itself,
    counterparty_groups,
    group_key,
    line_granularity,
    net_decimal,
    settlement_outcomes,
)
from src.betlinks import link_payload
from src.commission import Commission
from src.promos.redundancy import brand_key
from src.promos.schema import PromoKind
from src.redundancy import REDUNDANT_PAIRS as ODDS_REDUNDANT_PAIRS
from src.redundancy import is_redundant_pair
from src.schema import Market, Quote, QuoteStatus, Selection, Sport
from src.settlement import regime_for
from src.sources.registry import SOURCES, VIEW_ONLY_SOURCES
from src.vocab import draw_is_priced

#: How many concrete executions to keep per offer.  The dashboard shows a
#: detail panel, not a screener — three named games is guidance, thirty is
#: another board.
MAX_PLANS_PER_OFFER = 3

#: Dollar unit used when the offer does not state its own amount.  Every stake
#: in a plan scales linearly, so per-$100 figures are exact, not estimates.
PLAN_UNIT = 100.0

#: How many hedge books to consider per selection before enumerating
#: assignments.  Prices below the fourth-best hedge cannot win the floor, so
#: widening this only slows the scan.
_HEDGE_CANDIDATES_PER_SELECTION = 4

#: How many assignments to keep per market when the scan's stake is
#: provisional.  The published-limit gate cannot run at a stake nobody places,
#: so it runs later at the offer's real size — and a market must carry spares,
#: or the best-priced-but-unplaceable assignment takes the market down with it.
_FALLBACKS_PER_MARKET = 4

#: Modes the stake solver knows.  ``bonus`` is stake-not-returned credit,
#: ``cash`` is real money, ``boosted`` is real money whose winnings are
#: multiplied before settlement.
MODE_BONUS = "bonus"
MODE_CASH = "cash"
MODE_BOOSTED = "boosted"

#: The planner's own tolerance, used by ten comparisons here.  The eleventh —
#: "this venue prices itself to lose" — now runs inside
#: :func:`arb.crossed_against_itself` and therefore follows *arb's* epsilon, not
#: this one.  The two are equal today; if either moves, that one comparison
#: moves with arb rather than with its neighbours here.
_EPSILON = 1e-9


# ── offer-text parsing ───────────────────────────────────────────────────────

_AMERICAN_ODDS = re.compile(r"^\s*([+-])\s*(\d{3,5})\s*$")
_DECIMAL_ODDS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")

#: The ``Bet $X … get $Y`` shape, whether it arrives in the enricher's own
#: canonical wording (:func:`src.promos.enrich.extract_mechanics` writes ``Bet
#: $5, get $150 in bonus bets``) or in the venue's, which is what actually
#: reaches here: ``enrich`` keeps a venue summary whenever it is already
#: specific, so the single commonest welcome-offer phrasing in the wild —
#: "Bet $5 **or more**, get $150 in bonus bets" — never had the comma where a
#: strict pattern demanded it.  The qualifying leg, its cost and the whole
#: expected-value line disappeared silently for it.
#:
#: The gap between the two amounts is deliberately narrow: any run of words
#: without a digit or a second dollar sign, so "bet $5 or more, get $150" and
#: "bet $5 to get $150" both match while "bet $5 … $20 … get $150" does not
#: quietly pair the wrong two numbers.
_QUALIFYING_SUMMARY = re.compile(
    r"^\s*bet\s+\$([0-9][0-9,]*(?:\.[0-9]+)?)"
    # Connectives only — not an arbitrary run of characters.  A 24-character
    # window crossed whole clauses, so "Bet $50 first bet, if it loses get $50
    # back" parsed as a bet-and-get and its conditional refund was priced as
    # credit that always arrives.
    # "$5+", "$5 or more", "$5 minimum" — the plus sign is written flush
    # against the amount, so a mandatory space made that alternative dead.
    r"(?:\s*\+|\s+(?:or\s+more|or\s+greater|minimum|min))?"
    r"\s*[,:&]?\s*(?:and\s+|&\s*|then\s+|to\s+|-\s*)?"
    r"(?:get|receive|earn)\s+(?:up\s+to\s+)?\$",
    re.IGNORECASE,
)

#: A refund dressed as a bet-and-get.  The connective narrowing caught the form
#: where the condition precedes ``get``; this catches the commoner one where it
#: trails, which otherwise priced a second-chance offer as credit that always
#: arrives — roughly double its real value, since the credit lands only when
#: the qualifying bet loses.
_CONDITIONAL_REFUND = re.compile(
    r"\b(?:"
    # Named second-chance products.
    r"second\s+chance|2nd\s+chance|bet\s+reset|money\s+back|stake\s+refund"
    # "…if/when/should your bet loses", any ordering.  ``you`` alongside
    # ``your``: "if you lose your first bet" is as common as "if your first bet
    # loses" and matched nothing, pricing a second-chance offer as credit in
    # hand.  ``loss``/``lost`` cover "settles as a loss", "settled as a loss".
    r"|(?:if|when|should)\s+(?:you|your|it|the)\b[^.;]{0,48}?"
    r"\b(?:lose|loses|losing|lost|loss)\b"
    # "doesn't win", "does not win", "is not a winner", "not a winner?" — the
    # standard US safety-net wording, written that way precisely because a
    # push or a void is not a loss.  Without this the *win* pattern below
    # matched the negated clause and stamped the offer's own inverse on it.
    r"|(?:do(?:es)?n[’']?t|do(?:es)?\s+not|did\s+not|didn[’']?t|never|not)"
    r"\s+(?:a\s+)?\b(?:win|wins|won|winner)\b"
    # "…unless it wins" — a loss condition stated the other way round.
    r"|unless\s+(?:your|it|the)\b[^.;]{0,48}?\b(?:wins?|won)\b"
    r"|if\s+lose\b|on\s+(?:a|your)\b[^.;]{0,24}?\blosing\b"
    # A refund word only when a loss sits in the same clause, either order.
    # A bare participle was overwhelmingly the void/postponement/fee sentence
    # ("wagers refunded on postponement", "withdrawal fees are refunded"),
    # which routed an ordinary bet-and-get into the insurance model; and it
    # matched only ``refunded``, missing ``refund``/``refunds`` where the real
    # mechanic is usually written.
    r"|(?:refunds?|refunded)\b[^.;]{0,60}?\b(?:lose|loses|losing|lost)\b"
    r"|(?:lose|loses|losing|lost)\b[^.;]{0,60}?\b(?:refunds?|refunded)\b"
    r")",
    re.IGNORECASE,
)

#: Credit that arrives only if the qualifying bet *wins*.  Consulted only when
#: no loss condition is present: copy that spells out both branches — "if it
#: loses you get it back; if it wins you keep the winnings", which is how
#: venues ordinarily write a safety net — is a refund, and letting the win
#: clause veto that priced a real second-chance offer as credit in hand.  On
#: live offers that overstated the guarantee by 54-62%, staked credit the
#: operator does not hold, and attached a caveat contradicting the offer's own
#: title ("$1500 Bonus Bets If Lose").
_WIN_CONDITIONAL = re.compile(
    r"\b(?:if|when|should|once)\s+(?:your?|it|the)\b[^.;]{0,48}?\b(?:wins?|won)\b",
    re.IGNORECASE,
)
#: ``your?`` and not ``your`` alone: the first live offer this planner ever
#: priced was TheLines' BetMGM "Bet $10 get $150 in bonus bets **if you win**",
#: and the subject set missing the bare ``you`` priced conditional credit as
#: unconditional — ev printed with no worth-once-it-lands caveat.  Admitting
#: ``you`` re-opens the "when you place your first bet — win or lose" trap the
#: comment below records, which is why _reward_is_win_contingent now applies
#: the same unconditional veto the loss reading has always had.


#: Copy that states the reward arrives *whatever* happens.  These are fixed
#: idioms — "win or lose", "whether it wins or loses", "regardless of the
#: outcome" — and every one of them names a loss inside the window
#: ``_CONDITIONAL_REFUND`` scans, so widening that pattern's subject set to
#: include ``you`` made the commonest bet-and-get wording in the market read as
#: insurance: "get $150 in bonus bets when you place your first $5 bet — win or
#: lose" routed a $5 qualifier into ``_plan_no_sweat``, printing a card that
#: staked $383 of the operator's own money for a $66.66 floor, with no caveat,
#: where the truth is $15 at risk for $100.  An unconditional phrase vetoes the
#: loss reading outright: it is a statement about *both* branches, so neither
#: branch is a condition.
_UNCONDITIONAL_REWARD = re.compile(
    r"\b(?:"
    r"win\s+or\s+lose|lose\s+or\s+win|won\s+or\s+lost"
    r"|whether\s+(?:or\s+not\s+)?(?:you|it|they|your)\b[^.;]{0,32}?\b(?:wins?|won)\b"
    r"|regardless\s+of\s+(?:the\s+)?(?:outcome|result|whether)"
    r"|no\s+matter\s+(?:the\s+)?(?:outcome|result|what|if)"
    r"|either\s+way"
    r")",
    re.IGNORECASE,
)


def _reward_is_loss_contingent(summary: str) -> bool:
    """Does the reward arrive only when the qualifying bet loses?

    An explicit "win or lose" wins over any loss word beside it: the phrase
    exists precisely to say the reward is unconditional, so a loss named in the
    same breath is one half of a promise, not a condition.
    """
    if _UNCONDITIONAL_REWARD.search(summary):
        return False
    return bool(_CONDITIONAL_REFUND.search(summary))


def _reward_is_win_contingent(summary: str) -> bool:
    """Only when a win is named and no loss condition is — a loss wins ties.

    The unconditional veto applies here exactly as it does to the loss reading:
    "win or lose" names a win, but as one half of a promise about both
    branches, not as a condition.  Without the veto, admitting the bare "you"
    subject would have re-read the commonest bet-and-get wording in the market
    ("when you place your first $5 bet — win or lose") as win-contingent.
    """
    if _UNCONDITIONAL_REWARD.search(summary):
        return False
    return bool(
        _WIN_CONDITIONAL.search(summary) and not _CONDITIONAL_REFUND.search(summary)
    )

_BOOST_PERCENT = re.compile(r"(\d{1,3})\s*%\s*(?:profit\s+|odds\s+)?boost", re.IGNORECASE)

_WAGERING_MULTIPLE = re.compile(r"(\d+(?:\.\d+)?)\s*x", re.IGNORECASE)


def parse_min_odds(text: str | None) -> float | None:
    """Published minimum-odds constraint as decimal odds, or ``None``.

    Books state the floor in American (``-200``, ``+100``) or decimal
    (``1.5``) form; the constraint always means "at least this long".  A value
    that parses to decimal odds at or below 1.0 is not odds at all and is
    treated as unparseable rather than as a constraint that excludes
    everything.
    """
    if not text:
        return None
    raw = str(text).strip()
    m = _AMERICAN_ODDS.match(raw)
    if m:
        magnitude = float(m.group(2))
        if magnitude == 0:
            return None
        if m.group(1) == "-":
            return 1.0 + 100.0 / magnitude
        return 1.0 + magnitude / 100.0
    m = _DECIMAL_ODDS.match(raw)
    if m:
        value = float(m.group(1))
        # A bare integer of 100 or more is American shorthand ("200" for +200),
        # which venues do write.  Between 1 and ~50 it is decimal odds.
        if value >= 100.0:
            return 1.0 + value / 100.0
        if value > 1.0:
            return value
    return None


def parse_qualifying_stake(summary: str | None) -> float | None:
    """The ``$X`` of a canonical ``Bet $X, get $Y …`` summary, or ``None``."""
    if not summary:
        return None
    m = _QUALIFYING_SUMMARY.match(summary)
    if not m:
        return None
    # Through ``_reward_is_loss_contingent``, not the raw pattern: an explicit
    # "win or lose" means the reward is unconditional, and reading the loss word
    # inside it as a condition threw away the qualifying stake — the one number
    # that makes this a bet-and-get rather than an insurance play.
    if _reward_is_loss_contingent(summary):
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_boost_percent(*parts: str | None) -> float | None:
    """A stated ``NN% (profit) boost`` from title/summary text, or ``None``."""
    for part in parts:
        if not part:
            continue
        m = _BOOST_PERCENT.search(part)
        if m:
            value = float(m.group(1))
            if 0 < value <= 300:
                return value
    return None


def parse_wagering_multiple(text: str | None) -> float | None:
    """``"1x"`` / ``"10x deposit"`` → 1.0 / 10.0, or ``None``."""
    if not text:
        return None
    m = _WAGERING_MULTIPLE.search(str(text))
    if not m:
        return None
    value = float(m.group(1))
    return value if value > 0 else None


# ── brand → stakeable odds feeds ─────────────────────────────────────────────

_REGISTERED_KEYS = frozenset(descriptor.key for descriptor in SOURCES)

#: Venues whose prices are a resting order book rather than a book's own line.
#: They are judged net of fee for self-crossing, exactly as
#: :func:`src.arb.find_opportunities` judges them: a sum below 1.0 at a
#: sportsbook is evidence about the parser, but at an order book it only means
#: two strangers left orders there, and "crossed" is a question about prices
#: **after** the venue's charge.
#: Read through :func:`arb._order_driven_sources`, not re-derived: this used to
#: string-match ``kind.value`` against a literal ``{"exchange",
#: "prediction_market"}``, which is a second spelling of the registry's own
#: ``SourceKind.has_stated_liquidity`` and would silently disagree with the
#: arbitrage engine the first time a kind was added.
_ORDER_DRIVEN = _order_driven_sources()


def stakeable_odds_sources(promo_source: str) -> tuple[str, ...]:
    """Odds feeds whose prices can be **staked at the promo's own book**.

    First-party feed first, then every registered redundant observation of the
    same counterparty (Action Network, VegasInsider, or VSiN). An empty tuple
    means the slate has no view of this book at all (Ontario tenants,
    promo-only brands), and the caller falls back to text guidance rather than
    borrowing another book's games.

    The ``VIEW_ONLY_SOURCES`` filter here is the **ambient** set, and that is a
    known, deliberately-conservative wrinkle rather than an oversight: whether
    a feed is stakeable is a per-run fact (``hardrock`` differs between IL and
    PA), but this filter can only *suppress* a plan — a stored IL run planned
    from a PA-configured box loses its hardrock plans; nothing is ever staked
    at a book the run's own set forbids, because ``_build_context``'s
    run-state ``usable`` filter guards the money path downstream.  Threading
    the run state here would recover those suppressed plans, at the cost of a
    state parameter through three registry-shape helpers; taken knowingly.
    """
    brand = brand_key(promo_source)
    ordered: list[str] = [brand]

    def expand() -> None:
        for source in tuple(ordered):
            for left, right in ODDS_REDUNDANT_PAIRS:
                if left == source and right not in ordered:
                    ordered.append(right)
                elif right == source and left not in ordered:
                    ordered.append(left)

    # Components here are tiny triangles; repeat until the transitive same-book
    # observations are all named.  They remain one counterparty in execution.
    while True:
        before = len(ordered)
        expand()
        if len(ordered) == before:
            break
    keys = [
        key
        for key in ordered
        if key in _REGISTERED_KEYS and key not in VIEW_ONLY_SOURCES
    ]
    if not keys:
        failover = f"an_{brand}"
        if failover in _REGISTERED_KEYS and failover not in VIEW_ONLY_SOURCES:
            ordered = [failover]
            while True:
                before = len(ordered)
                expand()
                if len(ordered) == before:
                    break
            keys = [
                key
                for key in ordered
                if key in _REGISTERED_KEYS and key not in VIEW_ONLY_SOURCES
            ]
    return tuple(keys)


# ── slate context ────────────────────────────────────────────────────────────


def _scan_into(skipped: Counter, *args, **kwargs) -> list["_Candidate"]:
    """Run one scan with a private counter and max-merge it into *skipped*.

    Every ``_scan`` call goes through here so no strategy can accidentally
    share one counter across two traversals of the same slate — see
    :func:`_merge_counts` for why that double-counted.
    """
    counts: Counter = Counter()
    found = _scan(*args, skipped=counts, **kwargs)
    _merge_counts(skipped, counts)
    return found


def _drop_group(
    context: "_PlanContext", reason: str, key: MarketGroup, rows: Sequence[Quote]
) -> None:
    """Record a group dropped before any book was indexed, per source."""
    context.unusable[reason] += 1
    by_source = context.dropped_groups.setdefault(reason, {})
    for source in {row.source for row in rows}:
        by_source.setdefault(source, set()).add(key)


def _dropped_over(context: "_PlanContext", promo_keys: Sequence[str]) -> Counter:
    """Pre-index drops for a brand, deduped per market rather than per feed.

    A market both feeds saw counts once; a market only one feed saw still
    counts.  Maxing the per-feed counters got the first right and the second
    wrong.
    """
    out: Counter = Counter()
    for reason, groups in context.dropped_groups.items():
        hit = {key for source in promo_keys for key in groups.get(source, ())}
        if hit:
            out[reason] = len(hit)
    return out


def _over_stated_limit(legs: Sequence["_Leg"]) -> bool:
    """Does any leg exceed the size its venue publishes?

    Checked wherever stakes are produced, not once in the solver: conversions
    are solved at :data:`PLAN_UNIT` and then scaled to the offer's real amount,
    so a hedge that fitted a $200 limit at $100 of credit is a $1333 leg at
    $1000 of credit.  The solver's check passed and the scaled plan was printed
    anyway — the very defect the check was added for, surviving for every
    offer whose bonus is not exactly $100, which is nearly all of them.
    """
    for leg in legs:
        limit = leg.quote.limit_amount
        if limit is not None and leg.stake > limit + _EPSILON:
            return True
    return False


#: Prefix for post-scan refusal tallies held alongside the scan counts.
#: Reconciled and stripped by :func:`_settle_counts`.
_POST_SCAN = "__post__"


def _refuse_market(skipped: Counter, reason: str, candidate: "_Candidate") -> None:
    """Record a refusal that happened *after* the scan, once per market.

    Held apart from the scan counts rather than merged into them.  The two
    phases refuse disjoint sets — a market has to survive the scan to reach
    ``_rescale`` at all — so their totals add; but two *scans* of one slate see
    the same refusal twice, so those take the larger.  One ``max`` over both
    was wrong in both directions: it reported 6 refusals where 3 markets
    existed in the boost path, and 3 where 5 markets were refused in the
    conversion path.
    """
    _record_refused_market(skipped, reason, candidate.view.key)


#: Where :func:`_refuse_market` parks the market keys it has refused, keyed by
#: reason.  Lives on the counter so it travels with the offer without changing
#: any signature; stripped by :func:`_settle_counts`.
_POST_SCAN_MARKETS = "__post_markets__"


def _record_refused_market(skipped: Counter, reason: str, market_key) -> None:
    """Note that one market was refused for one reason.

    Every phase records the same unit.  The scan counts refused *assignments*
    — many per market, and two scans of one slate see each twice — while the
    rescale counts markets; adding those gave a number that was neither, and
    maxing them under-reported.  A market refused in both phases is one
    refused market, and that is what the caveat beside these counts promises.
    """
    _refused_markets(skipped).setdefault(reason, set()).add(str(market_key))


def _refused_markets(skipped: Counter) -> dict[str, set]:
    holder = skipped.get(_POST_SCAN_MARKETS)
    if not isinstance(holder, dict):
        holder = {}
        skipped[_POST_SCAN_MARKETS] = holder
    return holder


def _settle_counts(skipped: Counter) -> dict[str, int]:
    """Fold post-scan refusals into the scan counts and drop the bookkeeping.

    Max within a phase (two scans of one slate see the same refusal twice),
    add across phases (the scan and the rescale refuse different markets) —
    and *per market*, so a market refused in both phases counts once.
    """
    markets = skipped.get(_POST_SCAN_MARKETS)
    markets = markets if isinstance(markets, dict) else {}
    out: dict[str, int] = {}
    for key, count in skipped.items():
        if key == _POST_SCAN_MARKETS or key.startswith(_POST_SCAN):
            continue
        out[key] = count
    # Market-keyed reasons are the size of the union, whichever phase saw them.
    for reason, keys in markets.items():
        out[reason] = len(keys)
    return dict(sorted(out.items()))


def _merge_counts(into: Counter, counts: Mapping[str, int]) -> None:
    """Fold one scan's gate counts into an offer's, taking the **larger**.

    Not ``Counter.update``, which adds.  Every scan for one offer walks the
    same slate, so a market refused by ``regime_mismatch`` in the qualifying
    scan is the same market refused in the conversion scan — one refusal seen
    twice, not two refusals.  Summing them doubled every count for the two-scan
    strategies (``no_sweat``, ``qualify_then_convert``): a book showing
    ``regime_mismatch ×90`` under a one-scan strategy showed ``×180`` under a
    two-scan one, on the same run, beside a caveat promising the counts say
    what was refused.
    """
    for reason, count in counts.items():
        if reason == _POST_SCAN_MARKETS:
            # The refused-market sets ride along on the same counter; they
            # union rather than max, because two scans naming different
            # markets refused both of them.
            holder = _refused_markets(into)
            for key, keys in (count or {}).items():
                holder.setdefault(key, set()).update(keys)
            continue
        if count > into.get(reason, 0):
            into[reason] = count


def _row_rank(
    row: Quote, commissions: Mapping[str, Commission] | None
) -> tuple[float, float, bool]:
    """Total order over one book's rows for one selection, best first.

    Price decides; everything after it exists so that *ties* decide the same
    way whatever order the rows arrived in.  A book routinely prices a main
    line and an alternate ladder at the same number, and picking whichever the
    list happened to hold first made two things depend on row order: the
    ``observed_at`` printed beside the plan, and — because the freshness of the
    chosen row feeds ``MAX_OBSERVATION_SPREAD`` — whether the plan existed at
    all.  Freshest wins the tie, then the main line over an alternate, then the
    market id as a last resort so the order is total rather than merely
    usually-decisive.

    Three keys are enough to be total, and a fourth would be unreachable: two
    rows sharing ``(source, selection, is_alternate)`` are caught by the
    duplicate fingerprint above and never reach this comparison, so the only
    pair it ever sees is a main line against an alternate — which the third key
    already separates.  A market-id tiebreak sat here looking load-bearing and
    could be deleted with the whole suite green, because nothing can reach it.
    """
    return (
        net_decimal(row, commissions),
        row.observed_at.timestamp(),
        not row.is_alternate,
    )


def _better_row(
    row: Quote, held: Quote, commissions: Mapping[str, Commission] | None
) -> bool:
    return _row_rank(row, commissions) > _row_rank(held, commissions)


@dataclass(frozen=True)
class _GroupView:
    """One comparable market, cleaned the way ``_examine_group`` cleans it."""

    key: MarketGroup
    sport: Sport
    market: Market
    rows: tuple[Quote, ...]
    #: Best ACTIVE row per (source, selection), better of main/alternate.
    best: Mapping[str, Mapping[Selection, Quote]]
    #: Contract shape per source, judged from all rows including suspended.
    shapes: Mapping[str, frozenset[Selection]]
    #: Sources excluded for publishing duplicate prices at one alternate status.
    faulted: frozenset[str]
    commence: datetime


@dataclass
class _PlanContext:
    """Everything the per-offer scans share, computed once per slate."""

    as_of: datetime
    commissions: Mapping[str, Commission] | None
    one_counterparty: Mapping[str, Sequence[frozenset[str]]]
    #: The run's jurisdiction, for leg links: a state-partitioned book's front
    #: door is that state's own.  ``None`` for an ungoverned run.
    state: str | None = None
    groups: dict[MarketGroup, _GroupView] = field(default_factory=dict)
    groups_by_source: dict[str, list[MarketGroup]] = field(default_factory=dict)
    #: Groups dropped before any book was indexed, per source that had rows in
    #: them: ``{source: Counter({reason: n})}``.  Without this a book whose
    #: games have *all* started reads as "no rows from this book" — identical
    #: to a book the collection genuinely missed — which points the operator at
    #: the collector when the real answer is "you are looking at a stale run".
    #: On live data four hours after a scrape that was 16 of 60 offers.
    dropped_groups: dict[str, dict[str, set]] = field(default_factory=dict)
    #: Groups a source was excluded from for describing a different fixture, or
    #: for pricing its own complete market below 1.0 — a parse fault either way,
    #: reported per source so the offer whose book it is can say so.
    excluded_groups: dict[str, dict[str, set]] = field(default_factory=dict)
    #: Groups a source was *excluded* from for publishing two prices for one
    #: selection at one alternate status.  Indexed separately from
    #: ``groups_by_source`` precisely because the source is not in that index —
    #: it was popped — so without this a parse fault at the promo book made its
    #: markets disappear with no count anywhere, and a book whose every market
    #: faulted was reported as having produced no rows at all.
    faulted_by_source: dict[str, list[MarketGroup]] = field(default_factory=dict)
    #: Group-level facts that removed a group before any offer saw it.
    unusable: Counter = field(default_factory=Counter)


def _build_context(
    quotes: Sequence[Quote],
    *,
    as_of: datetime,
    commissions: Mapping[str, Commission] | None,
    one_counterparty: Mapping[str, Sequence[frozenset[str]]] | None,
    state: str | None = None,
) -> _PlanContext:
    # The plan's own state decides which feeds are view-only — the ambient
    # constant is frozen from this process's ODDS_STATE and disagrees on
    # ``hardrock`` between IL and PA/DC, so a stored PA run planned from an IL
    # box would have staked promo legs at a book Pennsylvania declares
    # view-only.  ``state=None`` (an ungoverned run) resolves to the
    # republished-only set, the same generous fallback every other reader uses.
    from src.sources.registry import view_only_for_run

    plan_view_only = view_only_for_run((state or "").strip().upper())
    usable = [quote for quote in quotes if quote.source not in plan_view_only]
    context = _PlanContext(
        as_of=as_of,
        commissions=commissions,
        state=state,
        one_counterparty=(
            counterparty_groups(usable, view_only=plan_view_only)
            if one_counterparty is None
            else one_counterparty
        ),
    )

    grouped: dict[MarketGroup, list[Quote]] = defaultdict(list)
    for quote in usable:
        grouped[group_key(quote)].append(quote)

    for key, rows in grouped.items():
        _, market, _, _, line = key

        # Same refusals as the arb detector, for the same reasons: a group whose
        # sport is contested has unknowable settlement rules, and a line at an
        # unmodelled granularity would be settled as though it could never land
        # on its own number.
        sports = {row.sport for row in rows}
        if len(sports) > 1:
            majority = Counter(row.sport for row in rows).most_common()
            if len(majority) > 1 and majority[0][1] == majority[1][1]:
                _drop_group(context, "mixed_sport", key, rows)
                continue
            keep = majority[0][0]
            rows = [row for row in rows if row.sport is keep]
        if line is not None and line_granularity(line) == LINE_UNSUPPORTED:
            _drop_group(context, "unsupported_line_granularity", key, rows)
            continue

        # A fixture any source says has started cannot be planned: both the
        # promo leg and the hedge would be bets on a game already running.
        commence = min(row.commence_time for row in rows)
        if commence <= as_of:
            _drop_group(context, "already_started", key, rows)
            continue

        sport = rows[0].sport
        rows_by_source: dict[str, list[Quote]] = defaultdict(list)
        for row in rows:
            rows_by_source[row.source].append(row)
        shapes = {
            source: contract_shape(sport, market, key[2], source_rows)
            for source, source_rows in rows_by_source.items()
        }

        best: dict[str, dict[Selection, Quote]] = defaultdict(dict)
        seen: set[tuple[str, Selection, bool]] = set()
        faulted: set[str] = set()
        for row in rows:
            if row.status is not QuoteStatus.ACTIVE:
                continue
            fingerprint = (row.source, row.selection, row.is_alternate)
            if fingerprint in seen:
                # Two prices for one selection at one alternate status is a
                # parse fault; betting the better of them is betting on the
                # fault.  The book is excluded, the group survives.
                faulted.add(row.source)
                continue
            seen.add(fingerprint)
            held = best[row.source].get(row.selection)
            if held is None or _better_row(row, held, context.commissions):
                best[row.source][row.selection] = row
        for source in faulted:
            best.pop(source, None)

        # Two exclusions the arb detector applies and this module's docstring
        # claims parity with.  Both are about a book whose rows are not the bet
        # they appear to be, so a plan drawn from one is not the plan printed.
        #
        # A source describing a different fixture from the consensus: with the
        # home and away sides inverted at one book, the "hedge" backs the same
        # team as the promo leg.  The detector's comment calls it "cheap to
        # check and catastrophic to miss: two legs that both back the same team
        # lose the entire bankroll together" — measured here at a $110 swing
        # per $100 of credit against a printed floor of +$57.62.
        outliers = {source for source in _fixture_outliers(rows) if source in best}
        # A book whose own complete market prices below 1.0 has been mispaired
        # by the parser.  Judged on quoted prices, as the detector judges it:
        # the question is whether the parse paired them correctly, and
        # commission is not part of that.  Left in, it inflated a printed
        # conversion from ~44% to 58%.
        self_crossed: set[str] = set()
        for source in sorted(best):
            shape = shapes.get(source) or frozenset()
            selections = best[source]
            if not shape or not set(selections) >= set(shape):
                continue
            if crossed_against_itself(
                [selections[selection] for selection in shape],
                context.commissions,
                order_driven=source in _ORDER_DRIVEN,
            ):
                self_crossed.add(source)
        for source in outliers | self_crossed:
            best.pop(source, None)
        for reason, sources in (
            ("legs_disagree_on_the_game", outliers),
            ("source_prices_itself_to_lose", self_crossed),
        ):
            if not sources:
                continue
            by_source = context.excluded_groups.setdefault(reason, {})
            for source in sources:
                by_source.setdefault(source, set()).add(key)

        view = _GroupView(
            key=key,
            sport=sport,
            market=market,
            rows=tuple(rows),
            best={source: dict(table) for source, table in best.items()},
            shapes=shapes,
            faulted=frozenset(faulted),
            commence=commence,
        )
        context.groups[key] = view
        for source in best:
            context.groups_by_source.setdefault(source, []).append(key)
        for source in faulted:
            context.faulted_by_source.setdefault(source, []).append(key)
        # A book that priced this market but had every row suspended never
        # enters ``best``, so it looks identical to a book that was not
        # collected.  "Nothing you can take right now" and "we never saw this
        # book" are different facts and get different sentences.
        suspended = context.dropped_groups.setdefault("no_active_price", {})
        excluded_here = faulted | outliers | self_crossed
        for source in rows_by_source:
            if source not in best and source not in excluded_here:
                suspended.setdefault(source, set()).add(key)

    return context


# ── stake solving and outcome evaluation ─────────────────────────────────────


@dataclass(frozen=True)
class _Leg:
    quote: Quote
    stake: float
    net_odds: float
    role: str  # "promo" | "hedge"
    mode: str  # MODE_BONUS | MODE_CASH | MODE_BOOSTED


def _bonus_cash(result: str, net_odds: float) -> float:
    """Cash a stake-not-returned credit pays under *result*, per unit of credit.

    A push hands the credit back rather than converting it, so it contributes
    zero cash here; the plan notes that the play simply re-runs.  A half-lose
    returns nothing: half the credit is consumed losing and the refunded half
    is credit again, not cash.
    """
    if result == WIN:
        return net_odds - 1.0
    if result == HALF_WIN:
        return (net_odds - 1.0) / 2.0
    return 0.0


def _outcome_profits(
    view: _GroupView,
    legs: Sequence[_Leg],
    *,
    shape: frozenset[Selection],
    refund_rate: float = 0.0,
    refund_base: float = 0.0,
) -> list[tuple[str, float]]:
    """Cash profit in every settlement outcome, from the rounded stakes.

    *refund_rate* / *refund_base* implement no-sweat refunds: when the promo
    leg outright loses, ``refund_rate * refund_base`` of credit value is added
    (the refund arrives as credit; its cash value is the measured conversion
    rate).  A half-lose refunds nothing — books split quarter-line stakes into
    two bets and refund policies on the surviving half vary, so crediting it
    would overstate the floor.
    """
    event_key, market, period, _, line = view.key
    outcomes = settlement_outcomes(view.sport, market, period, line, shape)
    rows: list[tuple[str, float]] = []
    for label, results in outcomes:
        total = 0.0
        for leg in legs:
            result = results[leg.quote.selection]
            if leg.mode == MODE_BONUS:
                total += leg.stake * _bonus_cash(result, leg.net_odds)
            else:
                total += leg.stake * _return_multiplier(result, leg.net_odds) - leg.stake
            if leg.role == "promo" and result == LOSE and refund_rate > 0.0:
                total += refund_rate * refund_base
        rows.append((label, total))
    return rows


def _settled_floor(promo_selection: Selection, outcome_profits: Sequence[tuple[str, float]],
                   view: _GroupView, shape: frozenset[Selection]) -> float:
    """Worst outcome in which the promo leg actually settles.

    A push voids the promo leg — a bonus bet is handed back, a cash stake is
    refunded — so the play re-runs rather than converting.  Ranking plans by
    the all-outcomes floor would score every pushable market as zero and hide
    the real ordering; the all-outcomes floor is still reported separately as
    ``guaranteed_cash``.
    """
    event_key, market, period, _, line = view.key
    outcomes = settlement_outcomes(view.sport, market, period, line, shape)
    settled: list[float] = []
    for (_, results), (_, profit) in zip(outcomes, outcome_profits):
        if results[promo_selection] == PUSH:
            continue
        settled.append(profit)
    return min(settled) if settled else min(p for _, p in outcome_profits)


def _round_cents(value: float) -> float:
    return round(value + _EPSILON, 2)


# ── the scan ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Candidate:
    view: _GroupView
    promo_leg: _Leg
    hedge_legs: tuple[_Leg, ...]
    outcome_profits: tuple[tuple[str, float], ...]
    floor: float
    settled_floor: float
    metric: float
    notes: tuple[str, ...]

    @property
    def legs(self) -> tuple[_Leg, ...]:
        return (self.promo_leg, *self.hedge_legs)


def _scan(
    context: _PlanContext,
    promo_keys: Sequence[str],
    *,
    mode: str,
    stake: float,
    min_promo_decimal: float | None = None,
    boost_percent: float | None = None,
    refund_rate: float = 0.0,
    skipped: Counter,
    stake_is_provisional: bool = False,
) -> list[_Candidate]:
    """Best hedged executions for a fixed promo book across the whole slate.

    *stake* is the promo-side amount: credit for ``bonus`` mode, cash for
    ``cash``/``boosted``.  Hedge stakes are solved per candidate to equalise
    the settled outcomes, then rounded to cents, then the floor is re-measured
    from the rounded values.

    Set *stake_is_provisional* when *stake* is a unit of account that a later
    ``_rescale`` replaces with the offer's real size — the shared conversion
    scan is the only such caller.  It suppresses the published-limit gate,
    which would otherwise measure a stake nobody places; ``_rescale`` applies
    that gate at the size actually staked, for every scale including 1.0.
    """
    # A market where the promo book was excluded for a parse fault is counted
    # once, whether or not the book still priced other markets.  This runs over
    # ``promo_keys`` rather than the priced subset so that a book whose *every*
    # market faulted still reports the fault instead of vanishing.
    faulted_groups = {
        key for source in promo_keys for key in context.faulted_by_source.get(source, ())
    }
    if faulted_groups:
        skipped["duplicate_selection"] = max(
            skipped.get("duplicate_selection", 0), len(faulted_groups)
        )
    # Groups dropped before any book was indexed — a started game, a contested
    # sport, an unmodelled line — counted for this book too.  Without them a
    # book that is half stale reports fewer refusals than it had, and the
    # caveat pointing at these counts explains less than it claims to.
    #
    # Summed over the *union of groups*, not maxed over the feeds.  A brand's
    # two feeds price overlapping but different slates, so ``max`` under-counts
    # by everything only the smaller feed saw — on live data 54 reported
    # against 82 real.  Counting per group is the same dedup the faulted
    # counters use, and is right whether the feeds overlap fully or not at all.
    _merge_counts(skipped, _dropped_over(context, promo_keys))
    promo_set = [key for key in promo_keys if key in context.groups_by_source]
    if not promo_set:
        return []

    group_keys: list[MarketGroup] = sorted(
        {key for source in promo_set for key in context.groups_by_source[source]},
        key=str,
    )

    # Books thrown out of the markets this one plays in.  Counted per market:
    # the exclusion is a fact about the market, not about the promo book, and
    # it is why an otherwise-hedgeable game came back with no hedge.
    played = set(group_keys) | {
        key for source in promo_keys for key in context.faulted_by_source.get(source, ())
    }
    for reason, by_source in context.excluded_groups.items():
        hit = len({
            key for groups in by_source.values() for key in groups if key in played
        })
        if hit:
            skipped[reason] = max(skipped.get(reason, 0), hit)

    candidates: list[_Candidate] = []
    for key in group_keys:
        view = context.groups[key]
        _, market, period, _, line = key

        behind = _counterparties(view.rows, context.one_counterparty)
        # Counted once per (book, selection) for this market rather than once
        # per promo side considered — so a stale book on a three-way market is
        # three refusals, one per price it offered, not the nine that counting
        # per promo side produced.
        stale_here: set[tuple[str, Selection]] = set()

        # The promo book's contract.  In a window where books price the draw, a
        # two-way moneyline is ambiguous about ties — the arb detector refuses
        # it, and a promo plan built on it would be a guess about the whole
        # stake, so it is refused here for the same reason.
        promo_rows: dict[Selection, tuple[str, Quote]] = {}
        promo_shape: frozenset[Selection] | None = None
        for source in promo_set:
            table = view.best.get(source)
            if not table:
                continue
            shape = view.shapes[source]
            if (
                market is Market.MONEYLINE
                and draw_is_priced(view.sport, period)
                and Selection.DRAW not in shape
            ):
                _record_refused_market(skipped, "ambiguous_tie_settlement", view.key)
                continue
            if promo_shape is None:
                promo_shape = shape
            elif shape != promo_shape:
                # The brand's two feeds disagree about the contract, which by
                # the same derivation as above means one of them does not price
                # the draw — already refused by the check above, so this is
                # belt-and-braces rather than a distinct reason.
                #
                # Currently unreachable, and deliberately kept.  ``shapes`` is
                # built from the group's single sport/market/period, so the only
                # per-source variation ``arb.contract_shape`` can produce is a
                # moneyline in a draw window, which the check above already
                # removes.  That invariant lives in *another module*: were
                # ``contract_shape`` to gain a second axis of variation, this
                # branch is what stops two feeds pricing different contracts
                # from being joined, and its absence would be a silently wrong
                # plan rather than a loud failure.  A local dead guard gets
                # deleted; one whose premise is imported does not.
                _record_refused_market(skipped, "ambiguous_tie_settlement", view.key)
                continue
            for selection, quote in table.items():
                promo_rows.setdefault(selection, (source, quote))
        if promo_shape is None or not promo_rows:
            continue

        # Hedge tables: every other book pricing the same contract, same
        # settlement regime as the promo book, best net odds first.
        promo_sources_here = {source for source, _ in promo_rows.values()}
        promo_regime = regime_for(next(iter(promo_sources_here)))
        hedge_tables: dict[Selection, list[tuple[str, Quote]]] = defaultdict(list)
        for source, table in view.best.items():
            if source in promo_set:
                continue
            shape = view.shapes[source]
            if shape != promo_shape:
                # Within one group the market is fixed, and ``contract_shape``
                # varies only by whether the draw is a priced selection — so a
                # differing shape here *is* the ambiguous-tie case, always.  A
                # second "shape_mismatch" reason used to sit in the else, where
                # it could never increment: a gate the reader believes exists
                # and the counts can never report.
                _record_refused_market(skipped, "ambiguous_tie_settlement", view.key)
                continue
            if regime_for(source) is not promo_regime:
                _record_refused_market(skipped, "regime_mismatch", view.key)
                continue
            for selection, quote in table.items():
                hedge_tables[selection].append((source, quote))
        for selection in hedge_tables:
            hedge_tables[selection].sort(
                key=lambda item: (
                    -net_decimal(item[1], context.commissions),
                    item[0],
                )
            )

        # Try every promo-side selection: conversion quality depends on the
        # promo leg's own odds, and the best side is not knowable in advance.
        for promo_selection in sorted(promo_rows, key=lambda s: s.value):
            promo_source, promo_quote = promo_rows[promo_selection]
            if (
                min_promo_decimal is not None
                and promo_quote.decimal_odds < min_promo_decimal - _EPSILON
            ):
                _record_refused_market(skipped, "below_min_odds", view.key)
                continue

            hedge_selections = sorted(promo_shape - {promo_selection}, key=lambda s: s.value)
            # An assertion, not a bare ``continue``.  ``arb.contract_shape``
            # returns two or three selections for every (sport, market, period),
            # so removing the promo side always leaves at least one — the branch
            # was unreachable, uncounted, and therefore invisible to the "every
            # dropped record is counted" rule.  It still guards something real:
            # an empty pool list makes ``any(not pool for pool in pools)`` false
            # and ``_product([])`` yield one empty combination, so the market
            # would plan with *no hedge legs at all* and print an unhedged stake
            # as a guarantee.  Loud beats silent for that.
            assert hedge_selections, f"contract shape {promo_shape} has no hedge side"
            # Gate **before** truncating, not after.  Sources that can never be
            # a hedge for this promo leg — the promo book's own counterparty,
            # its declared failover feed — are removed first, so the
            # best-N window holds N *usable* prices.  Truncating first let four
            # measured mirrors of the promo book, all quoting better than the
            # one real hedge on the board, fill the window and push the genuine
            # $66.66-floor plan out of consideration; the offer then reported
            # zero plans and blamed ``same_counterparty`` for all four.
            promo_who = behind.get(promo_source, promo_source)
            pools = []
            for selection in hedge_selections:
                usable = []
                for source, quote in hedge_tables.get(selection, []):
                    if (
                        behind.get(source, source) == promo_who
                        or is_redundant_pair(source, promo_source)
                    ):
                        _record_refused_market(skipped, "same_counterparty", view.key)
                        continue
                    # Freshness is a per-source fact once the promo leg is
                    # fixed, so it belongs here with the other pre-filters.
                    # Left to the combo loop it let four stale-but-better books
                    # fill the four-deep window and hide a viable +$56.34 plan,
                    # reporting only ``observation_spread ×4`` — the same
                    # truncate-before-gating defect round 3 fixed for
                    # counterparties, on the neighbouring gate.
                    if abs(quote.observed_at - promo_quote.observed_at) > MAX_OBSERVATION_SPREAD:
                        stale_here.add((source, selection))
                        continue
                    usable.append((source, quote))
                window = usable[:_HEDGE_CANDIDATES_PER_SELECTION]
                if usable:
                    # Price alone can fill the window with rows that each pair
                    # with the promo leg and not with each other; the
                    # combination gate then rejects every one and reports only
                    # ``observation_spread``, while a fully simultaneous plan
                    # sat one row outside the window.
                    #
                    # Both ends, not just the freshest.  Every usable row is
                    # already within ``MAX_OBSERVATION_SPREAD`` of the *promo*
                    # leg — the pairing that fails is hedge-to-hedge, which can
                    # be twice that apart — so the row that rescues the
                    # combination is as often the older one as the newer.
                    # Reaching for the newest alone made the rescue work in one
                    # time direction only: on a board mirrored in time and
                    # otherwise identical, the plan was found at +$48.38 one way
                    # round and missed the other, which left scrape order
                    # deciding which plans exist and blamed
                    # ``observation_spread`` for the ones it dropped.
                    by_time = sorted(usable, key=lambda item: item[1].observed_at)
                    for edge in (by_time[0], by_time[-1]):
                        if edge not in window:
                            window = [*window, edge]
                pools.append(window)
            if any(not pool for pool in pools):
                _record_refused_market(skipped, "no_hedge_price", view.key)
                continue

            ranked: list[_Candidate] = []
            for combo in _product(pools):
                # Hedges at the promo book's own counterparty are already out —
                # that test is per-source and was applied when the pools were
                # built.  This one cannot be: it is about the *combination*.
                #
                # Across **every** leg including hedge-vs-hedge: a
                # position may not take both feeds of one failover pair.  The
                # first gate does not imply this one — the stitch that matters
                # is between two *hedges* on a three-way market, where neither
                # is at the promo book: an_betmgm's away price and betmgm's
                # home price are one book seen twice, so the two can only
                # differ by one of them being stale, and no operator can place
                # both.  Taking them anyway invents a synthetic book whose
                # printed floor is unreachable — measured at $109.52 against an
                # executable $98.41, and settling $40 under the printed
                # guarantee when the stale feed is the generous one.  This is
                # the discipline ``src.arb._uses_both_failover_feeds`` enforces
                # on every arb position, applied here to every plan.
                sources = [promo_source, *(source for source, _ in combo)]
                if _uses_both_failover_feeds(sources):
                    _record_refused_market(skipped, "both_failover_feeds", view.key)
                    continue
                legs_quotes = [promo_quote, *(quote for _, quote in combo)]
                observed = [quote.observed_at for quote in legs_quotes]
                if max(observed) - min(observed) > MAX_OBSERVATION_SPREAD:
                    _record_refused_market(skipped, "observation_spread", view.key)
                    continue

                promo_net = net_decimal(promo_quote, context.commissions)
                hedge_nets = [net_decimal(quote, context.commissions) for _, quote in combo]

                # Sanity: at cash prices, a margin past REFUSE_MARGIN is a
                # stale quote or a palpable error, not a plan.
                cash_sum = 1.0 / promo_net + sum(1.0 / net for net in hedge_nets)
                if cash_sum < 1.0 - REFUSE_MARGIN:
                    _record_refused_market(skipped, "implausible_price", view.key)
                    continue

                candidate = _solve(
                    view,
                    promo_selection=promo_selection,
                    promo_quote=promo_quote,
                    promo_net=promo_net,
                    combo=combo,
                    hedge_nets=hedge_nets,
                    shape=promo_shape,
                    mode=mode,
                    stake=stake,
                    boost_percent=boost_percent,
                    refund_rate=refund_rate,
                    refusals=skipped,
                    stake_is_provisional=stake_is_provisional,
                )
                if candidate is None:
                    continue
                ranked.append(candidate)
            if ranked:
                ranked.sort(key=lambda c: -c.metric)
                # One winner per market when the stake is real, because the
                # limit gate has already run inside ``_solve`` and every
                # candidate here is placeable.  When it is provisional that gate
                # is deferred to ``_rescale``, so the best-priced assignment can
                # be one the offer's real size cannot place — and keeping it
                # alone let it *shadow* a legal runner-up on the same market,
                # which ``_rescale`` then deleted along with the market.
                # Measured: a $50-limit hedge at 1.55 beat a six-figure-limit
                # hedge at 1.50, and a $150 offer printed zero plans and "no
                # hedgeable market passed every gate" over a placeable +$100.
                keep = _FALLBACKS_PER_MARKET if stake_is_provisional else 1
                candidates.extend(ranked[:keep])
        if stale_here:
            # The market, not the (source, selection) pairs: this tally sits
            # in the same map as every other reason and must carry the same
            # unit, or a three-way board reports more refusals than it has
            # prices.
            _record_refused_market(skipped, "observation_spread", view.key)

    candidates.sort(
        key=lambda c: (
            -c.metric,
            c.view.key[0],
            c.view.market.value,
            c.view.key[2].value,
            str(c.view.key[4] if c.view.key[4] is not None else ""),
            c.promo_leg.quote.selection.value,
            c.promo_leg.quote.source,
        )
    )
    return candidates


def _product(pools: Sequence[Sequence[tuple[str, Quote]]]) -> Iterable[tuple[tuple[str, Quote], ...]]:
    if not pools:
        return
    if len(pools) == 1:
        for item in pools[0]:
            yield (item,)
        return
    for item in pools[0]:
        for rest in _product(pools[1:]):
            yield (item, *rest)


def _solve(
    view: _GroupView,
    *,
    promo_selection: Selection,
    promo_quote: Quote,
    promo_net: float,
    combo: Sequence[tuple[str, Quote]],
    hedge_nets: Sequence[float],
    shape: frozenset[Selection],
    mode: str,
    stake: float,
    boost_percent: float | None,
    refund_rate: float,
    refusals: Counter,
    stake_is_provisional: bool = False,
) -> _Candidate | None:
    """Solve hedge stakes for one assignment and price every outcome.

    Every mode reduces to the same shape: fix a target return ``T`` that the
    position collects in each settled outcome, put ``T / net_odds`` on each
    hedge, and the settled outcomes equalise exactly (the derivations are in
    the mode branches).  Stakes are then rounded to cents and the floor is
    re-measured from the rounded values, so the printed numbers are the
    guaranteed ones.
    """
    notes: list[str] = []
    refund_base = 0.0
    if mode == MODE_BONUS:
        # Promo leg wins → cash (n*-1)·B with no cash outlay.  Hedge s wins →
        # cash h_s·n_s − Σh.  Setting h_s·n_s = (n*−1)·B makes every settled
        # outcome pay (n*−1)·B − Σh.
        target = (promo_net - 1.0) * stake
        promo_leg = _Leg(promo_quote, _round_cents(stake), promo_net, "promo", MODE_BONUS)
    elif mode == MODE_BOOSTED:
        # Not a counted refusal: ``boost_percent_unknown`` could never appear in
        # ``skipped``, so it read as a live refusal path that no board could
        # reach.  Both callers of this mode supply a number — ``_plan_boost``'s
        # locked branch runs under ``if boost_pct is not None``, and its
        # breakeven re-solve passes a computed float — and both are in this
        # file, so the invariant is local and an assertion states it where the
        # dead reason string only obscured it.
        assert boost_percent is not None, "boosted mode needs a boost percentage"
        boosted_net = 1.0 + (promo_net - 1.0) * (1.0 + boost_percent / 100.0)
        # Cash stake S at boosted odds n_b: wins collect S·n_b (stake back plus
        # boosted winnings).  h_s·n_s = S·n_b equalises the settled outcomes.
        target = stake * boosted_net
        promo_leg = _Leg(promo_quote, _round_cents(stake), boosted_net, "promo", MODE_BOOSTED)
        notes.append(
            f"priced with the {round(boost_percent, 2):g}% boost applied to the "
            f"quoted {promo_quote.decimal_odds:.2f}"
        )
    elif mode == MODE_CASH:
        if refund_rate > 0.0:
            # No-sweat: a losing promo stake comes back as credit worth
            # ``refund_rate`` on the dollar.  Promo wins → S·n* − S − Σh; hedge
            # wins → h·n − Σh − S + r·S.  Setting h_s·n_s = S·(n* − r) makes the
            # two identical, for any number of hedge legs.
            target = stake * (promo_net - refund_rate)
            refund_base = stake
            notes.append(
                f"a losing stake refunds as credit, valued at its measured "
                f"{refund_rate * 100.0:.1f}% conversion"
            )
        else:
            target = stake * promo_net
        promo_leg = _Leg(promo_quote, _round_cents(stake), promo_net, "promo", MODE_CASH)
    else:  # pragma: no cover - modes are module constants
        raise ValueError(f"unknown plan mode {mode!r}")

    hedge_legs = tuple(
        _Leg(quote, _round_cents(target / net), net, "hedge", MODE_CASH)
        for (source, quote), net in zip(combo, hedge_nets)
    )
    if any(leg.stake <= 0 for leg in hedge_legs):
        _record_refused_market(refusals, "hedge_stake_rounds_to_zero", view.key)
        return None
    # A leg above the size the venue publishes cannot be placed as printed, so
    # the floor computed from it is not a floor.  The arb detector caps a
    # position at the stated limits (``_fits`` / ``max_total_stake``); here the
    # promo stake is fixed by the offer — you cannot part-use a bonus bet — so
    # the honest answer is to refuse the assignment and count it.  Measured on
    # live data: a $481.44 "worst case" resting on a $1018.56 leg at a book
    # advertising $40.
    legs = (promo_leg, *hedge_legs)
    # Every leg, not just the hedges: the promo stake scales with the offer
    # amount exactly as a hedge's does, so a $250 bonus printed a guarantee
    # resting on a $250 leg at a book advertising $40 — the mirror of the
    # defect this gate was added for, left un-mirrored for three rounds.
    #
    # Skipped outright when the stake is provisional.  The conversion scan is
    # solved once per slate at ``PLAN_UNIT`` and reused for every offer on the
    # book, so its stakes are a unit of account, not stakes anyone places;
    # measuring them against a venue's limit refused markets that fit the real
    # offer.  A $25 bonus behind a $20 hedge limit lost its only plan — the
    # printed hedge would have been $7.76 — to a refusal counted against a
    # $31.05 leg that exists nowhere, under a caveat saying nothing passed
    # every gate.  ``_rescale`` applies the gate at the size actually staked.
    if not stake_is_provisional and _over_stated_limit(legs):
        _record_refused_market(refusals, "stake_over_stated_limit", view.key)
        return None
    profits = _outcome_profits(
        view,
        legs,
        shape=shape,
        refund_rate=refund_rate,
        refund_base=refund_base,
    )
    rounded = tuple((label, round(profit, 2) or 0.0) for label, profit in profits)
    floor = min(profit for _, profit in rounded)
    settled = _settled_floor(promo_selection, rounded, view, shape)
    return _Candidate(
        view=view,
        promo_leg=promo_leg,
        hedge_legs=hedge_legs,
        outcome_profits=rounded,
        floor=floor,
        settled_floor=settled,
        metric=settled,
        notes=tuple(notes),
    )


# ── plan payloads ────────────────────────────────────────────────────────────


def _candidate_payload(
    candidate: _Candidate, *, as_of: datetime, state: str | None = None
) -> dict[str, Any]:
    view = candidate.view
    event_key, market, period, side, line = view.key
    reference = candidate.promo_leg.quote
    oldest = min(leg.quote.observed_at for leg in candidate.legs)
    return {
        "event_key": event_key,
        "sport": view.sport.value,
        "league": reference.league,
        "home_team": reference.home_team,
        "away_team": reference.away_team,
        "commence_time": view.commence.isoformat(),
        "market": market.value,
        "period": period.value,
        "side": side.value if side else None,
        "line": line,
        "legs": [
            {
                "role": leg.role,
                "source": leg.quote.source,
                "selection": leg.quote.selection.value,
                # The line as printed on this book's own row — for a spread the
                # away side's sign differs from the group's canonical line, and
                # the page must render what the book shows, not re-derive it.
                "line": leg.quote.line,
                "decimal_odds": round(leg.quote.decimal_odds, 4),
                "american_odds": leg.quote.american_odds,
                "net_odds": round(leg.net_odds, 4),
                "stake": leg.stake,
                "stake_kind": leg.mode,
                "is_alternate": leg.quote.is_alternate,
                "observed_at": leg.quote.observed_at.isoformat(),
                # Where this leg gets placed.  A plan that names a promo, a book
                # and a stake but leaves the reader to find the game is a
                # calculation, not an instruction.  The plan's state picks a
                # state-partitioned book's own front door.
                "link": link_payload(leg.quote, state=state),
            }
            for leg in candidate.legs
        ],
        "outcome_profits": [[label, profit] for label, profit in candidate.outcome_profits],
        "guaranteed_cash": round(candidate.floor or 0.0, 2),
        "settled_cash": round(candidate.settled_floor or 0.0, 2),
        "quote_age_seconds": max(0, int((as_of - oldest).total_seconds())),
        "notes": list(candidate.notes),
    }


def _offer_view(offer: Any) -> dict[str, Any]:
    """Row dicts from :class:`PromoStore` and :class:`PromoOffer` models alike."""
    if isinstance(offer, Mapping):
        return dict(offer)
    dump = offer.model_dump()
    dump["kind"] = offer.kind.value
    return dump


def _offer_ended(view: Mapping[str, Any], as_of: datetime) -> datetime | None:
    """The offer's stated end, when it has already passed — else ``None``.

    Store rows carry ISO strings, models carry datetimes; both are accepted and
    an unparseable or absent value reads as "no stated end", which is the
    permissive answer — an offer is refused for being *ended*, never for being
    vague about it.  Naive timestamps are read as UTC, matching the store's own
    ``_iso`` convention.
    """
    raw = view.get("ends_at")
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        ends = raw
    else:
        try:
            ends = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=as_of.tzinfo)
    return ends if ends < as_of else None


def _plan_for_offer(
    view: dict[str, Any],
    context: _PlanContext,
    *,
    conversion_cache: dict[tuple[tuple[str, ...], float | None], tuple[list[_Candidate], Counter]],
) -> dict[str, Any]:
    source = str(view.get("source") or "")
    kind_raw = str(view.get("kind") or "other")
    try:
        kind = PromoKind(kind_raw)
    except ValueError:
        kind = PromoKind.OTHER
    reward = str(view.get("reward_type") or "")
    summary = str(view.get("summary") or "")
    title = str(view.get("title") or "")
    # What the win/loss condition classifiers read.  The enricher's canonical
    # summary drops trailing clauses — TheLines' "Bet $10 get $150 in bonus
    # bets if you win" is summarised without the condition — so the title rides
    # along.  The *description* deliberately does not: TheLines describes two
    # different BetMGM promos in one blob, and the other promo's "if you don't
    # win" would read this one as insurance.
    condition_text = f"{summary} {title}".strip()

    promo_keys = stakeable_odds_sources(source)
    out: dict[str, Any] = {
        "strategy": "text_only",
        "book": list(promo_keys),
        "plans": [],
        "skipped": {},
        "caveats": [],
        "unit": None,
    }

    # A non-sportsbook product's credit cannot be staked on this slate: the
    # poker welcome bonus planned a 74% "conversion" of poker-room deposit
    # credit onto soccer moneylines, which is not executable.  Named and
    # left unplanned rather than silently dropped.
    product = str(view.get("product") or "").strip().lower()
    if product and product not in {"sportsbook", "sports", "predict"}:
        out["strategy"] = "text_only"
        out["skipped"] = {"non_sportsbook_product": 1}
        out["caveats"].append(
            f"this is a {product} promotion — its credit cannot be staked on "
            "the sportsbook slate, so no plan is computed"
        )
        return out

    # An offer whose own stated end has passed gets no concrete plan.  The
    # planner used to ignore ends_at entirely, so yesterday's boost planned
    # exactly like a live one — stakes, links and all.  The entry stays in the
    # payload (one entry per offer is the convention) with the reason counted.
    ended = _offer_ended(view, context.as_of)
    if ended is not None:
        out["strategy"] = "text_only"
        out["skipped"] = {"expired": 1}
        out["caveats"].append(
            f"offer ended {ended.date().isoformat()} — nothing here is placeable; "
            "re-collect promos before acting"
        )
        return out

    # The state verdict rides the offer row (stamped by the collector); an
    # unconfirmed offer still plans — the label is the safeguard, not a drop —
    # but every card carries the warning up front, because the first live run
    # showed the failure is real: the one "confirmed" national offer owed its
    # stamp to another offer's state list sharing its description.
    if context.state and not bool(view.get("state_confirmed", False)):
        out["caveats"].append(
            f"eligibility for {context.state} is not confirmed from the venue's "
            "own copy — verify in the app before staking anything"
        )

    if not promo_keys:
        out["strategy"] = "no_odds_coverage"
        out["caveats"].append(
            "no odds feed covers this book, so no concrete games can be named; "
            "the generic playbook below still applies"
        )
        return out
    if not any(key in context.groups_by_source for key in promo_keys):
        out["strategy"] = "no_odds_coverage"
        # Deduped across the brand's feeds, matching how ``_scan`` counts them:
        # a market faulted at both the first-party feed and its failover view is
        # one market, and summing the per-source lists reported it as two.
        faulted = len({
            key for source_key in promo_keys
            for key in context.faulted_by_source.get(source_key, ())
        })
        dropped = _dropped_over(context, promo_keys)
        # This book's *own* exclusions only.  Counting every market where any
        # book was thrown out attributed other venues' parse faults to a book
        # that never priced those games — and in this branch it has no markets
        # of its own, so the number was pure noise.
        for reason, by_source in context.excluded_groups.items():
            mine = {
                key for source_key in promo_keys
                for key in by_source.get(source_key, ())
            }
            if mine:
                dropped[reason] = max(dropped.get(reason, 0), len(mine))
        if faulted:
            # The book *did* produce rows; every one of its markets was thrown
            # out for publishing two prices for one selection.  Saying "no rows"
            # here points the operator at the collector when the fault is in
            # this book's parse.
            out["skipped"] = {"duplicate_selection": faulted}
            out["caveats"].append(
                f"this book priced {faulted} market(s) in the latest collection "
                "and every one was excluded for publishing two prices for the "
                "same selection — a parser fault, not a quiet book; no game can "
                "be named until it is fixed"
            )
        elif dropped.get("no_active_price") and len(dropped) == 1:
            count = dropped["no_active_price"]
            out["skipped"] = dict(sorted(dropped.items()))
            out["caveats"].append(
                f"this book priced {count} market(s) in the latest collection "
                "and every price was suspended — there is nothing takeable to "
                "plan with right now, which is not the same as the book being "
                "missing"
            )
        elif dropped.get("already_started") and len(dropped) == 1:
            # The commonest case by far on a page built some hours after its
            # scrape.  Reporting it as "no rows" sent the reader to the
            # collector for a run that is simply old.  Guarded on being the
            # *only* reason, because "every one of this book's 5" was printed
            # beside a skipped map reading ``already_started 5, no_active_price
            # 2`` — a sentence contradicting its own evidence.  Mixed causes
            # fall through to the enumerating branch below.
            count = dropped["already_started"]
            out["skipped"] = dict(sorted(dropped.items()))
            out["caveats"].append(
                f"every one of this book's {count} priced market(s) in the "
                "latest collection is on a game that has already started — the "
                "run is too old to plan from; collect again"
            )
        elif dropped:
            out["skipped"] = dict(sorted(dropped.items()))
            reasons = ", ".join(
                f"{reason.replace('_', ' ')} ×{count}"
                for reason, count in sorted(dropped.items())
            )
            out["caveats"].append(
                "this book's markets in the latest collection were all set "
                f"aside before pricing ({reasons}), so no game can be named"
            )
        else:
            out["caveats"].append(
                "the latest odds collection has no rows from this book, so no "
                "concrete games can be named"
            )
        return out
    if promo_keys[0].startswith("an_") or promo_keys[0] not in context.groups_by_source:
        # Either the brand has no first-party feed at all, or this run's
        # first-party rows are missing and only the failover view priced it.
        out["caveats"].append(
            "prices come from Action Network's view of this book — verify on "
            "the book before staking"
        )

    min_dec = parse_min_odds(view.get("min_odds"))

    skipped: Counter = Counter()

    def conversions(min_decimal: float | None) -> list[_Candidate]:
        """Per-$100 credit conversions for this book, computed once per slate.

        The cache holds the scan's gate counts alongside its candidates and
        replays them into this offer's counter.  Caching only the candidates
        left every offer after the first with an empty ``skipped`` map printed
        beside a caveat that says those counts explain the refusal — and which
        offer got the real counts depended on the order the offers arrived in.
        """
        cache_key = (promo_keys, min_decimal)
        if cache_key not in conversion_cache:
            counts: Counter = Counter()
            found = _scan(
                context,
                promo_keys,
                mode=MODE_BONUS,
                stake=PLAN_UNIT,
                min_promo_decimal=min_decimal,
                skipped=counts,
                # This scan is cached across every offer on the book, so its
                # stakes cannot be measured against a venue limit — each offer
                # rescales them to its own size and is gated there.
                stake_is_provisional=True,
            )
            conversion_cache[cache_key] = (found, counts)
        found, counts = conversion_cache[cache_key]
        _merge_counts(skipped, counts)
        return found

    bonus_amount = view.get("bonus_amount")
    qualifying = parse_qualifying_stake(summary)
    boost_pct = parse_boost_percent(title, summary, str(view.get("description") or ""))
    wagering = parse_wagering_multiple(view.get("wagering_requirement"))

    bonus_like = kind in {PromoKind.BONUS_BET, PromoKind.FREE_BET, PromoKind.SIGNUP_BONUS} or (
        reward in {"bonus_bets", "free_bet"}
    )

    # A boost *kind* whose reward is credit is a bet-and-get wearing a boost
    # label, and the boost branch priced it as one: a live $300 bonus-bet drop
    # was presented as a boost hunt with a −$1.95 worst case and no mention of
    # the $300, and a $250 *credit* became "risk $250 of your own money".  Five
    # of sixty live offers carry this shape.  Reward wins when it names a credit
    # type, since that is the concrete thing the operator receives.
    credit_reward = reward in {"bonus_bets", "free_bet", "site_credit"}
    # Only true stake-not-returned credit converts.  Site credit is cash that
    # needs clearing, and ``_plan_rollover`` two branches below is where the
    # rest of the dispatch sends it.
    parlay_credit = kind is PromoKind.PARLAY_BOOST and reward in {
        "bonus_bets",
        "free_bet",
    }

    if kind is PromoKind.PARLAY_BOOST and not credit_reward:
        # Genuinely unpriceable here, and now enforced rather than asserted in a
        # comment: the previous dispatch let a parlay boost whose reward_type
        # read ``bonus_bets`` fall through to conversion and print a card
        # telling the operator to hedge a single moneyline — priced as though
        # the promo were a bonus bet.  Correlated parlay legs are not modelled
        # anywhere in this codebase, and inventing a settlement model for them
        # is how a phantom guarantee gets published.
        out["caveats"].append(
            "a parlay boost pays on correlated legs, which this planner does "
            "not model — no hedge is computed and the playbook below stands"
        )
    elif parlay_credit:
        # The qualifying parlay is not modelled, but the credit it pays is an
        # ordinary bonus bet once it lands.  Price the conversion only, and say
        # which half is missing rather than discarding both.
        out["caveats"].append(
            "the qualifying leg is a parlay, which this planner does not "
            "model — only the conversion of the credit it pays is priced below"
        )
        _plan_conversion(out, skipped, conversions, bonus_amount, min_dec, context)
    elif (
        kind in {PromoKind.ODDS_BOOST, PromoKind.PROFIT_BOOST} and not credit_reward
    ) or reward == "boost":
        _plan_boost(out, context, promo_keys, skipped, boost_pct, bonus_amount, min_dec)
    elif (
        kind in {PromoKind.NO_SWEAT, PromoKind.RISK_FREE}
        or reward in {"no_sweat", "risk_free"}
        # Copy whose reward is conditional on losing *is* a no-sweat, whatever
        # the enricher labelled it.  Priced as a plain bonus it valued credit
        # that arrives only on a loss as credit in hand.
        or _reward_is_loss_contingent(condition_text)
    ):
        # ``risk_free`` is the label the enricher writes for "risk-free bet"
        # copy.  The *kind* was handled and the reward was not, so those offers
        # fell past every branch and printed nothing at all — no plan and no
        # sentence, which is the one outcome this module exists to avoid.
        _plan_no_sweat(out, context, promo_keys, skipped, conversions, bonus_amount, min_dec)
    elif kind is PromoKind.DEPOSIT_MATCH or (
        reward == "site_credit" and (not bonus_like or wagering is not None)
    ):
        if kind is PromoKind.PARLAY_BOOST:
            # Site credit clears by rollover wherever it comes from — but the
            # leg that earns it here is a parlay this planner does not model,
            # and the rollover branch would otherwise assert an unqualified
            # value for a reward contingent on hitting it.
            out["caveats"].append(
                "the credit is earned by a parlay, which this planner does not "
                "model — the clearing cost below assumes the credit arrives"
            )
        _plan_rollover(out, context, promo_keys, skipped, bonus_amount, wagering, min_dec)
    elif bonus_like and reward in {"bonus_bets", "free_bet", "site_credit", ""}:
        if qualifying is not None and bonus_amount:
            _plan_qualify_then_convert(
                out, context, promo_keys, skipped, conversions,
                qualifying=qualifying, bonus_amount=float(bonus_amount), min_dec=min_dec,
            )
        else:
            _plan_conversion(out, skipped, conversions, bonus_amount, min_dec, context)
    elif reward == "cash":
        out["caveats"].append(
            "the reward is cash rather than credit, so there is nothing to "
            "convert — take it and bet it however you like"
        )
    else:
        # Every remaining combination lands here with its reason named.  It used
        # to fall off the end: 19 (kind, reward) pairs the enricher really does
        # produce returned no plan, no caveat and an empty skipped map, which is
        # indistinguishable from a bug.
        label = kind.value.replace("_", " ")
        article = "an" if label[:1] in "aeiou" else "a"
        described = f"{article} {label}"
        if reward:
            described += f" paying {reward.replace('_', ' ')}"
        out["caveats"].append(
            f"this offer reads as {described}, which has no priceable "
            "mechanics to hedge — the playbook below is the whole answer"
        )

    # A win-conditional reward is priced as ordinary credit — which is right,
    # the credit converts the same way once it lands — but it does not always
    # land, and a bet-and-get card otherwise reads as though it does.  The
    # loss-conditional case has a model (`_plan_no_sweat`); this one does not,
    # so the honest move is to price it and say what the price assumes.
    if _reward_is_win_contingent(condition_text) and out["strategy"] in {
        "bonus_conversion",
        "qualify_then_convert",
    }:
        out["caveats"].append(
            "this credit arrives only if the qualifying bet wins, so the "
            "figures below are what it is worth once it does — not an "
            "expected value over both outcomes"
        )

    # Only claimed once a scan has actually applied it.  Printed before the
    # dispatch, it appeared on referral/loyalty/parlay offers that never scan
    # anything — a sentence about a promo-side leg that does not exist, and one
    # the code elsewhere treats as load-bearing.
    if min_dec is not None and out["strategy"] not in {"text_only", "no_odds_coverage"}:
        out["caveats"].insert(
            0,
            f"stated minimum odds {view.get('min_odds')} applied to the promo-side leg",
        )
    out["skipped"] = _settle_counts(skipped)
    if (
        not out["plans"]
        and out["strategy"] not in {"text_only", "no_odds_coverage"}
        # Only when a gate really did refuse something.  A mode that found
        # hedgeable markets and then dropped them all for not clearing a
        # profit says so in its own caveat; pointing at an empty ``skipped``
        # map would be a sentence about counts that are not there.
        and skipped
    ):
        out["caveats"].append(
            "no hedgeable market on this book passed every gate; the counts in "
            "'skipped' say what was refused and why"
        )
    return out


def _plan_conversion(
    out: dict[str, Any],
    skipped: Counter,
    conversions,
    bonus_amount: float | None,
    min_dec: float | None,
    context: _PlanContext,
) -> None:
    out["strategy"] = "bonus_conversion"
    amount = float(bonus_amount) if bonus_amount else PLAN_UNIT
    out["unit"] = {
        "kind": "bonus_credit",
        "amount": amount,
        "assumed": bonus_amount is None,
    }
    if bonus_amount is None:
        out["caveats"].append(
            f"the offer does not state a dollar amount; figures are per ${PLAN_UNIT:g} "
            "of credit and scale linearly"
        )
    priced = conversions(min_dec)
    candidates = [c for c in priced if c.settled_floor > _EPSILON]
    _note_unprofitable(out, priced, candidates)
    scale = amount / PLAN_UNIT
    survivors = _rescale_survivors(candidates, scale, skipped)
    for candidate, scaled in survivors[:MAX_PLANS_PER_OFFER]:
        payload = _candidate_payload(scaled, as_of=context.as_of, state=context.state)
        payload["conversion_pct"] = round(candidate.settled_floor / PLAN_UNIT * 100.0, 2)
        out["plans"].append(payload)
    out["caveats"].append(
        "the credit is treated as a single stake-not-returned bet; a push hands "
        "the credit back and the play re-runs"
    )


def _plan_qualify_then_convert(
    out: dict[str, Any],
    context: _PlanContext,
    promo_keys: Sequence[str],
    skipped: Counter,
    conversions,
    *,
    qualifying: float,
    bonus_amount: float,
    min_dec: float | None,
) -> None:
    out["strategy"] = "qualify_then_convert"
    out["unit"] = {"kind": "bet_and_get", "qualifying": qualifying, "bonus": bonus_amount,
                   "assumed": False}
    qualify = _scan_into(
        skipped, context, promo_keys,
        mode=MODE_CASH, stake=qualifying, min_promo_decimal=min_dec,
    )
    # The stated minimum applies to the conversion leg too — ``_plan_conversion``
    # already passes it, and the caveat this offer prints says the minimum was
    # "applied to the promo-side leg".  Passing ``None`` here printed convert
    # cards whose promo leg sat below the number the caveat claimed to enforce.
    priced = conversions(min_dec)
    conversion = [c for c in priced if c.settled_floor > _EPSILON]
    _note_unprofitable(out, priced, conversion)
    if qualify:
        payload = _candidate_payload(qualify[0], as_of=context.as_of, state=context.state)
        payload["step"] = "qualify"
        # The qualifying round-trip usually costs the vig; its settled floor is
        # that cost, signed.
        payload["qualifying_cost"] = round(-min(qualify[0].settled_floor, 0.0) or 0.0, 2)
        out["plans"].append(payload)
    best_rate = 0.0
    scale = bonus_amount / PLAN_UNIT
    convert_survivors = _rescale_survivors(conversion, scale, skipped)
    room = max(0, MAX_PLANS_PER_OFFER - len(out["plans"]))
    for candidate, scaled in convert_survivors[:room]:
        payload = _candidate_payload(scaled, as_of=context.as_of, state=context.state)
        payload["step"] = "convert"
        payload["conversion_pct"] = round(candidate.settled_floor / PLAN_UNIT * 100.0, 2)
        out["plans"].append(payload)
    if convert_survivors:
        # From the markets that survived, not from the ranked list: the
        # headline used to quote a conversion the operator cannot take.
        best_rate = convert_survivors[0][0].settled_floor / PLAN_UNIT
    if qualify and convert_survivors:
        cost = -min(qualify[0].settled_floor, 0.0) or 0.0
        out["expected_value"] = round(bonus_amount * best_rate - cost, 2)
        out["caveats"].append(
            f"net of the qualifying round-trip: ${bonus_amount:g} of credit at the "
            f"best measured {best_rate * 100.0:.1f}% conversion, minus the "
            f"${cost:.2f} qualifying cost"
        )
    out["caveats"].append(
        "place the qualifying stake first; the conversion legs only exist once "
        "the credit lands, so re-check prices then"
    )


def _plan_no_sweat(
    out: dict[str, Any],
    context: _PlanContext,
    promo_keys: Sequence[str],
    skipped: Counter,
    conversions,
    bonus_amount: float | None,
    min_dec: float | None,
) -> None:
    conversion = [c for c in conversions(min_dec) if c.settled_floor > _EPSILON]
    if not conversion:
        out["strategy"] = "text_only"
        out["caveats"].append(
            "no measurable credit conversion on this book right now, so the "
            "refund cannot be valued; the generic playbook below applies"
        )
        return
    # Valued at a rate the offer's own size can actually reach: the ranked
    # list still holds markets whose hedge breaks the venue's published size
    # once the credit is scaled, and quoting one of those put $42 of an
    # unbackable guarantee on the card.
    stake_guess = float(bonus_amount) if bonus_amount else PLAN_UNIT
    # Counted, not silently dropped, exactly as the two sibling re-scales do.
    # This comprehension discarded markets with no ``_refuse_market`` call, so
    # a book whose every conversion broke its hedge's published size reported
    # ``skipped={}`` and printed an empty "gated out:" line in the CLI.
    usable = [
        candidate
        for candidate, _ in _rescale_survivors(
            conversion, stake_guess / PLAN_UNIT, skipped)
    ]
    if not usable:
        out["strategy"] = "text_only"
        out["caveats"].append(
            "no market on this book can absorb a conversion at this size, so "
            "the refund cannot be valued; the generic playbook below applies"
        )
        return
    rate = usable[0].settled_floor / PLAN_UNIT
    out["strategy"] = "no_sweat_hedge"
    stake = float(bonus_amount) if bonus_amount else PLAN_UNIT
    out["unit"] = {"kind": "protected_stake", "amount": stake, "assumed": bonus_amount is None}
    out["refund_conversion_pct"] = round(rate * 100.0, 2)
    if bonus_amount is None:
        out["caveats"].append(
            f"the offer does not state its cap; figures are per ${PLAN_UNIT:g} of "
            "protected stake and scale linearly"
        )
    candidates = [
        candidate
        for candidate in _scan_into(
            skipped, context, promo_keys,
            mode=MODE_CASH, stake=stake, min_promo_decimal=min_dec,
            refund_rate=rate,
        )
        # Positive floors only, as every other mode already required.  Without
        # this the top plan for a *free insurance* offer could be one that
        # locks a loss in every outcome — measured at −$1.56 on ordinary wide
        # vig — printed in the same format as a profitable plan and with an
        # empty caveat list.  A promo that cannot beat doing nothing is not a
        # plan, and the refund rate above already says why.
        if candidate.settled_floor > _EPSILON
    ]
    for candidate in candidates[:MAX_PLANS_PER_OFFER]:
        out["plans"].append(_candidate_payload(candidate, as_of=context.as_of, state=context.state))
    if not candidates:
        out["caveats"].append(
            "no market on this book turns the insurance into a locked profit "
            "at today's hedge prices; the refund conversion above is what the "
            "credit would be worth if the qualifying bet loses"
        )


def _plan_boost(
    out: dict[str, Any],
    context: _PlanContext,
    promo_keys: Sequence[str],
    skipped: Counter,
    boost_pct: float | None,
    bonus_amount: float | None,
    min_dec: float | None,
) -> None:
    stake = float(bonus_amount) if bonus_amount else PLAN_UNIT
    if boost_pct is not None:
        out["strategy"] = "boost_locked"
        out["unit"] = {"kind": "boosted_stake", "amount": stake, "assumed": bonus_amount is None,
                       "boost_percent": boost_pct}
        candidates = [
            c for c in _scan_into(
                skipped, context, promo_keys,
                mode=MODE_BOOSTED, stake=stake, min_promo_decimal=min_dec,
                boost_percent=boost_pct,
            )
            if c.settled_floor > _EPSILON
        ]
        for candidate in candidates[:MAX_PLANS_PER_OFFER]:
            out["plans"].append(_candidate_payload(candidate, as_of=context.as_of, state=context.state))
        if not candidates:
            out["caveats"].append(
                f"no market on this book locks a profit at a {boost_pct:g}% boost "
                "against today's hedge prices"
            )
        return
    # No stated percentage: report, per market, the smallest boost that locks a
    # profit, so the operator can match the book's boost token to the cheapest
    # target.  ``n_be = 1/(1 − Σ 1/n_hedge)`` is the promo-side net price at
    # which the position breaks even.
    out["strategy"] = "boost_breakeven"
    out["unit"] = {"kind": "boosted_stake", "amount": stake, "assumed": bonus_amount is None,
                   "boost_percent": None}
    out["caveats"].append(
        "the offer does not state its boost percentage; markets below are "
        "ranked by the smallest boost that locks a profit"
    )
    cash = _scan_into(
        skipped, context, promo_keys,
        mode=MODE_CASH, stake=stake, min_promo_decimal=min_dec,
    )
    rows: list[tuple[float, _Candidate]] = []
    for candidate in cash:
        hedge_sum = sum(1.0 / leg.net_odds for leg in candidate.hedge_legs)
        if hedge_sum >= 1.0 - _EPSILON:
            # The hedges alone already cover the book, so no promo price —
            # boosted by any amount — breaks even.  Counted, not dropped: the
            # caveat beside these numbers points the reader at ``skipped``.
            _refuse_market(skipped, "no_breakeven_boost_exists", candidate)
            continue
        breakeven_net = 1.0 / (1.0 - hedge_sum)
        base = candidate.promo_leg.net_odds
        if base <= _EPSILON + 1.0:
            # A promo price with no profit in it cannot be scaled into one: the
            # boost multiplies ``base - 1``, which is zero here.  Reachable only
            # at prices no book quotes, but the schema's floor is 1.0 exclusive
            # rather than 1.001, so a legal 1.0000000001 lands here — kept and
            # counted rather than deleted as dead.
            _refuse_market(skipped, "promo_price_too_short_to_boost", candidate)
            continue
        needed = ((breakeven_net - 1.0) / (base - 1.0) - 1.0) * 100.0
        rows.append((max(needed, 0.0), candidate))
    rows.sort(key=lambda item: (item[0], -item[1].settled_floor, item[1].view.key[0]))
    boosted_rows = []
    # A private counter so the re-solve's refusals are recorded once, as
    # post-scan facts, rather than incremented straight onto the offer's.
    resolve_refusals: Counter = Counter()
    for needed, candidate in rows:
        # Re-solve at the boost being advertised.  The candidates above come
        # from the *cash* scan, so their hedge stakes were sized for the
        # unboosted price and the hedge-wins outcome does not contain the boost
        # at all — the printed floor was invariant in the boost percentage.
        # The card said "needs a 1.6%+ boost" beside "worst case −$0.92", and no
        # boost, however large, made those stakes reach the first claim: 11 of
        # 18 such cards on a live run contradicted themselves that way.
        boosted = _solve(
            candidate.view,
            promo_selection=candidate.promo_leg.quote.selection,
            promo_quote=candidate.promo_leg.quote,
            promo_net=candidate.promo_leg.net_odds,
            combo=tuple((leg.quote.source, leg.quote) for leg in candidate.hedge_legs),
            hedge_nets=[leg.net_odds for leg in candidate.hedge_legs],
            shape=frozenset(leg.quote.selection for leg in candidate.legs),
            mode=MODE_BOOSTED,
            stake=stake,
            boost_percent=needed,
            refund_rate=0.0,
            refusals=resolve_refusals,
        )
        if boosted is None:
            # The re-solve records into its own counter; fold its refused
            # markets into the offer's, keyed the same way.
            for reason in list(_refused_markets(resolve_refusals)):
                _record_refused_market(skipped, reason, candidate.view.key)
            resolve_refusals.clear()
            continue
        resolve_refusals.clear()
        boosted_rows.append((needed, boosted))
    for needed, boosted in boosted_rows[:MAX_PLANS_PER_OFFER]:
        payload = _candidate_payload(boosted, as_of=context.as_of, state=context.state)
        payload["breakeven_boost_pct"] = round(needed, 2)
        out["plans"].append(payload)


def _plan_rollover(
    out: dict[str, Any],
    context: _PlanContext,
    promo_keys: Sequence[str],
    skipped: Counter,
    bonus_amount: float | None,
    wagering: float | None,
    min_dec: float | None,
) -> None:
    out["strategy"] = "rollover_grind"
    out["unit"] = {"kind": "wagered_dollars", "amount": PLAN_UNIT, "assumed": True}
    candidates = _scan_into(
        skipped, context, promo_keys,
        mode=MODE_CASH, stake=PLAN_UNIT, min_promo_decimal=min_dec,
    )
    for candidate in candidates[:MAX_PLANS_PER_OFFER]:
        payload = _candidate_payload(candidate, as_of=context.as_of, state=context.state)
        payload["cost_per_100_wagered"] = round(-min(candidate.settled_floor, 0.0) or 0.0, 2)
        out["plans"].append(payload)
    if candidates and bonus_amount and wagering:
        cost_rate = (-min(candidates[0].settled_floor, 0.0) or 0.0) / PLAN_UNIT
        clearing = float(bonus_amount) * wagering * cost_rate
        out["expected_value"] = round(float(bonus_amount) - clearing, 2)
        out["caveats"].append(
            f"clearing {wagering:g}x on ${float(bonus_amount):g} through the cheapest "
            f"market above costs about ${clearing:.2f}; the estimate assumes credit "
            "cashes at face value once cleared"
        )
    out["caveats"].append(
        "grind rollover through the lowest-cost two-sided market, hedging each "
        "round; the cost shown is the vig per $100 pushed through"
    )


def _note_unprofitable(
    out: dict[str, Any], priced: Sequence[_Candidate], kept: Sequence[_Candidate],
) -> None:
    """Say so when hedgeable markets were dropped for not clearing a profit.

    ``_plan_no_sweat`` and ``_plan_boost`` both carry a "no market … locks a
    profit" sentence for exactly this; the two conversion builders dropped the
    same candidates silently.  That produced two wrong states.  With every
    market hedgeable and no floor positive, the offer printed zero plans, an
    empty ``skipped`` and only the boilerplate caveat — no reason at all, and
    the CLI's "run with --verbose to see why" then said nothing.  With one
    further unhedgeable market on the slate, the counts became non-empty and
    the summary sentence fired: "the counts in 'skipped' say what was refused
    and why", over markets that were hedgeable and are in no count.
    """
    dropped = len(priced) - len(kept)
    if dropped <= 0:
        return
    out["caveats"].append(
        f"{dropped} hedgeable market{'s' if dropped != 1 else ''} on this book "
        "priced a conversion that does not clear a profit at today's hedge "
        "prices, so they are not listed; they are not in the counts below"
    )


def _rescale_survivors(
    candidates: Sequence[_Candidate], scale: float, skipped: Counter,
) -> list[tuple[_Candidate, _Candidate]]:
    """The best assignment per market that the offer's real size can place.

    The provisional scan keeps several assignments per market precisely so this
    can fall through to a placeable one.  Ranked order is preserved, so the
    first survivor for a market is the best-priced one that fits.

    A market is counted as refused only when *every* assignment on it failed —
    counting the loser of a market that went on to plan would put a market in
    both the plans list and the refusal tally, under a caveat saying the counts
    explain why nothing was printed.
    """
    survivors: list[tuple[_Candidate, _Candidate]] = []
    planned: set = set()
    refused: dict = {}
    for candidate in candidates:
        key = (candidate.view.key, candidate.promo_leg.quote.selection)
        if key in planned:
            continue
        scaled = _rescale(candidate, scale)
        if scaled is None:
            refused.setdefault(key, candidate)
            continue
        planned.add(key)
        refused.pop(key, None)
        survivors.append((candidate, scaled))
    for candidate in refused.values():
        _refuse_market(skipped, "stake_over_stated_limit", candidate)
    return survivors


def _rescale(candidate: _Candidate, scale: float) -> _Candidate | None:
    """Scale a per-unit candidate to the offer's stated dollars.

    Stakes scale linearly, so re-rounding to cents after scaling keeps the
    floor exact at the printed stakes; outcome profits are re-derived from the
    scaled legs rather than multiplied, so rounding never compounds.
    """
    if abs(scale - 1.0) < _EPSILON:
        # Gated even here.  The shortcut used to return unchecked, on the
        # grounds that ``_solve`` had already measured these stakes against the
        # venue's limit — true when the scan gated its own provisional stakes,
        # false now that it does not.  An offer of exactly ``PLAN_UNIT`` is the
        # one size that reaches this branch, and it would have been the one
        # size no limit gate ever ran on.
        if _over_stated_limit(candidate.legs):
            return None
        return candidate
    promo = candidate.promo_leg
    scaled_promo = _Leg(promo.quote, _round_cents(promo.stake * scale), promo.net_odds,
                        promo.role, promo.mode)
    scaled_hedges = tuple(
        _Leg(leg.quote, _round_cents(leg.stake * scale), leg.net_odds, leg.role, leg.mode)
        for leg in candidate.hedge_legs
    )
    legs = (scaled_promo, *scaled_hedges)
    if _over_stated_limit(legs):
        return None
    shape = frozenset(leg.quote.selection for leg in legs)
    profits = _outcome_profits(candidate.view, legs, shape=shape)
    rounded = tuple((label, round(profit, 2) or 0.0) for label, profit in profits)
    floor = min(profit for _, profit in rounded)
    settled = _settled_floor(scaled_promo.quote.selection, rounded, candidate.view, shape)
    return _Candidate(
        view=candidate.view,
        promo_leg=scaled_promo,
        hedge_legs=scaled_hedges,
        outcome_profits=rounded,
        floor=floor,
        settled_floor=settled,
        metric=candidate.metric,
        notes=candidate.notes,
    )


def build_promo_plans(
    offers: Sequence[Any],
    quotes: Sequence[Quote],
    *,
    as_of: datetime,
    commissions: Mapping[str, Commission] | None = None,
    one_counterparty: Mapping[str, Sequence[frozenset[str]]] | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Concrete usage plans for every offer, keyed ``"source|offer_id"``.

    *offers* accepts :class:`PromoStore` row dicts and :class:`PromoOffer`
    models interchangeably.  *quotes* is one odds run's worth of rows, already
    event-reconciled the way :func:`src.report._arb_payload` reconciles them.
    *one_counterparty* is the measured/recorded counterparty gate; left
    ``None`` it is measured from the quotes, the same default the arb detector
    applies.  *state* is the run's jurisdiction, threaded to every leg link so
    a state-partitioned book links its own front door — offers are already
    state-matched, so their links must not point at another licence's site.
    """
    context = _build_context(
        quotes,
        as_of=as_of,
        commissions=commissions,
        one_counterparty=one_counterparty,
        state=state,
    )
    conversion_cache: dict[tuple[tuple[str, ...], float | None], tuple[list[_Candidate], Counter]] = {}
    plans: dict[str, Any] = {}
    for offer in offers:
        view = _offer_view(offer)
        key = f"{view.get('source', '')}|{view.get('offer_id', '')}"
        plans[key] = _plan_for_offer(view, context, conversion_cache=conversion_cache)
    return {
        "plans": plans,
        "meta": {
            "as_of": as_of.isoformat(),
            "quote_count": len(quotes),
            "group_count": len(context.groups),
            "unusable_groups": dict(sorted(context.unusable.items())),
        },
    }


__all__ = [
    "MAX_PLANS_PER_OFFER",
    "PLAN_UNIT",
    "build_promo_plans",
    "parse_boost_percent",
    "parse_min_odds",
    "parse_qualifying_stake",
    "parse_wagering_multiple",
    "stakeable_odds_sources",
]
