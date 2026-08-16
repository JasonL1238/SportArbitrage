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
    ``"TENNIS-humbertugo"``.  This is what ``event_key`` is built from, so it
    must not depend on which book supplied the name."""

    name: str
    """Canonical display name."""

    abbr: str
    """Short label.  A club abbreviation for closed rosters, the normalized slug
    for open ones."""

    roster: str | None = None
    """Closed roster this came from, or ``None`` for an open-roster resolution."""


# ── shared text normalization ────────────────────────────────────────────────


#: Letters NFKD cannot decompose, and what they are conventionally written as.
#:
#: NFKD splits base+diacritic — ``é`` becomes ``e`` plus a combining mark, which
#: is why dropping the marks folds ``"Andrés"`` onto ``"Andres"``.  A letter that
#: is *not* a base plus a mark survives NFKD unchanged and was then removed by
#: the alphanumeric filter downstream, which does not fold it — it **deletes**
#: it:
#:
#: ===============  ===================  =========================
#: name             produced             at another book
#: ===============  ===================  =========================
#: ``Tromsø``       ``SOCCER-troms``     ``SOCCER-tromso``
#: ``Nordsjælland`` ``…-nordsjlland``    ``…-nordsjaelland``
#: ``Ægir``         ``SOCCER-gir``       ``SOCCER-aegir``
#: ``Þór``          ``SOCCER-or``        —
#: ``Łódź``         ``SOCCER-odz``       —
#: ===============  ===================  =========================
#:
#: ``Ægir`` losing its first letter to become ``gir`` is the shape to worry
#: about: a short mangled stem is the most collision-prone thing this module can
#: produce, and it is one a human reading the output would never guess at.
#:
#: This is not a merge rule and cannot join two clubs — it only stops letters
#: being thrown away.  Each mapping is the spelling another source in this
#: pipeline already uses: ``ø -> o`` yields exactly Pinnacle's ``tromso`` and
#: ``æ -> ae`` exactly its ``nordsjaelland``.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH",
    "ß": "ss",
    "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D",
    "ħ": "h", "Ħ": "H",
    "ı": "i", "İ": "I",
})


def _fold(raw: str) -> str:
    """Strip accents so ``"Andrés Martín"`` and ``"Andres Martin"`` agree.

    Both spellings occur across the books for the same tennis player, so folding
    here is what lets them join at all.  Letters NFKD cannot decompose are
    transliterated first; see :data:`_TRANSLITERATE` for why deleting them was
    worse than not folding them.
    """
    text = unicodedata.normalize("NFKD", raw.translate(_TRANSLITERATE))
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
    for _abbr, name, city, nickname in rows:
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


#: A "competitor" that is really a **statistic**, not a side.
#:
#: Books hang aggregate markets off a container shaped exactly like a fixture,
#: with the two statistics in the participant fields.  Bovada sends ``"Away Total
#: Runs @ Home Total Runs"``; Pinnacle sends ``"Home Runs (16 Games)"`` against
#: ``"Away Runs (16 Games)"``, a figure aggregated across the whole day's slate.
#:
#: These have to be told apart from a competitor the adapter simply failed to
#: recognise, because the two are graded differently and rightly so: an
#: unresolved *competitor* is a rejection, which marks the whole source
#: unhealthy, and one of these containers on a live slate was enough to put a
#: book's 29,711 good rows behind ``ok=0``.  A market this collector does not
#: cover is a counted skip.
#:
#: Recognised here rather than in each adapter so the rule is one rule.  Both
#: shapes are safe to match: no competitor in any sport is named for the side it
#: is playing on, and none carries a game count in brackets.
_STATISTIC_SIDE = re.compile(
    r"^(?:home|away)\s+\S|\(\s*\d+\s+games?\s*\)", re.IGNORECASE
)


def is_statistic(raw: str | None) -> bool:
    """Is *raw* an aggregate statistic dressed as a competitor?

    Adapters use this the way they use :func:`is_pairing` — to tell "out of
    scope" from "broken" — and skip rather than reject.
    """
    return bool(raw) and bool(_STATISTIC_SIDE.search(raw))


def is_futures(raw: str | None) -> bool:
    """Is *raw* an outright/futures label dressed as a competitor?

    The third member of the :func:`is_pairing` / :func:`is_statistic` family, and
    it exists for the same reason: ``docs/INPUT_CONTRACT.md`` classes futures as
    deliberately out of scope, so an adapter meeting one should count a **skip**,
    not a rejection that marks the whole source unhealthy.

    ``_open_slug`` has always refused these, but it refuses by returning ``None``
    — which an adapter can only read as "this name did not resolve".  BetMGM's
    Belgian second tier lists "RSC Anderlecht Futures" as a fixture participant;
    before this, admitting that competition turned one out-of-scope entry into a
    standing ``source_unhealthy:rejections`` warning on every run.
    """
    if not raw:
        return False
    return any(
        word in _FUTURES_MARKERS for word in _words(raw, drop_parentheticals=False)
    )


# ── open rosters ─────────────────────────────────────────────────────────────

#: Words that mark a string as an outright/futures label rather than a
#: competitor.  Books put these in the *participant* field, not only in the
#: market name: Kambi's NBA slate returns ``homeName`` values like
#: ``"Atlanta Hawks Markets 2026/2027"`` and ``"Eastern Conference Winner
#: 2026/2027"``.  Resolving those as clubs is how a futures book becomes a
#: fixture list.
#: Generational suffixes on a *person's* name.  Dropped from tennis slugs —
#: books mix "Martin Damm" and "Martin Damm Jr" for the same player — and never
#: consulted for clubs, where ``jr``/``junior`` separates real junior teams.
_GENERATIONAL_SUFFIXES: frozenset[str] = frozenset({"jr", "sr", "junior", "senior"})

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
_PAIRING = re.compile(r"\s(?:/|\+|\bvs?\b)\s", re.IGNORECASE)

#: ``&`` as well, but only where a pairing is a thing that exists.
#:
#: Fixing the word "and" and leaving the symbol was half a fix: Bovada writes the
#: same club as "Brighton & Hove Albion", so the ampersand went on discarding the
#: identical real fixture through the identical path.  It cost more than one row.
#: Three sources priced Brighton v Aston Villa and the pipeline produced three
#: non-comparable outcomes and **zero cross-book markets**: Bovada's copy was
#: skipped as a pairing, and the two survivors disagreed on the key.
#:
#: The ampersand cannot simply be dropped, because a doubles entry may well be
#: written "Galloway & King".  What makes the two cases separable is that doubles
#: is a *tennis* regime — clubs do not pair up — so the symbol is read as a
#: separator for individual competitors and as part of the name for clubs.  With
#: no sport stated the strict reading applies, since skipping a row is the
#: recoverable error and mis-slugging two names into one is not.
_PAIRING_WITH_AMPERSAND = re.compile(r"\s(?:/|&|\+|\bvs?\b)\s", re.IGNORECASE)

#: Sports whose competitors are people, and where two of them can therefore
#: enter as one pair.
_INDIVIDUAL_SPORTS: frozenset[Sport] = frozenset({Sport.TENNIS})

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
        # Verified against a live slate: each of these produced only merges
        # between spellings of one club (Drita, Ballkani, Universitatea Craiova,
        # Levski Sofia, Neftchi, Zilina, Besiktas, Zira, Gent, Auxerre, Roma,
        # Monaco), and none fused two clubs any single source distinguishes.
        "kf", "cso", "pfc", "pfk", "msk", "jk", "kaa", "aj", "cs", "ik", "as",
        # Legal forms and one sponsor prefix, each measured on the 2026-08-14
        # Illinois run as splitting one fixture across two event keys with
        # US-bettable books on both sides:
        #   parma / parmacalcio          betmgm + thescore vs six others
        #   udinese / udinesecalcio      betmgm + onexbet vs seven
        #   frosinone / frosinonecalcio  betmgm + onexbet + thescore vs six
        #   cagliari / cagliaricalcio    onexbet + thescore vs seven
        #   lazio / sslazio              betmgm vs eight
        #   lecce / uslecce              betmgm vs seven
        #   venezia / unionevenezia      onexbet vs eight
        #   leverkusen / bayerleverkusen fanduel alone vs six
        # "bayer" is a sponsor rather than a club type, and is safe here only
        # because tokens match whole words: "bayern" is a different token and
        # Bayern Munich is untouched.
        "calcio", "ss", "us", "unione", "bayer",
        # Same class, same run: pure club-type affixes that split one fixture.
        #   lille / lilleosc            betrivers + fanduel vs thescore
        #   angers / angerssco          three books vs betmgm
        #   atalanta / atalantabc       three books vs betmgm
        #   palmeirassp / sepalmeirassp hardrock vs betmgm
        #   mainz / fsvmainz05          fanduel vs thescore
        # "se" belongs on this list by shape — SE Palmeiras, Sociedade
        # Esportiva — and is refused because it is also Sergipe's state code,
        # which is how books tell two Brazilian clubs of one name apart. The
        # disjointness assertion in ``TestAStateCodeIsNotAClubType`` caught it;
        # the ``_STATE_CODES_NOT_CLUB_TYPES`` comment is the standing warning.
        "osc", "sco", "bc", "fsv",
    }
)

#: A club's founding year written as part of its name — "Como 1907", "Bologna
#: 1909", "Padova 1966" — where another book writes the club alone.  Stripped
#: **only in trailing position**, which is the whole safety argument: a leading
#: year is part of how the club is known and distinguishes it ("1860 Munich"
#: reduced to "munich" would be a different claim about who is playing), while a
#: trailing one is decoration every other book omits.
#:
#: Two digits are never stripped, in either position.  "Schalke 04" is a founding
#: year too, and ``tests/test_fanduel_adapter.py`` pins ``SOCCER-schalke04``
#: against the committed capture — so "SV 07 Elversberg" is handled by a curated
#: alias instead, where the ambiguity is decided once by hand rather than by a
#: rule that cannot tell the two apart.
_TRAILING_FOUNDING_YEAR = re.compile(r"^1[89]\d\d$")

#: Tokens that look like club-type markers and are **state codes**, which is how
#: books tell same-named clubs in different states apart.
#:
#: Refused on the evidence, not on principle.  One live capture carries
#: ``Botafogo RJ`` (Bovada) and ``Botafogo SP`` / ``Botafogo-SP`` (four other
#: books) — two different clubs.  Any rule that strips a two-letter Brazilian
#: state suffix merges them, and since one book prices Rio and four price São
#: Paulo, the merge produces a single event key carrying prices from two
#: unrelated fixtures: the phantom this module exists to avoid.
#:
#: ``sc`` is the uncomfortable one.  It is in the list above as Sporting Club and
#: is *also* Santa Catarina (``Avaí-SC``, ``Chapecoense-SC``), so it strips a
#: state code today.  It stays because removing it would break the Sporting Club
#: merges the list is there for, and because no same-named pair collides on it in
#: any capture — but it is the one entry that could, and this is where a future
#: reader should look first.
_STATE_CODES_NOT_CLUB_TYPES: frozenset[str] = frozenset({"rs", "ce", "se", "mg", "pr"})

#: Clubs whose *full* spelling must not be reduced, because the reduction is
#: another club's name.  Consulted before any token is stripped.
#:
#: Stripping club-type affixes is right in general — "FC Tulsa" and "Tulsa" are
#: one club — but it is right only because the affix carries no identity.  A
#: handful of names break that: "Barcelona SC" is Barcelona Sporting Club of
#: Guayaquil and reduces onto FC Barcelona; "CD Nacional" is the Madeira club and
#: reduces onto Nacional of Montevideo.  Neither the token nor its position tells
#: them apart — "SC Freiburg" and "CD Leganés" strip correctly by the same rule —
#: so the exceptions are named rather than inferred.
#:
#: This is a floor, not a guarantee.  Short generic stems ("Nacional",
#: "América", "Unión") are ambiguous across confederations however they are
#: written, and a sport-scoped key cannot resolve that on its own; a name not
#: listed here is not thereby proved unique.  Two competitors merging still
#: requires *both* sides of a fixture to collide before it can produce a false
#: join, which is why this is a latent identity fault rather than a live pricing
#: one — but it is the single error this module says it will not make, so the
#: cases that are known are pinned.
_SOCCER_DISAMBIGUATIONS: dict[str, str] = {
    "barcelonasc": "barcelonasc",
    "cdnacional": "cdnacional",
    # Smarkets' "Wolves FC" is a Brisbane club — its opponent that day was Magic
    # United, and the same run carries Brisbane Wolves, Wynnum Wolves and
    # Wollongong Wolves as separate correct keys.  The ``"wolves"`` alias below
    # sent it to Wolverhampton, on a date that also held a real Wolverhampton
    # fixture 9.5 hours away, well inside soccer's 30-hour tolerance.  Nothing
    # caught it: it produced no phantom only because the *other* side of the two
    # fixtures differed, which is the escape clause this module's own comment
    # calls a latent fault rather than a live one.
    #
    # Pinned rather than de-aliased.  The pin is consulted before any club-type
    # token is stripped, so "Wolves FC" keeps its "fc" and never reaches the
    # alias table — while a bare "Wolves" still resolves to Wolverhampton, which
    # three tests require.  The Brisbane club stays split from its own other
    # spellings, and that is the intended direction: an unjoined source is
    # recoverable, two clubs' prices on one fixture are not.
    "wolvesfc": "wolvesfc",
}

#: Conjunctions inside a club's own name, which books spell three ways for the
#: same club: "Brighton & Hove Albion" (Bovada), "Brighton and Hove Albion"
#: (Pinnacle), "Brighton" (FanDuel).  Normalization already folds the symbol
#: away, so only the word needs dropping to make all three agree.
#:
#: Safe in a way the club-type tokens are not: no two clubs are distinguished by
#: the presence of a conjunction, so this cannot merge distinct competitors.
_CLUB_CONJUNCTIONS: frozenset[str] = frozenset({"and"})

#: Marker words that are also a real club's entire name.
#:
#: Named one at a time rather than inferred, because no structural rule
#: separates them: "Junior" is Atlético Junior of Barranquilla and "II" is a
#: reserve side, and both are a single unstripped marker word.  Refusing
#: "Junior" rejected the row, and one rejection marks the whole source
#: unhealthy — an exchange lost its health over one correctly-spelled Colombian
#: club.
#:
#: Matched per **word**, not against the whole name.  Keyed on the exact tuple
#: ``("junior",)``, the fix reached "Junior" and stopped there: "Junior W" and
#: "Junior Youth" are the same club's women's and youth sides, are all-marker
#: names too, and went on being rejected — the fix looked complete because the
#: one row that had failed now passed.
_MARKER_WORDS_THAT_NAME_A_CLUB: frozenset[str] = frozenset({"junior"})

#: Identity markers several venues spell differently for the same thing.
#:
#: The markers are what keeps a women's side apart from the men's, and they were
#: five independent tokens — so "Aston Villa W", "Aston Villa Women" and "Aston
#: Villa Ladies" produced three participant keys and joined nothing.  That made
#: the marker synthesised from a competition name useless in practice: it emits
#: a bare ``w``, which could only ever meet a venue that also writes ``w``.
#:
#: Only spellings that are the *same word* are folded.  Reserve designations are
#: left alone on purpose: ``II``, ``B`` and ``C`` are ordinals a club can field
#: simultaneously, so merging them could fuse two real, different sides — and an
#: unmade join is recoverable where a false one is not.
_SUFFIX_SPELLINGS: dict[str, str] = {
    # The same languages the competition pattern reads.  These were four
    # English/Spanish spellings while the pattern knew ten languages, and
    # ``with_marker`` decides "is this name already marked?" by folding through
    # this map — so Kambi's ``Frauen-Bundesliga`` / ``Freiburg Frauen`` was
    # marked a second time and produced ``freiburgfrauenw`` beside another
    # venue's ``freiburgw``.  Exactly the defect ``with_marker`` was written
    # for, in the languages the same round had just added.
    "women": "w",
    "womens": "w",
    "ladies": "w",
    "fem": "w",
    "femenino": "w",
    "femenina": "w",
    "femenil": "w",
    "femminile": "w",
    "feminine": "w",
    "feminino": "w",
    "frauen": "w",
    "vrouwen": "w",
    "damer": "w",
    "kvinner": "w",
    "kvinde": "w",
    "reserve": "reserves",
    "res": "reserves",
    "jr": "junior",
}

#: Tokens that **must never** be stripped, because they separate a reserve,
#: youth, or women's side from the senior men's side of the same club.  Merging
#: those pairs a women's or reserve fixture at one book with the men's fixture at
#: another — two different games, both real, prices unrelated.
_IDENTITY_SUFFIXES: frozenset[str] = frozenset(
    {
        "ii", "iii", "iv", "b", "c", "w", "women", "womens", "ladies", "fem",
        "reserves", "reserve", "res", "youth", "academy", "jr", "junior",
        "femenino", "femenina", "femenil", "femminile", "feminine", "feminino",
        "frauen", "vrouwen", "damer", "kvinner", "kvinde",
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
    # "Paris St-G": the hyphenated short form, found 2026-08-16 when the first
    # Hard Rock soccer capture ("Paris Saint Germain") split PSG@Rennes from
    # another book's short spelling in the committed corpus.
    "parisstg": "parissaintgermain",
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
    # MLS and the promoted English clubs, where FanDuel writes the city alone
    # and every other book writes the club.  The table encoded FanDuel's EPL
    # short forms from an earlier slate and was never extended, so nine soccer
    # fixtures sat under two keys and six of them were two single-book events
    # rather than one cross-book market.
    #
    # Curated one at a time, and deliberately not inferred from a prefix: on the
    # same capture "Inter" is Internazionale and not Inter Miami, "Paris FC" is
    # not Paris Saint-Germain, "Racing Club" is not Racing Santander,
    # "Deportivo" is not Deportivo Pasto, and "Tigre" is not Tigres FC
    # Zipaquira.  A rule that merged short into long would fuse every one of
    # those pairs, and a false merge puts two clubs' prices on one fixture.
    "atlantautd": "atlantaunited",
    "dcutd": "dcunited",
    "minnesotautd": "minnesotaunited",
    "kansascity": "sportingkansascity",
    "columbus": "columbuscrew",
    "colorado": "coloradorapids",
    "philadelphia": "philadelphiaunion",
    "coventry": "coventrycity",
    "hull": "hullcity",
    "ipswich": "ipswichtown",
    # One Colombian club under three spellings, one per venue: Smarkets sends
    # "Junior", Pinnacle "Junior de Barranquilla", Kambi "Atletico Junior".
    # Letting the first resolve instead of being rejected did not join it to the
    # other two — it only moved the failure from a counted rejection to a silent
    # three-way split, which no check reports.
    "juniordebarranquilla": "junior",
    "juniorbarranquilla": "junior",
    "atleticojunior": "junior",
    "sheffieldutd": "sheffieldunited",
    "sheffwed": "sheffieldwednesday",
    # BetRivers' Kambi tenant is the only source that abbreviates an MLS city to
    # its three-letter code — the other tenant of the same platform writes the
    # club out — so each of these was one fixture under two keys, with the
    # abbreviating side alone on its own. Measured on the 2026-08-14 Illinois
    # run: 14 clubs, every one of them 1 source against 8 or 9.
    #
    # Curated rather than inferred, for the reason the comment above gives: the
    # shape is <city code> + <nickname>, and no rule can tell "COL Crew" from a
    # club actually called "Col". Each key here is a city code plus the club's
    # own nickname, which is why none of them is the bare-nickname hazard that
    # the "wolves" entry is.
    "chifire": "chicagofire",
    "colcrew": "columbuscrew",
    "colrapids": "coloradorapids",
    "houdynamo": "houstondynamo",
    "minunited": "minnesotaunited",
    "nerevolution": "newenglandrevolution",
    "nycfc": "newyorkcity",
    "orlcity": "orlandocity",
    "phiunion": "philadelphiaunion",
    "portimbers": "portlandtimbers",
    "seasounders": "seattlesounders",
    "sjearthquakes": "sanjoseearthquakes",
    "sportingkc": "sportingkansascity",
    "vanwhitecaps": "vancouverwhitecaps",
    # Three more MLS splits from the same run that are not Kambi's doing — two
    # books simply write the city in full and the rest abbreviate it, or write
    # the club's legal name. "LA Galaxy" is the widest: three sources against
    # seven, and ``betmgm`` x ``fanduel`` is a US-bettable pair across the split.
    "lagalaxy": "losangelesgalaxy",
    "stlouiscity": "saintlouiscity",
    "nashvillesoccer": "nashville",
    # Founding years the trailing-year rule deliberately will not touch, because
    # they are two digits and lead the name. "SV 07 Elversberg" is one club with
    # "Elversberg"; "Schalke 04" must keep its own, and no rule distinguishes
    # them — so the ambiguity is decided here, once, by hand.
    "07elversberg": "elversberg",
    "04leverkusen": "leverkusen",
    "paderborn07": "paderborn",
    "mainz05": "mainz",
    "1mainz05": "mainz",
    # Two spellings of one French club, and two regional suffixes another book
    # omits. "de" is not stripped as a rule — it is identity-bearing in Spanish
    # and Portuguese names — so these are decided one at a time here.
    "staderennais": "staderennes",
    # Bare "Rennes" is Stade Rennais — a city with one professional club, so
    # the short form is safe where bare "Deportivo" (below) is not.  Found
    # 2026-08-16, same corpus split as "parisstg" above.
    "rennes": "staderennes",
    "strasbourgalsace": "strasbourg",
    "olympiquedemarseille": "olympiquemarseille",
    # Two long spellings of one club. The *bare* "Deportivo" that FanDuel sends
    # is deliberately left split: the comment above records that on a live
    # capture "Deportivo" is Deportivo Pasto, so claiming it is La Coruña would
    # be the false merge this table exists to avoid. One unjoined source is
    # recoverable; two clubs' prices on one fixture is not.
    "deportivodelacoruna": "deportivolacoruna",
    # Spanish and French clubs the committed captures carry under two spellings
    # each, found when the split detector below was widened past tennis. Every
    # pair here is one fixture: same opponent, same kickoff, disjoint sources.
    "internazionalemilano": "inter",
    "espanyolbarcelona": "espanyol",
    "racingdesantander": "racingsantander",
    "deportivoalaves": "alaves",
    "betis": "realbetis",
    "celta": "celtavigo",
    "celtadevigo": "celtavigo",
    "stadebrestois29": "brest",
    "marseille": "olympiquemarseille",
    "estactroyes": "troyes",
}


def _open_slug(raw: str, *, sport: Sport) -> str | None:
    """Deterministic identity slug for an open-roster competitor.

    Returns ``None`` when the string does not look like a competitor at all.
    """
    if not raw or not raw.strip():
        return None
    if _SEASON_SPAN.search(raw):
        return None
    if _pairing_pattern(sport).search(raw):
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
        # Joined without a separator, so one book splitting a given name into
        # two tokens still lands on the other books' key: Bovada writes "Soon
        # Woo Kwon" where FanDuel and Matchbook write "Soonwoo Kwon", and
        # ``kwon.soon.woo`` never met ``kwon.soonwoo``.  Tennis has no alias
        # table — the names are unbounded — so this is the only mechanism that
        # can reconcile them.
        #
        # Measured before changing it: across the 324 tennis display names in the
        # captured corpus this merges exactly one pair — that one — and produces
        # no name with two keys and no key holding two names that share no
        # token.
        #
        # The residual risk is narrow, because the sort happens *before* the
        # join: a split given name only collides with its joined form when the
        # two halves sort adjacent.  "Soon"/"Woo" do, after "Kwon".  "Ana"/"Li"
        # do not — ``anachenli`` against ``analichen`` — so the shapes that
        # could fuse two different people are those where one person's name
        # happens to be another's with an interior split at an alphabetically
        # adjacent point.  No such pair exists in the corpus, and the direction
        # that would matter needs two *different* players.
        #
        # Generational suffixes are dropped, not sorted in.  Books mix "Martin
        # Damm" and "Martin Damm Jr" for the same ATP player (whose father also
        # played), so ``dammjrmartin`` and ``dammmartin`` were two keys for one
        # person — and bovada's rows for his match never joined the other three
        # books', excluding that leg from every cross-book comparison with
        # nothing anywhere to say so: every check groups by event key or
        # participant pair, and both differed.  Dropping the suffix can only
        # fuse a father and son playing *the same opponent on the same date*,
        # which a tour schedule cannot produce; the club-side ``jr``/``junior``
        # handling is untouched because there it separates real junior teams.
        person_words = [
            word for word in words if word not in _GENERATIONAL_SUFFIXES
        ] or words
        return "".join(sorted(person_words)) or None

    # Clubs whose full spelling reduces onto a different club keep it verbatim.
    # Looked up on the club alone, with any identity marker split off first and
    # put back after.
    #
    # Keyed on the whole name, the pin was defeated by any ``W`` / ``II`` /
    # ``Reserves`` / ``U20``: "Barcelona SC W" missed the pin, fell through to
    # the stripping below, lost ``SC`` as a club-type token and landed on
    # ``barcelonaw`` — the same slug as "Barcelona W", which is the exact merge
    # the pin exists to prevent.  Same for "CD Nacional II" against
    # "Nacional II".  It is the identical hole the alias path had, one lookup
    # over, and it stayed open when that one was closed.
    pin_words, pin_suffix = _split_identity_suffix(words)
    pinned = _SOCCER_DISAMBIGUATIONS.get("".join(pin_words))
    if pinned is not None:
        # Folded here too.  The fold below happens *after* this early return, so
        # the two pinned clubs were the only ones whose women's and reserve
        # sides still split four ways — ``Barcelona SC W`` / ``Women`` /
        # ``Ladies`` / ``Fem``.  Every unpinned club folded correctly, which is
        # why nothing noticed.
        # Folded **and sorted**, exactly as the main path below does.
        #
        # The fold was added here when the main path got it; the sort was not,
        # so a competition supplying two markers ("w u17") split this club's key
        # by the order they happened to arrive in — ``barcelonascwu17`` against
        # ``barcelonascu17w`` for one side.  Two pinned clubs, and the same miss
        # the comment above calls out for the fold.
        return pinned + "".join(
            sorted(_SUFFIX_SPELLINGS.get(word, word) for word in pin_suffix)
        )

    # Clubs: drop pure club-type affixes and name-internal conjunctions, but
    # never an identity suffix.
    kept = [
        word
        for word in words
        if (word not in _CLUB_TYPE_TOKENS and word not in _CLUB_CONJUNCTIONS)
        or word in _IDENTITY_SUFFIXES
    ]
    # If stripping removed everything, the name *was* only club-type tokens and
    # carries no identity.
    if not kept:
        return None
    # A name that reduced to nothing but identity suffixes — "II", "W",
    # "Reserves" — is a marker with no club attached, not a club.
    #
    # Except where one of those markers *is* a club's whole name; see
    # :data:`_MARKER_WORDS_THAT_NAME_A_CLUB`.  "Junior W" is then the club's
    # women's side and keeps both words, exactly as "Freiburg W" does.
    if all(word in _IDENTITY_SUFFIXES for word in kept) and not (
        _MARKER_WORDS_THAT_NAME_A_CLUB.intersection(kept)
    ):
        return None
    kept = [_SUFFIX_SPELLINGS.get(word, word) for word in kept]
    # The identity markers are sorted; the club's own words are not.
    #
    # A competition can supply more than one marker, and a team may already
    # carry some of them — so one venue writes "Freiburg U17" and gets ``w``
    # appended while another writes "Freiburg" and gets ``w u17``.  Joined in
    # arrival order those are ``freiburgu17w`` and ``freiburgwu17``: the same
    # side, split by nothing but the order two markers happened to arrive in.
    # A club's own words are order-bearing ("Manchester United" is not "United
    # Manchester"); a set of markers is not.
    club, suffix = _split_identity_suffix(kept)
    # Trailing founding year, after the identity suffix is split off so that
    # "Como 1907 W" keeps its ``w``.  Never when it is the club's only word.
    if len(club) > 1 and _TRAILING_FOUNDING_YEAR.match(club[-1]):
        club = club[:-1]
    kept = club + sorted(suffix)
    slug = "".join(kept)
    aliased = _SOCCER_ALIASES.get(slug)
    if aliased is not None:
        return aliased
    # An alias keyed on the club alone still has to reach the club's women's,
    # reserve and youth sides, which carry a suffix the key cannot contain.
    # Without this, aliasing "Junior de Barranquilla" onto "Junior" left their
    # *women's* sides split — the same defect one identity suffix further down,
    # and the reason the first Junior fix traded a visible rejection for a
    # silent split rather than closing it.
    club, suffix = _split_identity_suffix(kept)
    if suffix:
        aliased = _SOCCER_ALIASES.get("".join(club))
        if aliased is not None:
            return aliased + "".join(suffix)
    return slug


def _split_identity_suffix(kept: list[str]) -> tuple[list[str], list[str]]:
    """Split trailing identity markers off the club that carries them.

    Never empties the club: "Junior" is both a marker word and the whole name,
    so a rule that stripped every suffix would leave nothing to alias.
    """
    cut = len(kept)
    while cut > 1 and kept[cut - 1] in _IDENTITY_SUFFIXES:
        cut -= 1
    return kept[:cut], kept[cut:]


def _pairing_pattern(sport: Sport | None) -> re.Pattern[str]:
    """Which separators name two competitors, for this kind of competitor."""
    if sport is None or sport in _INDIVIDUAL_SPORTS:
        return _PAIRING_WITH_AMPERSAND
    return _PAIRING


#: A competition whose *name* carries the marker that separates a women's,
#: reserve or youth side from the senior men's one.
#:
#: The identity suffixes above work when the marker is on the competitor —
#: "Sturm Graz II", "Freiburg (W)" — and are blind when it lives only in the
#: competition.  Pinnacle routes every unrecognised soccer competition to one
#: catch-all league and the league name is then read by nothing, so
#: ``Club Friendlies Women`` Utrecht v De Graafschap produced an ``event_key``
#: byte-identical to the men's fixture.
#:
#: Nothing downstream can recover from that: ``dedup_key`` includes ``source``,
#: so the storage constraint is blind across sources; the participant pair is
#: identical, so ``participant_pair_disagreement`` cannot fire; and soccer's
#: 30-hour clustering tolerance puts an afternoon women's game and an evening
#: men's game in one fixture.  Injecting one men's fixture beside the real
#: women's rows produced an **11.2% "guaranteed" position ranked first in the
#: whole run**.
_COMPETITION_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Every spelling of "women's" a soccer competition name is published under,
    # not just the two this started with.
    #
    # ``\bwomen'?s?\b|\bladies\b|\bfem(?:enin[ao])?\b`` matched English and
    # Spanish and nothing else, so the German Frauen-Bundesliga, Italian Serie A
    # Femminile, Brazilian Campeonato Brasileiro Feminino, Mexican Liga MX
    # Femenil, Dutch Eredivisie Vrouwen, French D1 Féminine, the Scandinavian
    # leagues and the WSL all produced a **men's** event key — the exact fault
    # this marker exists to prevent, in every language but two.  Brazil matters
    # concretely: ``football/brazil`` is a default Kambi path and Pinnacle routes
    # unrecognised soccer into the catch-all.
    #
    # Competition names only.  A false positive here mislabels a whole
    # competition, so each alternative is a word that does not occur in a men's
    # competition name: no bare ``dam`` (Amsterdam), no bare ``fem``.
    (
        re.compile(
            r"\bwomen'?s?\b"
            r"|\bladies\b"
            r"|\bfem(?:in|en)?in[aeo]?\b|\bfemenil\b|\bfeminil\b|\bfemminile\b"
            r"|\bfrauen\b"
            r"|\bvrouwen\b"
            r"|\bdamallsvenskan\b|\bdamer\b"
            r"|\bkvinde\w*\b|\bkvinne\w*\b"
            r"|\bwsl\b"
            r"|\bnwsl\b",
            re.I,
        ),
        "w",
    ),
    (re.compile(r"\bu-?(\d{2})\b", re.I), "u"),
    (re.compile(r"\breserves?\b", re.I), "reserves"),
    (re.compile(r"\b(?:youth|junior|juvenil)\b", re.I), "youth"),
)


def competition_marker(competition_name: str | None, sport: Sport | None = None) -> str | None:
    """The identity marker a competition's own name implies, if any.

    Returned as one of the tokens :data:`_IDENTITY_SUFFIXES` already knows, so a
    caller can append it to a competitor's name and get the same key it would
    have had if the venue had put the marker there itself.

    Only for **clubs**.  The marker exists because one club fields a senior
    men's side, a women's side and a reserve side under one name, so the
    competition is the only thing telling them apart.  A player is not like
    that: Polona Hercog is Polona Hercog whether the draw is called "ITF Women"
    or not, and appending a marker to her split 50 tennis fixtures away from
    every other book — a regression this argument was written after causing.
    """
    if not competition_name or (sport is not None and sport in _INDIVIDUAL_SPORTS):
        return None
    # Folded first.  The patterns are ASCII and the venues are not: "France - D1
    # Féminine" and "Première Ligue Féminine" matched nothing and produced a
    # **men's** event key, which is the fault this whole mechanism exists to
    # prevent — and the unaccented spelling of the same competition matched, so
    # whether it worked depended on which venue wrote it.
    folded = _fold(competition_name)
    # **Every** marker the name carries, not the first one matched.
    #
    # Returning on the first match meant a women's *youth* competition keyed
    # identically to the senior women's fixture between the same clubs:
    # "Frauen-Bundesliga" and "Frauen-Bundesliga U17" both gave ``w``, so the
    # two produced byte-identical event keys and soccer's 30-hour clustering
    # joined them.  The participant pair is the same, so no orientation or
    # pairing check can see it — the same false-join shape the marker exists to
    # prevent, one age group over.
    #
    # Space-separated, and :func:`with_marker` appends only the components a
    # name does not already carry, so ``"w u17"`` on a team already written
    # "U17" adds just the ``w``.
    markers: list[str] = []
    for pattern, token in _COMPETITION_MARKERS:
        found = pattern.search(folded)
        if found is None:
            continue
        if token == "u":
            age = found.group(1)
            token = f"u{age}" if f"u{age}" in _IDENTITY_SUFFIXES else "youth"
        if token not in markers:
            markers.append(token)
    return " ".join(markers) or None


def with_marker(name: str | None, marker: str | None) -> str | None:
    """Attach a competition's identity marker to a competitor, if it needs one.

    A venue that puts the marker on the *competition* usually leaves it off the
    teams — that is the whole reason :func:`competition_marker` exists — but not
    always, and not consistently within one venue.  Brazil's U20 championship is
    the case that proved it: Kambi names the competition ``Campeonato
    Brasileiro U20`` **and** the teams ``Palmeiras-SP U20``, so appending the
    marker produced ``palmeirasspu20u20`` for the fixture while the betoffer's
    own outcome labels still said ``palmeirasspu20``.  The two no longer
    matched, and 36 rows became ``unknown_selection`` rejections — which marks
    the whole source failed.

    Compared after folding the spellings, so ``"Aston Villa Women"`` is not
    given a second ``w``.
    """
    if not name or not marker:
        return name
    words = {
        _SUFFIX_SPELLINGS.get(word, word)
        for word in _words(name, drop_parentheticals=False)
    }
    # Component by component: a competition can carry more than one marker —
    # "Frauen-Bundesliga U17" is both — and a team may already carry some of
    # them and not others.
    missing = [part for part in marker.split() if part not in words]
    return f"{name} {' '.join(missing)}" if missing else name


def is_pairing(raw: str | None, sport: Sport | None = None) -> bool:
    """Does *raw* name two competitors rather than one?

    Adapters need this to tell "out of scope" from "broken".  A tennis doubles
    entry — ``"Luis Carlos Alvarez / Alan Magadan"`` — is deliberately
    unresolvable, but it is a market this collector does not cover rather than a
    participant it failed to recognise.  Without the distinction an adapter
    reports a rejection, and a rejection fails the whole source: one doubles
    match in Pinnacle's tennis slate took the entire book's run down.

    *sport* decides whether ``&`` separates two competitors or belongs to one
    club's name; see :data:`_PAIRING_WITH_AMPERSAND`.  Adapters always know it,
    and pass it.
    """
    return bool(raw) and bool(_pairing_pattern(sport).search(raw))


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
