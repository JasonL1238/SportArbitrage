"""LeoVegas promotions from the public Next.js CMS / Relay payload.

``/en-row/promotions`` embeds ``__NEXT_DATA__`` whose Relay ``initialData``
lists promotion cards (name/title, teaser, terms, CTA URL) without login.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import absolute_url, next_data
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint
from src.sources.guards import FormatChangeError

SOURCE_KEY = "leovegas_kambi"
DEFAULT_URL = "https://www.leovegas.com/en-row/promotions"
ENDPOINT = "promotions-next"


class LeoVegasPromoAdapter:
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
            source_key, timeout=timeout, client=client, host_interval=0.4
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        return [self._http.get(self.url, endpoint=ENDPOINT, expect_json=False)]

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
            cards = list(_promotion_cards(data))
            if not cards:
                raise FormatChangeError(
                    f"{source}:{ENDPOINT}: Relay payload carried no promotion cards"
                )
            seen: set[str] = set()
            for card in cards:
                offer = self._offer_from(card, raw=raw, source=source)
                if offer is None:
                    outcome.skipped["unusable_promo"] += 1
                    continue
                if offer.offer_id in seen:
                    outcome.skipped["duplicate_offer_id"] += 1
                    continue
                seen.add(offer.offer_id)
                outcome.offers.append(offer)
        return outcome

    def _offer_from(
        self, card: Mapping[str, Any], *, raw: RawResponse, source: str
    ) -> PromoOffer | None:
        attrs = card.get("attributes") if isinstance(card.get("attributes"), Mapping) else {}
        title = str(
            attrs.get("title")
            or card.get("name")
            or ""
        ).strip()
        # CMS internal names like "Casino - Lunch Spins - EN + ROW"
        if " - EN" in title or title.endswith(" - PLU"):
            title = title.split(" - EN")[0].split(" - PLU")[0].strip()
        if not title or len(title) < 4:
            return None
        teaser = str(attrs.get("teaser") or "").strip()
        terms_obj = attrs.get("terms")
        terms = ""
        if isinstance(terms_obj, Mapping):
            terms = str(terms_obj.get("text") or "")[:4000]
        elif isinstance(terms_obj, str):
            terms = terms_obj[:4000]
        description = teaser or terms[:400]
        url = _cta_url(attrs) or self.url
        if url.startswith("/"):
            url = absolute_url("https://www.leovegas.com", url)
        vertical = str(attrs.get("vertical") or "").strip().lower()
        product = "casino" if vertical == "casino" else (vertical or "sportsbook")
        offer_id = str(attrs.get("slug") or attrs.get("tournamentId") or card.get("id") or title)
        kind = classify_kind(title, description, terms[:200])
        return PromoOffer(
            source=source,
            offer_id=offer_id,
            kind=kind,
            title=title[:200],
            description=description[:4000],
            terms=terms,
            url=url,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            raw_kind=vertical or "promotion",
            product=product,
            metadata={"feed": "leovegas_cms"},
        )

    def close(self) -> None:
        self._http.close()


def _promotion_cards(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    props = data.get("props")
    if not isinstance(props, Mapping):
        return []
    initial = props.get("initialData")
    if not isinstance(initial, list):
        return []
    out: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for entry in initial:
        payload = _relay_payload(entry)
        if payload is None:
            continue
        for node in _walk_promotions(payload):
            key = str(node.get("id") or node.get("name") or id(node))
            if key in seen:
                continue
            seen.add(key)
            out.append(node)
    return out


def _relay_payload(entry: Any) -> Mapping[str, Any] | None:
    if not isinstance(entry, list) or len(entry) < 2:
        return None
    blob = entry[1]
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except ValueError:
            return None
    if not isinstance(blob, Mapping):
        return None
    # Shape: {fetchTime, payload: {data: …}} or already the inner payload.
    if "payload" in blob and isinstance(blob["payload"], Mapping):
        return blob["payload"]
    return blob


def _walk_promotions(node: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(node, Mapping):
        typ = node.get("type")
        type_name = typ.get("name") if isinstance(typ, Mapping) else None
        if type_name == "promotion":
            found.append(node)
        for value in node.values():
            found.extend(_walk_promotions(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_promotions(item))
    return found


def _cta_url(attrs: Mapping[str, Any]) -> str | None:
    actions = attrs.get("actions")
    if not isinstance(actions, Mapping):
        return None
    for key in ("secondary", "primary", "tertiary"):
        action = actions.get(key)
        if isinstance(action, Mapping):
            url = action.get("url")
            if isinstance(url, str) and url.strip() and url.strip().lower() != "none":
                return url.strip()
    url = attrs.get("url")
    if isinstance(url, str) and url.strip() and url.strip().lower() != "none":
        return url.strip()
    return None


__all__ = ["LeoVegasPromoAdapter", "SOURCE_KEY"]
