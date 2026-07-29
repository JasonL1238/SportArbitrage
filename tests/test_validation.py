"""Validation must actually catch corruption, not just pass clean data.

Every test here injects a specific fault into otherwise-valid rows and asserts
that the corresponding check fires.  Without these, a passing validation report
proves nothing.

All rows are **synthetic** — hand-built to hold one specific fault — and none of
them is an observed price.  The real captured slates are exercised elsewhere.

The fixtures below cover every sport collected, because most of what validation
now knows is sport-dependent: a 30-day futures horizon that is right for baseball
rejects every NFL game, a draw that is corruption on a hockey full-game moneyline
is correct on the same fixture's regulation moneyline, and a plausible total is
8.5 runs or 44.5 points depending only on which sport the row claims to be.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.leagues import league as get_league
from src.schema import (
    CORE_MARKETS_BY_SPORT,
    Market,
    Period,
    Quote,
    Selection,
    Side,
    Sport,
    draw_is_priced,
)
from src.validation import Severity, validate
from tests.conftest import make_quote

OBSERVED = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)


# ── synthetic fixtures, one per sport ────────────────────────────────────────


@dataclass(frozen=True)
class Fixture:
    """One synthetic game, in enough detail to build a complete book for it."""

    sport: Sport
    league: str
    home_team: str
    away_team: str
    home_participant: str
    away_participant: str
    commence: datetime
    total: float
    spread: float

    @property
    def event_key(self) -> str:
        competition = get_league(self.league)
        day = self.commence.astimezone(competition.scheduling_tz).date()
        return f"{self.away_participant}@{self.home_participant}:{day.isoformat()}"


MLB_GAME = Fixture(
    sport=Sport.BASEBALL,
    league="MLB",
    home_team="Miami Marlins",
    away_team="Philadelphia Phillies",
    home_participant="MLB-MIA",
    away_participant="MLB-PHI",
    commence=datetime(2026, 7, 28, 22, 41, tzinfo=UTC),
    total=8.5,
    spread=1.5,
)

#: A real NFL game, five months out.  The whole season is priced in July, which
#: is why the futures horizon cannot be a single global constant.
NFL_GAME = Fixture(
    sport=Sport.FOOTBALL,
    league="NFL",
    home_team="Buffalo Bills",
    away_team="Baltimore Ravens",
    home_participant="NFL-BUF",
    away_participant="NFL-BAL",
    commence=datetime(2026, 12, 20, 18, 0, tzinfo=UTC),
    total=44.5,
    spread=3.5,
)

NHL_GAME = Fixture(
    sport=Sport.HOCKEY,
    league="NHL",
    home_team="Boston Bruins",
    away_team="Anaheim Ducks",
    home_participant="NHL-BOS",
    away_participant="NHL-ANA",
    commence=datetime(2026, 9, 29, 23, 0, tzinfo=UTC),
    total=6.5,
    spread=1.5,
)

SOCCER_GAME = Fixture(
    sport=Sport.SOCCER,
    league="EPL",
    home_team="Arsenal",
    away_team="Chelsea",
    home_participant="SOCCER-arsenal",
    away_participant="SOCCER-chelsea",
    commence=datetime(2026, 8, 15, 14, 0, tzinfo=UTC),
    total=2.5,
    spread=0.5,
)

#: Tennis has no home player: src.events.orient sorts the two participants by
#: key, so "away" is simply the lexicographically smaller one.
TENNIS_MATCH = Fixture(
    sport=Sport.TENNIS,
    league="ATP",
    home_team="Ugo Humbert",
    away_team="Carlos Alcaraz",
    home_participant="TENNIS-humbertugo",
    away_participant="TENNIS-alcarazcarlos",
    commence=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    total=22.5,
    spread=3.5,
)

WNBA_GAME = Fixture(
    sport=Sport.BASKETBALL,
    league="WNBA",
    home_team="Chicago Sky",
    away_team="Atlanta Dream",
    home_participant="WNBA-CHI",
    away_participant="WNBA-ATL",
    commence=datetime(2026, 7, 28, 23, 0, tzinfo=UTC),
    total=160.5,
    spread=4.5,
)


def _quote(fixture: Fixture, source: str, **overrides) -> Quote:
    """One synthetic row on *fixture*, with everything identity-related filled in."""
    defaults = dict(
        source=source,
        sport=fixture.sport,
        league=fixture.league,
        event_key=fixture.event_key,
        source_event_id=f"{source}-{fixture.event_key}",
        home_participant=fixture.home_participant,
        away_participant=fixture.away_participant,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        commence_time=fixture.commence,
        observed_at=OBSERVED,
    )
    defaults.update(overrides)
    return make_quote(**defaults)



def _nfl_slate(count: int) -> list[Fixture]:
    """`count` distinct NFL fixtures, drawn from the real roster.

    The per-event coverage rule needs a realistic slate: on a two-game slate one
    absence is 50% and there is no way to tell a partial rename from a book simply
    not posting that market on one game.
    """
    from src.participants import roster_members

    clubs = [m for m in roster_members("nfl")]
    assert 2 * count <= len(clubs), f"only {len(clubs)} NFL clubs available"
    out: list[Fixture] = []
    for index in range(count):
        home, away = clubs[2 * index], clubs[2 * index + 1]
        out.append(
            Fixture(
                sport=Sport.FOOTBALL,
                league="NFL",
                home_team=home.name,
                away_team=away.name,
                home_participant=home.key,
                away_participant=away.key,
                commence=NFL_GAME.commence + timedelta(minutes=30 * index),
                total=41.5 + index,
                spread=2.5,
            )
        )
    return out


def _book(fixture: Fixture, source: str, **overrides) -> list[Quote]:
    """One book pricing every core market of *fixture*'s sport.

    The moneyline is three-way exactly where the sport prices a draw, which is
    what makes this a clean slate for soccer and for hockey regulation without
    special-casing either.
    """
    rows: list[Quote] = []
    core = CORE_MARKETS_BY_SPORT[fixture.sport]
    tag = f"{source}-{fixture.event_key}"

    if Market.MONEYLINE in core:
        prices = [(Selection.HOME, 1.91), (Selection.AWAY, 1.95)]
        if draw_is_priced(fixture.sport, Period.FULL_GAME):
            prices = [(Selection.HOME, 2.40), (Selection.AWAY, 3.30), (Selection.DRAW, 3.40)]
        rows += [
            _quote(
                fixture,
                source,
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{tag}-ml",
                **overrides,
            )
            for selection, odds in prices
        ]
    if Market.SPREAD in core:
        rows += [
            _quote(
                fixture,
                source,
                market=Market.SPREAD,
                selection=selection,
                line=line,
                decimal_odds=odds,
                source_market_id=f"{tag}-sp",
                **overrides,
            )
            for selection, line, odds in (
                (Selection.HOME, -fixture.spread, 1.95),
                (Selection.AWAY, fixture.spread, 1.91),
            )
        ]
    if Market.TOTAL in core:
        rows += [
            _quote(
                fixture,
                source,
                market=Market.TOTAL,
                selection=selection,
                line=fixture.total,
                decimal_odds=odds,
                source_market_id=f"{tag}-tot",
                **overrides,
            )
            for selection, odds in ((Selection.OVER, 1.91), (Selection.UNDER, 1.95))
        ]
    return rows


def _codes(quotes) -> set[str]:
    return {finding.code for finding in validate(quotes).findings}


def _errors(quotes) -> set[str]:
    return {finding.code for finding in validate(quotes).errors}


def _two_source_pair(**overrides):
    """A minimal clean slate: one market priced by two books."""
    return [
        make_quote(source="bookA", selection=Selection.HOME, decimal_odds=1.9),
        make_quote(source="bookA", selection=Selection.AWAY, decimal_odds=2.05),
        make_quote(source="bookB", selection=Selection.HOME, decimal_odds=1.95),
        make_quote(source="bookB", selection=Selection.AWAY, decimal_odds=2.0),
    ]


def test_clean_slate_passes_except_for_expected_coverage_gap() -> None:
    """The minimal slate only has moneylines, so the coverage check must flag
    the missing core markets — proving that check is live."""
    report = validate(_two_source_pair())
    assert {f.code for f in report.errors} == {"core_market_absent"}


def _full_slate(source: str, *, home_odds: float = 1.9) -> list:
    """One book pricing all three core full-game baseball markets."""
    return [
        make_quote(source=source, market=Market.MONEYLINE, selection=Selection.HOME,
                   decimal_odds=home_odds, source_market_id=f"{source}-ml"),
        make_quote(source=source, market=Market.MONEYLINE, selection=Selection.AWAY,
                   decimal_odds=2.05, source_market_id=f"{source}-ml"),
        make_quote(source=source, market=Market.SPREAD, selection=Selection.HOME, line=1.5,
                   decimal_odds=1.5, source_market_id=f"{source}-sp"),
        make_quote(source=source, market=Market.SPREAD, selection=Selection.AWAY, line=-1.5,
                   decimal_odds=2.6, source_market_id=f"{source}-sp"),
        make_quote(source=source, market=Market.TOTAL, selection=Selection.OVER, line=8.5,
                   decimal_odds=1.92, source_market_id=f"{source}-tot"),
        make_quote(source=source, market=Market.TOTAL, selection=Selection.UNDER, line=8.5,
                   decimal_odds=1.9, source_market_id=f"{source}-tot"),
    ]


def test_two_book_full_slate_is_clean() -> None:
    report = validate(_full_slate("bookA") + _full_slate("bookB"))
    assert report.ok, [str(f) for f in report.errors]


def test_negative_overround_is_an_error() -> None:
    """Both sides above evens means the book priced itself to lose, which in
    practice means the parser mispaired prices or lines."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[0] = make_quote(
        source="bookA", market=Market.MONEYLINE, selection=Selection.HOME,
        decimal_odds=2.6, source_market_id="bookA-ml",
    )
    assert "negative_overround" in _codes(quotes)


