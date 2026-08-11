"""Offline parse pins for DraftKings and Hard Rock first-party adapters."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.raw_store import RawResponse, RawStore
from src.schema import Market, Selection
from src.sources.caesars import parse_caesars
from src.sources.draftkings import parse_draftkings
from src.sources.hardrock import parse_hardrock

RAW = Path(__file__).resolve().parent / "fixtures" / "raw"
LIVE_REGRESSIONS = Path(__file__).resolve().parent / "fixtures" / "live_regressions"


def _load(pattern: str) -> RawResponse:
    path = next(RAW.glob(pattern))
    env = json.loads(path.read_text())
    return RawResponse(
        source=env["source"],
        endpoint=env["endpoint"],
        url=env["url"],
        status_code=env["status_code"],
        body=env["body"],
        fetched_at=datetime.fromisoformat(env["fetched_at"]),
        content_type=env.get("content_type"),
        request_params=env.get("request_params") or {},
        headers=env.get("headers") or {},
    )


def test_draftkings_parses_game_lines_from_fixture() -> None:
    raw = _load("draftkings__*_eventgroup-42648_*.json")
    # Fixture commence times are in August 2026; observe before that.
    raw = RawResponse(
        source=raw.source,
        endpoint=raw.endpoint,
        url=raw.url,
        status_code=raw.status_code,
        body=raw.body,
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        content_type=raw.content_type,
        request_params=raw.request_params,
        headers=raw.headers,
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert len(outcome.quotes) == 12
    markets = {q.market for q in outcome.quotes}
    assert markets == {Market.MONEYLINE, Market.SPREAD, Market.TOTAL}
    # Away @ Home: BKN @ PHI → home is Philadelphia
    phi = [q for q in outcome.quotes if q.home_participant == "NBA-PHI"]
    assert phi
    assert all(q.league == "NBA" for q in outcome.quotes)


def test_draftkings_parses_current_sportscontent_shape() -> None:
    """The live Illinois page replaced v5 eventgroups with normalized stores."""
    payload = {
        "events": [{
            "id": "34469547", "name": "WAS Nationals @ PHI Phillies",
            "startEventDate": "2026-08-03T22:40:00Z", "status": "NOT_STARTED",
            "participants": [
                {"name": "PHI Phillies", "venueRole": "Home"},
                {"name": "WAS Nationals", "venueRole": "Away"},
            ],
        }],
        "markets": [
            {"id": "ml", "eventId": "34469547", "name": "Moneyline", "tags": ["PrimaryMarket"]},
            {"id": "rl", "eventId": "34469547", "name": "Run Line", "tags": ["PrimaryMarket"]},
            {"id": "to", "eventId": "34469547", "name": "Total", "tags": ["PrimaryMarket"]},
        ],
        "selections": [
            {"id": "a-ml", "marketId": "ml", "label": "WAS Nationals", "displayOdds": {"american": "+144", "decimal": "2.44"}},
            {"id": "h-ml", "marketId": "ml", "label": "PHI Phillies", "displayOdds": {"american": "−175", "decimal": "1.57"}},
            {"id": "a-rl", "marketId": "rl", "label": "WAS Nationals", "points": 1.5, "displayOdds": {"american": "-143", "decimal": "1.69"}},
            {"id": "h-rl", "marketId": "rl", "label": "PHI Phillies", "points": -1.5, "displayOdds": {"american": "+119", "decimal": "2.19"}},
            {"id": "o", "marketId": "to", "label": "Over", "points": 9.5, "displayOdds": {"american": "+102", "decimal": "2.02"}},
            {"id": "u", "marketId": "to", "label": "Under", "points": 9.5, "displayOdds": {"american": "-122", "decimal": "1.81"}},
        ],
    }
    raw = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }
    by_market_selection = {(q.market, q.selection): q for q in outcome.quotes}
    assert by_market_selection[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_market_selection[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_market_selection[Market.TOTAL, Selection.UNDER].line == 9.5


def test_draftkings_takes_true_odds_over_the_printed_decimal() -> None:
    """``trueOdds`` is the price; ``displayOdds.decimal`` is its 2dp rendering.

    The two disagree in the payload itself, and the American value is the
    referee: on the 2026-08-08 Pennsylvania capture ``trueOdds`` agreed with the
    published American price on 122 of 122 selections and the printed decimal on
    67, drifting up to 2.06% — ``−213`` printed as ``1.46`` against a true
    1.46948357.  Preferring the printed one failed the live run with fourteen
    ``odds_format_mismatch`` errors; before validation caught it, the rounding
    — always downward — quietly understated every payout.
    """
    payload = {
        "events": [{
            "id": "1", "name": "WAS Nationals @ PHI Phillies",
            "startEventDate": "2026-08-03T22:40:00Z", "status": "NOT_STARTED",
            "participants": [
                {"name": "PHI Phillies", "venueRole": "Home"},
                {"name": "WAS Nationals", "venueRole": "Away"},
            ],
        }],
        "markets": [
            {"id": "ml", "eventId": "1", "name": "Moneyline", "tags": ["PrimaryMarket"]},
        ],
        "selections": [
            # The live disagreement, verbatim: −213 encodes 1.46948357, printed 1.46.
            {"id": "h", "marketId": "ml", "label": "PHI Phillies", "trueOdds": 1.46948357,
             "displayOdds": {"american": "−213", "decimal": "1.46"}},
            {"id": "a", "marketId": "ml", "label": "WAS Nationals", "trueOdds": 2.31,
             "displayOdds": {"american": "+131", "decimal": "2.31"}},
        ],
    }
    raw = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections
    by_sel = {q.selection: q for q in outcome.quotes}
    assert abs(by_sel[Selection.HOME].decimal_odds - 1.46948357) < 1e-9, (
        "the row must carry the price, not the page's rounding of it"
    )
    assert by_sel[Selection.HOME].american_odds == -213
    # And a payload with no trueOdds still parses off the printed value — the
    # older shape this converter was written for.
    for sel in payload["selections"]:
        del sel["trueOdds"]
    raw2 = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    fallback = parse_draftkings([raw2])
    assert not fallback.rejections
    assert {q.decimal_odds for q in fallback.quotes} == {1.46, 2.31}


def test_hardrock_joins_root_idx_to_ladder() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    # Observe before the synthetic commence time.
    early = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ladder = RawResponse(
        source=ladder.source, endpoint=ladder.endpoint, url=ladder.url,
        status_code=ladder.status_code, body=ladder.body, fetched_at=early,
        content_type=ladder.content_type, request_params=ladder.request_params,
        headers=ladder.headers,
    )
    events = RawResponse(
        source=events.source, endpoint=events.endpoint, url=events.url,
        status_code=events.status_code, body=events.body, fetched_at=early,
        content_type=events.content_type, request_params=events.request_params,
        headers=events.headers,
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert len(outcome.quotes) == 6
    by_sel = {(q.market, q.selection): q for q in outcome.quotes}
    # fav rootIdx 61 → 1.64516129 (home Red Sox), dog 78 → 2.30 (away Yankees)
    assert abs(by_sel[Market.MONEYLINE, Selection.HOME].decimal_odds - 1.64516129) < 1e-6
    assert abs(by_sel[Market.MONEYLINE, Selection.AWAY].decimal_odds - 2.30) < 1e-6
    assert by_sel[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_sel[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_sel[Market.TOTAL, Selection.OVER].line == 8.5


def test_hardrock_accepts_current_millisecond_event_times() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    payload = events.json()
    expected = datetime.fromisoformat(
        payload["data"]["betSync"]["events"]["data"][0]["eventTime"].replace(
            "Z", "+00:00"
        )
    )
    payload["data"]["betSync"]["events"]["data"][0]["eventTime"] = (
        expected.timestamp() * 1000
    )
    events = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6


def test_hardrock_accepts_current_selection_name_lines() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    payload = events.json()
    for market in payload["data"]["betSync"]["events"]["data"][0]["markets"]:
        line = market.pop("line", None)
        if line is None or market["type"].endswith(":ML"):
            continue
        for selection in market["selection"]:
            if market["type"].endswith(":OU"):
                selection["name"] = f"{selection['name']} {line}"
            elif selection["type"] == "AH":
                selection["name"] = f"{selection['name']} +{abs(line)}"
            else:
                selection["name"] = f"{selection['name']} -{abs(line)}"
    events = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6


def test_current_hardrock_il_capture_replays_offline_without_rejections() -> None:
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    assert {raw.endpoint for raw in raws} == {"ladder", "tree", "events-BASEBALL"}
    tree = next(raw for raw in raws if raw.endpoint == "tree")
    assert tree.request_params == {"segment": "il"}
    outcome = parse_hardrock(raws)
    assert not outcome.rejections
    assert len(outcome.quotes) >= 100


def test_hardrock_reads_the_vs_template_home_first() -> None:
    """Hard Rock's current titles are ``"Home vs Away"``, not ``"Away @ Home"``.

    The venue changed template mid-season: the committed 2026-07-31 capture reads
    ``"New York Yankees @ Boston Red Sox"`` and this 2026-08-04 one reads
    ``"Astros vs Blue Jays"`` — the other order, with nothing in the payload
    stating orientation.  Read away-first, every Hard Rock row carried an inverted
    event key, formed its own fixture and joined nothing; the 2026-08-11 IL slate
    put it against the other 33 feeds on 25 of 25 shared MLB fixtures.
    """
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    events = next(raw for raw in raws if raw.endpoint == "events-BASEBALL")
    titles = {
        event["name"]
        for event in events.json()["data"]["betSync"]["events"]["data"]
    }
    assert "Astros vs Blue Jays" in titles, "fixture no longer carries the vs template"

    outcome = parse_hardrock(raws)
    oriented = {
        (quote.away_participant, quote.home_participant) for quote in outcome.quotes
    }
    assert ("MLB-TOR", "MLB-HOU") in oriented
    assert ("MLB-HOU", "MLB-TOR") not in oriented


def test_hardrock_skips_an_event_name_whose_order_it_cannot_read() -> None:
    """An unverified separator is a skip, never a guessed orientation.

    Guessing costs an inverted event key, which is silent: the rows parse, price
    plausibly, and quietly stop joining every other book.  A counted skip is the
    loud version of the same gap.
    """
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    events = next(raw for raw in raws if raw.endpoint == "events-BASEBALL")
    payload = events.json()
    for event in payload["data"]["betSync"]["events"]["data"]:
        event["name"] = event["name"].replace(" vs ", " - ")
    rewritten = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=events.fetched_at,
        content_type=events.content_type,
        request_params=events.request_params,
        headers=events.headers,
    )
    others = [raw for raw in raws if raw.endpoint != "events-BASEBALL"]
    outcome = parse_hardrock([*others, rewritten])
    assert not outcome.quotes
    assert not outcome.rejections
    assert outcome.skipped["ambiguous_home_away_order"] > 0


def test_caesars_parses_bar_wrapped_names() -> None:
    raw = _load("caesars__*_event_*.json")
    raw = RawResponse(
        source=raw.source, endpoint=raw.endpoint, url=raw.url,
        status_code=raw.status_code, body=raw.body,
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_caesars([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }


def test_hardrock_refuses_unknown_root_idx() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    body = json.loads(events.body)
    body["data"]["betSync"]["events"]["data"][0]["markets"][0]["selection"][0]["rootIdx"] = 99999
    early = datetime(2026, 8, 1, tzinfo=timezone.utc)
    events = RawResponse(
        source=events.source, endpoint=events.endpoint, url=events.url,
        status_code=200, body=json.dumps(body), fetched_at=early,
    )
    ladder = RawResponse(
        source=ladder.source, endpoint=ladder.endpoint, url=ladder.url,
        status_code=ladder.status_code, body=ladder.body, fetched_at=early,
    )
    outcome = parse_hardrock([ladder, events])
    assert any(r.reason == "root_idx_not_on_ladder" for r in outcome.rejections)
