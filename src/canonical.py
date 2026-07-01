"""Canonical team-name and market normalization.

Maps variant team names (abbreviations, city-only, diacritics, whitespace
differences) to a single canonical key so cross-source event matching is
reliable.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# ── Team alias maps ──────────────────────────────────────────────────────────

_NBA_ALIASES: dict[str, str] = {
    "lakers": "los_angeles_lakers",
    "la lakers": "los_angeles_lakers",
    "los angeles lakers": "los_angeles_lakers",
    "celtics": "boston_celtics",
    "boston celtics": "boston_celtics",
    "knicks": "new_york_knicks",
    "ny knicks": "new_york_knicks",
    "new york knicks": "new_york_knicks",
    "heat": "miami_heat",
    "miami heat": "miami_heat",
    "bucks": "milwaukee_bucks",
    "milwaukee bucks": "milwaukee_bucks",
    "sixers": "philadelphia_76ers",
    "76ers": "philadelphia_76ers",
    "philadelphia 76ers": "philadelphia_76ers",
    "warriors": "golden_state_warriors",
    "golden state warriors": "golden_state_warriors",
    "nuggets": "denver_nuggets",
    "denver nuggets": "denver_nuggets",
    "suns": "phoenix_suns",
    "phoenix suns": "phoenix_suns",
    "mavericks": "dallas_mavericks",
    "dallas mavericks": "dallas_mavericks",
}

_MLB_ALIASES: dict[str, str] = {
    "yankees": "new_york_yankees",
    "ny yankees": "new_york_yankees",
    "new york yankees": "new_york_yankees",
    "red sox": "boston_red_sox",
    "boston red sox": "boston_red_sox",
    "dodgers": "los_angeles_dodgers",
    "la dodgers": "los_angeles_dodgers",
    "los angeles dodgers": "los_angeles_dodgers",
    "cubs": "chicago_cubs",
    "chicago cubs": "chicago_cubs",
    "mets": "new_york_mets",
    "ny mets": "new_york_mets",
    "new york mets": "new_york_mets",
    "astros": "houston_astros",
    "houston astros": "houston_astros",
    "braves": "atlanta_braves",
    "atlanta braves": "atlanta_braves",
    "phillies": "philadelphia_phillies",
    "philadelphia phillies": "philadelphia_phillies",
}

_NFL_ALIASES: dict[str, str] = {
    "chiefs": "kansas_city_chiefs",
    "kansas city chiefs": "kansas_city_chiefs",
    "eagles": "philadelphia_eagles",
    "philadelphia eagles": "philadelphia_eagles",
    "bills": "buffalo_bills",
    "buffalo bills": "buffalo_bills",
    "49ers": "san_francisco_49ers",
    "san francisco 49ers": "san_francisco_49ers",
    "cowboys": "dallas_cowboys",
    "dallas cowboys": "dallas_cowboys",
    "ravens": "baltimore_ravens",
    "baltimore ravens": "baltimore_ravens",
    "panthers": "carolina_panthers",
    "carolina panthers": "carolina_panthers",
    "packers": "green_bay_packers",
    "green bay packers": "green_bay_packers",
}

_NHL_ALIASES: dict[str, str] = {
    "rangers": "new_york_rangers",
    "ny rangers": "new_york_rangers",
    "new york rangers": "new_york_rangers",
    "islanders": "new_york_islanders",
    "ny islanders": "new_york_islanders",
    "new york islanders": "new_york_islanders",
    "bruins": "boston_bruins",
    "boston bruins": "boston_bruins",
    "maple leafs": "toronto_maple_leafs",
    "toronto maple leafs": "toronto_maple_leafs",
}

_SPORT_ALIAS_MAPS: dict[str, dict[str, str]] = {
    "basketball_nba": _NBA_ALIASES,
    "baseball_mlb": _MLB_ALIASES,
    "americanfootball_nfl": _NFL_ALIASES,
    "icehockey_nhl": _NHL_ALIASES,
}

# ── Market family normalization ──────────────────────────────────────────────

_MARKET_FAMILIES: dict[str, str] = {
    "h2h": "moneyline",
    "moneyline": "moneyline",
    "1x2": "moneyline",
    "spreads": "spread",
    "spread": "spread",
    "run_line": "spread",
    "puck_line": "spread",
    "handicap": "spread",
    "totals": "totals",
    "over_under": "totals",
    "ou": "totals",
}


# ── Public API ───────────────────────────────────────────────────────────────


def normalize_name(raw: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace, remove punctuation."""
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def canonical_team(raw: str, sport_key: str | None = None) -> str:
    """Return a canonical team key for *raw*, using alias maps when available.

    Falls back to ``normalize_name`` when no alias matches.
    """
    normed = normalize_name(raw)
    if sport_key:
        aliases = _SPORT_ALIAS_MAPS.get(sport_key, {})
        if normed in aliases:
            return aliases[normed]
    for aliases in _SPORT_ALIAS_MAPS.values():
        if normed in aliases:
            return aliases[normed]
    return normed.replace(" ", "_")


def canonical_market(raw: str) -> str:
    """Map a market key to its canonical family name."""
    return _MARKET_FAMILIES.get(raw.lower().strip(), raw.lower().strip())


def token_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word tokens of two strings."""
    tokens_a = set(normalize_name(a).split())
    tokens_b = set(normalize_name(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
