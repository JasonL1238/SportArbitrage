"""Cloudbet's handicap framing, which used to delete half of every spread ladder.

There was no test file for this adapter at all, which is how a filter that
discarded 358 rows on one run — and forced a negative home line on 100% of the
rows it kept — survived unremarked.
"""
from __future__ import annotations

import glob

import pytest

from src.raw_store import RawStore
from src.schema import Market, Selection
from src.sources.cloudbet import parse_cloudbet


@pytest.fixture(scope="module")
def outcome():
    paths = sorted(glob.glob("tests/fixtures/raw/cloudbet__*.json"))
    assert paths, "the cloudbet capture is missing"
    store = RawStore("tests/fixtures/raw")
    return parse_cloudbet([store.read(path) for path in paths])


def test_both_handicap_directions_survive(outcome) -> None:
    """``handicap=+1.5`` is not a restatement of ``handicap=-1.5``.

    The filter this replaces skipped every positive handicap as
    ``mirror_spread_framing``, on the stated premise that the feed "lists both
    handicap=-1.5 and handicap=+1.5 pairs for the same run line". One baseball
    fixture in the captures carries all four rows with independent prices::

        handicap=+1.5   home 1.49   away 2.50
        handicap=-1.5   home 2.80   away 1.42

    1.49 is not the mirror of 2.80. They are the market where the home team is
    favoured and the market where the away team is, and a fixture has one of
    each. Deleting the positive direction made the home line negative in every
    stored cloudbet row, so whenever the home team was the underdog the main
    line was the wrong bet — which is what fired
    ``line_favours_the_other_competitor`` on 12 of 33 shared fixtures.
    """
    home_lines = [
        quote.line
        for quote in outcome.quotes
        if quote.market is Market.SPREAD and quote.selection == Selection.HOME.value
    ]
    assert home_lines, "the capture carries no spreads"
    assert any(line > 0 for line in home_lines), "every home line is still negative"
    assert any(line < 0 for line in home_lines)
    assert outcome.skipped.get("mirror_spread_framing", 0) == 0


def test_each_handicap_direction_is_its_own_market(outcome) -> None:
    """Two directions, two market ids — the ids were keyed on ``abs(line)``.

    With the absolute value, ``+1.5`` and ``-1.5`` collapsed onto one id
    carrying two home rows and two away rows. ``src/schema.py`` calls that shape
    out by name: two distinct offers sharing a key is what fires
    ``spread_not_mirrored`` and ``negative_overround``.
    """
    by_market: dict[tuple[str, str, str], dict[str, float]] = {}
    for quote in outcome.quotes:
        if quote.market is not Market.SPREAD:
            continue
        key = (quote.event_key, str(quote.source_market_id), quote.period.value)
        sides = by_market.setdefault(key, {})
        assert quote.selection not in sides, f"two {quote.selection} rows under {key}"
        sides[quote.selection] = quote.line

    paired = [sides for sides in by_market.values() if len(sides) == 2]
    assert paired, "no two-sided spread markets at all"
    for sides in paired:
        assert abs(sides["home"] + sides["away"]) < 1e-9, sides


def test_a_total_keeps_one_number_on_both_sides(outcome) -> None:
    """Only spreads negate.

    Applying the spread's home/away negation to totals produced ``NFL full-game
    total -48.5`` — over 48.5 and under *minus* 48.5. The contract suite caught
    it, and this pins the distinction where the adapter makes it.
    """
    by_market: dict[tuple[str, str, str], set[float]] = {}
    for quote in outcome.quotes:
        if quote.market is not Market.TOTAL:
            continue
        key = (quote.event_key, str(quote.source_market_id), quote.period.value)
        by_market.setdefault(key, set()).add(quote.line)
    assert by_market, "the capture carries no totals"
    for key, lines in by_market.items():
        assert len(lines) == 1, f"{key} holds {sorted(lines)}"
        assert next(iter(lines)) > 0, key
