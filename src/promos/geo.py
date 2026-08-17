"""US state / Canada province codes and eligibility text parsers."""
from __future__ import annotations

import re
from typing import Iterable

US_STATES: frozenset[str] = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    }
)

CA_PROVINCES: frozenset[str] = frozenset(
    {
        "AB",
        "BC",
        "MB",
        "NB",
        "NL",
        "NS",
        "NT",
        "NU",
        "ON",
        "PE",
        "QC",
        "SK",
        "YT",
    }
)

ALL_REGIONS: frozenset[str] = US_STATES | CA_PROVINCES

DEFAULT_US_PROMO_REGIONS: tuple[str, ...] = (
    "AZ",
    "CO",
    "CT",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "MI",
    "NJ",
    "NY",
    "OH",
    "PA",
    "TN",
    "VA",
    "WV",
    "WY",
)

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "ontario": "ON",
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "quebec": "QC",
    "saskatchewan": "SK",
    "nova scotia": "NS",
    "new brunswick": "NB",
    "newfoundland": "NL",
    "newfoundland and labrador": "NL",
    "prince edward island": "PE",
}

_NAME_RE = re.compile(
    r"\b("
    + "|".join(sorted((re.escape(n) for n in _STATE_NAMES), key=len, reverse=True))
    + r")\b",
    re.I,
)

#: Explicit jurisdiction contexts (positive).
_ELIGIBLE_CTX = re.compile(
    r"(?:(?:is|are|currently)\s+)?"
    r"(?:available|eligible|offered|valid|open|"
    r"(?:physically\s+)?located|residen(?:t|ce)|operat(?:es|ing))\s+"
    r"(?:only\s+)?(?:in|to|of)\s+"
    r"([^.!?\n]{2,400})",
    re.I,
)
# These grabs deliberately do NOT stop at ";" — legal copy separates state lists
# with semicolons ("Not available in NV; NY residents are also excluded"), and a
# ";" boundary made that exclusion fail open. The generated-note feedback loop
# this once guarded against is closed at the source instead: enrich_offer never
# feeds eligibility_notes back into the parse.
# Two verb families: the first excludes with or without a preposition
# ("excludes New York"); the second is exclusionary only WITH one — bare
# "void" is market boilerplate ("bets are void if the match is abandoned")
# and grabbing its clause false-excluded the states named after it.
# Verb families by required preposition: the first excludes with or without
# one ("excludes New York"); void/prohibited/banned need in/for/to (bare
# "void" is market boilerplate); restricted/barred need in/from — "restricted
# TO New Jersey" means NJ-only-eligible, and reading it as exclusion
# fabricated a stored NJ veto.  "not (open|permitted|…)" tolerates modal
# auxiliaries and "be": "will not be permitted to participate".  The chunk
# cap is 600: a spelled-out multi-state exclusion list routinely exceeds
# 200, and every state past the cap escaped the veto.
_NOT_ELIGIBLE_FORM = (
    r"(?:(?:will|shall|may|can|do(?:es)?)\s+)?not\s+(?:be\s+)?"
    r"(?:open|offered|valid|permitted|allowed|eligible|available)"
)
# The first family tolerates adjacent punctuation ("except: NJ, NY, PA" is
# ordinary legal formatting) — requiring whitespace let a colon-introduced
# list bypass the veto and reach the bare-list fallback as innocent codes.
_EXCLUDE_CTX = re.compile(
    r"(?:(?:not\s+available|unavailable|excluding|excludes?|"
    r"except(?:ing)?(?:\s+for)?|but\s+not|other\s+than|ineligible)"
    r"\b\s*[:\-–—]?\s*(?:in|for|to)?"
    r"|(?:void|prohibited|banned)\s+(?:in|for|to)"
    r"|(?:restricted|barred)\s+(?:in|from)"
    r"|" + _NOT_ELIGIBLE_FORM + r"\s+(?:in|for|to))"
    r"\s*[:\-–—]?\s*([^.!?\n]{2,600})",
    re.I,
)
_NOT_AVAILABLE_SPAN = re.compile(
    r"(?:(?:not\s+available|unavailable|ineligible)\b\s*[:\-–—]?\s*(?:in|for|to)?"
    r"|(?:void|prohibited|banned)\s+(?:in|for|to)"
    r"|(?:restricted|barred)\s+(?:in|from)"
    r"|" + _NOT_ELIGIBLE_FORM + r"\s+(?:in|for|to))"
    r"\s*[:\-–—]?\s*[^.!?\n]{2,600}",
    re.I,
)

