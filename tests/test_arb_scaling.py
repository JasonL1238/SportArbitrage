"""The arbitrage engine at ten to thirty sources rather than three.

Three things break as the source list grows, and none of them is visible with
three books:

* **The search is exponential in the source count.**  ``itertools.product`` over
  the eligible books is ``sources ** selections``, and ``selections`` is three
  for a soccer or hockey-regulation moneyline.  The replacement is linear; these
  tests hold it to producing *the same answer*, because a faster function that
  quietly picks a different book is not an optimisation.
* **Several refusals were all-or-nothing across the whole source set.**  One
  mispaired book refused a market for every book, which costs more the more
  books there are.
* **Exchange legs are not priced net.**  Exchanges quote tighter than
  sportsbooks, so their legs are exactly the ones that drag a cross-book sum
  below 1.0 — commission ignored, the false positives concentrate on precisely
  the sources this work adds.
"""
from __future__ import annotations

import itertools
import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from src.arb import (
    _best_assignment,
    _fixture_outliers,
    find_opportunities,
    net_decimal,
)
from src.commission import (
    NO_COMMISSION,
    ContractFeeCommission,
    WinningsCommission,
)
from src.schema import Market, Period, Quote, Selection, Sport
from tests.conftest import make_quote


# ── a brute-force reference ──────────────────────────────────────────────────


def _reference_assignment(*, needed, eligible, best, require_distinct_sources, commissions):
    """The exhaustive search the linear one replaced, kept as an oracle.

    Deliberately the *old* implementation, transcribed: keep the first
    combination with a strictly lower total, enumerating in lexicographic order
    of option indices.  That enumeration order is what decides ties, and ties are
    common — two books at -110 is the most ordinary pair of prices there is — so
    an equivalence test that only compared totals would pass while the two
    functions disagreed about which book to bet at.
    """
    selections = sorted(needed, key=lambda s: s.value)
    options = [[s for s in eligible if selection in best[s]] for selection in selections]
    if any(not opts for opts in options):
        return None
    winner = None
    for combination in itertools.product(*options):
        if require_distinct_sources and len(set(combination)) < 2:
            continue
        assignment = {
            selection: best[source][selection]
            for selection, source in zip(selections, combination)
        }
        total = sum(
            1.0 / net_decimal(quote, commissions) for quote in assignment.values()
        )
        if winner is None or total < winner[1]:
            winner = (assignment, total)
    return winner


def _market(sources, selections, prices, *, sport=Sport.BASEBALL, league="MLB"):
    """``best``-shaped mapping: ``{source: {selection: Quote}}``."""
    best: dict[str, dict[Selection, Quote]] = {}
    for source in sources:
        offered = {}
        for selection in selections:
            odds = prices.get((source, selection))
            if odds is None:
                continue
            offered[selection] = make_quote(
                source=source,
                selection=selection,
                decimal_odds=odds,
                sport=sport,
                league=league,
            )
        if offered:
            best[source] = offered
    return best


