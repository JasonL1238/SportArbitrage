"""Participant identity, per league.

Cross-source joining is only as good as participant identity, so this module
refuses to guess.  There are two regimes, because the sports genuinely differ:

**Closed rosters** (MLB, NBA, WNBA, NHL, NFL).  A name either resolves to one of
that league's clubs or it does not resolve at all.  There is no partial-credit
fallback that lets ``"NFL Futures"`` or ``"Philadelphia Phillies (A Nola)"`` flow
downstream as a participant.  Resolution is scoped to **one roster**: a global
index would make "Rangers" mean both Texas and New York, "Panthers" both
Carolina and Florida, "Kings" both Los Angeles and Sacramento, and "Jets" both
New York and Winnipeg.

**Open rosters** (soccer clubs, tennis players).  No finite membership list
exists, so identity is a *deterministic normalization* of the name instead of a
lookup.  The bias is explicit and one-directional: normalization is only allowed
to be as aggressive as it can be without merging two distinct competitors.  When
in doubt the forms stay apart, which costs a **missed** match — the event simply
does not join across books and no arbitrage is reported on it.  The alternative
error, a false match, joins two unrelated events whose prices are unconstrained,
and that is exactly the shape of a phantom arbitrage.

Concretely, that bias is why reserve and age-group markers are **kept**: "FK
Transinvest II" must not resolve to "FK Transinvest", and "Freiburg (W)" must not
resolve to "Freiburg".  A merge there pairs a women's or reserve fixture with the
senior men's fixture at another book.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.leagues import League
from src.rosters import AMBIGUOUS, EXTRA_ALIASES, ROSTERS
from src.vocab import Sport

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Participant:
    """One resolved competitor."""

    key: str
    """Globally unique, stable identity — ``"MLB-CIN"``, ``"SOCCER-arsenal"``,
    ``"TENNIS-humbert.ugo"``.  This is what ``event_key`` is built from, so it
    must not depend on which book supplied the name."""

    name: str
    """Canonical display name."""

    abbr: str
    """Short label.  A club abbreviation for closed rosters, the normalized slug
    for open ones."""

    roster: str | None = None
    """Closed roster this came from, or ``None`` for an open-roster resolution."""

    @property
    def is_roster_member(self) -> bool:
        return self.roster is not None


# ── shared text normalization ────────────────────────────────────────────────


def _fold(raw: str) -> str:
    """Strip accents so ``"Andrés Martín"`` and ``"Andres Martin"`` agree.

    Both spellings occur across the three books for the same tennis player, so
    folding here is what lets them join at all.
    """
    text = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _words(raw: str, *, drop_parentheticals: bool) -> list[str]:
    text = _fold(raw)
    if drop_parentheticals:
        text = _PARENTHETICAL.sub(" ", text)
    return [word for word in _NON_ALNUM.sub(" ", text.lower()).split() if word]


def _normalize(raw: str) -> str:
    """Index key form for closed rosters: lowercase alphanumerics only."""
    return "".join(_words(raw, drop_parentheticals=True))


# ── closed rosters ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Roster:
    name: str
    by_abbr: dict[str, Participant]
    index: dict[str, str]


def _build_roster(roster_name: str) -> _Roster:
    rows = ROSTERS[roster_name]
    by_abbr: dict[str, Participant] = {}
    for abbr, name, _city, _nick in rows:
        by_abbr[abbr] = Participant(
            key=f"{roster_name.upper()}-{abbr}", name=name, abbr=abbr, roster=roster_name
        )

    index: dict[str, str] = {}
    collisions: set[str] = set()

    def add(text: str, abbr: str) -> None:
        norm = _normalize(text)
        if not norm:
            return
        existing = index.get(norm)
        if existing is not None and existing != abbr:
            collisions.add(norm)
            return
        index[norm] = abbr

    abbr_keys: set[str] = set()
    for abbr, name, city, nickname in rows:
        abbr_keys.add(_normalize(abbr))
        add(abbr, abbr)
        add(name, abbr)
        add(nickname, abbr)
        if city:
            # A city alone is deliberately NOT registered as an identity: it
            # names a place, not a club.  Every spelling observed from the three
            # books carries a nickname or an abbreviation ("CHI White Sox",
            # "Cincinnati Reds", "LA Chargers"), so nothing real is lost — and
            # registering it caused a genuine cross-league false match, where
            # "Cincinnati Reds" resolved against the NFL roster as the Bengals
            # purely on the shared city.
            add(f"{city} {nickname}", abbr)

    # Anything that resolved to two different clubs is not an identity.
    for norm in collisions:
        index.pop(norm, None)

    # An abbreviation that is also an ordinary word inside another club's name is
    # not an identity either.  This is not hypothetical: the WNBA's Los Angeles
    # Sparks abbreviate to LAS, and "las" is the first word of "Las Vegas Aces",
    # so every Aces row resolved to two clubs and therefore to none.  Detecting
    # it structurally keeps the guarantee true as rosters change, instead of
    # relying on someone noticing the next collision by hand.
    word_tokens: set[str] = set()
    for abbr, name, city, nickname in rows:
        for text in (name, city, nickname):
            if text:
                word_tokens.update(_words(text, drop_parentheticals=True))
    for abbr_key in abbr_keys:
        if abbr_key in word_tokens and index.get(abbr_key) is not None:
            owner = index[abbr_key]
            # Only drop it when the colliding word belongs to a *different* club.
            others = {
                a
                for a, name, city, nickname in rows
                if a != owner
                and abbr_key in _words(f"{name} {city} {nickname}", drop_parentheticals=True)
            }
            if others:
                index.pop(abbr_key, None)

    for fragment in AMBIGUOUS.get(roster_name, frozenset()):
        index.pop(_normalize(fragment), None)

    index.update(EXTRA_ALIASES.get(roster_name, {}))
    return _Roster(name=roster_name, by_abbr=by_abbr, index=index)


_ROSTERS: dict[str, _Roster] = {name: _build_roster(name) for name in ROSTERS}


def resolve_roster(raw: str | None, roster_name: str) -> Participant | None:
    """Resolve *raw* against one closed roster, else ``None``.

    Returning ``None`` is a signal to reject the record, not to substitute a
    placeholder.

    Every contiguous run of words is matched against the index and the *set* of
    clubs found decides the answer: exactly one club resolves, zero or several do
    not.  Scanning spans rather than stripping a prefix matters in both
    directions.  Books wrap the name in their own decoration on either side —
    Kambi sends ``"CHI White Sox"``, ``"VGS Golden Knights"`` and ``"LA
    Chargers"``, and a doubleheader arrives as ``"White Sox - Game 2"``.  A
    prefix-only rule resolves the first and silently fails the last, which drops
    game two of every doubleheader.

    Requiring *uniqueness* is what keeps a whole matchup from resolving to one of
    its teams: ``"Chicago White Sox at Chicago Cubs"`` finds two clubs and so
    resolves to neither, where a longest-suffix rule would confidently answer
    "Cubs".  Ambiguous fragments never contribute, so ``"LA Chargers"`` lands on
    the Chargers and never on the ambiguous ``"LA"``.

    The guarantee is one of *extraction*, not of validation: a single-club string
    with surrounding text ("World Series Winner - Dodgers") resolves to that
    club.  Deciding that such a row is a futures market rather than a game is the
    adapter's job, not this function's.
    """
    roster = _ROSTERS.get(roster_name)
    if roster is None or not raw:
        return None
    words = _words(raw, drop_parentheticals=True)
    if not words:
        return None
    found: set[str] = set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            abbr = roster.index.get("".join(words[start:end]))
            if abbr is not None:
                found.add(abbr)
    if len(found) != 1:
        return None
    return roster.by_abbr[next(iter(found))]


def roster_members(roster_name: str) -> tuple[Participant, ...]:
    """Every club in one roster, ordered by abbreviation."""
    roster = _ROSTERS[roster_name]
    return tuple(roster.by_abbr[abbr] for abbr in sorted(roster.by_abbr))


# ── open rosters ─────────────────────────────────────────────────────────────

#: Words that mark a string as an outright/futures label rather than a
#: competitor.  Books put these in the *participant* field, not only in the
#: market name: Kambi's NBA slate returns ``homeName`` values like
#: ``"Atlanta Hawks Markets 2026/2027"`` and ``"Eastern Conference Winner
#: 2026/2027"``.  Resolving those as clubs is how a futures book becomes a
#: fixture list.
_FUTURES_MARKERS: frozenset[str] = frozenset(
    {
        "markets",
        "winner",
        "champion",
        "champions",
        "championship",
        "futures",
        "awards",
        "outright",
        "specials",
        "mvp",
        "qualify",
        "relegated",
        "relegation",
        "promotion",
        "goalscorer",
        "topscorer",
        "conference",
        "division",
    }
)

#: A season span such as ``2026/2027`` — another outright marker.
_SEASON_SPAN = re.compile(r"\b20\d{2}\s*/\s*\d{2,4}\b")

#: A pairing of two competitors rather than one, as in a tennis doubles entry
#: ``"R Galloway / E King"``.  These must be **unresolvable**, not normalized.
#:
#: Two separate reasons, either of which is sufficient.  The slug for an
#: open-roster competitor is order-independent, which is what lets "Xiyu Wang"
#: and "Wang Xiyu" join — applied to a pairing it scrambles four names into one
#: meaningless token (``e.galloway.king.r``) that could collide with a different
#: pairing of the same surnames.  And doubles entries are written with
#: initialised surnames, which cannot be reconciled with another book's spelling
#: of the same pair anyway.  A row that can never join is worse than no row: it
#: inflates coverage while contributing nothing.
#: Only *symbol* separators count.  The word "and" must not: "Brighton and Hove
#: Albion" is one club, and treating it as a pairing rejected a real Premier
#: League fixture outright.
_PAIRING = re.compile(r"\s(?:/|&|\+|\bvs?\b)\s", re.IGNORECASE)

#: Generic club-type tokens that books add or omit inconsistently for the *same*
#: club and that never by themselves distinguish two clubs: "FC Tulsa" and
#: "Tulsa" are one club, as are "Kapfenberger SV" and "Kapfenberger".
#:
#: This list is deliberately short.  Every token added to it merges two spellings
#: forever, so it holds only affixes that are pure club-type markers.  Words that
#: can carry identity are absent on purpose — "Deportivo", "Athletic",
#: "Sporting", "Real", "Dynamo" and "Union" all distinguish real clubs from one
#: another and are never stripped.
_CLUB_TYPE_TOKENS: frozenset[str] = frozenset(
    {
        "fc", "afc", "cf", "sc", "ac", "cd", "ca", "ec", "sv", "sk", "if", "bk",
        "fk", "nk", "hnk", "rc", "ks", "kv", "vfl", "vfb", "tsv", "sd", "ud",
        "club", "cfc", "sca", "asd", "ssd", "acf", "ssc", "usl",
    }
)

#: Tokens that **must never** be stripped, because they separate a reserve,
#: youth, or women's side from the senior men's side of the same club.  Merging
#: those pairs a women's or reserve fixture at one book with the men's fixture at
#: another — two different games, both real, prices unrelated.
_IDENTITY_SUFFIXES: frozenset[str] = frozenset(
    {
        "ii", "iii", "iv", "b", "c", "w", "women", "womens", "ladies", "fem",
        "reserves", "reserve", "res", "youth", "academy", "jr", "junior",
        "u16", "u17", "u18", "u19", "u20", "u21", "u22", "u23",
    }
)

#: Curated short forms for clubs whose spellings cannot be reconciled by
#: normalization alone.  ``"Wolves"`` and ``"Wolverhampton Wanderers"`` share no
#: tokens, so without an alias they stay apart — which is safe but loses the
#: join.  Keys and values are normalized slugs.
_SOCCER_ALIASES: dict[str, str] = {
    "wolves": "wolverhampton",
    "wolverhamptonwanderers": "wolverhampton",
    "manutd": "manchesterunited",
    "manchesterutd": "manchesterunited",
    "manunited": "manchesterunited",
    "mancity": "manchestercity",
    "spurs": "tottenham",
    "tottenhamhotspur": "tottenham",
    "psg": "parissaintgermain",
    "parissg": "parissaintgermain",
    "internazionale": "inter",
    "intermilan": "inter",
    "bayernmunich": "bayernmunchen",
    "bayern": "bayernmunchen",
    "borussiadortmund": "dortmund",
    "bvb": "dortmund",
    "atleticomadrid": "atleticomadrid",
    "atletimadrid": "atleticomadrid",
    "barcelona": "barcelona",
    "barca": "barcelona",
    "realmadrid": "realmadrid",
    "newcastle": "newcastleunited",
    "leeds": "leedsunited",
    "westham": "westhamunited",
    "brighton": "brightonhovealbion",
    "nottinghamforest": "nottmforest",
    "sheffieldutd": "sheffieldunited",
    "sheffwed": "sheffieldwednesday",
}


def _open_slug(raw: str, *, sport: Sport) -> str | None:
    """Deterministic identity slug for an open-roster competitor.

    Returns ``None`` when the string does not look like a competitor at all.
    """
    if not raw or not raw.strip():
        return None
    if _SEASON_SPAN.search(raw):
        return None
    if _PAIRING.search(raw):
        return None

    # Parentheticals are kept for soccer, because "(W)" is the only thing
    # separating a women's side from the men's, and dropped for tennis, where
    # Pinnacle appends a scoring-unit marker: "Savannah Dada-Mascoll (Games)".
    words = _words(raw, drop_parentheticals=sport is Sport.TENNIS)
    if not words:
        return None
    if any(word in _FUTURES_MARKERS for word in words):
        return None

    if sport is Sport.TENNIS:
        # Books order a player's names differently — Kambi sends "Xiyu Wang"
        # where another may send "Wang Xiyu" — and neither ordering is
        # authoritative, so the slug is order-independent.  Sorting is safe here
        # in a way it would not be for clubs: a player's name tokens are a set,
        # while "Manchester United" and "United Manchester" are not the same
        # kind of variation.
        return ".".join(sorted(words)) or None

    # Clubs: drop pure club-type affixes but never an identity suffix.
    kept = [
        word
        for word in words
        if word not in _CLUB_TYPE_TOKENS or word in _IDENTITY_SUFFIXES
    ]
    # If stripping removed everything, the name *was* only club-type tokens and
    # carries no identity.
    if not kept:
        return None
    # A name that reduced to nothing but identity suffixes ("II", "W") is not a
    # club either.
    if all(word in _IDENTITY_SUFFIXES for word in kept):
        return None
    slug = "".join(kept)
    return _SOCCER_ALIASES.get(slug, slug)


def resolve_open(raw: str | None, sport: Sport) -> Participant | None:
    """Resolve an open-roster competitor by deterministic normalization."""
    if not raw:
        return None
    slug = _open_slug(raw, sport=sport)
    if slug is None:
        return None
    return Participant(
        key=f"{sport.value.upper()}-{slug}",
        name=" ".join(_fold(raw).split()),
        abbr=slug,
        roster=None,
    )


# ── the one entry point ──────────────────────────────────────────────────────


def canonical_participant(raw: str | None, competition: League) -> Participant | None:
    """Resolve *raw* to a participant in *competition*, else ``None``.

    Dispatches on whether the league has a closed roster.  Adapters should call
    only this, so which regime a league uses stays a property of the league
    rather than something each adapter re-decides.
    """
    if competition.roster is not None:
        return resolve_roster(raw, competition.roster)
    return resolve_open(raw, competition.sport)
