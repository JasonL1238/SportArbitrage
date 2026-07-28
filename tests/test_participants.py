"""Participant identity, including every spelling seen in live payloads.

The cases below are not invented.  Each cross-book pair was taken from a live
capture of the three books on 2026-07-28, which is why they look arbitrary:
Kambi really does send ``"VGS Golden Knights"`` for a club whose own
abbreviation is VGK, and ``"LA Chargers"`` and ``"LA Rams"`` for two different
clubs in the same city.

The negative cases matter more than the positive ones.  A missed match costs a
join; a false match invents an event, and the prices of two unrelated events are
unconstrained, which is exactly the shape of a phantom arbitrage.
"""
from __future__ import annotations

import pytest

from src.leagues import LEAGUES, league
from src.participants import (
    canonical_participant,
    resolve_open,
    resolve_roster,
    roster_members,
)
from src.rosters import ROSTERS
from src.vocab import Sport


def _abbr(raw, roster: str = "mlb"):
    found = resolve_roster(raw, roster)
    return found.abbr if found else None


# ── closed rosters ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("roster", "expected_size"),
    [("mlb", 30), ("nfl", 32), ("nhl", 32), ("nba", 30), ("wnba", 15)],
)
def test_roster_sizes_and_uniqueness(roster: str, expected_size: int) -> None:
    rows = ROSTERS[roster]
    assert len(rows) == expected_size
    assert len({abbr for abbr, *_ in rows}) == expected_size, "abbreviations must be unique"
    assert len({name for _, name, *_ in rows}) == expected_size, "names must be unique"


@pytest.mark.parametrize("roster", sorted(ROSTERS))
def test_every_club_resolves_from_its_own_forms(roster: str) -> None:
    for member in roster_members(roster):
        assert _abbr(member.name, roster) == member.abbr, member.name
        assert _abbr(member.abbr, roster) == member.abbr, member.abbr


@pytest.mark.parametrize("roster", sorted(ROSTERS))
def test_participant_keys_are_namespaced_by_roster(roster: str) -> None:
    """Keys must be globally unique so a single event_key namespace is safe."""
    for member in roster_members(roster):
        assert member.key == f"{roster.upper()}-{member.abbr}"
        assert member.roster == roster


def test_rosters_are_not_merged_into_one_index() -> None:
    """The cross-league collisions that make a global index unsafe.

    "Rangers" is Texas in baseball and New York in hockey; "Panthers" is Carolina
    in football and Florida in hockey; "Kings" is Los Angeles in hockey and
    Sacramento in basketball; "Jets" is New York in football and Winnipeg in
    hockey.  Each must resolve *within its own league* and never leak across.
    """
    assert _abbr("Rangers", "mlb") == "TEX"
    assert _abbr("Rangers", "nhl") == "NYR"
    assert _abbr("Panthers", "nfl") == "CAR"
    assert _abbr("Panthers", "nhl") == "FLA"
    assert _abbr("Kings", "nhl") == "LA"
    assert _abbr("Kings", "nba") == "SAC"
    assert _abbr("Jets", "nfl") == "NYJ"
    assert _abbr("Jets", "nhl") == "WPG"
    # And a club from the wrong league does not resolve at all.
    assert resolve_roster("Cincinnati Reds", "nfl") is None
    assert resolve_roster("Dallas Cowboys", "mlb") is None