class TestLinearAssignmentMatchesTheExhaustiveSearch:
    """Same answer, including which book each leg is taken at."""

    @pytest.mark.parametrize("seed", range(60))
    def test_randomised_markets_agree_exactly(self, seed: int) -> None:
        rng = random.Random(seed)
        source_count = rng.randint(1, 6)
        sources = [f"book{i}" for i in range(source_count)]
        # A three-way market is soccer: a draw is not a settlement outcome of a
        # full-game baseball moneyline, and the schema rejects one outright.
        three_way = rng.random() < 0.5
        selections = (
            [Selection.HOME, Selection.AWAY, Selection.DRAW]
            if three_way
            else [Selection.HOME, Selection.AWAY]
        )
        sport, league = (
            (Sport.SOCCER, "EPL") if three_way else (Sport.BASEBALL, "MLB")
        )
        # A coarse price grid on purpose: ties between books are the case the
        # tie-breaking rule exists for, and a continuous grid would never produce
        # one.
        grid = [1.5, 1.8, 1.9, 2.0, 2.1, 3.0]
        prices = {}
        for source in sources:
            for selection in selections:
                if rng.random() < 0.2:
                    continue  # this book does not offer that side
                prices[(source, selection)] = rng.choice(grid)
        best = _market(sources, selections, prices, sport=sport, league=league)
        eligible = [s for s in sources if s in best]
        needed = frozenset(selections)
        if not eligible:
            pytest.skip("no book offered anything")

        for distinct in (True, False):
            mine = _best_assignment(
                needed=needed,
                eligible=eligible,
                best=best,
                require_distinct_sources=distinct,
                commissions={},
            )
            theirs = _reference_assignment(
                needed=needed,
                eligible=eligible,
                best=best,
                require_distinct_sources=distinct,
                commissions={},
            )
            if theirs is None:
                assert mine is None, seed
                continue
            assert mine is not None, seed
            assert mine[1] == pytest.approx(theirs[1]), seed
            assert {s: q.source for s, q in mine[0].items()} == {
                s: q.source for s, q in theirs[0].items()
            }, seed

    def test_a_tie_between_books_resolves_the_same_way(self) -> None:
        """Two books at identical prices is the ordinary case, not an edge one."""
        best = _market(
            ["a", "b", "c"],
            [Selection.HOME, Selection.AWAY],
            {
                ("a", Selection.HOME): 2.0,
                ("b", Selection.HOME): 2.0,
                ("c", Selection.HOME): 2.0,
                ("a", Selection.AWAY): 2.0,
                ("b", Selection.AWAY): 2.0,
                ("c", Selection.AWAY): 2.0,
            },
        )
        needed = frozenset({Selection.HOME, Selection.AWAY})
        mine = _best_assignment(
            needed=needed, eligible=["a", "b", "c"], best=best,
            require_distinct_sources=True, commissions={},
        )
        theirs = _reference_assignment(
            needed=needed, eligible=["a", "b", "c"], best=best,
            require_distinct_sources=True, commissions={},
        )
        assert {s: q.source for s, q in mine[0].items()} == {
            s: q.source for s, q in theirs[0].items()
        }

    def test_one_book_only_yields_no_legal_position(self) -> None:
        best = _market(
            ["solo"],
            [Selection.HOME, Selection.AWAY],
            {("solo", Selection.HOME): 2.1, ("solo", Selection.AWAY): 2.1},
        )
        assert (
            _best_assignment(
                needed=frozenset({Selection.HOME, Selection.AWAY}),
                eligible=["solo"],
                best=best,
                require_distinct_sources=True,
                commissions={},
            )
            is None
        )


class TestScale:
    def test_thirty_sources_on_a_full_soccer_slate_stays_fast(self) -> None:
        """The measurement that motivated the rewrite, as a regression bound.

        700 three-way fixtures — today's live slate is 728 — priced by 30 books.
        The exhaustive search took 8.43 s on this shape; anything in that
        neighbourhood means the exponent is back.  The bound is deliberately
        loose (a second) so it measures the algorithm rather than the machine.
        """
        start = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        quotes: list[Quote] = []
        for fixture in range(700):
            commence = start + timedelta(days=3, minutes=fixture % 60)
            for book in range(30):
                # Books differ slightly, but none of them prices its own complete
                # market below 1.0 — that would be a parser fault, and the
                # engine would (correctly) exclude every book before measuring
                # anything.
                for selection, odds in (
                    (Selection.HOME, 2.20 + 0.002 * book),
                    (Selection.AWAY, 3.40 - 0.002 * book),
                    (Selection.DRAW, 3.40),
                ):
                    quotes.append(
                        make_quote(
                            source=f"book{book:02d}",
                            sport=Sport.SOCCER,
                            league="EPL",
                            market=Market.MONEYLINE,
                            period=Period.FULL_GAME,
                            selection=selection,
                            decimal_odds=odds,
                            event_key=f"SOCCER-a{fixture}@SOCCER-b{fixture}:2026-07-31",
                            source_event_id=f"e{fixture}",
                            home_participant=f"SOCCER-b{fixture}",
                            away_participant=f"SOCCER-a{fixture}",
                            home_team=f"Home {fixture}",
                            away_team=f"Away {fixture}",
                            commence_time=commence,
                            source_market_id=f"m{fixture}",
                        )
                    )
        began = time.perf_counter()
        report = find_opportunities(quotes)
        elapsed = time.perf_counter() - began
        assert report.comparable_group_count == 700
        # A tripwire for an order-of-magnitude regression, not a benchmark.  The
        # budget was 1.0s, written when this measured 0.58s on a developer's Mac;
        # detection has since grown ~47% heavier and the headroom went with it.  On
        # the 4-vCPU `ubuntu-latest` runner the same case measures 2.11s (CI run
        # 31232635917), so the assertion failed on every CI run while passing on
        # every desk — a clock that reports the runner, not the code.  Five seconds
        # still catches the accidental quadratic this exists to catch.
        assert elapsed < 5.0, f"{elapsed:.2f}s for 700 three-way markets at 30 sources"


