"""Bovada promotions from the public promotions index and detail pages.

The listing page is SSR HTML (Next).  Card titles and ``/promotions/{slug}``
links are visible logged-out; detail pages carry title + meta description with
the bonus amount.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import (
    absolute_url,
    hrefs,
    meta_description,
    page_title,
)
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint

log = logging.getLogger(__name__)

SOURCE_KEY = "bovada"
INDEX_URL = "https://www.bovada.lv/promotions"
INDEX_ENDPOINT = "promotions-index"
DETAIL_ENDPOINT_PREFIX = "promotions-detail-"

#: Known sports-relevant slugs always fetched even if the index markup drifts.
SEED_SLUGS: tuple[str, ...] = (
    "sports-welcome-bonus",
    "same-game-parlay",
)

class BovadaPromoAdapter:
    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_details: int = 8,
    ) -> None:
        self._source_key = source_key
        self.max_details = max_details
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=0.4
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        index = self._http.get(INDEX_URL, endpoint=INDEX_ENDPOINT, expect_json=False)
        slugs = self._slugs_from_index(index.body)
        raws = [index]
        for slug in slugs[: self.max_details]:
            url = absolute_url(INDEX_URL + "/", slug)
            try:
                raws.append(
                    self._http.get(
                        url,
                        endpoint=f"{DETAIL_ENDPOINT_PREFIX}{slug}",
                        expect_json=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad slug must not kill the book
                log.warning("%s: detail %s failed: %s", self.source_key, slug, exc)
        return raws

    def _slugs_from_index(self, body: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for href in hrefs(body, prefix="/promotions/"):
            slug = href.rstrip("/").split("/promotions/", 1)[-1].strip("/")
            if not slug or "/" in slug or slug in seen:
                continue
            seen.add(slug)
            found.append(slug)
        for seed in SEED_SLUGS:
            if seed not in seen:
                found.insert(0, seed)
                seen.add(seed)
        return found

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        latest = latest_per_endpoint(raws)
        index = next((r for r in latest if r.endpoint == INDEX_ENDPOINT), None)
        if index is not None:
            self._parse_index(index, outcome)
        for raw in latest:
            if not raw.endpoint.startswith(DETAIL_ENDPOINT_PREFIX):
                continue
            self._parse_detail(raw, outcome)
        return outcome

    def _parse_index(self, raw: RawResponse, outcome: PromoParseOutcome) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        # Prefer stable path slugs from the index; title text alone drifts.
        for href in hrefs(raw.body, prefix="/promotions/"):
            slug = href.rstrip("/").split("/promotions/", 1)[-1].strip("/")
            if not slug or "/" in slug:
                continue
            if any(o.offer_id == slug for o in outcome.offers):
                continue
            title = slug.replace("-", " ").title()
            kind = classify_kind(title, slug)
            outcome.offers.append(
                PromoOffer(
                    source=source,
                    offer_id=slug,
                    kind=kind,
                    title=title,
                    description=meta_description(raw.body) or "",
                    url=absolute_url(INDEX_URL + "/", slug),
                    observed_at=raw.fetched_at,
                    raw_ref=raw.ref,
                    raw_kind="index_card",
                    product="sportsbook" if "sport" in slug else "",
                )
            )

    def _parse_detail(self, raw: RawResponse, outcome: PromoParseOutcome) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        slug = raw.endpoint.removeprefix(DETAIL_ENDPOINT_PREFIX)
        title = page_title(raw.body) or slug.replace("-", " ").title()
        # Drop site chrome suffixes.
        title = re.sub(r"\s*\|\s*Bovada.*$", "", title, flags=re.I).strip()
        description = meta_description(raw.body) or ""
        if not title:
            outcome.skipped["detail_without_title"] += 1
            return
        # Prefer detail rows over index cards with the same slug.
        outcome.offers = [o for o in outcome.offers if o.offer_id != slug]
        kind = classify_kind(title, description, slug)
        product = "sportsbook"
        if "casino" in slug or "casino" in title.lower():
            product = "casino"
        elif "poker" in slug:
            product = "poker"
        outcome.offers.append(
            PromoOffer(
                source=source,
                offer_id=slug,
                kind=kind,
                title=title,
                description=description,
                url=absolute_url(INDEX_URL + "/", slug),
                observed_at=raw.fetched_at,
                raw_ref=raw.ref,
                raw_kind=slug,
                product=product,
            )
        )

    def close(self) -> None:
        self._http.close()


__all__ = ["BovadaPromoAdapter", "SOURCE_KEY"]
