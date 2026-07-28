"""Strict MLB team canonicalization.

Cross-source joining is only as good as team identity, so this module refuses
to guess.  A name either resolves to one of the thirty MLB clubs or it does
not resolve at all — there is no partial-credit fallback that lets ``"MLB
Futures"`` or ``"Philadelphia Phillies (A Nola)"`` flow downstream as a team.

Ambiguous fragments are rejected on purpose: ``"Chicago"`` could be either
Chicago club, ``"Sox"`` could be either Sox, ``"New York"`` and ``"Los
Angeles"`` are two clubs each.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Team:
    abbr: str
    name: str
    city: str
    nickname: str


#: The thirty MLB clubs.  ``abbr`` values are the stable identity used in
#: ``event_key``; changing one changes cross-source joining.
TEAMS: tuple[Team, ...] = (
    Team("ARI", "Arizona Diamondbacks", "Arizona", "Diamondbacks"),
    Team("ATH", "Athletics", "", "Athletics"),
    Team("ATL", "Atlanta Braves", "Atlanta", "Braves"),
    Team("BAL", "Baltimore Orioles", "Baltimore", "Orioles"),
    Team("BOS", "Boston Red Sox", "Boston", "Red Sox"),
    Team("CHC", "Chicago Cubs", "Chicago", "Cubs"),
    Team("CIN", "Cincinnati Reds", "Cincinnati", "Reds"),
    Team("CLE", "Cleveland Guardians", "Cleveland", "Guardians"),
    Team("COL", "Colorado Rockies", "Colorado", "Rockies"),
    Team("CWS", "Chicago White Sox", "Chicago", "White Sox"),
    Team("DET", "Detroit Tigers", "Detroit", "Tigers"),
    Team("HOU", "Houston Astros", "Houston", "Astros"),
    Team("KC", "Kansas City Royals", "Kansas City", "Royals"),
    Team("LAA", "Los Angeles Angels", "Los Angeles", "Angels"),
    Team("LAD", "Los Angeles Dodgers", "Los Angeles", "Dodgers"),
    Team("MIA", "Miami Marlins", "Miami", "Marlins"),
    Team("MIL", "Milwaukee Brewers", "Milwaukee", "Brewers"),
    Team("MIN", "Minnesota Twins", "Minnesota", "Twins"),
    Team("NYM", "New York Mets", "New York", "Mets"),
    Team("NYY", "New York Yankees", "New York", "Yankees"),
    Team("PHI", "Philadelphia Phillies", "Philadelphia", "Phillies"),
    Team("PIT", "Pittsburgh Pirates", "Pittsburgh", "Pirates"),
    Team("SD", "San Diego Padres", "San Diego", "Padres"),
    Team("SEA", "Seattle Mariners", "Seattle", "Mariners"),
    Team("SF", "San Francisco Giants", "San Francisco", "Giants"),
    Team("STL", "St. Louis Cardinals", "St. Louis", "Cardinals"),
    Team("TB", "Tampa Bay Rays", "Tampa Bay", "Rays"),
    Team("TEX", "Texas Rangers", "Texas", "Rangers"),
    Team("TOR", "Toronto Blue Jays", "Toronto", "Blue Jays"),
    Team("WSH", "Washington Nationals", "Washington", "Nationals"),
)

BY_ABBR: dict[str, Team] = {t.abbr: t for t in TEAMS}

#: Spellings that the city-prefix rule below cannot derive: former names,
#: colloquialisms, and bare nicknames absent from the table.  Keys are
#: normalized (lowercase, alphanumeric-only) forms.
_EXTRA_ALIASES: dict[str, str] = {
    "oaklandathletics": "ATH",
    "oaklandas": "ATH",
    "sacramentoathletics": "ATH",
    "clevelandindians": "CLE",
    "stlouiscardinals": "STL",
    "saintlouiscardinals": "STL",
    "arizonadbacks": "ARI",
    "dbacks": "ARI",
    "washingtonnats": "WSH",
    "nats": "WSH",
    "anaheimangels": "LAA",
    "losangelesangelsofanaheim": "LAA",
    "tampabaydevilrays": "TB",
}

#: Fragments that match more than one club and must never resolve.  Most are
#: also caught by collision detection; they are listed so the intent survives
#: any future edit to the team table.
_AMBIGUOUS: frozenset[str] = frozenset(
    {"chicago", "new york", "ny", "los angeles", "la", "sox", "chi", "mlb"}
)


def _words(raw: str) -> list[str]:
    """Lowercase, strip accents and parentheticals, split into word tokens."""
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PARENTHETICAL.sub(" ", text)
    return [word for word in _NON_ALNUM.sub(" ", text.lower()).split() if word]


def _normalize(raw: str) -> str:
    """Index key form: lowercase alphanumerics only."""
    return "".join(_words(raw))


def _build_index() -> dict[str, str]:
    index: dict[str, str] = {}
    collisions: set[str] = set()

    def add(key: str, abbr: str) -> None:
        norm = _normalize(key)
        if not norm:
            return
        existing = index.get(norm)
        if existing is not None and existing != abbr:
            collisions.add(norm)
            return
        index[norm] = abbr

    for team in TEAMS:
        add(team.abbr, team.abbr)
        add(team.name, team.abbr)
        add(team.nickname, team.abbr)
        if team.city:
            add(team.city, team.abbr)
            add(f"{team.city} {team.nickname}", team.abbr)

    # Anything that resolved to two different clubs is not an identity.
    for norm in collisions:
        index.pop(norm, None)
    for fragment in _AMBIGUOUS:
        index.pop(_normalize(fragment), None)

    index.update({k: v for k, v in _EXTRA_ALIASES.items()})
    return index


_INDEX: dict[str, str] = _build_index()


def canonical_team(raw: str | None) -> Team | None:
    """Resolve *raw* to the one :class:`Team` it names, else ``None``.

    Returning ``None`` is a signal to reject the record, not to substitute a
    placeholder.

    Every contiguous run of words is matched against the index and the *set* of
    clubs found decides the answer: exactly one club resolves, zero or several
    do not.  Scanning spans rather than stripping a prefix matters in both
    directions.

    Books wrap the name in their own decoration on either side — Kambi sends
    ``"CHI White Sox"`` and ``"WAS Nationals"``, and a doubleheader arrives as
    ``"White Sox - Game 2"``.  A prefix-only rule resolves the first and
    silently fails the second, which drops game two of every doubleheader.

    Requiring *uniqueness* is what keeps a whole matchup from resolving to one
    of its teams: ``"Chicago White Sox at Chicago Cubs"`` finds two clubs and so
    resolves to neither, where a longest-suffix rule would confidently answer
    "Cubs".  Ambiguous fragments never contribute, so ``"CHI White Sox"`` still
    lands on the White Sox and never on the ambiguous ``"Sox"``.

    The guarantee is one of *extraction*, not of validation: a single-club
    string with surrounding text ("World Series Winner - Dodgers") resolves to
    that club.  Deciding that such a row is a futures market rather than a game
    is the adapter's job, not this function's.
    """
    if not raw:
        return None
    words = _words(raw)
    if not words:
        return None
    found: set[str] = set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            abbr = _INDEX.get("".join(words[start:end]))
            if abbr is not None:
                found.add(abbr)
    if len(found) != 1:
        return None
    return BY_ABBR[next(iter(found))]