def test_unmirrored_spread_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[3] = make_quote(
        source="bookA", market=Market.SPREAD, selection=Selection.AWAY, line=1.5,
        decimal_odds=2.6, source_market_id="bookA-sp",
    )
    assert "spread_not_mirrored" in _codes(quotes)


def test_mismatched_total_lines_within_one_market_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[5] = make_quote(
        source="bookA", market=Market.TOTAL, selection=Selection.UNDER, line=9.5,
        decimal_odds=1.9, source_market_id="bookA-tot",
    )
    assert "line_mismatch_within_market" in _codes(quotes)


def test_home_away_disagreement_across_sources_is_an_error() -> None:
    """If two books disagree about which team is home, the join is wrong."""
    quotes = _full_slate("bookA") + [
        make_quote(source="bookB", home_team="Philadelphia Phillies",
                   away_team="Miami Marlins", home_participant="MLB-PHI",
                   away_participant="MLB-MIA", source_market_id="bookB-ml")
    ]
    assert "home_away_disagreement" in _codes(quotes)


def test_start_time_disagreement_across_sources_is_an_error() -> None:
    quotes = _full_slate("bookA")
    late = quotes[0].commence_time + timedelta(hours=3)
    quotes.append(make_quote(source="bookB", commence_time=late, source_market_id="bookB-ml"))
    assert "start_time_disagreement" in _codes(quotes)


