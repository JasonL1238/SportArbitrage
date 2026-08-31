"""The dashboard is a view: it must reflect the database and never invent.

The store is populated here with **synthetic** rows built through
``tests.conftest.make_quote`` rather than through the adapters.  Two reasons: the
page must be testable without the network, and the thing under test is what the
report *says about* rows, which is independent of who produced them.  Every price
in this file is invented; none of it is observed data.
"""
from __future__ import annotations

import json
import os
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
    build_report,
    render_fragment,
    render_page,
)
from src.report_copy import SCHEMA_FIELDS, SKIP_NOTES
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
        # ``smarkets`` is a real registered venue and it charges commission, so
        # the fixture contains at least one row whose net price differs from its
        # quoted one.  Without that, every commission-aware path on the page was
        # inert under test: ``netOdds``, ``americanOf``, ``charges`` and
        # ``venueKind`` could each be replaced by a constant and the harness
        # stayed green, because two synthetic sportsbooks never exercise them.
        "sources": ("book_a", "book_b", "smarkets"),
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
            # One side of a total with no counterpart — a group the overview
            # used to count under "each with every side priced".  Kept active so
            # the active filter alone cannot hide it.
            make_quote(**common, market=Market.TOTAL, selection=Selection.OVER,
                       line=spec["total_line"] + 1.0, decimal_odds=1.80,
                       source_market_id=f"{market_id}tot-half"),
            # A window where the scores can end level: a complete three-way, so
            # the page has both a market it may sum and — by dropping the draw
            # in a harness case — one it must refuse.  Without the home and away
            # legs here, ``sumsToAMargin``'s moneyline-shape check was only ever
            # exercised against two-way windows, and ``return rows.length >= 2``
            # produced a page byte-identical to the real rule.
            make_quote(**common, market=Market.MONEYLINE, period=spec["extra_period"],
                       selection=Selection.HOME, decimal_odds=2.20 + drift,
                       source_market_id=f"{market_id}alt"),
            make_quote(**common, market=Market.MONEYLINE, period=spec["extra_period"],
                       selection=Selection.AWAY, decimal_odds=2.30 - drift,
                       source_market_id=f"{market_id}alt"),
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


ROWS_PER_FIXTURE = 11
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
        for source in ("book_a", "book_b", "smarkets"):
            rows = [q for q in quotes if q.source == source]
            if not rows:
                continue
            store.save_health(
                run_id,
                SourceHealth(
                    source_key=source, ok=True, checked_at=started, request_count=2,
                    raw_bytes=4096, latency_ms=42.0, quote_count=len(rows),
                    event_count=len({q.event_key for q in rows}), skipped_count=15,
                ),
            )
            # book_a was asked for hockey and soccer; it returned hockey only, so
            # soccer is a gap the page has to be able to name.  smarkets is on
            # baseball only — it is here so the fixture contains a venue that
            # charges commission, not so it covers every sport.
            if source == "smarkets":
                configured = [("MLB", "baseball")]
            elif source == "book_a":
                configured = [
                    ("MLB", "baseball"), ("WNBA", "basketball"),
                    ("NHL", "hockey"), ("EPL", "soccer"),
                ]
            else:
                configured = [
                    ("MLB", "baseball"), ("WNBA", "basketball"), ("NHL", "hockey"),
                ]
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

    assert coverage["baseball"]["books"] == ["book_a", "book_b", "smarkets"]
    assert coverage["basketball"]["books"] == ["book_a", "book_b"]
    # The case that matters: one book, so nothing about it can be compared.
    assert coverage["hockey"]["books"] == ["book_a"]
    # Configured and returned nothing at all — reported, not omitted.
    assert coverage["soccer"]["books"] == []
    assert coverage["soccer"]["quote_count"] == 0


def test_a_venue_whose_prices_are_on_the_page_is_described(tmp_path) -> None:
    """Health rows alone used to decide who the page knew how to describe.

    A source whose quotes were embedded but whose health row was not — a
    ``--runs`` larger than ``--quote-runs``, or a fixture that stored prices
    without a health row — was absent from ``SOURCE_INFO``.  The page then
    fell back to the raw key, treated the venue as a free sportsbook, and
    printed the *gross* American odds beside the *net* return for every one of
    its rows.  The commission-aware paths (``netOdds``, ``americanOf``,
    ``charges``, ``venueKind``) were all inert under that shape.
    """
    from src.sources.base import SourceHealth

    store = Store(tmp_path / "quoted-only.sqlite3")
    started = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    run_id = store.start_run(started)
    quotes = slate_quotes()
    store.save_quotes(run_id, quotes)
    # Health for the free books only — smarkets has prices and no health row.
    for source in ("book_a", "book_b"):
        rows = [q for q in quotes if q.source == source]
        store.save_health(
            run_id,
            SourceHealth(
                source_key=source, ok=True, checked_at=started, request_count=1,
                quote_count=len(rows), event_count=1,
            ),
        )
    store.finish_run(run_id, finished_at=started, report=_Findings(quotes))

    data = build_report(store)
    by_key = {entry["key"]: entry for entry in data["sources"]}
    assert "smarkets" in by_key
    assert by_key["smarkets"]["commission"], by_key["smarkets"]
    assert by_key["smarkets"]["kind"] == "exchange"


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
    for forbidden in ("@import", "XMLHttpRequest", "WebSocket", "//fonts."):
        assert forbidden not in page
    # ``fetch('/api/…')`` calls are the scrape/plan buttons talking to the local
    # ``--serve`` control plane.  They are same-origin localhost only, and the
    # buttons stay disabled on ``file://``.  Rather than pinning how many there
    # are — a count that went stale every time an endpoint was added, while
    # saying nothing about *where* they reach — every fetch target must be a
    # string literal naming a same-origin ``/api/`` path.  A template literal,
    # a variable, or an absolute URL all fail the match and the test.
    #
    # Audited over the page's *code*, not its data: the JSON payload embeds
    # scraped text, and a venue's promo terms have carried that venue's own
    # telemetry script — ``fetch(ajaxurl, …)`` — as inert, escaped string data.
    # Counting data as code makes the guard fail on what a book wrote, not on
    # what this page does.
    code = re.sub(
        r'<script type="application/json" id="report-data">[\s\S]*?</script>',
        "",
        page,
    )
    fetch_targets = re.findall(r"""fetch\(\s*(['"])(.*?)\1""", code)
    assert fetch_targets, "the serve control plane's fetch calls have vanished"
    assert code.count("fetch(") == len(fetch_targets), (
        "a fetch() whose target is not a plain string literal cannot be "
        "audited for same-origin; use a literal '/api/...' path"
    )
    for _, target in fetch_targets:
        assert target.startswith("/api/"), (
            f"fetch target {target!r} is not a same-origin /api/ path"
        )
    assert "fetch('/api/collect'" in code
    assert "fetch('http" not in code and 'fetch("http' not in code

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
    # Each list is asserted against the enum it is built from, so on its own this
    # only catches a vocabulary being *dropped* from the payload — two empty
    # lists compare equal, and so do two renamed ones.  The neighbour above
    # carries an inertness guard for the same reason; this one had none.
    assert set(shown) >= {"sport", "market", "period", "selection", "side", "status"}
    assert all(values for values in shown.values()), shown
    assert "baseball" in shown["sport"] and "moneyline" in shown["market"]
    assert "full_game" in shown["period"] and "draw" in shown["selection"]
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
    # The promo-plan block's own message.  Without this the whole block — every
    # guard rounds 2-4 added to it — could be deleted and this driver would not
    # notice, because it only ever asserted its neighbours' output.
    assert "promo plan cards render" in result.stdout
    # This page embeds every run, so the truncation branch is unreachable here
    # and the harness says so rather than passing silently.
    assert "truncation not exercised" in result.stdout
    # Same reason as the promo-plan line above, for the blocks added since: each
    # of these is a whole check whose deletion would otherwise leave this driver
    # green, because it only ever asserts its neighbours' output.
    for marker in (
        "the sportsbook picker narrows every surface",
        "the remembered switch and pick round-trip through storage",
        "the scrape machine drives both kinds",
        "an impossible sportsbook pick is forgotten",
        "the after-tax rule caps the deduction",
        "each sortable table holds its own sort",
        "a venue page with nothing to show asks for a venue",
    ):
        assert marker in result.stdout, f"the harness never reported: {marker}"


def test_the_page_wakes_up_holding_what_was_left_in_storage(
    populated: Store, tmp_path
) -> None:
    """The remembered offshore switch and sportsbook pick are read exactly once,
    while the page's script is being evaluated.

    A harness process that starts with an empty store can therefore only ever
    prove the *write* half: with ``let showOffshore = false`` hardcoded in place
    of the read, every write still lands in the right place in the right format
    and the ordinary smoke run stays green. Proving the read needs a process that
    was already holding something when the page loaded, which is what the seed
    is.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "SPORTARB_SMOKE_SEED": json.dumps(
                {
                    "sportarb.showOffshore": "1",
                    "sportarb.book": "book_a",
                    # The rates are parsed out of these strings once, while the
                    # script evaluates.  A rate stored as a number reads back as
                    # no tax, which is indistinguishable from never having set
                    # one — the same class of defect as the switch above.
                    "sportarb.taxFed": "0.24",
                    "sportarb.taxState": "0.0495",
                    "sportarb.taxCap": "0",
                }
            ),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the page wakes up holding what was left in storage" in result.stdout


def test_the_page_script_runs_against_a_truncated_payload(
    populated: Store, tmp_path
) -> None:
    """The same script, on a page whose picker lists more collections than it
    carries prices for.

    This is the ordinary shape of a real page — ``--quote-runs`` is what keeps a
    dashboard from being tens of megabytes — and the harness had never seen it.
    Everything above renders every run's prices, so ``detailLoaded`` was only
    ever asked about runs that *were* loaded: it could be replaced outright with
    ``return true`` and the whole suite stayed green, while the page told a
    reader that a collection holding 71 saved responses had saved nothing.

    Two runs, one embedded.  The harness switches the picker to the other one,
    which is the only way any of this is reachable.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    page = tmp_path / "dashboard-truncated.html"
    page.write_text(
        render_page(build_report(populated, quote_runs=1)), encoding="utf-8"
    )
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script ran clean" in result.stdout
    assert "truncation is distinguished" in result.stdout
    assert "named on screen" in result.stdout


