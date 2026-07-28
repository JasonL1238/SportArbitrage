"""Team canonicalization, including every spelling seen in live payloads."""
from __future__ import annotations

import pytest

from src.teams import TEAMS, canonical_team


def _abbr(raw):
    team = canonical_team(raw)
    return team.abbr if team else None


def test_all_thirty_clubs_are_present_and_unique() -> None:
    assert len(TEAMS) == 30
    assert len({team.abbr for team in TEAMS}) == 30
    assert len({team.name for team in TEAMS}) == 30


@pytest.mark.parametrize("team", TEAMS, ids=lambda t: t.abbr)
def test_each_club_resolves_from_its_own_forms(team) -> None:
    assert _abbr(team.name) == team.abbr
    assert _abbr(team.abbr) == team.abbr
    assert _abbr(team.nickname) == team.abbr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Kambi sends city abbreviation + nickname.
        ("CIN Reds", "CIN"),
        ("CLE Guardians", "CLE"),
        ("CHI White Sox", "CWS"),
        ("CHI Cubs", "CHC"),
        ("WAS Nationals", "WSH"),
        ("NY Mets", "NYM"),
        ("NY Yankees", "NYY"),
        ("LA Angels", "LAA"),
        ("LA Dodgers", "LAD"),
        ("STL Cardinals", "STL"),
        ("TB Rays", "TB"),
        ("SD Padres", "SD"),
        ("SF Giants", "SF"),
        ("BOS Red Sox", "BOS"),
        ("ARI Diamondbacks", "ARI"),
        ("KC Royals", "KC"),
        ("TOR Blue Jays", "TOR"),
        ("Athletics", "ATH"),
        # FanDuel appends the probable pitcher.
        ("Philadelphia Phillies (A Nola)", "PHI"),
        ("St. Louis Cardinals (TBD)", "STL"),
        ("Miami Marlins (S Alcantara)", "MIA"),
        # Punctuation and case variants.
        ("st louis cardinals", "STL"),
        ("ST. LOUIS CARDINALS", "STL"),
        # Former names.
        ("Oakland Athletics", "ATH"),
        ("Cleveland Indians", "CLE"),
    ],
)
def test_live_spellings_resolve(raw: str, expected: str) -> None:
    assert _abbr(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Ambiguous between two clubs — must never resolve to one of them.
        "Chicago",
        "New York",
        "Los Angeles",
        "Sox",
        "CHI",
        "NY",
        "LA",
        # Not teams at all: these are what leaked into the team fields before.
        "MLB Futures",
        "MLB Player Awards",
        "MLB Player Markets",
        "American League Cy Young 2026",
        "World Series 2026 - Exact Result",
        "Team to Make Playoffs 2026",
        "Over",
        "Under",
        "5+",
        "Draw",
        "",
        "   ",
    ],
)
def test_non_teams_and_ambiguous_names_do_not_resolve(raw: str) -> None:
    assert canonical_team(raw) is None


def test_none_input_is_safe() -> None:
    assert canonical_team(None) is None
    assert _abbr(None) is None


def test_resolution_prefers_the_longest_matching_suffix() -> None:
    """"CHI White Sox" must land on the White Sox, never on the ambiguous "Sox"
    and never on Chicago's other club."""
    assert _abbr("CHI White Sox") == "CWS"
    assert _abbr("Chicago White Sox") == "CWS"
    assert _abbr("Chicago Cubs") == "CHC"