def test_conflicting_duplicate_is_an_error() -> None:
    """Two different prices for one selection — how pitcher-conditional
    moneylines leak in as the moneyline."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", market=Market.MONEYLINE, selection=Selection.HOME,
                   decimal_odds=1.7, source_market_id="bookA-ml-conditional")
    )
    assert "conflicting_duplicate" in _codes(quotes)


def test_identical_duplicate_is_only_a_warning() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(quotes[0])
    report = validate(quotes)
    assert "duplicate_quote" in {f.code for f in report.warnings}
    assert "conflicting_duplicate" not in {f.code for f in report.errors}


def test_futures_style_commence_time_is_an_error() -> None:
    """FanDuel futures containers carry openDate 2030/2099; a game does not."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    far = quotes[0].observed_at + timedelta(days=400)
    quotes.append(make_quote(source="bookA", commence_time=far, source_market_id="bookA-fut"))
    assert "commence_time_too_far" in _codes(quotes)


def test_implausible_total_is_an_error() -> None:
    """The Kambi thousandths bug produced a total of 8000 runs."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", market=Market.TOTAL, selection=Selection.OVER,
                   line=8000.0, decimal_odds=1.9, source_market_id="bookA-bad")
    )
    assert "implausible_total" in _codes(quotes)


def test_implausible_spread_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", market=Market.SPREAD, selection=Selection.HOME,
                   line=1500.0, decimal_odds=1.9, source_market_id="bookA-bad-sp")
    )
    assert "line_out_of_range" in _codes(quotes)


def test_source_reported_change_time_after_observation_is_an_error() -> None:
    """Pinnacle's cutoffAt is in the future; using it as a timestamp made fresh
    data look like it came from the future."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    future = quotes[0].observed_at + timedelta(hours=6)
    quotes.append(make_quote(source="bookA", last_change_at=future, source_market_id="bookA-x"))
    assert "last_change_in_future" in _codes(quotes)


def test_unresolved_participant_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(make_quote(source="bookA", home_team="MLB Futures", source_market_id="bookA-y"))
    assert "unresolved_participant" in _codes(quotes)


