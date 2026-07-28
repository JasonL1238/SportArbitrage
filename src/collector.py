"""The collection pipeline and its command line.

One run does the same five things for every source, in order: fetch, persist the
raw bytes, parse those bytes, validate the result, persist. Nothing downstream
ever sees data that skipped a step, and nothing is reported as collected unless
it survived validation.

A source that fails does not abort the run — the other sources still collect and
the failure is recorded as unhealthy, because unattended operation is a
requirement. A run is only ``ok`` when validation passes *and* at least two
sportsbooks actually produced data.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from src import settings
from src.arb import ArbReport, best_prices, find_opportunities
from src.events import reconcile_event_keys
from src.raw_store import RawResponse, RawStore
from src.schema import BaseballQuote
from src.sources.base import BaseballSource, ParseOutcome, SourceHealth
from src.sources.betrivers_kambi import BetRiversKambiAdapter
from src.sources.fanduel import FanDuelAdapter
from src.sources.guards import SourceError
from src.sources.pinnacle import PinnacleAdapter
from src.store import Store
from src.validation import Severity, ValidationReport, validate

log = logging.getLogger("collector")

#: Every source is a public endpoint of the sportsbook's own web experience,
#: fetched directly. No third-party odds API, key, account, or paid service.
SOURCE_FACTORIES = {
    "fanduel": FanDuelAdapter,
    "pinnacle": PinnacleAdapter,
    "betrivers_kambi": BetRiversKambiAdapter,
}

#: The goal requires two independent sportsbooks; one is not a pipeline.
MIN_HEALTHY_SOURCES = 2


class RunResult:
    """What one collection run produced."""

    def __init__(
        self,
        run_id: int | None,
        quotes: list[BaseballQuote],
        health: list[SourceHealth],
        report: ValidationReport,
        arb: ArbReport | None = None,
    ) -> None:
        self.run_id = run_id
        self.quotes = quotes
        self.health = health
        self.report = report
        self.arb = arb

    @property
    def ok(self) -> bool:
        return self.report.ok

    def print_summary(self) -> None:
        print(f"\n=== run {self.run_id if self.run_id is not None else '(not stored)'} ===")
        for health in sorted(self.health, key=lambda h: h.source_key):
            print(f"  {health.summary()}")
        print(f"  validation: {self.report.summary()}")
        for finding in self.report.errors[:20]:
            print(f"    ERROR   {finding.code}: {finding.message}")
        for finding in self.report.warnings[:10]:
            print(f"    warning {finding.code}: {finding.message}")
        extra_errors = len(self.report.errors) - 20
        extra_warnings = len(self.report.warnings) - 10
        if extra_errors > 0:
            print(f"    ... {extra_errors} more errors")
        if extra_warnings > 0:
            print(f"    ... {extra_warnings} more warnings")

        if self.arb is not None:
            print(f"  arbitrage: {self.arb.summary()}")
            for opportunity in self.arb.opportunities[:10]:
                print(opportunity.describe())
            # Rejections are printed because "found nothing" and "found
            # something and refused it for a stated reason" are different facts.
            rejected = Counter(d.code for d in self.arb.diagnostics)
            if rejected:
                print(f"    rejected: {dict(rejected)}")


def build_sources(keys: Sequence[str] | None = None) -> list[BaseballSource]:
    selected = list(keys) if keys else list(SOURCE_FACTORIES)
    unknown = [key for key in selected if key not in SOURCE_FACTORIES]
    if unknown:
        raise SystemExit(f"unknown source(s): {unknown}; known: {sorted(SOURCE_FACTORIES)}")
    return [SOURCE_FACTORIES[key](timeout=settings.HTTP_TIMEOUT) for key in selected]


def collect_once(
    sources: Sequence[BaseballSource],
    *,
    raw_store: RawStore,
    store: Store | None,
    as_of: datetime | None = None,
) -> RunResult:
    """Fetch, persist raw, parse, validate, persist — for every source.

    *as_of* is the moment arbitrage is judged against, defaulting to now.  It is
    injectable so a test can pin it rather than depending on the wall clock.
    """
    started_at = datetime.now(UTC)
    run_id = store.start_run(started_at) if store else None

    all_quotes: list[BaseballQuote] = []
    health_reports: list[SourceHealth] = []

    for source in sources:
        health, outcome = _collect_source(source, raw_store=raw_store, store=store, run_id=run_id)
        health_reports.append(health)
        all_quotes.extend(outcome.quotes)
        if store is not None and run_id is not None:
            store.save_health(run_id, health)
            store.save_rejections(run_id, outcome.rejections)
            store.save_skipped(run_id, source.source_key, dict(outcome.skipped))

    # Event identity is settled across all sources at once, before anything is
    # validated or compared. Each adapter can only number doubleheaders over its
    # own slate, which makes "#2" a per-source ordinal rather than an identity;
    # left uncorrected, one book's game 2 joins onto another book's game 1.
    all_quotes, rekeys = reconcile_event_keys(all_quotes)

    report = validate(all_quotes)
    _check_source_count(health_reports, report)
    for rekey in rekeys:
        report.add(
            Severity.WARNING,
            "event_key_reconciled",
            f"corrected a per-source doubleheader ordinal: {rekey}",
            source=rekey.source,
            event_key=rekey.now,
        )

    # Gated on the clock: a live run only cares about games not yet started.
    arb_report = find_opportunities(all_quotes, as_of=as_of or datetime.now(UTC))

    if store is not None and run_id is not None:
        # One unusable run must not end an unattended watch loop, and it must
        # not be recorded as though nothing happened either.
        try:
            store.save_quotes(run_id, all_quotes)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to persist quotes for run %s", run_id)
            report.add(
                Severity.ERROR,
                "quotes_not_persisted",
                f"{type(exc).__name__}: {exc} — the run's rows violate a storage "
                "invariant, most likely two prices for one selection",
            )
        store.save_findings(run_id, report.findings)
        store.finish_run(run_id, finished_at=datetime.now(UTC), report=report)

    return RunResult(run_id, all_quotes, health_reports, report, arb_report)


def _collect_source(
    source: BaseballSource,
    *,
    raw_store: RawStore,
    store: Store | None,
    run_id: int | None,
) -> tuple[SourceHealth, ParseOutcome]:
    checked_at = datetime.now(UTC)
    started = time.perf_counter()
    raws: list[RawResponse] = []

    try:
        raws = source.fetch_raw()
        latency_ms = (time.perf_counter() - started) * 1000
    except SourceError as exc:
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_kind=getattr(exc, "kind", "source_error"),
                error_message=str(exc),
            ),
            ParseOutcome(),
        )
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
        log.exception("unexpected failure fetching %s", source.source_key)
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_kind="unexpected",
                error_message=f"{type(exc).__name__}: {exc}",
            ),
            ParseOutcome(),
        )

    # Raw bytes land on disk before anything interprets them.
    unchanged_payloads = 0
    for raw in raws:
        path = raw_store.write(raw)
        unchanged = False
        if store is not None and run_id is not None:
            previous = store.previous_sha(raw.source, raw.endpoint, before_run_id=run_id)
            unchanged = previous is not None and previous == raw.sha256
            store.record_raw(run_id, raw, path, unchanged=unchanged)
        unchanged_payloads += int(unchanged)

    try:
        outcome = source.parse(raws)
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected failure parsing %s", source.source_key)
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                request_count=len(raws),
                raw_bytes=sum(raw.byte_size for raw in raws),
                latency_ms=latency_ms,
                unchanged_payloads=unchanged_payloads,
                error_kind=getattr(exc, "kind", "parse_error"),
                error_message=f"{type(exc).__name__}: {exc}",
            ),
            ParseOutcome(),
        )

    # A rejection means the source offered in-scope data this parser could not
    # represent faithfully, so it counts against health — but it must always
    # come with an explanation.
    error_kind: str | None = None
    error_message: str | None = None
    if not outcome.quotes:
        error_kind = "empty_after_parse"
        error_message = (
            f"parsed 0 quotes from {len(raws)} responses; skipped={dict(outcome.skipped)}"
        )
    elif outcome.rejections:
        reasons = Counter(rejection.reason for rejection in outcome.rejections)
        error_kind = "rejections"
        error_message = (
            f"{len(outcome.rejections)} record(s) rejected: {dict(reasons)}; "
            f"first: {outcome.rejections[0].detail}"
        )

    return (
        SourceHealth(
            source_key=source.source_key,
            ok=bool(outcome.quotes) and not outcome.rejections,
            checked_at=checked_at,
            request_count=len(raws),
            raw_bytes=sum(raw.byte_size for raw in raws),
            latency_ms=latency_ms,
            quote_count=len(outcome.quotes),
            event_count=len(outcome.event_keys),
            rejection_count=len(outcome.rejections),
            skipped_count=sum(outcome.skipped.values()),
            unchanged_payloads=unchanged_payloads,
            error_kind=error_kind,
            error_message=error_message,
        ),
        outcome,
    )


def _check_source_count(health: Sequence[SourceHealth], report: ValidationReport) -> None:
    producing = [h.source_key for h in health if h.quote_count > 0]
    if len(producing) < MIN_HEALTHY_SOURCES:
        report.add(
            Severity.ERROR,
            "insufficient_sources",
            f"only {len(producing)} source(s) produced data ({producing or 'none'}); "
            f"at least {MIN_HEALTHY_SOURCES} are required",
        )


# ── replay ───────────────────────────────────────────────────────────────────


def replay_run(run_id: int, *, store: Store, raw_store: RawStore) -> tuple[bool, list[str]]:
    """Re-parse a stored run's raw responses and compare to what was stored.

    This is the offline reproduction guarantee: if parsing is not a pure
    function of the captured bytes, this fails.
    """
    problems: list[str] = []
    stored = store.load_quotes(run_id)
    by_source_paths: dict[str, list[Path]] = {}
    for source_key, path in store.raw_paths(run_id):
        by_source_paths.setdefault(source_key, []).append(path)

    if not by_source_paths:
        return False, [f"run {run_id} has no stored raw responses"]

    replayed: list[BaseballQuote] = []
    for source_key, paths in by_source_paths.items():
        factory = SOURCE_FACTORIES.get(source_key)
        if factory is None:
            problems.append(f"{source_key}: no adapter available to replay this source")
            continue
        source = factory()
        try:
            raws = [raw_store.read(path) for path in paths]
            replayed.extend(source.parse(raws).quotes)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{source_key}: replay raised {type(exc).__name__}: {exc}")
        finally:
            source.close()

    stored_map = {q.dedup_key: q for q in stored}
    replay_map = {q.dedup_key: q for q in replayed}

    if len(stored) != len(replayed):
        problems.append(f"row count differs: stored {len(stored)}, replayed {len(replayed)}")

    missing = set(stored_map) - set(replay_map)
    added = set(replay_map) - set(stored_map)
    for key in sorted(missing)[:5]:
        problems.append(f"replay lost row: {key}")
    for key in sorted(added)[:5]:
        problems.append(f"replay invented row: {key}")

    for key in sorted(set(stored_map) & set(replay_map)):
        before, after = stored_map[key], replay_map[key]
        if abs(before.decimal_odds - after.decimal_odds) > 1e-9:
            problems.append(
                f"odds changed on replay for {key}: {before.decimal_odds} -> {after.decimal_odds}"
            )
        if before.observed_at != after.observed_at:
            problems.append(f"observed_at changed on replay for {key}")
        if len(problems) > 20:
            break

    return not problems, problems


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cmd_collect(args: argparse.Namespace) -> int:
    raw_store = RawStore(settings.RAW_DIR)
    sources = build_sources(args.source)
    store = None if args.no_store else Store(settings.DB_PATH)
    exit_code = 0
    try:
        iteration = 0
        while True:
            iteration += 1
            result = collect_once(sources, raw_store=raw_store, store=store)
            result.print_summary()
            exit_code = 0 if result.ok else 1
            if not args.watch:
                break
            if args.max_runs and iteration >= args.max_runs:
                break
            log.info("sleeping %ss before next run", args.interval)
            time.sleep(args.interval)
    finally:
        for source in sources:
            source.close()
        if store is not None:
            store.close()
    return exit_code


def _cmd_replay(args: argparse.Namespace) -> int:
    raw_store = RawStore(settings.RAW_DIR)
    with Store(settings.DB_PATH) as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs to replay")
            return 1
        ok, problems = replay_run(run_id, store=store, raw_store=raw_store)
        print(f"replay of run {run_id}: {'PASS' if ok else 'FAIL'}")
        for problem in problems[:25]:
            print(f"  {problem}")
        return 0 if ok else 1


def _cmd_runs(args: argparse.Namespace) -> int:
    with Store(settings.DB_PATH) as store:
        rows = store.run_summaries(limit=args.limit)
        if not rows:
            print("no runs recorded")
            return 1
        print(f"{'run':>4}  {'started':<26} {'ok':<3} {'quotes':>7} {'events':>7} {'err':>4} {'warn':>5}")
        for row in rows:
            print(
                f"{row['id']:>4}  {row['started_at']:<26} {str(bool(row['ok'])):<5}"
                f"{row['quote_count']:>7} {row['event_count']:>7}"
                f"{row['error_count']:>4} {row['warning_count']:>5}"
            )
            for health in store.health_for_run(row["id"]):
                status = "ok" if health["ok"] else f"FAILED[{health['error_kind']}]"
                print(
                    f"        {health['source_key']:<16} {status:<24}"
                    f"{health['quote_count']:>6} quotes {health['event_count']:>4} events"
                )
        return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with Store(settings.DB_PATH) as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        rows = store.query(
            """SELECT source, event_key, home_team, away_team, commence_time, market, period,
                      side, selection, line, decimal_odds, american_odds, status, observed_at
                 FROM quote WHERE run_id = ?
                ORDER BY event_key, market, period, side, line, selection
                LIMIT ?""",
            (run_id, args.limit),
        )
        print(f"run {run_id}: showing {len(rows)} rows")
        for row in rows:
            line = "" if row["line"] is None else f" {row['line']:+g}"
            side = f" {row['side']}" if row["side"] else ""
            print(
                f"  {row['source']:<16} {row['event_key']:<22} {row['market']:<16}"
                f"{row['period']:<16}{side:<5} {row['selection']:<6}{line:<7}"
                f" {row['decimal_odds']:>7.3f} {row['american_odds']:>+5d}  {row['status']}"
            )
        return 0


def _cmd_arb(args: argparse.Namespace) -> int:
    """Re-run arbitrage detection over a stored run, without refetching."""
    with Store(settings.DB_PATH) as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        quotes, _ = reconcile_event_keys(store.load_quotes(run_id))
        report = find_opportunities(
            quotes, total_stake=args.stake, min_margin=args.min_margin / 100.0
        )
        print(f"run {run_id}: {report.summary()}")
        for opportunity in report.opportunities:
            print(opportunity.describe())
        rejected = Counter(d.code for d in report.diagnostics)
        if rejected:
            print(f"\nrejected: {dict(rejected)}")
            if args.verbose:
                for diagnostic in report.diagnostics:
                    print(f"  [{diagnostic.code}] {diagnostic.event_key} "
                          f"{diagnostic.market.value}/{diagnostic.period.value}: {diagnostic.detail}")
        return 0


def _cmd_lines(args: argparse.Namespace) -> int:
    """Best available price per selection, across books — the line-shopping view.

    This is the surface arbitrage is drawn from, so printing it is how you tell a
    genuine "no edge today" apart from a market nobody is actually comparing.
    """
    with Store(settings.DB_PATH) as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        quotes, _ = reconcile_event_keys(store.load_quotes(run_id))
        surface = best_prices(quotes)

        shown = 0
        for key in sorted(surface, key=str):
            selections = surface[key]
            sources = {quote.source for quote in selections.values()}
            if args.cross_book_only and len(sources) < 2:
                continue
            event_key, market, period, side, line = key
            label = f"{market.value}/{period.value}" + (f"/{side.value}" if side else "")
            line_label = "" if line is None else f" @ {line:+g}"
            overround = sum(1.0 / q.decimal_odds for q in selections.values())
            print(f"{event_key} {label}{line_label}  (sum {overround:.4f})")
            for selection, quote in sorted(selections.items(), key=lambda i: i[0].value):
                print(
                    f"    {selection.value:<6} {quote.decimal_odds:>7.3f} "
                    f"{quote.american_odds:>+6d}  {quote.source}"
                )
            shown += 1
            if shown >= args.limit:
                break
        print(f"\n{shown} market(s) shown of {len(surface)}")
        return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Per-source health across recent runs, so a slow decay is visible."""
    with Store(settings.DB_PATH) as store:
        rows = store.run_summaries(limit=args.limit)
        if not rows:
            print("no runs recorded")
            return 1
        per_source: dict[str, list[bool]] = {}
        for row in rows:
            for health in store.health_for_run(row["id"]):
                per_source.setdefault(health["source_key"], []).append(bool(health["ok"]))

        print(f"{'source':<18} {'runs':>5} {'ok':>5} {'rate':>6}  last")
        worst_ok = True
        for source_key, outcomes in sorted(per_source.items()):
            ok_count = sum(outcomes)
            rate = ok_count / len(outcomes)
            recent = "".join("." if ok else "X" for ok in outcomes)
            print(f"{source_key:<18} {len(outcomes):>5} {ok_count:>5} {rate * 100:>5.0f}%  {recent}")
            if rate < args.min_rate:
                worst_ok = False
        latest = rows[0]
        print(
            f"\nlatest run {latest['id']} at {latest['started_at']}: "
            f"{'ok' if latest['ok'] else 'FAILED'}, {latest['quote_count']} quotes, "
            f"{latest['error_count']} errors, {latest['warning_count']} warnings"
        )
        if not worst_ok:
            print(f"a source is below the {args.min_rate * 100:.0f}% success threshold")
        return 0 if worst_ok and latest["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="python -m src.collector",
        description="Collect and normalize real MLB betting data from public sportsbook endpoints.",
    )
    subparsers = parser.add_subparsers(dest="command")

    collect = subparsers.add_parser("collect", help="run one collection pass (default)")
    collect.add_argument(
        "--source", action="append", choices=sorted(SOURCE_FACTORIES), help="repeatable"
    )
    collect.add_argument("--watch", action="store_true", help="collect repeatedly")
    collect.add_argument("--interval", type=int, default=settings.DEFAULT_INTERVAL_SECONDS)
    collect.add_argument("--max-runs", type=int, default=0, help="0 means unlimited")
    collect.add_argument("--no-store", action="store_true", help="skip the database, still store raw")
    collect.set_defaults(func=_cmd_collect)

    replay = subparsers.add_parser("replay", help="re-parse a stored run's raw responses")
    replay.add_argument("--run", type=int, help="run id; defaults to the most recent")
    replay.set_defaults(func=_cmd_replay)

    runs = subparsers.add_parser("runs", help="list recent runs and per-source health")
    runs.add_argument("--limit", type=int, default=10)
    runs.set_defaults(func=_cmd_runs)

    show = subparsers.add_parser("show", help="print normalized rows from a run")
    show.add_argument("--run", type=int)
    show.add_argument("--limit", type=int, default=40)
    show.set_defaults(func=_cmd_show)

    arb = subparsers.add_parser("arb", help="find arbitrage in a stored run")
    arb.add_argument("--run", type=int, help="run id; defaults to the most recent")
    arb.add_argument("--stake", type=float, default=100.0, help="bankroll per position")
    arb.add_argument(
        "--min-margin", type=float, default=0.0, help="minimum edge to report, in percent"
    )
    arb.add_argument("--verbose", action="store_true", help="explain every rejected market")
    arb.set_defaults(func=_cmd_arb)

    lines = subparsers.add_parser("lines", help="best price per market across books")
    lines.add_argument("--run", type=int, help="run id; defaults to the most recent")
    lines.add_argument("--limit", type=int, default=20)
    lines.add_argument(
        "--cross-book-only",
        action="store_true",
        help="only markets priced by more than one book",
    )
    lines.set_defaults(func=_cmd_lines)

    health = subparsers.add_parser("health", help="per-source success rate across recent runs")
    health.add_argument("--limit", type=int, default=20, help="how many runs to look back over")
    health.add_argument("--min-rate", type=float, default=0.8, help="failure threshold, 0-1")
    health.set_defaults(func=_cmd_health)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["collect", *(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