@pytest.mark.parametrize(
    ("raw", "expected", "roster"),
    [
        # ── baseball: Kambi sends city abbreviation + nickname ────────────────
        ("CIN Reds", "CIN", "mlb"),
        ("CLE Guardians", "CLE", "mlb"),
        ("CHI White Sox", "CWS", "mlb"),
        ("CHI Cubs", "CHC", "mlb"),
        ("WAS Nationals", "WSH", "mlb"),
        ("NY Mets", "NYM", "mlb"),
        ("NY Yankees", "NYY", "mlb"),
        ("LA Angels", "LAA", "mlb"),
        ("LA Dodgers", "LAD", "mlb"),
        ("Athletics", "ATH", "mlb"),
        # FanDuel appends the probable pitcher.
        ("Philadelphia Phillies (A Nola)", "PHI", "mlb"),
        ("St. Louis Cardinals (TBD)", "STL", "mlb"),
        # A doubleheader arrives with a game suffix.
        ("White Sox - Game 2", "CWS", "mlb"),
        # Punctuation, case, former names.
        ("st louis cardinals", "STL", "mlb"),
        ("ST. LOUIS CARDINALS", "STL", "mlb"),
        ("Oakland Athletics", "ATH", "mlb"),
        ("Cleveland Indians", "CLE", "mlb"),
        # ── football: Kambi abbreviation + nickname, incl. two LA clubs ───────
        ("LA Chargers", "LAC", "nfl"),
        ("LA Rams", "LAR", "nfl"),
        ("NY Giants", "NYG", "nfl"),
        ("NY Jets", "NYJ", "nfl"),
        ("SF 49ers", "SF", "nfl"),
        ("JAX Jaguars", "JAX", "nfl"),
        ("WAS Commanders", "WAS", "nfl"),
        ("GB Packers", "GB", "nfl"),
        ("TB Buccaneers", "TB", "nfl"),
        ("Washington Football Team", "WAS", "nfl"),
        ("Oakland Raiders", "LV", "nfl"),
        # ── hockey ───────────────────────────────────────────────────────────
        ("VGS Golden Knights", "VGK", "nhl"),
        ("Vegas Golden Knights", "VGK", "nhl"),
        ("NY Rangers", "NYR", "nhl"),
        ("MTL Canadiens", "MTL", "nhl"),
        ("TOR Maple Leafs", "TOR", "nhl"),
        ("LA Kings", "LA", "nhl"),
        ("Arizona Coyotes", "UTA", "nhl"),
        # ── basketball ───────────────────────────────────────────────────────
        ("Golden State Valkyries", "GSV", "wnba"),
        ("Toronto Tempo", "TOR", "wnba"),
        ("Connecticut Sun", "CON", "wnba"),
        ("LA Clippers", "LAC", "nba"),
        ("Philadelphia 76ers", "PHI", "nba"),
    ],
)
def test_live_spellings_resolve(raw: str, expected: str, roster: str) -> None:
    assert _abbr(raw, roster) == expected


@pytest.mark.parametrize(
    ("raw", "roster"),
    [
        # Ambiguous between two clubs in the same league.
        ("Chicago", "mlb"),
        ("New York", "mlb"),
        ("Los Angeles", "mlb"),
        ("Sox", "mlb"),
        ("CHI", "mlb"),
        ("NY", "mlb"),
        ("LA", "mlb"),
        ("New York", "nfl"),
        ("Los Angeles", "nfl"),
        ("LA", "nfl"),
        ("New York", "nhl"),
        ("Los Angeles", "nba"),
        # Futures containers — what leaked into the participant fields before.
        ("MLB Futures", "mlb"),
        ("MLB Player Awards", "mlb"),
        ("American League Cy Young 2026", "mlb"),
        ("World Series 2026 - Exact Result", "mlb"),
        ("NFL Futures", "nfl"),
        ("NFL Specials", "nfl"),
        ("NFL Draft", "nfl"),
        ("NHL Specials", "nhl"),
        ("NHL Awards", "nhl"),
        ("NBA Player Awards", "nba"),
        ("WNBA Futures", "wnba"),
        # A whole matchup must resolve to neither side.
        ("Chicago White Sox at Chicago Cubs", "mlb"),
        ("Dallas Cowboys @ New York Giants", "nfl"),
        ("Florida Panthers @ Carolina Hurricanes", "nhl"),
        # Selections and junk.
        ("Over", "mlb"),
        ("Under", "mlb"),
        ("5+", "mlb"),
        ("Draw", "mlb"),
        ("", "mlb"),
        ("   ", "mlb"),
    ],
)
def test_non_participants_and_ambiguous_names_do_not_resolve(raw: str, roster: str) -> None:
    assert resolve_roster(raw, roster) is None


def test_none_input_is_safe() -> None:
    assert resolve_roster(None, "mlb") is None
    assert resolve_open(None, Sport.SOCCER) is None
    assert canonical_participant(None, league("MLB")) is None


