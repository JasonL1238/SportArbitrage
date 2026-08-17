"""Max-profit usage guidance for scraped promotions."""
from __future__ import annotations

from src.promos.schema import PromoKind, PromoOffer


def build_usage_guidance(offer: PromoOffer) -> str:
    """Return a short, actionable playbook for the dashboard detail panel."""
    parts: list[str] = []
    summary = offer.summary or offer.title
    # ``reward_type`` is the stored vocabulary (``bonus_bets``, ``site_credit``,
    # …) and is what the branches below match on.  ``reward`` is the same value
    # spelled for a human and is only ever printed: matching against it silently
    # never fired, because every underscored label loses its underscore here.
    reward_type = offer.reward_type or ""
    reward = reward_type.replace("_", " ")
    amount = offer.bonus_amount
    min_dep = offer.min_deposit
    wagering = offer.wagering_requirement
    min_odds = offer.min_odds

    if offer.kind in {PromoKind.BONUS_BET, PromoKind.FREE_BET} or reward_type in {
        "bonus_bets",
        "free_bet",
    }:
        stake = f"${amount:,.10g} " if amount is not None else ""
        parts.append(
            f"Treat the {stake}{reward or 'bonus/free bet'} as stake-not-returned credit. "
            "Bet the full amount on a liquid major-market side (moneyline/spread), "
            "then hedge the opposite outcome at another book for a locked profit."
        )
        parts.append(
            "Prefer American odds near even money or slight favorites so the hedge stake "
            "stays manageable; avoid parlays and low-liquidity props."
        )
    elif offer.kind is PromoKind.DEPOSIT_MATCH or reward_type == "site_credit":
        match_bits = summary
        if amount is not None:
            match_bits = f"up to ${amount:,.10g} match"
        parts.append(
            f"Deposit only what you need to unlock the match ({match_bits}"
            + (f"; min deposit ${min_dep:,.10g}" if min_dep is not None else "")
            + "). Convert site credit through qualifying bets, then hedge off-book."
        )
        if wagering:
            parts.append(
                f"Clear the {wagering} wagering requirement with mid-odds, two-way markets; "
                "low odds burn credit inefficiently and long odds raise void/risk."
            )
        else:
            parts.append(
                "Check rollover before depositing — if playthrough is steep, size the "
                "deposit down to the smallest amount that still unlocks usable credit."
            )
    elif offer.kind in {PromoKind.NO_SWEAT, PromoKind.RISK_FREE} or reward_type in {
        "no_sweat",
        "risk_free",
    }:
        cap = f"${amount:,.10g}" if amount is not None else "the stated max"
        parts.append(
            f"Stake up to {cap} on a single qualifying bet. If it loses, convert the "
            "refund/bonus bet with a hedge at a second book for near-locked EV."
        )
        parts.append(
            "If the first bet wins, you keep the profit — so pick a real edge or a "
            "hedgeable major market, not a long-shot prop."
        )
    elif offer.kind in {
        PromoKind.ODDS_BOOST,
        PromoKind.PROFIT_BOOST,
        PromoKind.PARLAY_BOOST,
    } or reward_type == "boost":
        parts.append(
            "Compare the boosted price to a sharp/no-vig fair line. Only use the boost "
            "when boosted EV exceeds the unboosted market after stake limits."
        )
        parts.append(
            "For profit boosts, calculate boosted profit vs hedge cost; skip if the "
            "boosted side cannot be laid off cleanly elsewhere."
        )
    elif offer.kind is PromoKind.SIGNUP_BONUS:
        if amount is not None or offer.is_specific:
            parts.append(
                f"Use the concrete offer ({summary}). Complete only the minimum qualifying "
                "action (bet/deposit) required, then convert bonus value via hedgeable "
                "two-way markets."
            )
        else:
            parts.append(
                "Offer mechanics are incomplete on the public page — open the terms, "
                "confirm reward type and qualifying stake, then convert with a hedge."
            )
    elif offer.kind is PromoKind.REFERRAL:
        parts.append(
            "Referrals usually pay after the friend qualifies. Only use if the friend "
            "value exceeds your effort; do not create circular accounts."
        )
    else:
        if offer.is_specific:
            parts.append(
                f"Follow the published mechanics ({summary}). Qualifying bets should be "
                "hedgeable majors; avoid parlays unless the promo requires them."
            )
        else:
            parts.append(
                "Public copy is vague. Open the offer URL, extract stake/reward/odds "
                "rules, then convert any bonus credit with a two-book hedge."
            )

    if min_odds:
        parts.append(f"Respect the minimum odds requirement ({min_odds}).")
    if offer.eligible_regions:
        parts.append(
            "Eligible regions observed/stated: " + ", ".join(offer.eligible_regions) + "."
        )
    if offer.ineligible_regions:
        parts.append(
            "Explicitly excluded: " + ", ".join(offer.ineligible_regions) + "."
        )
    if offer.eligibility_notes:
        parts.append(offer.eligibility_notes.rstrip(".") + ".")
    if offer.requires_login:
        parts.append("Full terms may require login — verify before depositing.")

    return " ".join(parts)


def apply_usage_guidance(offers: list[PromoOffer]) -> list[PromoOffer]:
    out: list[PromoOffer] = []
    for offer in offers:
        if offer.usage_guidance:
            out.append(offer)
            continue
        out.append(offer.model_copy(update={"usage_guidance": build_usage_guidance(offer)}))
    return out


__all__ = ["apply_usage_guidance", "build_usage_guidance"]
