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
from uuid import uuid4

from src import settings
from src.promos import registry
from src.promos.base import PromoParseOutcome, PromoSource, PromoSourceHealth
from src.promos.deepen import deepen_by_source
from src.promos.enrich import enrich_offers
from src.promos.redundancy import brand_coverage, prefer_primary_offers
from src.promos.schema import PromoOffer
from src.promos.store import PromoStore
from src.promos.strategy import apply_usage_guidance
from src.jurisdictions import JURISDICTIONS
from src.raw_store import RawResponse, RawStore
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


def offer_confirmed_for_state(offer: PromoOffer, state: str) -> bool:
    """Eligibility fails closed: silence is not permission for a state run."""
    target = state.strip().upper()
    return target in offer.eligible_regions and target not in offer.ineligible_regions


@dataclass
class PromoRunResult:
    run_id: int | None
    started_at: datetime
    finished_at: datetime
    offers: list[PromoOffer] = field(default_factory=list)
    health: list[PromoSourceHealth] = field(default_factory=list)
    ok: bool = False
    filtered_unconfirmed: int = 0

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


@dataclass(frozen=True)
class PromoBatchResult:
    batch_id: str
    detected_state: str
    runs: tuple[tuple[str, PromoRunResult], ...]

    @property
    def ok(self) -> bool:
        return bool(self.runs) and all(result.ok for _, result in self.runs)


class CachedPromoSource:
    """Fetch one generic catalog once and replay its bytes for each state."""

    def __init__(self, source: PromoSource) -> None:
        self._source = source
        self._raws: list[RawResponse] | None = None

    @property
    def source_key(self) -> str:
        return self._source.source_key

    def fetch_raw(self) -> list[RawResponse]:
        if self._raws is None:
            self._raws = list(self._source.fetch_raw())
        return list(self._raws)

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        return self._source.parse(raws)

    def close(self) -> None:
        # Per-state collection closes its inputs; the batch owns this shared
        # wrapper and closes the underlying client after the last state.
        pass

    def close_underlying(self) -> None:
        self._source.close()


def build_sources(
    keys: Sequence[str] | None = None,
    *,
    timeout: float | None = None,
    state: str | None = None,
    route_scope: str = "all",
) -> list[PromoSource]:
    if state is None and route_scope == "all":
        entries = registry.PROMO_SOURCES
    elif route_scope == "global":
        entries = registry.global_promo_sources()
    elif route_scope == "state" and state is not None:
        entries = registry.state_promo_sources_for_state(state)
    else:
        raise ValueError("state is required for state-scoped promo construction")
    factories = {entry.key: entry.factory() for entry in entries}
    selected = [key for key in (keys or factories) if key in factories]
    unknown = [key for key in (keys or ()) if key not in PROMO_FACTORIES]
    if unknown:
        raise KeyError(f"unknown promo source(s): {unknown}; known: {sorted(PROMO_FACTORIES)}")
    timeout = settings.HTTP_TIMEOUT if timeout is None else timeout
    sources: list[PromoSource] = []
    for key in selected:
        factory = factories[key]
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
    Specificity is reassessed after enrich/deepen in
    :func:`_reassess_health_after_enrich`.
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


