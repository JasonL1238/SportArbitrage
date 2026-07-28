"""The league registry, and the per-league facts the pipeline depends on."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.leagues import BY_KEY, LEAGUES, LEAGUES_BY_SPORT, is_known, league
from src.rosters import ROSTERS
from src.vocab import Sport


def test_keys_are_unique() -> None:
    assert len(BY_KEY) == len(LEAGUES)


def test_every_sport_has_at_least_one_league() -> None:
    for sport in Sport:
        assert LEAGUES_BY_SPORT[sport], f"{sport} has no registered league"


def test_lookup_of_an_unknown_league_raises_rather_than_defaulting() -> None:
    assert is_known("MLB")
    assert not is_known("KBO")
    with pytest.raises(KeyError, match="unknown league"):
        league("KBO")


@pytest.mark.parametrize("competition", LEAGUES, ids=lambda c: c.key)
class TestEveryLeague:
    def test_roster_reference_exists(self, competition) -> None:
        if competition.roster is not None:
            assert competition.roster in ROSTERS

    def test_a_closed_roster_league_matches_its_rosters_sport(self, competition) -> None:
        """A league must not point at a roster from another sport."""
        if competition.roster is not None:
            assert competition.roster in ROSTERS
            # The roster name is the league key lowercased for the closed leagues,
            # which is what keeps the participant key namespace legible.
            assert competition.roster == competition.key.lower()

    def test_plausible_total_range_is_ordered_and_positive(self, competition) -> None:
        low, high = competition.plausible_total_range
        assert 0 < low < high

    def test_tolerances_are_positive(self, competition) -> None:
        assert competition.same_event_tolerance > timedelta(0)
        assert competition.max_schedule_horizon > timedelta(0)


class TestHomeAwaySemantics:
    def test_tennis_has_no_home_advantage(self) -> None:
        """Each book orders the two players arbitrarily, so "home" is not a fact
        and src.events imposes its own ordering instead."""
        for competition in LEAGUES_BY_SPORT[Sport.TENNIS]:
            assert not competition.has_home_away, competition.key

    def test_team_sports_do_have_a_home_side(self) -> None:
        for sport in (Sport.BASEBALL, Sport.BASKETBALL, Sport.HOCKEY, Sport.FOOTBALL, Sport.SOCCER):
            for competition in LEAGUES_BY_SPORT[sport]:
                assert competition.has_home_away, competition.key


class TestScheduleHorizon:
    def test_baseball_is_tight_and_the_scheduled_sports_are_not(self) -> None:
        """The rule this replaced was a single 30 days, which would have rejected
        every real NFL and NHL game: the NFL's whole season is priced in July and
        the NHL's openers are two months out."""
        assert league("MLB").max_schedule_horizon <= timedelta(days=31)
        assert league("NFL").max_schedule_horizon >= timedelta(days=200)
        assert league("NHL").max_schedule_horizon >= timedelta(days=200)

    def test_a_real_nfl_fixture_is_inside_the_horizon(self) -> None:
        """Captured live: FanDuel listed Eagles @ Cowboys on 2026-11-26 while the
        collector was run on 2026-07-28 — 121 days ahead."""
        assert timedelta(days=121) < league("NFL").max_schedule_horizon

    def test_an_absurd_date_is_still_outside_it(self) -> None:
        """Captured live: FanDuel's "NFL Specials" container carries an openDate of
        2030-12-01, which is what this guard still has to catch."""
        assert timedelta(days=1587) > league("NFL").max_schedule_horizon


class TestTotalRanges:
    def test_ranges_reflect_the_sports_scoring(self) -> None:
        """A units error or a market mapped to the wrong sport shows up here: an
        8000-run baseball total and a 2.5-point NFL total are both impossible."""
        assert not _in_range("MLB", 8000.0)
        assert _in_range("MLB", 8.5)
        assert not _in_range("NFL", 2.5)
        assert _in_range("NFL", 44.5)
        assert _in_range("EPL", 2.5)
        assert not _in_range("EPL", 44.5)
        assert _in_range("WNBA", 165.5)
        assert _in_range("NHL", 6.0)

    def test_no_two_sports_ranges_are_interchangeable(self) -> None:
        """Soccer and basketball totals must not both be plausible for one number,
        or the check cannot catch a sport mix-up."""
        assert not _in_range("EPL", 165.5)
        assert not _in_range("WNBA", 2.5)


def _in_range(key: str, total: float) -> bool:
    low, high = league(key).plausible_total_range
    return low <= total <= high
