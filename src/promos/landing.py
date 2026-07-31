"""Generic public landing / promotions-page adapter.

Used for books whose promo catalog is HTML marketing copy (or a JSON API that
is empty when logged out).  Pulls title, meta description, and obvious promo
headings from one or more public URLs.  Better than silence; not a substitute
for a structured catalog when one exists.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.geo import merge_regions, normalize_region
from src.promos.html_util import meta_description, page_title, strip_tags
from src.promos.schema import PromoKind, PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint

log = logging.getLogger(__name__)

_HEADING = re.compile(r"<h([1-3])[^>]*>(.*?)</h\1>", re.I | re.S)
_PROMO_WORD = re.compile(
    r"bonus|free bet|promo|boost|welcome|deposit|risk[\s-]?free|no sweat|refer",
    re.I,
)


@dataclass(frozen=True)
class LandingTarget:
    """One public URL to scrape for promo copy."""

    url: str
    endpoint: str
    label: str = ""
    region: str = ""
    """Optional US state / CA province code observed for this landing host."""


_COOKIE_WALL = re.compile(
    r"we got cookies|cookie (?:consent|settings)|accept all cookies|"
    r"challenge-platform|cf-browser-verification|just a moment",
    re.I,
)


class LandingPromoAdapter:
    """Fetch marketing/promo HTML pages and emit coarse PromoOffer rows."""

    def __init__(
        self,
        *,
        source_key: str,
        targets: Sequence[LandingTarget],
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        host_interval: float = 0.4,
        headers: dict[str, str] | None = None,
        default_kind: PromoKind = PromoKind.OTHER,
        empty_is_ok: bool = False,
    ) -> None:
        self._source_key = source_key
        self.targets = tuple(targets)
        self.default_kind = default_kind
        self.empty_is_ok = empty_is_ok
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=host_interval,
            headers=headers or {},
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        raws: list[RawResponse] = []
        errors: list[Exception] = []
        for target in self.targets:
            try:
                raws.append(
                    self._http.get(target.url, endpoint=target.endpoint, expect_json=False)
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: %s failed: %s", self.source_key, target.endpoint, exc)
                errors.append(exc)
        if not raws:
            # Re-raise the last error so the collector records a real failure.
            raise errors[-1] if errors else RuntimeError(f"{self.source_key}: no targets")
        # Partial success is success: one live marketing page is enough to
        # surface a welcome offer even if a secondary URL 404s.
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        by_endpoint = {t.endpoint: t for t in self.targets}
        merged: dict[str, PromoOffer] = {}
        pages_with_signal = 0
        for raw in latest_per_endpoint(raws):
            target = by_endpoint.get(raw.endpoint)
            source = envelope_source((raw,), fallback=self.source_key)
            title = page_title(raw.body) or (target.label if target else raw.endpoint)
            description = meta_description(raw.body) or ""
            headings = [
                strip_tags(m.group(2))
                for m in _HEADING.finditer(raw.body)
                if _PROMO_WORD.search(m.group(2) or "")
            ]
            before = len(merged)
            # Page-level offer only when meta copy names a concrete offer class.
            page_blob = f"{title} {description}"
            concrete = re.compile(
                r"welcome (?:bonus|offer)|sign[- ]?up bonus|free (?:bet|spins?)|"
                r"bonus bet|no[- ]deposit bonus|odds boost|profit boost|no sweat|"
                r"deposit match|risk[- ]?free (?:bet|wager|play)|\$\d|\d+%",
                re.I,
            )
            region = ""
            if target and target.region:
                region = normalize_region(target.region) or target.region.upper()
            regions = [region] if region else []

            def _upsert(offer: PromoOffer) -> None:
                prior = merged.get(offer.offer_id)
                if prior is None:
                    merged[offer.offer_id] = offer
                    return
                merged[offer.offer_id] = prior.model_copy(
                    update={
                        "eligible_regions": merge_regions(
                            prior.eligible_regions, offer.eligible_regions
                        ),
                        "description": prior.description or offer.description,
                    }
                )

            if concrete.search(page_blob):
                # Stable across state hosts so IL/NJ landings merge eligibility.
                offer_id = _stable_id("page", title)
                kind = classify_kind(title, description)
                if kind is PromoKind.OTHER:
                    kind = self.default_kind
                _upsert(
                    PromoOffer(
                        source=source,
                        offer_id=offer_id,
                        kind=kind,
                        title=title.strip() or raw.endpoint,
                        description=description[:4000],
                        url=target.url if target else raw.url,
                        observed_at=raw.fetched_at,
                        raw_ref=raw.ref,
                        raw_kind=target.label if target else "landing",
                        product="sportsbook",
                        eligible_regions=list(regions),
                    )
                )
            for heading in headings[:12]:
                if len(heading) < 12 or not concrete.search(heading):
                    continue
                offer_id = _stable_id("heading", heading)
                _upsert(
                    PromoOffer(
                        source=source,
                        offer_id=offer_id,
                        kind=classify_kind(heading),
                        title=heading,
                        # Keep page meta/URL off heading cards so deepen does
                        # not re-pull welcome $ copy onto unrelated boosts.
                        description="",
                        url=None,
                        observed_at=raw.fetched_at,
                        raw_ref=raw.ref,
                        raw_kind="heading",
                        product="sportsbook",
                        eligible_regions=list(regions),
                    )
                )
            if len(merged) > before:
                pages_with_signal += 1
            elif _COOKIE_WALL.search(raw.body or ""):
                outcome.reject(
                    source,
                    "cookie_wall",
                    "page served a cookie/bot challenge instead of promo copy",
                )
            elif self.empty_is_ok:
                outcome.skipped["no_public_catalog"] += 1
            else:
                outcome.skipped["no_promo_signals"] += 1
        outcome.offers = list(merged.values())
        if pages_with_signal and outcome.skipped.get("no_promo_signals"):
            # Partial multi-state success should not look empty.
            del outcome.skipped["no_promo_signals"]
        return outcome

    def close(self) -> None:
        self._http.close()


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", parts[-1].lower()).strip("-")[:40]
    return f"{slug}-{digest}" if slug else digest


__all__ = ["LandingPromoAdapter", "LandingTarget"]
