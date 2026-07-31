"""Collect promotions / bonus offers from every registered sportsbook.

Sibling pipeline to :mod:`src.collector`.  Does not produce Quotes and does not
feed arbitrage — it surfaces free-EV situations (signup bonuses, boosts, no-sweat
bets, referrals) from the same books the odds slate covers.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Mapping, Sequence

from src import settings
from src.promos import registry
from src.promos.base import PromoParseOutcome, PromoSource, PromoSourceHealth
from src.promos.redundancy import brand_coverage, prefer_primary_offers
from src.promos.schema import PromoOffer
from src.promos.store import PromoStore
from src.raw_store import RawStore
from src.sources.guards import SourceError

log = logging.getLogger("promos")

PROMO_FACTORIES: dict[str, Callable[..., PromoSource]] = {
    entry.key: entry.factory() for entry in registry.PROMO_SOURCES
}

#: Skips that mean "the venue answered with a structured empty catalog" — a
#: real, useful fact — rather than "we got HTML chrome and invented nothing".
_EMPTY_CATALOG_SKIPS = frozenset(
    {
        "empty_merchandising",
        "non_sports_product",
        "casino_other",
        "landing_redundant",
        "no_public_catalog",
    }
)


@dataclass
class PromoRunResult:
    run_id: int | None
    started_at: datetime
    finished_at: datetime
    offers: list[PromoOffer] = field(default_factory=list)
    health: list[PromoSourceHealth] = field(default_factory=list)
    ok: bool = False

    def summary_lines(self) -> list[str]:
        ok_brands, total_brands, _ = brand_coverage(self.health)
        lines = [
            f"promo run {'OK' if self.ok else 'DEGRADED'} — "
            f"{len(self.offers)} offers from "
            f"{sum(1 for h in self.health if h.ok)}/"
            f"{len(self.health)} sources "
            f"({ok_brands}/{total_brands} brands covered)"
        ]
        for item in self.health:
            lines.append("  " + item.summary())
        by_source: dict[str, int] = {}
        for offer in self.offers:
            by_source[offer.source] = by_source.get(offer.source, 0) + 1
        if by_source:
            lines.append(
                "  by source: "
                + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
            )
        return lines


def build_sources(
    keys: Sequence[str] | None = None,
    *,
    timeout: float | None = None,
) -> list[PromoSource]:
    selected = list(keys) if keys else list(registry.keys())
    unknown = [key for key in selected if key not in PROMO_FACTORIES]
    if unknown:
        raise KeyError(f"unknown promo source(s): {unknown}; known: {sorted(PROMO_FACTORIES)}")
    timeout = settings.HTTP_TIMEOUT if timeout is None else timeout
    sources: list[PromoSource] = []
    for key in selected:
        factory = PROMO_FACTORIES[key]
        try:
            sources.append(factory(timeout=timeout))
        except TypeError:
            sources.append(factory())
    return sources


def dedupe_offers(offers: Sequence[PromoOffer]) -> list[PromoOffer]:
    """Keep the first row per ``(source, offer_id)``."""
    seen: set[tuple[str, str]] = set()
    out: list[PromoOffer] = []
    for offer in offers:
        key = offer.dedup_key
        if key in seen:
            continue
        seen.add(key)
        out.append(offer)
    return out


def _health_ok(outcome: PromoParseOutcome) -> tuple[bool, str | None, str | None]:
    """Decide whether a successful fetch+parse is actually healthy.

    Usable offers win even if another surface on the same source rejected.
    Structured empty catalogs (FanDuel merchandising ``[]``) are OK only when
    no fallback surface also reported that it found nothing.
    """
    if outcome.offers:
        return True, None, None
    if outcome.skipped.get("no_promo_signals"):
        return False, "empty_after_parse", "page had no promo signals"
    if outcome.skipped.get("landing_without_promo_copy"):
        return False, "empty_after_parse", "marketing page had no promo copy"
    if outcome.rejections:
        reason = outcome.rejections[0].reason
        if reason in {"cookie_wall", "captcha", "bot_wall"}:
            return False, reason, outcome.rejections[0].detail
        return False, "parse_rejections", outcome.rejections[0].detail
    skip_keys = set(outcome.skipped)
    if skip_keys & _EMPTY_CATALOG_SKIPS:
        return True, None, None
    if outcome.skipped.get("no_brand_offers"):
        return False, "empty_after_parse", "TheLines page had no offers for this brand"
    return False, "empty_after_parse", "no offers parsed"


def _collect_source(
    source: PromoSource,
    *,
    raw_store: RawStore | None,
) -> tuple[PromoParseOutcome, PromoSourceHealth, list]:
    started = time.perf_counter()
    checked_at = datetime.now(UTC)
    try:
        raws = source.fetch_raw()
    except SourceError as exc:
        raws = []
        if getattr(exc, "raw", None) is not None:
            raws = [exc.raw]
        if raw_store is not None:
            for raw in raws:
                try:
                    raw_store.write(raw)
                except Exception:  # noqa: BLE001
                    log.exception("raw write failed for %s failure capture", source.source_key)
        health = PromoSourceHealth(
            source_key=source.source_key,
            ok=False,
            checked_at=checked_at,
            request_count=len(raws),
            raw_bytes=sum(len(r.body.encode("utf-8")) for r in raws),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error_kind=exc.kind,
            error_message=str(exc),
        )
        return PromoParseOutcome(), health, raws
    except Exception as exc:  # noqa: BLE001 — isolate one bad adapter
        log.exception("promo fetch failed for %s", source.source_key)
        health = PromoSourceHealth(
            source_key=source.source_key,
            ok=False,
            checked_at=checked_at,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error_kind="fetch_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        return PromoParseOutcome(), health, []

    if raw_store is not None:
        for raw in raws:
            try:
                raw_store.write(raw)
            except Exception:  # noqa: BLE001
                log.exception("raw write failed for %s", source.source_key)

    try:
        outcome = source.parse(raws)
    except Exception as exc:  # noqa: BLE001 — isolate one bad parser
        log.exception("promo parse failed for %s", source.source_key)
        health = PromoSourceHealth(
            source_key=source.source_key,
            ok=False,
            checked_at=checked_at,
            request_count=len(raws),
            raw_bytes=sum(len(r.body.encode("utf-8")) for r in raws),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error_kind="parse_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        return PromoParseOutcome(), health, raws

    outcome.offers = dedupe_offers(outcome.offers)
    ok, error_kind, error_message = _health_ok(outcome)
    health = PromoSourceHealth(
        source_key=source.source_key,
        ok=ok,
        checked_at=checked_at,
        request_count=len(raws),
        raw_bytes=sum(len(r.body.encode("utf-8")) for r in raws),
        latency_ms=(time.perf_counter() - started) * 1000.0,
        offer_count=len(outcome.offers),
        rejection_count=len(outcome.rejections),
        skipped_count=sum(outcome.skipped.values()),
        error_kind=error_kind,
        error_message=error_message,
    )
    return outcome, health, raws


def collect_promos_once(
    *,
    sources: Sequence[str] | None = None,
    store: bool = True,
    persist_raw: bool = True,
    on_progress: Callable[[Mapping[str, object]], None] | None = None,
) -> PromoRunResult:
    started_at = datetime.now(UTC)
    built = build_sources(sources)
    raw_store = RawStore(settings.PROMO_RAW_DIR) if persist_raw else None
    promo_store = PromoStore(settings.PROMO_DB_PATH) if store else None
    run_id = promo_store.start_run() if promo_store is not None else None

    offers: list[PromoOffer] = []
    health_rows: list[PromoSourceHealth] = []
    finished_at = started_at
    ok = False
    total = len(built)

    def _progress(payload: dict[str, object]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception:  # noqa: BLE001 — UI callbacks must not abort collect
            log.debug("promo progress callback failed", exc_info=True)

    try:
        _progress(
            {
                "phase": "starting",
                "message": f"Starting promo scrape of {total} book(s)",
                "done": 0,
                "total": total,
                "offer_count": 0,
                "kind": "promos",
            }
        )
        for index, source in enumerate(built):
            _progress(
                {
                    "phase": "fetching",
                    "message": f"Fetching {source.source_key}…",
                    "done": index,
                    "total": total,
                    "offer_count": len(offers),
                    "source": source.source_key,
                    "kind": "promos",
                }
            )
            try:
                outcome, health, _ = _collect_source(source, raw_store=raw_store)
                health_rows.append(health)
                offers.extend(outcome.offers)
                log.info("%s", health.summary())
            except Exception as exc:  # noqa: BLE001 — never abort the slate
                log.exception("unexpected promo failure for %s", source.source_key)
                health_rows.append(
                    PromoSourceHealth(
                        source_key=source.source_key,
                        ok=False,
                        checked_at=datetime.now(UTC),
                        error_kind="unexpected",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    log.debug("close failed for %s", source.source_key, exc_info=True)
            _progress(
                {
                    "phase": "fetched",
                    "message": f"{source.source_key}: {health_rows[-1].offer_count} offer(s)",
                    "done": index + 1,
                    "total": total,
                    "offer_count": len(offers),
                    "source": source.source_key,
                    "kind": "promos",
                }
            )

        offers = prefer_primary_offers(dedupe_offers(offers))
        ok_brands, total_brands, _ = brand_coverage(health_rows)
        # Run health folds TheLines secondaries into brand coverage so a dead
        # first-party landing does not fail the slate when tl_* covered the book.
        ok = bool(health_rows) and (
            ok_brands >= max(1, total_brands // 2)
            or sum(1 for h in health_rows if h.ok) >= max(1, len(health_rows) // 2)
        )
        finished_at = datetime.now(UTC)
        if promo_store is not None and run_id is not None:
            try:
                promo_store.finish_run(run_id, ok=ok, offers=offers, health=health_rows)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist promo run %s", run_id)
                ok = False
    finally:
        if promo_store is not None:
            promo_store.close()

    return PromoRunResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        offers=offers,
        health=health_rows,
        ok=ok,
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    result = collect_promos_once(
        sources=args.source,
        store=not args.no_store,
        persist_raw=not args.no_raw,
    )
    for line in result.summary_lines():
        print(line)
    if args.verbose:
        for offer in result.offers:
            end = offer.ends_at.isoformat() if offer.ends_at else "-"
            print(
                f"  [{offer.source}] {offer.kind.value:14} {offer.title[:70]}"
                f"  ends={end}"
            )
    return 0 if result.ok else 1


def _cmd_show(args: argparse.Namespace) -> int:
    store = PromoStore(settings.PROMO_DB_PATH)
    try:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no promo runs stored yet", file=sys.stderr)
            return 1
        rows = store.offers_for_run(run_id)
        print(f"run {run_id}: {len(rows)} offers")
        for row in rows[: args.limit]:
            print(
                f"  {row['source']:16} {row['kind']:14} {row['title'][:72]}"
            )
    finally:
        store.close()
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    store = PromoStore(settings.PROMO_DB_PATH)
    try:
        for row in store.list_runs(limit=args.limit):
            status = "OK" if row["ok"] else "FAIL"
            print(
                f"#{row['id']} {status} offers={row['offer_count']} "
                f"sources={row['source_count']} started={row['started_at']}"
            )
    finally:
        store.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    refusal = settings.refuse_bad_settings()
    if refusal is not None:
        return refusal
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="python -m src.promos",
        description=(
            "Scrape sportsbook promotions, signup bonuses, boosts, and other "
            "public free-EV offers for every large book on the odds slate."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    collect = sub.add_parser("collect", help="fetch and normalize promo catalogs")
    collect.add_argument(
        "--source",
        action="append",
        choices=sorted(PROMO_FACTORIES),
        help="repeatable; default is every registered promo source",
    )
    collect.add_argument("--no-store", action="store_true")
    collect.add_argument("--no-raw", action="store_true")
    collect.add_argument("--verbose", "-v", action="store_true")
    collect.set_defaults(func=_cmd_collect)

    show = sub.add_parser("show", help="print offers from a stored promo run")
    show.add_argument("--run", type=int)
    show.add_argument("--limit", type=int, default=50)
    show.set_defaults(func=_cmd_show)

    runs = sub.add_parser("runs", help="list recent promo runs")
    runs.add_argument("--limit", type=int, default=10)
    runs.set_defaults(func=_cmd_runs)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["collect", *(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
