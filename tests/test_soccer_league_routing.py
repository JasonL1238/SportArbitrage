"""Every adapter's soccer competition routing, and the collisions it must refuse.

Four adapters classified soccer competitions by unanchored substring against a
marker table — BetMGM and Cloudbet carried the *same* table byte for byte — and
a fifth, Smarkets, had a country-prefixed table holding un-prefixed keys, so it
matched nothing at all. The failure mode is identical in every case: a
competition of the same name in another country, or a second tier of the same
name in the same country, lands on a senior league key.

Nothing here is invented. Every string is a competition one of the tracked
captures or the 2026-08-14 Illinois run actually carried, except where a case is
marked as a shape the venue's own catalogue proves exists.

League is not part of ``event_key``, so none of this loses or mis-joins a row —
what it corrupts is the league filter, the coverage grid and every per-league
count. The exception is the women's tier, where the *competition* carries the
distinction and the club names do not; that one is identity, and it is why every
adapter here also applies ``competition_marker``.
"""
from __future__ import annotations

import pytest

from src.sources.betmgm import COMPETITION_LEAGUE as BETMGM_IDS
from src.sources.betmgm import SOCCER_CATCH_ALL as BETMGM_CATCH_ALL
from src.sources.cloudbet import _competition_league as cloudbet_league
from src.sources.hardrock import _competition_league as hardrock_league
from src.sources.onexbet import league_from_label as onexbet_league
from src.sources.smarkets import _classify as smarkets_classify

OTHER = "SOCCER_OTHER"


def club_key(name: str) -> str | None:
    """The soccer participant slug a book's spelling resolves to."""
    from src.participants import _open_slug
    from src.vocab import Sport

    return _open_slug(name, sport=Sport.SOCCER)


def smarkets_league(competition_slug: str) -> str | None:
    found = smarkets_classify(f"/sport/football/{competition_slug}/a/b")
    return found[1] if found else None


class TestTheSeniorCompetitionsStillRoute:
    """The routing that must not regress while the collisions are closed."""

    @pytest.mark.parametrize(
        "competition_id, league",
        [
            (102841, "EPL"),
            (104417, "MLS"),
            (102829, "LA_LIGA"),
            (102846, "SERIE_A"),
            (102842, "BUNDESLIGA"),
            (102843, "LIGUE_1"),
        ],
    )
    def test_betmgm_routes_by_id(self, competition_id: int, league: str) -> None:
        assert BETMGM_IDS.get(competition_id) == league

    @pytest.mark.parametrize(
        "key, name, league",
        [
            ("soccer-england-premier-league", "Premier League", "EPL"),
            ("soccer-usa-major-league-soccer", "Major League Soccer", "MLS"),
            ("soccer-spain-la-liga", "LaLiga", "LA_LIGA"),
            ("soccer-italy-serie-a", "Serie A", "SERIE_A"),
            ("soccer-germany-bundesliga", "Bundesliga", "BUNDESLIGA"),
            ("soccer-france-ligue-1", "Ligue 1", "LIGUE_1"),
        ],
    )
    def test_cloudbet_routes_by_country_and_name(
        self, key: str, name: str, league: str
    ) -> None:
        assert cloudbet_league({"key": key, "name": name}) == league

    @pytest.mark.parametrize(
        "comp_name, league",
        [
            ("Spain - La Liga", "LA_LIGA"),
            ("France - Ligue 1", "LIGUE_1"),
            ("England - Premier League", "EPL"),
            ("Germany - Bundesliga", "BUNDESLIGA"),
            ("Italy - Serie A", "SERIE_A"),
            ("USA - Major League Soccer", "MLS"),
        ],
    )
    def test_hardrock_routes_by_country_and_name(
        self, comp_name: str, league: str
    ) -> None:
        assert hardrock_league(comp_name, is_soccer=True) == league

    @pytest.mark.parametrize(
        "slug, league",
        [
            ("england-premier-league", "EPL"),
            ("us-major-league-soccer", "MLS"),
            ("spain-la-liga", "LA_LIGA"),
            ("italy-serie-a", "SERIE_A"),
            ("germany-bundesliga", "BUNDESLIGA"),
            ("france-ligue-1", "LIGUE_1"),
        ],
    )
    def test_smarkets_routes_by_its_own_prefixed_slug(
        self, slug: str, league: str
    ) -> None:
        """These six resolved to **nothing** before.

        ``LEAGUE_BY_SLUG`` held ``premier-league`` / ``la-liga`` / ``serie-a``
        while Smarkets' URLs carry ``england-premier-league`` /
        ``spain-la-liga`` / ``italy-serie-a``, so the table matched 0 of 947
        football events on the 2026-08-14 capture and every top-flight fixture
        the largest soccer feed in the run priced was filed as SOCCER_OTHER.
        """
        assert smarkets_league(slug) == league

    @pytest.mark.parametrize(
        "label, league",
        [
            ("England. Premier League", "EPL"),
            ("Spain. La Liga", "LA_LIGA"),
            ("Italy. Serie A", "SERIE_A"),
            ("Germany. Bundesliga", "BUNDESLIGA"),
            ("France. Ligue 1", "LIGUE_1"),
            ("USA. MLS", "MLS"),
        ],
    )
    def test_onexbet_routes_by_country_prefixed_label(
        self, label: str, league: str
    ) -> None:
        assert onexbet_league(label) == league