#: Comma-separated abbreviation lists: ``AR, AZ, CO, … WV``.
_CODE_LIST = re.compile(
    r"\b((?:[A-Z]{2})(?:\s*,\s*[A-Z]{2}){2,}(?:\s*(?:,\s*)?(?:(?i:and)\s+|&\s*)?[A-Z]{2})?)\b"
)
#: Exclusion vocabulary that marks a clause geographic-exclusionary no matter
#: its shape.  "may not <verb>" is deliberately separate: it excludes a place
#: only with a non-substantive tail — "NV residents may not participate" is
#: geography, "residents may not participate more than once" is a frequency
#: cap, and "may not bet on in-state college teams" is a market restriction.
#: "except" is deliberately NOT here: its eligible-side work is done by
#: _ELIGIBLE_CHUNK_STOP (which cuts the chunk before the forfeit check ever
#: runs), while in the ";"-fragment filter it dropped fragments whole —
#: "Not available in NJ except as otherwise stated" lost its NJ veto, and
#: an adapter-stamped NJ stayed confirmed against exclusion copy.
_EXCLUSION_WORD = re.compile(
    r"\b(?:exclud\w*|ineligible|not\s+available|unavailable|"
    r"void|prohibit\w*|banned|restricted(?!\s+to)|barred|"
    r"(?:(?:will|shall|may|can|do(?:es)?)\s+)?not\s+(?:be\s+)?"
    r"(?:eligible|open|offered|valid|permitted|allowed)|"
    r"do(?:es)?\s+not\s+qualify)\b",
    re.I,
)
#: An exclusion trigger *inside* an eligible chunk starts the exception
#: tail: "available in NJ, PA and WV except New York" keeps the head
#: eligible and leaves the tail to the exclusion scan.
_ELIGIBLE_CHUNK_STOP = re.compile(
    r"\bexcept(?:ing)?(?:\s+for)?\b|\bexcluding\b|\bbut\s+not\b|"
    r"\bother\s+than\b",
    re.I,
)
_MAY_NOT = re.compile(
    r"\bmay\s+not\s+(?:participate|claim|play|bet|redeem|enter|qualify)\b"
    r"(?P<tail>[^.;!?\n]*)",
    re.I,
)
#: A may-not verb is a *geographic* exclusion by default; only a tail that
#: names a conduct object (frequency, market, channel) reads as a conduct
#: cap.  The earlier polarity — a whitelist of acceptable exclusion tails —
#: failed open: "may not participate in this promotion due to state
#: regulations" fell off the whitelist and the state stayed confirmable.
_CONDUCT_MARKER = re.compile(
    r"\bmore\s+than\b|\bagain\b|\bmultiple\b|\btwice\b|"
    r"\bper\s+(?:day|week|month|year|customer|person|account|household|"
    r"offer|promotion|promo|bet|wager)\b|"
    r"\bin\s+conjunction\b|\bcombin\w*\b|\bstack\w*\b|\bemploy\w*\b|"
    r"\bon\s+(?:in-state|any|specific|certain|college|professional)\b",
    re.I,
)


def _carries_geo_exclusion(clause: str) -> bool:
    """Does this clause exclude a *place*, as opposed to restricting conduct?

    Any clause that does must never feed an eligible-side pass — that is the
    `_FRAGMENT_EXCLUSION` principle, generalized: it now guards whole-clause
    masking and the ``;``-fragment filter alike, in both code and name form.
    """
    if _EXCLUSION_WORD.search(clause):
        return True
    for m in _MAY_NOT.finditer(clause):
        if not _CONDUCT_MARKER.search(m.group("tail") or ""):
            return True
    return False