def test_a_display_name_naming_another_club_is_an_error() -> None:
    """The display name and the join key must describe the same competitor, or
    the row joins onto a game it is not about."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", home_team="New York Mets", source_market_id="bookA-swap")
    )
    assert "participant_key_mismatch" in _codes(quotes)


def test_odds_format_mismatch_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", decimal_odds=1.9, american_odds=500,
                   implied_probability=1 / 1.9, source_market_id="bookA-z")
    )
    assert "odds_format_mismatch" in _codes(quotes)


def test_implied_probability_must_match_decimal_odds() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", decimal_odds=1.9, american_odds=-111,
                   implied_probability=0.25, source_market_id="bookA-w")
    )
    assert "implied_probability_mismatch" in _codes(quotes)


def test_missing_core_market_is_an_error() -> None:
    """Catches an upstream rename that turns a real market into a silent skip."""
    quotes = [q for q in _full_slate("bookA") if q.market is not Market.TOTAL]
    quotes += _full_slate("bookB")
    assert "core_market_absent" in _errors(quotes)


def test_no_shared_events_is_an_error() -> None:
    quotes = _full_slate("bookA")
    other = [
        make_quote(source="bookB", event_key="MLB-ATL@MLB-NYM:2026-07-28",
                   home_team="New York Mets", away_team="Atlanta Braves",
                   home_participant="MLB-NYM", away_participant="MLB-ATL",
                   market=q.market, period=q.period,
                   selection=q.selection, side=q.side, line=q.line,
                   decimal_odds=q.decimal_odds,
                   source_market_id=f"bookB-{q.market.value}")
        for q in _full_slate("bookB")
    ]
    assert "no_shared_events" in _codes(quotes + other)


def test_empty_input_is_an_error() -> None:
    report = validate([])
    assert not report.ok
    assert {f.code for f in report.errors} == {"no_quotes"}


def test_team_total_requires_a_side() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes += [
        make_quote(source="bookA", market=Market.TEAM_TOTAL, selection=Selection.OVER,
                   side=Side.HOME, line=4.5, decimal_odds=1.95,
                   source_market_id="bookA-tt"),
        make_quote(source="bookA", market=Market.TEAM_TOTAL, selection=Selection.UNDER,
                   side=Side.HOME, line=4.5, decimal_odds=1.9,
                   source_market_id="bookA-tt"),
    ]
    report = validate(quotes)
    assert report.ok, [str(f) for f in report.errors]


def test_report_severity_split_and_summary() -> None:
    report = validate(_full_slate("bookA") + _full_slate("bookB"))
    assert report.ok
    assert "PASS" in report.summary()
    assert all(f.severity in (Severity.ERROR, Severity.WARNING) for f in report.findings)


# ── the sport-aware checks ───────────────────────────────────────────────────


class TestEverySportHasACleanSlate:
    """Each sport's own complete slate must pass, or nothing below proves anything.

    This is the test that would have failed loudest before the rewrite: the NFL
    and NHL slates were rejected wholesale by a 30-day futures horizon, the soccer
    slate's three-way moneyline summed over three legs, and tennis was reported
    broken for not having totals.
    """

    @pytest.mark.parametrize(
        "fixture",
        [MLB_GAME, NFL_GAME, NHL_GAME, SOCCER_GAME, TENNIS_MATCH, WNBA_GAME],
        ids=lambda f: f.league,
    )
    def test_a_two_book_slate_is_clean(self, fixture: Fixture) -> None:
        report = validate([*_book(fixture, "book_a"), *_book(fixture, "book_b")])
        assert report.errors == [], "\n".join(str(f) for f in report.errors)

    def test_a_mixed_sport_slate_is_clean(self) -> None:
        quotes = [
            row
            for fixture in (MLB_GAME, NFL_GAME, NHL_GAME, SOCCER_GAME, TENNIS_MATCH)
            for source in ("book_a", "book_b")
            for row in _book(fixture, source)
        ]
        report = validate(quotes)
        assert report.errors == [], "\n".join(str(f) for f in report.errors)


class TestFuturesHorizon:
    """The horizon is per league, because the truth varies by an order of magnitude."""

    def test_a_real_nfl_game_five_months_out_is_not_futures_leakage(self) -> None:
        """The NFL's whole season is priced in July.  A 30-day horizon rejected
        every real NFL and NHL game collected — 32 FanDuel markets, 16 Pinnacle
        matchups and 17 Kambi events, all of them genuine."""
        quotes = [*_book(NFL_GAME, "book_a"), *_book(NFL_GAME, "book_b")]
        ahead = NFL_GAME.commence - OBSERVED
        assert ahead > timedelta(days=140)
        assert "commence_time_too_far" not in _codes(quotes)

    def test_an_nhl_opener_two_months_out_is_not_futures_leakage(self) -> None:
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b")]
        assert NHL_GAME.commence - OBSERVED > timedelta(days=60)
        assert "commence_time_too_far" not in _codes(quotes)

    def test_a_2030_nfl_specials_container_is_still_caught(self) -> None:
        """FanDuel's "NFL Specials" event carries an openDate of 2030-12-01.  The
        horizon is a backstop rather than the primary defence, but it must still
        be a live check after being widened to 400 days."""
        specials = Fixture(
            sport=Sport.FOOTBALL,
            league="NFL",
            home_team="Buffalo Bills",
            away_team="Baltimore Ravens",
            home_participant="NFL-BUF",
            away_participant="NFL-BAL",
            commence=datetime(2030, 12, 1, 18, 0, tzinfo=UTC),
            total=44.5,
            spread=3.5,
        )
        quotes = [*_book(NFL_GAME, "book_a"), *_book(NFL_GAME, "book_b")]
        quotes += _book(specials, "book_a")
        assert "commence_time_too_far" in _errors(quotes)

    def test_a_baseball_game_five_months_out_is_still_futures_leakage(self) -> None:
        """The same distance that is normal for the NFL is absurd for MLB, which
        is the whole reason the horizon cannot be one number."""
        far = Fixture(
            sport=Sport.BASEBALL,
            league="MLB",
            home_team="Miami Marlins",
            away_team="Philadelphia Phillies",
            home_participant="MLB-MIA",
            away_participant="MLB-PHI",
            commence=NFL_GAME.commence,
            total=8.5,
            spread=1.5,
        )
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b")]
        quotes += _book(far, "book_a")
        assert "commence_time_too_far" in _errors(quotes)


class TestDrawLegality:
    """A draw is corruption in one window and correct in the next one over."""

    def test_a_hockey_full_game_draw_is_rejected_by_the_schema(self) -> None:
        """The shootout decides an NHL game, so a "draw" on the full-game
        moneyline is a misparsed third runner."""
        with pytest.raises(ValidationError):
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                period=Period.FULL_GAME,
                selection=Selection.DRAW,
                decimal_odds=4.0,
            )

    def test_a_hockey_full_game_draw_smuggled_past_the_schema_is_an_error(self) -> None:
        """``model_copy`` does not re-run the schema, and neither does a row read
        back from storage, so validation checks draw legality itself rather than
        trusting that the schema already did."""
        legal = _quote(
            NHL_GAME,
            "book_a",
            market=Market.MONEYLINE,
            period=Period.REGULATION,
            selection=Selection.DRAW,
            decimal_odds=4.0,
            source_market_id="book_a-forged",
        )
        forged = legal.model_copy(update={"period": Period.FULL_GAME})
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b"), forged]
        assert "illegal_draw" in _errors(quotes)

    def test_a_hockey_regulation_draw_is_accepted(self) -> None:
        """A 60-minute hockey market is three-way: Pinnacle returns period 6 with
        home/away/draw prices, Kambi calls it "Match Odds - Regular Time"."""
        regulation = [
            _quote(
                NHL_GAME,
                source,
                market=Market.MONEYLINE,
                period=Period.REGULATION,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-reg-ml",
            )
            for source in ("book_a", "book_b")
            for selection, odds in (
                (Selection.HOME, 2.40),
                (Selection.AWAY, 3.30),
                (Selection.DRAW, 3.40),
            )
        ]
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b"), *regulation]
        report = validate(quotes)
        assert report.errors == [], "\n".join(str(f) for f in report.errors)

    def test_a_baseball_full_game_draw_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_quote(selection=Selection.DRAW, decimal_odds=4.0)

    def test_a_soccer_full_game_draw_is_accepted(self) -> None:
        quotes = [*_book(SOCCER_GAME, "book_a"), *_book(SOCCER_GAME, "book_b")]
        assert any(q.selection is Selection.DRAW for q in quotes)
        assert "illegal_draw" not in _codes(quotes)


class TestOutcomeCount:
    """How many legs a complete market has is a fact about the sport."""

    def _two_way_soccer(self, source: str) -> list[Quote]:
        """A soccer 90-minute moneyline with the draw leg dropped."""
        return [
            _quote(
                SOCCER_GAME,
                source,
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-{SOCCER_GAME.event_key}-ml",
            )
            for selection, odds in ((Selection.HOME, 2.40), (Selection.AWAY, 3.30))
        ]

    def test_a_soccer_moneyline_missing_its_draw_is_an_error(self) -> None:
        """1/2.40 + 1/3.30 = 0.720, which is not a market at all: a third of the
        probability is missing.  Read as a two-way market it looks either like
        free money or like the book pricing itself to lose, and both readings
        blame the prices for a dropped row."""
        quotes = [
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.MONEYLINE],
            *self._two_way_soccer("book_a"),
            *_book(SOCCER_GAME, "book_b"),
        ]
        errors = _errors(quotes)
        assert "draw_leg_missing" in errors

    def test_a_soccer_moneyline_missing_its_draw_is_not_called_a_priced_market(self) -> None:
        """The specific misdiagnosis being avoided: reporting the two remaining
        prices as an overround failure, which sends whoever reads the report
        looking at the prices instead of at the missing leg."""
        quotes = [
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.MONEYLINE],
            *self._two_way_soccer("book_a"),
            *_book(SOCCER_GAME, "book_b"),
        ]
        assert "negative_overround" not in _codes(quotes)

    def test_a_hockey_regulation_moneyline_missing_its_draw_is_an_error(self) -> None:
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b")]
        quotes += [
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                period=Period.REGULATION,
                selection=selection,
                decimal_odds=odds,
                source_market_id="book_a-reg-ml",
            )
            for selection, odds in ((Selection.HOME, 2.40), (Selection.AWAY, 3.30))
        ]
        assert "draw_leg_missing" in _errors(quotes)

    def test_a_two_way_hockey_full_game_moneyline_is_complete(self) -> None:
        """The same fixture's full-game moneyline is two-way and must not be
        asked for a draw — the shootout decides it."""
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b")]
        assert "draw_leg_missing" not in _codes(quotes)
        assert "incomplete_market" not in _codes(quotes)

    def test_a_two_way_nfl_moneyline_is_complete(self) -> None:
        """An NFL tie voids the moneyline rather than being priced, so two legs
        is the complete market even though the game can end level."""
        quotes = [*_book(NFL_GAME, "book_a"), *_book(NFL_GAME, "book_b")]
        assert "draw_leg_missing" not in _codes(quotes)
        assert "incomplete_market" not in _codes(quotes)

    def test_a_three_way_soccer_overround_is_summed_over_three_legs(self) -> None:
        """2.40 / 3.30 / 3.40 sums to 1.0138 over three legs and to 0.720 over
        two.  Summing the wrong number of outcomes is how a fair market gets
        reported as a book pricing itself to lose."""
        quotes = [*_book(SOCCER_GAME, "book_a"), *_book(SOCCER_GAME, "book_b")]
        assert "negative_overround" not in _codes(quotes)

    def test_a_genuinely_negative_three_way_overround_is_still_caught(self) -> None:
        quotes = [
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.MONEYLINE],
            *[
                _quote(
                    SOCCER_GAME,
                    "book_a",
                    market=Market.MONEYLINE,
                    selection=selection,
                    decimal_odds=odds,
                    source_market_id=f"book_a-{SOCCER_GAME.event_key}-ml",
                )
                for selection, odds in (
                    (Selection.HOME, 3.60),
                    (Selection.AWAY, 4.20),
                    (Selection.DRAW, 4.60),
                )
            ],
            *_book(SOCCER_GAME, "book_b"),
        ]
        assert "negative_overround" in _errors(quotes)


class TestHomeAwayAgreement:
    """Home/away is a fact in team sports and an imposed ordering in tennis."""

    def _reversed_tennis(self, source: str) -> list[Quote]:
        """A book that lists the two players the other way round.

        Both books' rows are put under one event key deliberately: the question
        under test is what the cross-source check concludes when it sees them
        together, not whether they would have been grouped in the first place.
        """
        return [
            _quote(
                TENNIS_MATCH,
                source,
                home_participant=TENNIS_MATCH.away_participant,
                away_participant=TENNIS_MATCH.home_participant,
                home_team=TENNIS_MATCH.away_team,
                away_team=TENNIS_MATCH.home_team,
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-rev-ml",
            )
            for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95))
        ]

    def test_two_books_ordering_a_tennis_match_differently_still_describe_one_pair(
        self,
    ) -> None:
        """There is no home player: src.events.orient imposes the ordering and the
        books have no opinion, so reporting a home/away disagreement here would
        report a disagreement that does not exist.  What must still be checked is
        that the two books name the same two players."""
        quotes = [*_book(TENNIS_MATCH, "book_a"), *self._reversed_tennis("book_b")]
        codes = _codes(quotes)
        assert "home_away_disagreement" not in codes
        assert "participant_pair_disagreement" not in codes

    def test_a_row_that_ignores_the_imposed_tennis_ordering_is_an_error(self) -> None:
        """It is not a disagreement between books; it is a row whose event key can
        never match another book's for the same match."""
        quotes = [*_book(TENNIS_MATCH, "book_a"), *self._reversed_tennis("book_b")]
        assert "participant_order_not_canonical" in _errors(quotes)

    def test_two_books_naming_different_players_is_an_error(self) -> None:
        """Same fixture by key, different matchup — the failure the pair check
        exists for, and the one tennis is exposed to precisely because it has no
        home/away check to fall back on."""
        other = [
            _quote(
                TENNIS_MATCH,
                "book_b",
                home_participant="TENNIS-humbertugo",
                away_participant="TENNIS-janniksinner",
                home_team="Ugo Humbert",
                away_team="Jannik Sinner",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id="book_b-other-ml",
            )
            for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95))
        ]
        quotes = [*_book(TENNIS_MATCH, "book_a"), *other]
        assert "participant_pair_disagreement" in _errors(quotes)

    def test_a_soccer_home_away_swap_is_still_caught(self) -> None:
        """Soccer clubs do have a home side, so the check stays live there.

        The swapped book carries the event key its *own* participants produce,
        because that is the only state the pipeline can reach: reconciliation
        rebuilds the key from what each source reported.  Grouping these checks
        on the key therefore put the reversed rows in a group of their own, where
        they had nobody to disagree with — the check ran and could never fire.
        """
        swapped = [
            _quote(
                SOCCER_GAME,
                "book_b",
                home_participant=SOCCER_GAME.away_participant,
                away_participant=SOCCER_GAME.home_participant,
                home_team=SOCCER_GAME.away_team,
                away_team=SOCCER_GAME.home_team,
                event_key=f"{SOCCER_GAME.home_participant}@"
                          f"{SOCCER_GAME.away_participant}:2026-08-15",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id="book_b-swap-ml",
            )
            for selection, odds in (
                (Selection.HOME, 2.40),
                (Selection.AWAY, 3.30),
                (Selection.DRAW, 3.40),
            )
        ]
        quotes = [*_book(SOCCER_GAME, "book_a"), *swapped]
        codes = _codes(quotes)
        assert "home_away_disagreement" in codes
        # One fixture between two sources is a neutral-venue judgement, not a
        # broken adapter, so it is said rather than failed on; the live slate has
        # exactly one such case, a pre-season friendly.
        assert "home_away_disagreement" not in _errors(quotes)


