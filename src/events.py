"""Cross-source event identity.

Two books describe the same fixture differently ("Cincinnati Reds", "CIN Reds",
"Cincinnati Reds (C Burns)"; "Ugo Humbert", "Humbert Ugo") and report start times
that differ by a minute or two — or, in tennis, by hours.  ``event_key`` gives
them one shared identity so the normalized table can be joined across sources.

The key is built from **participant keys and the scheduling date only**:

    ``AWAY@HOME:YYYY-MM-DD``  →  ``MLB-CIN@MLB-PHI:2026-07-28``

Three things are deliberately *not* in it.

*League* is absent because books disagree about classification and that
disagreement must not be able to break a join — see :mod:`src.leagues`.
*Sport* is absent because it is already implied: participant keys are namespaced
(``MLB-CIN``, ``SOCCER-arsenal``, ``TENNIS-humbertugo``), so no two sports can
collide.  And the *time of day* is absent because books round start times
differently; the date bucket plus the doubleheader pass below carries that load.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from src.leagues import League, is_known
from src.leagues import league as get_league
from src.participants import Participant
from src.schema import Quote


def scheduling_date(commence_time: datetime, competition: League) -> date:
    """The calendar date a fixture belongs to, in its league's own calendar."""
    if commence_time.tzinfo is None:
        raise ValueError("commence_time must be timezone-aware")
    return commence_time.astimezone(competition.scheduling_tz).date()


def build_event_key(
    away_key: str, home_key: str, commence_time: datetime, competition: League
) -> str:
    """``AWAY@HOME:YYYY-MM-DD`` on the league's scheduling date."""
    return f"{away_key}@{home_key}:{scheduling_date(commence_time, competition).isoformat()}"


def orient(
    first: Participant,
    second: Participant,
    competition: League,
    *,
    home: Participant | None = None,
) -> tuple[Participant, Participant]:
    """Decide which participant is "away" and which is "home".

    Returns ``(away, home)``.

    Where home advantage is a real property of the fixture the book's own
    assignment is authoritative and is passed in as *home*.  Where it is not —
    tennis — each book orders the two names however it likes, and trusting that
    order would mislabel which player every price refers to and flip the sign of
    any handicap.  For those leagues this imposes one deterministic order
    instead, by participant key, so every book lands on the same answer without
    having to agree on anything but the two names.
    """
    if not competition.has_home_away:
        away, home_side = sorted((first, second), key=lambda p: p.key)
        return away, home_side
    if home is None:
        raise ValueError(
            f"{competition.key} has home/away, so the book's home participant must be given"
        )
    other = second if home.key == first.key else first
    return other, home


def resolve_doubleheaders(
    events: dict[str, tuple[str, datetime]],
) -> dict[str, str]:
    """Assign unique event keys when a matchup is played twice in one day.

    *events* maps a source's own event id to ``(base_key, commence_time)``.
    Returns source-event-id -> final event key, appending ``#2``, ``#3`` … to the
    later game(s) ordered by start time.  Deterministic, so two sources that see
    the same doubleheader agree on which game is which.

    This is a per-source pass and its ordinals are therefore provisional; see
    :func:`reconcile_event_keys`, which re-derives them across all sources.
    """
    grouped: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for source_event_id, (base_key, commence_time) in events.items():
        grouped[base_key].append((commence_time, source_event_id))

    resolved: dict[str, str] = {}
    for base_key, entries in grouped.items():
        if len(entries) == 1:
            resolved[entries[0][1]] = base_key
            continue
        for index, (_, source_event_id) in enumerate(sorted(entries), start=1):
            resolved[source_event_id] = base_key if index == 1 else f"{base_key}#{index}"
    return resolved


# ── cross-source reconciliation ──────────────────────────────────────────────


def cluster_start_times(
    times: Iterable[datetime], tolerance: timedelta
) -> list[list[datetime]]:
    """Group start times that describe the same fixture.

    A moment joins the current fixture when it is within *tolerance* of the
    previous member **and** within *tolerance* of the cluster's first member.
    Two books a minute apart land together; the two halves of an MLB doubleheader
    do not.  *tolerance* is the league's
    :attr:`~src.leagues.League.same_event_tolerance`, because the right width
    differs by an order of magnitude between baseball and tennis.

    The second condition is what stops a **chain**.  Pure single-linkage bounds
    each *gap* and not the total span, so N sources stepping along one at a time
    fuse into one fixture spanning up to ``(N-1) × tolerance``: three books
    listing kickoffs 29 hours apart in sequence became one event under soccer's
    30-hour width, and the resulting "position" had its three legs on three
    different days — reported as a 16.67% guarantee, with nothing downstream to
    catch it, because :func:`src.arb._fixture_outliers` deliberately does not
    compare ``commence_time``.

    Bounding the span makes the width mean what it says: at 30 hours a fixture
    can be listed anywhere inside a 30-hour window and no further, whatever the
    source count.  It costs nothing on real data — the widest cluster the live
    slate forms spans 26 hours, inside the same bound that permits it.
    """
    ordered = sorted(times)
    if not ordered:
        return []
    clusters: list[list[datetime]] = [[ordered[0]]]
    for moment in ordered[1:]:
        current = clusters[-1]
        if moment - current[-1] <= tolerance and moment - current[0] <= tolerance:
            current.append(moment)
        else:
            clusters.append([moment])
    return clusters


