"""betPARX promotions from the Playtech lobby's public promotion configuration.

``GET {base}/initialResources/promotionsConfiguration/en_US`` is the JSON the
``/promotions`` page renders from — internal name, headline, product, guest
visibility, a scheduler window and the ids of two web-content blocks per
promotion — and ``GET {base}/webContent/en_US_{id}`` returns each block as an
HTML fragment (the "learn more" copy and the full terms).  No account is
involved; both answer a plain client.

The host is the licence.  ``pa.betparx.com`` and ``nj.betparx.com`` are separate
lobbies with their own catalogues, so the adapter is built per state from
:attr:`src.jurisdictions.PromoRoute.betparx_url` and stamps that state as the
offer's eligible region — the same footing as the BetRivers landing, not a
nationwide catalogue whose eligibility has to be read out of the terms.

Measured 2026-08-23 from a Philadelphia egress: six promotions, four of them
sportsbook (a "100% Bet Insurance up to $50" new-user offer, the Eagles +50
weekly boosts), one casino, one promo-code stub.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import strip_tags
from src.promos.schema import PromoKind, PromoOffer
from src.raw_store import RawResponse
from src.sources._common import SourceClient, envelope_source, latest_per_endpoint
from src.sources.guards import FormatChangeError, require_mapping

log = logging.getLogger(__name__)

SOURCE_KEY = "betparx_kambi"
DEFAULT_BASE_URL = "https://pa.betparx.com"
DEFAULT_REGION = "PA"
CONFIG_ENDPOINT = "promotions-configuration"
CONTENT_ENDPOINT_PREFIX = "webcontent-"
#: Products the sportsbook slate cares about.  Playtech leaves the field blank
#: on site-wide tiles (the promo-code entry), which are kept and classified on
#: their copy rather than dropped for having no product.
SPORTS_PRODUCTS = frozenset({"sportsbook", ""})
_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    return _WS.sub(" ", strip_tags(fragment or "").replace("\xa0", " ")).strip()


def _window(promo: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    """The first ``between`` scheduler condition, as two aware datetimes."""
    for group in promo.get("scheduler") or ():
        for item in group if isinstance(group, list) else ():
            for setting in item.get("schedulerItemSettings") or ():
                if setting.get("conditionType") != "between":
                    continue
                parts = str(setting.get("conditionDuration", "")).split()
                if len(parts) != 2:
                    continue
                try:
                    start, end = (
                        datetime.fromisoformat(p.replace("Z", "+00:00")).astimezone(UTC)
                        for p in parts
                    )
                except ValueError:
                    continue
                return start, end
    return None, None


class BetParxPromoAdapter:
    """Collects one betPARX state lobby's public promotion catalogue."""

    def __init__(
        self,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        region: str = DEFAULT_REGION,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_details: int = 12,
    ) -> None:
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.region = region.upper()
        self.max_details = max_details
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=0.35,
            headers={"Referer": f"{self.base_url}/promotions"},
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self) -> list[RawResponse]:
        config = self._http.get(
            f"{self.base_url}/initialResources/promotionsConfiguration/en_US",
            endpoint=CONFIG_ENDPOINT,
        )
        raws = [config]
        wanted = self._content_ids_to_fetch(self._sports_promotions(self._promotions(config)))
        for content_id in wanted[: self.max_details]:
            try:
                raws.append(
                    self._http.get(
                        f"{self.base_url}/webContent/en_US_{content_id}",
                        endpoint=f"{CONTENT_ENDPOINT_PREFIX}{content_id}",
                        expect_json=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one block, not the catalogue
                log.warning("%s: web content %s failed: %s", self._source_key, content_id, exc)
        return raws

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        latest = latest_per_endpoint(raws)
        config = next((r for r in latest if r.endpoint == CONFIG_ENDPOINT), None)
        if config is None:
            return outcome
        source = envelope_source((config,), fallback=self._source_key)
        contents = {
            r.endpoint[len(CONTENT_ENDPOINT_PREFIX):]: _text(r.body or "")
            for r in latest
            if r.endpoint.startswith(CONTENT_ENDPOINT_PREFIX)
        }
        for promo in self._promotions(config):
            product = str(promo.get("product") or "").lower()
            if product not in SPORTS_PRODUCTS:
                outcome.skipped[f"product:{product}"] += 1
                continue
            if promo.get("isEnabledForGuest") is False:
                outcome.skipped["login_only"] += 1
                continue
            headline = _text(str(promo.get("description") or ""))
            name = _text(str(promo.get("name") or ""))
            title = headline or name
            if not title:
                outcome.skipped["untitled"] += 1
                continue
            detail_id, terms_id = self._content_ids(promo)
            detail = contents.get(detail_id, "")
            terms = contents.get(terms_id, "")
            # Headline and "learn more" copy name the offer; the terms block
            # is a whole T&C document that mentions every product (a sign-up
            # offer's terms read "profit boost" before they read "insurance").
            kind = PromoKind.OTHER
            for copy in (title, detail, terms):
                kind = classify_kind(copy)
                if kind is not PromoKind.OTHER:
                    break
            if kind is PromoKind.OTHER and re.search(r"new users?|sign up", f"{title} {detail}", re.I):
                kind = PromoKind.SIGNUP_BONUS
            starts_at, ends_at = _window(promo)
            page = str((promo.get("tileSettings") or {}).get("imageActionPageURL") or "")
            raw_ref = config.ref
            outcome.offers.append(
                PromoOffer(
                    source=source,
                    offer_id=str(promo.get("id") or promo.get("seoFriendlyName") or name),
                    kind=kind,
                    title=title[:200],
                    description=(detail or name)[:4000],
                    terms=terms[:4000],
                    url=f"{self.base_url}{page}" if page.startswith("/") else f"{self.base_url}/promotions",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    observed_at=config.fetched_at,
                    raw_ref=raw_ref,
                    raw_kind=product or "site",
                    product="sportsbook",
                    eligible_regions=[self.region],
                    metadata={"name": name, "seo": str(promo.get("seoFriendlyName") or "")},
                )
            )
        return outcome

    # ── helpers ──────────────────────────────────────────────────────────────

    def _promotions(self, config: RawResponse) -> list[Mapping[str, Any]]:
        try:
            payload = json.loads(config.body or "")
        except ValueError as exc:
            raise FormatChangeError(f"{self._source_key}: configuration is not JSON: {exc}") from exc
        payload = require_mapping(payload, source=self._source_key, endpoint=CONFIG_ENDPOINT)
        promotions = payload.get("promotions")
        if not isinstance(promotions, Mapping):
            raise FormatChangeError(
                f"{self._source_key}: configuration carries no 'promotions' mapping"
            )
        return [p for p in promotions.values() if isinstance(p, Mapping)]

    def _sports_promotions(self, promotions: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [
            p
            for p in promotions
            if str(p.get("product") or "").lower() in SPORTS_PRODUCTS
            and p.get("isEnabledForGuest") is not False
        ]

    @classmethod
    def _content_ids_to_fetch(cls, promotions: Sequence[Mapping[str, Any]]) -> list[str]:
        """Every public block the promotions name, once each, in first-seen order.

        Three weekly boosts share one terms block and the promo-code stub has
        none; fetching per promotion asked for that block three times and for
        an empty id once.
        """
        seen: list[str] = []
        for promo in promotions:
            for content_id in cls._content_ids(promo):
                if content_id and content_id not in seen:
                    seen.append(content_id)
        return seen

    @staticmethod
    def _content_ids(promo: Mapping[str, Any]) -> tuple[str, str]:
        detail = str(promo.get("detailedDescriptionWebcontentId") or "")
        terms = str(promo.get("termsConditionsWebcontentId") or "")
        # ``TERMS_FROM_IMS`` is Playtech's marker for terms served only inside
        # the account system; there is no public block behind it.
        if terms == "TERMS_FROM_IMS":
            terms = ""
        return detail, terms

    def close(self) -> None:
        self._http.close()


__all__ = ["BetParxPromoAdapter", "SOURCE_KEY"]