class TestStartTimeTolerance:
    """The tolerance is per league because a tennis start time is an estimate."""

    def _shifted(self, fixture: Fixture, source: str, delta: timedelta) -> list[Quote]:
        return [
            q.model_copy(update={"commence_time": q.commence_time + delta})
            for q in _book(fixture, source)
        ]

    def test_tennis_books_listing_a_match_hours_apart_agree(self) -> None:
        """A match is scheduled "not before" the preceding match on court and each
        book publishes its own estimate, so the same first-round match is
        routinely listed hours apart.  A 20-minute rule split nearly every tennis
        match into two events."""
        quotes = [
            *_book(TENNIS_MATCH, "book_a"),
            *self._shifted(TENNIS_MATCH, "book_b", timedelta(hours=5)),
        ]
        assert "start_time_disagreement" not in _codes(quotes)

    def test_tennis_books_a_day_apart_still_disagree(self) -> None:
        """The window is wide, not infinite: 14 hours is chosen because the same
        two players never meet twice in a day."""
        quotes = [
            *_book(TENNIS_MATCH, "book_a"),
            *self._shifted(TENNIS_MATCH, "book_b", timedelta(hours=20)),
        ]
        assert "start_time_disagreement" in {f.code for f in validate(quotes).warnings}

    def test_baseball_books_five_hours_apart_disagree(self) -> None:
        """Two games of a doubleheader are separated by a game plus a changeover,
        so baseball keeps a tight window — the same shift that is normal for
        tennis is a mis-join here."""
        quotes = [
            *_book(MLB_GAME, "book_a"),
            *self._shifted(MLB_GAME, "book_b", timedelta(hours=5)),
        ]
        assert "start_time_disagreement" in {f.code for f in validate(quotes).warnings}

    def test_a_one_minute_difference_is_tolerated_everywhere(self) -> None:
        for fixture in (MLB_GAME, NFL_GAME, SOCCER_GAME, TENNIS_MATCH):
            quotes = [
                *_book(fixture, "book_a"),
                *self._shifted(fixture, "book_b", timedelta(minutes=1)),
            ]
            assert "start_time_disagreement" not in _codes(quotes), fixture.league


