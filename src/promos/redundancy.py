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


def _specificity_score(offer: PromoOffer) -> tuple[int, int, int]:
    """Higher is better — used when primary/secondary welcome rows collide.

    Length is only a tie-break among already-specific rows so a longer vague
    TheLines title cannot displace a shorter vague first-party welcome.
    """
    specific = int(bool(offer.is_specific))
    amount = int(offer.bonus_amount is not None)
    length = len(offer.summary or offer.title or "") if specific or amount else 0
    return (specific, amount, length)


def _is_welcomeish(title: str) -> bool:
    return bool(re.search(r"welcome|sign.?up|bonus code|new customer", title, re.I))


def _overlapping_primaries(
    secondary: PromoOffer, primaries: Sequence[PromoOffer]
) -> list[PromoOffer]:
    """Primaries that collide with this secondary title (exact or soft welcome)."""
    snorm = _norm_title(secondary.title)
    out: list[PromoOffer] = []
    for primary in primaries:
        pnorm = _norm_title(primary.title)
        if snorm == pnorm:
            out.append(primary)
            continue
        # Soft overlap only against other welcome-ish primaries — never a
        # game boost just because both share a digit token.
        if (
            _is_welcomeish(secondary.title)
            and _is_welcomeish(primary.title)
            and _loose_overlap(snorm, pnorm)
        ):
            out.append(primary)
    return out


def prefer_primary_offers(offers: Sequence[PromoOffer]) -> list[PromoOffer]:
    """Keep first-party rows; drop TheLines duplicates when primary covers a brand.

    Secondary-only titles (exact game promos TheLines has and DraftKings API
    does not) are kept so coverage stays specific.  When a vague primary welcome
    soft-overlaps a concrete TheLines welcome, keep the more specific row —
    compared only to the overlapping primary, never the brand's best promo.
    """
    primary_by_brand: dict[str, list[PromoOffer]] = {}
    for offer in offers:
        if offer.source.startswith("tl_"):
            continue
        brand = brand_key(offer.source)
        primary_by_brand.setdefault(brand, []).append(offer)

    drop_primary: set[tuple[str, str]] = set()
    keep_secondary: set[tuple[str, str]] = set()
    drop_secondary: set[tuple[str, str]] = set()

    for offer in offers:
        if not offer.source.startswith("tl_"):
            continue
        brand = brand_key(offer.source)
        primaries = primary_by_brand.get(brand) or []
        if not primaries:
            continue
        overlaps = _overlapping_primaries(offer, primaries)
        if not overlaps:
            continue
        # Compare only against overlapping primaries.
        best_overlap = max(overlaps, key=_specificity_score)
        if _specificity_score(offer) > _specificity_score(best_overlap):
            for primary in overlaps:
                drop_primary.add(primary.dedup_key)
            keep_secondary.add(offer.dedup_key)
        else:
            drop_secondary.add(offer.dedup_key)

    out: list[PromoOffer] = []
    for offer in offers:
        if offer.source.startswith("tl_"):
            if offer.dedup_key in drop_secondary:
                continue
            brand = brand_key(offer.source)
            if brand not in primary_by_brand:
                out.append(offer)
                continue
            if offer.dedup_key in keep_secondary:
                out.append(offer)
                continue
            # Non-overlapping secondary titles stay.
            if not _overlapping_primaries(offer, primary_by_brand[brand]):
                out.append(offer)
            continue
        if offer.dedup_key in drop_primary:
            continue
        out.append(offer)
    return out


_BRAND_NOISE = re.compile(
    r"^(?:fanduel|draftkings|betmgm|caesars|bet365|hardrock|fanatics|bovada)+"
)


def _welcome_core(norm: str) -> str:
    """Strip brand prefixes / amount digits so welcome titles collide."""
    text = _BRAND_NOISE.sub("", norm)
    text = re.sub(r"\d+", "", text)
    for noise in ("bonuscode", "sportsbook", "promo", "offer", "bonus"):
        text = text.replace(noise, "")
    return text


def _loose_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    # Shared 8+ char digit/money token
    for token in re.findall(r"\d{2,}", a):
        if token in b:
            return True
    # Welcome cores: "FanDuel Welcome Offer" vs "Welcome Offer $150"
    ca, cb = _welcome_core(a), _welcome_core(b)
    if ca and cb and (ca in cb or cb in ca or ca == cb):
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