class TestAnotherCountrysCompetitionOfTheSameName:
    """The collision every one of these tables had. Each is a real listing."""

    def test_cloudbet(self) -> None:
        for key, name in (
            ("soccer-iceland-premier-league", "Premier League"),
            ("soccer-wales-premier-league", "Premier League"),
            ("soccer-brazil-serie-a", "Brasileiro Serie A"),
            ("soccer-austria-bundesliga", "Bundesliga"),
        ):
            assert cloudbet_league({"key": key, "name": name}) == OTHER, key

    def test_hardrock(self) -> None:
        """``Brazil - Serie A`` was filed as Italy's SERIE_A on 19 fixtures of
        the 2026-08-14 run, and ``league_disagreement`` had been saying so."""
        for comp_name in (
            "Brazil - Serie A",
            "Wales - Premier League",
            "Austria - Bundesliga",
            "UEFA - Europa League",
            "Mexico - Liga MX",
        ):
            assert hardrock_league(comp_name, is_soccer=True) == OTHER, comp_name

    def test_onexbet(self) -> None:
        for label in (
            "Brazil. Campeonato Brasileiro. Serie A",
            "Wales. Premier League",
            "Austria. Bundesliga",
            "Ecuador. Serie A",
            "Bhutan. Premier League",
        ):
            assert onexbet_league(label) is None, label

    def test_smarkets(self) -> None:
        for slug in ("brazil-serie-a", "england-championship", "japan-j-league"):
            assert smarkets_league(slug) == OTHER, slug

    def test_betmgm(self) -> None:
        """Five competitions named "Premier League" reached EPL, which reported
        29 fixtures on a slate holding 10."""
        for competition_id in (102838, 102839, 102809, 102782, 999_999):
            assert BETMGM_IDS.get(competition_id, BETMGM_CATCH_ALL) == BETMGM_CATCH_ALL


class TestTheSecondTierOfTheSameCountry:
    """The half a lone ``"2."`` guard covered for Bundesliga and nothing else."""

    def test_cloudbet(self) -> None:
        for key, name in (
            ("soccer-germany-2-bundesliga", "2. Bundesliga"),
            ("soccer-spain-la-liga-2", "LaLiga 2"),
            ("soccer-france-ligue-2", "Ligue 2"),
            ("soccer-england-premier-league-2", "Premier League 2"),
        ):
            assert cloudbet_league({"key": key, "name": name}) == OTHER, key

    def test_hardrock(self) -> None:
        for comp_name in (
            "Spain - La Liga 2",
            "France - Ligue 2",
            "Brazil - Serie B",
            "Germany - 2. Bundesliga",
            "Argentina - Primera B Nacional",
        ):
            assert hardrock_league(comp_name, is_soccer=True) == OTHER, comp_name

    def test_onexbet(self) -> None:
        """1xBet's own catalogue proves the shape: it carries "New South Wales
        Premier League 1/2", "Victoria Premier League 1 U23" and "Ontario
        Premier League 1/2", safe today only because Australia and Canada are
        not anchored countries."""
        for label in (
            "England. Premier League 2",
            "England. Premier League 2. Division 1",
            "Spain. La Liga 2",
            "Italy. Serie A U19",
            "Germany. 2. Bundesliga",
        ):
            assert onexbet_league(label) is None, label

    def test_smarkets(self) -> None:
        for slug in ("spain-la-liga-2", "italy-serie-b", "germany-3-liga", "england-league-1"):
            assert smarkets_league(slug) == OTHER, slug

    def test_betmgm(self) -> None:
        for competition_id in (102830, 102845, 102376, 102848, 102361):
            assert BETMGM_IDS.get(competition_id, BETMGM_CATCH_ALL) == BETMGM_CATCH_ALL