def test_a_pickem_spread_does_not_false_fail_the_drill_down_harness(
    populated: Store, tmp_path
) -> None:
    """The mirrored-rung check must not convict a correct page at line 0.

    A pick'em spread's mirror carries the SAME line (``-0 === 0``), so the
    injected rung legitimately belongs to the sibling group, and the first
    spelling of the harness check read that as "abs-merge is back" — the
    first committed fixture holding a pick'em spread would have hard-failed
    every harness-driven test against a correct implementation.  The harness
    now prefers a non-zero-line target and, when the whole board is pick'em,
    says so and skips the injection instead of inventing a defect.  This
    page's newest run holds ONLY line-0 spreads, which is the board that
    used to false-fail.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    from src.sources.base import SourceHealth
    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=odds,
                   market=Market.SPREAD, line=0.0, source_market_id="sp-pk",
                   commence_time=kickoff)
        for source, selection, odds in (
            ("book_a", Selection.HOME, 1.95),
            ("book_a", Selection.AWAY, 1.87),
            ("book_b", Selection.HOME, 1.93),
            ("book_b", Selection.AWAY, 1.89),
            # The offshore-switch block requires the current run to hold an
            # offshore row whenever the payload holds any; smarkets is this
            # fixture's offshore venue, and its pair keeps the board pick'em.
            ("smarkets", Selection.HOME, 1.96),
            ("smarkets", Selection.AWAY, 1.90),
        )
    ]
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    run = populated.start_run(started)
    populated.save_quotes_by_source(run, rows)
    for source in ("book_a", "book_b", "smarkets"):
        count = len([q for q in rows if q.source == source])
        populated.save_health(
            run,
            SourceHealth(
                source_key=source, ok=True, checked_at=started, request_count=1,
                raw_bytes=1024, latency_ms=40.0, quote_count=count,
                event_count=1, skipped_count=1,
            ),
        )
    populated.save_skipped(run, "book_a", {"matchup_type:special": 1})
    populated.finish_run(
        run, finished_at=started + timedelta(seconds=1),
        report=ValidationReport(quote_count=len(rows), event_count=1),
        counterparties={},
    )

    page = tmp_path / "dashboard-pickem.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mirrored-rung check skipped; only pick'em spreads" in result.stdout, (
        result.stdout
    )


def test_the_page_paints_the_locality_labels(populated: Store, tmp_path) -> None:
    """Execute the renderer against a run that actually carries a flagged position.

    The static pins on the JS source (`no_local_leg` / `non_local_label` appear in
    the text) cannot tell a rendered badge from a comment: dead-coding the badge
    while leaving its name in the file survives them. The harness's locality block
    reads the painted DOM instead — and this test is what keeps that block from
    reporting "not exercised" forever, by storing a PA state run whose only
    position has no reachable leg.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=odds,
                   source_market_id="m", commence_time=kickoff)
        for source, selection, odds in (
            ("pinnacle", Selection.HOME, 2.20),
            ("bovada", Selection.AWAY, 2.20),
            # A worse local price on the same market. It changes no leg of the
            # position, but the page opens on this run in the US-only view, and
            # with pinnacle and bovada both offshore an all-offshore slate would
            # leave that view rowless — which fails the routing check for
            # reasons that have nothing to do with locality.
            ("fanduel", Selection.HOME, 2.00),
        )
    ]
    # A second sport with no flagged position, so the harness can filter the
    # flagged one off the page and check the note admits the filter hid it —
    # with one sport in the run that branch of the locality block never runs.
    rows += [
        make_quote(source=source, selection=selection, decimal_odds=odds,
                   sport=Sport.HOCKEY, league="NHL",
                   event_key=f"NHL-PIT@NHL-PHI:{kickoff:%Y-%m-%d}",
                   source_event_id="evt-nhl", source_market_id="m-nhl",
                   home_participant="NHL-PHI", away_participant="NHL-PIT",
                   home_team="Philadelphia Flyers", away_team="Pittsburgh Penguins",
                   commence_time=kickoff)
        for source, selection, odds in (
            ("fanduel", Selection.HOME, 1.90),
            ("draftkings", Selection.AWAY, 1.90),
        )
    ]
    started = datetime(2026, 7, 28, 5, 0, tzinfo=UTC)
    run = populated.start_run(
        started, jurisdiction="PA", route_scope="state",
    )
    populated.save_quotes_by_source(run, rows)
    # The page opens on this run, and the harness refuses a page with empty
    # regions — so the run needs the same health/skip furniture the fixture's
    # own runs carry, not just its three quotes.
    from src.sources.base import SourceHealth

    for source in ("pinnacle", "bovada", "fanduel"):
        count = len([q for q in rows if q.source == source])
        populated.save_health(
            run,
            SourceHealth(
                source_key=source, ok=True, checked_at=started, request_count=1,
                raw_bytes=1024, latency_ms=40.0, quote_count=count,
                event_count=1, skipped_count=1,
            ),
        )
    populated.save_skipped(run, "pinnacle", {"matchup_type:special": 1})
    populated.finish_run(
        run, finished_at=started + timedelta(seconds=1),
        report=ValidationReport(quote_count=len(rows), event_count=1),
        counterparties={},
    )

    page = tmp_path / "dashboard-locality.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "locality labels render" in result.stdout, result.stdout
    # The filtered-view half of the block skips itself when every sport holds a
    # flagged position; this page carries the NHL game precisely so it cannot,
    # and losing those rows must fail here rather than shrink the check.
    assert "filtered-note check skipped" not in result.stdout, result.stdout