def test_unknown_roster_is_safe() -> None:
    assert resolve_roster("Cincinnati Reds", "cricket") is None


# ── open rosters: soccer clubs ───────────────────────────────────────────────


def _same(a: str, b: str, sport: Sport) -> bool:
    left, right = resolve_open(a, sport), resolve_open(b, sport)
    return left is not None and right is not None and left.key == right.key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Club-type affixes are added and dropped inconsistently for one club.
        ("FC Zurich", "Zurich"),
        ("FC Tulsa", "Tulsa"),
        ("Kapfenberger SV", "Kapfenberger"),
        ("AC Milan", "Milan"),
        # Curated short forms that share no tokens with the full name.
        ("Wolves", "Wolverhampton Wanderers"),
        ("Man Utd", "Manchester United"),
        ("Spurs", "Tottenham"),
        ("PSG", "Paris Saint-Germain"),
        # Accents differ between books.
        ("Atletico Madrid", "Atlético Madrid"),
    ],
)
def test_soccer_club_spellings_that_must_agree(a: str, b: str) -> None:
    assert _same(a, b, Sport.SOCCER), f"{a!r} should resolve the same as {b!r}"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # A reserve side is NOT the senior side.  Merging these pairs a reserve
        # fixture at one book with the first-team fixture at another.
        ("FK Transinvest", "FK Transinvest II"),
        ("CSKA Sofia", "CSKA Sofia II"),
        ("Real Madrid", "Real Madrid B"),
        # A women's side is not the men's side.
        ("Freiburg", "Freiburg (W)"),
        ("FC Zurich", "FC Zurich Women"),
        # An age-group side is not the senior side.
        ("Benfica", "Benfica U21"),
        # Genuinely different clubs that normalization must not fuse.
        ("Real Madrid", "Atletico Madrid"),
        ("Sporting CP", "Sporting Kansas City"),
        ("Manchester United", "Manchester City"),
    ],
)
def test_soccer_clubs_that_must_stay_distinct(a: str, b: str) -> None:
    assert not _same(a, b, Sport.SOCCER), f"{a!r} must NOT resolve the same as {b!r}"


# ── open rosters: tennis players ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Books order a player's names differently; neither ordering is authoritative.
        ("Ugo Humbert", "Humbert Ugo"),
        ("Xiyu Wang", "Wang Xiyu"),
        ("Qinwen Zheng", "Zheng Qinwen"),
        # Accents and internal capitalisation differ between books.
        ("Andrés Martín", "Andres Martin"),
        ("Caty Mcnally", "Caty McNally"),
        # Pinnacle appends a scoring-unit marker to the participant name.
        ("Savannah Dada-Mascoll (Games)", "Savannah Dada-Mascoll"),
        # Hyphens and punctuation.
        ("Gilles Arnaud Bailly", "Gilles-Arnaud Bailly"),
    ],
)
def test_tennis_player_spellings_that_must_agree(a: str, b: str) -> None:
    assert _same(a, b, Sport.TENNIS), f"{a!r} should resolve the same as {b!r}"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Same surname, different players — the classic tennis false match.
        ("Cruz Hewitt", "Lleyton Hewitt"),
        ("Xiyu Wang", "Xinyu Wang"),
        ("Caty Mcnally", "Nicholas Mcnally"),
        ("Alex Michelsen", "Mackenzie McDonald"),
    ],
)
def test_tennis_players_that_must_stay_distinct(a: str, b: str) -> None:
    assert not _same(a, b, Sport.TENNIS), f"{a!r} must NOT resolve the same as {b!r}"


@pytest.mark.parametrize(
    "raw",
    [
        # Kambi puts outright labels in the participant field itself.
        "Atlanta Hawks Markets 2026/2027",
        "Eastern Conference Winner 2026/2027",
        "NBA Championship 2026/2027",
        "NBA Most Valuable Player 2026/2027",
        "Western Conference Markets 2026/2027",
        "Atlantic Division 2026/2027",
        "Top Goalscorer",
        "To Be Relegated",
        "",
        "   ",
    ],
)
def test_open_roster_rejects_outright_labels(raw: str) -> None:
    assert resolve_open(raw, Sport.SOCCER) is None
    assert resolve_open(raw, Sport.TENNIS) is None


