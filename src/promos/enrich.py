"""Extract concrete offer mechanics from promo free text."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.promos.geo import merge_regions, parse_eligibility
from src.promos.schema import PromoOffer

_MONEY = r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|\d+(?:\.\d+)?)"

#: The reward amount must be dollar-marked: "get 30% profit boost" and
#: "receive 3 bonus bets" fabricated $30 / $3 through the optional "$".
_MONEY_STRICT = r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|\d+(?:\.\d+)?)"

_BET_GET = re.compile(
    rf"bet\s+{_MONEY}\s*(?:or\s+more)?\s*[,:]?\s*"
    rf"(?:and\s+|to\s+)?(?:get|receive|earn)\s+(?:up\s+to\s+)?{_MONEY_STRICT}",
    re.I,
)
_GET_IN_BONUS = re.compile(
    rf"(?:get|receive|earn)\s+(?:up\s+to\s+)?{_MONEY_STRICT}\s+"
    r"(?:in\s+)?(bonus\s+bets?|free\s+bets?|site\s+credit|casino\s+credits?)",
    re.I,
)
_PCT_MATCH = re.compile(
    rf"(?<![\d.])(\d+)\s*%\s*(?:deposit\s+)?match(?:\s+up\s+to\s+{_MONEY})?",
    re.I,
)
#: A bare "up to <number>" is money only when it is not a percentage —
#: "Up to 100% Parlay Boosts" stored bonus_amount=100 dollars.
_UP_TO = re.compile(rf"up\s+to\s+{_MONEY_STRICT}(?!\s*%|\s*percent\b|\d|\.\d)", re.I)
_MIN_DEPOSIT = re.compile(rf"min(?:imum)?\s+deposit\s+(?:of\s+)?{_MONEY}", re.I)
_MIN_ODDS = re.compile(
    r"min(?:imum)?\s+odds\s+(?:of\s+)?([+-]?\d{2,4}|\d+(?:\.\d+)?)",
    re.I,
)
_WAGERING = re.compile(
    r"(\d+\s*x|\d+x)\s*(?:wagering|playthrough|rollover)?|"
    r"(?:wagering|playthrough|rollover)\s*(?:requirement\s*)?(?:of\s*)?(\d+\s*x|\d+x)",
    re.I,
)
_PROFIT_BOOST = re.compile(r"(?<![\d.])(\d+)\s*%\s*(?:profit\s+)?boosts?\b", re.I)

_MONTH_INDEX = {
    name: index
    for index, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
#: Absolute offer-end dates only.  Relative durations ("bonus bets expire
#: within seven days") describe the credit's usage window, not the offer's
#: end, and must not match.  Word boundaries on the verb keep "weekends
#: 9/6/2026" and "Legends September 1" from manufacturing an end date, and
#: the day's lookahead keeps "ends September 2026" from reading the year's
#: first two digits as a day.
_ENDS_AT = re.compile(
    r"\b(?:ends?|expires?|valid\s+(?:through|thru|until))\b\s*:?\s*(?:on\s+)?"
    r"(?:(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(?P<day>\d{1,2})(?!\d)"
    r"(?:st|nd|rd|th)?(?:,?\s*(?P<year>\d{4}))?"
    r"|(?P<mm>\d{1,2})/(?P<dd>\d{1,2})/(?P<yyyy>\d{4}))",
    re.I,
)
#: A stated start date makes a year-less end date decidable: "start on
#: August 13 … end on August 15" seen on the 16th is an *elapsed* window, not
#: next year's.
_STARTS_AT = re.compile(
    r"\b(?P<verb>starts?|begins?|began|started)\b\s*:?\s*(?:on\s+)?"
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(?P<day>\d{1,2})(?!\d)",
    re.I,
)
#: Past-tense start verbs: "started December 20" seen in August means *last*
#: December when the anchor-year date would be in the future — a window that
#: straddled the year boundary and has already run.  Present-tense "starts
#: December 20" genuinely means the upcoming December.
_PAST_START_VERBS = frozenset({"began", "started"})


def extract_ends_at(text: str, observed_at: datetime | None) -> datetime | None:
    """A stated absolute end date in promo copy, or None.

    Year-less dates take the observation year, rolling forward one year when
    the result would predate the observation — "ends January 5" seen in
    August means next January — *unless* the same copy states a start date
    that is also past, which makes the window an elapsed one and the past
    end date the honest reading. End-of-day so an offer ending today still
    plans all day.
    """
    anchor = observed_at if observed_at is not None else datetime.now(UTC)
    for match in _ENDS_AT.finditer(text or ""):
        # An impossible date ("ends June 31") skips to the next stated one
        # rather than discarding a genuine later date in the same copy.
        try:
            if match.group("month"):
                month = _MONTH_INDEX[match.group("month").lower()]
                day = int(match.group("day"))
                year_text = match.group("year")
                year = int(year_text) if year_text else anchor.year
                when = datetime(year, month, day, 23, 59, tzinfo=UTC)
                if not year_text and anchor.tzinfo is not None:
                    if when < anchor:
                        # Elapsed evidence must cohere: a window's start
                        # cannot follow its own end.  "started December 20
                        # and ends January 5" seen late December is a LIVE
                        # straddling run — the past start is real, but it
                        # postdates the candidate past end, so that end
                        # belongs to *next* year.
                        starts = _same_sentence_starts(text, anchor, match)
                        if not any(s <= anchor and s <= when for s in starts):
                            when = datetime(year + 1, month, day, 23, 59, tzinfo=UTC)
                    else:
                        # Mirror: "started December 20 and ends December 30"
                        # seen in January — the past-tense start proves last
                        # December, so the future-looking end belongs to last
                        # year too.  Only shift when the shifted end still
                        # follows the inferred start ("started December 20
                        # and ends March 1" is a live straddling window).
                        last_year = [
                            s
                            for s in _same_sentence_starts(text, anchor, match)
                            if s.year == anchor.year - 1
                        ]
                        if last_year:
                            try:
                                shifted = datetime(
                                    year - 1, month, day, 23, 59, tzinfo=UTC
                                )
                            except ValueError:
                                shifted = None
                            if shifted is not None and all(
                                shifted >= s for s in last_year
                            ):
                                when = shifted
            else:
                when = datetime(
                    int(match.group("yyyy")),
                    int(match.group("mm")),
                    int(match.group("dd")),
                    23,
                    59,
                    tzinfo=UTC,
                )
        except ValueError:
            continue
        return when
    return None


def _same_sentence_starts(
    text: str, anchor: datetime, end_match: re.Match[str]
) -> list[datetime]:
    """Start dates sharing the end date's sentence, with inferred years.

    Same-sentence only: a terms blob often describes several promos, and a
    different promo's "started August 1" must not stamp this one's future
    end date as elapsed.  The start may sit on either side of the end
    ("ends August 15, having started August 13"), and abbreviation dots
    ("9:00 a.m. ET", "U.S. customers") are not sentence boundaries.
    A past-tense start whose anchor-year date lies in the future belongs to
    *last* year ("started December 20" seen in January) and is returned as
    such; a present-tense one means the upcoming date.
    """
    starts: list[datetime] = []
    for match in _STARTS_AT.finditer(text or ""):
        if match.end() <= end_match.start():
            between = text[match.end() : end_match.start()]
        elif match.start() >= end_match.end():
            between = text[end_match.end() : match.start()]
        else:
            continue
        # Multi-dot abbreviations (a.m., p.m., U.S., e.g.), common single-dot
        # ones, and decimal points ("1.5x boost", "odds of 1.50", "$0.50")
        # are not sentence boundaries.
        cleaned = re.sub(r"\b(?:[A-Za-z]\.){2,}", " ", between)
        cleaned = re.sub(
            r"\b(?:approx|est|no|vs|etc|inc|min|max)\.", " ", cleaned, flags=re.I
        )
        cleaned = re.sub(r"(?<=\d)\.(?=\d)", "", cleaned)
        if re.search(r"[.!?]", cleaned):
            continue
        try:
            started = datetime(
                anchor.year,
                _MONTH_INDEX[match.group("month").lower()],
                int(match.group("day")),
                tzinfo=UTC,
            )
        except ValueError:
            continue
        if started > anchor and match.group("verb").lower() in _PAST_START_VERBS:
            # "started December 20 … ends January 5" seen in August: the
            # past tense proves the start was *last* December, so the
            # year-straddling window has already run.
            try:
                started = started.replace(year=anchor.year - 1)
            except ValueError:  # Feb 29 into a non-leap year
                continue
        starts.append(started)
    return starts


_VAGUE_TITLE = re.compile(
    r"^(?:welcome(?:\s+offer|\s+bonus)?|sign[- ]?up(?:\s+bonus|\s+offer)?|"
    r"new\s+(?:customer|player)\s+(?:bonus|offer)|promotions?|bonus|"
    r"daily\s+boosts?|odds\s+boosts?|boost(?:s)?(?:\s+hub)?)$",
    re.I,
)

#: A sentence that negates rewards ("… are not valid", "do not count")
#: names them only to exclude them.
_NEGATED_REWARD_SENTENCE = re.compile(
    r"\b(?:not\s+valid|not\s+eligible|ineligible|exclud\w*|"
    r"do(?:es)?\s+not\s+(?:count|qualify))\b",
    re.I,
)

_REWARD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bonus\s+bets?", re.I), "bonus_bets"),
    (re.compile(r"free\s+bets?", re.I), "free_bet"),
    (re.compile(r"site\s+credit|bonus\s+funds?|casino\s+credits?", re.I), "site_credit"),
    (re.compile(r"cash\s+bonus|\bcash\s+back\b", re.I), "cash"),
    (re.compile(r"\d+\s*%\s*(?:profit\s+)?boosts?\b|profit\s+boost|odds\s+boost|parlay\s+boost", re.I), "boost"),
    (re.compile(r"no\s*sweat|bet\s+reset", re.I), "no_sweat"),
    (re.compile(r"risk[- ]?free", re.I), "risk_free"),
)


def _money(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _blob(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def is_vague_text(*parts: str | None) -> bool:
    """True when copy lacks concrete $/%/reward mechanics.

    Reward words alone (``free bets``, ``cash bonus``) are still vague — need
    a dollar amount or percentage tied to the offer.
    """
    text = _blob(*parts).strip()
    if not text:
        return True
    if _VAGUE_TITLE.match(text.strip()):
        return True
    has_money = bool(re.search(r"\$\s*\d", text))
    has_pct = bool(re.search(r"\d+\s*%", text))
    return not (has_money or has_pct)


def extract_mechanics(*parts: str | None) -> dict[str, Any]:
    """Pull structured fields + a concrete summary line from free text."""
    text = _blob(*parts)
    out: dict[str, Any] = {
        "summary": "",
        "bonus_amount": None,
        "min_deposit": None,
        "min_odds": None,
        "wagering_requirement": None,
        "reward_type": "",
        "is_specific": False,
    }
    if not text:
        return out

    # The reward is read title-first, and a body sentence that *negates*
    # rewards never votes: the live DK profit boost stored
    # reward_type=bonus_bets from its own terms' "Bets placed with bonus
    # rewards (including … Bonus Bets …) are not valid" — the sentence
    # excluding them — and planned at ~5x its value as credit conversion.
    reward = ""
    title_part = (parts[0] or "") if parts else ""
    for pattern, label in _REWARD_PATTERNS:
        if pattern.search(title_part):
            reward = label
            break
    if not reward:
        # Joined with ". " so reward patterns never match across a
        # sentence boundary — a space-join turned "…$500 Cash. Bonus
        # Spins…" into a fabricated "cash bonus" reward (stored live,
        # leovegas_on, runs #43–#46, with the offer's deepen fetch lost
        # to the is_specific cascade).
        # Abbreviation dots ("etc.", "e.g.") are not sentence ends: the
        # dot in "… Profit Boosts, etc.) are not valid" severed the reward
        # names from their own negation, and the un-negated fragment voted.
        scannable = re.sub(
            r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group(0).replace(".", ""), text
        )
        scannable = re.sub(
            r"\b(etc|approx|est|no|vs|inc|min|max)\.", r"\1", scannable, flags=re.I
        )
        scan = ". ".join(
            sentence
            for sentence in re.split(r"[.!?\n]", scannable)
            if not _NEGATED_REWARD_SENTENCE.search(sentence)
        )
        for pattern, label in _REWARD_PATTERNS:
            if pattern.search(scan):
                reward = label
                break

    bonus: float | None = None
    min_deposit: float | None = None
    summary_bits: list[str] = []

    m = _BET_GET.search(text)
    if m:
        stake = _money(m.group(1))
        reward_amt = _money(m.group(2))
        if reward_amt is not None:
            bonus = reward_amt
        if stake is not None and reward_amt is not None:
            label = reward.replace("_", " ") if reward else "bonus"
            summary_bits.append(f"Bet ${stake:,.10g}, get ${reward_amt:,.10g} in {label}")
        if stake is not None and min_deposit is None:
            min_deposit = stake

    if not summary_bits:
        m = _GET_IN_BONUS.search(text)
        if m:
            amt = _money(m.group(1))
            kind = re.sub(r"\s+", " ", m.group(2).lower())
            if amt is not None:
                bonus = amt
                summary_bits.append(f"Get ${amt:,.10g} in {kind}")
                if not reward:
                    if "bonus bet" in kind:
                        reward = "bonus_bets"
                    elif "free bet" in kind:
                        reward = "free_bet"
                    elif "credit" in kind:
                        reward = "site_credit"

    m = _PCT_MATCH.search(text)
    if m:
        pct = m.group(1)
        cap = _money(m.group(2)) if m.lastindex and m.lastindex >= 2 else None
        if cap is not None:
            bonus = bonus if bonus is not None else cap
            summary_bits.append(f"{pct}% deposit match up to ${cap:,.10g}")
        else:
            summary_bits.append(f"{pct}% deposit match")
        if not reward:
            reward = "site_credit"

    if bonus is None:
        m = _UP_TO.search(text)
        if m and reward:
            bonus = _money(m.group(1))

    m = _MIN_DEPOSIT.search(text)
    if m:
        min_deposit = _money(m.group(1))

    min_odds = None
    m = _MIN_ODDS.search(text)
    if m:
        min_odds = m.group(1)

    wagering = None
    m = _WAGERING.search(text)
    if m:
        wagering = (m.group(1) or m.group(2) or "").replace(" ", "").lower() or None

    m = _PROFIT_BOOST.search(text)
    if m and not summary_bits:
        summary_bits.append(f"{m.group(1)}% profit boost")
        reward = reward or "boost"

    if not summary_bits and reward and bonus is not None:
        summary_bits.append(f"${bonus:,.10g} {reward.replace('_', ' ')}")

    summary = summary_bits[0] if summary_bits else ""
    is_specific = bool(summary) or (
        bonus is not None and bool(reward)
    ) or (bool(reward) and bool(re.search(r"\$\s*\d|\d+\s*%", text)))
    if is_specific and not summary:
        cleaned = re.sub(r"\s+", " ", text).strip()
        # Prefer a money-bearing slice, starting at a word boundary — an
        # unanchored window opens mid-word ("…Un|ited States Dollars…").
        money = re.search(r"(?:^|(?<=\s)).{0,40}\$\s*[\d,]+.{0,60}\S*", cleaned)
        summary = (money.group(0).strip() if money else cleaned)[:160]

    out.update(
        {
            "summary": summary,
            "bonus_amount": bonus,
            "min_deposit": min_deposit,
            "min_odds": min_odds,
            "wagering_requirement": wagering,
            "reward_type": reward,
            "is_specific": is_specific,
        }
    )
    return out


def enrich_offer(offer: PromoOffer) -> PromoOffer:
    """Fill structured fields, geo, and summary from existing text surfaces."""
    mechanics = extract_mechanics(offer.title, offer.description, offer.terms, offer.summary)
    # eligibility_notes is this function's own output, never adapter input; feeding
    # it back into the parse made enrichment non-idempotent — a generated
    # "Not available in: X; Eligible in: …" note re-read on the next pass moved the
    # whole eligible list into ineligible.
    text = _blob(offer.title, offer.description, offer.terms)
    eligible, ineligible, notes = parse_eligibility(text)
    eligible = merge_regions(offer.eligible_regions, eligible)
    ineligible = merge_regions(offer.ineligible_regions, ineligible)
    if ineligible:
        eligible = [c for c in eligible if c not in set(ineligible)]

    eligibility_notes = offer.eligibility_notes
    if notes:
        existing = [s for s in (x.strip() for x in eligibility_notes.split(";")) if s]
        fresh: list[str] = []
        for segment in (x.strip() for x in notes.split(";")):
            if segment and segment not in existing and segment not in fresh:
                fresh.append(segment)
        eligibility_notes = "; ".join(existing + fresh)

    existing_summary = offer.summary or ""
    mech_summary = mechanics["summary"] or ""
    if mech_summary and (not existing_summary or is_vague_text(existing_summary)):
        summary = mech_summary
    elif existing_summary and not is_vague_text(existing_summary):
        summary = existing_summary
    elif not is_vague_text(offer.title) and mechanics["is_specific"]:
        summary = offer.title.strip()
    else:
        summary = existing_summary or mech_summary

    is_specific = bool(offer.is_specific or mechanics["is_specific"])
    if summary and not is_vague_text(summary):
        is_specific = True

    return offer.model_copy(
        update={
            "summary": summary,
            "bonus_amount": offer.bonus_amount
            if offer.bonus_amount is not None
            else mechanics["bonus_amount"],
            "min_deposit": offer.min_deposit
            if offer.min_deposit is not None
            else mechanics["min_deposit"],
            "min_odds": offer.min_odds or mechanics["min_odds"],
            "wagering_requirement": offer.wagering_requirement
            or mechanics["wagering_requirement"],
            "reward_type": offer.reward_type or mechanics["reward_type"],
            "ends_at": offer.ends_at or extract_ends_at(text, offer.observed_at),
            "eligible_regions": eligible,
            "ineligible_regions": ineligible,
            "eligibility_notes": eligibility_notes,
            "is_specific": is_specific,
        }
    )


def enrich_offers(offers: list[PromoOffer]) -> list[PromoOffer]:
    return [enrich_offer(o) for o in offers]


def offer_needs_deepen(offer: PromoOffer) -> bool:
    return (not offer.is_specific) and bool(offer.url) and not offer.requires_login


__all__ = [
    "enrich_offer",
    "enrich_offers",
    "extract_ends_at",
    "extract_mechanics",
    "is_vague_text",
    "offer_needs_deepen",
]
