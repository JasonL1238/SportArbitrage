"""Follow vague promo URLs to pull concrete offer mechanics."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Iterable

import httpx

from src.promos.enrich import enrich_offer, offer_needs_deepen
from src.promos.html_util import meta_description, page_title, terms_slice, visible_text
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse, RawStore

log = logging.getLogger(__name__)

DEFAULT_MAX_DETAILS = 12

#: Browser-like headers matching the page's real audience (the same set the
#: TheLines adapter uses); the old self-identifying UA had no upside.
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


def _detail_text(body: str) -> tuple[str, str, str, bool]:
    """Return (title, description, terms slice, whether a Terms marker was hit).

    The unmarked fallback is *visible* text — a bare tag strip left script and
    style bodies in, and rows briefly stored ``@font-face`` CSS as terms.
    """
    title = page_title(body) or ""
    desc = meta_description(body) or ""
    marked = terms_slice(body)
    if marked is not None:
        return title, desc, marked, True
    return title, desc, visible_text(body, 6000), False


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
            "terms": offer.terms or terms[:4000],
            "title": offer.title,
            "metadata": {
                **offer.metadata,
                "deepened_from": url,
                "deepened_title": title[:200],
            },
        }
    )
    return enrich_offer(merged)


def _apply_terms(offer: PromoOffer, *, terms: str, url: str, marked: bool) -> PromoOffer:
    """Merge a fetch of the offer's own declared T&C link — terms only.

    The link came from the page that minted the offer, so the title-relevance
    guard does not apply; but this is **fill-only into empty terms**, and only
    from a Terms-heading-marked slice.  Replacement is forbidden outright: on
    the first live firing, four offers' genuine parsed terms were replaced by
    the venue's sitewide house rules (which of course carry a Terms heading),
    importing another document's state list wholesale.
    """
    if not terms or not marked or offer.terms:
        return offer
    merged = offer.model_copy(
        update={
            "terms": terms[:4000],
            "metadata": {**offer.metadata, "deepened_terms_from": url},
        }
    )
    return enrich_offer(merged)


def _fetch_detail(
    http: httpx.Client,
    url: str,
    *,
    source: str,
    raw_store: RawStore | None,
) -> tuple[str, str, str, bool] | None:
    """One GET; leaves a raw envelope when a store is threaded through."""
    resp = http.get(url, headers=_HEADERS)
    if raw_store is not None:
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:48]
        try:
            raw_store.write(
                RawResponse(
                    source=source,
                    endpoint=f"deepen-{slug or 'x'}",
                    url=url,
                    status_code=resp.status_code,
                    body=resp.text or "",
                    fetched_at=datetime.now(UTC),
                    content_type=resp.headers.get("content-type"),
                )
            )
        except Exception:
            log.exception("deepen raw write failed for %s %s", source, url)
    if resp.status_code >= 400 or not resp.text:
        return None
    return _detail_text(resp.text)


#: A shareable fetch cache: URL → parsed detail, or None for a *failed* fetch.
#: Failures are cached too — a venue's shared footer terms link 404ing must
#: cost one request, not one per offer.  Pass one dict across the sources and
#: states of a batch and every URL is fetched exactly once per batch.
DeepenCache = dict[str, "tuple[str, str, str, bool] | None"]


def deepen_offers(
    offers: Iterable[PromoOffer],
    *,
    client: httpx.Client | None = None,
    max_details: int = DEFAULT_MAX_DETAILS,
    timeout: float = 20.0,
    raw_store: RawStore | None = None,
    cache: DeepenCache | None = None,
) -> list[PromoOffer]:
    """Fetch detail pages for vague offers until specifics appear or cap hits."""
    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    if cache is None:
        cache = {}
    try:
        out: list[PromoOffer] = []
        fetched = 0
        for offer in offers:
            current = enrich_offer(offer)
            if not offer_needs_deepen(current):
                out.append(current)
                continue
            # The offer's own declared T&C link first — but only while the
            # offer has no terms at all (the fetch can only fill, never
            # replace); the promo page itself is the fallback.
            candidates: list[tuple[str, bool]] = []
            terms_url = str(current.metadata.get("terms_url") or "")
            # A terms_url equal to the offer's own URL keeps the terms-only
            # path *deliberately*: it forgoes the full detail merge (title,
            # description, unmarked-text fill), but the merge's vague-welcome
            # allowance would let an unrelated chrome page's "$10" fill empty
            # terms — marked-slice-or-nothing fails closed instead.
            if terms_url and not current.terms:
                candidates.append((terms_url, True))
            if current.url and current.url != terms_url:
                candidates.append((current.url, False))
            for url, is_terms_link in candidates:
                if url in cache:
                    detail = cache[url]
                else:
                    if fetched >= max_details:
                        continue
                    fetched += 1
                    try:
                        detail = _fetch_detail(
                            http, url, source=current.source, raw_store=raw_store
                        )
                    except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                        log.warning(
                            "deepen failed for %s %s: %s", current.source, url, exc
                        )
                        detail = None
                    cache[url] = detail
                if detail is None:
                    continue
                title, desc, terms, marked = detail
                if is_terms_link:
                    current = _apply_terms(current, terms=terms, url=url, marked=marked)
                else:
                    current = _apply_detail(
                        current, title=title, desc=desc, terms=terms, url=url
                    )
                if not offer_needs_deepen(current):
                    break
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
    raw_store: RawStore | None = None,
    cache: DeepenCache | None = None,
) -> list[PromoOffer]:
    """Apply a per-source detail budget so one book cannot starve the rest."""
    by_source: dict[str, list[PromoOffer]] = {}
    order: list[str] = []
    for offer in offers:
        if offer.source not in by_source:
            by_source[offer.source] = []
            order.append(offer.source)
        by_source[offer.source].append(offer)

    if cache is None:
        cache = {}
    out: list[PromoOffer] = []
    for source in order:
        out.extend(
            deepen_offers(
                by_source[source],
                client=client,
                max_details=max_details_per_source,
                raw_store=raw_store,
                cache=cache,
            )
        )
    return out


__all__ = ["DEFAULT_MAX_DETAILS", "DeepenCache", "deepen_by_source", "deepen_offers"]
