"""Follow vague promo URLs to pull concrete offer mechanics."""
from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx

from src.promos.enrich import enrich_offer, offer_needs_deepen
from src.promos.html_util import meta_description, page_title
from src.promos.schema import PromoOffer

log = logging.getLogger(__name__)

DEFAULT_MAX_DETAILS = 12
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(body: str) -> str:
    text = _TAG_RE.sub(" ", body or "")
    return _WS_RE.sub(" ", text).strip()


def _detail_text(body: str) -> tuple[str, str, str]:
    """Return (title, description, terms-ish body slice)."""
    title = page_title(body) or ""
    desc = meta_description(body) or ""
    plain = _strip_html(body)
    terms = plain[:6000]
    return title, desc, terms


def _title_related(offer_title: str, page_title: str, page_text: str) -> bool:
    """True when the detail page plausibly belongs to this offer."""
    ot = re.sub(r"[^a-z0-9]+", "", (offer_title or "").lower())
    pt = re.sub(r"[^a-z0-9]+", "", (page_title or "").lower())
    if not ot:
        return False
    # Empty page titles must not match every offer (``"" in ot`` is always true).
    if pt and (ot in pt or pt in ot):
        return True
    # Vague welcome titles may deepen against a concrete welcome detail page.
    if re.search(r"welcome|signup|sign.?up", offer_title or "", re.I) and re.search(
        r"\$\s*\d|bonus\s+bet|free\s+bet|deposit\s+match",
        f"{page_title} {page_text}",
        re.I,
    ):
        return True
    # Require a distinctive token from the offer title in the *page title*
    # (not the whole body — shared hubs mention many boost words).
    if not pt:
        return False
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]{5,}", (offer_title or "").lower())
        if t
        not in {
            "bonus",
            "offer",
            "promo",
            "sports",
            "welcome",
            "boost",
            "bets",
            "daily",
            "odds",
        }
    ]
    return any(t in pt for t in tokens[:6])


def _apply_detail(offer: PromoOffer, *, title: str, desc: str, terms: str, url: str) -> PromoOffer:
    # Shared catalog URLs must not stamp welcome mechanics onto unrelated cards.
    if not _title_related(offer.title, title, f"{desc} {terms}"):
        return offer
    merged = offer.model_copy(
        update={
            "description": offer.description or desc,
            "terms": offer.terms or terms,
            "title": offer.title,
            "metadata": {
                **offer.metadata,
                "deepened_from": url,
                "deepened_title": title[:200],
            },
        }
    )
    return enrich_offer(merged)


def deepen_offers(
    offers: Iterable[PromoOffer],
    *,
    client: httpx.Client | None = None,
    max_details: int = DEFAULT_MAX_DETAILS,
    timeout: float = 20.0,
) -> list[PromoOffer]:
    """Fetch detail pages for vague offers until specifics appear or cap hits."""
    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        out: list[PromoOffer] = []
        fetched = 0
        cache: dict[str, tuple[str, str, str]] = {}
        for offer in offers:
            current = enrich_offer(offer)
            if not offer_needs_deepen(current):
                out.append(current)
                continue
            url = current.url or ""
            if not url:
                out.append(current)
                continue
            if url in cache:
                title, desc, terms = cache[url]
                out.append(
                    _apply_detail(current, title=title, desc=desc, terms=terms, url=url)
                )
                continue
            if fetched >= max_details:
                out.append(current)
                continue
            try:
                resp = http.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,*/*",
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; SportArbitragePromos/1.0)"
                        ),
                    },
                )
                fetched += 1
                if resp.status_code >= 400 or not resp.text:
                    out.append(current)
                    continue
                title, desc, terms = _detail_text(resp.text)
                cache[url] = (title, desc, terms)
                out.append(
                    _apply_detail(current, title=title, desc=desc, terms=terms, url=url)
                )
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                log.warning("deepen failed for %s %s: %s", current.source, url, exc)
                out.append(current)
        return out
    finally:
        if owned:
            http.close()


def deepen_by_source(
    offers: list[PromoOffer],
    *,
    max_details_per_source: int = DEFAULT_MAX_DETAILS,
    client: httpx.Client | None = None,
) -> list[PromoOffer]:
    """Apply a per-source detail budget so one book cannot starve the rest."""
    by_source: dict[str, list[PromoOffer]] = {}
    order: list[str] = []
    for offer in offers:
        if offer.source not in by_source:
            by_source[offer.source] = []
            order.append(offer.source)
        by_source[offer.source].append(offer)

    out: list[PromoOffer] = []
    for source in order:
        out.extend(
            deepen_offers(
                by_source[source],
                client=client,
                max_details=max_details_per_source,
            )
        )
    return out


__all__ = ["DEFAULT_MAX_DETAILS", "deepen_by_source", "deepen_offers"]
