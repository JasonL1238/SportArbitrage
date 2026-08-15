"""Offline pins for named sportsbook columns on VegasInsider."""
from __future__ import annotations

import glob

import pytest

from src.raw_store import RawResponse, RawStore
from src.schema import Market, Selection
from src.sources.vegasinsider import parse_vegasinsider


#: The 2026-08-03 page carries 8 games × 3 markets × 2 sides = 48 rows per
#: column.  ``vi_hardrock`` publishes eight fewer — four mid-move pairings no
#: book hung at once, each dropped whole by ``refuse_mid_move_pairings``: the
#: LAD@CHC total ``o8.5 +110 / u8.5 −105`` (implied sum 0.9884), the WSH@PHI
#: spread ``home −1.5 +145 / away +1.5 −140`` (0.9915 — the pair the first
#: synthesized ``market_key`` tore in half and never judged), and two
#: **zero-vig** spreads, ``±1.5 −115/+115`` and ``±1.5 −170/+170`` (sum
#: exactly 1.0) — a retail column's genuine pairs on this same capture carry
#: +0.65% to +4.1% margin, so at-fair is as fabricated as below it.
@pytest.mark.parametrize(
    ("source", "expected", "dropped"),
    [
        ("vi_draftkings", 48, 0),
        ("vi_caesars", 48, 0),
        ("vi_hardrock", 40, 8),
        ("vi_fanatics", 48, 0),
        ("vi_bet365", 48, 0),
    ],
)
def test_real_mlb_capture_produces_complete_game_lines(
    source: str, expected: int, dropped: int
) -> None:
    path = glob.glob(f"tests/fixtures/raw/{source}__*.json")
    assert len(path) == 1
    raw = RawStore("tests/fixtures/raw").read(path[0])
    outcome = parse_vegasinsider([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == expected
    assert outcome.skipped["market_prices_the_book_to_lose"] == dropped
    assert {quote.source for quote in outcome.quotes} == {source}
    assert {quote.market for quote in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }


def _pairing_outcome(*quotes):
    from src.sources.base import ParseOutcome

    outcome = ParseOutcome()
    outcome.quotes.extend(quotes)
    return outcome


def _spread_leg(selection, line, decimal, american, **overrides):
    from tests.conftest import make_quote

    return make_quote(
        market=Market.SPREAD, selection=selection, line=line,
        decimal_odds=decimal, american_odds=american, source_market_id=None,
        **overrides,
    )


def test_an_exactly_fair_tracker_pairing_is_dropped_whole() -> None:
    """At-fair is as fabricated as below it on a retail tracker column.

    Hard Rock's genuine pairs on the committed 2026-08-03 capture carry
    +0.65% to +4.1% margin (the floor is SF@TEX at +0.649%, measured — do not
    calibrate a threshold from prose without re-measuring); the same column's ``±1.5 −170/+170`` (implied sum
    exactly 1.0) is a mid-move read the book never hung, and round 2 of the
    adversarial loop measured it publishing with no finding anywhere — the
    guard's old boundary kept anything ≥ fair, and the validation epsilon had
    silenced the last check that fired on it.  A pairing is kept only when it
    carries some margin.
    """
    from src.sources._common import refuse_mid_move_pairings

    fair = _pairing_outcome(
        _spread_leg(Selection.AWAY, 1.5, 1 + 100 / 170, -170),
        _spread_leg(Selection.HOME, -1.5, 2.7, 170),
    )
    refuse_mid_move_pairings("vi_test", fair)
    assert fair.quotes == []
    assert dict(fair.skipped) == {"market_prices_the_book_to_lose": 2}

    margined = _pairing_outcome(
        _spread_leg(Selection.AWAY, 1.5, 1 + 100 / 140, -140),
        _spread_leg(Selection.HOME, -1.5, 2.2, 120),
    )
    refuse_mid_move_pairings("vi_test", margined)
    assert len(margined.quotes) == 2
    assert dict(margined.skipped) == {}


def test_two_disjoint_spread_offers_sharing_a_line_are_not_judged() -> None:
    """home −1.5 and away −1.5 are two markets, not one market's two sides.

    The unsigned ``market_key`` fallback cannot tell them apart, and the sum
    of two disjoint outcomes is legitimately below 1.0 — deleting them as a
    fabricated pairing would be the guard manufacturing the very row loss it
    exists to prevent.  Judged only when the two signed lines are opposites.
    """
    from src.sources._common import refuse_mid_move_pairings

    disjoint = _pairing_outcome(
        _spread_leg(Selection.HOME, -1.5, 2.9, 190),
        _spread_leg(Selection.AWAY, -1.5, 2.9, 190),
    )
    refuse_mid_move_pairings("vi_test", disjoint)
    assert len(disjoint.quotes) == 2
    assert dict(disjoint.skipped) == {}


def test_line_direction_and_total_side_survive_html_parsing() -> None:
    path = glob.glob("tests/fixtures/raw/vi_draftkings__*.json")
    raw = RawStore("tests/fixtures/raw").read(path[0])
    quotes = parse_vegasinsider([raw]).quotes
    first_event = quotes[0].event_key
    by_market_selection = {
        (quote.market, quote.selection): quote
        for quote in quotes if quote.event_key == first_event
    }
    assert by_market_selection[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_market_selection[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_market_selection[Market.TOTAL, Selection.OVER].line == 9.5
    assert by_market_selection[Market.TOTAL, Selection.UNDER].line == 9.5


def _page_raw(endpoint: str, html: str) -> RawResponse:
    """One VegasInsider page as the store would hand it to the parser."""
    import hashlib

    body = html.strip()
    return RawResponse.from_envelope(
        {
            "envelope_version": 3,
            "source": endpoint.rsplit("-", 1)[-1],
            "endpoint": endpoint,
            "url": f"https://www.vegasinsider.com/{endpoint}/",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "fetched_at": "2026-08-14T07:50:00+00:00",
            "request_params": {},
            "headers": {},
            "capture_id": "test-vegasinsider",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "byte_size": len(body.encode("utf-8")),
            "body": body,
        }
    )


def test_a_missing_book_column_is_counted_rather_than_returned_silently() -> None:
    """VegasInsider dropped every per-book column from its MLB page mid-day.

    Measured on 2026-08-14: the 01:54 capture carries eleven columns
    (``Time, Open, Bet365, BetMGM, DraftKings, Caesars, FanDuel, HardRock,
    Fanatics, RiversCasino, Consensus``); the 07:50 capture carries
    ``Time, Open, Consensus`` and **46 fixtures**. The parser looked its book up
    with ``headers.index`` and returned bare on ``ValueError``, so all eight
    ``vi_*`` sources discarded that whole board every run afterwards and reported
    ``parsed 0 quotes from 4 responses; skipped={}``.

    An empty skip dictionary on a parse that dropped everything is precisely what
    ``docs/INPUT_CONTRACT.md`` forbids — it makes "we collected everything"
    unfalsifiable. ``vi_fanatics`` was the only source that hit zero on *all*
    its pages, so it was the only one that surfaced at all; the other seven lost
    one board each and looked merely thin.
    """
    page = """
    <table class="odds-table">
      <thead><th>Time</th><th>Open</th><th>Consensus</th><th></th></thead>
      <tbody id="odds-table-moneyline--0">
        <tr><td class="game-time">7:05 PM</td></tr>
        <tr><th><a class="team-name">Miami Marlins</a></th><td>+120</td></tr>
        <tr><th><a class="team-name">Cincinnati Reds</a></th><td>-140</td></tr>
      </tbody>
    </table>
    """
    raw = _page_raw("odds-mlb-fanatics", page)
    outcome = parse_vegasinsider([raw])

    assert outcome.quotes == []
    assert outcome.rejections == []
    # The book that vanished, by name.
    assert outcome.skipped["book_column_absent:Fanatics"] == 1
    # And what the page did offer, so the diagnosis does not need the bytes.
    assert any(
        key.startswith("columns_offered:") and "Consensus" in key
        for key in outcome.skipped
    ), dict(outcome.skipped)
    assert sum(outcome.skipped.values()) > 0, "a parse that dropped everything must say why"