#: Clause-level suffix exclusions: "OR residents are also excluded",
#: "New York, AZ, and OR customers are excluded", "Customers located in NJ
#: are excluded", "Any resident of New York may not participate".  The verb
#: anchors the reading, so even English-colliding codes are safe here.  The
#: pass anchors on the person word *nearest* the verb — an earlier person
#: word in the same clause ("open to players in … and Nevada residents are
#: excluded") must not swallow the true anchor into the gap — and reads the
#: region list on whichever side of the noun it sits.
_EXCLUDE_VERB = re.compile(
    r"\b(?:(?:are|is)\s+(?:also\s+)?(?:excluded|ineligible|not\s+eligible)"
    r"|(?:will|shall)\s+not\s+be\s+(?:eligible|permitted|allowed)"
    r"|do(?:es)?\s+not\s+qualify"
    r"|may\s+not\s+(?:participate|claim|play|bet|redeem|enter|qualify)\b"
    r"(?P<tail>[^.;!?\n]*))",
    re.I,
)
_PERSON_WORD = re.compile(
    r"\b(?:residents?|players?|customers?|users?|members?|bettors?|"
    r"patrons?|persons?|individuals?)\b",
    re.I,
)
_CLAUSE = re.compile(r"[^.;!?\n]+")
_WALK_TOKEN = re.compile(r"[A-Za-z]+|,|&")


def _regions_before(prefix: str) -> list[str]:
    """The region list ending at *prefix*'s tail — codes and spelled-out
    names, read backward, tolerating commas, ``&``, and and/or conjunctions
    (Oxford comma included).  Uppercase ``OR`` reads as Oregon even where an
    all-caps list means it as a conjunction — over-excluding fails closed.
    The walk stops at the first token that is neither a region nor a
    separator, so prose ahead of the list never joins it.  An eligible list
    glued to the exclusion by ", and" IS swallowed — over-exclusion vetoes a
    state (fail-closed), where the comma-stop this replaced under-excluded
    and left adapter-stamped states confirmable (fail-open)."""
    tokens = _WALK_TOKEN.findall(prefix)
    found: list[str] = []
    i = len(tokens) - 1
    while i >= 0:
        tok = tokens[i]
        low = tok.lower()
        if tok in {",", "&"}:
            i -= 1
            continue
        if len(tok) == 2 and tok.isupper() and tok in ALL_REGIONS:
            found.append(tok)
            i -= 1
            continue
        if low in {"and", "or"}:
            i -= 1
            continue
        if i >= 2 and (
            code := _STATE_NAMES.get(
                f"{tokens[i - 2].lower()} {tokens[i - 1].lower()} {low}"
            )
        ):
            found.append(code)
            i -= 3
            continue
        if i >= 1 and (code := _STATE_NAMES.get(f"{tokens[i - 1].lower()} {low}")):
            found.append(code)
            i -= 2
            continue
        if code := _STATE_NAMES.get(low):
            found.append(code)
            i -= 1
            continue
        break
    found.reverse()
    out: list[str] = []
    for code in found:
        if code not in out:
            out.append(code)
    return out


#: Canada licence forms like ``CA-ON`` / ``CA-ONT`` (Ontario), not California.
#: Three-letter spellings appear in live DraftKings terms ("located in
#: CA-ONT") — the two-letter-only pattern left "CA" to read as California.
_CA_PROV = re.compile(r"\bCA-([A-Z]{2,3})\b")
_CA_PROV_ALIASES = {"ONT": "ON", "QUE": "QC", "ALB": "AB", "SAS": "SK", "MAN": "MB"}


def _ca_prov_code(raw: str) -> str:
    return _CA_PROV_ALIASES.get(raw, raw)


def normalize_region(value: str) -> str | None:
    text = (value or "").strip().upper()
    if text in ALL_REGIONS:
        return text
    name = (value or "").strip().lower()
    return _STATE_NAMES.get(name)


#: Region codes that double as ordinary English words or abbreviations; a
#: clause fragment like "ID verification required" or "OK to combine" must not
#: read as Idaho or Oklahoma.
_AMBIGUOUS_CODES = frozenset({"ID", "IN", "OK", "OR", "ON", "HI", "ME"})


def _add_region_token(token: str, seen: set[str], out: list[str]) -> None:
    text = re.sub(r"[^A-Za-z]", " ", token or "").strip()
    if not text:
        return
    code = _STATE_NAMES.get(text.lower())
    if code and code not in seen:
        seen.add(code)
        out.append(code)
        return
    # Two-letter fallback: uppercase in the source only — lowercase "or"/"in"
    # after a separator is English, not Oregon/Indiana.
    m = re.match(r"^([A-Z]{2})\b", text)
    if not m:
        return
    code = m.group(1)
    if code in _AMBIGUOUS_CODES:
        rest = text.split()[1:]
        # Only a bare code or "XX only" reads as a state here.  Person-word
        # clauses ("OR residents are also excluded") carry their own verb and
        # polarity, so they are parsed by the _SUFFIX_EXCLUDE_VERB pass at
        # the clause level — feeding them into the surrounding context's list
        # inverted "available in NJ; OR customers are excluded" into
        # OR-eligible.
        if rest and rest != ["only"]:
            return
    if code in ALL_REGIONS and code not in seen:
        seen.add(code)
        out.append(code)