def test_open_roster_rejects_names_that_are_only_club_type_tokens() -> None:
    assert resolve_open("FC", Sport.SOCCER) is None
    assert resolve_open("FC SC", Sport.SOCCER) is None
    # ...and names that are only an identity suffix.
    assert resolve_open("II", Sport.SOCCER) is None
    assert resolve_open("(W)", Sport.SOCCER) is None


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_canonical_participant_dispatches_on_the_league() -> None:
    """A closed-roster league resolves against its roster; an open one normalizes."""
    mlb = canonical_participant("CIN Reds", league("MLB"))
    assert mlb is not None and mlb.key == "MLB-CIN" and mlb.roster == "mlb"

    atp = canonical_participant("Ugo Humbert", league("ATP"))
    assert atp is not None and atp.roster is None and atp.key.startswith("TENNIS-")

    epl = canonical_participant("Wolves", league("EPL"))
    assert epl is not None and epl.roster is None and epl.key == "SOCCER-wolverhampton"

    # A closed-roster league refuses a name that is not in its roster, rather
    # than falling back to open-roster normalization.
    assert canonical_participant("Wolverhampton Wanderers", league("MLB")) is None


@pytest.mark.parametrize("competition", LEAGUES, ids=lambda c: c.key)
def test_every_registered_league_can_be_asked(competition) -> None:
    """No league is registered with a roster name that does not exist."""
    if competition.roster is not None:
        assert competition.roster in ROSTERS
        # A closed roster refuses anything outside its membership.
        assert canonical_participant("definitely not a competitor", competition) is None
    else:
        # An open roster has no membership list to check against, so any plausible
        # name normalizes to an identity.  That is the regime, not a bug: scope is
        # the adapter's job, and the outright-label guard is what keeps futures
        # containers out.  What must hold is that it is *deterministic*.
        first = canonical_participant("Some Club Name", competition)
        second = canonical_participant("some club name", competition)
        assert first is not None and second is not None
        assert first.key == second.key
        assert canonical_participant("Eastern Conference Winner 2026/2027", competition) is None
    # The call never raises, whatever it is handed.
    assert canonical_participant("", competition) is None
    assert canonical_participant(None, competition) is None


@pytest.mark.parametrize(
    "raw",
    [
        # Tennis doubles entries name two competitors, not one.
        "R Galloway / E King",
        "Galloway / King",
        "Bolelli & Vavassori",
        "Purcell + Thompson",
    ],
)
def test_a_pairing_of_two_competitors_is_unresolvable(raw: str) -> None:
    """A doubles pairing must not normalize to one identity.

    The open-roster slug is order-independent, which is what lets "Xiyu Wang" and
    "Wang Xiyu" join — applied to a pairing it scrambles four names into one
    token that could collide with a different pairing of the same surnames.  A row
    that can never join is worse than no row: it inflates coverage while
    contributing nothing.
    """
    assert resolve_open(raw, Sport.TENNIS) is None
    assert resolve_open(raw, Sport.SOCCER) is None


def test_singles_names_are_unaffected_by_the_pairing_guard() -> None:
    """The guard requires the separator to be surrounded by spaces, so hyphenated
    and apostrophed names still resolve."""
    for name in ("Gilles-Arnaud Bailly", "Savannah Dada-Mascoll", "Alex de Minaur"):
        assert resolve_open(name, Sport.TENNIS) is not None


def test_the_word_and_is_not_a_pairing_separator() -> None:
    """Only symbol separators count.  "Brighton and Hove Albion" is one club, and
    treating " and " as a pairing rejected a real Premier League fixture outright —
    it was the single `unknown_participant` rejection in a whole live run.

    The cost of the narrower rule is that a doubles pair written "X and Y" would
    resolve; no book in this set writes them that way (they use "/" or "&").
    """
    club = resolve_open("Brighton and Hove Albion", Sport.SOCCER)
    assert club is not None and club.abbr == "brightonandhovealbion"
    assert resolve_open("Brighton and Hove Albion", Sport.SOCCER) != resolve_open(
        "Brighton", Sport.SOCCER
    )
