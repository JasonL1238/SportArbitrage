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
_EXCLUDE_CTX = re.compile(
    r"(?:not\s+available|unavailable|excluding|except(?:\s+for)?|ineligible)\s+"
    r"(?:in|for|to)?\s*([^.!?\n]{2,200})",
    re.I,
)
_NOT_AVAILABLE_SPAN = re.compile(
    r"(?:not\s+available|unavailable|ineligible)\s+(?:in|for|to)?\s*[^.!?\n]{2,200}",
    re.I,
)

#: Comma-separated abbreviation lists: ``AR, AZ, CO, … WV``.
_CODE_LIST = re.compile(
    r"\b((?:[A-Z]{2})(?:\s*,\s*[A-Z]{2}){2,}(?:\s*(?:,|and|&)\s*[A-Z]{2})?)\b"
)
#: Canada licence forms like ``CA-ON`` (Ontario), not California.
_CA_PROV = re.compile(r"\bCA-([A-Z]{2})\b")


def normalize_region(value: str) -> str | None:
    text = (value or "").strip().upper()
    if text in ALL_REGIONS:
        return text
    name = (value or "").strip().lower()
    return _STATE_NAMES.get(name)


def _add_region_token(token: str, seen: set[str], out: list[str]) -> None:
    text = re.sub(r"[^A-Za-z]", " ", token or "").strip()
    if not text:
        return
    code = _STATE_NAMES.get(text.lower())
    if code and code not in seen:
        seen.add(code)
        out.append(code)
        return
    m = re.match(r"^([A-Za-z]{2})\b", text)
    if not m:
        return
    code = m.group(1).upper()
    if code in ALL_REGIONS and code not in seen:
        seen.add(code)
        out.append(code)


def _codes_from_separated_list(chunk: str) -> list[str]:
    """Parse ``NJ or PA``, ``AR, AZ, OR, IN`` style lists without English false positives.

    Splits on commas / ``and`` only — never on ``or`` — so Oregon (``OR``) survives.
    Pairwise ``X or Y`` region mentions are collected with an explicit regex.
    """
    if not chunk:
        return []
    seen: set[str] = set()
    out: list[str] = []
    cleaned = _CA_PROV.sub(lambda m: f" {m.group(1)} ", chunk)
    # Explicit "NJ or PA" / "New Jersey or Pennsylvania" pairs.
    pair = re.compile(
        r"\b([A-Z]{2}|[A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)*)\s+or\s+"
        r"([A-Z]{2}|[A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)*)\b",
        re.I,
    )
    for match in pair.finditer(cleaned):
        _add_region_token(match.group(1), seen, out)
        _add_region_token(match.group(2), seen, out)
    # Comma / and lists (includes Oregon as a normal item).
    parts = re.split(r"\s*(?:,|/|\band\b)\s*", cleaned, flags=re.I)
    for part in parts:
        _add_region_token(part, seen, out)
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
    upper = _CA_PROV.sub(lambda m: f" {m.group(1)} ", text.upper())
    for match in _CODE_LIST.finditer(upper):
        for code in _codes_from_list_chunk(match.group(1), comma_list=True):
            if code not in seen:
                seen.add(code)
                out.append(code)
    for match in _CA_PROV.finditer(text.upper()):
        code = match.group(1)
        if code in CA_PROVINCES and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def parse_eligibility(text: str) -> tuple[list[str], list[str], str]:
    """Return (eligible, ineligible, notes) parsed from promo copy."""
    if not text:
        return [], [], ""
    eligible: list[str] = []
    ineligible: list[str] = []
    notes: list[str] = []

    scrubbed = _NOT_AVAILABLE_SPAN.sub(" ", text)

    for match in _EXCLUDE_CTX.finditer(text):
        chunk = match.group(1)
        found = _codes_from_list_chunk(chunk, comma_list=False)
        for code in regions_from_text(chunk):
            if code not in found:
                found.append(code)
        for code in found:
            if code not in ineligible:
                ineligible.append(code)
        if found:
            notes.append(f"Not available in: {', '.join(found)}")

    for match in _ELIGIBLE_CTX.finditer(scrubbed):
        start = match.start()
        prefix = scrubbed[max(0, start - 4) : start].lower()
        if prefix.endswith("not "):
            continue
        chunk = match.group(1)
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

    # Bare comma-separated abbreviation lists anywhere in terms.
    if not eligible:
        upper = _CA_PROV.sub(lambda m: f" {m.group(1)} ", text.upper())
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