_PERSON_OF = re.compile(
    r"^(?:[A-Za-z]+\s+){0,2}?(?:residents?|players?|customers?|users?|"
    r"members?|bettors?|patrons?|persons?|individuals?)\s+(?:of|in)\s+",
    re.I,
)


def _codes_from_separated_list(chunk: str) -> list[str]:
    """Parse ``NJ or PA``, ``AR, AZ, OR, IN`` style lists without English false positives.

    Splits on commas / semicolons / ``and`` only — never on ``or`` — so Oregon
    (``OR``) survives.  Semicolons matter on the exclusion side: legal copy
    writes "Not available in NV; NY residents are also excluded", and losing
    the post-semicolon clause fails open.
    Pairwise ``X or Y`` region mentions are collected with an explicit regex.
    """
    if not chunk:
        return []
    seen: set[str] = set()
    out: list[str] = []
    cleaned = _CA_PROV.sub(lambda m: f" {_ca_prov_code(m.group(1))} ", chunk)
    # Drop clause fragments carrying their own *geographic* exclusion before
    # any pair/list matching — they belong to parse_eligibility's suffix
    # pass.  Conduct restrictions ("users may not redeem more than once")
    # are not exclusions and must not cost the fragment its eligible list.
    cleaned = ";".join(
        part for part in cleaned.split(";") if not _carries_geo_exclusion(part)
    )
    # Explicit "NJ or PA" / "New Jersey or Pennsylvania" pairs.
    pair = re.compile(
        r"\b([A-Z]{2}|[A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)*)\s+or\s+"
        r"([A-Z]{2}|[A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)*)\b",
        re.I,
    )
    for match in pair.finditer(cleaned):
        _add_region_token(match.group(1), seen, out)
        _add_region_token(match.group(2), seen, out)
    # Comma / semicolon / and lists (includes Oregon as a normal item).
    parts = re.split(r"\s*(?:,|/|;|\band\b)\s*", cleaned, flags=re.I)
    for part in parts:
        _add_region_token(part, seen, out)
        # "residents of KS" — a person-noun prefix hides the code from the
        # leading-token rule; read the trailing region list after of/in.
        noun = _PERSON_OF.match(part)
        if noun:
            for code in _regions_before(part):
                if code not in seen:
                    seen.add(code)
                    out.append(code)
    return out


def _codes_from_list_chunk(chunk: str, *, comma_list: bool) -> list[str]:
    """Extract region codes from a jurisdiction list chunk (not free prose)."""
    if not chunk:
        return []
    # Prefer separator-aware parsing — handles "NJ or PA" and "AR, AZ, IN".
    separated = _codes_from_separated_list(chunk)
    if separated:
        return separated
    # Fall back to full state/province names only.
    seen: set[str] = set()
    out: list[str] = []
    for match in _NAME_RE.finditer(chunk):
        code = _STATE_NAMES.get(match.group(1).lower())
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    if out or not comma_list:
        return out
    scrubbed = _CA_PROV.sub(" ", chunk.upper())
    for token in re.findall(r"\b[A-Z]{2}\b", scrubbed):
        if token in ALL_REGIONS and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def regions_from_text(text: str) -> list[str]:
    """Extract region codes from jurisdiction-flavored text (not free prose).

    Bare English words like ``in`` / ``or`` / ``on`` are never treated as
    Indiana / Oregon / Ontario outside an explicit state-code list.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _NAME_RE.finditer(text):
        code = _STATE_NAMES.get(match.group(1).lower())
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    # Scrub CA-ON / CA-AB before scanning comma lists so CA is not California.
    upper = _CA_PROV.sub(lambda m: f" {_ca_prov_code(m.group(1))} ", text.upper())
    for match in _CODE_LIST.finditer(upper):
        for code in _codes_from_list_chunk(match.group(1), comma_list=True):
            if code not in seen:
                seen.add(code)
                out.append(code)
    for match in _CA_PROV.finditer(text.upper()):
        code = _ca_prov_code(match.group(1))
        if code in CA_PROVINCES and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _list_continuation(fragment: str) -> bool:
    """A ``;``-fragment that is only more of the same region list.

    Legal copy separates state lists with semicolons ("…in NJ; NY; PA"), so
    the eligible grab may ride a ';' — but ONLY across fragments made of
    regions and their separators.  Any other word starts a new clause whose
    polarity is its own business, which is the structural guarantee that no
    exclusion phrasing, recognized or not, can ride an eligible grab across
    a semicolon.
    """
    text = _CA_PROV.sub(lambda m: f" {_ca_prov_code(m.group(1))} ", fragment)
    words = re.findall(r"[A-Za-z]+", text)
    if not words:
        return False
    i = 0
    while i < len(words):
        low = words[i].lower()
        if low in {"and", "or", "only"}:
            i += 1
            continue
        if len(words[i]) == 2 and words[i].isupper() and words[i] in ALL_REGIONS:
            i += 1
            continue
        if (
            i + 2 < len(words)
            and f"{low} {words[i + 1].lower()} {words[i + 2].lower()}" in _STATE_NAMES
        ):
            i += 3
            continue
        if i + 1 < len(words) and f"{low} {words[i + 1].lower()}" in _STATE_NAMES:
            i += 2
            continue
        if low in _STATE_NAMES:
            i += 1
            continue
        return False
    return True


def _narrow_eligible_chunk(chunk: str) -> str:
    """The grab's own fragment plus pure region-list continuations."""
    parts = chunk.split(";")
    kept = [parts[0]]
    for part in parts[1:]:
        if not _list_continuation(part):
            break
        kept.append(part)
    return ";".join(kept)


