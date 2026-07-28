"""Build a local dashboard from what the collector has already stored.

This reads the SQLite store and writes one self-contained HTML file.  It is a
*view*, never a second source of truth: every number on the page comes from a
query in this module, and the module never fetches anything.  That keeps the
report honest — if the page shows 1,656 rows it is because 1,656 rows are in the
database, and generating the report cannot alter what was collected.

The page opens from ``file://`` with no network access: no CDN, no webfont, no
remote image, no telemetry.

Now that several sports are collected, the page has to answer one more question
before any of its prices mean anything: **which sports are actually usable?**  A
sport priced by a single book cannot be compared against anything, so a large row
count for it is not progress.  The coverage grid states the per-sport, per-book
counts and marks every sport against the two-book bar, and the sport filter lets
the rest of the page be read one sport at a time.

    python -m src.report                 # write data/dashboard.html
    python -m src.report --open          # ... and open it
    python -m src.report --serve 8000    # serve it locally instead
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from src import settings
from src.leagues import is_known
from src.leagues import league as get_league
from src.report_assets import BODY, CSS, JS
from src.schema import (
    PERIOD_RULES,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    period_rules,
    scoring_unit,
)
from src.store import IncompatibleDatabase, Store

#: How many recent runs to embed quote rows for.  Bounded because the page holds
#: every row in memory; the run list itself always covers more runs than this.
DEFAULT_QUOTE_RUNS = 6
DEFAULT_RUN_LIMIT = 40

#: Two books is the bar for a sport being comparable at all.  Mirrors
#: ``src.collector.MIN_HEALTHY_SOURCES``; kept as its own constant rather than
#: imported, because the report must stay usable when the adapters are not.
MIN_BOOKS_FOR_COMPARISON = 2

#: What each source is, in the reader's terms rather than the parser's.
SOURCE_NOTES: dict[str, dict[str, str]] = {
    "fanduel": {
        "label": "FanDuel",
        "host": "sbapi.il.sportsbook.fanduel.com",
        "what": "Read from the feeds behind FanDuel's own sport pages. Whole-game bets only "
                "— who wins, the handicap, and the combined total — but it does publish "
                "extra numbers beyond its main line.",
    },
    "pinnacle": {
        "label": "Pinnacle",
        "host": "guest.api.arcadia.pinnacle.com",
        "what": "The most detailed of the three: parts of a game as well as the whole, and "
                "the only one that tells you the largest bet it will accept.",
    },
    "betrivers_kambi": {
        "label": "BetRivers",
        "host": "eu-offering-api.kambicdn.com",
        "what": "Lists the most fixtures, and says in words which part of the game each bet "
                "covers — including whether overtime counts. That is what let Pinnacle's "
                "unlabelled period numbering be verified.",
    },
}

#: Why an offer the books do publish is deliberately not collected.  Matched by
#: prefix, longest first, so a specific reason beats the family it belongs to.
SKIP_NOTES: list[tuple[str, str]] = [
    # ── not a single fixture ─────────────────────────────────────────────────
    ("market_on_non_game_event",
     "A market attached to a season-long container — a futures or specials page — "
     "rather than to a fixture."),
    ("non_game_event",
     "A season-long or tournament-long bet: division winners, awards, outrights. Not a "
     "single fixture."),
    ("market_on_out_of_scope_matchup",
     "A market belonging to something already ruled out as a fixture, so the market "
     "goes with it rather than being kept as an orphan."),
    ("participant_priced_market",
     "Priced per competitor with no home/away side to it — the shape a futures market "
     "has. Reading it as a fixture would invent a game."),
    ("matchup_type:special",
     "Bets on individual players, or on the exact final score. Not a bet on the "
     "fixture itself, so out of scope."),
    ("child_matchup",
     "A duplicate of a fixture the book also lists in a different scoring unit — games "
     "won rather than sets, corners rather than goals. Treating it as its own fixture "
     "would fabricate a second game between the same two competitors."),
    ("alternate_scoring_unit",
     "The same fixture priced in a different unit (games, corners). A handicap in games "
     "is not a handicap in sets, so it is not filed as one."),
    ("corners_market",
     "Corners rather than goals. A different thing being counted."),
    ("tennis_doubles", "A doubles match: four competitors, not two."),
    ("market_on_doubles_event", "Belongs to a doubles match, which is out of scope."),

    # ── already started, or already collected ────────────────────────────────
    ("event_already_started",
     "The fixture is under way. Only pre-match prices are collected, because an in-play "
     "price and a pre-match price are not the same market."),
    ("market_on_started_event",
     "Belongs to a fixture already under way, so it is not a pre-match price."),
    ("already_collected_from_sport_page",
     "The same market was already read from another of this book's pages. Kept once, "
     "counted here, so the duplicate is visible rather than silently dropped."),

    # ── a different contract from the one it resembles ───────────────────────
    ("out_of_scope_market:different_settlement_window",
     "Settles on a stretch of the game this tool does not track, and no second book "
     "labels it the same way, so there is no way to confirm what it settles on."),
    ("out_of_scope_market:voids_on_draw",
     "Your stake comes back if the scores are level, which makes it a different bet "
     "from picking a winner."),
    ("out_of_scope_market:bonus_settlement_rule",
     "Carries an extra payout rule, so it is not the plain market it looks like."),
    ("out_of_scope_market:combined_outcome",
     "One bet on two things happening together. Not one of the four kinds collected."),
    ("out_of_scope_market:margin_prop",
     "A bet on the exact winning margin rather than on a handicap."),
    ("out_of_scope_market:goal_prop", "A bet about a specific goal, not about the result."),
    ("out_of_scope_market:period_out_of_scope",
     "A part of the game whose settlement rules are not recorded, so the pipeline "
     "refuses to guess whether a tie voids the bet."),
    ("out_of_scope_market:prop", "A bet on a player or an occurrence within the game."),
    ("out_of_scope_market", "A kind of market outside the four collected here."),
    ("period_out_of_scope",
     "A part of the game whose settlement rules are not recorded. Collecting it would "
     "mean guessing whether a level score voids the bet, and that guess is worth the "
     "whole stake."),
    ("market_out_of_scope",
     "A market this sport does not have in a comparable form here — a tennis handicap "
     "in games, for instance."),
    ("not_a_full_period_result",
     "Settles on something other than the result of a whole scoring window."),
    ("half_only_market",
     "Settles at half time only, and is collected only where the book labels the period "
     "unambiguously."),
    ("draw_voids_market",
     "A two-way price on a fixture that can end level: a draw refunds the stake. That "
     "is a different contract from a three-way result market."),
    ("soccer_3way_handicap_prices_a_draw",
     "A handicap with three results, including the draw. Not the same product as the "
     "two-way handicap, so it is not filed as one."),
    ("asian_quarter_line_splits_the_stake",
     "A quarter line splits your stake across two numbers. Merging it with the ordinary "
     "total would misstate what settles."),
    ("odd_even_market",
     "Whether the total is an odd or even number. No over/under and no number, so it is "
     "not a total."),
    ("yes_no_market", "A yes/no proposition rather than a result, handicap or total."),
    ("combined_outcome_market",
     "One bet on two outcomes together — a different contract from either alone."),
    ("correct_score_market", "The exact final score, not the result."),
    ("player_prop", "A bet on one player's performance rather than on the fixture."),
    ("pitcher_conditional_market",
     "Cancelled unless a named starter actually plays. That condition makes it a "
     "different bet from the plain one."),
    ("opta_derived_market",
     "Priced off a third-party statistics feed rather than being one of the book's own "
     "game markets."),
    ("unmapped_criterion",
     "The book's own name for this market is not one of the labels this tool recognises. "
     "Guessing which of the four kinds it is would put a false price in the data, so it "
     "is counted and left alone."),
    ("criterion:",
     "The book's own label for a market outside the four kinds collected here."),
    ("extra_time",
     "A cup tie's extra time, or 'to qualify'. A different contract from the 90 minutes "
     "the ordinary market settles on."),
    ("market_type:OUTRIGHT",
     "A season-long or tournament-long bet rather than a single fixture."),
    ("futures", "A season-long or tournament-long bet rather than a single fixture."),
    ("outright", "A season-long or tournament-long bet rather than a single fixture."),
    ("unknown_league",
     "A competition this tool has no recorded rules for. Counted rather than guessed at."),

    # ── the row itself was unusable ──────────────────────────────────────────
    ("runner_without_price",
     "A selection the book listed without a price. There is nothing to record."),
    ("outcome_without_odds",
     "A selection the book listed without a price. There is nothing to record."),
    ("feed_american_odds_disagreed_with_decimal",
     "The book sent two forms of the same price that do not agree with each other. "
     "Neither can be trusted over the other, so the row is not kept."),
]

#: Field-by-field reference, shown so "one consistent schema" is inspectable.
#: Every field of :class:`~src.schema.Quote` must appear — a field with no
#: explanation would render as a blank row in the page's own answer to "what is
#: one row?".
SCHEMA_FIELDS: list[tuple[str, str, bool, str]] = [
    ("source", "str", True, "Which sportsbook this price came from."),
    ("observed_at", "datetime", True, "When this collector fetched the payload. Never a book's own clock."),
    ("raw_ref", "str", True,
     "Points at the stored raw response the price was parsed from. Single-valued, so "
     "it can be checked against the response's own fetch time."),
    ("identity_raw_ref", "str", False,
     "Points at the response that supplied the fixture's identity, when that came from "
     "a different call than the price — Pinnacle needs two. Kept separate so a reader "
     "never has to split one field on a grammar nobody declared."),
    ("sport", "enum", True,
     "Which sport. Part of every fixture's identity, so a baseball handicap and a "
     "soccer handicap can never be joined even though both are 'spread'."),
    ("league", "str", True,
     "Canonical competition key — MLB, WNBA, EPL, ATP. Recorded for coverage "
     "reporting, and deliberately not part of the fixture key: the books disagree "
     "about classification constantly, and that must not break a join."),
    ("event_key", "str", True,
     "Shared fixture identity: AWAY@HOME:date, built from participant keys on the "
     "league's own scheduling date."),
    ("source_event_id", "str", True, "The book's own id for the fixture."),
    ("home_participant", "str", True,
     "Resolved identity of the home side (MLB-MIA, SOCCER-arsenal). This is what "
     "everything joins on."),
    ("away_participant", "str", True,
     "Resolved identity of the away side. In tennis there is no home player, so the "
     "two are ordered deterministically instead of trusting the book's ordering."),
    ("home_team", "str", True, "Home side's canonical display name, for humans."),
    ("away_team", "str", True, "Away side's canonical display name, for humans."),
    ("commence_time", "datetime", True, "Scheduled start, in UTC."),
    ("market", "enum", True,
     "One of four: who wins, winner with a handicap, combined total, one side's total."),
    ("period", "enum", True,
     "The scoring window the bet settles on: the whole game including overtime, "
     "regulation only, a half, or an opening stretch of innings."),
    ("selection", "enum", True, "home, away, draw, over or under."),
    ("side", "enum", False, "Which participant a team total refers to. Set only for team totals."),
    ("line", "float", False, "The handicap or total, from this selection's perspective. Set only where meaningful."),
    ("is_alternate", "bool", True, "True for an alternate-line market. Books really do price the same number twice."),
    ("decimal_odds", "float", True, "Price as a decimal multiplier. Must be above 1.0."),
    ("american_odds", "int", True, "The same price in American format."),
    ("implied_probability", "float", True, "1 / decimal odds, as the book implies it."),
    ("source_market_id", "str", False, "The book's own market id — what defines one market group."),
    ("source_selection_id", "str", False, "The book's own id for this selection."),
    ("limit_amount", "float", False, "Maximum stake, where the book publishes one."),
    ("status", "enum", True, "active or suspended."),
    ("last_change_at", "datetime", False, "When the book says the price last moved, where it says so."),
]

#: Plain-language glossary.  Betting notation is compact but opaque, and this page is
#: meant to be readable by someone who has never placed a bet, so every term the
#: tables use is spelled out here alongside the word a sportsbook would use.
GLOSSARY: list[dict[str, str]] = [
    {
        "term": "Sportsbook",
        "plain": "A company that takes bets. This tool reads three of them: FanDuel, "
                 "Pinnacle and BetRivers.",
    },
    {
        "term": "A price (decimal)",
        "plain": "What a winning bet pays back in total, per unit staked. 2.30 means a "
                 "winning $100 bet returns $230 — your $100 back plus $130 profit.",
    },
    {
        "term": "American odds",
        "plain": "The same price written the way US books show it. +130 means a $100 bet "
                 "profits $130. −150 means you must stake $150 to profit $100. A minus "
                 "sign marks the favourite.",
    },
    {
        "term": "The book's implied chance",
        "plain": "Turn the price back into a percentage: 1 ÷ 2.30 = 43%. That is roughly "
                 "how likely the book is treating that outcome — plus a bit extra, which "
                 "is its cut.",
    },
    {
        "term": "The book's margin",
        "plain": "Add up the implied chances for every side of one bet and it comes to "
                 "more than 100%. The excess is the book's built-in profit. Around 4–7% "
                 "is normal. Under 100% would be impossible, and would mean this tool had "
                 "paired the wrong prices together.",
    },
    {
        "term": "Two or more books",
        "plain": "The bar for a sport being usable at all. A single book's price cannot "
                 "be compared with anything, so a sport only one book covers is listed "
                 "as not comparable however many prices it has.",
    },
    {
        "term": "Who wins",
        "plain": "The simplest bet: pick the winner, no adjustments. Books call it the "
                 "moneyline. In soccer — and in hockey over regulation time only — a draw "
                 "is a third thing you can back, because the game really can end level.",
    },
    {
        "term": "Winner with a handicap",
        "plain": "One side is given a head start, to even up a mismatch. \"Reds −1.5\" "
                 "wins only if the Reds win by 2 or more. Books call it the run line in "
                 "baseball, the puck line in hockey, the point spread in football and "
                 "basketball and the Asian handicap in soccer — the same bet each time.",
    },
    {
        "term": "Combined total",
        "plain": "Ignore who wins: bet on whether both sides together score more (over) "
                 "or fewer (under) than a set number. What gets counted depends on the "
                 "sport — runs, goals, points or games.",
    },
    {
        "term": "One side's total",
        "plain": "The same over/under idea applied to a single team's score. Books call "
                 "it a team total.",
    },
    {
        "term": "Part of the game",
        "plain": "Most bets settle on the whole game as the book settles it — overtime, "
                 "extra innings and shootouts included. Some settle on regulation time "
                 "only, or on a half, or on the first innings. Those are different bets: "
                 "a hockey game level after 60 minutes is a draw for one and not for the "
                 "other, so this tool never compares them with each other.",
    },
    {
        "term": "The number (the line)",
        "plain": "The figure a bet hinges on — the handicap, or the over/under total. "
                 "Half numbers like 8.5 exist so there is no tie.",
    },
    {
        "term": "Alternate line",
        "plain": "Besides its main number, a book offers the same bet at other numbers at "
                 "different prices. Both are real bets, so both are collected and marked.",
    },
    {
        "term": "Max bet",
        "plain": "The largest stake the book will accept on that selection, where it "
                 "publishes one. Pinnacle does; the others mostly do not.",
    },
    {
        "term": "Suspended",
        "plain": "The book is showing the price but not currently accepting bets on it — "
                 "usually because it is about to move.",
    },
    {
        "term": "Fixture ID",
        "plain": "This tool's own name for a fixture, like MLB-CLE@MLB-CIN:2026-07-28 — "
                 "the away side, the home side, and the date. Each book uses its own "
                 "private ID, so this is what lets their prices be compared.",
    },
    {
        "term": "Competition (league)",
        "plain": "Which league or tour a fixture belongs to. It is recorded, but it is "
                 "deliberately not part of a fixture's identity: the books disagree about "
                 "classification constantly, and that disagreement must not be able to "
                 "stop their prices being compared.",
    },
    {
        "term": "Saved response, fingerprint",
        "plain": "Every page fetched is saved to disk exactly as it arrived, with a "
                 "fingerprint of its contents. Two fetches with the same fingerprint are "
                 "byte-for-byte identical, which is how a stuck feed becomes visible.",
    },
    {
        "term": "Re-check from saved data",
        "plain": "The saved pages are read again from disk and re-interpreted from "
                 "scratch. If that produces anything different from what was stored, "
                 "something is wrong. Its verdict is on the Checks page.",
    },
]

VOCAB_NOTE = (
    "These lists are closed. A source value that cannot be mapped onto one of them is "
    "rejected upstream and counted, never passed through as free text — a free-text "
    "fallback is what makes unnormalized data look normalized."
)


class _Interner:
    """String table, so a value repeated on a thousand rows is stored once."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._index: dict[str, int] = {}

    def __call__(self, value: object) -> int | None:
        if value is None:
            return None
        text = str(value)
        found = self._index.get(text)
        if found is None:
            found = len(self.values)
            self._index[text] = found
            self.values.append(text)
        return found