def _reassess_health_after_enrich(
    health_rows: list[PromoSourceHealth],
    offers: Sequence[PromoOffer],
) -> list[PromoSourceHealth]:
    """Flip vague-only / emptied sources to failed once enrich/deepen has run."""
    by_source: dict[str, list[PromoOffer]] = {}
    for offer in offers:
        by_source.setdefault(offer.source, []).append(offer)
    out: list[PromoSourceHealth] = []
    for row in health_rows:
        source_offers = by_source.get(row.source_key, [])
        if not row.ok:
            out.append(row)
            continue
        if not source_offers:
            # Originally OK with offers that were all folded away as weaker dups.
            if row.offer_count > 0:
                out.append(
                    PromoSourceHealth(
                        source_key=row.source_key,
                        ok=False,
                        checked_at=row.checked_at,
                        request_count=row.request_count,
                        raw_bytes=row.raw_bytes,
                        latency_ms=row.latency_ms,
                        offer_count=0,
                        rejection_count=row.rejection_count,
                        skipped_count=row.skipped_count,
                        error_kind="deduped_empty",
                        error_message="offers removed as weaker duplicates of primary",
                    )
                )
            else:
                out.append(row)
            continue
        if any(o.is_specific for o in source_offers):
            out.append(
                PromoSourceHealth(
                    source_key=row.source_key,
                    ok=True,
                    checked_at=row.checked_at,
                    request_count=row.request_count,
                    raw_bytes=row.raw_bytes,
                    latency_ms=row.latency_ms,
                    offer_count=len(source_offers),
                    rejection_count=row.rejection_count,
                    skipped_count=row.skipped_count,
                )
            )
            continue
        if all(o.requires_login for o in source_offers):
            out.append(row)
            continue
        out.append(
            PromoSourceHealth(
                source_key=row.source_key,
                ok=False,
                checked_at=row.checked_at,
                request_count=row.request_count,
                raw_bytes=row.raw_bytes,
                latency_ms=row.latency_ms,
                offer_count=len(source_offers),
                rejection_count=row.rejection_count,
                skipped_count=row.skipped_count,
                error_kind="vague_offers",
                error_message=f"{len(source_offers)} offer(s) without concrete mechanics",
            )
        )
    return out


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
                except Exception:
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
    except Exception as exc:  # isolate one bad adapter
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
            except Exception:
                log.exception("raw write failed for %s", source.source_key)

    try:
        outcome = source.parse(raws)
    except Exception as exc:  # isolate one bad parser
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
    jurisdiction: str | None = None,
    batch_id: str | None = None,
    built_sources: Sequence[PromoSource] | None = None,
) -> PromoRunResult:
    started_at = datetime.now(UTC)
    run_state = (jurisdiction or settings.STATE).strip().upper()
    built = list(built_sources) if built_sources is not None else build_sources(sources)
    raw_store = RawStore(settings.PROMO_RAW_DIR) if persist_raw else None
    promo_store = PromoStore(settings.PROMO_DB_PATH) if store else None
    run_id = (
        promo_store.start_run(
            jurisdiction=run_state,
            batch_id=batch_id,
            route_scope="state",
        )
        if promo_store is not None
        else None
    )

    offers: list[PromoOffer] = []
    health_rows: list[PromoSourceHealth] = []
    finished_at = started_at
    ok = False
    filtered_unconfirmed = 0
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
                "message": f"Starting promo scrape of {total} feed(s)",
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
            except Exception as exc:  # never abort the slate
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

        offers = dedupe_offers(offers)
        _progress(
            {
                "phase": "enriching",
                "message": "Enriching offer specifics and eligibility…",
                "done": total,
                "total": total,
                "offer_count": len(offers),
                "kind": "promos",
            }
        )
        # Enrich/deepen before primary preference so concrete TheLines welcomes
        # are not dropped against a still-vague first-party "Welcome offer".
        offers = enrich_offers(offers)
        offers = deepen_by_source(offers)
        offers = enrich_offers(offers)
        offers = prefer_primary_offers(offers)
        offers = apply_usage_guidance(offers)
        # Stamp the state verdict rather than filtering on it.  The first live
        # run dropped 51 of 54 offers here with no audit trail — a source that
        # answered with twelve genuine offers graded FAILED because this line
        # emptied it — and the one offer that *did* pass owed its confirmation
        # to another offer's state list sharing its description.  Storing the
        # verdict labels both mistakes instead of hiding them; the predicate
        # itself is unchanged and still fails closed.
        offers = [
            offer.model_copy(
                update={"state_confirmed": offer_confirmed_for_state(offer, run_state)}
            )
            for offer in offers
        ]
        filtered_unconfirmed = sum(1 for offer in offers if not offer.state_confirmed)
        health_rows = _reassess_health_after_enrich(health_rows, offers)
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
                promo_store.finish_run(
                    run_id,
                    ok=ok,
                    offers=offers,
                    health=health_rows,
                    notes=(
                        f"{filtered_unconfirmed} offer(s) stored unconfirmed for "
                        f"{run_state}: eligibility was not affirmatively named by "
                        "the venue's own copy"
                    ),
                )
            except Exception:
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
        filtered_unconfirmed=filtered_unconfirmed,
    )