def test_the_page_describes_a_quarter_line_the_way_the_detector_settles_it(
    populated: Store, tmp_path
) -> None:
    """The sentence and the stake sizing must agree about who is paid.

    A quarter line splits the stake across two half-lines, so at the one score
    between them half pushes and half settles — for *one* side as a half-win and
    for the other as a half-loss, never both.  The page told both sides it "pays
    half": on 127 of the 254 quarter-line rows in the committed captures it
    promised a payout to the reader who was losing half the stake there, and the
    two sentences appeared side by side on the same fixture panel.  It also named
    scores that cannot occur ("a 0.5-goal loss") and thresholds that overlapped
    the split ("win by 1 or more; a 1-goal win pays half").

    The expectations are generated here, from :func:`src.arb.settlement_outcomes`
    — the function that decides what the position is actually worth — so this
    cannot be satisfied by editing a string in the harness.  It is the same
    shape as the overround cross-check: one oracle, two languages.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the dashboard's script cannot be executed here")

    from src.arb import settlement_outcomes

    cases: list[dict] = []
    plans = [
        (Sport.SOCCER, Market.SPREAD, (Selection.HOME, Selection.AWAY),
         (-2.75, -1.75, -1.25, -0.75, -0.25, 0.25, 0.75, 1.25, 1.75, 2.75)),
        (Sport.SOCCER, Market.TOTAL, (Selection.OVER, Selection.UNDER),
         (2.25, 2.75, 3.25, 3.75, 4.25)),
        (Sport.BASKETBALL, Market.SPREAD, (Selection.HOME, Selection.AWAY),
         (-6.75, -6.25, 6.25, 6.75)),
    ]
    for sport, market, shape, lines in plans:
        for line in lines:
            outcomes = settlement_outcomes(
                sport, market, Period.FULL_GAME, line, set(shape)
            )
            landing, verdicts = next(
                (name, mapping) for name, mapping in outcomes
                if any(str(v).startswith("half") for v in mapping.values())
            )
            for selection in shape:
                # Each row stores the line from its own side's perspective, and
                # that is what the page is handed.
                own = line if selection in (Selection.HOME, Selection.OVER) else -line
                cases.append({
                    "sport": sport.value,
                    "market": market.value,
                    "selection": selection.value,
                    "line": own if market is Market.SPREAD else line,
                    "half_wins": str(verdicts[selection]) == "half_win",
                    "landing": abs(int(round(line))),
                })
    assert cases and any(c["half_wins"] for c in cases)
    assert not all(c["half_wins"] for c in cases), "the oracle must discriminate"

    expectations = tmp_path / "quarter-lines.json"
    expectations.write_text(json.dumps(cases), encoding="utf-8")
    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")
    harness = Path(__file__).parent / "dashboard_smoke.mjs"
    result = subprocess.run(
        [node, str(harness), str(page), str(expectations)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "quarter lines agree with the detector" in result.stdout


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


def test_the_scrape_controls_are_on_the_page() -> None:
    from src.report_assets import BODY, JS

    for needle in (
        "scrape-btn", "scrape-scope", "scrape-status", "scrape-progress",
        "run-list", "/api/collect", "/api/status", "paintScrape",
        "promo-scrape-btn", "promo-scrape-status", "/api/promos/collect",
        "promo-region", "promo-detail", "usage_guidance", "selectedPromoKey",
        "/api/promos/status", "SCRAPE_KINDS", 'id="promos"',
    ):
        assert needle in BODY or needle in JS, needle
    assert 'id="history"' in BODY
    assert "PRIMARY_PANELS" in JS
    assert "selectRun" in JS
    assert "wireScrape(SCRAPE_KINDS.odds)" in JS
    assert "wireScrape(SCRAPE_KINDS.promos)" in JS
    # Front door is arbitrage; past scrapes live under History, not the rail.
    assert BODY.index('href="#arb"') < BODY.index('href="#history"')
    assert BODY.index('href="#promos"') < BODY.index('href="#history"')
    assert 'id="run-list"' in BODY
    assert "run-block" not in BODY
    assert "run-pick-screen" not in BODY
    # AN Open is view-only: shown for context, never best-price highlighted.
    assert "isViewOnly" in JS
    assert "view_only" in JS


def test_rebuild_dashboard_writes_the_newest_run(populated: Store, tmp_path, monkeypatch) -> None:
    from src import report as report_mod
    from src import settings as settings_mod

    out = tmp_path / "dashboard.html"
    monkeypatch.setattr(settings_mod, "DB_PATH", populated.path)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", populated.path)
    info = report_mod._rebuild_dashboard(
        out, run_limit=10, quote_runs=2, max_quote_rows=50_000,
    )
    assert out.exists() and out.stat().st_size > 1000
    assert info["run_id"] == populated.latest_run_id()
    text = out.read_text(encoding="utf-8")
    assert 'id="run-list"' in text
    assert 'id="scrape-btn"' in text


def test_run_collect_from_ui_rejects_a_bad_tier() -> None:
    from src import report as report_mod

    with pytest.raises(ValueError, match="unknown tier"):
        report_mod._run_collect_from_ui(tier="turbo", sport=None, league="MLB")


def test_promo_payload_and_build_report_include_promos(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)

    empty = report_mod._promo_payload()
    assert empty["run"] is None
    assert empty["offers"] == []

    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id,
        ok=True,
        offers=[
            PromoOffer(
                source="draftkings",
                offer_id="welcome",
                kind=PromoKind.SIGNUP_BONUS,
                title="Welcome Bonus",
                summary="Bet $5, get $150 in bonus bets",
                terms="New customers in NJ only. Min odds -200.",
                observed_at=datetime.now(UTC),
                url="https://example.test/promo",
                eligible_regions=["NJ"],
                bonus_amount=150.0,
                reward_type="bonus_bets",
                usage_guidance="Hedge the bonus bet at another book.",
                is_specific=True,
            )
        ],
        health=[
            PromoSourceHealth(
                source_key="draftkings",
                ok=True,
                checked_at=datetime.now(UTC),
                offer_count=1,
            )
        ],
    )
    store.close()

    payload = report_mod._promo_payload()
    assert payload["run"]["id"] == run_id
    assert payload["offers"][0]["title"] == "Welcome Bonus"
    assert payload["offers"][0]["summary"].startswith("Bet $5")
    assert payload["offers"][0]["terms"]
    assert payload["offers"][0]["eligible_regions"] == ["NJ"]
    assert payload["offers"][0]["usage_guidance"]
    assert payload["offers"][0]["is_specific"] is True
    # The state verdict rides the payload so the panel can label an offer
    # whose copy never named the run's state instead of hiding it.
    assert payload["offers"][0]["state_confirmed"] is False
    assert payload["health"][0]["source_key"] == "draftkings"

    monkeypatch.setattr(settings_mod, "DB_PATH", populated.path)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", populated.path)
    data = report_mod.build_report(populated, max_quote_rows=50)
    assert "promos" in data
    assert data["promos"]["offers"][0]["kind"] == "signup_bonus"
    # DraftKings has no rows in the populated odds store, so the wiring must say
    # so per offer rather than dropping the plans key or inventing legs.
    plan = data["promos"]["plans"]["draftkings|welcome"]
    assert plan["strategy"] == "no_odds_coverage"
    assert plan["plans"] == []
    assert data["promos"]["plan_meta"]["odds_run_id"] == max(data["detail_runs"])


def test_promo_plans_ride_the_report_payload_end_to_end(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """An offer at a book the odds store covers gets concrete legs in the page data.

    Through the real wiring — promo store on disk, odds store on disk,
    ``build_report`` — not by calling the planner directly.  ``generated_at``
    is pinned before the slate's first pitch because plans refuse games that
    have already started, and "the fixture aged past its own games" must not
    read as "the wiring broke".
    """
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)

    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id,
        ok=True,
        offers=[
            PromoOffer(
                source="smarkets",
                offer_id="credit-drop",
                kind=PromoKind.BONUS_BET,
                title="£100 in free bets",
                observed_at=datetime.now(UTC),
                bonus_amount=100.0,
                reward_type="bonus_bets",
            )
        ],
        health=[
            PromoSourceHealth(
                source_key="smarkets", ok=True,
                checked_at=datetime.now(UTC), offer_count=1,
            )
        ],
    )
    store.close()

    data = report_mod.build_report(
        populated,
        max_quote_rows=50,
        generated_at=datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
    )
    plan = data["promos"]["plans"]["smarkets|credit-drop"]
    assert plan["strategy"] == "bonus_conversion", plan
    assert plan["plans"], plan["skipped"]
    best = plan["plans"][0]
    legs = best["legs"]
    assert legs[0]["role"] == "promo"
    assert legs[0]["source"] == "smarkets"
    assert legs[0]["stake_kind"] == "bonus"
    assert all(leg["source"] != "smarkets" for leg in legs[1:])
    assert best["guaranteed_cash"] == pytest.approx(
        min(profit for _, profit in best["outcome_profits"])
    )
    assert data["promos"]["plan_meta"]["odds_run_id"] == max(data["detail_runs"])
    # The page's own JSON round-trip.
    json.dumps(data["promos"]["plans"])


def test_promo_plans_carry_the_arb_panels_takeable_verdict(tmp_path, monkeypatch) -> None:
    """Best overall and best takeable, side by side, legs labelled like arb legs.

    On a PA run a DraftKings credit hedged at Smarkets is the best execution on
    the board and not one the operator can place. The first pass keeps it and
    labels the Smarkets leg; the second pass, every unreachable venue removed,
    hedges at FanDuel and is what ``takeable_plans`` carries.
    """
    from src.report import _promo_plans

    path, run_ids = _pa_state_run(tmp_path, [
        ("draftkings", Selection.AWAY, 3.0),
        ("smarkets", Selection.HOME, 1.6),
        ("fanduel", Selection.HOME, 1.5),
    ])
    offer = {
        "source": "draftkings", "offer_id": "o1", "kind": "bonus_bet",
        "title": "Bonus bet drop", "summary": "", "description": "",
        "reward_type": "bonus_bets", "bonus_amount": 100.0,
        "min_odds": None, "wagering_requirement": None, "state_confirmed": True,
    }
    with Store(path) as opened:
        plans, meta = _promo_plans(opened, [offer], run_ids, datetime.now(UTC), state="PA")
    assert meta and "odds_run_id" in meta
    entry = plans["draftkings|o1"]
    best = entry["plans"][0]
    assert best["takeable"] is False
    assert best["non_local_legs"] == [{"source": "smarkets", "origin_label": "offshore"}]
    hedge = best["legs"][1]
    assert hedge["origin"] == "offshore" and hedge["origin_local"] is False
    assert hedge["non_local_label"] == "not reachable from PA"
    assert best["legs"][0]["origin_local"] is True
    local = entry["takeable_plans"]
    assert local and local[0]["takeable"] is True
    assert [leg["source"] for leg in local[0]["legs"]] == ["draftkings", "fanduel"]
    assert "takeable_skipped" in entry


def test_the_page_renders_the_promo_takeable_section() -> None:
    from src.report_assets import JS

    assert "function promoTakeableHtml(entry, o)" in JS
    assert "Best takeable from here" in JS
    assert "best overall plan above is takeable from here" in JS
    # Promo legs reuse the arb leg's pill, so one vocabulary reaches both.
    body = JS[JS.index("function promoPlanCardHtml"):JS.index("function promoPlanHtml")]
    assert "originPill(leg)" in body
    assert "arbBlockers(plan)" in body


def test_a_planner_failure_does_not_take_the_page_down(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """The promos panel predates plans and must survive without them.

    ``_promo_plans`` catches everything the planner can raise, and until this
    test nothing held it there: narrowing the ``except`` to a type the planner
    never raises left the whole suite green.  The page then died on an offer
    the planner happened to choke on — the promo scrape and every odds panel
    lost with it.
    """
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)
    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id, ok=True,
        offers=[PromoOffer(
            source="smarkets", offer_id="boom", kind=PromoKind.BONUS_BET,
            title="Boom", observed_at=datetime.now(UTC),
            bonus_amount=100.0, reward_type="bonus_bets",
        )],
        health=[PromoSourceHealth(
            source_key="smarkets", ok=True, checked_at=datetime.now(UTC), offer_count=1,
        )],
    )
    store.close()

    def explode(*args, **kwargs):
        raise RuntimeError("planner exploded")

    import src.promos.planner as planner_mod
    monkeypatch.setattr(planner_mod, "build_promo_plans", explode)

    data = report_mod.build_report(
        populated, max_quote_rows=50,
        generated_at=datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
    )
    # The page still builds, the offer still shows, and the reason is named.
    assert data["promos"]["offers"], "the offer was lost with the plan"
    assert data["promos"]["plans"] == {}
    reason = data["promos"]["plan_meta"]["reason"]
    assert reason.startswith("planner_failed"), reason
    assert "planner exploded" in reason
    # And it renders.
    page = report_mod.render_page(data)
    assert "report-data" in page and page.startswith("<!")


def test_an_empty_odds_run_is_reported_as_a_failure_not_a_success(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """``empty_odds_run`` is the one no-plan state that carries a run id.

    The panel's note reads ``reason`` only when there is no ``odds_run_id``, so
    this state announced itself as "plans priced from odds run #N".  Pinned at
    the payload, where the note's inputs come from.
    """
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)
    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id, ok=True,
        offers=[PromoOffer(
            source="smarkets", offer_id="x", kind=PromoKind.BONUS_BET,
            title="X", observed_at=datetime.now(UTC),
            bonus_amount=100.0, reward_type="bonus_bets",
        )],
        health=[PromoSourceHealth(
            source_key="smarkets", ok=True, checked_at=datetime.now(UTC), offer_count=1,
        )],
    )
    store.close()

    monkeypatch.setattr(populated, "load_quotes", lambda *a, **k: [])
    data = report_mod.build_report(
        populated, max_quote_rows=50,
        generated_at=datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
    )
    meta = data["promos"]["plan_meta"]
    assert meta["reason"] == "empty_odds_run", meta
    assert data["promos"]["plans"] == {}



def _store_with_future_games(tmp_path) -> Store:
    """An odds run whose games are always ahead of the wall clock.

    The serve path prices with ``datetime.now(UTC)``, and a fixture pinned to a
    calendar date silently stops producing plans the day it passes — which is
    what happened to both serve tests on 2026-07-28.  They kept passing on an
    empty result, so the handler could have stopped pricing altogether.
    """
    from src.sources.base import SourceHealth

    store = Store(tmp_path / "future.sqlite3")
    observed = datetime.now(UTC)
    commence = observed + timedelta(hours=6)
    quotes = []
    for source, selection, odds in (
        ("smarkets", Selection.AWAY, 3.0),
        ("fanduel", Selection.HOME, 1.5),
    ):
        quotes.append(make_quote(
            source=source, selection=selection, decimal_odds=odds,
            observed_at=observed, commence_time=commence,
        ))
    run_id = store.start_run(observed)
    store.save_quotes(run_id, quotes)
    for source in ("smarkets", "fanduel"):
        store.save_health(run_id, SourceHealth(
            source_key=source, ok=True, checked_at=observed,
        ))
    store.finish_run(run_id, finished_at=observed, report=_Findings(quotes))
    return store


def test_the_serve_response_carries_plans_not_a_hardcoded_absence(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """``--serve``'s promo response prices against the stored odds run.

    It called the payload builder with no odds store at all, so it always
    answered ``plans: {}`` with ``reason: no_odds_run`` — a hardcoded claim
    that there was no odds run, in the one serve path that never reloads the
    page afterwards.  Nothing covered either serve handler.
    """
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)
    # The serve helper opens the odds store from settings, as the server does.
    odds = _store_with_future_games(tmp_path)
    monkeypatch.setattr(settings_mod, "DB_PATH", odds.path)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", odds.path)

    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id, ok=True,
        offers=[PromoOffer(
            source="smarkets", offer_id="credit", kind=PromoKind.BONUS_BET,
            title="Credit", observed_at=datetime.now(UTC),
            bonus_amount=100.0, reward_type="bonus_bets",
        )],
        health=[PromoSourceHealth(
            source_key="smarkets", ok=True, checked_at=datetime.now(UTC), offer_count=1,
        )],
    )
    store.close()

    payload = report_mod._promo_payload_for_serve()
    assert payload["offers"], "the serve response lost the offers"
    meta = payload["plan_meta"]
    assert meta is not None
    assert meta.get("reason") != "no_odds_run", (
        "the serve response claims there is no odds run while one is stored"
    )
    assert meta.get("odds_run_id") == odds.latest_run_id(), (
        "the serve response priced against a run that is not the newest"
    )
    # Concrete legs, not merely a map keyed by offer: ``plans`` is truthy
    # whenever an offer exists, so asserting on it alone passed for years of
    # wall-clock drift with nothing priced at all.
    entry = payload["plans"]["smarkets|credit"]
    assert entry["plans"], (payload["plan_meta"], entry)
    assert entry["plans"][0]["legs"][0]["source"] == "smarkets"


def test_the_serve_response_degrades_rather_than_failing_the_scrape(
    tmp_path, monkeypatch,
) -> None:
    """An unreadable odds database must not fail a promo scrape's response.

    With a stored offer, not an empty promo store: the reason is only attached
    when there is something to explain, so an empty store meant this test could
    never observe ``odds_store_unreadable`` — the string it exists to pin had
    never once been produced.
    """
    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)
    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id, ok=True,
        offers=[PromoOffer(
            source="smarkets", offer_id="credit", kind=PromoKind.BONUS_BET,
            title="Credit", observed_at=datetime.now(UTC),
            bonus_amount=100.0, reward_type="bonus_bets",
        )],
        health=[PromoSourceHealth(
            source_key="smarkets", ok=True, checked_at=datetime.now(UTC), offer_count=1,
        )],
    )
    store.close()

    broken = tmp_path / "not-a-database.sqlite3"
    broken.write_text("this is not sqlite")
    monkeypatch.setattr(settings_mod, "DB_PATH", broken)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", broken)

    payload = report_mod._promo_payload_for_serve()
    # The scrape's own result survives; only the plans are lost.
    assert payload["offers"], "an odds-side failure took the promo rows with it"
    assert payload["plans"] == {}
    reason = payload["plan_meta"]["reason"]
    assert reason.startswith("odds_store_unreadable"), reason
    assert reason != "no_odds_run", (
        "an unreadable odds database is reported as there being no odds run"
    )


def test_an_empty_promo_store_needs_no_explanation(tmp_path, monkeypatch) -> None:
    """No offers, nothing to explain — and no invented reason either."""
    from src import report as report_mod
    from src import settings as settings_mod

    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", tmp_path / "promos.sqlite3")
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", tmp_path / "promos.sqlite3")
    broken = tmp_path / "not-a-database.sqlite3"
    broken.write_text("this is not sqlite")
    monkeypatch.setattr(settings_mod, "DB_PATH", broken)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", broken)

    payload = report_mod._promo_payload_for_serve()
    assert payload["plans"] == {}
    assert payload["offers"] == []



def test_the_serve_promo_endpoint_answers_with_plans(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """Drive the real ``--serve`` handler over a real socket.

    Neither serve handler had any test at all, so the promo endpoint's payload
    was free to disagree with the page's.  The scrape itself is stubbed — no
    network — but the request, the handler, the JSON and the payload builder
    are the real ones.
    """
    import http.server
    import json as json_mod
    import threading
    import urllib.request

    from src import report as report_mod
    from src import settings as settings_mod
    from src.promos.base import PromoSourceHealth
    from src.promos.schema import PromoKind, PromoOffer
    from src.promos.store import PromoStore

    promo_db = tmp_path / "promos.sqlite3"
    monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(report_mod.settings, "PROMO_DB_PATH", promo_db)
    monkeypatch.setattr(settings_mod, "DB_PATH", populated.path)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", populated.path)

    store = PromoStore(promo_db)
    run_id = store.start_run()
    store.finish_run(
        run_id, ok=True,
        offers=[PromoOffer(
            source="smarkets", offer_id="credit", kind=PromoKind.BONUS_BET,
            title="Credit", observed_at=datetime.now(UTC),
            bonus_amount=100.0, reward_type="bonus_bets",
        )],
        health=[PromoSourceHealth(
            source_key="smarkets", ok=True, checked_at=datetime.now(UTC), offer_count=1,
        )],
    )
    store.close()

    # No network: the scrape is a stub, the rows above stand in for its result.
    monkeypatch.setattr(
        report_mod, "_run_promos_from_ui",
        lambda **kwargs: {"offer_count": 1, "sources": ["smarkets"]},
    )

    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")

    captured: dict = {}
    real_server = http.server.ThreadingHTTPServer

    class _Capturing(real_server):
        def __init__(self, address, handler):
            super().__init__(address, handler)
            captured["server"] = self

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _Capturing)

    thread = threading.Thread(
        target=report_mod._serve,
        args=(page, 0),
        kwargs=dict(open_browser=False, run_limit=5,
                    quote_runs=2, max_quote_rows=50),
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if "server" in captured:
            break
        import time
        time.sleep(0.01)
    assert "server" in captured, "the server never started"
    server = captured["server"]
    port = server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/promos/collect",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json_mod.loads(response.read())
    finally:
        server.shutdown()

    assert payload["ok"] is True, payload
    promos = payload["promos"]
    assert promos["offers"], "the endpoint answered without the offers"
    meta = promos["plan_meta"]
    assert meta is not None and meta.get("reason") != "no_odds_run", (
        "the endpoint claims no odds run while one is stored"
    )
    assert meta.get("odds_run_id") is not None
    assert promos["plans"], "the endpoint answered with no plans"


def test_the_serve_bet_endpoints_persist_and_return_the_ledger(
    populated: Store, tmp_path, monkeypatch,
) -> None:
    """The browser's log/edit round trip uses the real localhost control plane."""
    import http.server
    import json as json_mod
    import threading
    import time
    import urllib.request

    from src import report as report_mod
    from src import settings as settings_mod

    bet_db = tmp_path / "bets.sqlite3"
    monkeypatch.setattr(settings_mod, "BET_DB_PATH", bet_db)
    monkeypatch.setattr(report_mod.settings, "BET_DB_PATH", bet_db)
    monkeypatch.setattr(settings_mod, "DB_PATH", populated.path)
    monkeypatch.setattr(report_mod.settings, "DB_PATH", populated.path)

    page = tmp_path / "dashboard.html"
    page.write_text(render_page(build_report(populated)), encoding="utf-8")

    captured: dict = {}
    real_server = http.server.ThreadingHTTPServer

    class _Capturing(real_server):
        def __init__(self, address, handler):
            super().__init__(address, handler)
            captured["server"] = self

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _Capturing)
    thread = threading.Thread(
        target=report_mod._serve,
        args=(page, 0),
        kwargs=dict(open_browser=False, run_limit=5,
                    quote_runs=2, max_quote_rows=50),
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if "server" in captured:
            break
        time.sleep(0.01)
    assert "server" in captured, "the server never started"
    server = captured["server"]
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route: str, body: dict) -> dict:
        request = urllib.request.Request(
            base + route,
            data=json_mod.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json_mod.loads(response.read())

    try:
        logged = post("/api/bets/log", {
            "kind": "single",
            "sport": "baseball",
            "home_team": "Chicago Cubs",
            "selection": "Chicago Cubs",
            "legs": [{
                "book": "fanduel",
                "selection": "Chicago Cubs",
                "american_odds": 120,
                "stake": 25,
            }],
        })
        leg_id = logged["bets"]["slips"][0]["legs"][0]["id"]
        settled = post("/api/bets/leg", {
            "leg_id": leg_id,
            "status": "won",
        })
        edited = post("/api/bets/slip", {
            "slip_id": logged["result"]["slip_id"],
            "note": "Ticket checked against the book.",
        })
        with urllib.request.urlopen(base + "/api/bets", timeout=30) as response:
            fetched = json_mod.loads(response.read())
    finally:
        server.shutdown()

    assert logged["ok"] is True
    assert bet_db.exists()
    assert settled["bets"]["summary"]["profit"] == 30.0
    assert edited["bets"]["slips"][0]["note"] == "Ticket checked against the book."
    assert fetched["bets"] == edited["bets"]


def test_report_tests_never_read_the_developers_sidecar_databases(tmp_path) -> None:
    """The autouse isolation is in force, and is checked rather than assumed.

    ``build_report`` reads both paths itself, so without the fixture in
    ``tests/conftest.py`` every report test silently renders whatever was last
    scraped or logged on the developer's machine.
    """
    from src import report as report_mod
    from src import settings as settings_mod

    for setting, filename in (("PROMO_DB_PATH", "promos.sqlite3"),
                              ("BET_DB_PATH", "bets.sqlite3")):
        default = Path("data", filename).resolve()
        for module in (settings_mod, report_mod.settings):
            active = Path(getattr(module, setting)).resolve()
            assert active != default, (
                f"a report test is pointed at the real {filename}; the autouse "
                "_hermetic_promo_db fixture in tests/conftest.py is not in force"
            )
            assert not active.exists(), (
                f"the isolated {filename} should not exist unless a test built it"
            )



# ── the pieces this file deliberately stubs ──────────────────────────────────


def test_the_real_validation_report_still_fits_the_store(tmp_path) -> None:
    """These tests hand the store a stand-in report, so this pins the real one.

    Skipped while ``src.validation`` is mid-rewrite rather than silently passing:
    the point of the test is that the two agree.
    """
    # Imported, not ``importorskip``-ed: ``src.validation`` is first-party and
    # the whole point of this test is that the real report fits the store.  The
    # helper would turn a genuine ImportError — the exact breakage worth
    # catching — into a skip and an exit code of 0.
    import src.validation as validation
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
    from src.raw_store import RawStore

    # Resolved against this file, not against the working directory, exactly as
    # ``tests/conftest.py`` does it and for the same reason: run from anywhere
    # but the repository root the glob matched nothing, and the ``skip`` below
    # turned "I looked in the wrong place" into "this source has no captures" —
    # a green run that checked nothing.  A registered source with no fixture is
    # a real failure, so it is one.
    fixtures = Path(__file__).parent / "fixtures" / "raw"
    paths = sorted(fixtures.glob(f"{source}__*.json"))
    assert paths, (
        f"no captured responses for {source} in {fixtures} — every registered "
        "source needs a fixture, or this guard silently stops covering it"
    )
    store = RawStore(fixtures)
    adapter = SOURCE_FACTORIES[source]()
    try:
        skipped = adapter.parse([store.read(path) for path in paths]).skipped
    finally:
        adapter.close()

    # A source that skips nothing is *well behaved*, not broken, so this no
    # longer asserts ``skipped``.  That assertion failed ``caesars``,
    # ``draftkings`` and ``hardrock`` for the sole offence of parsing their
    # captures cleanly, which is the outcome the collector is built to reach.
    # The vacuity it was guarding against — every parse quietly ceasing to
    # count skips, leaving this test green over nothing — is real, but it is a
    # property of the corpus rather than of any one source, and it is pinned as
    # one by ``test_the_skip_reason_guard_is_not_vacuous`` below.
    unexplained = [
        reason for reason in skipped
        if not any(reason.startswith(prefix) for prefix, _ in SKIP_NOTES)
    ]
    assert unexplained == [], unexplained


def test_the_skip_reason_guard_is_not_vacuous() -> None:
    """The corpus above must actually exercise the explanation guard.

    ``test_every_real_skip_reason_has_an_explanation`` passes trivially for a
    source that skipped nothing, which is correct per source and worthless as
    the whole story: were every adapter to stop counting skips, all 25 would
    pass while covering nothing.  The floors here are set well under the
    captured slate's real figures — 172 distinct reasons across 22 of 25
    sources — so re-capturing may move them without going red, while a
    collapse in counting cannot hide.
    """
    from src.raw_store import RawStore

    fixtures = Path(__file__).parent / "fixtures" / "raw"
    store = RawStore(fixtures)
    reasons: set[str] = set()
    sources_that_skip = 0
    for name in sorted(SOURCE_FACTORIES):
        paths = sorted(fixtures.glob(f"{name}__*.json"))
        if not paths:
            continue  # no capture: the per-source test above already fails it
        adapter = SOURCE_FACTORIES[name]()
        try:
            skipped = adapter.parse([store.read(path) for path in paths]).skipped
        finally:
            adapter.close()
        if skipped:
            sources_that_skip += 1
            reasons.update(skipped)

    assert len(reasons) >= 120, (
        f"only {len(reasons)} distinct skip reasons across the captured slate "
        "(was 172) — adapters have stopped counting what they drop, and the "
        "per-source explanation guard now proves nothing"
    )
    assert sources_that_skip >= 20, (
        f"only {sources_that_skip} sources skipped anything (was 22 of 25) — "
        "too few to keep the explanation guard honest"
    )


def _store_with_two_kambi_skins(tmp_path) -> tuple[Store, list]:
    """An odds run holding a measurable mirror.

    Two Kambi skins quote an identical book across twelve games — 24 shared
    selections, past ``MIN_SHARED_SELECTIONS`` — so the mirror is *measured*
    rather than merely suspected, and a planner given these quotes must refuse
    to hedge one skin at the other.
    """
    from src.sources.base import SourceHealth

    store = Store(tmp_path / "mirrors.sqlite3")
    observed = datetime.now(UTC)
    commence = observed + timedelta(hours=6)
    quotes = []
    for i in range(12):
        shared = dict(
            event_key=f"MLB-A{i}@MLB-H{i}:2026-07-28",
            home_participant=f"MLB-H{i}", away_participant=f"MLB-A{i}",
            observed_at=observed, commence_time=commence,
        )
        for source in ("betrivers_kambi", "leovegas_kambi"):
            quotes.append(make_quote(source=source, selection=Selection.AWAY,
                                     decimal_odds=3.00, **shared))
            quotes.append(make_quote(source=source, selection=Selection.HOME,
                                     decimal_odds=1.50, **shared))
        quotes.append(make_quote(source="fanduel", selection=Selection.HOME,
                                 decimal_odds=1.40, **shared))
        quotes.append(make_quote(source="fanduel", selection=Selection.AWAY,
                                 decimal_odds=3.20, **shared))
    run_id = store.start_run(observed)
    store.save_quotes(run_id, quotes)
    for source in ("betrivers_kambi", "leovegas_kambi", "fanduel"):
        store.save_health(run_id, SourceHealth(
            source_key=source, ok=True, checked_at=observed,
        ))
    store.finish_run(run_id, finished_at=observed, report=_Findings(quotes))
    return store, [run_id]


def test_the_page_builds_promo_plans_behind_a_counterparty_gate(tmp_path, monkeypatch) -> None:
    """The gate at the *call site*, not the planner's default.

    ``build_promo_plans`` derives the groups itself when handed ``None``, so
    the planner is safe either way — but both production callers pass the
    measured groups explicitly, and nothing asserted they pass anything at
    all.  Replacing either with ``{}`` left the whole suite green while the
    dashboard offered a hedge at the same Kambi licence as the promo book.
    """
    import src.promos.planner as planner_module

    store, run_ids = _store_with_two_kambi_skins(tmp_path)
    seen: dict = {}
    real = planner_module.build_promo_plans

    def spy(offers, quotes, **kwargs):
        seen.update(kwargs)
        return real(offers, quotes, **kwargs)

    monkeypatch.setattr(planner_module, "build_promo_plans", spy)
    offers = [{
        "source": "betrivers_kambi", "offer_id": "offer-1", "kind": "bonus_bet",
        "title": "Bonus bet drop", "summary": "", "description": "",
        "reward_type": "bonus_bets", "bonus_amount": 100.0,
        "min_odds": None, "wagering_requirement": None,
    }]
    from src.report import _promo_plans

    with store:
        _promo_plans(store, offers, run_ids, datetime.now(UTC))

    gate = seen.get("one_counterparty")
    assert gate, f"the page built plans with no counterparty gate: {seen!r}"
    pairs = [frozenset(group) for groups in gate.values() for group in groups]
    assert frozenset({"betrivers_kambi", "leovegas_kambi"}) in pairs, gate


# ── books you cannot bet from the US ─────────────────────────────────────────
#
# Pinnacle and the offshore exchanges publish the sharpest lines on the page, so
# they stay in the data.  What must not happen is the page presenting a position
# as takeable when one of its legs is at a venue that will not accept a US
# customer: the reader stakes the licensed side and finds no counterparty for the
# other, which is an unhedged bet rather than a slightly optimistic margin.


def test_the_feed_filter_reads_the_registry_rather_than_the_key_prefix() -> None:
    """First-party versus republished is one classification, off the registry.

    The prefixes ``an_`` and ``vi_`` are a naming convention, and a page filter
    built on the spelling would mis-sort the first republisher that did not
    follow it — into the first-party half, which is the direction that matters,
    because a mirror is not a counterparty. The payload already carries the
    answer as ``diagnostic_only``, and the filter reads that.
    """
    from src.report import _source_entry
    from src.sources.registry import REPUBLISHED_SOURCE_KEYS, keys

    for key in keys():
        entry = _source_entry(key)
        assert entry["diagnostic_only"] == (key in REPUBLISHED_SOURCE_KEYS), key

    assert _source_entry("an_fanduel")["diagnostic_only"] is True
    assert _source_entry("vsin_circa")["diagnostic_only"] is True, (
        "a republisher whose key carries no an_/vi_ prefix must still sort as one"
    )
    assert _source_entry("fanduel")["diagnostic_only"] is False


def test_both_boards_can_be_narrowed_to_one_kind_of_feed() -> None:
    """The two pickers exist, are wired, and filter off the shared predicate.

    The page is handwritten, so a control can ship rendered-but-inert: the
    element sits in the markup, nothing listens to it, and the reader picks
    "first-party only" and watches a republished board not change.
    """
    from src.report_assets import BODY, JS

    for node_id in ("f-feed", "events-feed"):
        assert f'id="{node_id}"' in BODY, node_id
        assert f"'{node_id}'" in JS, f"{node_id} is rendered but nothing reads it"
        for value in ('value="first"', 'value="republished"'):
            assert value in BODY

    # One predicate behind both, rather than each panel spelling the test.
    assert "const feedKind = (key) =>" in JS
    assert JS.count("feedKind(str(r[COL.source]))") >= 2

    # Both are in the refresh wiring, beside the filters that already worked.
    assert "'f-feed', 'f-source'" in JS
    assert "el('events-feed').addEventListener" in JS


def test_the_game_search_is_labelled_on_both_boards() -> None:
    """An unlabelled search box beside three labelled selects reads as decoration.

    Both boxes worked; the operator did not find either, and asked for a search
    that already existed. The label is the fix.
    """
    from src.report_assets import BODY

    for node_id in ("q", "events-q"):
        assert f'<label class="eyebrow" for="{node_id}">Search</label>' in BODY, node_id
    assert BODY.count("Search a team to find one\n        game") + BODY.count(
        "Search a team to find one game"
    ) == 2, "both panels should name the search in their own copy"


def test_every_venue_says_whether_it_can_be_bet_from_the_us() -> None:
    """The flag is read off the registry, not restated in the report layer.

    A literal list here would drift the moment a source was added, and drift
    silently: an unflagged offshore book renders identically to a licensed one.
    """
    from src.report import _source_entry
    from src.sources.registry import US_UNAVAILABLE_SOURCE_KEYS, keys

    for key in keys():
        entry = _source_entry(key)
        assert "us_unavailable" in entry, f"{key} does not say whether it is bettable"
        assert entry["us_unavailable"] == (key in US_UNAVAILABLE_SOURCE_KEYS), key

    entry = _source_entry("pinnacle")
    assert entry["us_unavailable"] is True
    # Independent of view-only: Pinnacle is a real order book, not a mirror, and
    # collapsing the two axes would drop it out of best-price highlighting.
    assert entry["view_only"] is False, (
        "Pinnacle was marked view-only; that is a different question, and "
        "answering it this way removes the sharpest line on the board"
    )


def test_a_legacy_runs_badge_does_not_flip_with_the_readers_state() -> None:
    """The badge's fallback arm is ``view_only_for_run``, never the ambient set.

    For a legacy run (jurisdiction ``""``) the old ``is_view_only`` arm graded
    ``hardrock`` by the reader's ODDS_STATE — " · context only, not a book" on
    a PA-built page, a counterparty on an IL-built one — while the same page's
    arb payload, resolved from the run, kept showing the hardrock-legged
    position beside the badge that disowned it.  Found independently by two
    round-6 reviewers.
    """
    import pytest

    from src.report import _source_entry
    from src.sources import registry

    answers = set()
    for ambient_state in ("IL", "PA"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                registry,
                "VIEW_ONLY_SOURCES",
                registry.view_only_for_run(ambient_state),
            )
            answers.add(_source_entry("hardrock", state="")["view_only"])
    assert answers == {False}, (
        "a legacy run's hardrock badge flipped with the reader's configuration"
    )


def test_the_payload_carries_a_source_map_for_legacy_runs(populated: Store) -> None:
    """``""`` is a real jurisdiction key — legacy runs carry it.

    Without an entry the page's ``sourceInfo`` fell back to the top-level
    ``sources`` array, built for the *latest* run's state, so a legacy run's
    badges were graded by whatever run happened to be newest.
    """
    from src.jurisdictions import JURISDICTIONS

    data = build_report(populated)
    by_state = data["sources_by_jurisdiction"]
    for state in (*JURISDICTIONS, "GLOBAL", ""):
        assert state in by_state, f"no source map for jurisdiction {state!r}"
    legacy = {entry["key"]: entry for entry in by_state[""]}
    if "hardrock" in legacy:
        assert legacy["hardrock"]["view_only"] is False, (
            "the legacy column must grade by view_only_for_run(''), which never "
            "contains a retail key"
        )


def test_a_us_regulated_venue_is_not_marked_unbettable() -> None:
    """Kalshi is the one that must not be swept up by the set.

    ``pinnacle`` deliberately is swept up, and ``polymarket_us`` deliberately is
    not — the CFTC-designated venue is takeable while the offshore Polymarket
    that used to hold the other side of this pin was deregistered on 2026-08-13.
    Both directions are pinned here because the page uses this flag to decide
    what the US-only view shows, so an error either way is a wrong answer to
    "can I actually take this price".
    """
    from src.report import _source_entry

    for key in ("kalshi", "fanduel", "draftkings", "polymarket_us"):
        assert _source_entry(key)["us_unavailable"] is False, key
    assert _source_entry("pinnacle")["us_unavailable"] is True


def test_the_arb_payload_carries_both_the_us_only_and_the_offshore_view(
    populated: Store,
) -> None:
    """Detection is Python, so the toggle cannot recompute — both are precomputed.

    The default bundle is the US-only one.  ``with_offshore`` is the superset,
    and the fixture's ``smarkets`` legs are the thing that separates them.
    """
    from src.report import _arb_payload
    from src.sources.registry import US_UNAVAILABLE_SOURCE_KEYS

    with populated as store:
        run_ids = [row["id"] for row in store.run_summaries(limit=5)]
        payload = _arb_payload(store, run_ids, as_of=datetime(2026, 7, 28, 8, 0, tzinfo=UTC))

    assert payload, "no arb bundles were built for the fixture's runs"
    for run_id, bundle in payload.items():
        assert "with_offshore" in bundle, f"run {run_id} carries only one view"
        # The whole promise of the default view: no opportunity it lists can
        # have a leg the reader is unable to place.
        for opportunity in bundle["opportunities"]:
            legs = {leg["source"] for leg in opportunity["legs"]}
            offshore = US_UNAVAILABLE_SOURCE_KEYS.intersection(legs)
            assert not offshore, (
                f"the US-only view offered a position needing {sorted(offshore)}, "
                "which cannot be staked from the United States"
            )
        # Removing legs can only ever remove positions, never invent them.
        assert len(bundle["opportunities"]) <= len(bundle["with_offshore"]["opportunities"])


def test_the_us_only_view_still_refuses_the_republished_mirrors(populated: Store) -> None:
    """The regression the exclusion set is easy to write wrong.

    ``find_opportunities(view_only_sources=None)`` means "use the process
    default", so unioning the offshore keys onto ``None`` resolves to the
    offshore keys *alone* and quietly re-admits every Action Network and
    VegasInsider mirror as a leg — turning a book and its own republished copy
    into the two sides of a fictional arbitrage.
    """
    from src.report import _arb_payload
    from src.sources.registry import REPUBLISHED_SOURCE_KEYS

    with populated as store:
        run_ids = [row["id"] for row in store.run_summaries(limit=5)]
        payload = _arb_payload(store, run_ids, as_of=datetime(2026, 7, 28, 8, 0, tzinfo=UTC))

    for bundle in payload.values():
        for opportunity in bundle["opportunities"]:
            legs = {leg["source"] for leg in opportunity["legs"]}
            mirrors = REPUBLISHED_SOURCE_KEYS.intersection(legs)
            assert not mirrors, f"a republished mirror became a leg: {sorted(mirrors)}"


def _pa_state_run(tmp_path, sources) -> tuple[Path, list[int]]:
    """One PA run recorded under ``route_scope="state"``, with the given legs."""
    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=odds,
                   source_market_id="m", commence_time=kickoff)
        for source, selection, odds in sources
    ]
    path = tmp_path / "db.sqlite3"
    with Store(path) as opened:
        run = opened.start_run(
            datetime.now(UTC), jurisdiction="PA", route_scope="state",
        )
        opened.save_quotes_by_source(run, rows)
        opened.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=len(rows), event_count=1),
            counterparties={},
        )
    return path, [run]