class TestAWomensCompetitionNeverLandsOnTheMensKey:
    """The one case in this file that is identity, not labelling.

    BetMGM's table read ``Frauen-Bundesliga`` as ``BUNDESLIGA`` — live, at an
    adapter the women's-marker test recorded as *immune* to exactly this. The
    league key is the second line of defence; ``competition_marker`` on the club
    names is the first, and every adapter here now applies it.
    """

    def test_cloudbet(self) -> None:
        assert (
            cloudbet_league(
                {"key": "soccer-germany-frauen-bundesliga", "name": "Frauen-Bundesliga"}
            )
            == OTHER
        )

    def test_hardrock(self) -> None:
        assert hardrock_league("Germany - Frauen Bundesliga", is_soccer=True) == OTHER
        assert hardrock_league("England - Premier League U21", is_soccer=True) == OTHER


class TestNothingIsDroppedForBeingUnrecognised:
    """Every adapter with a bulk fetch keeps the fixture; 1xBet deliberately does not."""

    def test_cloudbet_and_hardrock_catch_everything(self) -> None:
        assert cloudbet_league({"key": "soccer-bhutan-a-division", "name": "A Division"}) == OTHER
        assert hardrock_league("Denmark - Superliga", is_soccer=True) == OTHER

    def test_onexbet_keeps_its_drop_on_purpose(self) -> None:
        """1xBet fetches one request per competition and listed **847** soccer
        competitions on 2026-08-14 against the 20 it collects. A catch-all would
        make one pass ~830 requests at a venue that served a CAPTCHA on its
        baseball scope in that same run, so the drop stays a counted skip.
        """
        assert onexbet_league("Denmark. Superliga") is None


# ── club identity: the spellings that must merge, and the ones that must not ──


class TestOneClubUnderTwoSpellings:
    """Splits measured on the 2026-08-14 Illinois run, each with a US-bettable
    book stranded on one side.

    A split is the most expensive identity failure this pipeline has: the two
    halves never enter the same ``event_key``, so no cross-source check can fire
    on them — ``participant_pair_disagreement`` groups by ``event_key`` and
    ``_check_orientation`` groups by the participant-key set, and a split differs
    in both — and the dashboard shows two thin games instead of one deep one.
    """

    @pytest.mark.parametrize(
        "left, right",
        [
            # Italian and Spanish legal forms.
            ("Parma", "Parma Calcio"),
            ("Udinese", "Udinese Calcio"),
            ("Frosinone", "Frosinone Calcio"),
            ("Cagliari", "Cagliari Calcio"),
            ("Lazio", "SS Lazio"),
            ("Lecce", "US Lecce"),
            ("Venezia", "Unione Venezia"),
            ("Atalanta", "Atalanta BC"),
            # Founding year in trailing position.
            ("Como", "Como 1907"),
            ("Bologna", "Bologna 1909"),
            # Two digits, decided by alias because no rule can tell them from
            # Schalke 04.
            ("Elversberg", "SV 07 Elversberg"),
            ("Leverkusen", "Bayer 04 Leverkusen"),
            ("Paderborn", "Paderborn 07"),
            # French club-type affixes.
            ("Lille", "Lille OSC"),
            ("Angers", "Angers SCO"),
            # BetRivers' Kambi tenant is the only source that abbreviates an MLS
            # city; the other tenant of the same platform writes it out.
            ("CHI Fire", "Chicago Fire"),
            ("COL Crew", "Columbus Crew"),
            ("COL Rapids", "Colorado Rapids"),
            ("HOU Dynamo", "Houston Dynamo"),
            ("MIN United", "Minnesota United"),
            ("NE Revolution", "New England Revolution"),
            ("NYCFC", "New York City"),
            ("ORL City", "Orlando City"),
            ("PHI Union", "Philadelphia Union"),
            ("POR Timbers", "Portland Timbers"),
            ("SEA Sounders", "Seattle Sounders"),
            ("SJ Earthquakes", "San Jose Earthquakes"),
            ("Sporting KC", "Sporting Kansas City"),
            ("VAN Whitecaps", "Vancouver Whitecaps"),
            ("LA Galaxy", "Los Angeles Galaxy"),
        ],
    )
    def test_the_two_spellings_are_one_club(self, left: str, right: str) -> None:
        assert club_key(left) == club_key(right)


