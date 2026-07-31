"""FanDuel promotions — merchandising API plus public marketing pages.

``GET /promos/api/merchandising`` is the same JSON the sportsbook Promotions
tab loads (requires ``x-sportsbook-region``).  Logged-out it often returns an
empty ``promotions`` list; the marketing pages still advertise the current
welcome offer, so those are fetched as a fallback surface.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import meta_description, page_title
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint, parse_iso_time

log = logging.getLogger(__name__)

SOURCE_KEY = "fanduel"
MERCH_URL = "https://api.sportsbook.fanduel.com/promos/api/merchandising"
MERCH_ENDPOINT = "merchandising"
#: Prefer the sportsbook origin — www.fanduel.com is PerimeterX-walled from
#: many research egresses even when the API answers.
LANDING_URL = "https://sportsbook.fanduel.com/navigation/promotions"
LANDING_ENDPOINT = "promotions-page"
DEFAULT_REGION = "IL"


class FanDuelPromoAdapter:
    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        region: str = DEFAULT_REGION,
    ) -> None:
        self._source_key = source_key
        self.region = region.upper()
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=0.35,
            headers={
                "Origin": "https://sportsbook.fanduel.com",
                "Referer": "https://sportsbook.fanduel.com/",
                "Accept": "application/json, text/html, */*",
                "x-sportsbook-region": self.region,
            },
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        raws: list[RawResponse] = []
        try:
            raws.append(
                self._http.get(
                    MERCH_URL,
                    endpoint=MERCH_ENDPOINT,
                    params={
                        "fdProEnabled": "false",
                        "channel": "desktop",
                        "adaptiveTokenEnabled": "true",
                    },
                    record_params={"region": self.region},
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: merchandising skipped: %s", self.source_key, exc)
            if getattr(exc, "raw", None) is not None:
                raws.append(exc.raw)
        # Marketing HTML is PerimeterX-walled from some egresses; never let that
        # (or a merch failure) erase the other surface.
        try:
            raws.append(
                self._http.get(
                    LANDING_URL,
                    endpoint=LANDING_ENDPOINT,
                    expect_json=False,
                    headers={"Accept": "text/html,application/xhtml+xml,*/*"},
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: marketing page skipped: %s", self.source_key, exc)
            if getattr(exc, "raw", None) is not None:
                raws.append(exc.raw)
        if not raws:
            raise RuntimeError(f"{self.source_key}: merchandising and marketing both failed")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        for raw in latest_per_endpoint(raws):
            if raw.endpoint == MERCH_ENDPOINT:
                self._parse_merch(raw, outcome)
            elif raw.endpoint == LANDING_ENDPOINT:
                self._parse_landing(raw, outcome)
            else:
                outcome.skipped["foreign_endpoint"] += 1
        return outcome

    def _parse_merch(self, raw: RawResponse, outcome: PromoParseOutcome) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        try:
            payload = raw.json()
        except ValueError as exc:
            outcome.reject(source, "bad_json", str(exc))
            return
        if not isinstance(payload, Mapping):
            outcome.reject(source, "not_object", "merchandising root is not an object")
            return
        promotions = payload.get("promotions")
        if promotions is None:
            outcome.reject(source, "missing_promotions", "no promotions field")
            return
        if not isinstance(promotions, list):
            outcome.reject(source, "bad_promotions", "promotions is not a list")
            return
        if not promotions:
            outcome.skipped["empty_merchandising"] += 1
            return
        for item in promotions:
            if not isinstance(item, Mapping):
                outcome.skipped["bad_promotion"] += 1
                continue
            offer = self._from_merch_item(item, raw=raw, source=source)
            if offer is None:
                outcome.skipped["unusable_promotion"] += 1
                continue
            outcome.offers.append(offer)

    def _from_merch_item(
        self, item: Mapping[str, Any], *, raw: RawResponse, source: str
    ) -> PromoOffer | None:
        title = _first_str(
            item,
            "title",
            "headline",
            "name",
            "promotionTitle",
            "displayName",
        )
        if not title:
            return None
        description = _first_str(item, "description", "subtitle", "summary")
        terms = _first_str(item, "terms", "termsAndConditions")
        offer_id = str(
            item.get("promotionId")
            or item.get("id")
            or item.get("offerId")
            or title
        )
        kind = classify_kind(
            _first_str(item, "type", "category", "promoType"),
            title,
            description,
        )
        return PromoOffer(
            source=source,
            offer_id=offer_id,
            kind=kind,
            title=title,
            description=description,
            terms=terms[:4000],
            url=_first_str(item, "url", "deeplink", "webUrl")
            or "https://sportsbook.fanduel.com/navigation/promotions",
            starts_at=parse_iso_time(item.get("startDate") or item.get("startsAt")),
            ends_at=parse_iso_time(item.get("endDate") or item.get("expiresAt")),
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            raw_kind=_first_str(item, "type", "category"),
            product="sportsbook",
        )

    def _parse_landing(self, raw: RawResponse, outcome: PromoParseOutcome) -> None:
        # Only used when the API catalog is empty — avoid duplicating structured rows.
        if any(o.raw_ref != raw.ref for o in outcome.offers):
            # Merch already produced rows.
            if outcome.offers:
                outcome.skipped["landing_redundant"] += 1
                return
        source = envelope_source((raw,), fallback=self.source_key)
        title = page_title(raw.body) or "FanDuel Sportsbook"
        description = meta_description(raw.body) or ""
        blob = f"{title} {description}".lower()
        # Require a real offer word — bare "bet"/"promo" in chrome is not an offer.
        if not any(
            w in blob
            for w in (
                "bonus",
                "welcome offer",
                "free bet",
                "odds boost",
                "profit boost",
                "no sweat",
                "bet reset",
            )
        ):
            outcome.skipped["landing_without_promo_copy"] += 1
            return
        kind = classify_kind(title, description)
        outcome.offers.append(
            PromoOffer(
                source=source,
                offer_id="marketing-sportsbook",
                kind=kind,
                title=title.strip(),
                description=description[:4000],
                url=LANDING_URL,
                observed_at=raw.fetched_at,
                raw_ref=raw.ref,
                raw_kind="marketing",
                product="sportsbook",
            )
        )

    def close(self) -> None:
        self._http.close()


def _first_str(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = ["FanDuelPromoAdapter", "SOURCE_KEY"]