def test_the_dashboard_labels_positions_with_no_state_licensed_leg(tmp_path) -> None:
    """The dashboard's half of the exact-state leg rule, which was unpinned.

    Setting ``state_scoped = False`` left ``tests/test_report.py`` byte-identical,
    so the surface a reader actually looks at could render every out-of-state-only
    position on a Pennsylvania board with nothing saying so. The position is shown
    — that is the policy — but flagged, and every foreign leg carries the label.
    ``--serve`` rebuilds through this same function.
    """
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("pinnacle", Selection.HOME, 2.20),
        ("bovada", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    # Measured on the offshore-admitted view: the US-only default drops Bovada as
    # a leg first, so the locality rule would have nothing left to act on and the
    # assertion would pass without exercising it.
    bundle = payload[str(run_ids[0])]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    assert bundle["non_local_flagged"] == 1
    entry = bundle["opportunities"][0]
    assert entry["no_local_leg"] is True
    assert all(
        leg["non_local_label"] == "not reachable from PA" for leg in entry["legs"]
    )


def test_near_misses_ride_the_arb_payload_with_locality(tmp_path) -> None:
    """The watchlist reaches the page: closest non-crossing markets, each leg
    carrying the same reachability verdict the positions carry."""
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("draftkings", Selection.HOME, 2.05),
        ("fanduel", Selection.AWAY, 1.90),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    bundle = payload[str(run_ids[0])]
    assert bundle["opportunities"] == []
    assert len(bundle["near_misses"]) == 1
    miss = bundle["near_misses"][0]
    assert miss["margin_pct"] < 0
    assert miss["takeable"] is True
    assert {leg["source"] for leg in miss["legs"]} == {"draftkings", "fanduel"}
    assert all(leg["local"] for leg in miss["legs"])
    # And a blank bundle carries the key too, so the page never branches.
    from src.report import _blank_arb_bundle

    assert _blank_arb_bundle(100.0)["near_misses"] == []


def test_the_page_renders_the_watchlist_and_the_zero_floor_pill() -> None:
    from src.report_assets import JS

    assert "bag.near_misses" in JS
    assert "Closest to crossing in this scrape" in JS
    assert "floor $0 · protects a push, wins nothing" in JS


def test_takeable_means_every_leg_placeable_from_the_state(tmp_path) -> None:
    """``takeable`` is stricter than ``no_local_leg`` and the page reads the strict one.

    A Pennsylvania board with one DraftKings leg and one Pinnacle leg is not
    wholly foreign, so ``no_local_leg`` is false — and the page used to count
    exactly that as takeable and add its profit to "guaranteed $". Neither leg
    can be placed alone. The position only forms in the offshore-admitted
    bundle (the US-only detector never admits Pinnacle as a leg), which is the
    bundle the page now also draws from, so it is shown, marked not takeable,
    and the blocking leg is named with where it is.
    """
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("draftkings", Selection.HOME, 2.20),
        ("pinnacle", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    assert payload[str(run_ids[0])]["opportunities"] == []
    bundle = payload[str(run_ids[0])]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    entry = bundle["opportunities"][0]
    assert entry["no_local_leg"] is False
    assert entry["takeable"] is False
    assert entry["non_local_legs"] == [{"source": "pinnacle", "origin_label": "offshore"}]

    path, run_ids = _pa_state_run(tmp_path / "local", [
        ("draftkings", Selection.HOME, 2.20),
        ("fanduel", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))
    entry = payload[str(run_ids[0])]["opportunities"][0]
    assert entry["takeable"] is True
    assert entry["non_local_legs"] == []


def test_the_page_counts_takeable_with_the_strict_key() -> None:
    """The headline numbers filter on ``arbTakeable``, never on ``no_local_leg`` alone."""
    from src.report_assets import JS

    assert "function arbTakeable(o)" in JS
    assert "opps.filter(arbTakeable)" in JS
    assert ".filter(arbTakeable).length" in JS, "the rail refresher counts the same way"
    assert "o.takeable !== undefined" in JS, "older payloads fall back to the weaker key"
    # Positions the US-only detector never formed — a leg at a book no US
    # customer can bet — are shown and marked, not dropped.
    assert "function arbPositions(bag)" in JS
    assert "not takeable · " in JS


def test_the_dashboard_links_a_state_partitioned_book_to_its_own_state(tmp_path) -> None:
    """The payload's leg links carry the run's jurisdiction into ``bet_link``.

    Run 32's two real opportunities each shipped ``il.betrivers.com`` on a
    ``betrivers_kambi`` leg whose prices came from ``rsiuspa``/``US-PA`` — the
    dashboard's copy of the same wrong door the SMS carried.  The SMS side is
    pinned in ``tests/test_alerts.py``; this is the page a reader clicks.
    """
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("fanduel", Selection.HOME, 2.20),
        ("betrivers_kambi", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    bundle = payload[str(run_ids[0])]
    assert bundle["opportunities"], "the two-book PA position must survive"
    links = {
        leg["source"]: (leg["link"] or {}).get("url", "")
        for opp in bundle["opportunities"]
        for leg in opp["legs"]
    }
    assert links["betrivers_kambi"] == "https://pa.betrivers.com/?page=sportsbook"
    assert "il.betrivers.com" not in " ".join(links.values())


def test_the_dashboard_keeps_positions_that_do_have_one(tmp_path) -> None:
    """The other direction: a legitimate PA board is neither emptied nor flagged."""
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("fanduel", Selection.HOME, 2.20),
        ("pinnacle", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    bundle = payload[str(run_ids[0])]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    assert bundle["non_local_flagged"] == 0
    entry = bundle["opportunities"][0]
    assert entry["no_local_leg"] is False
    labels = {leg["source"]: leg["non_local_label"] for leg in entry["legs"]}
    assert labels["fanduel"] == ""
    assert labels["pinnacle"] == "not reachable from PA"


def test_every_leg_states_where_its_venue_is(tmp_path) -> None:
    """Each leg carries an origin, not only the foreign ones a warning covers.

    ``non_local_label`` is one bit, and one bit made a book licensed one state
    over, a federally regulated venue and an offshore exchange read identically
    on the page. A local leg carried no mark at all, so "reachable" was an
    inference from an absence.
    """
    from src.report import _arb_payload

    path, run_ids = _pa_state_run(tmp_path, [
        ("fanduel", Selection.HOME, 2.20),
        ("pinnacle", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    legs = {
        leg["source"]: leg
        for leg in payload[str(run_ids[0])]["with_offshore"]["opportunities"][0]["legs"]
    }
    assert legs["fanduel"]["origin"] == "in_state"
    assert legs["fanduel"]["origin_label"] == "PA"
    assert legs["fanduel"]["origin_local"] is True
    assert legs["pinnacle"]["origin"] == "offshore"
    assert legs["pinnacle"]["origin_label"] == "offshore"
    assert legs["pinnacle"]["origin_local"] is False
    # The shorter phrase the CLI and the SMS print is unchanged beside it, so the
    # page cannot say something those two contradict.
    assert legs["pinnacle"]["non_local_label"] == "not reachable from PA"
    assert legs["fanduel"]["non_local_label"] == ""


def test_the_us_only_bundle_answers_from_the_same_marking(tmp_path) -> None:
    """Both views of one run are built over one marking, and each stays honest.

    A labelled leg cannot actually appear in the US-only view of a PA run: the
    global and offshore venues are excluded as legs before locality is asked,
    and every US-bettable book PA does not license (``hardrock``) is in the
    run's view-only set — asserted here so the claim stops being true loudly
    rather than silently.  What the US-only view must still get right is the
    *absence*: the position the offshore view shows flagged simply is not in
    this view, so its count says 0 rather than inheriting the sibling's 1.
    """
    from src.report import _arb_payload
    from src.sources import registry

    assert "hardrock" in registry.view_only_for_run("PA"), (
        "a US-bettable book PA does not license can now be a leg; give the "
        "US-only view a real labelled case instead of pinning the absence"
    )

    path, run_ids = _pa_state_run(tmp_path, [
        ("pinnacle", Selection.HOME, 2.20),
        ("bovada", Selection.AWAY, 2.20),
    ])
    with Store(path) as opened:
        payload = _arb_payload(opened, run_ids, as_of=datetime.now(UTC))

    entry = payload[str(run_ids[0])]
    # US-only view: the position's legs are excluded, so nothing is shown and
    # nothing is flagged — 0/0, not a stale copy of the offshore view's 1/1.
    assert entry["opportunities"] == []
    assert entry["non_local_flagged"] == 0
    assert len(entry["with_offshore"]["opportunities"]) == 1
    assert entry["with_offshore"]["non_local_flagged"] == 1


def test_a_state_licensed_republisher_is_not_badged_global() -> None:
    """The Books panel's one job is saying where each feed's number comes from.

    ``route_scope`` was ``"state" if key in RETAIL_SOURCE_KEYS else "global"``,
    written before the state-licensed republishers were split out of
    ``global_sources()``. ``an_fanduel`` is asked for Pennsylvania's own book id
    (255) on a PA run, so badging it GLOBAL beside ``vi_fanduel`` — a Las Vegas
    column that genuinely is one — erases the distinction rules (a) and (c) exist
    to make visible.
    """
    from src.report import _source_entry

    for key in ("fanduel", "an_fanduel", "an_parx", "an_thescore"):
        assert _source_entry(key)["route_scope"] == "state", key
    # Feeds that publish one board for the whole country stay global — including
    # ``an_bovada``, an Action Network feed pinned to a fixed id rather than a
    # per-state one, which is the case a membership test on the ``an_`` prefix
    # would have got wrong.
    for key in ("vi_fanduel", "vsin_circa", "an_bovada", "pinnacle", "smarkets"):
        assert _source_entry(key)["route_scope"] == "global", key


def test_a_legacy_scope_run_is_treated_the_same_way_arb_treats_it(tmp_path) -> None:
    """One run must not have two position counts — and ``legacy`` is governed.

    Sharing the predicate was not enough — the three callers spelled the *decision*
    three ways, so a run recorded ``jurisdiction="PA", route_scope="legacy"`` was
    filtered by ``arb`` (which keyed on the jurisdiction) and not by the dashboard
    (which keyed on ``route_scope``): 1 position here, 0 there, nothing accounting
    for the gap.

    **This test previously pinned the opposite answer, and that was a money-path
    defect.** Agreeing was right; agreeing on *admitting* was not. ``jurisdiction``
    and ``route_scope`` were added in different commits, so a database migrated
    across the gap holds rows with a real jurisdiction and ``'legacy'`` — the rows
    ``arb --run <old>`` and this payload read — and on those, ``arb`` printed **and
    texted** a "PA" arbitrage whose two legs were an offshore book and a book that
    geoblocks the US. Only a scope the operator widened on purpose (``global``,
    ``all``) is exempt, because that is a request to see the wider board; a
    forgotten scope is not.

    The legs here are ``pinnacle``/``bovada`` — nothing reachable from
    Pennsylvania — so the position is flagged and *counted*, its labels shown,
    never silently unmarked.
    """
    from src.report import _arb_payload
    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=2.20,
                   source_market_id="m", commence_time=kickoff)
        for source, selection in (
            ("pinnacle", Selection.HOME), ("bovada", Selection.AWAY),
        )
    ]
    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(
            datetime.now(UTC), jurisdiction="PA", route_scope="legacy",
        )
        store.save_quotes_by_source(run, rows)
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=len(rows), event_count=1),
            counterparties={},
        )
    with Store(path) as store:
        payload = _arb_payload(store, [run], as_of=datetime.now(UTC))

    bundle = payload[str(run)]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    assert bundle["non_local_flagged"] == 1
    assert bundle["opportunities"][0]["no_local_leg"] is True


def test_a_global_run_is_not_marked_by_jurisdiction(tmp_path) -> None:
    """"GLOBAL" is not a jurisdiction, so nothing about it is out of state."""
    from src.report import _arb_payload
    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=2.20,
                   source_market_id="m", commence_time=kickoff)
        for source, selection in (
            ("pinnacle", Selection.HOME), ("bovada", Selection.AWAY),
        )
    ]
    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(
            datetime.now(UTC), jurisdiction="GLOBAL", route_scope="global",
        )
        store.save_quotes_by_source(run, rows)
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=len(rows), event_count=1),
            counterparties={},
        )
    with Store(path) as store:
        payload = _arb_payload(store, [run], as_of=datetime.now(UTC))

    bundle = payload[str(run)]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    assert bundle["non_local_flagged"] == 0
    entry = bundle["opportunities"][0]
    assert entry["no_local_leg"] is False
    assert all(leg["non_local_label"] == "" for leg in entry["legs"])


