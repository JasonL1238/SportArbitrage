"""Intentional dual feeds for the same sportsbook's promotions.

Mirrors :mod:`src.redundancy` for odds: first-party promo adapters are primary;
TheLines ``tl_*`` tenants are secondaries that keep a brand covered when the
venue API/landing is empty or blocked.
"""
from __future__ import annotations

import re
from typing import Sequence

from src.promos.base import PromoSourceHealth
from src.promos.schema import PromoOffer

#: ``(primary, secondary)`` — primary is the book-direct adapter when one exists.
#: Brands with only a TheLines tenant still appear here so brand folding works.
REDUNDANT_PAIRS: tuple[tuple[str, str], ...] = (
    ("fanduel", "tl_fanduel"),
    ("draftkings", "tl_draftkings"),
    ("betmgm", "tl_betmgm"),
    ("caesars", "tl_caesars"),
    ("bet365", "tl_bet365"),
    ("hardrock", "tl_hardrock"),
    ("fanatics", "tl_fanatics"),
)

#: Brand key for a ``tl_*`` secondary (or the key itself for primaries).
BRAND_FOR_SOURCE: dict[str, str] = {}
for _primary, _secondary in REDUNDANT_PAIRS:
    BRAND_FOR_SOURCE[_primary] = _primary
    BRAND_FOR_SOURCE[_secondary] = _primary


def brand_key(source_key: str) -> str:
    """Map ``tl_betmgm`` → ``betmgm``; pass through unknown keys."""
    if source_key in BRAND_FOR_SOURCE:
        return BRAND_FOR_SOURCE[source_key]
    if source_key.startswith("tl_"):
        return source_key[3:]
    return source_key


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def prefer_primary_offers(offers: Sequence[PromoOffer]) -> list[PromoOffer]:
    """Keep first-party rows; drop TheLines duplicates when primary covers a brand.

    Secondary-only titles (exact game promos TheLines has and DraftKings API
    does not) are kept so coverage stays specific.
    """
    primary_norms: dict[str, set[str]] = {}
    for offer in offers:
        if offer.source.startswith("tl_"):
            continue
        brand = brand_key(offer.source)
        primary_norms.setdefault(brand, set()).add(_norm_title(offer.title))

    out: list[PromoOffer] = []
    for offer in offers:
        if not offer.source.startswith("tl_"):
            out.append(offer)
            continue
        brand = brand_key(offer.source)
        norms = primary_norms.get(brand)
        if not norms:
            # No first-party catalog — keep the whole TheLines set for the brand.
            out.append(offer)
            continue
        # Drop near-duplicates; keep secondary titles primary lacks.
        if _norm_title(offer.title) in norms:
            continue
        # Soft overlap: secondary welcome vs primary welcome with shared tokens.
        if any(
            _loose_overlap(_norm_title(offer.title), p) for p in norms
        ) and re.search(r"welcome|sign.?up|bonus code|new customer", offer.title, re.I):
            continue
        out.append(offer)
    return out


def _loose_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    # Shared 8+ char digit/money token
    for token in re.findall(r"\d{2,}", a):
        if token in b:
            return True
    return False


def brand_coverage(
    health: Sequence[PromoSourceHealth],
) -> tuple[int, int, list[str]]:
    """Return ``(ok_brands, total_brands, ok_brand_keys)`` after folding ``tl_*``.

    A brand is covered when its primary **or** any secondary health row is OK.
    Sources that are not in a redundant pair count as their own brand.
    """
    by_brand: dict[str, bool] = {}
    for row in health:
        brand = brand_key(row.source_key)
        by_brand[brand] = by_brand.get(brand, False) or bool(row.ok)
    ok_keys = sorted(k for k, ok in by_brand.items() if ok)
    return len(ok_keys), len(by_brand), ok_keys


__all__ = [
    "BRAND_FOR_SOURCE",
    "REDUNDANT_PAIRS",
    "brand_coverage",
    "brand_key",
    "prefer_primary_offers",
]
