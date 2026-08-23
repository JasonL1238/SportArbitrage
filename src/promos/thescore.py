"""theScore Bet promotions from its public promotional-terms help centre.

The sportsbook itself (``sportsbook.us-{st}.thescore.bet``) serves no promotions
page anonymously — ``/promotions`` is a bare shell — but the brand's help
centre does: ``https://sportsbook.thescore.bet/legal/promo-terms`` is a Zendesk
section whose articles are the **full terms** of every running promotion,
including the state clause ("Must be physically present in MI, NJ, PA, or WV")
that eligibility is read from.  Nothing here needs a login.

Most of what is published there is invitation-only (bet-and-gets sent to
existing players), and the article says so.  Those are kept and labelled
rather than dropped: the planner prices what a holder of the invitation would
do, and the Campaign table is where the operator decides which invitations
they hold.

Measured 2026-08-23: 10 promotion articles plus two boilerplate ones (the
section's own terms and the responsible-gaming policy), which are skipped by
name.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import absolute_url, page_title, strip_tags
from src.promos.schema import PromoKind, PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint

log = logging.getLogger(__name__)

SOURCE_KEY = "thescore"
DEFAULT_INDEX_URL = "https://sportsbook.thescore.bet/legal/promo-terms"
INDEX_ENDPOINT = "promo-terms-index"
ARTICLE_ENDPOINT_PREFIX = "promo-terms-article-"

_ARTICLE_HREF = re.compile(r'href="((?:https?://[^"/]+)?/hc/en-us/articles/(\d+)[^"]*)"')
#: Section furniture, not promotions.
_BOILERPLATE = re.compile(r"Promotional-Terms-and-Conditions|Responsible-Gaming", re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_ARTICLE_BODY = re.compile(r'<div[^>]+class="[^"]*article-body[^"]*"[^>]*>(.*?)</div>\s*<', re.I | re.S)
_TITLE_SUFFIX = re.compile(r"\s*[–-]\s*theScore Bet\s*$", re.I)
_INVITED = re.compile(r"\binvited\b", re.I)
_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    return _WS.sub(" ", strip_tags(fragment or "").replace("\xa0", " ")).strip()


class TheScorePromoAdapter:
    """Collects theScore Bet's published promotional terms, one offer per article."""

    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        index_url: str = DEFAULT_INDEX_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_details: int = 12,
    ) -> None:
        self._source_key = source_key
        self.index_url = index_url
        self.max_details = max_details
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=0.35,
            headers={"Referer": index_url},
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        index = self._http.get(self.index_url, endpoint=INDEX_ENDPOINT, expect_json=False)
        raws = [index]
        for url, article_id in self._article_urls(index.body or "")[: self.max_details]:
            try:
                raws.append(
                    self._http.get(
                        url,
                        endpoint=f"{ARTICLE_ENDPOINT_PREFIX}{article_id}",
                        expect_json=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one article, not the section
                log.warning("%s: article %s failed: %s", self._source_key, url, exc)
        return raws

    def _article_urls(self, body: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href, article_id in _ARTICLE_HREF.findall(body):
            if article_id in seen or _BOILERPLATE.search(href):
                continue
            seen.add(article_id)
            found.append((absolute_url(self.index_url, href), article_id))
        return found

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        latest = latest_per_endpoint(raws)
        for raw in latest:
            if not raw.endpoint.startswith(ARTICLE_ENDPOINT_PREFIX):
                continue
            source = envelope_source((raw,), fallback=self._source_key)
            body = raw.body or ""
            heading = _H1.search(body)
            title = _text(heading.group(1)) if heading else ""
            if not title:
                page = page_title(body) or ""
                title = _TITLE_SUFFIX.sub("", page).strip()
            if not title:
                outcome.skipped["article_without_title"] += 1
                continue
            article = _ARTICLE_BODY.search(body)
            terms = _text(article.group(1)) if article else ""
            if not terms:
                # Fall back to everything after the heading, which on this
                # theme is the article and the section footer.
                tail = body[heading.end():] if heading else body
                terms = _text(tail)
            if not terms:
                outcome.skipped["article_without_terms"] += 1
                continue
            # The title names the offer; the article is a full T&C document
            # whose boilerplate mentions bonus bets under every promotion.
            kind = classify_kind(title)
            if kind is PromoKind.OTHER:
                kind = classify_kind(terms)
            invited = bool(_INVITED.search(terms[:1500]))
            outcome.offers.append(
                PromoOffer(
                    source=source,
                    offer_id=raw.endpoint[len(ARTICLE_ENDPOINT_PREFIX):],
                    kind=kind,
                    title=title[:200],
                    description="",
                    terms=terms[:4000],
                    url=raw.url,
                    observed_at=raw.fetched_at,
                    raw_ref=raw.ref,
                    raw_kind="promo-terms-article",
                    product="sportsbook",
                    eligibility_notes="invited players only" if invited else "",
                    metadata={"invited_only": invited},
                )
            )
        return outcome

    def close(self) -> None:
        self._http.close()


__all__ = ["TheScorePromoAdapter", "SOURCE_KEY"]
