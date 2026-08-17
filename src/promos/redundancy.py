"""Intentional dual feeds for the same sportsbook's promotions.

Mirrors :mod:`src.redundancy` for odds: first-party promo adapters are primary;
TheLines ``tl_*`` tenants are secondaries that keep a brand covered when the
venue API/landing is empty or blocked.
"""
from __future__ import annotations

import re
from typing import Sequence

from src.promos.base import PromoSourceHealth
from src.promos.geo import merge_regions
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


def _fold_evidence(
    winner: PromoOffer, pairs: Sequence[tuple[PromoOffer, bool]]
) -> PromoOffer:
    """Carry a dropped row's jurisdiction evidence onto the kept row.

    A collision used to discard the loser whole — an aggregator blurb with
    dollars beat first-party terms carrying the brand's only state list, and
    the state evidence vanished. Each pair is ``(loser, full)``: a *full*
    fold carries eligible regions and terms; a narrow one carries only the
    ineligible list, so an ambiguously-attributed loser (one vague primary
    colliding with two different specific welcomes, or a loser reached
    through a drop chain) can only narrow the fail-closed reading — its
    eligible list describes *some* promo of the brand, not necessarily this
    one, and folding it in would confirm states this offer's copy never
    named.
    """
    if not pairs:
        return winner
    ineligible = merge_regions(
        winner.ineligible_regions, *(loser.ineligible_regions for loser, _ in pairs)
    )
    blocked = set(ineligible)
    full_losers = [loser for loser, full in pairs if full]
    eligible = [
        code
        for code in merge_regions(
            winner.eligible_regions, *(loser.eligible_regions for loser in full_losers)
        )
        if code not in blocked
    ]
    terms = winner.terms or next(
        (loser.terms for loser in full_losers if loser.terms), ""
    )
    if (
        eligible == winner.eligible_regions
        and ineligible == winner.ineligible_regions
        and terms == winner.terms
    ):
        return winner
    metadata = dict(winner.metadata)
    metadata["merged_evidence_from"] = ", ".join(
        dict.fromkeys(loser.source for loser, _ in pairs)
    )
    return winner.model_copy(
        update={
            "eligible_regions": eligible,
            "ineligible_regions": ineligible,
            "terms": terms,
            "metadata": metadata,
        }
    )


def prefer_primary_offers(offers: Sequence[PromoOffer]) -> list[PromoOffer]:
    """Keep first-party rows; drop TheLines duplicates when primary covers a brand.

    Secondary-only titles (exact game promos TheLines has and DraftKings API
    does not) are kept so coverage stays specific.  When a vague primary welcome
    soft-overlaps a concrete TheLines welcome, keep the more specific row —
    compared only to the overlapping primary, never the brand's best promo.
    Either way the dropped side's state evidence folds into the kept row.
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
    merged_from: dict[tuple[str, str], list[PromoOffer]] = {}
    # Who dropped whom — a merge recorded under a winner that is *itself*
    # dropped in a later collision must follow the drop chain to the row that
    # actually survives, or the first loser's evidence silently vanishes.
    dropped_to: dict[tuple[str, str], list[tuple[str, str]]] = {}

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
                dropped_to.setdefault(primary.dedup_key, []).append(offer.dedup_key)
            keep_secondary.add(offer.dedup_key)
            merged_from.setdefault(offer.dedup_key, []).extend(overlaps)
        else:
            drop_secondary.add(offer.dedup_key)
            dropped_to.setdefault(offer.dedup_key, []).append(
                best_overlap.dedup_key
            )
            merged_from.setdefault(best_overlap.dedup_key, []).append(offer)

    # Re-route merges whose recorded winner was itself dropped, and decide how
    # much of each loser may fold.  Full folds (eligible + terms) require an
    # unambiguous attribution: a direct collision whose loser feeds exactly
    # one surviving row.  A loser reached through a drop chain, or attached
    # to several winners, folds narrow (ineligible only).
    attachments: dict[tuple[str, str], list[tuple[PromoOffer, bool]]] = {}
    winners_per_loser: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for key, losers in merged_from.items():
        targets = {key}
        expanded: set[tuple[str, str]] = set()
        while True:
            movable = [t for t in targets if t in dropped_to and t not in expanded]
            if not movable:
                break
            for t in movable:
                expanded.add(t)
                targets.discard(t)
                targets.update(w for w in dropped_to[t] if w not in expanded)
        chained = targets != {key}
        for target in targets:
            for loser in losers:
                attachments.setdefault(target, []).append((loser, not chained))
                winners_per_loser.setdefault(loser.dedup_key, set()).add(target)
    merged_final: dict[tuple[str, str], list[tuple[PromoOffer, bool]]] = {}
    for target, pairs in attachments.items():
        entries: list[tuple[PromoOffer, bool]] = []
        for loser, direct in pairs:
            full = direct and len(winners_per_loser[loser.dedup_key]) == 1
            entries.append((loser, full))
        # The mirror of the many-winners rule: a winner absorbing *two*
        # different losers' full evidence is claiming to be two promos at
        # once — at most one can be it, so every fold demotes to narrow.
        if sum(1 for _, full in entries if full) > 1:
            entries = [(loser, False) for loser, _ in entries]
        merged_final[target] = entries

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
    if not merged_final:
        return out
    return [
        _fold_evidence(offer, merged_final.get(offer.dedup_key, ())) for offer in out
    ]


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
