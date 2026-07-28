"""The dashboard is a view: it must reflect the database and never invent."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.report import (
    QUOTE_COLUMNS,
    SCHEMA_FIELDS,
    SKIP_NOTES,
    build_report,
    render_fragment,
    render_page,
)
from src.schema import BaseballQuote, Market, Period, QuoteStatus, Selection, Side
from src.sources.base import SourceHealth
from src.store import Store
from src.validation import validate
from tests.conftest import make_quote


def _quotes() -> list[BaseballQuote]:
    """A small but structurally complete slate: two books, two games, four markets."""
    out: list[BaseballQuote] = []
    for source, drift in (("book_a", 0.0), ("book_b", 0.05)):
        for event, home, away in (
            ("PHI@MIA:2026-07-28", "Miami Marlins", "Philadelphia Phillies"),
            ("NYM@ATL:2026-07-28", "Atlanta Braves", "New York Mets"),
        ):
            common = dict(
                source=source, event_key=event, home_team=home, away_team=away,
                source_event_id=f"{source}-{event}",
            )
            out += [
                make_quote(**common, market=Market.MONEYLINE, selection=Selection.HOME,
                           decimal_odds=1.90 + drift, source_market_id=f"{source}{event}ml"),
                make_quote(**common, market=Market.MONEYLINE, selection=Selection.AWAY,
                           decimal_odds=2.05 - drift, source_market_id=f"{source}{event}ml"),
                make_quote(**common, market=Market.TOTAL_RUNS, selection=Selection.OVER,
                           line=8.5, decimal_odds=1.95, source_market_id=f"{source}{event}tot"),
                make_quote(**common, market=Market.TOTAL_RUNS, selection=Selection.UNDER,
                           line=8.5, decimal_odds=1.95, source_market_id=f"{source}{event}tot"),
                make_quote(**common, market=Market.RUN_LINE, selection=Selection.HOME,
                           line=-1.5, decimal_odds=2.40, source_market_id=f"{source}{event}rl"),
                make_quote(**common, market=Market.RUN_LINE, selection=Selection.AWAY,
                           line=1.5, decimal_odds=1.62, source_market_id=f"{source}{event}rl"),
                make_quote(**common, market=Market.TEAM_TOTAL_RUNS, selection=Selection.OVER,
                           side=Side.HOME, line=4.5, decimal_odds=1.87,
                           source_market_id=f"{source}{event}tt", status=QuoteStatus.SUSPENDED),
                make_quote(**common, market=Market.MONEYLINE, period=Period.FIRST_5_INNINGS,
                           selection=Selection.DRAW, decimal_odds=8.5,
                           source_market_id=f"{source}{event}f5"),
            ]
    return out


@pytest.fixture()
def populated(tmp_path) -> Store:
    """Two stored runs, the second with one price moved."""
    store = Store(tmp_path / "report.sqlite3")
    base = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    for index in range(2):
        started = base + timedelta(minutes=5 * index)
        run_id = store.start_run(started)
        quotes = _quotes()
        if index == 1:
            first = quotes[0]
            quotes[0] = first.model_copy(update={"decimal_odds": first.decimal_odds + 0.10})
        store.save_quotes(run_id, quotes)
        for source in ("book_a", "book_b"):
            store.save_health(
                run_id,
                SourceHealth(
                    source_key=source, ok=True, checked_at=started, request_count=2,
                    raw_bytes=4096, latency_ms=42.0, quote_count=16, event_count=2,
                    skipped_count=15,
                ),
            )
        store.save_skipped(run_id, "book_a", {"matchup_type:special": 12, "criterion:Draw No Bet": 3})
        store.save_findings(run_id, validate(quotes).findings)
        store.finish_run(
            run_id, finished_at=started + timedelta(seconds=1), report=validate(quotes)
        )
    yield store
    store.close()


def test_empty_database_is_reported_not_rendered(tmp_path) -> None:
    with Store(tmp_path / "empty.sqlite3") as store:
        with pytest.raises(LookupError):
            build_report(store)


def test_counts_match_the_database(populated: Store) -> None:
    data = build_report(populated)
    stored = populated.query("SELECT COUNT(*) AS n FROM quote")[0]["n"]

    assert len(data["quotes"]["rows"]) == stored
    assert len(data["runs"]) == 2
    assert data["meta"]["latest_run_id"] == 2
    assert [run["quote_count"] for run in data["runs"]] == [32, 32]  # 2 books × 2 games × 8
    # Newest first, so the run picker opens on the latest.
    assert [run["id"] for run in data["runs"]] == [2, 1]


def test_rows_decode_back_to_the_stored_values(populated: Store) -> None:
    """The interned payload must survive a round trip, or the page shows nonsense."""
    data = build_report(populated)
    col = {name: i for i, name in enumerate(QUOTE_COLUMNS)}
    strings = data["strings"]
    decoded = [
        {
            name: (strings[row[col[name]]] if isinstance(row[col[name]], int)
                   and name in {"source", "market", "period", "selection", "event_key"}
                   else row[col[name]])
            for name in ("source", "market", "period", "selection", "event_key", "decimal_odds")
        }
        for row in data["quotes"]["rows"]
    ]
    expected = populated.query(
        "SELECT source, market, period, selection, event_key, decimal_odds FROM quote ORDER BY run_id, id"
    )
    assert decoded == [dict(row) for row in expected]


def test_null_columns_survive_interning(populated: Store) -> None:
    """``side`` and ``line`` are absent on a moneyline; null must stay null and not
    become the string "None"."""
    data = build_report(populated)
    col = {name: i for i, name in enumerate(QUOTE_COLUMNS)}
    moneylines = [
        row for row in data["quotes"]["rows"]
        if data["strings"][row[col["market"]]] == "moneyline"
    ]
    assert moneylines
    assert all(row[col["side"]] is None and row[col["line"]] is None for row in moneylines)
    assert "None" not in data["strings"]


def test_quote_runs_bound_is_stated_not_silent(populated: Store) -> None:
    data = build_report(populated, quote_runs=1)
    run_ids = {row[QUOTE_COLUMNS.index("run_id")] for row in data["quotes"]["rows"]}
    assert run_ids == {2}
    # The page must be able to say that it is showing less than everything.
    assert data["meta"]["runs_recorded"] == 2
    assert data["meta"]["runs_with_rows"] == 1


def test_detail_tables_are_scoped_to_embedded_runs(populated: Store) -> None:
    data = build_report(populated, quote_runs=1)
    assert {row["run_id"] for row in data["skipped"]} == {2}
    assert all(row["run_id"] == 2 for row in data["raws"])


def test_page_reaches_no_network(populated: Store) -> None:
    page = render_page(build_report(populated))
    references = [
        url for url in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', page)
        if not url.startswith("#")
    ]
    assert references == []
    for forbidden in ("@import", "fetch(", "XMLHttpRequest", "WebSocket", "//fonts."):
        assert forbidden not in page


def test_embedded_payload_cannot_break_out_of_its_script_tag(populated: Store) -> None:
    data = build_report(populated)
    data["runs"][0]["note"] = "</script><script>alert(1)</script>"
    page = render_page(data)
    body = re.search(
        r'<script type="application/json" id="report-data">(.*?)</script>', page, re.S
    )
    assert body is not None
    assert "</script" not in body.group(1)
    assert json.loads(body.group(1))["runs"][0]["note"] == "</script><script>alert(1)</script>"


def test_fragment_omits_the_document_shell(populated: Store) -> None:
    """A host that supplies its own <head> must not receive a second one."""
    fragment = render_fragment(build_report(populated))
    assert "<!doctype" not in fragment.lower()
    assert "<html" not in fragment.lower()
    assert "<style>" in fragment and 'id="report-data"' in fragment


def test_replay_verdict_is_carried_verbatim(populated: Store) -> None:
    data = build_report(populated, replay_note="FAIL (3)")
    assert data["meta"]["replay_note"] == "FAIL (3)"
    # Never a default that reads as success.
    assert build_report(populated)["meta"]["replay_note"] == "not checked"


def test_every_schema_field_is_documented() -> None:
    """A field added to the schema without an explanation would render blank, and the
    schema table is the page's answer to "what is one row?"."""
    documented = {name for name, *_ in SCHEMA_FIELDS}
    assert documented == set(BaseballQuote.model_fields)


