"""Cloudbet promotions from the public Next.js promotions page.

``/en/promotions`` embeds ``__NEXT_DATA__`` with ``seoContent.main-promos`` —
title, intro, uri, image — without login.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import absolute_url, next_data
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint
from src.sources.guards import FormatChangeError

log = logging.getLogger(__name__)

SOURCE_KEY = "cloudbet"
DEFAULT_URL = "https://www.cloudbet.com/en/promotions"
ENDPOINT = "promotions-page"


class CloudbetPromoAdapter:
    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        url: str = DEFAULT_URL,
    ) -> None:
        self._source_key = source_key
        self.url = url
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=0.35
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        return [
            self._http.get(self.url, endpoint=ENDPOINT, expect_json=False)
        ]

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        for raw in latest_per_endpoint(raws):
            if raw.endpoint != ENDPOINT:
                outcome.skipped["foreign_endpoint"] += 1
                continue
            source = envelope_source((raw,), fallback=self.source_key)
            data = next_data(raw.body)
            if data is None:
                outcome.reject(source, "missing_next_data", "no __NEXT_DATA__ script")
                continue
            try:
                page_props = data["props"]["pageProps"]
            except (KeyError, TypeError) as exc:
                outcome.reject(source, "bad_next_shape", str(exc))
                continue
            if not isinstance(page_props, Mapping):
                outcome.reject(source, "bad_page_props", "pageProps is not an object")
                continue
            seo = page_props.get("seoContent")
            if not isinstance(seo, Mapping):
                outcome.reject(source, "missing_seo", "seoContent missing")
                continue
            seen: set[str] = set()
            for bucket in ("main-promos", "sidebar-promos"):
                items = seo.get(bucket)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        outcome.skipped["bad_promo"] += 1
                        continue
                    offer = self._offer_from(item, raw=raw, source=source)
                    if offer is None:
                        outcome.skipped["unusable_promo"] += 1
                        continue
                    if offer.offer_id in seen:
                        outcome.skipped["duplicate_offer_id"] += 1
                        continue
                    seen.add(offer.offer_id)
                    outcome.offers.append(offer)
            if not outcome.offers and not outcome.rejections:
                raise FormatChangeError(
                    f"{source}:{ENDPOINT}: seoContent carried no promo cards"
                )
        return outcome

    def _offer_from(
        self, item: Mapping[str, Any], *, raw: RawResponse, source: str
    ) -> PromoOffer | None:
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        intro = str(item.get("intro") or "").strip()[:4000]
        uri = str(item.get("uri") or "").strip()
        # Only relative venue paths become offer URLs — reject absolute,
        # scheme-relative, and scheme URIs (javascript:/data:) from CMS JSON.
        safe_relative = bool(uri) and not uri.startswith("//") and ":" not in uri.split("/", 1)[0]
        if safe_relative:
            url = absolute_url("https://www.cloudbet.com/en/", uri)
            offer_id = uri
        else:
            url = self.url
            offer_id = uri or title
        product = "casino" if "/casino/" in uri else "sportsbook" if uri else ""
        kind = classify_kind(title, intro, uri)
        return PromoOffer(
            source=source,
            offer_id=offer_id,
            kind=kind,
            title=title,
            description=intro,
            url=url,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            raw_kind=uri.split("/")[1] if "/" in uri else "",
            product=product,
            metadata={"image": item.get("image")},
        )

    def close(self) -> None:
        self._http.close()


__all__ = ["CloudbetPromoAdapter", "SOURCE_KEY"]