@dataclass(frozen=True)
class Rekey:
    """One quote group whose event key was corrected during reconciliation."""

    source: str
    source_event_id: str
    was: str
    now: str

    def __str__(self) -> str:
        return f"{self.source}/{self.source_event_id}: {self.was} -> {self.now}"


def reconcile_event_keys(quotes: Sequence[Quote]) -> tuple[list[Quote], list[Rekey]]:
    """Re-derive every event key from all sources at once.

    Each adapter numbers repeat fixtures over *its own* slate, which makes ``#2``
    a per-source ordinal rather than an identity.  The failure that causes is
    silent and expensive: if one book lists both games of a doubleheader and
    another lists only the second, the second book's game 2 is numbered as though
    it were game 1, and its prices are joined onto the other book's game 1.  Two
    unrelated games then look like one event priced by two books — which is
    exactly the shape of a phantom arbitrage.

    This pass replaces that ordinal with something intrinsic.  Fixtures are
    identified by *when they start*, clustered across every source at once, so a
    book that lists only the late game still lands in the late game's cluster.
    Ordering no longer depends on which games a particular source happened to
    return, nor on tie-breaking by source-specific ids.

    Grouping is on the participant keys the adapters already resolved, not on the
    incoming ``event_key``, so a source that mislabels home and away cannot hide
    inside a key that looks right.  Rows whose league is unknown are left
    untouched for validation to reject.
    """
    by_matchup: dict[tuple[str, str], list[Quote]] = defaultdict(list)
    untouched: list[Quote] = []
    for quote in quotes:
        if not is_known(quote.league):
            untouched.append(quote)
            continue
        by_matchup[(quote.away_participant, quote.home_participant)].append(quote)

    result: list[Quote] = []
    changes: list[Rekey] = []
    for (away, home), group in by_matchup.items():
        # Every row in a matchup group shares a sport; the tolerance is taken
        # from the widest league present so a tennis match classified as ATP by
        # one book and Challenger by another is still clustered as one fixture.
        tolerance = max(get_league(q.league).same_event_tolerance for q in group)
        competition = get_league(group[0].league)
        clusters = cluster_start_times({q.commence_time for q in group}, tolerance)

        # Number the fixtures of each scheduling date in start-time order.  The
        # date comes from the cluster's earliest time so a cluster straddling
        # midnight is not split in half.
        #
        # The ordinal is a **within-run** identity, and deliberately so: it is a
        # rank among the fixtures this collection can see, which is the only
        # thing available that does not depend on one source's numbering.  It is
        # therefore not stable across collections.  Adapters drop started games,
        # so once game one of a doubleheader is under way game two is the only
        # cluster left and is numbered one — inheriting the bare key game one
        # carried an hour before.  Nothing within a run is mispaired by that;
        # what it means is that a consumer joining rows *across* runs must pin
        # the fixture by scheduled start as well as by key, which is what
        # ``betRows`` in the dashboard does.
        key_for_cluster: dict[int, str] = {}
        seen_per_date: dict[date, int] = defaultdict(int)
        for index, cluster in enumerate(clusters):
            day = scheduling_date(cluster[0], competition)
            seen_per_date[day] += 1
            ordinal = seen_per_date[day]
            base = f"{away}@{home}:{day.isoformat()}"
            key_for_cluster[index] = base if ordinal == 1 else f"{base}#{ordinal}"

        for quote in group:
            index = next(
                i for i, cluster in enumerate(clusters) if quote.commence_time in cluster
            )
            correct = key_for_cluster[index]
            if quote.event_key == correct:
                result.append(quote)
                continue
            changes.append(
                Rekey(
                    source=quote.source,
                    source_event_id=quote.source_event_id,
                    was=quote.event_key,
                    now=correct,
                )
            )
            result.append(quote.model_copy(update={"event_key": correct}))

    result.extend(untouched)
    # Deduplicate the change log: one entry per source event, not per row.
    unique = {(c.source, c.source_event_id, c.was, c.now): c for c in changes}
    return result, sorted(unique.values(), key=str)