QUOTE_COLUMNS = [
    "run_id", "source", "sport", "league", "event_key", "source_event_id",
    "home_participant", "away_participant", "home_team", "away_team", "commence_time",
    "market", "period", "selection", "side", "line", "is_alternate",
    "decimal_odds", "american_odds", "implied_probability", "source_market_id",
    "limit_amount", "status", "last_change_at",
]

_INTERNED = frozenset({
    "source", "sport", "league", "event_key", "source_event_id", "home_participant",
    "away_participant", "home_team", "away_team", "commence_time", "market", "period",
    "selection", "side", "source_market_id", "status", "last_change_at",
})


def build_report(
    store: Store,
    *,
    run_limit: int = DEFAULT_RUN_LIMIT,
    quote_runs: int = DEFAULT_QUOTE_RUNS,
    replay_note: str = "not checked",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble everything the page shows, as plain JSON-ready data."""
    generated_at = generated_at or datetime.now(UTC)

    run_rows = store.query(
        """SELECT r.id, r.started_at, r.finished_at, r.ok, r.quote_count, r.event_count,
                  r.error_count, r.warning_count, r.note,
                  (SELECT COUNT(*) FROM raw_response w WHERE w.run_id = r.id) AS raw_count
             FROM collection_run r
            ORDER BY r.id DESC LIMIT ?""",
        (run_limit,),
    )
    if not run_rows:
        raise LookupError(f"no collection runs recorded in {store.path}")

    runs: list[dict[str, Any]] = []
    for row in run_rows:
        health = [
            {
                "key": h["source_key"],
                "ok": bool(h["ok"]),
                "quote_count": h["quote_count"],
                "event_count": h["event_count"],
                "request_count": h["request_count"],
                "raw_bytes": h["raw_bytes"],
                "latency_ms": h["latency_ms"],
                "rejection_count": h["rejection_count"],
                "skipped_count": h["skipped_count"],
                "unchanged_payloads": h["unchanged_payloads"],
                "error_kind": h["error_kind"],
                "error_message": h["error_message"],
            }
            for h in store.health_for_run(row["id"])
        ]
        sports, gaps = _coverage_for_run(store, row["id"])
        runs.append(
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "duration_ms": _duration_ms(row["started_at"], row["finished_at"]),
                "ok": bool(row["ok"]),
                "quote_count": row["quote_count"],
                "event_count": row["event_count"],
                "error_count": row["error_count"],
                "warning_count": row["warning_count"],
                "raw_count": row["raw_count"],
                "note": row["note"],
                "total_latency_ms": sum(h["latency_ms"] or 0 for h in health),
                "sources": sorted(health, key=lambda h: h["key"]),
                "sports": sports,
                "league_gaps": gaps,
            }
        )

    quote_run_ids = [run["id"] for run in runs[:quote_runs]]
    quotes = _quote_payload(store, quote_run_ids)
    strings: list[str] = quotes.pop("_strings")
    latest_run_id = runs[0]["id"]

    detail_ids = tuple(quote_run_ids)
    placeholders = ",".join("?" * len(detail_ids))

    return {
        "meta": {
            "generated_at": generated_at.isoformat(),
            "db_path": str(store.path),
            "db_name": store.path.name,
            "latest_run_id": latest_run_id,
            "lede": _lede(runs[0]),
            "slate_dates": _slate_dates(quotes, strings),
            "replay_note": replay_note,
            "vocab_note": VOCAB_NOTE,
            "runs_recorded": len(runs),
            "runs_with_rows": len(quote_run_ids),
            "min_books": MIN_BOOKS_FOR_COMPARISON,
        },
        "sources": [
            dict(key=key, **SOURCE_NOTES.get(key, _unknown_source(key)))
            for key in sorted({h["key"] for run in runs for h in run["sources"]})
        ],
        "runs": runs,
        "quotes": quotes,
        "participants": _participants(quotes, strings),
        "sport_facts": _sport_facts(),
        "period_labels": _period_labels(),
        "league_names": _league_names(store),
        "skipped": [
            {"run_id": r["run_id"], "source": r["source"], "reason": r["reason"], "count": r["count"]}
            for r in store.query(
                f"SELECT run_id, source, reason, count FROM skipped WHERE run_id IN ({placeholders})",
                detail_ids,
            )
        ],
        "skip_notes": [list(pair) for pair in SKIP_NOTES],
        "rejections": [
            {"run_id": r["run_id"], "source": r["source"], "reason": r["reason"], "detail": r["detail"]}
            for r in store.query(
                f"SELECT run_id, source, reason, detail FROM rejection "
                f"WHERE run_id IN ({placeholders}) ORDER BY id LIMIT 500",
                detail_ids,
            )
        ],
        "findings": [
            {
                "run_id": r["run_id"], "severity": r["severity"], "code": r["code"],
                "message": r["message"], "source": r["source"], "event_key": r["event_key"],
            }
            for r in store.query(
                f"SELECT run_id, severity, code, message, source, event_key FROM finding "
                f"WHERE run_id IN ({placeholders}) "
                f"ORDER BY CASE severity WHEN 'error' THEN 0 ELSE 1 END, id LIMIT 500",
                detail_ids,
            )
        ],
        "raws": [
            {
                "run_id": r["run_id"], "source": r["source"], "endpoint": r["endpoint"],
                "url": r["url"], "status_code": r["status_code"], "fetched_at": r["fetched_at"],
                "byte_size": r["byte_size"], "sha256": r["sha256"], "unchanged": bool(r["unchanged"]),
            }
            for r in store.query(
                f"SELECT * FROM raw_response WHERE run_id IN ({placeholders}) ORDER BY run_id DESC, id",
                detail_ids,
            )
        ],
        "glossary": GLOSSARY,
        "schema_fields": [
            {"name": name, "type": kind, "required": required, "note": note}
            for name, kind, required, note in SCHEMA_FIELDS
        ],
        "vocabularies": [
            {"name": "sport", "values": [s.value for s in Sport]},
            {"name": "market", "values": [m.value for m in Market]},
            {"name": "period", "values": [p.value for p in Period]},
            {"name": "selection", "values": [s.value for s in Selection]},
            {"name": "side", "values": [s.value for s in Side]},
            {"name": "status", "values": [s.value for s in QuoteStatus]},
        ],
        "strings": strings,
    }


def _blank_sport(sport: str) -> dict[str, Any]:
    return {
        "sport": sport,
        "per_source": {},
        "leagues": {},
        "quote_count": 0,
        "event_count": 0,
    }


def _coverage_for_run(
    store: Store, run_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Per-sport coverage for one run, and the configured leagues that came back empty.

    Both come out of the database: the per-sport, per-book counts from the stored
    rows, and the gaps from what each source recorded it had been *asked* for.
    Without that second query a league nobody managed to collect is
    indistinguishable from a league nobody wanted.
    """
    per_sport: dict[str, dict[str, Any]] = {}
    for row in store.sport_source_matrix(run_id):
        sport = per_sport.setdefault(row["sport"], _blank_sport(row["sport"]))
        sport["per_source"][row["source"]] = (
            sport["per_source"].get(row["source"], 0) + row["quote_count"]
        )
        league = sport["leagues"].setdefault(
            row["league"],
            {"league": row["league"], "per_source": {}, "quote_count": 0, "event_count": 0},
        )
        league["per_source"][row["source"]] = row["quote_count"]
        league["quote_count"] += row["quote_count"]
        sport["quote_count"] += row["quote_count"]

    # Fixture counts are queried separately: summing the per-source counts would
    # count one fixture once per book that priced it.
    for row in store.query(
        """SELECT sport, league, COUNT(DISTINCT event_key) AS event_count
             FROM quote WHERE run_id = ? GROUP BY sport, league""",
        (run_id,),
    ):
        sport = per_sport.get(row["sport"])
        if sport is not None and row["league"] in sport["leagues"]:
            sport["leagues"][row["league"]]["event_count"] = row["event_count"]
    for row in store.sport_coverage(run_id):
        if row["sport"] in per_sport:
            per_sport[row["sport"]]["event_count"] = row["event_count"]

    # Fixtures more than one book priced.  A sport can have two books and no
    # overlap at all — one book's NHL openers against another's friendly — and in
    # that case there is still nothing on the page that can be compared, so the
    # count is shown next to the book count rather than instead of it.
    cross_book = store.cross_book_event_counts(run_id, min_books=MIN_BOOKS_FOR_COMPARISON)

    gaps: list[dict[str, str]] = []
    for row in store.league_coverage(run_id):
        # A sport that was configured and returned nothing at all still gets a
        # row, reported as priced by zero books.  Leaving it out would make the
        # page silent about exactly the case it exists to surface.
        per_sport.setdefault(row["sport"], _blank_sport(row["sport"]))
        if row["quote_count"] == 0:
            gaps.append(
                {"source": row["source_key"], "league": row["league"], "sport": row["sport"]}
            )

    result: list[dict[str, Any]] = []
    for sport in sorted(per_sport):
        entry = per_sport[sport]
        books = sorted(key for key, count in entry["per_source"].items() if count)
        result.append(
            {
                "sport": sport,
                "quote_count": entry["quote_count"],
                "event_count": entry["event_count"],
                "cross_book_events": cross_book.get(sport, 0),
                "books": books,
                "per_source": entry["per_source"],
                "meets_bar": len(books) >= MIN_BOOKS_FOR_COMPARISON,
                "comparable": (
                    len(books) >= MIN_BOOKS_FOR_COMPARISON and cross_book.get(sport, 0) > 0
                ),
                "leagues": [
                    {
                        "league": league["league"],
                        "quote_count": league["quote_count"],
                        "event_count": league["event_count"],
                        "books": sorted(k for k, v in league["per_source"].items() if v),
                        "per_source": league["per_source"],
                    }
                    for league in sorted(
                        entry["leagues"].values(), key=lambda item: -item["quote_count"]
                    )
                ],
            }
        )
    return result, sorted(gaps, key=lambda gap: (gap["sport"], gap["league"], gap["source"]))


def _quote_payload(store: Store, run_ids: Sequence[int]) -> dict[str, Any]:
    """Quote rows for the given runs, columnar and string-interned."""
    intern = _Interner()
    rows: list[list[Any]] = []
    if run_ids:
        placeholders = ",".join("?" * len(run_ids))
        selected = ", ".join(QUOTE_COLUMNS)
        for row in store.query(
            f"SELECT {selected} FROM quote WHERE run_id IN ({placeholders}) ORDER BY run_id, id",
            tuple(run_ids),
        ):
            rows.append(
                [
                    intern(row[name]) if name in _INTERNED else row[name]
                    for name in QUOTE_COLUMNS
                ]
            )
    return {"columns": QUOTE_COLUMNS, "rows": rows, "_strings": intern.values}


def _participants(quotes: dict[str, Any], strings: Sequence[str]) -> dict[str, dict[str, str]]:
    """Map each participant key onto a display name and a short label.

    "Reds win by 2 or more" is a sentence; "home -1.5" is a notation.  The label
    is derived from the identity the pipeline already resolved and the display
    name stored beside it — never from a fresh guess at the book's spelling — so
    the page cannot name a competitor the validator would have rejected.
    """
    names: dict[str, dict[str, str]] = {}
    columns = quotes["columns"]
    pairs = (
        (columns.index("home_participant"), columns.index("home_team")),
        (columns.index("away_participant"), columns.index("away_team")),
    )
    for row in quotes["rows"]:
        for key_index, name_index in pairs:
            key = row[key_index]
            if key is None:
                continue
            participant = strings[key]
            if participant in names:
                continue
            display = strings[row[name_index]] if row[name_index] is not None else participant
            # Keys are namespaced — "MLB-CIN", "TENNIS-humbert.ugo" — and the part
            # after the namespace is the shortest honest label available.
            _, _, short = participant.partition("-")
            names[participant] = {
                "name": display,
                "abbr": short or participant,
                "short": _short_name(display, short or participant),
            }
    return names


def _short_name(display: str, fallback: str) -> str:
    """The last word of a display name: "Reds", "Marlins", "Arsenal", "Humbert"."""
    words = display.split()
    if not words:
        return fallback
    return words[-1]


def _sport_facts() -> dict[str, dict[str, Any]]:
    """What each sport's totals count, and where a draw is a real outcome.

    Read straight out of :mod:`src.vocab`, so the page's wording cannot drift from
    the settlement rules the pipeline actually applies.  This is what lets one
    describe-the-bet routine say "9 or more runs" for baseball and "goals" for
    hockey without either being hardcoded beside the other.
    """
    facts: dict[str, dict[str, Any]] = {}
    for sport in Sport:
        facts[sport.value] = {
            "label": sport.value.capitalize(),
            "unit": scoring_unit(sport),
            "periods": {
                period.value: {
                    "draw_is_priced": period_rules(sport, period).draw_is_priced,
                    "tie_possible": period_rules(sport, period).tie_possible,
                    "unit": period_rules(sport, period).scoring_unit,
                }
                for period in Period
                if (sport, period) in PERIOD_RULES
            },
        }
    return facts


def _period_labels() -> dict[str, dict[str, str]]:
    """Plain wording for each scoring window, and the book's own shorthand.

    ``full_game`` and ``regulation`` are spelled out because the difference
    between them is the whole reason they are separate rows: one includes
    overtime and cannot be tied, the other excludes it and can.
    """
    return {
        "full_game": {"plain": "Whole game", "term": "full game, overtime included"},
        "regulation": {"plain": "Regulation only", "term": "overtime excluded"},
        "first_half": {"plain": "First half", "term": "1st half"},
        "first_5_innings": {"plain": "First 5 innings", "term": "F5"},
        "first_1_inning": {"plain": "First inning", "term": "1st inning"},
    }


def _league_names(store: Store) -> dict[str, str]:
    """Human names for every league key present in the database."""
    names: dict[str, str] = {}
    for table in ("quote", "source_league"):
        for row in store.query(f"SELECT DISTINCT league FROM {table}"):
            key = row["league"]
            names.setdefault(key, get_league(key).name if is_known(key) else key)
    return names


def _unknown_source(key: str) -> dict[str, str]:
    return {"label": key, "host": "—", "what": "No description recorded for this source."}


def _duration_ms(started_at: str, finished_at: str | None) -> float | None:
    if not finished_at:
        return None
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(finished_at)
    return (end - start).total_seconds() * 1000


def _column(quotes: dict[str, Any], name: str) -> list[Any]:
    index = quotes["columns"].index(name)
    return [row[index] for row in quotes["rows"]]


def _slate_dates(quotes: dict[str, Any], strings: Sequence[str]) -> str:
    """The scheduling dates covered, read straight off the event keys."""
    keys = {i for i in _column(quotes, "event_key") if i is not None}
    dates = sorted({strings[i].split(":")[-1].split("#")[0] for i in keys})
    if not dates:
        return "no fixtures stored"
    return dates[0] if len(dates) == 1 else f"{dates[0]} … {dates[-1]}"


def _lede(latest: dict[str, Any]) -> str:
    names = {"betrivers_kambi": "BetRivers", "fanduel": "FanDuel", "pinnacle": "Pinnacle"}
    books = [names.get(h["key"], h["key"]) for h in latest["sources"] if h["quote_count"] > 0]
    listed = (
        " and ".join(books) if len(books) < 3 else f"{', '.join(books[:-1])} and {books[-1]}"
    )
    usable = [entry["sport"] for entry in latest["sports"] if entry["comparable"]]
    single = [entry["sport"] for entry in latest["sports"] if not entry["comparable"]]
    verdict = (
        f"Two or more books priced the same fixtures in {_join(usable)}, so those can "
        f"be compared."
        if usable
        else "No sport was priced by two books on this pass, so nothing can be compared."
    )
    if single:
        verdict += (
            f" {_join(single).capitalize()} did not clear that bar — one book only, or no "
            "fixture that two of them both priced — so those prices cannot be compared "
            "with anything. They are listed rather than hidden."
        )
    count = len(latest["sports"])
    return (
        f"Sportsbooks publish prices for today's fixtures on their own websites. This tool "
        f"reads them, converts three different formats into one, checks the result and "
        f"saves it. On its most recent pass it collected {latest['quote_count']:,} prices "
        f"across {latest['event_count']} fixtures in {count} sport"
        f"{'' if count == 1 else 's'} from {listed or 'no books'}. {verdict} Nothing on "
        f"this page is fetched live — it is all read back out of what was saved."
    )


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# ── rendering ────────────────────────────────────────────────────────────────


def _embed(data: dict[str, Any]) -> str:
    """JSON for a ``<script>`` block.

    ``<`` only ever appears inside a JSON string, so escaping it as ``\\u003c``
    keeps the payload valid JSON while making ``</script>`` unrepresentable.
    """
    dumped = json.dumps(data, separators=(",", ":"), allow_nan=False)
    return dumped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_fragment(data: dict[str, Any]) -> str:
    """Body-only markup, for hosts that supply their own document shell."""
    return (
        f"<style>{CSS}</style>\n"
        f"{BODY}\n"
        f'<script type="application/json" id="report-data">{_embed(data)}</script>\n'
        f"<script>{JS}</script>\n"
    )


def render_page(data: dict[str, Any]) -> str:
    """A complete standalone document."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>Odds Collector — what got grabbed</title>\n"
        "</head>\n<body>\n"
        f"{render_fragment(data)}"
        "</body>\n</html>\n"
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _replay_note(store: Store, run_id: int) -> str:
    """Re-parse the run's stored bytes so the page can state replay's verdict.

    Imported lazily: the report itself must stay usable without the adapters.
    """
    try:
        from src.collector import replay_run
        from src.raw_store import RawStore

        ok, problems = replay_run(run_id, store=store, raw_store=RawStore(settings.RAW_DIR))
        return "PASS" if ok else f"FAIL ({len(problems)})"
    except Exception as exc:  # noqa: BLE001 - a view must never be the thing that breaks
        return f"unavailable: {type(exc).__name__}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.report",
        description="Write a self-contained dashboard for what the collector has stored.",
    )
    parser.add_argument("--out", type=Path, default=settings.DATA_DIR / "dashboard.html")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUN_LIMIT,
                        help="how many runs to list")
    parser.add_argument("--quote-runs", type=int, default=DEFAULT_QUOTE_RUNS,
                        help="how many recent runs to embed price rows for")
    parser.add_argument("--fragment", action="store_true",
                        help="write body-only markup instead of a whole document")
    parser.add_argument("--no-replay-check", action="store_true",
                        help="skip re-parsing the latest run's stored bytes")
    parser.add_argument("--open", action="store_true", help="open the file when it is written")
    parser.add_argument("--serve", type=int, metavar="PORT",
                        help="serve the report on localhost instead of only writing it")
    args = parser.parse_args(argv)

    try:
        store = Store(settings.DB_PATH)
    except IncompatibleDatabase as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"cannot open {settings.DB_PATH}: {exc}", file=sys.stderr)
        return 1

    with store:
        latest = store.latest_run_id()
        note = "not checked"
        if latest is not None and not args.no_replay_check:
            note = _replay_note(store, latest)
        try:
            data = build_report(
                store, run_limit=args.runs, quote_runs=args.quote_runs, replay_note=note
            )
        except LookupError as exc:
            print(f"{exc}\nrun `python -m src.collector collect` first", file=sys.stderr)
            return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    markup = render_fragment(data) if args.fragment else render_page(data)
    args.out.write_text(markup, encoding="utf-8")

    rows = len(data["quotes"]["rows"])
    latest_sports = data["runs"][0]["sports"]
    usable = [entry["sport"] for entry in latest_sports if entry["comparable"]]
    single = [entry["sport"] for entry in latest_sports if not entry["comparable"]]
    print(
        f"wrote {args.out} ({len(markup) / 1024:.0f} KB): "
        f"{data['meta']['runs_recorded']} runs listed, "
        f"{rows:,} price rows from {data['meta']['runs_with_rows']} run(s), "
        f"replay {data['meta']['replay_note']}"
    )
    print(
        f"  latest run: {len(usable)} comparable sport(s) — {MIN_BOOKS_FOR_COMPARISON}+ books "
        f"on a shared fixture ({', '.join(usable) or 'none'})"
        + (f"; not comparable: {', '.join(single)}" if single else "")
    )

    if args.serve:
        return _serve(args.out, args.serve, open_browser=args.open)
    if args.open:
        webbrowser.open(args.out.resolve().as_uri())
    return 0


def _serve(path: Path, port: int, *, open_browser: bool) -> int:
    """Serve the report's directory on localhost, for browsers that dislike file://."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(path.parent.resolve()))
    url = f"http://127.0.0.1:{port}/{path.name}"
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {url} — ctrl-c to stop")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
