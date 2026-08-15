"""BetMGM's league routing, its catch-all, and the two traps that hid behind it.

BetMGM had no focused test file at all — it was covered only by the
registry-parameterised contract and replay suites, which is why a soccer feed
carrying 109 competitions could be classified by unanchored substring for as
long as it was.  Everything here is measured against the tracked capture or the
2026-08-14 Illinois run, never invented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.raw_store import RawResponse
from src.sources.betmgm import COMPETITION_LEAGUE, SOCCER_CATCH_ALL, parse_betmgm

FIXTURES = Path(__file__).parent / "fixtures" / "raw"


def _league(competition_id: int) -> str:
    """What ``_competition`` resolves a soccer competition id to."""
    return COMPETITION_LEAGUE.get(competition_id) or SOCCER_CATCH_ALL


@pytest.fixture(scope="module")
def outcome():
    raws = [
        RawResponse.from_envelope(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(FIXTURES.glob("betmgm__*.json"))
    ]
    assert raws, "the betmgm capture is missing"
    return parse_betmgm(raws)


class TestSoccerIsRoutedByIdAndNeverBySubstring:
    """Every one of these was a live mis-classification on 2026-08-14.

    The adapter matched competition names by unanchored substring, so the same
    feed that carries "LaLiga" beside "LaLiga 2" and "Serie A" beside
    "Brasileiro Serie A" filed both members of each pair on the senior key.  Ids
    make the collisions unreachable rather than guarded case by case, which is
    what Pinnacle and Matchbook already say in their own tables.
    """

    def test_the_senior_competitions_route(self) -> None:
        assert _league(102841) == "EPL"  # England - Premier League
        assert _league(104417) == "MLS"
        assert _league(102846) == "SERIE_A"  # Italy
        assert _league(102842) == "BUNDESLIGA"
        assert _league(102843) == "LIGUE_1"

    def test_la_liga_is_reachable_at_all(self) -> None:
        """BetMGM writes it as one word, "LaLiga", and the marker was "la liga".

        ``LA_LIGA`` was declared in ``SPORT_SCOPES`` and could not be produced
        from the soccer feed by any input — a configured league permanently at
        zero, which ``docs/INPUT_CONTRACT.md`` calls out as the thing an honest
        ``leagues`` list must not do.
        """
        assert _league(102829) == "LA_LIGA"

    def test_the_second_tiers_stay_out_of_the_senior_key(self) -> None:
        for competition_id, name in (
            (102830, "LaLiga 2"),
            (102845, "2. Bundesliga"),
            (102376, "Ligue 2"),
            (102848, "Serie B"),
            (102361, "Brasileiro Serie B"),
        ):
            assert _league(competition_id) == SOCCER_CATCH_ALL, name

    def test_a_different_countrys_competition_of_the_same_name_is_not_the_senior_one(
        self,
    ) -> None:
        """"Brasileiro Serie A" is not Serie A, and five competitions called
        "Premier League" are not the English one.  The substring table reported
        29 EPL fixtures on a slate that held 10."""
        assert _league(102838) == SOCCER_CATCH_ALL  # Brasileiro Serie A
        for unrouted in (102839, 102809, 102782, 102811, 102540, 102234):
            assert _league(unrouted) == SOCCER_CATCH_ALL

    def test_an_unknown_competition_is_kept_rather_than_dropped(self) -> None:
        """The whole point of the catch-all.  BetMGM returned 712 soccer
        fixtures on 2026-08-14 and discarded 599 of them for having an
        unrecognised competition; 333 were fixtures other books in the same run
        had priced, and 109 of those were showing exactly one source."""
        assert _league(-1) == SOCCER_CATCH_ALL
        assert _league(999_999) == SOCCER_CATCH_ALL


class TestTheCaptureParsesCleanly:
    def test_the_catch_all_admits_competitions_that_used_to_be_dropped(
        self, outcome
    ) -> None:
        leagues = {quote.league for quote in outcome.quotes}
        assert SOCCER_CATCH_ALL in leagues, (
            "the tracked capture carries UEFA Europa League, which was dropped "
            "before the catch-all existed"
        )
        assert "LA_LIGA" in leagues

    def test_nothing_is_rejected(self, outcome) -> None:
        """A futures entry listed as a fixture is a skip, not a rejection.

        BetMGM's Belgian second tier lists "RSC Anderlecht Futures" as a
        participant.  Admitting that competition turned one out-of-scope row
        into a standing ``source_unhealthy:rejections`` warning until
        ``is_futures`` screened it the way ``is_pairing`` and ``is_statistic``
        already screened their own cases.
        """
        assert outcome.rejections == [], "; ".join(
            f"{r.reason}: {r.detail}" for r in outcome.rejections[:5]
        )

    def test_every_soccer_fixture_lands_on_one_side_per_selection(
        self, outcome
    ) -> None:
        """The duplicate that exposed the ``sourceName`` inversion.

        BetMGM prices "FC Dynamo Kiev" on a fixture whose participant it spells
        "FC Dynamo Kyiv", so the label match fails and the option falls through
        to the ``sourceName`` "1"/"2" rule.  That rule was applied in the US
        "Away at Home" direction unconditionally, so on soccer's 1X2 the away
        price landed on the home key — visible only as a duplicate key, and only
        because the home option had already claimed it.
        """
        seen: set[tuple[str, ...]] = set()
        for quote in outcome.quotes:
            key = (
                quote.event_key,
                quote.market.value,
                quote.period.value,
                quote.selection,
                str(quote.side),
                str(quote.line),
            )
            assert key not in seen, key
            seen.add(key)