def collect_promos_batch_once(
    states: Sequence[str],
    *,
    detected_state: str,
    sources: Sequence[str] | None = None,
    store: bool = True,
    persist_raw: bool = True,
    on_progress: Callable[[Mapping[str, object]], None] | None = None,
) -> PromoBatchResult:
    batch_id = uuid4().hex
    globals_built = build_sources(sources, route_scope="global")
    cached = [CachedPromoSource(source) for source in globals_built]
    runs: list[tuple[str, PromoRunResult]] = []
    try:
        for state in states:
            state_sources = build_sources(sources, state=state, route_scope="state")
            result = collect_promos_once(
                store=store,
                persist_raw=persist_raw,
                on_progress=on_progress,
                jurisdiction=state,
                batch_id=batch_id,
                built_sources=[*state_sources, *cached],
            )
            runs.append((state, result))
    finally:
        for source in cached:
            source.close_underlying()
    return PromoBatchResult(
        batch_id=batch_id,
        detected_state=detected_state,
        runs=tuple(runs),
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    from src.state_selection import StateSelectionError, detect_and_select

    try:
        selection = detect_and_select(args.state)
    except StateSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"auto-state: detected {selection.detected_state or 'nothing'} via "
        f"{selection.provider or 'no provider'}; "
        f"batch states: {', '.join(selection.states)}"
    )
    if selection.note:
        print(f"auto-state: {selection.note}")
    batch = collect_promos_batch_once(
        selection.states,
        detected_state=selection.detected_state,
        sources=args.source,
        store=not args.no_store,
        persist_raw=not args.no_raw,
    )
    for state, result in batch.runs:
        print(f"\n--- {state} promo run ---")
        for line in result.summary_lines():
            print(line)
        print(
            f"  unconfirmed for {state}: {result.filtered_unconfirmed} "
            "(stored and labeled, not dropped)"
        )
        if args.verbose:
            for offer in result.offers:
                end = offer.ends_at.isoformat() if offer.ends_at else "-"
                label = offer.summary or offer.title
                confirmed = "yes" if offer.state_confirmed else f"NOT {state}"
                print(
                    f"  [{offer.source}] {offer.kind.value:14} {label[:70]}"
                    f"  ends={end} specific={offer.is_specific} confirmed={confirmed}"
                )
    return 0 if batch.ok else 1


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