class TestTotalPlausibility:
    """A total outside the league's range is a units error or a wrong sport."""

    def _with_total(self, fixture: Fixture, line: float) -> list[Quote]:
        return [
            *_book(fixture, "book_a"),
            *_book(fixture, "book_b"),
            *[
                _quote(
                    fixture,
                    "book_a",
                    market=Market.TOTAL,
                    selection=selection,
                    line=line,
                    decimal_odds=1.91,
                    source_market_id="book_a-alt-total",
                    is_alternate=True,
                )
                for selection in (Selection.OVER, Selection.UNDER)
            ],
        ]

    def test_an_8000_run_baseball_total_is_an_error(self) -> None:
        """Kambi sends odds *and lines* in thousandths, so a total of 8.0 arrives
        as 8000."""
        assert "implausible_total" in _errors(self._with_total(MLB_GAME, 8000.0))

    def test_a_2_5_point_nfl_total_is_an_error(self) -> None:
        """A well-formed number that is absurd for the sport it landed in: this is
        a soccer goals line wearing a football label."""
        assert "implausible_total" in _errors(self._with_total(NFL_GAME, 2.5))

    def test_a_44_5_point_nfl_total_is_fine(self) -> None:
        assert "implausible_total" not in _codes(self._with_total(NFL_GAME, 44.5))

    def test_a_2_5_goal_soccer_total_is_fine(self) -> None:
        """The same 2.5 that is an error in the NFL is the most common soccer
        total there is."""
        assert "implausible_total" not in _codes(self._with_total(SOCCER_GAME, 2.5))

    def test_a_44_5_goal_soccer_total_is_an_error(self) -> None:
        assert "implausible_total" in _errors(self._with_total(SOCCER_GAME, 44.5))

    def test_a_partial_period_total_below_the_full_game_floor_is_fine(self) -> None:
        """The range is stated for a whole contest.  Five innings score less than
        nine, so only the upper bound carries over — otherwise every
        first-five-innings total in MLB would be an error."""
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b")]
        quotes += [
            _quote(
                MLB_GAME,
                "book_a",
                market=Market.TOTAL,
                period=Period.FIRST_5_INNINGS,
                selection=selection,
                line=3.5,
                decimal_odds=1.91,
                source_market_id="book_a-f5-total",
            )
            for selection in (Selection.OVER, Selection.UNDER)
        ]
        assert "implausible_total" not in _codes(quotes)

    def test_a_team_total_below_the_game_total_floor_is_fine(self) -> None:
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b")]
        quotes += [
            _quote(
                MLB_GAME,
                "book_a",
                market=Market.TEAM_TOTAL,
                selection=selection,
                side=Side.HOME,
                line=3.5,
                decimal_odds=1.91,
                source_market_id="book_a-tt",
            )
            for selection in (Selection.OVER, Selection.UNDER)
        ]
        assert "implausible_total" not in _codes(quotes)

    def test_a_thousandths_team_total_is_still_an_error(self) -> None:
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b")]
        quotes += [
            _quote(
                MLB_GAME,
                "book_a",
                market=Market.TEAM_TOTAL,
                selection=selection,
                side=Side.HOME,
                line=3500.0,
                decimal_odds=1.91,
                source_market_id="book_a-tt",
            )
            for selection in (Selection.OVER, Selection.UNDER)
        ]
        assert "implausible_total" in _errors(quotes)


