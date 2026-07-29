"""Cross-source event identity, especially repeat fixtures.

A doubleheader is where cross-source joining goes wrong quietly.  Each adapter
numbers the games it happens to see, so ``#2`` is a per-source ordinal rather
than an identity, and the resulting mis-join produces two unrelated games wearing
one event key — the exact shape of a phantom arbitrage.  The tests below
construct each way that can happen and pin the corrected behaviour.

The multi-sport cases pin the two things that differ by sport: the calendar a
date is bucketed against, and how far apart two books' start times may be and
still mean one fixture.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.events import (
    build_event_key,
    cluster_start_times,
    orient,
    reconcile_event_keys,
    resolve_doubleheaders,
    scheduling_date,
)
from src.leagues import league
from src.participants import canonical_participant, resolve_open
from src.schema import Selection, Sport
from tests.conftest import make_quote

MLB = league("MLB")
ATP = league("ATP")
EPL = league("EPL")

EASTERN_EVENING = datetime(2026, 7, 28, 23, 10, tzinfo=UTC)  # 19:10 ET


class TestSchedulingDate:
    def test_us_leagues_use_eastern_not_utc(self) -> None:
        """A 01:45Z first pitch is the previous evening in the US."""
        assert (
            scheduling_date(datetime(2026, 7, 29, 1, 45, tzinfo=UTC), MLB).isoformat()
            == "2026-07-28"
        )

    def test_global_competitions_use_utc(self) -> None:
        """Tennis and soccer have no single domestic calendar, so UTC is used —
        and a 01:45Z match is therefore the 29th, not the 28th."""
        assert (
            scheduling_date(datetime(2026, 7, 29, 1, 45, tzinfo=UTC), ATP).isoformat()
            == "2026-07-29"
        )

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            scheduling_date(datetime(2026, 7, 28, 23, 10), MLB)

    def test_key_is_built_from_participant_keys(self) -> None:
        assert (
            build_event_key("MLB-PHI", "MLB-MIA", EASTERN_EVENING, MLB)
            == "MLB-PHI@MLB-MIA:2026-07-28"
        )

    def test_keys_cannot_collide_across_sports(self) -> None:
        """Participant keys are namespaced, so no two sports share an event key
        even though league is deliberately absent from it."""
        baseball = build_event_key("MLB-TEX", "MLB-BOS", EASTERN_EVENING, MLB)
        hockey = build_event_key("NHL-NYR", "NHL-BOS", EASTERN_EVENING, league("NHL"))
        assert baseball != hockey
        assert "MLB-" in baseball and "NHL-" in hockey


class TestOrient:
    def test_a_team_sport_trusts_the_books_home_side(self) -> None:
        home = canonical_participant("Miami Marlins", MLB)
        away = canonical_participant("Philadelphia Phillies", MLB)
        assert orient(away, home, MLB, home=home) == (away, home)
        # Argument order must not matter; the named home side wins.
        assert orient(home, away, MLB, home=home) == (away, home)

    def test_a_team_sport_requires_the_home_side_to_be_named(self) -> None:
        home = canonical_participant("Miami Marlins", MLB)
        away = canonical_participant("Philadelphia Phillies", MLB)
        with pytest.raises(ValueError, match="home participant"):
            orient(away, home, MLB)

    def test_tennis_ordering_is_imposed_and_book_independent(self) -> None:
        """Neither book's ordering is authoritative, so both must land on the
        same answer regardless of the order they supplied the two players in."""
        a = resolve_open("Ugo Humbert", Sport.TENNIS)
        b = resolve_open("Andres Martin", Sport.TENNIS)
        assert orient(a, b, ATP) == orient(b, a, ATP)

    def test_tennis_event_keys_agree_across_books_that_disagree_on_order(self) -> None:
        start = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        book_a = orient(
            resolve_open("Ugo Humbert", Sport.TENNIS),
            resolve_open("Andrés Martín", Sport.TENNIS),
            ATP,
        )
        book_b = orient(
            resolve_open("Martin Andres", Sport.TENNIS),
            resolve_open("Humbert Ugo", Sport.TENNIS),
            ATP,
        )
        assert build_event_key(book_a[0].key, book_a[1].key, start, ATP) == build_event_key(
            book_b[0].key, book_b[1].key, start, ATP
        )


class TestClustering:
    def test_two_books_a_minute_apart_are_one_game(self) -> None:
        """FanDuel reports every start a minute later than the other books."""
        times = [EASTERN_EVENING, EASTERN_EVENING + timedelta(minutes=1)]
        assert len(cluster_start_times(times, MLB.same_event_tolerance)) == 1

    def test_a_doubleheader_is_two_games(self) -> None:
        times = [EASTERN_EVENING, EASTERN_EVENING + timedelta(hours=3, minutes=30)]
        assert len(cluster_start_times(times, MLB.same_event_tolerance)) == 2

    def test_the_boundary_is_the_tolerance(self) -> None:
        base, tol = EASTERN_EVENING, MLB.same_event_tolerance
        assert len(cluster_start_times([base, base + tol], tol)) == 1
        assert len(cluster_start_times([base, base + tol + timedelta(minutes=1)], tol)) == 2

    def test_tennis_tolerates_hours_of_disagreement(self) -> None:
        """A match is scheduled "after the preceding match on court" and each book
        publishes its own estimate, so four hours apart is still one match.  Under
        the 90-minute baseball tolerance this would split into two events and the
        join would break on exactly the matches both books cover."""
        times = [EASTERN_EVENING, EASTERN_EVENING + timedelta(hours=4)]
        assert len(cluster_start_times(times, ATP.same_event_tolerance)) == 1
        assert len(cluster_start_times(times, MLB.same_event_tolerance)) == 2

    def test_empty_input(self) -> None:
        assert cluster_start_times([], MLB.same_event_tolerance) == []


def _slate(source: str, *, games: list[tuple[str, datetime]]) -> list:
    """Synthetic quotes: one moneyline pair per game, keyed as an adapter would.

    Adapters number doubleheaders over their own slate, so this reproduces the
    per-source numbering that reconciliation has to correct.
    """
    base_keys = {
        event_id: (build_event_key("MLB-CLE", "MLB-CIN", commence, MLB), commence)
        for event_id, commence in games
    }
    resolved = resolve_doubleheaders(base_keys)

    quotes = []
    for event_id, commence in games:
        for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95)):
            quotes.append(
                make_quote(
                    source=source,
                    source_event_id=event_id,
                    event_key=resolved[event_id],
                    home_participant="MLB-CIN",
                    away_participant="MLB-CLE",
                    home_team="Cincinnati Reds",
                    away_team="Cleveland Guardians",
                    commence_time=commence,
                    selection=selection,
                    decimal_odds=odds,
                    source_market_id=f"{source}-{event_id}-ml",
                )
            )
    return quotes


GAME_1 = datetime(2026, 7, 28, 21, 10, tzinfo=UTC)
GAME_2 = datetime(2026, 7, 29, 0, 40, tzinfo=UTC)  # 20:40 ET, same slate date
KEY_1 = "MLB-CLE@MLB-CIN:2026-07-28"
KEY_2 = "MLB-CLE@MLB-CIN:2026-07-28#2"


class TestReconciliation:
    def test_agreeing_books_are_left_alone(self) -> None:
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b1", GAME_1 + timedelta(minutes=1)), ("b2", GAME_2)]),
        ]
        reconciled, changes = reconcile_event_keys(quotes)
        assert changes == []
        assert {q.event_key for q in reconciled} == {KEY_1, KEY_2}

    def test_a_book_listing_only_the_late_game_is_not_joined_onto_game_one(self) -> None:
        """The dangerous case.  Book B lists only game 2, so its own adapter
        numbers it as though it were the only game and gives it the bare key —
        fusing book B's game 2 prices onto book A's game 1."""
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b2", GAME_2)]),
        ]
        before = {(q.source, q.event_key) for q in quotes}
        assert ("book_b", KEY_1) in before, "the defect must exist in the incoming data"

        reconciled, changes = reconcile_event_keys(quotes)
        assert changes, "reconciliation should have corrected book_b"
        by_key: dict[str, set[str]] = {}
        for quote in reconciled:
            by_key.setdefault(quote.event_key, set()).add(quote.source)
        assert by_key[KEY_1] == {"book_a"}
        assert by_key[KEY_2] == {"book_a", "book_b"}

    def test_books_that_disagree_on_time_order_still_agree_on_identity(self) -> None:
        quotes = [
            *_slate("book_a", games=[("a1", GAME_1), ("a2", GAME_2)]),
            *_slate("book_b", games=[("b_late", GAME_2), ("b_early", GAME_1)]),
        ]
        reconciled, _ = reconcile_event_keys(quotes)
        for quote in reconciled:
            expected = KEY_1 if quote.commence_time == GAME_1 else KEY_2
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
        before_midnight = datetime(2026, 7, 29, 3, 55, tzinfo=UTC)  # 23:55 ET on the 28th
        after_midnight = datetime(2026, 7, 29, 4, 5, tzinfo=UTC)  # 00:05 ET on the 29th
        quotes = [
            *_slate("book_a", games=[("a1", before_midnight)]),
            *_slate("book_b", games=[("b1", after_midnight)]),
        ]
        reconciled, _ = reconcile_event_keys(quotes)
        assert {q.event_key for q in reconciled} == {KEY_1}

    def test_rows_with_an_unknown_league_are_left_for_validation(self) -> None:
        bad = make_quote().model_copy(update={"league": "NOT_A_LEAGUE"})
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
        other = [
            make_quote(
                source="book_a",
                source_event_id="other",
                event_key="MLB-PHI@MLB-MIA:2026-07-28",
                source_market_id="book_a-other",
            )
        ]
        quotes = [*_slate("book_a", games=[("a1", GAME_1)]), *other]
        reconciled, changes = reconcile_event_keys(quotes)
        assert changes == []
        assert {q.event_key for q in reconciled} == {KEY_1, "MLB-PHI@MLB-MIA:2026-07-28"}

    def test_different_sports_do_not_interfere(self) -> None:
        """Two fixtures in different sports can never be fused, because the
        participant keys they group on are namespaced."""
        soccer = [
            make_quote(
                source="book_a",
                sport=Sport.SOCCER,
                league="EPL",
                source_event_id="s1",
                event_key="SOCCER-arsenal@SOCCER-chelsea:2026-07-28",
                home_participant="SOCCER-chelsea",
                away_participant="SOCCER-arsenal",
                home_team="Chelsea",
                away_team="Arsenal",
                commence_time=GAME_1,
                source_market_id="book_a-s1",
            )
        ]
        reconciled, changes = reconcile_event_keys([*_slate("book_a", games=[("a1", GAME_1)]), *soccer])
        assert changes == []
        assert len({q.event_key for q in reconciled}) == 2

    def test_a_tennis_match_listed_hours_apart_stays_one_event(self) -> None:
        """The tennis analogue of the doubleheader bug, in the opposite
        direction: too tight a tolerance splits one match into two events."""
        a = resolve_open("Ugo Humbert", Sport.TENNIS)
        b = resolve_open("Andres Martin", Sport.TENNIS)
        away, home = orient(a, b, ATP)
        start = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)

        def row(source: str, when: datetime):
            return make_quote(
                source=source,
                sport=Sport.TENNIS,
                league="ATP",
                source_event_id=f"{source}-1",
                event_key=build_event_key(away.key, home.key, when, ATP),
                home_participant=home.key,
                away_participant=away.key,
                home_team=home.name,
                away_team=away.name,
                commence_time=when,
                source_market_id=f"{source}-ml",
            )

        reconciled, _ = reconcile_event_keys(
            [row("book_a", start), row("book_b", start + timedelta(hours=4))]
        )
        assert len({q.event_key for q in reconciled}) == 1


class TestRealFixtureSlate:
    def test_reconciliation_never_moves_a_row_to_a_different_matchup(
        self, all_fixture_quotes
    ) -> None:
        """The invariant, rather than a count of corrections.

        This asserted ``changes == []`` — "the books agreed on the captured
        slate" — which was a fact about *that* capture, not about the code.  It
        held for three books captured minutes apart and stopped holding the
        moment a fifth was captured three hours later: a tennis match whose
        "not before" estimate had moved across midnight legitimately needed its
        date corrected, and the test called that a regression.

        What must be true of every correction, on any slate, is that it moves a
        row *within its own matchup*: reconciliation may change which fixture of
        a pair a row belongs to and which date it is filed under, and may never
        change who is playing.  Getting that wrong is the doubleheader mis-join,
        and it is silent.
        """
        reconciled, changes = reconcile_event_keys(all_fixture_quotes)
        assert len(reconciled) == len(all_fixture_quotes)

        before = {
            (quote.source, quote.source_event_id): (
                quote.away_participant,
                quote.home_participant,
            )
            for quote in all_fixture_quotes
        }
        for change in changes:
            matchup = before[(change.source, change.source_event_id)]
            assert change.now.split(":")[0] == "@".join(matchup), change
            assert change.was != change.now

    def test_every_row_ends_on_a_key_its_own_participants_imply(
        self, all_fixture_quotes
    ) -> None:
        """After reconciliation the key is derived from the row, not inherited."""
        reconciled, _ = reconcile_event_keys(all_fixture_quotes)
        for quote in reconciled:
            matchup, _, rest = quote.event_key.partition(":")
            assert matchup == f"{quote.away_participant}@{quote.home_participant}"
            assert rest[:10].startswith("20"), quote.event_key