def test_documented_required_flags_match_the_schema() -> None:
    for name, _kind, required, _note in SCHEMA_FIELDS:
        field = BaseballQuote.model_fields[name]
        assert required == field.is_required() or field.default is not None, name


def test_the_page_script_runs_and_fills_every_region(populated: Store, tmp_path) -> None:
    """Execute the dashboard's own script against a real page.

    Python can check the payload but not the page.  A missing payload field does not
    raise in JavaScript — it renders as ``NaN`` or ``undefined``, which reads as
    missing data rather than as a bug.  This ran the two real defects to ground during
    development, so it stays in the suite.  Skipped where node is absent: the pipeline
    itself never needs it.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script ran clean" in result.stdout
    assert "svg well-formed" in result.stdout


def test_every_element_the_script_reaches_for_exists() -> None:
    """``el('typo')`` returns null and the whole page dies on the first render, with
    nothing on screen to say why.  Cheaper to catch here."""
    from src.report_assets import BODY, JS

    wanted = set(re.findall(r"el\('([a-z0-9-]+)'\)", JS))
    wanted |= set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", JS))
    present = set(re.findall(r'id="([a-z0-9-]+)"', BODY)) | {"report-data"}
    assert wanted - present == set()
    assert len(wanted) > 20, "the extraction stopped matching; this guard is inert"


def test_the_run_scoped_sections_all_have_a_renderer() -> None:
    """A nav entry pointing at a section nothing fills would look like a data gap."""
    from src.report_assets import BODY

    linked = set(re.findall(r'<a href="#([a-z-]+)"', BODY))
    sections = set(re.findall(r'<section id="([a-z-]+)"', BODY))
    assert linked == sections


@pytest.mark.parametrize("source", ["fanduel", "pinnacle", "betrivers_kambi"])
def test_every_real_skip_reason_has_an_explanation(source: str, request) -> None:
    """The page tells the reader why an offer was not collected.  A new skip reason
    with no note would silently render as a generic shrug, so it fails here first."""
    from src.sources.betrivers_kambi import BetRiversKambiAdapter
    from src.sources.fanduel import FanDuelAdapter
    from src.sources.pinnacle import PinnacleAdapter

    adapters = {
        "fanduel": (FanDuelAdapter, "fanduel_raw"),
        "pinnacle": (PinnacleAdapter, "pinnacle_raw"),
        "betrivers_kambi": (BetRiversKambiAdapter, "kambi_raw"),
    }
    adapter_cls, fixture = adapters[source]
    adapter = adapter_cls()
    try:
        skipped = adapter.parse(request.getfixturevalue(fixture)).skipped
    finally:
        adapter.close()

    assert skipped, f"{source} skipped nothing, so this guard proves nothing"
    unexplained = [
        reason for reason in skipped
        if not any(reason.startswith(prefix) for prefix, _ in SKIP_NOTES)
    ]
    assert unexplained == []