class TestCoreCoverageBySport:
    def test_tennis_is_not_faulted_for_having_no_totals(self) -> None:
        """Tennis carries only the match winner.  The previous hardcoded
        {moneyline, run line, total runs} reported every tennis book as broken."""
        quotes = [*_book(TENNIS_MATCH, "book_a"), *_book(TENNIS_MATCH, "book_b")]
        assert {q.market for q in quotes} == {Market.MONEYLINE}
        assert "core_market_absent" not in _codes(quotes)

    def test_a_football_book_that_stopped_returning_spreads_is_an_error(self) -> None:
        quotes = [
            *[q for q in _book(NFL_GAME, "book_a") if q.market is not Market.SPREAD],
            *_book(NFL_GAME, "book_b"),
        ]
        assert "core_market_absent" in _errors(quotes)

    def test_coverage_is_judged_per_sport_not_across_the_whole_slate(self) -> None:
        """A book with a full baseball slate and no soccer totals must not be
        excused because its baseball totals satisfy a slate-wide check."""
        quotes = [
            *_book(MLB_GAME, "book_a"),
            *_book(MLB_GAME, "book_b"),
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.TOTAL],
            *_book(SOCCER_GAME, "book_b"),
        ]
        errors = [f for f in validate(quotes).errors if f.code == "core_market_absent"]
        assert errors and "soccer" in errors[0].message
        assert all("baseball" not in f.message for f in errors)

    def test_a_partial_rename_within_one_sport_is_caught_per_event(self) -> None:
        """One book loses the total on a minority of a full slate.

        The slate has to be slate-sized: the rule deliberately refuses to call a
        one-in-three gap a rename, because on that sample it cannot be told apart
        from a book not posting the market on one game.
        """
        slate = _nfl_slate(10)
        stripped = set(range(3))  # a clear minority, so "absent for a few" holds
        quotes: list[Quote] = []
        for index, fixture in enumerate(slate):
            rows = _book(fixture, "book_a")
            if index in stripped:
                rows = [q for q in rows if q.market is not Market.TOTAL]
            quotes.extend(rows)
            quotes.extend(_book(fixture, "book_b"))
        # Reported, but as a warning: from one run a renamed label and a book that
        # simply prices some fixtures thinly are indistinguishable, and grading it
        # an error made every real soccer run FAIL permanently.  Telling the two
        # apart needs this source's own past coverage.
        report = validate(quotes)
        assert "core_market_absent_for_event" in {f.code for f in report.warnings}
        assert "core_market_absent_for_event" not in {f.code for f in report.errors}

    def test_a_gap_too_small_to_diagnose_is_not_called_a_rename(self) -> None:
        """The other side of the same rule.

        On a three-game slate a single absence is 33%, which trips a bare
        "present for most" threshold — but a soccer or football book not posting a
        handicap on one fixture is ordinary.  With no sample to separate the two,
        the honest grade is a warning, and calling it an error failed every real
        run over a book's pricing decision.
        """
        slate = _nfl_slate(3)
        quotes: list[Quote] = []
        for index, fixture in enumerate(slate):
            rows = _book(fixture, "book_a")
            if index == 0:
                rows = [q for q in rows if q.market is not Market.TOTAL]
            quotes.extend(rows)
        report = validate(quotes)
        assert "core_market_absent_for_event" not in {f.code for f in report.errors}
        assert "core_market_thinly_offered" in {f.code for f in report.warnings}


