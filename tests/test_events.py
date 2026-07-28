"""Cross-source event identity, especially doubleheaders.

A doubleheader is where cross-source joining goes wrong quietly.  Each adapter
numbers the games it happens to see, so ``#2`` is a per-source ordinal rather
than an identity, and the resulting mis-join produces two unrelated games
wearing one event key — the exact shape of a phantom arbitrage.  The tests below
construct each way that can happen and pin the corrected behaviour.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.events import (
    SAME_GAME_TOLERANCE,
    build_event_key,
    cluster_start_times,
    reconcile_event_keys,
    resolve_doubleheaders,
    scheduling_date,
)
from tests.conftest import make_quote

EASTERN_EVENING = datetime(2026, 7, 28, 23, 10, tzinfo=UTC)  # 19:10 ET


class TestSchedulingDate:
    def test_uses_eastern_not_utc(self) -> None:
        """A 01:45Z first pitch is the previous evening in the US."""
        assert scheduling_date(datetime(2026, 7, 29, 1, 45, tzinfo=UTC)).isoformat() == "2026-07-28"

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            scheduling_date(datetime(2026, 7, 28, 23, 10))

    def test_key_format(self) -> None:
        assert build_event_key("PHI", "MIA", EASTERN_EVENING) == "PHI@MIA:2026-07-28"


class TestClustering:
    def test_two_books_a_minute_apart_are_one_game(self) -> None:
        """FanDuel reports every start a minute later than the other books."""
        times = [EASTERN_EVENING, EASTERN_EVENING + timedelta(minutes=1)]
        assert len(cluster_start_times(times)) == 1

    def test_a_doubleheader_is_two_games(self) -> None:
        times = [EASTERN_EVENING, EASTERN_EVENING + timedelta(hours=3, minutes=30)]
        assert len(cluster_start_times(times)) == 2

    def test_the_boundary_is_the_tolerance(self) -> None:
        base = EASTERN_EVENING
        assert len(cluster_start_times([base, base + SAME_GAME_TOLERANCE])) == 1
        assert len(cluster_start_times([base, base + SAME_GAME_TOLERANCE + timedelta(minutes=1)])) == 2

    def test_empty_input(self) -> None:
        assert cluster_start_times([]) == []


def _slate(source: str, *, games: list[tuple[str, datetime]]) -> list:
    """Synthetic quotes: one moneyline pair per game, keyed as an adapter would.

    Adapters number doubleheaders over their own slate, so this reproduces the
    per-source numbering that reconciliation has to correct.
    """
    base_keys = {}
    for event_id, commence in games:
        base_keys[event_id] = (build_event_key("CLE", "CIN", commence), commence)
    resolved = resolve_doubleheaders(base_keys)

    quotes = []
    for event_id, commence in games:
        for selection, odds in (("home", 1.91), ("away", 1.95)):
            from src.schema import Selection

            quotes.append(
                make_quote(
                    source=source,
                    source_event_id=event_id,
                    event_key=resolved[event_id],
                    home_team="Cincinnati Reds",
                    away_team="Cleveland Guardians",
                    commence_time=commence,
                    selection=Selection.HOME if selection == "home" else Selection.AWAY,
                    decimal_odds=odds,
                    source_market_id=f"{source}-{event_id}-ml",
                )
            )
    return quotes


GAME_1 = datetime(2026, 7, 28, 21, 10, tzinfo=UTC)
GAME_2 = datetime(2026, 7, 29, 0, 40, tzinfo=UTC)  # 20:40 ET, same slate date


class TestReconciliation:
    def test_agreeing_books_are_left_alone(self) -> None:
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b1", GAME_1 + timedelta(minutes=1)), ("b2", GAME_2)]),
        ]
        reconciled, changes = reconcile_event_keys(quotes)
        assert changes == []
        assert {q.event_key for q in reconciled} == {
            "CLE@CIN:2026-07-28",
            "CLE@CIN:2026-07-28#2",
        }

    def test_a_book_listing_only_the_late_game_is_not_joined_onto_game_one(self) -> None:
        """The dangerous case.  Book B lists only game 2, so its own adapter
        numbers it as though it were the only game and gives it the bare key —
        fusing book B's game 2 prices onto book A's game 1."""
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b2", GAME_2)]),
        ]
        # Confirm the defect exists in the incoming data.
        before = {(q.source, q.event_key) for q in quotes}
        assert ("book_b", "CLE@CIN:2026-07-28") in before

        reconciled, changes = reconcile_event_keys(quotes)
        assert changes, "reconciliation should have corrected book_b"
        by_key: dict[str, set[str]] = {}
        for quote in reconciled:
            by_key.setdefault(quote.event_key, set()).add(quote.source)
        # Game 1 is book_a alone; game 2 is where the two books meet.
        assert by_key["CLE@CIN:2026-07-28"] == {"book_a"}
        assert by_key["CLE@CIN:2026-07-28#2"] == {"book_a", "book_b"}

    def test_books_that_disagree_on_time_order_still_agree_on_identity(self) -> None:
        """Book B has the two start times transposed relative to book A.  Because
        identity now comes from the start time rather than from sort position,
        each book's early game lands on the early key."""
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b_late", GAME_2), ("b_early", GAME_1)]),
        ]
        reconciled, _ = reconcile_event_keys(quotes)
        for quote in reconciled:
            expected = (
                "CLE@CIN:2026-07-28" if quote.commence_time == GAME_1 else "CLE@CIN:2026-07-28#2"
            )
            assert quote.event_key == expected

    def test_a_book_reporting_both_games_at_one_time_collapses_loudly(self) -> None:
        """When a feed repeats game 1's time for game 2 there is no information
        left to separate them.  Collapsing both into one key produces duplicate
        priced selections, which validation reports as an error — far better than
        the silent cross-book swap that ordering by source id produced."""
        quotes = _slate("book_b", games=[("b1", GAME_1), ("b2", GAME_1)])
        reconciled, _ = reconcile_event_keys(quotes)
        assert len({q.event_key for q in reconciled}) == 1
        keys = [q.dedup_key for q in reconciled]
        assert len(keys) != len(set(keys)), "the collision must be visible to validation"

    def test_a_cluster_straddling_eastern_midnight_is_not_split(self) -> None:
        """Two books ten minutes apart across 00:00 ET would otherwise land on
        different calendar dates and so on different event keys."""
        before_midnight = datetime(2026, 7, 29, 3, 55, tzinfo=UTC)  # 23:55 ET on the 28th
        after_midnight = datetime(2026, 7, 29, 4, 5, tzinfo=UTC)  # 00:05 ET on the 29th
        quotes = [
            *_slate("book_a", games=[("a1", before_midnight)]),
            *_slate("book_b", games=[("b1", after_midnight)]),
        ]
        reconciled, _ = reconcile_event_keys(quotes)
        assert len({q.event_key for q in reconciled}) == 1
        assert {q.event_key for q in reconciled} == {"CLE@CIN:2026-07-28"}

    def test_rows_with_unresolvable_teams_are_left_for_validation(self) -> None:
        # The schema forbids identical teams, so start from a valid pair and
        # then break one side the way a parser regression would.
        bad = make_quote().model_copy(update={"home_team": "Not A Team At All"})
        reconciled, changes = reconcile_event_keys([bad])
        assert len(reconciled) == 1
        assert reconciled[0].event_key == bad.event_key
        assert changes == []

    def test_reconciliation_is_idempotent(self) -> None:
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b2", GAME_2)]),
        ]
        once, first_changes = reconcile_event_keys(quotes)
        twice, second_changes = reconcile_event_keys(once)
        assert first_changes
        assert second_changes == []
        assert [q.event_key for q in once] == [q.event_key for q in twice]

    def test_separate_matchups_do_not_interfere(self) -> None:
        from src.schema import Selection

        other = [
            make_quote(
                source="book_a",
                source_event_id="other",
                event_key="PHI@MIA:2026-07-28",
                selection=Selection.HOME,
                source_market_id="book_a-other",
            )
        ]
        quotes = [*_slate("book_a", games=[("a1", GAME_1)]), *other]
        reconciled, changes = reconcile_event_keys(quotes)
        assert changes == []
        assert {q.event_key for q in reconciled} == {"CLE@CIN:2026-07-28", "PHI@MIA:2026-07-28"}


class TestRealFixtureSlate:
    def test_the_captured_doubleheader_needs_no_correction(self, all_fixture_quotes) -> None:
        """Both books saw both games of the CLE@CIN doubleheader and numbered
        them consistently, so reconciliation is a no-op here.  That is what makes
        the synthetic cases above necessary."""
        reconciled, changes = reconcile_event_keys(all_fixture_quotes)
        assert changes == []
        assert len(reconciled) == len(all_fixture_quotes)
        assert {q.event_key for q in reconciled if q.event_key.startswith("CLE@CIN")} == {
            "CLE@CIN:2026-07-28",
            "CLE@CIN:2026-07-28#2",
        }
