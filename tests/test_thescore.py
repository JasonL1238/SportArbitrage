"""Offline parse pins for the theScore Bet first-party adapter.

The traps this venue carries are all in what a payload *states* rather than in
what it malforms, so most of these build a payload that is perfectly well formed
and wrong — a market typed ``MONEYLINE`` whose sides are "Yes" and "No", a
three-way whose draw has gone, a handicap whose two legs stopped mirroring.
None of those raise anywhere; each one publishes a plausible price.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.raw_store import RawResponse
from src.schema import Market, Period, Selection
from src.sources.thescore import (
    COMPETITIONS,
    SOURCE_KEY,
    TheScoreAdapter,
    parse_thescore,
    redact_anonymous_token,
)

RAW = Path(__file__).resolve().parent / "fixtures" / "raw"


def _load(pattern: str) -> RawResponse:
    path = next(RAW.glob(pattern))
    envelope = json.loads(path.read_text())
    return RawResponse(
        source=envelope["source"],
        endpoint=envelope["endpoint"],
        url=envelope["url"],
        status_code=envelope["status_code"],
        body=envelope["body"],
        fetched_at=datetime.fromisoformat(envelope["fetched_at"]),
        content_type=envelope.get("content_type"),
        request_params=envelope.get("request_params") or {},
        headers=envelope.get("headers") or {},
    )


def _rebody(raw: RawResponse, payload: dict) -> RawResponse:
    return RawResponse(
        source=raw.source,
        endpoint=raw.endpoint,
        url=raw.url,
        status_code=raw.status_code,
        body=json.dumps(payload),
        fetched_at=raw.fetched_at,
        content_type=raw.content_type,
        request_params=raw.request_params,
        headers=raw.headers,
    )


def _cards_of(payload: dict) -> list[dict]:
    return [
        card
        for child in payload["data"]["competitionSection"]["sectionChildren"]
        for card in (child.get("marketplaceShelfChildren") or [])
        if card.get("markets")
    ]


def _pregame(payload: dict) -> dict:
    """The first card that actually produces rows.

    A live board carries in-play events too, and the MLB fixture's first card is
    one — so a mutation applied to ``_cards_of(payload)[0]`` would change a card
    that is skipped before any of these guards run, and every assertion below it
    would pass for the wrong reason.
    """
    for card in _cards_of(payload):
        event = (card.get("event") or {}).get("fallbackEvent") or {}
        if event.get("status") == "PRE_GAME":
            return card
    raise AssertionError("fixture has no pregame card")


@pytest.fixture
def mlb() -> RawResponse:
    return _load("thescore__*_lines-mlb_*.json")


@pytest.fixture
def mls() -> RawResponse:
    return _load("thescore__*_lines-mls_*.json")


# ── the shape of a clean board ───────────────────────────────────────────────


def test_the_committed_board_parses_without_a_single_rejection(mlb) -> None:
    outcome = parse_thescore([mlb])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert outcome.quotes
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE,
        Market.SPREAD,
        Market.TOTAL,
    }
    assert {q.period for q in outcome.quotes} == {Period.FULL_GAME}
    assert {q.league for q in outcome.quotes} == {"MLB"}


def test_a_price_is_the_exact_rational_and_not_the_display_string(mlb) -> None:
    """``numeratorLong / denominatorLong``, never ``formattedOdds``."""
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    market = card["markets"][0]
    selection = market["selections"][0]
    numerator = float(selection["odds"]["numeratorLong"])
    denominator = float(selection["odds"]["denominatorLong"])

    quotes = parse_thescore([mlb]).quotes
    priced = [q for q in quotes if q.source_market_id == market["id"]]
    assert priced
    assert any(q.decimal_odds == pytest.approx(numerator / denominator) for q in priced)


def test_a_soccer_three_way_keeps_its_draw(mls) -> None:
    outcome = parse_thescore([mls])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    by_market: dict[tuple, set[Selection]] = {}
    for quote in outcome.quotes:
        by_market.setdefault(quote.market_key, set()).add(quote.selection)
    assert by_market
    for sides in by_market.values():
        assert sides == {Selection.HOME, Selection.DRAW, Selection.AWAY}


def test_an_in_play_event_is_counted_rather_than_priced(mlb) -> None:
    """Live and pregame events share one board; only the pregame ones are rows."""
    payload = json.loads(mlb.body)
    live = sum(
        1
        for card in _cards_of(payload)
        if ((card.get("event") or {}).get("fallbackEvent") or {}).get("status")
        == "IN_PLAY"
    )
    outcome = parse_thescore([mlb])
    assert outcome.skipped["event_status:IN_PLAY"] == live


# ── the three witnesses ──────────────────────────────────────────────────────


def test_a_first_inning_proposition_typed_as_a_moneyline_produces_no_rows(mlb) -> None:
    """The trap: right type, right selection types, wrong market entirely.

    theScore prices "Run In The 1st Inning - Enhanced Odds" as ``MONEYLINE``
    with ``AWAY_MONEYLINE``/``HOME_MONEYLINE`` sides named "Yes" and "No".
    Nothing downstream catches it — the implied probabilities sum above one, the
    ``market_key`` differs from the real moneyline's, and the selections collide
    with nothing — so it would publish as the game line at a plausible price.
    """
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    moneyline = next(m for m in card["markets"] if m["type"] == "MONEYLINE")
    trap = copy.deepcopy(moneyline)
    trap["id"] = "Market:trap"
    trap["name"] = "Run In The 1st Inning - Enhanced Odds"
    for selection, label in zip(trap["selections"], ("Yes", "No")):
        selection["id"] = f"MarketSelection:trap-{label}"
        selection["name"]["fullName"] = label
        selection["participant"] = None
    card["markets"].append(trap)

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == "Market:trap"]
    # Screened by the *name*, before the selections are ever read — so the
    # counter says which witness answered.
    assert outcome.skipped["market_names_a_sub_period"] == 1
    # And the genuine moneyline in the same payload still publishes.
    assert [q for q in outcome.quotes if q.source_market_id == moneyline["id"]]


def test_a_moneyline_whose_sides_are_not_competitors_is_refused(mlb) -> None:
    """The same trap with a full-game name, so only the third witness is left."""
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    moneyline = next(m for m in card["markets"] if m["type"] == "MONEYLINE")
    trap = copy.deepcopy(moneyline)
    trap["id"] = "Market:trap"
    trap["name"] = "Enhanced Odds"
    for selection, label in zip(trap["selections"], ("Yes", "No")):
        selection["id"] = f"MarketSelection:trap-{label}"
        selection["name"]["fullName"] = label
        selection["participant"] = None
    card["markets"].append(trap)

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == "Market:trap"]
    assert [
        r for r in outcome.rejections if r.reason == "selection_is_not_a_competitor"
    ]


def test_the_two_orientation_witnesses_must_agree(mlb) -> None:
    """``type`` says home and the participant says away: refuse, do not pick one.

    Choosing either one publishes a price on the wrong team, which pairs against
    another book's honest price as a guaranteed profit that loses both legs.
    """
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    moneyline = next(m for m in card["markets"] if m["type"] == "MONEYLINE")
    home = next(s for s in moneyline["selections"] if s["type"] == "HOME_MONEYLINE")
    away = next(s for s in moneyline["selections"] if s["type"] == "AWAY_MONEYLINE")
    home["participant"], away["participant"] = away["participant"], home["participant"]

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == moneyline["id"]]
    assert [
        r for r in outcome.rejections if r.reason == "orientation_witnesses_disagree"
    ]


# ── the completeness guards ──────────────────────────────────────────────────


def test_a_three_way_that_lost_its_draw_publishes_nothing(mls) -> None:
    """A 3-way missing its draw is byte-identical to a genuine 2-way market.

    Published, its two prices leave a third of the probability unbacked, and
    staking both against another book turns a floor of zero into minus the
    stake.  The two surviving legs must go with it.
    """
    payload = json.loads(mls.body)
    card = _pregame(payload)
    market = card["markets"][0]
    market["selections"] = [s for s in market["selections"] if s["type"] != "DRAW"]

    outcome = parse_thescore([_rebody(mls, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == market["id"]]
    assert [
        r
        for r in outcome.rejections
        if r.reason == "three_way_moneyline_without_a_draw"
    ]


def test_a_handicap_whose_legs_stop_mirroring_publishes_nothing(mlb) -> None:
    """The venue signs each leg, so nothing here would recompute the mirror."""
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    spread = next(m for m in card["markets"] if m["type"] == "SPREAD")
    spread["selections"][0]["points"]["decimalPoints"] = 99.5

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == spread["id"]]
    assert [
        r for r in outcome.rejections if r.reason == "handicap_legs_do_not_mirror"
    ]


def test_a_suspended_leg_is_a_counted_skip_rather_than_a_rejection(mlb) -> None:
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    total = next(m for m in card["markets"] if m["type"] == "TOTAL")
    total["selections"][0]["odds"] = None

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert outcome.skipped["selection_without_odds"] == 1
    assert not [
        r for r in outcome.rejections if r.reason == "handicap_legs_do_not_mirror"
    ]


def test_a_two_way_moneyline_on_a_draw_league_is_never_merged(mls) -> None:
    """Soccer's three-way is its own type; a two-way there is a different product."""
    payload = json.loads(mls.body)
    card = _pregame(payload)
    market = card["markets"][0]
    market["type"] = "MONEYLINE"
    market["selections"] = [s for s in market["selections"] if s["type"] != "DRAW"]

    outcome = parse_thescore([_rebody(mls, payload)])
    assert not [q for q in outcome.quotes if q.source_market_id == market["id"]]
    assert outcome.skipped["two_way_moneyline_on_a_draw_league"] == 1