def test_every_key_the_arb_payload_ships_is_read_by_the_page() -> None:
    """A payload key with no renderer is a fix that stopped halfway.

    ``non_local_withheld`` (this key's predecessor) shipped for a round with
    nothing reading it, so the dashboard showed an empty Pennsylvania board while
    ``collector arb`` printed "withheld 4 position(s)" for the same run — the
    exact two-counts-no-explanation failure the key was added to prevent.
    Asserted over every key rather than that one, so the next key added to this
    payload cannot repeat it.
    """
    from src.report_assets import JS
    from src.report import _blank_arb_bundle

    shipped = set(_blank_arb_bundle(100.0)) - {"with_offshore"}
    assert "non_local_flagged" in shipped, "the payload key vanished; update this test"
    unread = sorted(key for key in shipped if key not in JS)
    assert not unread, (
        f"the arb payload ships {unread} and no dashboard code reads it — either "
        "render it or stop shipping it"
    )


def test_the_page_reads_the_per_position_locality_keys() -> None:
    """The per-opportunity keys are below the every-key test's reach.

    ``_blank_arb_bundle`` has no opportunities, so ``no_local_leg`` and
    ``non_local_label`` are not in the set that test sweeps — a renderer could
    drop the per-leg labels and it would stay green. Pinned by name instead.
    """
    from src.report_assets import JS

    assert "no_local_leg" in JS
    assert "non_local_label" in JS


