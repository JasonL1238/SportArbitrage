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
    # This page embeds every run, so the truncation branch is unreachable here
    # and the harness says so rather than passing silently.
    assert "truncation not exercised" in result.stdout


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
        "run-list", "/api/collect", "/api/status", "paintScrapeProgress",
        "promo-scrape-btn", "promo-scrape-status", "/api/promos/collect",
        "promo-region", "promo-detail", "usage_guidance", "selectedPromoKey",
        "/api/promos/status", "wirePromoScrapeButton", 'id="promos"',
    ):
        assert needle in BODY or needle in JS, needle
    assert 'id="history"' in BODY
    assert "PRIMARY_PANELS" in JS
    assert "selectRun" in JS
    assert "wireScrapeButton" in JS
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

    payload = report_mod._promo_payload_for_serve()
    assert payload["offers"], "the serve response lost the offers"
    meta = payload["plan_meta"]
    assert meta is not None
    assert meta.get("reason") != "no_odds_run", (
        "the serve response claims there is no odds run while one is stored"
    )
    assert meta.get("odds_run_id") is not None


def test_the_serve_response_degrades_rather_than_failing_the_scrape(
    tmp_path, monkeypatch,
) -> None:
    """An unreadable odds database must not fail a promo scrape's response."""
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


def test_report_tests_never_read_the_developers_promo_database(tmp_path) -> None:
    """The autouse isolation is in force, and is checked rather than assumed.

    ``build_report`` reads ``settings.PROMO_DB_PATH`` itself, so without the
    fixture in ``tests/conftest.py`` every report test silently renders whatever
    was last scraped into ``data/promos.sqlite3`` — which is how a venue's own
    telemetry script came to be counted as page code.
    """
    from src import report as report_mod
    from src import settings as settings_mod

    default = Path("data/promos.sqlite3").resolve()
    for module in (settings_mod, report_mod.settings):
        active = Path(module.PROMO_DB_PATH).resolve()
        assert active != default, (
            "a report test is pointed at the real promo database; the autouse "
            "_hermetic_promo_db fixture in tests/conftest.py is not in force"
        )
        assert not active.exists(), (
            "the isolated promo database should not exist unless a test built it"
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

    assert skipped, f"{source} skipped nothing, so this guard proves nothing"
    unexplained = [
        reason for reason in skipped
        if not any(reason.startswith(prefix) for prefix, _ in SKIP_NOTES)
    ]
    assert unexplained == [], unexplained
