"""Offline parse pins for DraftKings and Hard Rock first-party adapters."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.raw_store import RawResponse
from src.schema import Market, Selection
from src.sources.caesars import parse_caesars
from src.sources.draftkings import parse_draftkings
from src.sources.hardrock import parse_hardrock

RAW = Path(__file__).resolve().parent / "fixtures" / "raw"


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
