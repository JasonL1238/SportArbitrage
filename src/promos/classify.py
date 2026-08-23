"""Map free-text venue labels into :class:`~src.promos.schema.PromoKind`."""
from __future__ import annotations

from src.promos.schema import PromoKind

#: Ordered rules: first match wins.  More specific phrases before broad ones.
#: Needles are matched as whole words/phrases (not bare substrings), so
#: ``refer`` does not fire on ``referee`` / ``preferred``.
_RULES: tuple[tuple[tuple[str, ...], PromoKind], ...] = (
    (("no sweat", "nosweat", "bet reset"), PromoKind.NO_SWEAT),
    (("profit boost",), PromoKind.PROFIT_BOOST),
    (("parlay boost", "sgp", "same game parlay"), PromoKind.PARLAY_BOOST),
    (("odds boost", "boosted odds", "enhanced odds"), PromoKind.ODDS_BOOST),
    # "Bet insurance" is the same shape: a losing first stake comes back as credit.
    (("risk free", "risk-free", "first bet back", "bet insurance", "insurance"), PromoKind.RISK_FREE),
    (("free bet", "free bets"), PromoKind.FREE_BET),
    (("bonus bet", "bonus bets"), PromoKind.BONUS_BET),
    (("deposit match", "matched deposit"), PromoKind.DEPOSIT_MATCH),
    # Referrals before signup: copy often says "sign up" inside a refer card.
    (("refer a friend", "referral", "refer-a-friend"), PromoKind.REFERRAL),
    (("welcome", "sign up", "signup", "new customer", "join now"), PromoKind.SIGNUP_BONUS),
    (("loyalty", "rewards", "rakeback", "vip"), PromoKind.LOYALTY),
)


def classify_kind(*parts: str | None) -> PromoKind:
    """Best-effort kind from headlines, categories, and descriptions."""
    import re

    blob = " ".join(p for p in parts if p).lower()
    if not blob:
        return PromoKind.OTHER
    for needles, kind in _RULES:
        for needle in needles:
            if " " in needle or "-" in needle:
                if needle in blob:
                    return kind
            elif re.search(rf"\b{re.escape(needle)}\b", blob):
                return kind
    return PromoKind.OTHER


__all__ = ["classify_kind"]
