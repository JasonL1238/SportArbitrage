"""Build a local dashboard from what the collector has already stored.

This reads the SQLite store and writes one self-contained HTML file.  It is a
*view*, never a second source of truth: every number on the page comes from a
query in this module, and the module never fetches anything.  That keeps the
report honest — if the page shows 1,656 rows it is because 1,656 rows are in the
database, and generating the report cannot alter what was collected.

The page opens from ``file://`` with no network access: no CDN, no webfont, no
remote image, no telemetry.

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
from src.report_assets import BODY, CSS, JS
from src.schema import Market, Period, QuoteStatus, Selection, Side
from src.store import Store

#: How many recent runs to embed quote rows for.  Bounded because the page holds
#: every row in memory; the run list itself always covers more runs than this.
DEFAULT_QUOTE_RUNS = 6
DEFAULT_RUN_LIMIT = 40

#: What each source is, in the reader's terms rather than the parser's.
SOURCE_NOTES: dict[str, dict[str, str]] = {
    "fanduel": {
        "label": "FanDuel",
        "host": "sbapi.il.sportsbook.fanduel.com",
        "what": "Read from the feed behind FanDuel's own MLB page. Whole-game bets only — "
                "who wins, the handicap, and total runs — but it does publish extra "
                "numbers beyond its main line.",
    },
    "pinnacle": {
        "label": "Pinnacle",
        "host": "guest.api.arcadia.pinnacle.com",
        "what": "The most detailed of the three: whole game, first 5 innings and first "
                "inning, and the only one that tells you the largest bet it will accept.",
    },
    "betrivers_kambi": {
        "label": "BetRivers",
        "host": "eu-offering-api.kambicdn.com",
        "what": "Lists the most games, and labels which part of the game each bet covers "
                "in words. That is what let Pinnacle's unlabelled numbering be verified.",
    },
}

#: Why an offer the books do publish is deliberately not collected.  Matched by
#: prefix, longest first, so a specific reason beats the family it belongs to.
SKIP_NOTES: list[tuple[str, str]] = [
    ("matchup_type:special",
     "Bets on individual players, or on the exact final score. Not a bet on the game "
     "itself, so out of scope."),
    ("criterion:Total Runs Odd/Even",
     "Whether the total is an odd or even number. There is no over/under and no "
     "number, so it is not a runs total."),
    ("criterion:Draw No Bet",
     "Your stake comes back if the scores are level. That makes it a different bet "
     "from picking a winner, so it is not filed as one."),
    ("criterion:Lead After 5 Innings",
     "Almost certainly the same thing as who is ahead after 5 innings — but it is "
     "worded differently, and guessing wrong would put a false price in the data."),
    ("criterion:Match Odds (",
     "Cancelled unless a named pitcher actually starts. That condition makes it a "
     "different bet from the plain one."),
    ("criterion:Total Runs - First",
     "Covers a stretch of innings this tool does not track, and no second book "
     "labels it, so there is no way to confirm what it settles on."),
    ("criterion:",
     "A bet on a player or on a one-off occurrence, rather than on one of the four "
     "kinds of game bet collected here."),
    ("non_game_event",
     "A season-long bet — division winners, awards — rather than a single game."),
]

#: Field-by-field reference, shown so "one consistent schema" is inspectable.
SCHEMA_FIELDS: list[dict[str, Any]] = [
    ("source", "str", True, "Which sportsbook this price came from."),
    ("observed_at", "datetime", True, "When this collector fetched the payload. Never a book's own clock."),
    ("raw_ref", "str", True, "Points at the stored raw response this row was parsed from."),
    ("sport", "str", True, "Always baseball in this pipeline."),
    ("league", "str", True, "Always MLB in this pipeline."),
    ("event_key", "str", True, "Shared game identity: AWAY@HOME:date on the US/Eastern scheduling date."),
    ("source_event_id", "str", True, "The book's own id for the game."),
    ("home_team", "str", True, "Home club as the book spells it."),
    ("away_team", "str", True, "Away club as the book spells it."),
    ("commence_time", "datetime", True, "First pitch, in UTC."),
    ("market", "enum", True, "One of four: moneyline, run line, total runs, team total runs."),
    ("period", "enum", True, "Full game, first five innings, or first inning."),
    ("selection", "enum", True, "home, away, draw, over or under."),
    ("side", "enum", False, "Which team a team total refers to. Set only for team totals."),
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
        "term": "Who wins",
        "plain": "The simplest bet: pick the winning team, no adjustments. Books call it "
                 "the moneyline.",
    },
    {
        "term": "Winner with a handicap",
        "plain": "One team is given a head start in runs, to even up a mismatch. \"Reds "
                 "−1.5\" wins only if the Reds win by 2 or more. \"Reds +1.5\" wins if "
                 "they win, or lose by exactly 1. Books call it the run line.",
    },
    {
        "term": "Total runs",
        "plain": "Ignore who wins. Bet on whether both teams combined score more (over) "
                 "or fewer (under) than a set number.",
    },
    {
        "term": "One team's runs",
        "plain": "The same over/under idea applied to a single team's score. Books call "
                 "it a team total.",
    },
    {
        "term": "Part of the game",
        "plain": "Most bets settle on the whole game. Books also price just the first 5 "
                 "innings, or just the first inning. Those can end level, so they have a "
                 "third outcome: a tie.",
    },
    {
        "term": "The number (the line)",
        "plain": "The runs figure a bet hinges on — the handicap, or the over/under "
                 "total. Half numbers like 8.5 exist so there is no tie.",
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
        "term": "Game ID",
        "plain": "This tool's own name for a game, like CLE@CIN:2026-07-28 — the away "
                 "team, the home team, and the date. Each book uses its own private ID, "
                 "so this is what lets their prices be compared.",
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
    "run_id", "source", "event_key", "source_event_id", "home_team", "away_team",
    "commence_time", "market", "period", "selection", "side", "line", "is_alternate",
    "decimal_odds", "american_odds", "implied_probability", "source_market_id",
    "limit_amount", "status", "last_change_at",
]

_INTERNED = frozenset({
    "source", "event_key", "source_event_id", "home_team", "away_team", "commence_time",
    "market", "period", "selection", "side", "source_market_id", "status", "last_change_at",
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
                  r.error_count, r.warning_count,
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
                "total_latency_ms": sum(h["latency_ms"] or 0 for h in health),
                "sources": sorted(health, key=lambda h: h["key"]),
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
        },
        "sources": [
            dict(key=key, **SOURCE_NOTES.get(key, _unknown_source(key)))
            for key in sorted({h["key"] for run in runs for h in run["sources"]})
        ],
        "runs": runs,
        "quotes": quotes,
        "team_names": _team_names(quotes, strings),
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
            {"name": "market", "values": [m.value for m in Market]},
            {"name": "period", "values": [p.value for p in Period]},
            {"name": "selection", "values": [s.value for s in Selection]},
            {"name": "side", "values": [s.value for s in Side]},
            {"name": "status", "values": [s.value for s in QuoteStatus]},
        ],
        "strings": strings,
    }


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


def _team_names(quotes: dict[str, Any], strings: Sequence[str]) -> dict[str, dict[str, str]]:
    """Map each spelling the books use onto a short name the page can say.

    "Reds win by 2 or more" is a sentence; "home -1.5" is a notation.  The mapping
    goes through the same canonicalizer the pipeline uses, so the page cannot invent
    a club the validator would have rejected.
    """
    from src.teams import canonical_team

    raw_indexes = {
        index
        for column in ("home_team", "away_team")
        for index in _column(quotes, column)
        if index is not None
    }
    names: dict[str, dict[str, str]] = {}
    for index in raw_indexes:
        raw = strings[index]
        team = canonical_team(raw)
        if team is not None:
            names[raw] = {"abbr": team.abbr, "nickname": team.nickname, "name": team.name}
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
        return "no games stored"
    return dates[0] if len(dates) == 1 else f"{dates[0]} … {dates[-1]}"


def _lede(latest: dict[str, Any]) -> str:
    names = {"betrivers_kambi": "BetRivers", "fanduel": "FanDuel", "pinnacle": "Pinnacle"}
    books = [names.get(h["key"], h["key"]) for h in latest["sources"] if h["quote_count"] > 0]
    listed = (
        " and ".join(books) if len(books) < 3 else f"{', '.join(books[:-1])} and {books[-1]}"
    )
    return (
        f"Three sportsbooks publish betting prices for today's baseball on their own "
        f"websites. This tool reads them, converts three different formats into one, checks "
        f"the result and saves it. On its most recent pass it collected "
        f"{latest['quote_count']:,} prices across {latest['event_count']} games from "
        f"{listed}. Nothing on this page is fetched live — it is all read back out of what "
        f"was saved."
    )


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
        "<title>MLB Odds Collector — what got grabbed</title>\n"
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
    print(
        f"wrote {args.out} ({len(markup) / 1024:.0f} KB): "
        f"{data['meta']['runs_recorded']} runs listed, "
        f"{rows:,} price rows from {data['meta']['runs_with_rows']} run(s), "
        f"replay {data['meta']['replay_note']}"
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