def test_the_page_tells_the_reader_when_positions_are_flagged() -> None:
    """Reading the number is not the same as accounting for it.

    The distinction has to reach words: "an edge" and "somewhere else's prices"
    are different boards, and the labels below the note only explain themselves
    to a reader who already knows the phrase.
    """
    from src.report_assets import JS

    branch = JS[JS.index("non_local_flagged"):]
    branch = branch[:branch.index("if (!opps.length)")]
    assert "flagged" in branch
    # And it has to be the *right* words. The marking is
    # ``registry.takeable_from_state``, which leaves Kalshi — a venue no state
    # licenses as a sportsbook — unlabelled, so a note saying "this jurisdiction
    # does not license" told the reader their prediction-market edge had been
    # flagged for licensing. Reachability is the claim the code makes, and it cuts
    # both ways: the offshore Polymarket *is* labelled, being a different entity
    # from the CFTC-designated Polymarket US.
    note = branch[branch.index("flaggedNote"):]
    assert "reach from this jurisdiction" in note
    assert "does not license" not in note


def test_a_run_predating_the_scope_column_is_not_migrated_into_state_scope(tmp_path) -> None:
    """The migration default decided the locality question for every old run.

    ``jurisdiction`` and ``route_scope`` were added to the schema in *different*
    commits, so a database written between them has a populated jurisdiction and no
    scope. SQLite backfills every existing row with the ALTER's default, so
    declaring ``'state'`` asserted that historical runs had collected exact-state
    retail routes, which is a claim about a run nobody recorded. A row that predates
    the column has no scope, which is what ``legacy`` means.

    The stamp is honest; the *consequence* is that rule (a) still governs the row.
    This test asserted the opposite for a round — 1 opportunity, 0 flagged — which
    is how ``arb --run <old>`` came to print and text an unmarked "PA" arbitrage
    between an offshore book and one that geoblocks the US. What the run recorded
    about its slate does not change whether a Pennsylvania operator can reach
    Bovada.
    """
    import sqlite3

    from src.report import _arb_payload
    from src.validation import ValidationReport

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=2.20,
                   source_market_id="m", commence_time=kickoff)
        for source, selection in (
            ("pinnacle", Selection.HOME), ("bovada", Selection.AWAY),
        )
    ]
    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(
            datetime.now(UTC), jurisdiction="PA", route_scope="legacy",
        )
        store.save_quotes_by_source(run, rows)
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=len(rows), event_count=1),
            counterparties={},
        )

    # Simulate the older schema: the column simply did not exist yet.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE collection_run DROP COLUMN route_scope")
    with Store(path) as store:                      # reopening runs the migration
        migrated = store.run_row(run)["route_scope"]
        payload = _arb_payload(store, [run], as_of=datetime.now(UTC))

    assert migrated == "legacy", (
        f"the migration backfilled {migrated!r}; a run that predates the column "
        "did not collect exact-state routes and must not be claimed to have"
    )
    bundle = payload[str(run)]["with_offshore"]
    assert len(bundle["opportunities"]) == 1
    assert bundle["non_local_flagged"] == 1
    assert bundle["opportunities"][0]["no_local_leg"] is True


