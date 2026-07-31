"""DraftKings Sportsbook promotions from the public promotions query API.

``POST https://api.draftkings.com/en/api/promotions/v3/promotions/query`` is the
same JSON the logged-out ``/promos`` page renders.  No account is required for
the catalog; some term bodies still say "log in to view".
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint, parse_iso_time
from src.sources.guards import FormatChangeError, require_mapping

log = logging.getLogger(__name__)

SOURCE_KEY = "draftkings"
DEFAULT_URL = "https://api.draftkings.com/en/api/promotions/v3/promotions/query"
DEFAULT_QUERY: dict[str, Any] = {
    "productName": "Sportsbook",
    "filterByProduct": False,
    "zones": {"zoneName": "UniversalPromoPage"},
    "siteExperience": "US-SB",
}
ENDPOINT = "promotions-query"
SPORTS_PRODUCTS = frozenset({"sportsbook", "sports", "racing", "predict"})


class DraftKingsPromoAdapter:
    """Collects DraftKings' public UniversalPromoPage catalog."""

    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        url: str = DEFAULT_URL,
        sports_only: bool = True,
    ) -> None:
        self._source_key = source_key
        self.url = url
        self.sports_only = sports_only
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=0.35,
            headers={
                "Origin": "https://sportsbook.draftkings.com",
                "Referer": "https://sportsbook.draftkings.com/promos",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        raw = self._http.post(
            self.url,
            endpoint=ENDPOINT,
            json_body=DEFAULT_QUERY,
            record_params={"zone": "UniversalPromoPage"},
        )
        return [raw]

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        for raw in latest_per_endpoint(raws):
            if raw.endpoint != ENDPOINT:
                outcome.skipped["foreign_endpoint"] += 1
                continue
            self._parse_one(raw, outcome)
        return outcome

    def _parse_one(self, raw: RawResponse, outcome: PromoParseOutcome) -> None:
        source = envelope_source((raw,), fallback=self.source_key)
        try:
            payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
        except FormatChangeError as exc:
            outcome.reject(source, "not_object", str(exc))
            return
        zones = payload.get("zones")
        if not isinstance(zones, list):
            outcome.reject(source, "missing_zones", "payload.zones is not a list")
            return
        for zone in zones:
            if not isinstance(zone, Mapping):
                outcome.skipped["bad_zone"] += 1
                continue
            promotions = zone.get("promotions")
            if not isinstance(promotions, list):
                outcome.skipped["zone_without_promotions"] += 1
                continue
            for item in promotions:
                if not isinstance(item, Mapping):
                    outcome.skipped["bad_promotion"] += 1
                    continue
                offer = self._offer_from(item, raw=raw, source=source)
                if offer is None:
                    outcome.skipped["unusable_promotion"] += 1
                    continue
                if self.sports_only and offer.product and offer.product.lower() not in SPORTS_PRODUCTS:
                    # Casino-only cards stay out of the sports free-EV board.
                    outcome.skipped["non_sports_product"] += 1
                    continue
                if offer.kind.value == "other" and (offer.product or "").lower() == "casino":
                    outcome.skipped["casino_other"] += 1
                    continue
                outcome.offers.append(offer)

    def _offer_from(
        self, item: Mapping[str, Any], *, raw: RawResponse, source: str
    ) -> PromoOffer | None:
        merch = item.get("merchandisingData")
        if not isinstance(merch, Mapping):
            merch = {}
        title = _text(merch.get("promotionHeadline")) or _text(item.get("category"))
        if not title:
            return None
        description = _text(merch.get("promotionDescription")) or _text(
            merch.get("loggedOutDescription")
        )
        terms = _text(merch.get("terms"))
        category = _text(item.get("category"))
        product = _text(item.get("productName")) or _text(item.get("verticalGroup"))
        promo_id = item.get("promotionId")
        public_id = _text(item.get("publicPromotionId"))
        offer_id = str(promo_id) if promo_id is not None else public_id or title
        starts = parse_iso_time(item.get("startDate"))
        ends = parse_iso_time(item.get("expirationDate"))
        requires_login = bool(merch.get("displayToLoggedOutUsers") is False) or (
            terms.lower().startswith("please log in") if terms else False
        )
        # Do not feed terms into classify — they often contain "sign up to view
        # terms" and would mis-bucket referrals / boosts as signup bonuses.
        kind = classify_kind(category, title, description)
        user = item.get("userData") if isinstance(item.get("userData"), Mapping) else {}
        action = user.get("promotionActionButton") if isinstance(user, Mapping) else None
        url = None
        if isinstance(action, Mapping):
            url = _text(action.get("webRedirectUrl"))
        if not url:
            url = f"https://sportsbook.draftkings.com/promos#{public_id or offer_id}"
        return PromoOffer(
            source=source,
            offer_id=offer_id,
            kind=kind,
            title=title,
            description=description,
            terms=terms[:4000],
            url=url,
            starts_at=starts,
            ends_at=ends,
            observed_at=raw.fetched_at if isinstance(raw.fetched_at, datetime) else raw.fetched_at,
            raw_ref=raw.ref,
            raw_kind=category,
            product=product,
            requires_login=requires_login,
            metadata={
                "public_promotion_id": public_id,
                "is_opt_in": bool(item.get("isOptInPromotion")),
            },
        )

    def close(self) -> None:
        self._http.close()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


__all__ = ["DraftKingsPromoAdapter", "SOURCE_KEY"]