class TestThinlyOfferedMarkets:
    """Absent-for-a-few and present-for-a-few are opposite anomalies."""

    def _soccer_fixtures(self, count: int) -> list[Fixture]:
        clubs = [
            ("Arsenal", "SOCCER-arsenal"),
            ("Chelsea", "SOCCER-chelsea"),
            ("Liverpool", "SOCCER-liverpool"),
            ("Everton", "SOCCER-everton"),
            ("Fulham", "SOCCER-fulham"),
            ("Brentford", "SOCCER-brentford"),
            ("Burnley", "SOCCER-burnley"),
            ("Sunderland", "SOCCER-sunderland"),
            ("Newcastle United", "SOCCER-newcastleunited"),
            ("Aston Villa", "SOCCER-astonvilla"),
            ("Tottenham", "SOCCER-tottenham"),
            ("West Ham United", "SOCCER-westhamunited"),
            ("Manchester City", "SOCCER-manchestercity"),
            ("Manchester United", "SOCCER-manchesterunited"),
            ("Crystal Palace", "SOCCER-crystalpalace"),
            ("Wolverhampton", "SOCCER-wolverhampton"),
            ("Bournemouth", "SOCCER-bournemouth"),
            ("Brighton", "SOCCER-brighton"),
            ("Nottingham Forest", "SOCCER-nottmforest"),
            ("Leeds United", "SOCCER-leedsunited"),
        ]
        out = []
        for index in range(count):
            (home_team, home_key), (away_team, away_key) = clubs[2 * index], clubs[2 * index + 1]
            out.append(
                Fixture(
                    sport=Sport.SOCCER,
                    league="EPL",
                    home_team=home_team,
                    away_team=away_team,
                    home_participant=home_key,
                    away_participant=away_key,
                    commence=SOCCER_GAME.commence + timedelta(hours=index),
                    total=2.5,
                    spread=0.5,
                )
            )
        return out

    def test_a_market_offered_for_only_one_event_in_many_is_a_warning(self) -> None:
        """FanDuel's soccer handicaps and totals live on the per-event page, and
        the captured run fetched one of 127.  That is a collection decision, not a
        renamed label, so failing the whole run on it would cry wolf."""
        fixtures = self._soccer_fixtures(4)
        quotes: list[Quote] = []
        for index, fixture in enumerate(fixtures):
            for source in ("book_a", "book_b"):
                rows = _book(fixture, source)
                if source == "book_a" and index > 0:
                    rows = [r for r in rows if r.market is Market.MONEYLINE]
                quotes += rows
        report = validate(quotes)
        assert "core_market_thinly_offered" in {f.code for f in report.warnings}
        assert "core_market_absent_for_event" not in {f.code for f in report.errors}

    def test_a_market_missing_from_only_some_events_is_reported(self) -> None:
        """The other way round: present for most of a slate, absent for a few, is
        what a rename that hit some games looks like.

        Sized to a real slate on purpose — the rule refuses to make this call on a
        handful of events, where thin pricing and a rename are indistinguishable."""
        fixtures = self._soccer_fixtures(10)
        quotes: list[Quote] = []
        for index, fixture in enumerate(fixtures):
            for source in ("book_a", "book_b"):
                rows = _book(fixture, source)
                if source == "book_a" and index == 0:
                    rows = [r for r in rows if r.market is not Market.TOTAL]
                quotes += rows
        report = validate(quotes)
        assert "core_market_absent_for_event" in {f.code for f in report.warnings}


class TestLeagueIdentity:
    def test_an_unregistered_league_is_an_error(self) -> None:
        """src.events leaves a row with an unknown league out of reconciliation,
        so validation is the only place it can be reported."""
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b")]
        quotes.append(
            make_quote(source="book_a", league="KBO", source_market_id="book_a-kbo")
        )
        assert "unknown_league" in _errors(quotes)

    def test_a_sport_that_contradicts_its_league_is_an_error(self) -> None:
        """Every settlement rule is looked up by sport, so a soccer market
        labelled MLB would be settled with baseball's tie rules."""
        forged = _quote(
            SOCCER_GAME,
            "book_a",
            market=Market.MONEYLINE,
            selection=Selection.HOME,
            decimal_odds=2.40,
            source_market_id="book_a-mislabelled",
        ).model_copy(update={"league": "MLB"})
        quotes = [*_book(SOCCER_GAME, "book_a"), *_book(SOCCER_GAME, "book_b"), forged]
        assert "league_sport_mismatch" in _errors(quotes)

    def test_books_classifying_one_match_differently_is_only_a_warning(self) -> None:
        """The same tennis match is "ATP Challenger Bonn - R1" at one book and
        "challenger" at another.  League is deliberately not part of event
        identity, so this cannot break the join and must not fail the run."""
        other = [
            q.model_copy(update={"league": "ATP_CHALLENGER"})
            for q in _book(TENNIS_MATCH, "book_b")
        ]
        quotes = [*_book(TENNIS_MATCH, "book_a"), *other]
        report = validate(quotes)
        assert "league_disagreement" in {f.code for f in report.warnings}
        assert report.errors == [], "\n".join(str(f) for f in report.errors)


class TestCrossSportSanity:
    def test_a_sport_with_no_cross_book_overlap_is_a_warning_not_an_error(self) -> None:
        """On the day this was built no hockey fixture was priced by two of the
        three books at once: one had the season's openers, another had club
        friendlies.  That is a fact about the calendar, so it is said out loud
        without failing the run."""
        lonely = _book(NHL_GAME, "book_a")
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b"), *lonely]
        report = validate(quotes)
        assert "no_shared_events_for_sport" in {f.code for f in report.warnings}
        assert report.errors == [], "\n".join(str(f) for f in report.errors)

    def test_one_market_id_spanning_two_sports_is_an_error(self) -> None:
        forged = _quote(
            MLB_GAME,
            "book_a",
            market=Market.MONEYLINE,
            selection=Selection.HOME,
            decimal_odds=1.91,
            source_market_id="book_a-fused",
        )
        other = _quote(
            MLB_GAME,
            "book_a",
            market=Market.MONEYLINE,
            selection=Selection.AWAY,
            decimal_odds=1.95,
            source_market_id="book_a-fused",
        ).model_copy(update={"sport": Sport.SOCCER})
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b"), forged, other]
        assert "heterogeneous_market" in _errors(quotes)