# ── event acceptance ─────────────────────────────────────────────────────────


def test_a_started_event_is_not_priced_against_the_capture_clock(mlb) -> None:
    """"Has this started" is judged against ``raw.fetched_at``, never ``now``."""
    payload = json.loads(mlb.body)
    latest = max(
        ((c.get("event") or {}).get("fallbackEvent") or {}).get("startTime", "")
        for c in _cards_of(payload)
    )
    late = RawResponse(
        source=mlb.source,
        endpoint=mlb.endpoint,
        url=mlb.url,
        status_code=mlb.status_code,
        body=mlb.body,
        fetched_at=datetime.fromisoformat(latest.replace("Z", "+00:00"))
        + timedelta(hours=1),
        content_type=mlb.content_type,
        request_params=mlb.request_params,
        headers=mlb.headers,
    )
    outcome = parse_thescore([late])
    assert not outcome.quotes
    assert outcome.skipped["event_already_started"]


def test_a_competition_the_route_did_not_ask_for_is_counted(mlb) -> None:
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    card["event"]["fallbackEvent"]["competition"]["slug"] = "npb"

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert outcome.skipped["competition_off_route:npb"] == 1


def test_an_event_without_participant_ids_is_refused_not_guessed(mlb) -> None:
    """Without two ids the second orientation witness cannot answer at all."""
    payload = json.loads(mlb.body)
    card = _pregame(payload)
    card["event"]["fallbackEvent"]["homeParticipant"]["id"] = None

    outcome = parse_thescore([_rebody(mlb, payload)])
    assert [r for r in outcome.rejections if r.reason == "missing_participant_ids"]