class TestOneBadSourceDoesNotRefuseTheMarket:
    def _rows(self, source: str, home: float, away: float, **overrides) -> list[Quote]:
        return [
            make_quote(source=source, selection=Selection.HOME, decimal_odds=home,
                       source_market_id=f"{source}-m", **overrides),
            make_quote(source=source, selection=Selection.AWAY, decimal_odds=away,
                       source_market_id=f"{source}-m", **overrides),
        ]

    def test_a_book_that_prices_itself_to_lose_is_excluded_not_the_group(self) -> None:
        """The old code refused the whole market and its own message said
        otherwise — "none of *them* are used" meant that source's prices, while
        the return discarded every source's."""
        quotes = [
            # Mispaired: this book's own two prices sum below 1.0, which a book
            # never does.
            *self._rows("broken", home=2.60, away=2.60),
            *self._rows("good_a", home=2.00, away=2.00),
            *self._rows("good_b", home=1.95, away=2.05),
        ]
        report = find_opportunities(quotes)
        codes = {d.code for d in report.diagnostics}
        assert "source_prices_itself_to_lose" in codes
        assert report.comparable_group_count == 1, "the market is still compared"
        assert "broken" not in {
            leg.source for opp in report.opportunities for leg in opp.legs
        }

    def test_a_source_that_names_a_different_fixture_is_excluded_not_the_group(self) -> None:
        """One book with a mislabelled orientation must not remove a fixture from
        comparison for the other nine."""
        quotes = [
            *self._rows("good_a", home=2.00, away=2.00),
            *self._rows("good_b", home=1.95, away=2.05),
            *self._rows(
                "confused",
                home=2.50,
                away=1.60,
                home_participant="MLB-NYY",
                away_participant="MLB-BOS",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
            ),
        ]
        report = find_opportunities(quotes)
        assert "legs_disagree_on_the_game" in {d.code for d in report.diagnostics}
        assert report.opportunities, "the two agreeing books still produce a position"
        assert "confused" not in {
            leg.source for opp in report.opportunities for leg in opp.legs
        }

    def test_a_mislabelled_sport_costs_that_source_alone(self) -> None:
        quotes = [
            *self._rows("good_a", home=2.00, away=2.00),
            *self._rows("good_b", home=1.95, away=2.05),
            *self._rows("wrong_sport", home=2.50, away=1.60, sport=Sport.SOCCER,
                        league="EPL"),
        ]
        report = find_opportunities(quotes)
        assert "mixed_sport" in {d.code for d in report.diagnostics}
        assert report.opportunities

    def test_no_majority_sport_still_refuses_the_whole_group(self) -> None:
        """With nothing to be an outlier from, there is no safe reading."""
        quotes = [
            *self._rows("a", home=2.00, away=2.00),
            *self._rows("b", home=1.95, away=2.05, sport=Sport.SOCCER, league="EPL"),
        ]
        report = find_opportunities(quotes)
        assert "mixed_sport" in {d.code for d in report.diagnostics}
        assert not report.opportunities