#: Negated-eligible wording that forfeits a grab even though no ineligible
#: pass can attribute it ("participation from New York is not permitted").
_NEGATED_ELIGIBLE = re.compile(
    r"\b(?:(?:will|shall|may|can|do(?:es)?)\s+)?not\s+(?:be\s+)?"
    r"(?:open|available|offered|valid|permitted|allowed|eligible)\b|"
    r"\bdo(?:es)?\s+not\s+qualify\b|"
    r"\brestricted(?!\s+to)\b|\bbarred\b|"
    r"\b(?:can\s*not|cannot|can['\u2019]t)\b"
    r"(?!\s+be\s+(?:combined|stacked)\b|\s+be\s+used\s+in\s+conjunction)",
    re.I,
)


def _chunk_forfeits(chunk: str) -> bool:
    """No state may be confirmed from a grab whose own text carries exclusion
    signal — partial salvage of a mixed-polarity chunk is where every
    inversion of rounds six through eight lived.  Forfeited states may still
    reach ``ineligible`` through the exclusion passes; they simply cannot be
    *confirmed* by this grab.  Conduct-tail may-nots do not forfeit."""
    return _carries_geo_exclusion(chunk) or bool(_NEGATED_ELIGIBLE.search(chunk))


def _mask_geo_exclusion_clauses(text: str) -> str:
    """*text* with every geographic-exclusion clause blanked, same length.

    No eligible-side pass may read a clause that excludes a place — the
    eligible grabs deliberately read through ";" and scan spelled-out names,
    so without this mask "available in New Jersey; void in New York" fed NY
    to the eligible list whenever the exclusion verb was one the ineligible
    passes did not recognize.  Masking is the structural guarantee; the
    ineligible-wins filter remains as defense in depth.
    """
    out = list(text)
    mask_next = False
    for m in _CLAUSE.finditer(text):
        clause = m.group(0)
        carries = _carries_geo_exclusion(clause)
        if carries or mask_next:
            for i in range(m.start(), m.end()):
                out[i] = " "
        # "Excluded states:\nNJ, NY, PA" — the carrier ends in ":" (or an
        # open list comma) and its list sits in following clauses, innocent
        # on their own; without this carry the bare-list fallback confirmed
        # the excluded states.  The carry chains while clauses remain pure
        # region lists, so a wrapped list's later lines stay masked too.
        open_tail = clause.rstrip().lower().endswith(
            (":", ",", " and", " or", "&")
        ) or text[m.end() : m.end() + 1] == ";"
        mask_next = (carries and open_tail) or (
            mask_next and _list_continuation(clause)
        )
    return "".join(out)


