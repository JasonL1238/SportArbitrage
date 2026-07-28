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


class TestClusteringVersusReporting:
    """Joining a fixture and noticing a bad clock are different questions.

    Both were once answered by one number, which forced a choice between them: a
    window wide enough to join a fixture two books time differently is also wide
    enough to stop reporting that they differ.  Keeping them apart gets both.
    """

    @pytest.mark.parametrize("competition", LEAGUES, ids=lambda c: c.key)
    def test_reporting_is_never_looser_than_joining(self, competition) -> None:
        """A gap that splits a fixture into two events is always worth reporting,
        so the reporting threshold must sit inside the clustering window."""
        assert competition.time_disagreement_threshold <= competition.same_event_tolerance

    def test_baseball_alone_keeps_a_tight_clustering_window(self) -> None:
        """Only baseball plays the same pair twice in a day, so only baseball has
        a genuinely different fixture close enough for a wide window to swallow."""
        assert league("MLB").same_event_tolerance <= timedelta(minutes=90)
        for key in ("WNBA", "NHL", "NFL", "EPL", "ATP"):
            assert league(key).same_event_tolerance > timedelta(minutes=90), key

    def test_soccer_joins_a_three_hour_disagreement_and_still_reports_it(self) -> None:
        """The live case: Kambi listed Dortmund v Hamburger at 13:30Z where
        FanDuel and Pinnacle both said 16:30Z.  Under a 90-minute window that
        split in two, so Kambi never joined the other two and the pair gained a
        `#2` ordinal for a fixture that does not exist."""
        epl = league("EPL")
        gap = timedelta(hours=3)
        assert gap <= epl.same_event_tolerance, "must still be treated as one fixture"
        assert gap > epl.time_disagreement_threshold, "must still be reported"

    def test_tennis_treats_an_hours_long_gap_as_normal(self) -> None:
        """A tennis match starts when the previous match on court ends, so each
        book publishes its own estimate and being hours out is the feed working.
        Reporting it would put a finding on nearly every match."""
        atp = league("ATP")
        gap = timedelta(hours=4)
        assert gap <= atp.same_event_tolerance
        assert gap <= atp.time_disagreement_threshold, "should not be reported"

    @pytest.mark.parametrize("competition", LEAGUES_BY_SPORT[Sport.TENNIS], ids=lambda c: c.key)
    def test_every_tennis_tour_agrees_on_both_thresholds(self, competition) -> None:
        """All five tours are the same product; one of them silently keeping the
        team-sport default would report every match on that tour."""
        assert competition.same_event_tolerance == league("ATP").same_event_tolerance
        assert (
            competition.time_disagreement_threshold
            == league("ATP").time_disagreement_threshold
        )
