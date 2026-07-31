"""HTML promotions-index adapter with optional detail-page deepening.

Used for US majors and Ontario books that publish public ``/promotions`` pages
without a stable logged-out JSON catalog.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Sequence
from urllib.parse import urldefrag

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import absolute_url, hrefs, meta_description, page_title, strip_tags
from src.promos.schema import PromoKind, PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint

log = logging.getLogger(__name__)

INDEX_ENDPOINT = "promotions-index"
DETAIL_ENDPOINT_PREFIX = "promotions-detail-"

_PROMO_HREF = re.compile(
    r"/(?:promotions?|bonus(?:es)?|offers?|promo)/[a-z0-9][\w\-/%]*",
    re.I,
)
_CARD = re.compile(
    r"<(?:h[1-4]|a)[^>]*>(.{12,180}?)</(?:h[1-4]|a)>",
    re.I | re.S,
)
_CONCRETE = re.compile(
    r"welcome|bonus|free bet|bonus bet|deposit|risk[- ]?free|no sweat|"
    r"odds boost|profit boost|\$\d|\d+%",
    re.I,
)
_HREF_NEAR = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.{12,180}?)</a>',
    re.I | re.S,
)


def _canon_url(url: str) -> str:
    base, _ = urldefrag(url or "")
    return base.rstrip("/")


class HtmlCatalogPromoAdapter:
    def __init__(
        self,
        *,
        source_key: str,
        index_url: str,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_details: int = 10,
        default_kind: PromoKind = PromoKind.OTHER,
        eligible_regions: Sequence[str] | None = None,
        product: str = "sportsbook",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._source_key = source_key
        self.index_url = index_url
        self.max_details = max_details
        self.default_kind = default_kind
        # Only set when the adapter is itself geo-scoped (e.g. Ontario host).
        # National catalogs leave this empty so enrich/terms drive eligibility.
        self.eligible_regions = [r.upper() for r in (eligible_regions or ())]
        self.product = product
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=0.35,
            headers=headers or {},
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        index = self._http.get(self.index_url, endpoint=INDEX_ENDPOINT, expect_json=False)
        raws = [index]
        for slug_url in self._detail_urls(index.body)[: self.max_details]:
            slug = re.sub(r"[^a-z0-9]+", "-", slug_url.lower()).strip("-")[:48]
            try:
                raws.append(
                    self._http.get(
                        slug_url,
                        endpoint=f"{DETAIL_ENDPOINT_PREFIX}{slug or 'x'}",
                        expect_json=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: detail %s failed: %s", self.source_key, slug_url, exc)
        return raws

    def _detail_urls(self, body: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for href in hrefs(body):
            if not _PROMO_HREF.search(href):
                continue
            url = _canon_url(absolute_url(self.index_url, href))
            if url == _canon_url(self.index_url):
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)
        return found

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        latest = latest_per_endpoint(raws)
        index = next((r for r in latest if r.endpoint == INDEX_ENDPOINT), None)
        by_key: dict[str, PromoOffer] = {}
        url_index: dict[str, str] = {}
        if index is not None:
            self._parse_index(index, by_key, url_index)
        for raw in latest:
            if not raw.endpoint.startswith(DETAIL_ENDPOINT_PREFIX):
                continue
            self._parse_detail(raw, outcome, by_key, url_index)
        outcome.offers = list(by_key.values())
        if not outcome.offers:
            outcome.skipped["no_promo_signals"] += 1
        return outcome

    def _parse_index(
        self,
        raw: RawResponse,
        by_key: dict[str, PromoOffer],
        url_index: dict[str, str],
    ) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        title = page_title(raw.body) or f"{self.source_key} promotions"
        description = meta_description(raw.body) or ""
        if _CONCRETE.search(f"{title} {description}"):
            offer = self._make_offer(
                source=source,
                offer_id="index-meta",
                title=title.strip(),
                description=description,
                url=self.index_url,
                raw=raw,
                raw_kind="index",
            )
            by_key[offer.offer_id] = offer
            url_index[_canon_url(self.index_url)] = offer.offer_id

        for match in _HREF_NEAR.finditer(raw.body or ""):
            href, label = match.group(1), strip_tags(match.group(2))
            if len(label) < 12 or not _CONCRETE.search(label):
                continue
            if not _PROMO_HREF.search(href):
                continue
            url = absolute_url(self.index_url, href)
            offer_id = _stable_id(label)
            offer = self._make_offer(
                source=source,
                offer_id=offer_id,
                title=label,
                # Never copy page-level meta onto cards — it contaminates
                # boosts with welcome $ mechanics during enrich.
                description="",
                url=url,
                raw=raw,
                raw_kind="card",
            )
            by_key[offer_id] = offer
            url_index[_canon_url(url)] = offer_id

        for match in _CARD.finditer(raw.body or ""):
            heading = strip_tags(match.group(1))
            if len(heading) < 12 or not _CONCRETE.search(heading):
                continue
            offer_id = _stable_id(heading)
            if offer_id in by_key:
                continue
            # No per-offer detail URL — leave url empty so deepen does not
            # stamp the whole index page's welcome copy onto unrelated cards.
            by_key[offer_id] = self._make_offer(
                source=source,
                offer_id=offer_id,
                title=heading,
                description="",
                url="",
                raw=raw,
                raw_kind="card",
            )

    def _parse_detail(
        self,
        raw: RawResponse,
        outcome: PromoParseOutcome,
        by_key: dict[str, PromoOffer],
        url_index: dict[str, str],
    ) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        title = page_title(raw.body) or ""
        description = meta_description(raw.body) or ""
        if not title or not _CONCRETE.search(f"{title} {description}"):
            outcome.skipped["detail_without_promo_copy"] += 1
            return
        terms = re.sub(r"<[^>]+>", " ", raw.body or "")
        terms = re.sub(r"\s+", " ", terms).strip()[:6000]
        canon = _canon_url(raw.url)
        offer_id = url_index.get(canon) or _stable_id(title)
        prior = by_key.get(offer_id)
        offer = self._make_offer(
            source=source,
            offer_id=offer_id,
            title=(prior.title if prior else title.strip()),
            description=description or (prior.description if prior else ""),
            url=raw.url,
            raw=raw,
            raw_kind="detail",
            terms=terms,
        )
        if prior is not None:
            offer = prior.model_copy(
                update={
                    "description": offer.description or prior.description,
                    "terms": offer.terms or prior.terms,
                    "url": offer.url or prior.url,
                }
            )
        by_key[offer_id] = offer
        url_index[canon] = offer_id

    def _make_offer(
        self,
        *,
        source: str,
        offer_id: str,
        title: str,
        description: str,
        url: str,
        raw: RawResponse,
        raw_kind: str,
        terms: str = "",
    ) -> PromoOffer:
        kind = classify_kind(title, description, terms)
        if kind is PromoKind.OTHER:
            kind = self.default_kind
        return PromoOffer(
            source=source,
            offer_id=offer_id,
            kind=kind,
            title=title[:200],
            description=(description or "")[:4000],
            terms=terms[:4000],
            url=url,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            raw_kind=raw_kind,
            product=self.product,
            eligible_regions=list(self.eligible_regions),
        )

    def close(self) -> None:
        self._http.close()


def _stable_id(text: str) -> str:
    digest = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return f"{slug}-{digest}" if slug else digest


__all__ = ["HtmlCatalogPromoAdapter", "INDEX_ENDPOINT", "DETAIL_ENDPOINT_PREFIX"]