class TestTwoClubsAreNeverFused:
    """The refusals every rule above had to be written around.

    Each is a live counterexample recorded in ``src/participants.py``, and the
    bias is one-directional on purpose: an unmade join costs a missed market,
    while a false one puts two clubs' prices on a single fixture.
    """

    @pytest.mark.parametrize(
        "left, right, why",
        [
            ("Schalke 04", "Schalke", "a two-digit founding year is never stripped"),
            ("1860 Munich", "Munich", "a leading year is part of the club's name"),
            ("Botafogo RJ", "Botafogo SP", "a Brazilian state code separates two clubs"),
            ("Sturm Graz II", "Sturm Graz B", "II and B are ordinals a club fields at once"),
            ("Real Madrid", "Real Madrid B", "the reserve side is not the first team"),
            ("Freiburg", "Freiburg W", "the women's side is a different fixture"),
            ("Houston Dynamo", "Houston Dynamo 2", "MLS Next Pro is not the first team"),
            ("Los Angeles Galaxy", "Los Angeles Galaxy II", "same"),
            ("Inter", "Inter Miami", "short into long would fuse these"),
            ("Paris FC", "Paris Saint-Germain", "same"),
            ("Racing Club", "Racing Santander", "same"),
            ("Deportivo", "Deportivo Pasto", "same, and why bare Deportivo has no alias"),
            ("Tigre", "Tigres FC Zipaquira", "same"),
            ("Bayern Munich", "Bayer Leverkusen", "'bayer' is stripped, 'bayern' is a different token"),
            ("Real Madrid", "Real Betis", "'Real' is identity, not a club type"),
        ],
    )
    def test_they_stay_apart(self, left: str, right: str, why: str) -> None:
        assert club_key(left) != club_key(right), why


# ── 1xBet's phantom fixtures ─────────────────────────────────────────────────


class TestOnexbetDoesNotInventFixtures:
    """Two shapes reached ``canonical_participant``, which for soccer resolves
    anything at all, and became fixtures carrying real prices.

    On the 2026-08-14 Illinois run they were **24 of 1xBet's 259 event keys**,
    every one of them a single-source card on the dashboard:

    * 20 from the competition ``"Spain. La Liga. Team vs Player"`` — a
      player-prop shelf whose country prefix made ``league_from_label`` file it
      as LA_LIGA. Malaga alone spawned five, "vs Julian Alvarez", "vs Lookman".
    * 4 built from the literal strings ``Home`` and ``Away``.
    """

    def test_a_player_prop_shelf_is_not_a_competition(self) -> None:
        for label in (
            "Spain. La Liga. Team vs Player",
            "UEFA Champions League. Team vs Player",
        ):
            assert onexbet_league(label) is None, label
        # And the real competition it was hiding behind still routes.
        assert onexbet_league("Spain. La Liga") == "LA_LIGA"

    def test_an_aggregate_row_is_not_a_fixture(self) -> None:
        """``DI`` is 1xBet's own "aggregates N matches" descriptor.

        Across every captured 1xBet payload it appears on exactly 16 events and
        not one is a fixture — the names on those rows are ``Home``/``Away``,
        ``Home (Points)``/``Away (Points)``, ``Home (Runs)``/``Away (Runs)``. The
        screen only caught the parenthesised ones.

        Deliberately not solved by widening ``is_statistic``: a test asserts
        ``not is_statistic("Away")`` under the comment "And it must not catch a
        competitor", and five other adapters share that predicate.
        """
        import glob

        from src.raw_store import RawStore
        from src.sources.onexbet import parse_onexbet

        paths = sorted(glob.glob("tests/fixtures/raw/onexbet__*.json"))
        assert paths, "the onexbet captures are missing"
        store = RawStore("tests/fixtures/raw")
        outcome = parse_onexbet([store.read(path) for path in paths])

        invented = [
            quote.event_key
            for quote in outcome.quotes
            if "SOCCER-home" in quote.event_key or "SOCCER-away" in quote.event_key
        ]
        assert invented == [], invented
        assert outcome.rejections == []
