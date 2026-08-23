"""Map free-text venue labels into :class:`~src.promos.schema.PromoKind`."""
from __future__ import annotations

from src.promos.schema import PromoKind

#: Ordered rules: first match wins.  More specific phrases before broad ones.
#: Needles are matched as whole words/phrases (not bare substrings), so
#: ``refer`` does not fire on ``referee`` / ``preferred``.
_RULES: tuple[tuple[tuple[str, ...], PromoKind], ...] = (
    # Contests first: their titles promise "bonus bets" and "$500,000", and
    # every rule below would read that as credit the operator holds.  Six of
    # BetMGM's twelve live cards were priced as bonus bets this way.
    (
        (
            "free-to-play", "free to play", "pick 'em", "pick ‘em", "pick’em",
            "pick em", "win a share", "share of $", "sweepstakes", "contest",
            "daily prizes", "prize pool", "leaderboard", "win bonus bets",
            "win a bonus bet", "chance to win", "win prizes",
            # Pool-scale figures are never one customer's credit.
            "$100,000 in", "$250,000 in", "$500,000 in", "$1,000,000 in",
            "$1 million", "$2 million", "$5 million", "$100k ", "$250k ", "$500k ",
        ),
        PromoKind.CONTEST,
    ),
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
