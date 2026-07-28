"""The league registry.

A :class:`League` records what the pipeline needs to know about a competition
that no individual row can state: how far ahead a real fixture may legitimately
be scheduled, which calendar a game's date belongs to, whether "home" is a fact
or an arbitrary ordering, and how big a plausible total is.

Two design points are load-bearing.

**League is not part of event identity.**  ``event_key`` is built from the
participants and the date only (see :mod:`src.events`).  Books disagree about
league classification constantly — the same tennis match is "ATP Challenger
Bonn - R1" at Pinnacle, ``challenger`` at Kambi, and a numeric ``competitionId``
at FanDuel — and if that disagreement could change the key, the join would break
on exactly the events all three books cover.  ``league`` is therefore carried
for coverage reporting and validated as a *warning*, never used to join.

**Home/away is a fact in some sports and a coin flip in others.**  In a team
sport the home side is a real property and the books agree on it, so a
disagreement is a genuine error worth reporting.  In tennis there is no home
player: each book orders the two names however it likes.  Trusting that
ordering would flip the sign of a handicap and mislabel which player a price
refers to, so for those leagues :mod:`src.events` imposes its own deterministic
ordering and validation does not check agreement it has no right to expect.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from src.vocab import Sport

#: North American leagues schedule against the US Eastern calendar: a 22:10 ET
#: first pitch is "Tuesday's game" even though it is Wednesday in UTC.  Bucketing
#: on the Eastern date keeps two books that report 01:40Z and 01:45Z on the same
#: day instead of splitting them across midnight.
US_EASTERN = ZoneInfo("America/New_York")

#: Global competitions have no single domestic calendar, so UTC is used.  It is
#: the one choice both books' timestamps already agree on, and the doubleheader
#: clustering in :mod:`src.events` tolerates a midnight split anyway because it
#: groups on start time, not on the date string.
UTC_TZ = ZoneInfo("UTC")


@dataclass(frozen=True)
class League:
    """One competition the collector knows how to interpret."""

    key: str
    """Stable canonical identifier, e.g. ``"MLB"``.  Adapters map their own
    league identifiers onto this."""

    sport: Sport
    name: str

    roster: str | None = None
    """Name of the closed roster in :mod:`src.participants` that every
    participant in this league must resolve against, or ``None`` for an
    open-roster competition (soccer clubs, tennis players) where no finite
    membership list exists."""

    scheduling_tz: ZoneInfo = US_EASTERN

    has_home_away: bool = True
    """Whether "home" names a real property of the fixture.  ``False`` for
    tennis, where the two participants are ordered arbitrarily by each book."""

    max_schedule_horizon: timedelta = timedelta(days=30)
    """How far ahead a genuine fixture may start.

    This is a futures-leakage guard, and it has to be per-league because the
    truth varies by an order of magnitude: baseball books post lines a few days
    out, while the NFL's entire season is priced in July.  A single 30-day rule
    would have rejected every real NFL and NHL game collected here.

    It is a backstop, not the primary defence — the primary defence is that a
    futures container's participants do not resolve to two competitors.  What
    this still catches is the absurd: FanDuel's "NFL Specials" event carries an
    ``openDate`` of 2030-12-01."""

    plausible_total_range: tuple[float, float] = (0.5, 100.0)
    """Sanity bounds for a full-game total in this league.  A total outside them
    is a units error (Kambi sends lines in thousandths) or a market that was
    mapped to the wrong sport."""

    same_event_tolerance: timedelta = timedelta(minutes=90)
    """How far apart two books' start times may be and still describe the same
    fixture.

    This is a two-sided trade-off, and the two sides differ by sport.  Too small
    and one fixture splits into two events, breaking the join on exactly the
    events both books cover.  Too large and the two games of a doubleheader fuse
    into one, pairing prices from different games.

    90 minutes suits leagues that play doubleheaders: books disagree by a minute
    or two, while two games of an MLB doubleheader are separated by a full game
    plus a changeover.  Tennis needs far more, because a match is scheduled
    "after the preceding match on court" and each book publishes its own estimate
    — the same first-round match is routinely listed hours apart.  Tennis can
    afford it: the same two players never meet twice in one day, so there is no
    second fixture for a wide window to swallow."""


#: Tennis start times are estimates ("not before"), and each book publishes its
#: own, so the same match is routinely listed hours apart.  Widening the window
#: is safe because the same two players never meet twice on one day.
TENNIS_TOLERANCE = timedelta(hours=14)


def _l(*args, **kwargs) -> League:
    return League(*args, **kwargs)


#: Every league the collector can interpret.  Adding a league here is not enough
#: to collect it — an adapter must also map its own identifier onto the key.
LEAGUES: tuple[League, ...] = (
    # ── baseball ─────────────────────────────────────────────────────────────
    _l("MLB", Sport.BASEBALL, "Major League Baseball", roster="mlb",
       plausible_total_range=(4.0, 20.0)),
    # ── basketball ───────────────────────────────────────────────────────────
    # The NBA is in its offseason as of this writing, so it is registered but
    # unverified against live game data; the WNBA is mid-season and is what the
    # basketball support was actually validated on.
    _l("NBA", Sport.BASKETBALL, "National Basketball Association", roster="nba",
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(150.0, 300.0)),
    _l("WNBA", Sport.BASKETBALL, "Women's National Basketball Association", roster="wnba",
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(120.0, 250.0)),
    # ── hockey ───────────────────────────────────────────────────────────────
    _l("NHL", Sport.HOCKEY, "National Hockey League", roster="nhl",
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(3.0, 12.0)),
    # Pinnacle carries no NHL games in the offseason, but it does price club
    # friendlies, and those are the only real hockey markets it offers.  Without
    # a league to map them onto they were dropped as `league_not_registered`,
    # which made Pinnacle's hockey contribution a silent zero rather than a
    # visible "prices friendlies, not the NHL".  Open roster: these are club and
    # junior sides with no fixed membership list.
    _l("HOCKEY_OTHER", Sport.HOCKEY, "Other hockey competition", scheduling_tz=UTC_TZ,
       plausible_total_range=(3.0, 15.0)),
    # ── football ─────────────────────────────────────────────────────────────
    _l("NFL", Sport.FOOTBALL, "National Football League", roster="nfl",
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(20.0, 80.0)),
    # ── tennis ───────────────────────────────────────────────────────────────
    # Tour-level rather than per-tournament: Pinnacle exposes one "league" per
    # tournament *round* (37 of them on one day), which is a scheduling detail,
    # not a competition.  Since league is not part of event identity, collapsing
    # them costs nothing and keeps coverage reporting legible.
    _l("ATP", Sport.TENNIS, "ATP Tour", scheduling_tz=UTC_TZ, has_home_away=False,
       max_schedule_horizon=timedelta(days=30), plausible_total_range=(12.0, 60.0),
       same_event_tolerance=TENNIS_TOLERANCE),
    _l("WTA", Sport.TENNIS, "WTA Tour", scheduling_tz=UTC_TZ, has_home_away=False,
       max_schedule_horizon=timedelta(days=30), plausible_total_range=(12.0, 60.0),
       same_event_tolerance=TENNIS_TOLERANCE),
    _l("ATP_CHALLENGER", Sport.TENNIS, "ATP Challenger Tour", scheduling_tz=UTC_TZ,
       has_home_away=False, plausible_total_range=(12.0, 60.0),
       same_event_tolerance=TENNIS_TOLERANCE),
    _l("ITF", Sport.TENNIS, "ITF Tour", scheduling_tz=UTC_TZ, has_home_away=False,
       plausible_total_range=(12.0, 60.0), same_event_tolerance=TENNIS_TOLERANCE),
    # A catch-all, for the same reason soccer has one: a match must never be
    # dropped merely because its tour is unrecognised.  Without this, an adapter
    # meeting a competition like "Mens UTR Pro Series, Argentina" has to either
    # guess a tour or discard the match, and guessing mislabels coverage.
    _l("TENNIS_OTHER", Sport.TENNIS, "Other tennis competition", scheduling_tz=UTC_TZ,
       has_home_away=False, plausible_total_range=(12.0, 60.0),
       same_event_tolerance=TENNIS_TOLERANCE),
    # ── soccer ───────────────────────────────────────────────────────────────
    # Soccer is registered per competition where the books agree on one, and
    # otherwise under a catch-all so a fixture is never dropped merely because
    # its competition is unrecognised.  Identity does not depend on which of
    # these a book chose.
    _l("EPL", Sport.SOCCER, "English Premier League", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("MLS", Sport.SOCCER, "Major League Soccer", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("LA_LIGA", Sport.SOCCER, "Spanish La Liga", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("SERIE_A", Sport.SOCCER, "Italian Serie A", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("BUNDESLIGA", Sport.SOCCER, "German Bundesliga", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("LIGUE_1", Sport.SOCCER, "French Ligue 1", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
    _l("SOCCER_OTHER", Sport.SOCCER, "Other soccer competition", scheduling_tz=UTC_TZ,
       max_schedule_horizon=timedelta(days=400), plausible_total_range=(0.5, 8.0)),
)

BY_KEY: dict[str, League] = {league.key: league for league in LEAGUES}

LEAGUES_BY_SPORT: dict[Sport, tuple[League, ...]] = {
    sport: tuple(league for league in LEAGUES if league.sport is sport) for sport in Sport
}


def league(key: str) -> League:
    """Look up a league by canonical key, raising on an unknown one."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown league {key!r}; register it in src.leagues.LEAGUES "
            f"(known: {sorted(BY_KEY)})"
        ) from None


def is_known(key: str) -> bool:
    return key in BY_KEY