#: A polarity flip inside an exclusion grab: an eligible verb that OPENS a
#: new clause — a boundary or conjunction followed only by glue words.  The
#: verb-position rule this replaces cut at any eligible-verb *appearance*,
#: and "located" lives inside exclusion phrasing ("Excludes players located
#: in Michigan") — the cut emptied the grab and the eligible pass then
#: CONFIRMED the excluded state.  "located" is deliberately absent from the
#: flip verbs, and a verb reached across real words ("…, or customers
#: located in Nevada") is the exclusion's own list, not a flip.
_POLARITY_FLIP = re.compile(
    r"(?:[,;]|\b(?:and|or|but)\b)"
    r"(?:\s*(?:and|or|but)\b)?"
    r"(?:\s*\b(?:the|this|that|offer|promotion|promo|it|is|are|was|be|"
    r"will|also|only|now|still|currently|remains|stays))*"
    r"\s*(?:available|offered|valid|open|eligible)\b",
    re.I,
)


def _truncate_exclude_chunk(chunk: str) -> str:
    """Cut an exclusion grab at a clause-initial polarity flip.

    "excludes casino games, available to customers in NJ" must not read NJ
    as excluded; "void in New York and only valid in New Jersey" must keep
    NJ out of the veto.  ("ineligible" never trips the check: no word
    boundary precedes its embedded "eligible", and "not eligible" cannot
    match because "not" is not a glue word.)"""
    m = _POLARITY_FLIP.search(chunk)
    if not m:
        return chunk
    return chunk[: m.start()]


#: A newline that merely wraps a list is not a clause boundary.  Rounds 13,
#: 14, 15, and 16 each found another mechanism severed by a wrap — colon
#: lists, comma tails, semicolons, dashes, wordless glyph lines,
#: verb-after-list sentences — because every defense operated on one side
#: of the "\n".  Joining the wrap *before* any parsing lets each mechanism
#: see whole sentences: a newline preceded (ignoring trailing spaces) by an
#: open-list token, or followed by a conjunction line, becomes a space, and
#: blank/bullet filler between is consumed with it.
_WRAP_JOIN = re.compile(
    r"(?:(?<=[,;:&])|(?<=[\-–—])|(?<=\band)|(?<=\bor)|(?<=\bAND)|(?<=\bOR))"
    r"[ \t]*\n[\s\u00a0•*\-]*"
    r"|[ \t]*\n(?=[ \t]*(?:and|or)\b)",
    re.I,
)


def _join_wrapped_lists(text: str) -> str:
    return _WRAP_JOIN.sub(" ", text)