def test_the_comparable_sport_count_does_not_depend_on_the_reading_process(tmp_path) -> None:
    """Same run, same database — one answer, whatever ``ODDS_STATE`` says.

    ``cross_book_event_counts`` read the module-level ``VIEW_ONLY_SOURCES``, which is
    frozen at import from ``settings.STATE``: 27 keys under IL and 28 under PA,
    because Pennsylvania cannot stake Hard Rock. So a PA run read from an
    IL-configured box counted Hard Rock as a counterparty and printed a sport
    ``usable``, while an IL run read from a PA box excluded it and printed
    ``NO OVERLAP``. ``runs`` and ``health`` both render that bar.
    """
    from src.sources import registry
    from src.store import Store
    from src.validation import ValidationReport

    assert "hardrock" in registry.view_only_for_run("PA"), "fixture assumption changed"
    assert "hardrock" not in registry.view_only_for_run("IL")

    kickoff = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        make_quote(source=source, selection=selection, decimal_odds=2.05,
                   source_market_id="m", commence_time=kickoff)
        for source, selection in (
            ("fanduel", Selection.HOME), ("hardrock", Selection.AWAY),
        )
    ]
    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(
            datetime.now(UTC), jurisdiction="PA", route_scope="state",
        )
        store.save_quotes_by_source(run, rows)
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=len(rows), event_count=1),
            counterparties={},
        )
        # The run is a PA run, so Hard Rock is not a counterparty in it — whatever
        # this process is configured for. One fixture, one pair, so the count is
        # zero when Hard Rock is correctly excluded and one when it is not.
        counted = store.cross_book_event_counts(
            run, min_books=2, view_only=registry.view_only_for_run("PA"),
        )
        assert counted.get("baseball", 0) == 0, counted

        # And the ambient constant is what used to be used, so this states the gap
        # rather than leaving it implied.
        ambient = store.cross_book_event_counts(
            run, min_books=2, view_only=registry.VIEW_ONLY_SOURCES,
        )
        if "hardrock" not in registry.VIEW_ONLY_SOURCES:
            assert ambient.get("baseball", 0) == 1, (
                "this box is configured for a state that can stake Hard Rock, so "
                "the ambient set would have counted it — which is the defect"
            )


def test_a_run_with_no_recorded_jurisdiction_gets_one_answer_too(monkeypatch) -> None:
    """The rows the resolver exists for were the ones it still answered ambiently.

    ``view_only_for_run`` fixed four hand-written copies of "which sources are not a
    counterparty" and then fell back to the ambient ``VIEW_ONLY_SOURCES`` for any
    state it did not recognise. ``jurisdiction`` migrates with ``DEFAULT ''``, so
    *every row predating that column* reads as empty — precisely the stored runs
    ``runs``, ``health`` and ``report`` are asked about — and on those the answer
    stayed a function of the reader's ``ODDS_STATE``, ``hardrock`` being the key that
    differs between the IL and PA sets.

    Deterministic beats exact about a state nobody recorded: the mirrors are what is
    knowable without a jurisdiction.
    """
    from src.sources import registry

    assert registry.view_only_for_state("PA") != registry.view_only_for_state("IL")
    for ambient in ("PA", "IL"):
        monkeypatch.setattr(
            registry, "VIEW_ONLY_SOURCES", registry.view_only_for_state(ambient)
        )
        for state in ("", "GLOBAL", "XX", None):
            assert registry.view_only_for_run(state) == registry.REPUBLISHED_SOURCE_KEYS



def test_the_runs_table_does_not_relabel_a_scopeless_run_as_state_scoped(tmp_path) -> None:
    """``or "state"`` claimed exact-state collection for a run that recorded none.

    The badge exists to distinguish exactly that, and the page already renders a
    missing value as ``legacy`` — the word the store backfills onto rows predating
    the column — so the coercion could only replace the honest answer with a
    confident one.
    """
    from src.store import Store
    from src.validation import ValidationReport

    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(datetime.now(UTC), jurisdiction="PA", route_scope="")
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=0, event_count=0),
            counterparties={},
        )
    with Store(path) as store:
        data = build_report(store)

    entry = next(row for row in data["runs"] if row["id"] == run)
    assert entry["route_scope"] == ""


def test_a_run_whose_state_disagrees_with_the_detected_egress_says_so(
    tmp_path, monkeypatch
) -> None:
    """The label that replaced the detection gate, asserted rather than assumed.

    State selection used to refuse any run whose detected egress named a state
    collection does not support, which also meant an operator could not choose a
    state by hand.  That gate is gone (see ``state_selection``'s module docstring),
    and what stands in its place is this warning: a run may collect a state the
    connection does not match, but the reader has to be told.  The mechanism
    predates the change and had no test, so the guard the new design leans on
    could have been removed by an unrelated edit without anything failing.
    """
    from src import egress, report, settings
    from src.store import Store
    from src.validation import ValidationReport

    record = tmp_path / "egress.json"
    egress.save_detection(
        record,
        egress.detection_from_payload(
            {"ip": "203.0.113.31", "region_code": "CA"}, detected_at=datetime.now(UTC)
        ),
    )
    monkeypatch.setattr(settings, "EGRESS_STATE_PATH", record)
    monkeypatch.setattr(report.settings, "EGRESS_STATE_PATH", record)

    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        run = store.start_run(datetime.now(UTC), jurisdiction="IL", route_scope="state")
        store.finish_run(
            run, finished_at=datetime.now(UTC),
            report=ValidationReport(quote_count=0, event_count=0),
            counterparties={},
        )
    with Store(path) as store:
        warnings = build_report(store)["meta"]["jurisdiction_warnings"]

    assert any("IL" in note and "CA" in note for note in warnings), warnings

    # An agreeing reading is silent — the warning marks a disagreement, and a
    # warning on every run would be no warning at all.
    egress.save_detection(
        record,
        egress.detection_from_payload(
            {"ip": "203.0.113.31", "region_code": "IL"}, detected_at=datetime.now(UTC)
        ),
    )
    with Store(path) as store:
        agreeing = build_report(store)["meta"]["jurisdiction_warnings"]
    assert not any("CA" in note for note in agreeing), agreeing


# ── sort and filter by sportsbook ────────────────────────────────────────────
#
# The rail's Sportsbook picker folds feeds to the counterparty a bet would be
# placed at, and that fold has exactly one authority: ``betlinks.book_for``.
# These tests pin the payload's brand fields to it, pin the display names to
# one spelling, and pin the page's wiring — one predicate, one row pipeline
# split, and the panels that must NOT narrow.


def test_every_venue_the_page_names_has_a_brand() -> None:
    """``_source_entry``'s brand is ``book_for``'s answer for every registered key.

    The fold is never derived from the ``an_``/``vi_`` spelling of the key —
    ``vsin_circa`` is the counterexample with no prefix to derive from — and
    one shared Kambi adapter does not make BetRivers and LeoVegas one company.
    """
    from src.betlinks import book_for
    from src.report import _source_entry
    from src.sources.registry import keys

    for key in keys():
        entry = _source_entry(key)
        assert entry["brand"] == (book_for(key) or ""), key
        assert "brand_label" in entry, key

    assert _source_entry("draftkings")["brand"] == "draftkings"
    assert _source_entry("an_draftkings")["brand"] == "draftkings"
    assert _source_entry("vi_draftkings")["brand"] == "draftkings"
    assert _source_entry("an_draftkings")["brand_label"] == "DraftKings"
    assert _source_entry("vsin_circa")["brand"] == "circa"
    assert _source_entry("leovegas_kambi")["brand"] != _source_entry("betrivers_kambi")["brand"]
    # A consensus line is not a book, so it folds to nothing and the picker
    # never offers it as a peer of DraftKings.
    assert _source_entry("an_open")["brand"] == ""


def test_a_brand_is_the_same_in_every_jurisdiction() -> None:
    """The brand is a fact about the key, never about the run's state.

    ``_source_entry`` is emitted once per jurisdiction column; a state-varying
    brand would let those seven emissions disagree about which company a feed
    reads, on the one control that spans every panel.
    """
    from src.jurisdictions import JURISDICTIONS
    from src.report import _source_entry
    from src.sources.registry import keys

    for key in keys():
        seen = {
            (_source_entry(key, state=state)["brand"], _source_entry(key, state=state)["brand_label"])
            for state in (*JURISDICTIONS, "GLOBAL", None)
        }
        assert len(seen) == 1, (key, seen)


def test_every_brand_has_one_name() -> None:
    """``BRAND_LABELS`` covers every brand and never re-spells a source label.

    Hard Rock is already spelled three ways across the mirrors' labels; the
    brand table exists so the fold cannot add a fourth.
    """
    from src.betlinks import SITE, book_for
    from src.report_copy import BRAND_LABELS, SOURCE_NOTES
    from src.sources.registry import keys

    reachable = {book_for(key) for key in keys()} - {None}
    assert reachable <= set(BRAND_LABELS), reachable - set(BRAND_LABELS)
    # One entry per venue that takes a bet, no strays.
    assert set(BRAND_LABELS) == set(SITE)
    for brand, label in BRAND_LABELS.items():
        if brand in SOURCE_NOTES:
            assert label == SOURCE_NOTES[brand]["label"], (
                f"{brand}: BRAND_LABELS says {label!r}, SOURCE_NOTES says "
                f"{SOURCE_NOTES[brand]['label']!r} — one book, two spellings"
            )


def test_a_ghost_key_still_gets_a_brand() -> None:
    """Rows outlive registrations; a deregistered key is its own bucket.

    ``book_for`` passes an unknown key through, so the page shows it under its
    own name rather than dropping its rows from a brand-narrowed view or
    guessing which company it used to read.
    """
    from src.report import _source_entry

    entry = _source_entry("unibet_au")
    assert entry["brand"] == "unibet_au"
    assert entry["brand_label"] == "unibet_au"


def test_the_brand_fold_agrees_with_the_redundancy_pairs() -> None:
    """Two vocabularies, one claim: same brand ⇔ redundant feed pair.

    ``distinctness``'s redundancy table and ``betlinks``' fold were written
    independently; a pair they disagree on is a bug in one of them, found here
    rather than by a reader noticing the picker groups what the sources page
    calls distinct.
    """
    from src.betlinks import book_for
    from src.redundancy import REDUNDANT_PAIRS, is_redundant_pair
    from src.sources.registry import keys

    for a, b in REDUNDANT_PAIRS:
        if book_for(a) is None or book_for(b) is None:
            continue
        assert book_for(a) == book_for(b), (a, b)

    registered = [k for k in keys() if book_for(k)]
    for a in registered:
        for b in registered:
            if a < b and book_for(a) == book_for(b):
                assert is_redundant_pair(a, b), (
                    f"{a} and {b} fold to one brand but are not a redundant pair"
                )


def test_promo_source_names_come_from_the_two_label_authorities() -> None:
    """The page used to keep a third table of promo brand names; it had drifted
    ("Hard Rock" for "Hard Rock Bet") and never learned ``thescore``.  Now the
    payload names every promo key from ``BRAND_LABELS`` (TheLines tenants fold
    to their brand) or ``PROMO_ONLY_LABELS`` for the catalogues with no odds
    feed, and nothing is left to drift."""
    from src.promos import registry as promo_registry
    from src.report import _promo_brand_labels
    from src.report_copy import BRAND_LABELS, PROMO_ONLY_LABELS

    labels = _promo_brand_labels()
    assert set(labels) == set(promo_registry.base_keys()), (
        set(promo_registry.base_keys()) - set(labels)
    )
    assert labels["thescore"] == BRAND_LABELS["thescore"] == "theScore Bet"
    assert labels["tl_fanduel"] == BRAND_LABELS["fanduel"]
    assert labels["hardrock"] == BRAND_LABELS["hardrock"] == "Hard Rock Bet"
    assert labels["betmgm_on"] == PROMO_ONLY_LABELS["betmgm_on"]
    assert not set(PROMO_ONLY_LABELS) & set(BRAND_LABELS)