class TestFixtureOutliers:
    def test_a_minute_of_start_time_disagreement_is_not_a_different_fixture(self) -> None:
        """Books disagree about a kickoff by minutes as a matter of course; that
        is why clustering exists.  Treating it as "the legs describe different
        games" refused ordinary cross-book positions."""
        base = datetime(2026, 7, 28, 22, 41, tzinfo=UTC)
        legs = [
            make_quote(source="a", commence_time=base),
            make_quote(source="b", commence_time=base + timedelta(minutes=2)),
        ]
        assert _fixture_outliers(legs) == set()

    def test_a_different_matchup_is(self) -> None:
        legs = [
            make_quote(source="a"),
            make_quote(source="b"),
            make_quote(
                source="c",
                home_participant="MLB-NYY",
                away_participant="MLB-BOS",
                home_team="New York Yankees",
                away_team="Boston Red Sox",
            ),
        ]
        assert _fixture_outliers(legs) == {"c"}


class TestCommission:
    def test_an_exchange_edge_that_commission_eats_is_not_reported(self) -> None:
        """The failure this closes.  An exchange quotes tighter than a
        sportsbook, so its legs are precisely the ones that take a cross-book sum
        below 1.0 — and a margin measured on the quoted price would report every
        one of them."""
        quotes = [
            make_quote(source="sportsbook", selection=Selection.HOME, decimal_odds=2.02,
                       source_market_id="a"),
            make_quote(source="exchange", selection=Selection.AWAY, decimal_odds=2.02,
                       source_market_id="b"),
        ]
        gross = find_opportunities(quotes, commissions={})
        assert gross.opportunities, "a 1% edge on the quoted prices"

        net = find_opportunities(
            quotes,
            commissions={"exchange": WinningsCommission(label="exchange", rate=0.02)},
        )
        assert not net.opportunities, "2% of winnings is more than the 1% edge"

    def test_a_reported_position_states_the_price_it_is_actually_paid(self) -> None:
        quotes = [
            make_quote(source="sportsbook", selection=Selection.HOME, decimal_odds=2.30,
                       source_market_id="a"),
            make_quote(source="exchange", selection=Selection.AWAY, decimal_odds=2.30,
                       source_market_id="b"),
        ]
        report = find_opportunities(
            quotes,
            commissions={"exchange": WinningsCommission(label="exchange", rate=0.02)},
        )
        opportunity = report.opportunities[0]
        exchange_leg = next(leg for leg in opportunity.legs if leg.source == "exchange")
        assert exchange_leg.decimal_odds == pytest.approx(2.30)
        assert exchange_leg.net_odds == pytest.approx(1.0 + 1.30 * 0.98)
        assert any("net of commission" in note for note in opportunity.notes)
        # And the guarantee is computed on what is paid, not on what is quoted.
        assert opportunity.guaranteed_profit == pytest.approx(
            min(profit for _, profit in opportunity.outcome_profits)
        )

    def test_a_contract_fee_is_charged_on_entry_not_on_winnings(self) -> None:
        """Kalshi and Polymarket add a fee to the price you pay, so it is due
        whether or not the contract settles your way — which is not a rate on
        winnings with some equivalent number."""
        fee = ContractFeeCommission(label="kalshi", rate=0.07, shape="p_times_q")
        # 0.50 a contract, fee 0.07 * 0.5 * 0.5 = 0.0175, so 0.5175 buys a dollar.
        assert fee.net_decimal(2.0) == pytest.approx(1.0 / 0.5175)
        # At the extremes the fee vanishes and the price is nearly the quoted one.
        assert fee.net_decimal(100.0) == pytest.approx(
            1.0 / (0.01 + 0.07 * 0.01 * 0.99), rel=1e-9
        )

    def test_a_sportsbook_pays_what_it_quotes(self) -> None:
        quote = make_quote(source="fanduel", decimal_odds=1.91)
        assert net_decimal(quote) == pytest.approx(1.91)
        assert NO_COMMISSION.net_decimal(1.91) == pytest.approx(1.91)