def parse_eligibility(text: str) -> tuple[list[str], list[str], str]:
    """Return (eligible, ineligible, notes) parsed from promo copy."""
    if not text:
        return [], [], ""
    text = _join_wrapped_lists(text)
    eligible: list[str] = []
    ineligible: list[str] = []
    notes: list[str] = []

    masked = _mask_geo_exclusion_clauses(text)
    # The eligible grabs read the *unmasked* text: masking whole clauses ate
    # comma-spliced eligible lists sharing a clause with a market exclusion
    # ("Parlays and teasers are excluded, offer available in NJ and PA").
    # Chunk narrowing and forfeit below carry the mask's guarantee instead;
    # the bare-list fallback, which has no chunk to inspect, keeps the mask.
    scrubbed = _NOT_AVAILABLE_SPAN.sub(" ", text)

    # A manual scan, not finditer: the greedy 600-char grab at "Excludes …"
    # consumes a later "except New York" whole, and the flip cut then threw
    # the tail away un-rescanned — the genuine exclusion vanished and the
    # eligible pass confirmed NY.  Restarting the search from each chunk's
    # own start lets nested exclusion verbs match in their own right.
    scan_pos = 0
    while True:
        match = _EXCLUDE_CTX.search(text, scan_pos)
        if match is None:
            break
        scan_pos = match.start(1)
        chunk = match.group(1)
        end = match.end(1)
        # A long exclusion list wraps: the chunk stops at "\n" mid-list
        # ("not available in NJ, NY, PA,\nWV, MI, IL") and the tail lines
        # reached the bare-list fallback as innocent codes — which
        # CONFIRMED them.  While the chunk ends open (comma, conjunction,
        # colon) and the next line is a pure region list, the list
        # continues and the veto covers it.
        while (
            end < len(text)
            and text[end] == "\n"
            and chunk.rstrip().lower().endswith((",", " and", " or", "&", ":", ";"))
        ):
            nxt = _CLAUSE.match(text, end + 1)
            if nxt is None or not _list_continuation(nxt.group(0)):
                break
            chunk = f"{chunk.rstrip()} {nxt.group(0)}"
            end = nxt.end()
        chunk = _truncate_exclude_chunk(chunk)
        found = _codes_from_list_chunk(chunk, comma_list=False)
        for code in regions_from_text(chunk):
            if code not in found:
                found.append(code)
        for code in found:
            if code not in ineligible:
                ineligible.append(code)
        if found:
            notes.append(f"Not available in: {', '.join(found)}")

    # Clause-level exclusions ride inside either context's chunk ("available
    # in NJ; OR customers are excluded") and carry their own verb — in code
    # or spelled-out-name form.  Every clause of the text is scanned, so a
    # stand-alone "New York residents are excluded." sentence lands too, not
    # only clauses inside a context grab.  The anchor is the person word
    # *nearest* the verb, and the region list may sit on either side of it
    # ("NV residents…" / "residents of NV…").
    suffix_excluded: list[str] = []
    for clause_m in _CLAUSE.finditer(text):
        clause = clause_m.group(0)
        for match in _EXCLUDE_VERB.finditer(clause):
            tail = match.groupdict().get("tail")
            if tail is not None and _CONDUCT_MARKER.search(tail):
                continue  # conduct restriction, not geography
            before = clause[: match.start()]
            persons = list(_PERSON_WORD.finditer(before))
            if not persons:
                continue
            person = persons[-1]
            if match.start() - person.end() > 60:
                continue
            codes = _regions_before(before[: person.start()])
            if not codes:
                # "residents of NY and NJ are excluded" — the list follows
                # the noun; it is the exclusion's own list, so commas are
                # always list separators here.
                codes = _regions_before(before[person.end() :])
            for code in codes:
                if code not in ineligible:
                    ineligible.append(code)
                    suffix_excluded.append(code)
    if suffix_excluded:
        notes.append(f"Not available in: {', '.join(suffix_excluded)}")

    for match in _ELIGIBLE_CTX.finditer(scrubbed):
        start = match.start()
        prefix = scrubbed[max(0, start - 4) : start].lower()
        if prefix.endswith("not "):
            continue
        chunk = _narrow_eligible_chunk(match.group(1))
        stop = _ELIGIBLE_CHUNK_STOP.search(chunk)
        if stop:
            chunk = chunk[: stop.start()]
        if _chunk_forfeits(chunk):
            continue
        # Names + comma lists only — never ambiguous English words.
        found = regions_from_text(chunk)
        # Also pull unambiguous 2-letter codes from the chunk (NJ, NY, …).
        for code in _codes_from_list_chunk(chunk, comma_list=False):
            if code not in found:
                found.append(code)
        kept = [c for c in found if c not in ineligible]
        for code in kept:
            if code not in eligible:
                eligible.append(code)
        if kept:
            notes.append(f"Eligible in: {', '.join(kept)}")

    # Bare comma-separated abbreviation lists anywhere in terms (never in a
    # masked exclusion clause).
    if not eligible:
        upper = _CA_PROV.sub(lambda m: f" {_ca_prov_code(m.group(1))} ", masked.upper())
        for match in _CODE_LIST.finditer(upper):
            for code in _codes_from_list_chunk(match.group(1), comma_list=True):
                if code not in ineligible and code not in eligible:
                    eligible.append(code)
        if eligible:
            notes.append(f"Eligible in: {', '.join(eligible)}")

    note = "; ".join(notes)
    lower = text.lower()
    extras: list[str] = []
    if re.search(r"new\s+(?:customers?|players?|users?|members?)", lower):
        extras.append("New customers only")
    if re.search(r"existing\s+(?:customers?|players?|users?)", lower):
        extras.append("Existing customers")
    if re.search(r"opt[- ]?in", lower):
        extras.append("Opt-in required")
    if re.search(r"21\+|must be 21|age 21", lower):
        extras.append("21+")
    if re.search(r"19\+|must be 19|age 19", lower):
        extras.append("19+")
    if extras:
        note = "; ".join(x for x in (note, *extras) if x)
    return eligible, ineligible, note


def merge_regions(*groups: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            code = normalize_region(str(item)) or str(item).strip().upper()
            if not code or code not in ALL_REGIONS or code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


__all__ = [
    "ALL_REGIONS",
    "CA_PROVINCES",
    "DEFAULT_US_PROMO_REGIONS",
    "US_STATES",
    "merge_regions",
    "normalize_region",
    "parse_eligibility",
    "regions_from_text",
]