def test_the_promo_and_odds_brand_keys_meet() -> None:
    """The promo key space folds to the same brand keys as the odds side.

    The Campaign filter joins the two spaces (``promoBrandKey(o.source)``
    against the picked odds-side brand), so a promo brand key that is not the
    odds side's key for the same book silently empties the filter.  BetRivers
    and LeoVegas are the keys an invented brand vocabulary got wrong twice in
    design review — ``betrivers``/``leovegas`` instead of the ``_kambi`` keys.
    """
    from src.betlinks import book_for
    from src.promos.registry import PROMO_SOURCES
    from src.sources.registry import BY_BASE_KEY

    for descriptor in PROMO_SOURCES:
        key = descriptor.key
        brand = key[3:] if key.startswith("tl_") else key  # promoBrandKey's fold
        odds_key = brand[:-3] if brand.endswith("_on") else brand
        if odds_key in BY_BASE_KEY and book_for(odds_key):
            assert brand == book_for(odds_key) or brand.endswith("_on"), (
                f"promo key {key} folds to {brand}, but the odds side calls "
                f"this book {book_for(odds_key)} — the campaign filter would "
                f"match nothing"
            )


def test_the_book_picker_is_wired_to_every_section() -> None:
    """The rail control exists, one predicate spells the comparison, one pipeline.

    A control can ship rendered-but-inert; a predicate can be re-spelled per
    panel until two tabs disagree about what a brand means.  Both are string
    facts about the page source, pinned here the way the feed filter is.
    """
    from src.report_assets import BODY, JS

    assert 'id="book-pick"' in BODY
    assert 'id="book-meta"' in BODY
    assert "el('book-pick')" in JS, "book-pick is rendered but nothing reads it"
    assert "bookPick.addEventListener" in JS

    # One predicate per key space — odds source keys, promo keys, ledger
    # prose — each spelled exactly once, and nothing else compares against the
    # picked brand directly.
    assert "const keepBrand = (key) =>" in JS
    assert "function keepPromoBrand(key)" in JS
    assert "const keepLedgerBook = (name) =>" in JS
    assert JS.count("keepBrand(") >= 6
    assert JS.count("=== currentBrand") == 3
    # One pipeline: the split exists and nothing re-derives it.
    assert JS.count("const currentRows = ()") == 1
    assert JS.count("const sportRows = ()") == 1
    assert JS.count("const brandGames = ") == 1

    # Global control, not a per-panel one: it must NOT be in the per-panel
    # refresh wiring list the literal below anchors (see the f-feed test).
    assert "'f-feed', 'f-source'" in JS
    assert "'book-pick'" not in JS.split("const refreshOdds")[1].split("afterTyping")[0]


def test_the_after_tax_controls_are_wired_and_the_rate_rule_is_written_once() -> None:
    """The rate arithmetic exists exactly once, and nothing else applies a rate.

    The whole design rests on a split: Python computes the taxable *basis* of
    every outcome and applies no rate at all, so the picker can move without a
    new run, and so the rule that turns a basis into a bill has one home.  A
    second copy of ``winnings - deductible`` anywhere would be the drift this is
    here to prevent — the same failure ``arb.crossed_against_itself`` was folded
    together to fix.
    """
    from src.report_assets import BODY, JS

    assert 'id="tax-fed"' in BODY
    assert 'id="tax-state"' in BODY
    assert 'id="tax-cap"' in BODY
    assert 'id="tax-meta"' in BODY
    for control in ("tax-fed", "tax-state", "tax-cap"):
        assert f"el('{control}')" in JS, f"{control} is rendered but nothing reads it"
    assert "taxFedPick.addEventListener" in JS
    assert "taxStatePick.addEventListener" in JS
    assert "taxCapToggle.addEventListener" in JS

    # One implementation of the rule, and one of each helper built on it.
    assert JS.count("function taxBill(") == 1
    assert JS.count("function afterTax(") == 1
    assert JS.count("function afterTaxFloor(") == 1
    assert JS.count("DEDUCTIBLE_SHARE") == 3, (
        "the deduction cap is spelled somewhere other than the constant, "
        "the rule that reads it, and the rail line that describes it"
    )
    # The rate is multiplied in one place only.  Every surface reaches the
    # arithmetic through the helpers above.
    assert JS.count("* taxFed") == 1
    assert JS.count("* taxState") == 1

    # Every after-tax figure is gated on a rate being set, so the default page
    # is what it was before this existed.
    assert JS.count("function taxOn(") == 1
    assert JS.count("taxOn()") >= 5

    # Display only: the picker must not reach the detector's own verdicts.
    bill = JS.split("function taxBill(")[1].split("\n}")[0]
    for forbidden in ("takeable", "margin_pct", "roi_pct", "guaranteed_profit"):
        assert forbidden not in bill, f"the tax rule reads {forbidden}"


def test_the_after_tax_rates_come_from_python_not_the_page() -> None:
    """One home for the rates, like brand labels.

    A rate list written into the page's script could not be tested, reviewed or
    changed beside the module that documents what the layers mean.
    """
    from src.report_assets import BODY, JS
    from src.tax import FEDERAL_PRESETS, STATE_PRESETS

    assert "DATA.tax" in JS
    assert "TAX.federal" in JS and "TAX.state" in JS
    # The labels are the distinctive half — a bare rate like 0.1 is a substring
    # of unrelated CSS, so matching on those would fail on the wrong thing.
    for label, rate in (*FEDERAL_PRESETS, *STATE_PRESETS):
        if rate:
            assert label not in JS and label not in BODY, (
                f"the rate label {label!r} is written into the page instead of served"
            )
    # And the pickers are built from that list rather than from fixed markup.
    assert "<option" not in BODY.split('id="tax-fed"')[1].split("</div>")[0]


def test_the_compare_the_books_surfaces_keep_every_book() -> None:
    """A surface whose subject is "compare the books" narrows games, not rows.

    The odds board, the coverage grid, the games lists and the fixture panel
    read ``sportRows()`` and narrow through ``brandGames``; ``renderBook`` is
    exempt entirely, because its subject is the venue in its own heading and
    the picker naming a *different* venue must not empty it.
    """
    from src.report_assets import JS

    def slice_of(name: str, until: str) -> str:
        return JS.split(f"function {name}(")[1].split(f"function {until}(")[0]

    board = slice_of("renderOddsScreen", "renderBrowseGames")
    assert "sportRows()" in board and "brandGames(" in board
    assert "currentRows()" not in board

    events = slice_of("renderEvents", "selectEvent")
    assert "currentRows()" not in events

    fixture = slice_of("selectEvent", "renderEventDetail") if "function renderEventDetail(" in JS \
        else JS.split("function selectEvent(")[1].split("\nfunction ")[0]
    assert "sportRows()" in fixture
    assert "currentRows()" not in fixture

    feed_rows = JS.split("function feedFilteredRows(")[1].split("\nfunction ")[0]
    assert "sportRows()" in feed_rows and "currentRows()" not in feed_rows

    book_panel = slice_of("renderBook", "renderOdds")
    assert "sportRows()" in book_panel
    assert "currentRows()" not in book_panel
    assert "keepBrand(" not in book_panel


def test_the_books_panel_says_the_filter_does_not_apply_there() -> None:
    """Books lists every venue whatever the picker says, and says so on screen.

    A venue that failed produced no rows at all, so a row-based filter would
    erase exactly the venues the panel exists to report — and a deliberate
    exemption that is not stated reads as a bug to the next person.
    """
    from src.report_assets import BODY, JS

    assert 'id="sources-note"' in BODY
    sources = JS.split("function renderSources(")[1].split("function renderBook(")[0]
    assert "currentRows()" not in sources
    assert "does not hide" in sources


def test_a_position_survives_on_one_matching_leg() -> None:
    """Filtering arbitrage by book means "has a leg here", never "entirely here".

    Every opportunity spans two counterparties by construction, so requiring
    every leg would return nothing for every book, every time — and the empty
    state explains the semantics so a reader cannot mistake it.
    """
    from src.report_assets import JS

    arb = JS.split("function renderArb(")[1].split("function originPill(")[0]
    assert ".some((leg) => keepBrand(leg.source))" in arb
    assert ".every((leg) => keepBrand" not in arb
    assert "not which are entirely there" in JS


def test_the_unreachable_total_is_never_recomputed() -> None:
    """A reader-side filter may hide a flagged position, never shrink the total.

    ``non_local_flagged`` is composed in Python over the whole run; the page
    subtracts what it rendered and names the filter hiding the rest, so
    narrowing the board cannot under-report unreachability.
    """
    from src.report_assets import JS

    assert "Number(bag.non_local_flagged || 0) - flagged" in JS
    assert "const narrowedBy = " in JS
    # Reachability vocabulary, not licensing — the Kalshi inversion.
    assert "no leg at a venue you can reach from this jurisdiction" in JS


def test_a_hand_typed_book_is_never_guessed_at() -> None:
    """The ledger fold has exact arms only — no case-folding, no substrings.

    ``leg.book`` is operator prose on the record of real money; "DK app"
    silently becoming DraftKings would re-attribute profit, so the fold is a
    lookup and a fallback to the string itself, nothing fuzzier.
    """
    from src.report_assets import JS

    fold = JS.split("const ledgerBrand = ")[1].split("\n")[0]
    for guess in (".startsWith(", ".includes(", ".toLowerCase(", ".replace("):
        assert guess not in fold, f"ledgerBrand guesses via {guess}"
    # And the bets panel folds through it rather than comparing raw strings.
    bets = JS.split("function renderBets(")[1].split("function slipCard(")[0]
    assert "keepLedgerBook(leg.book)" in bets


def test_the_number_of_sortable_tables_the_comments_claim_is_the_real_one() -> None:
    """The prose beside this mechanism named a table count that was one too many,
    in five places at once, and every one of them was new.

    The mechanism was right and the number was decoration, which is exactly the
    shape that survives a review — so it is counted from the code instead.
    Sorting is opt-in: a table either passes ``sort`` in its options or calls
    ``applySort`` itself, and both are grep-able.
    """
    import re

    from src.report_assets import JS

    opted = set(re.findall(r"\bsort:\s*'([a-z-]+)'", JS))
    direct = set(re.findall(r"applySort\(\s*'([a-z-]+)'", JS))
    tables = opted | direct
    assert tables == {"findings", "rejections", "odds-table", "promo-campaign"}, tables
    spelled = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    root = Path(__file__).resolve().parents[1]
    for name in ("src/report_assets.py", "docs/architecture.md",
                 "tests/dashboard_smoke.mjs", "tests/test_report.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for word, value in spelled.items():
            for phrase in (f"{word} sortable table", f"{word} tables sharing",
                           f"shared by {word} it would"):
                if phrase in text:
                    assert value == len(tables), (
                        f"{name} says {phrase!r}; there are {len(tables)} sortable tables"
                    )


def test_each_table_holds_its_own_sort() -> None:
    """Four sortable tables, four sorts — keyed on the region id.

    The old module-global ``sortKey``/``sortDir`` pair was read by one table;
    shared by four it would make sorting one reorder another.  The map replaces
    it, the shared wiring is applied exactly once, and each table passes its
    own id.
    """
    from src.report_assets import JS

    assert "let sortKey" not in JS
    assert "let sortDir" not in JS
    assert "const SORTS = new Map()" in JS
    assert JS.count("th.classList.add('sortable')") == 1
    for region in ("'odds-table'", "'findings'", "'rejections'", "'promo-campaign'"):
        assert f"applySort({region}" in JS or f"sort: {region}" in JS, region


def test_the_bankroll_totals_are_outside_the_book_filter() -> None:
    """Money summaries never follow a view filter.

    ``betSummary()`` is settlement arithmetic computed in Python; the slips
    list narrows under a pick, but the bankroll strip, the per-book strip and
    the nav badge stay whole — a filtered profit figure would be a different
    (and wrong) claim about the ledger.
    """
    from src.report_assets import JS

    bets = JS.split("function renderBets(")[1].split("function slipCard(")[0]
    stats_part = bets.split("const stats = el('bets-stats')")[1].split("const listNote")[0]
    assert "keepBrand(" not in stats_part
    assert "shown" not in stats_part, "the bankroll strip reads the narrowed slips"
    nav = JS.split("function renderNavCounts(")[1].split("\nfunction ")[0]
    nav_bets = nav.split("nav-bets")[1].split("if (!run)")[0]
    assert "keepBrand(" not in nav_bets and "ledgerBrand(" not in nav_bets
