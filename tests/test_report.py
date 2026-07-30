"""The dashboard is a view: it must reflect the database and never invent.

The store is populated here with **synthetic** rows built through
``tests.conftest.make_quote`` rather than through the adapters.  Two reasons: the
page must be testable without the network, and the thing under test is what the
report *says about* rows, which is independent of who produced them.  Every price
in this file is invented; none of it is observed data.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.collector import SOURCE_FACTORIES
from src.report import (
    MIN_BOOKS_FOR_COMPARISON,
    QUOTE_COLUMNS,
    SCHEMA_FIELDS,
    SKIP_NOTES,
    build_report,
    render_fragment,
    render_page,
)
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Side, Sport
from src.store import Store
from tests.conftest import make_quote

# ── a small but structurally complete multi-sport slate ──────────────────────

#: Baseball and basketball are priced by both books; hockey by one only.  That
#: mirrors the real slate this was built against — Pinnacle had no NHL games — and
#: it is the case the page most needs to get right, because a large hockey row
#: count that cannot be compared with anything must not read as coverage.
SLATE = {
    "baseball": {
        "league": "MLB",
        "sport": Sport.BASEBALL,
        "sources": ("book_a", "book_b"),
        "fixtures": (
            ("MLB-PHI", "MLB-MIA", "Philadelphia Phillies", "Miami Marlins"),
            ("MLB-NYM", "MLB-ATL", "New York Mets", "Atlanta Braves"),
        ),
        "total_line": 8.5,
        "spread_line": 1.5,
        "team_total_line": 4.5,
        "extra_period": Period.FIRST_5_INNINGS,
    },
    "basketball": {
        "league": "WNBA",
        "sport": Sport.BASKETBALL,
        "sources": ("book_a", "book_b"),
        "fixtures": (("WNBA-NYL", "WNBA-LVA", "New York Liberty", "Las Vegas Aces"),),
        "total_line": 165.5,
        "spread_line": 6.5,
        "team_total_line": 82.5,
        "extra_period": Period.FIRST_HALF,
    },
    "hockey": {
        "league": "NHL",
        "sport": Sport.HOCKEY,
        "sources": ("book_a",),
        "fixtures": (("NHL-BOS", "NHL-TOR", "Boston Bruins", "Toronto Maple Leafs"),),
        "total_line": 5.5,
        "spread_line": 1.5,
        "team_total_line": 2.5,
        "extra_period": Period.REGULATION,
    },
}

FIXTURE_DAY = "2026-07-28"


def _fixture_quotes(spec: dict, source: str, drift: float) -> list[Quote]:
    out: list[Quote] = []
    for away_key, home_key, away_name, home_name in spec["fixtures"]:
        event_key = f"{away_key}@{home_key}:{FIXTURE_DAY}"
        common = dict(
            source=source,
            sport=spec["sport"],
            league=spec["league"],
            event_key=event_key,
            source_event_id=f"{source}-{event_key}",
            home_participant=home_key,
            away_participant=away_key,
            home_team=home_name,
            away_team=away_name,
        )
        market_id = f"{source}{event_key}"
        out += [
            make_quote(**common, market=Market.MONEYLINE, selection=Selection.HOME,
                       decimal_odds=1.90 + drift, source_market_id=f"{market_id}ml"),
            make_quote(**common, market=Market.MONEYLINE, selection=Selection.AWAY,
                       decimal_odds=2.05 - drift, source_market_id=f"{market_id}ml"),
            make_quote(**common, market=Market.TOTAL, selection=Selection.OVER,
                       line=spec["total_line"], decimal_odds=1.95,
                       source_market_id=f"{market_id}tot"),
            make_quote(**common, market=Market.TOTAL, selection=Selection.UNDER,
                       line=spec["total_line"], decimal_odds=1.95,
                       source_market_id=f"{market_id}tot"),
            make_quote(**common, market=Market.SPREAD, selection=Selection.HOME,
                       line=-spec["spread_line"], decimal_odds=2.40,
                       source_market_id=f"{market_id}sp"),
            make_quote(**common, market=Market.SPREAD, selection=Selection.AWAY,
                       line=spec["spread_line"], decimal_odds=1.62,
                       source_market_id=f"{market_id}sp"),
            make_quote(**common, market=Market.TEAM_TOTAL, selection=Selection.OVER,
                       side=Side.HOME, line=spec["team_total_line"], decimal_odds=1.87,
                       source_market_id=f"{market_id}tt", status=QuoteStatus.SUSPENDED),
            # A window where the scores can end level, so the draw is a real
            # third selection rather than a misparsed runner.
            make_quote(**common, market=Market.MONEYLINE, period=spec["extra_period"],
                       selection=Selection.DRAW, decimal_odds=8.5,
                       source_market_id=f"{market_id}alt"),
        ]
    return out


def slate_quotes() -> list[Quote]:
    quotes: list[Quote] = []
    for spec in SLATE.values():
        for index, source in enumerate(spec["sources"]):
            quotes += _fixture_quotes(spec, source, 0.05 * index)
    return quotes


ROWS_PER_FIXTURE = 8
EXPECTED_ROWS = sum(
    len(spec["fixtures"]) * len(spec["sources"]) * ROWS_PER_FIXTURE for spec in SLATE.values()
)


class _Findings:
    """A stand-in for :class:`src.validation.ValidationReport`.

    Storage never imports the validator — it only reads attributes off whatever it
    is handed — so these tests do not either.  That keeps the report's tests
    independent of a module they are not testing, and it is checked against the
    real thing in ``test_the_real_validation_report_still_fits_the_store``.
    """

    def __init__(self, quotes, errors: int = 0, warnings: int = 0) -> None:
        self.quote_count = len(quotes)
        self.event_count = len({q.event_key for q in quotes})
        self.errors = [_Finding("error", f"synthetic_error_{i}") for i in range(errors)]
        self.warnings = [_Finding("warning", f"synthetic_warning_{i}") for i in range(warnings)]
        self.findings = [*self.errors, *self.warnings]

    @property
    def ok(self) -> bool:
        return not self.errors


class _Finding:
    def __init__(self, severity: str, code: str) -> None:
        self.severity = type("Severity", (), {"value": severity})()
        self.code = code
        self.message = f"{code}: invented for this test"
        self.source = None
        self.event_key = None


@pytest.fixture()
def populated(tmp_path) -> Store:
    """Two stored runs over three sports, the second with one price moved."""
    from src.sources.base import SourceHealth  # local: keeps the import off module load

    store = Store(tmp_path / "report.sqlite3")
    base = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    for index in range(2):
        started = base + timedelta(minutes=5 * index)
        run_id = store.start_run(started)
        quotes = slate_quotes()
        if index == 1:
            first = quotes[0]
            quotes[0] = first.model_copy(update={"decimal_odds": first.decimal_odds + 0.10})
        store.save_quotes(run_id, quotes)
        for source in ("book_a", "book_b"):
            rows = [q for q in quotes if q.source == source]
            store.save_health(
                run_id,
                SourceHealth(
                    source_key=source, ok=True, checked_at=started, request_count=2,
                    raw_bytes=4096, latency_ms=42.0, quote_count=len(rows),
                    event_count=len({q.event_key for q in rows}), skipped_count=15,
                ),
            )
            # book_a was asked for hockey and soccer; it returned hockey only, so
            # soccer is a gap the page has to be able to name.
            configured = (
                [("MLB", "baseball"), ("WNBA", "basketball"), ("NHL", "hockey"), ("EPL", "soccer")]
                if source == "book_a"
                else [("MLB", "baseball"), ("WNBA", "basketball"), ("NHL", "hockey")]
            )
            store.save_league_coverage(
                run_id,
                source,
                [
                    (
                        league,
                        sport,
                        len([q for q in rows if q.league == league]),
                        len({q.event_key for q in rows if q.league == league}),
                    )
                    for league, sport in configured
                ],
            )
        store.save_skipped(run_id, "book_a", {"matchup_type:special": 12, "criterion:Draw No Bet": 3})
        report = _Findings(quotes, warnings=2)
        store.save_findings(run_id, report.findings)
        store.finish_run(
            run_id,
            finished_at=started + timedelta(seconds=1),
            report=report,
            note="usable sports (2+ books): baseball, basketball; one book only: hockey",
        )
    yield store
    store.close()


# ── the payload is the database ──────────────────────────────────────────────


def test_empty_database_is_reported_not_rendered(tmp_path) -> None:
    with Store(tmp_path / "empty.sqlite3") as store:
        with pytest.raises(LookupError):
            build_report(store)


def test_an_incompatible_database_is_refused_with_advice(tmp_path, monkeypatch, capsys) -> None:
    """The dashboard is a view over a schema.  Pointed at an older database it must
    say so and stop, rather than render a page built from columns that mean
    something else."""
    import src.report
    import src.settings
    from tests.test_pipeline import write_v3_database

    old = tmp_path / "legacy.sqlite3"
    write_v3_database(old)
    monkeypatch.setattr(src.settings, "DB_PATH", old)

    assert src.report.main(["--out", str(tmp_path / "out.html")]) == 1
    assert "schema version 3" in capsys.readouterr().err
    assert not (tmp_path / "out.html").exists()


def test_counts_match_the_database(populated: Store) -> None:
    data = build_report(populated)
    stored = populated.query("SELECT COUNT(*) AS n FROM quote")[0]["n"]

    assert len(data["quotes"]["rows"]) == stored
    assert len(data["runs"]) == 2
    assert data["meta"]["latest_run_id"] == 2
    assert [run["quote_count"] for run in data["runs"]] == [EXPECTED_ROWS, EXPECTED_ROWS]
    # Newest first, so the run picker opens on the latest.
    assert [run["id"] for run in data["runs"]] == [2, 1]


def test_rows_decode_back_to_the_stored_values(populated: Store) -> None:
    """The interned payload must survive a round trip, or the page shows nonsense."""
    data = build_report(populated)
    col = {name: i for i, name in enumerate(QUOTE_COLUMNS)}
    strings = data["strings"]
    interned = {"source", "sport", "league", "market", "period", "selection", "event_key",
                "home_participant", "away_participant"}
    wanted = ("source", "sport", "league", "market", "period", "selection", "event_key",
              "home_participant", "away_participant", "decimal_odds")
    decoded = [
        {
            name: (strings[row[col[name]]] if name in interned else row[col[name]])
            for name in wanted
        }
        for row in data["quotes"]["rows"]
    ]
    expected = populated.query(
        f"SELECT {', '.join(wanted)} FROM quote ORDER BY run_id, id"
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


# ── sport and league ─────────────────────────────────────────────────────────


def test_every_sport_is_reported_with_its_books(populated: Store) -> None:
    data = build_report(populated)
    coverage = {entry["sport"]: entry for entry in data["runs"][0]["sports"]}
    assert set(coverage) == {"baseball", "basketball", "hockey", "soccer"}

    assert coverage["baseball"]["books"] == ["book_a", "book_b"]
    assert coverage["basketball"]["books"] == ["book_a", "book_b"]
    # The case that matters: one book, so nothing about it can be compared.
    assert coverage["hockey"]["books"] == ["book_a"]
    # Configured and returned nothing at all — reported, not omitted.
    assert coverage["soccer"]["books"] == []
    assert coverage["soccer"]["quote_count"] == 0


def test_the_two_book_bar_is_stated_per_sport(populated: Store) -> None:
    data = build_report(populated)
    bar = {entry["sport"]: entry["meets_bar"] for entry in data["runs"][0]["sports"]}
    assert bar == {"baseball": True, "basketball": True, "hockey": False, "soccer": False}
    assert data["meta"]["min_books"] == MIN_BOOKS_FOR_COMPARISON

    # Two books is necessary but not sufficient: they must have priced the same
    # fixture.  Both facts are carried, because they come apart in real slates.
    shared = {entry["sport"]: entry["cross_book_events"] for entry in data["runs"][0]["sports"]}
    assert shared == {"baseball": 2, "basketball": 1, "hockey": 0, "soccer": 0}
    comparable = {entry["sport"]: entry["comparable"] for entry in data["runs"][0]["sports"]}
    assert comparable == {"baseball": True, "basketball": True, "hockey": False, "soccer": False}


def test_two_books_with_no_shared_fixture_is_not_called_comparable(tmp_path) -> None:
    """The failure the book count alone cannot see.

    Both books produce hockey, so a "2 books" test passes — but they priced
    different fixtures, so there is still nothing to compare.  This is the shape
    the real slate has: one book carries the NHL openers, another a friendly.
    """
    from src.report import _coverage_for_run

    spec = dict(SLATE["hockey"])
    with Store(tmp_path / "split.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        first = _fixture_quotes(spec, "book_a", 0.0)
        other = dict(spec)
        other["fixtures"] = (("NHL-LA", "NHL-COL", "Los Angeles Kings", "Colorado Avalanche"),)
        second = _fixture_quotes(other, "book_b", 0.05)
        store.save_quotes(run_id, [*first, *second])

        sports, _ = _coverage_for_run(store, run_id)
        hockey = next(entry for entry in sports if entry["sport"] == "hockey")
        assert hockey["books"] == ["book_a", "book_b"]
        assert hockey["meets_bar"] is True
        assert hockey["cross_book_events"] == 0
        assert hockey["comparable"] is False


def test_per_sport_counts_are_the_databases_own(populated: Store) -> None:
    data = build_report(populated)
    for entry in data["runs"][0]["sports"]:
        stored = populated.query(
            "SELECT COUNT(*) n, COUNT(DISTINCT event_key) e FROM quote WHERE run_id = 2 AND sport = ?",
            (entry["sport"],),
        )[0]
        assert entry["quote_count"] == stored["n"], entry["sport"]
        assert entry["event_count"] == stored["e"], entry["sport"]
        for source, count in entry["per_source"].items():
            per_source = populated.query(
                "SELECT COUNT(*) n FROM quote WHERE run_id = 2 AND sport = ? AND source = ?",
                (entry["sport"], source),
            )[0]["n"]
            assert count == per_source


def test_leagues_are_reported_within_their_sport(populated: Store) -> None:
    data = build_report(populated)
    by_sport = {entry["sport"]: entry for entry in data["runs"][0]["sports"]}
    assert [league["league"] for league in by_sport["baseball"]["leagues"]] == ["MLB"]
    assert [league["league"] for league in by_sport["hockey"]["leagues"]] == ["NHL"]
    assert data["league_names"]["MLB"] == "Major League Baseball"
    assert data["league_names"]["EPL"] == "English Premier League"


def test_a_configured_league_that_returned_nothing_is_a_named_gap(populated: Store) -> None:
    """A league absent from the results is otherwise indistinguishable from one
    nobody asked for, which is the difference between a bug and a decision.

    Gaps are per *book*, not per league: book_b was asked for the NHL and returned
    nothing, which is why hockey has only one book — and that is the fact the page
    has to be able to state, rather than just showing a smaller number.
    """
    data = build_report(populated)
    gaps = data["runs"][0]["league_gaps"]
    assert {(gap["source"], gap["league"]) for gap in gaps} == {
        ("book_a", "EPL"),   # asked for, book returned nothing at all
        ("book_b", "NHL"),   # asked for, the other book covered it and this one did not
    }
    assert all(gap["sport"] for gap in gaps)


def test_participants_are_named_from_resolved_identity(populated: Store) -> None:
    data = build_report(populated)
    assert data["participants"]["MLB-MIA"] == {
        "name": "Miami Marlins", "abbr": "MIA", "short": "Marlins",
    }
    assert data["participants"]["NHL-TOR"]["short"] == "Leafs"
    # Every stored participant key has a label, or the page would print the key.
    keys = {row["home_participant"] for row in populated.query("SELECT home_participant FROM quote")}
    assert keys <= set(data["participants"])


def test_sport_facts_come_from_the_settlement_table(populated: Store) -> None:
    """The page's wording is generated from the same rules the pipeline settles on,
    so it cannot describe a hockey total in runs."""
    data = build_report(populated)
    facts = data["sport_facts"]
    assert facts["baseball"]["unit"] == "runs"
    assert facts["hockey"]["unit"] == "goals"
    assert facts["basketball"]["unit"] == "points"
    assert facts["tennis"]["unit"] == "games"
    # Hockey over regulation is three-way; including the shootout it is not.
    assert facts["hockey"]["periods"]["regulation"]["draw_is_priced"] is True
    assert facts["hockey"]["periods"]["full_game"]["draw_is_priced"] is False
    assert facts["soccer"]["periods"]["full_game"]["draw_is_priced"] is True


# ── bounds and scoping ───────────────────────────────────────────────────────


def test_quote_runs_bound_is_stated_not_silent(populated: Store) -> None:
    data = build_report(populated, quote_runs=1)
    run_ids = {row[QUOTE_COLUMNS.index("run_id")] for row in data["quotes"]["rows"]}
    assert run_ids == {2}
    # The page must be able to say that it is showing less than everything.
    assert data["meta"]["runs_recorded"] == 2
    assert data["meta"]["runs_with_rows"] == 1


def test_a_run_without_embedded_rows_still_carries_its_own_totals(populated: Store) -> None:
    """Run 1 has no price rows in the page; its coverage still has to be there, or
    the sport grid would render empty as though nothing had been collected."""
    data = build_report(populated, quote_runs=1)
    older = next(run for run in data["runs"] if run["id"] == 1)
    assert older["quote_count"] == EXPECTED_ROWS
    assert {entry["sport"] for entry in older["sports"]} == {
        "baseball", "basketball", "hockey", "soccer"
    }


def test_detail_tables_are_scoped_to_embedded_runs(populated: Store) -> None:
    data = build_report(populated, quote_runs=1)
    # ``skipped`` is what carries the claim: it holds rows, and they are all run
    # 2's, so nothing from the unembedded run leaked in.
    assert data["skipped"], "an empty table proves nothing about which run it holds"
    assert {row["run_id"] for row in data["skipped"]} == {2}
    # ``raws`` used to carry an ``all(row["run_id"] == 2 ...)`` beside it, which
    # said nothing: this fixture stores no raw responses for *either* run, so the
    # subject was empty and the assertion would have held whatever leaked in.
    # Stated as the fact it is, so a later fixture that does store them makes
    # this fail rather than silently keeping a vacuous guarantee.
    assert data["raws"] == [], "no raw responses in this fixture to scope"


# ── the rendered page ────────────────────────────────────────────────────────


def test_page_reaches_no_network(populated: Store) -> None:
    page = render_page(build_report(populated))
    # Static markup first: nothing the browser would fetch on load.
    markup = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    references = [
        url for url in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', markup)
        if not url.startswith("#")
    ]
    assert references == []
    for forbidden in ("@import", "fetch(", "XMLHttpRequest", "WebSocket", "//fonts."):
        assert forbidden not in page

    # Then the script: it builds links at runtime, and every one of them must be
    # an in-page anchor.  A generated href is the one way an offline page could
    # still acquire a remote reference.
    from src.report_assets import JS

    generated = re.findall(r"""href=\\?["']([^"'$\\]*)""", JS)
    assert all(url.startswith("#") or url == "" for url in generated), generated
    for forbidden in ('href="http', "href='http", "src=\"http"):
        assert forbidden not in JS


def test_page_embeds_no_external_urls_at_all(populated: Store) -> None:
    """Not just no asset references: no http(s) URL that a browser could be made to
    load.  The only URLs in the payload are the sportsbook endpoints the collector
    recorded, and those are data, not links — so this asserts against the markup
    outside the JSON payload."""
    page = render_page(build_report(populated))
    payload = re.search(
        r'<script type="application/json" id="report-data">.*?</script>', page, re.S
    )
    assert payload is not None
    markup = page.replace(payload.group(0), "")
    assert "http://" not in markup
    assert "https://" not in markup
    for cdn in ("cdn.", "unpkg", "jsdelivr", "googleapis", "cdnjs"):
        assert cdn not in markup


def test_the_page_names_every_sport_and_league_it_holds(populated: Store) -> None:
    page = render_page(build_report(populated))
    for sport in ("baseball", "basketball", "hockey"):
        assert sport in page
    for league in ("MLB", "WNBA", "NHL"):
        assert league in page
    assert "Major League Baseball" in page
    # And the bar itself must be on the page, not only in the data.
    assert "not comparable" in page
    assert "Sports &amp; leagues" in page or "Sports & leagues" in page


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


# ── the page's own contracts ─────────────────────────────────────────────────


def test_every_schema_field_is_documented() -> None:
    """A field added to the schema without an explanation would render blank, and the
    schema table is the page's answer to "what is one row?"."""
    documented = {name for name, *_ in SCHEMA_FIELDS}
    assert documented == set(Quote.model_fields)


def test_documented_required_flags_match_the_schema() -> None:
    for name, _kind, required, _note in SCHEMA_FIELDS:
        field = Quote.model_fields[name]
        assert required == field.is_required() or field.default is not None, name


def test_the_vocabularies_shown_are_the_closed_enums(populated: Store) -> None:
    """The page claims these lists are closed, so it must show the real ones —
    including ``sport``, which only became a real dimension in this schema."""
    shown = {entry["name"]: entry["values"] for entry in build_report(populated)["vocabularies"]}
    assert shown["sport"] == [s.value for s in Sport]
    assert shown["market"] == [m.value for m in Market]
    assert shown["period"] == [p.value for p in Period]
    assert shown["selection"] == [s.value for s in Selection]
    assert shown["side"] == [s.value for s in Side]
    assert shown["status"] == [s.value for s in QuoteStatus]


def test_every_element_the_script_reaches_for_exists() -> None:
    """``el('typo')`` returns null and the whole page dies on the first render, with
    nothing on screen to say why.  Cheaper to catch here."""
    from src.report_assets import BODY, JS

    wanted = set(re.findall(r"el\('([a-z0-9-]+)'\)", JS))
    wanted |= set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", JS))
    present = set(re.findall(r'id="([a-z0-9-]+)"', BODY)) | {"report-data"}
    assert wanted - present == set()
    assert len(wanted) > 20, "the extraction stopped matching; this guard is inert"


def test_every_link_lands_on_a_real_section() -> None:
    """A nav entry pointing at a section that does not exist is a dead link; a
    section nothing reaches is a page a reader cannot get to."""
    from src.report_assets import BODY, JS

    linked = set(re.findall(r'<a href="#([a-z-]+)"', BODY))
    sections = set(re.findall(r'<section id="([a-z-]+)"', BODY))
    assert linked <= sections, linked - sections

    # Sections not linked from the markup must be reachable through the script's
    # own routing table, which is how the drill-down panels are opened.
    routed = set(re.findall(r"^\s{2}([a-z_]+):\s*\{\s*title:", JS, re.M))
    assert "overview" in routed, "the routing table stopped matching; this guard is inert"
    unreachable = sections - linked - routed
    assert unreachable == set(), unreachable


def test_the_sport_filter_is_wired_to_every_section() -> None:
    """A filter that some sections honour and others ignore is worse than none: two
    blocks under one heading would then describe different sports."""
    from src.report_assets import JS

    assert "currentSport" in JS
    # Rows reach the sections through exactly one filtered accessor.
    assert JS.count("const currentRows = ()") == 1
    assert "el('sport-pick').addEventListener" in JS
    # The coverage grid is deliberately NOT filtered — it is what tells the reader
    # the other sports exist — so it reads run.sports rather than currentRows().
    grid = JS.split("function renderSports()")[1].split("function renderSources()")[0]
    assert "currentRows()" not in grid


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
    assert "plain English ok" in result.stdout


def test_every_panel_can_be_routed_to(populated: Store, tmp_path) -> None:
    """The page is now a click-through rather than one long scroll, so a panel that
    does not open, or opens empty, is the whole feature failing.

    Driven separately from the render harness because it needs a settable
    ``location.hash``: the render harness deliberately has no ``location`` at all,
    which is what proves the first paint does not depend on the URL.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_routes.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "routing ok" in result.stdout


# ── the pieces this file deliberately stubs ──────────────────────────────────


def test_the_real_validation_report_still_fits_the_store(tmp_path) -> None:
    """These tests hand the store a stand-in report, so this pins the real one.

    Skipped while ``src.validation`` is mid-rewrite rather than silently passing:
    the point of the test is that the two agree.
    """
    validation = pytest.importorskip(
        "src.validation", reason="src.validation is being rewritten by another change"
    )
    with Store(tmp_path / "real.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        quotes = slate_quotes()
        store.save_quotes(run_id, quotes)
        report = validation.validate(quotes)
        store.save_findings(run_id, report.findings)
        store.finish_run(run_id, finished_at=datetime.now(UTC), report=report)
        stored = store.query("SELECT COUNT(*) n FROM finding WHERE run_id = ?", (run_id,))[0]["n"]
        assert stored == len(report.findings)
        assert build_report(store)["runs"][0]["quote_count"] == report.quote_count


@pytest.mark.parametrize("source", sorted(SOURCE_FACTORIES))
def test_every_real_skip_reason_has_an_explanation(source: str) -> None:
    """The page tells the reader why an offer was not collected.  A new skip
    reason with no note renders as a generic shrug, so it fails here first.

    Parametrised off the **registry**, as ``tests/conftest.py`` and
    ``tests/test_source_contract.py`` both are and both say why.  It was a
    hand-written list of three sources, so the five venues added after it was
    written went unchecked — 2,540 of 8,479 skipped records on the captured
    slate rendered as "Not one of the four kinds of game bet collected here",
    including 1,896 of Smarkets' 1,896 and 38 of Kalshi's 38.  Several of those
    were actively false: Smarkets' HANDICAP and OVER_UNDER skips *are* two of
    the four kinds, and an already-started market is not a market type at all.
    """
    import glob

    from src.raw_store import RawStore

    paths = sorted(glob.glob(f"tests/fixtures/raw/{source}__*.json"))
    if not paths:
        pytest.skip(f"no captured responses for {source}")
    store = RawStore("tests/fixtures/raw")
    adapter = SOURCE_FACTORIES[source]()
    try:
        skipped = adapter.parse([store.read(path) for path in paths]).skipped
    finally:
        adapter.close()

    assert skipped, f"{source} skipped nothing, so this guard proves nothing"
    unexplained = [
        reason for reason in skipped
        if not any(reason.startswith(prefix) for prefix, _ in SKIP_NOTES)
    ]
    assert unexplained == [], unexplained