# ── the capture ──────────────────────────────────────────────────────────────


def test_the_anonymous_token_never_reaches_the_stored_bytes() -> None:
    startup = _load("thescore__*_startup_*.json")
    assert "anonymousToken" in startup.body
    assert "[redacted]" in startup.body
    assert "Bearer" not in startup.body
    # The region survives, because it is the cross-check that makes travelling
    # between states safe.
    assert "US-IL" in startup.body


def test_the_filter_is_too_narrow_to_hide_a_refusal() -> None:
    """``check_http_response`` is handed ``raw.body``, so breadth here is a hazard.

    A filter that rewrote more than one key's value could turn a block page into
    an apparently-good 200 — which is why this one is a single anchored
    substitution rather than a sanitizer.
    """
    block = '{"error":"Access Denied","reason":"blocked","token":"keep-me"}'
    assert redact_anonymous_token(block) == block
    assert redact_anonymous_token('{"anonymousToken":"secret","a":1}') == (
        '{"anonymousToken":"[redacted]","a":1}'
    )


@pytest.mark.parametrize(
    "token", ["plain", 'has"quote', "back\\slash", 'both"\\mixed', "a/b+c="]
)
def test_redaction_fails_closed_on_every_escape_shape(token: str) -> None:
    """A partial match is worse than no match, and no match is worse still.

    The value is a JSON string, so it may hold escapes.  Matching it with
    ``[^"]*`` would stop at an escaped quote and replace a *prefix* — leaving
    the tail of the credential in the bytes inside malformed JSON.  Matching
    with ``[^"\\\\]*`` fails the opposite way and is worse: any backslash and
    the pattern does not match at all, so the whole token is stored.
    """
    body = json.dumps({"data": {"startup": {"anonymousToken": token, "keep": 1}}})
    stored = redact_anonymous_token(body)

    assert token not in stored
    assert json.loads(stored)["data"]["startup"]["anonymousToken"] == "[redacted]"
    # …and the fetch still gets the real value, decoded rather than sliced.
    adapter = TheScoreAdapter()
    adapter._capture_token(body)
    assert adapter._token == token


def test_parsing_is_pure_of_the_instance_that_fetched(mlb, mls) -> None:
    """``replay_run`` builds the adapter with no arguments, so parse must not read it."""
    configured = TheScoreAdapter(
        leagues=["MLB"], api_base="https://sportsbook.us-il.thescore.bet"
    )
    bare = TheScoreAdapter()
    raws = [mlb, mls]
    assert [q.dedup_key for q in bare.parse(raws).quotes] == [
        q.dedup_key for q in parse_thescore(raws).quotes
    ]
    # The configured instance collects MLB alone and still parses both, because
    # the league comes off the envelope label rather than off the instance.
    assert {q.league for q in configured.parse(raws).quotes} == {"MLB", "MLS"}


# ── configuration ────────────────────────────────────────────────────────────


def test_there_is_no_default_edge_to_fall_back_to() -> None:
    """An unrouted instance refuses rather than quietly pricing another state."""
    adapter = TheScoreAdapter()
    with pytest.raises(Exception) as excinfo:
        adapter.fetch_raw()
    assert "api_base" in str(excinfo.value)


def test_every_competition_label_is_distinct_after_slugging() -> None:
    """``_slug`` folds punctuation, so ``lines-la_liga`` and ``lines-la-liga`` collide."""
    from src.raw_store import _slug

    labels = [_slug(comp.label) for comp in COMPETITIONS]
    assert len(labels) == len(set(labels))


def test_every_competition_names_a_registered_league() -> None:
    from src.leagues import is_known

    for comp in COMPETITIONS:
        assert is_known(comp.league), comp
        assert comp.slug == comp.canonical_url.rsplit("/", 1)[-1]


def test_the_source_key_matches_the_module_name() -> None:
    assert SOURCE_KEY == "thescore"