def _cmd_plan(args: argparse.Namespace) -> int:
    """Concrete usage plans: latest promo run priced against the latest odds run.

    The same computation the dashboard embeds — same quotes, same event
    reconciliation, same counterparty gate — so this command and the page
    cannot disagree about what a promo is worth.
    """
    from datetime import UTC, datetime

    from src.arb import counterparty_groups, merge_counterparty_groups
    from src.events import reconcile_event_keys
    from src.promos.planner import build_promo_plans
    from src.store import Store

    promo_store = PromoStore(settings.PROMO_DB_PATH)
    odds_store = Store(settings.DB_PATH)
    try:
        # ``is None`` rather than falsy: ``--run 0`` is a value the operator
        # typed and must be answered, not silently replaced by the latest run.
        promo_run = args.run if args.run is not None else promo_store.latest_run_id()
        if promo_run is None:
            print("no promo runs stored yet — run `python -m src.promos collect` first",
                  file=sys.stderr)
            return 1
        # An existence check, not a listing: ``list_runs`` is
        # ``ORDER BY id DESC LIMIT n``, so capping it made every run outside the
        # newest n report as "not stored" — a false verdict of exactly the kind
        # this validation was added to prevent.  The listing is only for the
        # hint that names a few real runs.
        stored_runs = {row["id"] for row in promo_store.list_runs(limit=10)}
        if not promo_store.run_exists(promo_run):
            # A run id that does not exist read as a run holding no offers, and
            # exited 0 — indistinguishable from "every offer was gated out".
            known = ", ".join(f"#{run}" for run in sorted(stored_runs, reverse=True)[:10])
            print(
                f"no promo run #{promo_run} is stored; known runs: {known or 'none'}",
                file=sys.stderr,
            )
            return 1
        promo_row = promo_store.run_row(promo_run)
        promo_state = str((promo_row or {}).get("jurisdiction") or "").upper()
        if args.odds_run is not None:
            # The flag chooses *among same-state runs*, never across states:
            # planning IL promos against a PA slate would price hedges a
            # different licence answered for.  It exists because "latest" can
            # be a thin single-source probe run — the day this shipped, the
            # latest IL run was a 174-quote bet365 acceptance run.
            row = odds_store.run_row(args.odds_run)
            if row is None:
                print(f"no odds run #{args.odds_run} is stored", file=sys.stderr)
                return 1
            odds_jurisdiction = str(dict(row).get("jurisdiction") or "").upper()
            if promo_state and odds_jurisdiction != promo_state:
                print(
                    f"odds run #{args.odds_run} is {odds_jurisdiction or 'ungoverned'}, "
                    f"but promo run #{promo_run} is {promo_state} — refusing to price "
                    "one state's offers against another's slate",
                    file=sys.stderr,
                )
                return 1
            odds_run = args.odds_run
        else:
            odds_run = odds_store.latest_run_id(
                jurisdiction=promo_state or None,
            )
        if odds_run is None:
            print(
                f"no {promo_state or 'matching'} odds run is stored — run "
                "`python -m src.collector collect` first",
                  file=sys.stderr)
            return 1
        offers = promo_store.offers_for_run(promo_run)
        if args.source:
            # Validated against what the run actually holds.  An unknown key —
            # a typo, or the zsh trap where `--source 'a b'` arrives as one
            # argument rather than two — filtered every offer away and printed
            # "no offer produced a concrete plan", which reads as a verdict on
            # the promos rather than on the command line.
            available = {row["source"] for row in offers}
            wanted = set(args.source)
            unknown = sorted(wanted - available)
            if unknown:
                print(
                    f"promo run #{promo_run} has no offers from: {', '.join(unknown)}; "
                    f"it holds: {', '.join(sorted(available)) or 'nothing'}",
                    file=sys.stderr,
                )
                return 1
            offers = [row for row in offers if row["source"] in wanted]
        quotes = odds_store.load_quotes(odds_run)
        everything, _ = reconcile_event_keys(quotes)
        recorded = odds_store.recorded_counterparty_groups(odds_run)
        # The odds run's own jurisdiction governs both the gate measurement
        # and the plan links — never this process's ODDS_STATE.
        from src.sources.registry import view_only_for_run

        odds_row = odds_store.run_row(odds_run)
        odds_state = ((odds_row["jurisdiction"] if odds_row else "") or "").strip().upper()
        built = build_promo_plans(
            offers,
            everything,
            as_of=datetime.now(UTC),
            one_counterparty=merge_counterparty_groups(
                {}
                if recorded
                else counterparty_groups(
                    everything, view_only=view_only_for_run(odds_state)
                ),
                recorded,
            ),
            state=odds_state or None,
        )
        print(
            f"promo run #{promo_run} × odds run #{odds_run}: "
            f"{len(offers)} offers over {built['meta']['group_count']} priceable markets"
        )
        shown = 0
        for row in offers:
            key = f"{row['source']}|{row['offer_id']}"
            plan = built["plans"].get(key)
            if plan is None:
                continue
            concrete = plan.get("plans") or []
            if not concrete and not args.verbose:
                continue
            # ``is not None``: ``--limit 0`` means print none, not print every
            # one.  Under a truthiness test the two swapped places.
            if args.limit is not None and shown >= args.limit:
                break
            shown += 1
            label = row.get("summary") or row["title"]
            print(f"\n[{row['source']}] {row['kind']}: {label[:78]}")
            print(f"  strategy: {plan['strategy']}"
                  + (f"  ev={plan['expected_value']:+.2f}" if plan.get("expected_value") is not None else ""))
            for item in concrete:
                line = "" if item.get("line") is None else f" {item['line']:+g}"
                # A team total names whose total it is, or the two sides of one
                # fixture print as identical lines.
                if item.get("side"):
                    team = (item["home_team"] if item["side"] == "home"
                            else item["away_team"])
                    line = f" ({team}){line}"
                print(
                    f"  {item['away_team']} at {item['home_team']} — "
                    f"{item['market']}{line} {item['period']}"
                    + (f"  [{item['step']}]" if item.get("step") else "")
                )
                for leg in item["legs"]:
                    tag = {
                        "bonus": "credit",
                        "boosted": "boosted",
                    }.get(leg["stake_kind"], "cash")
                    # Each leg's own line, not the group's.  The two sides of a
                    # spread carry opposite signs, and printing only the
                    # canonical line left the operator unable to see which side
                    # of the game each stake goes on — the dashboard has shown
                    # this since the payload carried it.
                    leg_line = ""
                    if leg.get("line") is not None and item["market"] != "moneyline":
                        leg_line = f" {leg['line']:+g}"
                    # One padded field for selection-and-line together: the
                    # width belongs to the pair, not to the line alone, or a
                    # five-letter selection ("under") shifts every column after
                    # it relative to a four-letter one ("over").
                    side = f"{leg['selection']}{leg_line}"
                    print(
                        f"    {leg['role']:5} {leg['source']:16} {side:14} "
                        f"@ {leg['decimal_odds']:.3f} stake {leg['stake']:.2f} ({tag})"
                    )
                worst = item["guaranteed_cash"]
                settled = item["settled_cash"]
                extras = []
                if item.get("conversion_pct") is not None:
                    extras.append(f"conversion {item['conversion_pct']:.1f}%")
                if item.get("breakeven_boost_pct") is not None:
                    extras.append(f"needs {item['breakeven_boost_pct']:.1f}%+ boost")
                if item.get("cost_per_100_wagered") is not None:
                    extras.append(f"cost {item['cost_per_100_wagered']:.2f}/$100")
                extra = ("  " + ", ".join(extras)) if extras else ""
                print(f"    worst {worst:+.2f}, settles {settled:+.2f}{extra}")
                for note in item.get("notes") or ():
                    # Without these the card cannot be reconciled from its own
                    # printed prices: a boosted leg shows its *quoted* odds,
                    # and the note is what says a boost was applied to them.
                    print(f"      · {note}")
            for caveat in plan.get("caveats", []):
                print(f"  note: {caveat}")
            skipped = plan.get("skipped") or {}
            if skipped:
                gates = ", ".join(f"{k}×{v}" for k, v in skipped.items())
                print(f"  gated out: {gates}")
        if shown == 0 and args.limit != 0:
            # ``--limit 0`` asked for no lines printed; that is not a verdict on
            # the offers, which may all have planned perfectly well.
            print("\nno offer produced a concrete plan; run with --verbose to see why "
                  "each was gated out")
    finally:
        promo_store.close()
        odds_store.close()
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
    collect.add_argument(
        "--state",
        action="append",
        type=lambda value: value.strip().upper(),
        # From the jurisdiction table, never a second list: adding a state should
        # not mean finding every place the old four were spelled out.
        choices=sorted(JURISDICTIONS),
        help="state to scrape; repeatable. Naming any state replaces detection "
        "for this run; naming none collects the detected state",
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

    plan = sub.add_parser(
        "plan",
        help="price each stored offer against the latest odds run's games",
    )
    plan.add_argument("--run", type=int, help="promo run id (default: latest)")
    plan.add_argument(
        "--odds-run",
        type=int,
        dest="odds_run",
        help=(
            "odds run id to price against (default: the latest run in the promo "
            "run's own state; refused when the two runs' states differ)"
        ),
    )
    plan.add_argument(
        "--source",
        action="append",
        help="repeatable; only plan offers from these promo sources",
    )
    plan.add_argument("--limit", type=int, default=20,
                      help="most offers to print (default 20)")
    plan.add_argument("--verbose", "-v", action="store_true",
                      help="also print offers whose every market was gated out")
    plan.set_defaults(func=_cmd_plan)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["collect", *(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
