"""TheLines sportsbook-promos aggregator — Action Network–style failover for bonuses.

``https://www.thelines.com/betting/sportsbook-promos/`` lists exact current
welcome offers, game promos, and ongoing boosts across US books.  Action
Network has no equivalent public promo feed; this page is the parallel for
promotions.  One HTML response is shared by many ``tl_*`` registry tenants,
each filtering to one brand.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from datetime import UTC, datetime
from typing import Sequence

import httpx

from src.promos.base import PromoParseOutcome
from src.promos.classify import classify_kind
from src.promos.html_util import strip_tags
from src.promos.schema import PromoKind, PromoOffer
from src.raw_store import RawResponse
from src.sources._common import envelope_source, latest_per_endpoint
from src.sources.guards import BlockedError, CaptchaError

DEFAULT_URL = "https://www.thelines.com/betting/sportsbook-promos/"
ENDPOINT = "sportsbook-promos"

#: brand_key → substrings that identify that book in TheLines copy.
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "betmgm": ("betmgm", "bet mgm"),
    "draftkings": ("draftkings", "draft kings"),
    "fanduel": ("fanduel", "fan duel"),
    "caesars": ("caesars",),
    "bet365": ("bet365",),
    "hardrock": ("hard rock", "hardrock"),
    "fanatics": ("fanatics",),
}

_HEADING_SPLIT = re.compile(r"(<h[23][^>]*>.*?</h[23]>)", re.I | re.S)
_CODE = re.compile(
    r"(?:use\s+\w+\s+)?code\s+([A-Z0-9]{4,20})\b",
    re.I,
)
_GAME_PROMO = re.compile(
    r"\$|\bbonus bets?\b|\bfree bets?\b|\bprofit boost\b|\bodds boost\b|"
    r"\bno sweat\b|\bdeposit\b|\bget up to\b|\bbet \$",
    re.I,
)
_SKIP_HEADINGS = re.compile(
    r"^(best sportsbook promos|sportsbook promos explained|how to claim|"
    r"best sportsbook bonuses finder|best way to use|sportsbook promos for existing|"
    r"best sportsbook bonus for today|exciting games|top sportsbook promos by|"
    r"best sportsbook promos by sport|how our experts|different types|"
    r"no deposit bonus|sign-up or welcome|first-bet promo|referral bonus|"
    r"conclusion:|sportsbook promos faqs|nfl promos|nba promos|ufc promos|"
    r"college |nhl promos|mlb promos|wnba promos|latest sportsbook game promos|"
    r"promo codes$)",
    re.I,
)

#: Short in-process cache so seven ``tl_*`` tenants share one HTTP fetch per collect.
_PAGE_CACHE: dict[str, tuple[float, RawResponse]] = {}
_CACHE_TTL_S = 90.0


class TheLinesPromoAdapter:
    """Multi-tenant parser over TheLines' sportsbook-promos page."""

    def __init__(
        self,
        *,
        source_key: str,
        brand_key: str,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        url: str = DEFAULT_URL,
    ) -> None:
        if brand_key not in BRAND_ALIASES:
            raise ValueError(f"unknown TheLines brand_key {brand_key!r}")
        self._source_key = source_key
        self.brand_key = brand_key
        self.url = url
        # Fetch with plain httpx — not SourceClient.  TheLines' page legitimately
        # embeds Cloudflare CDN assets, so the odds-pipeline CDN branding scan
        # would refuse a healthy 200.  curl_cffi Chrome also gets challenged
        # here while browser-like httpx does not.
        owns_client = client is None
        if owns_client:
            client = httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.thelines.com/",
                },
            )
        self._owns_client = owns_client
        self._client = client

    @property
    def source_key(self) -> str:
        return self._source_key

    def fetch_raw(self) -> list[RawResponse]:
        cached = _PAGE_CACHE.get(self.url)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _CACHE_TTL_S:
            raw = cached[1]
            return [
                RawResponse(
                    source=self.source_key,
                    endpoint=raw.endpoint,
                    url=raw.url,
                    status_code=raw.status_code,
                    body=raw.body,
                    fetched_at=raw.fetched_at,
                    content_type=raw.content_type,
                    request_params=dict(raw.request_params),
                    headers=dict(raw.headers),
                    capture_id=raw.capture_id,
                )
            ]
        assert self._client is not None
        response = self._client.get(self.url)
        body = response.text
        lowered = body[:4000].lower()
        where = f"{self.source_key}:{ENDPOINT}"
        if response.status_code in {401, 403, 407} or (
            "attention required" in lowered
            or "just a moment" in lowered
            or "cf-browser-verification" in lowered
            or "challenge-platform" in lowered
        ):
            if "challenge" in lowered or "captcha" in lowered:
                raise CaptchaError(f"{where}: CAPTCHA/bot challenge served")
            raise BlockedError(f"{where}: access blocked (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise BlockedError(f"{where}: HTTP {response.status_code}")
        raw = RawResponse(
            source=self.source_key,
            endpoint=ENDPOINT,
            url=str(response.url),
            status_code=response.status_code,
            body=body,
            fetched_at=datetime.now(UTC),
            content_type=response.headers.get("content-type"),
            # The shared cleaner, not a local copy of it: this used to filter
            # ``set-cookie`` alone, which is one of the seven names on the denylist,
            # so an ``authorization`` or ``x-api-key`` echoed back by the origin was
            # written to a promo envelope on disk.
            headers=RawResponse.clean_headers(response.headers),
        )
        _PAGE_CACHE[self.url] = (now, raw)
        return [raw]

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        outcome = PromoParseOutcome()
        for raw in latest_per_endpoint(raws):
            if raw.endpoint != ENDPOINT:
                outcome.skipped["foreign_endpoint"] += 1
                continue
            source = envelope_source((raw,), fallback=self.source_key)
            offers = parse_thelines_offers(
                raw.body,
                source_key=source,
                brand_key=self.brand_key,
                observed_at=raw.fetched_at,
                raw_ref=raw.ref,
                page_url=self.url,
            )
            if not offers:
                outcome.skipped["no_brand_offers"] += 1
                continue
            outcome.offers.extend(offers)
        return outcome

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass


def parse_thelines_offers(
    body: str,
    *,
    source_key: str,
    brand_key: str,
    observed_at,
    raw_ref: str = "",
    page_url: str = DEFAULT_URL,
) -> list[PromoOffer]:
    """Pure parser — used by the adapter and fixture tests."""
    aliases = BRAND_ALIASES[brand_key]
    parts = _HEADING_SPLIT.split(body)
    offers: list[PromoOffer] = []
    seen: set[str] = set()

    for index, part in enumerate(parts):
        if not re.match(r"<h[23]", part, re.I):
            continue
        heading = html.unescape(strip_tags(part)).strip()
        heading = re.sub(r"\s+", " ", heading)
        if not heading or _SKIP_HEADINGS.search(heading):
            continue
        nxt = parts[index + 1] if index + 1 < len(parts) else ""
        text = html.unescape(strip_tags(nxt))
        text = re.sub(r"\s+", " ", text).strip()

        brand_hit = _brand_in(heading, aliases) or _brand_in(text[:400], aliases)
        if not brand_hit:
            continue

        section_kind = _section_kind(heading, aliases)
        if section_kind == "welcome":
            for title, description in _welcome_from_section(heading, text, aliases):
                _append_offer(
                    offers,
                    seen,
                    source_key=source_key,
                    brand_key=brand_key,
                    title=title,
                    description=description,
                    observed_at=observed_at,
                    raw_ref=raw_ref,
                    page_url=page_url,
                    raw_kind="welcome",
                )
        elif section_kind == "ongoing":
            for line in _ongoing_lines(text):
                if not _brand_in(line, aliases) and not _brand_in(heading, aliases):
                    # Section is already brand-scoped; lines inherit the brand.
                    pass
                _append_offer(
                    offers,
                    seen,
                    source_key=source_key,
                    brand_key=brand_key,
                    title=line,
                    description="",
                    observed_at=observed_at,
                    raw_ref=raw_ref,
                    page_url=page_url,
                    raw_kind="ongoing",
                )
        elif section_kind == "game" or (
            _GAME_PROMO.search(heading) and _brand_in(heading, aliases)
        ):
            _append_offer(
                offers,
                seen,
                source_key=source_key,
                brand_key=brand_key,
                title=heading,
                description=text[:400],
                observed_at=observed_at,
                raw_ref=raw_ref,
                page_url=page_url,
                raw_kind="game_promo",
            )
        elif section_kind == "finder":
            for title, description in _finder_rows(text, aliases):
                _append_offer(
                    offers,
                    seen,
                    source_key=source_key,
                    brand_key=brand_key,
                    title=title,
                    description=description,
                    observed_at=observed_at,
                    raw_ref=raw_ref,
                    page_url=page_url,
                    raw_kind="bonus_finder",
                )

    return offers


def _brand_in(text: str, aliases: Sequence[str]) -> bool:
    lower = text.lower()
    return any(alias in lower for alias in aliases)


def _section_kind(heading: str, aliases: Sequence[str]) -> str | None:
    lower = heading.lower().strip()
    if "bonuses finder" in lower:
        return "finder"
    if _GAME_PROMO.search(heading) and any(a in lower for a in aliases):
        # "$1500 … Use BetMGM Code …" style game promos
        if "code" in lower or re.search(r"\bvs\b|\bmlb\b|\bnba\b|\bnfl\b", lower):
            return "game"
    for alias in aliases:
        if lower in {f"{alias} bonus", f"{alias} promo", f"{alias} sportsbook promo"}:
            return "welcome"
        # "BetMGM Bonus" after normalizing spaces already in aliases list items
        compact = lower.replace(" ", "")
        alias_c = alias.replace(" ", "")
        if compact in {f"{alias_c}bonus", f"{alias_c}promo", f"{alias_c}sportsbookpromo"}:
            return "welcome"
        if lower.endswith("sportsbook promos") and alias in lower:
            return "ongoing"
        if lower == f"{alias} sportsbook promos":
            return "ongoing"
    # Hard Rock Bet Promo etc.
    if _brand_in(heading, aliases) and re.search(r"\b(bonus|promo)\b", lower):
        if lower.endswith("promos"):
            return "ongoing"
        return "welcome"
    return None


#: Explicit multi-promo markers inside one welcome review section.
_WELCOME_ORDINAL = re.compile(r"\bThe (first|second|third|fourth):\s*", re.I)


def _welcome_from_section(
    heading: str, text: str, aliases: Sequence[str]
) -> list[tuple[str, str]]:
    """Pull concrete welcome lines out of a brand review paragraph.

    A review section sometimes describes several distinct promos in one blob
    ("The first: … The second: …"), each with its own state list. Parsing the
    whole blob attributed every list to one offer — the "$150 if you win"
    MI/NJ/PA/WV-only promo stamped IL-confirmed on the first live run. Only
    explicit ordinal markers split a section; anything subtler risks
    fragmenting ordinary single-promo sections.
    """
    markers = list(_WELCOME_ORDINAL.finditer(text))
    if len(markers) >= 2:
        found: list[tuple[str, str]] = []
        seen_titles: set[str] = set()
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            span = text[marker.end() : end].strip()
            got = _welcome_from_span(span)
            if got is None:
                # A span with no recognizable welcome line stays its *own*
                # last-resort offer — falling through to the whole section
                # re-merged the state lists the split exists to keep apart.
                got = _last_resort_line(heading, span, aliases)
            title, description = got
            norm = re.sub(r"[^a-z0-9]+", "", title.lower())
            if norm in seen_titles:
                # Same mechanics offered under different state lists — the
                # ordinal keeps the second span from vanishing in title dedupe.
                title = f"{title} ({marker.group(1).lower()} offer)"
            seen_titles.add(norm)
            found.append((title, description))
        return found
    got = _welcome_from_span(text)
    if got is not None:
        return [got]
    return [_last_resort_line(heading, text, aliases)]


def _last_resort_line(
    heading: str, text: str, aliases: Sequence[str]
) -> tuple[str, str]:
    """A snippet-titled offer so enrich/deepen still has dollars to work on."""
    brand = next((a for a in aliases if a in heading.lower()), aliases[0])
    snippet = re.sub(r"\s+", " ", text).strip()
    money = re.search(r"\$[\d,]+[^.]{0,80}", snippet)
    if money:
        return (f"{brand.title()}: {money.group(0).strip()[:120]}", text[:500])
    return (f"{brand.title()} sportsbook promo (details on page)", text[:500])


def _welcome_from_span(text: str) -> tuple[str, str] | None:
    """One concrete welcome line from one promo's own span, or None."""
    patterns = (
        r"((?:Bet|Spend) \$\d+\+?(?:\s+and)?\s+get[^.!?]{0,80}(?:Bonus Bets?|FanCash|Bonuses|Instantly)[^.!?]{0,40})",
        r"(bet \$\d+[^.!?]{0,60}get[^.!?]{0,60})",
        r"(Get up to \$?[\d,]+[^.!?]{0,100})",
        r"(new players can get[^.!?]{0,100})",
        r"(sign[- ]?up bonus[^.!?]{0,80})",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip(" .")
            # Cut trailing review copy glued without punctuation.
            title = re.split(
                r"\s+(?:This sportsbook|The site|Well,|When you|For new|Our )\b",
                title,
                maxsplit=1,
            )[0].strip(" .")
            title = re.sub(
                r"\s+is currently available\b.*$", "", title, flags=re.I
            ).strip(" .")
            title = re.sub(
                r"\s+(?:which\s+is\s+)?(?:valid|available)\s+in\b.*$",
                "",
                title,
                flags=re.I,
            ).strip(" .")
            if len(title) >= 12:
                return title[:160], text[:500]
    # Never emit a bare "welcome offer" — keep hunting for $ / reward mechanics
    # elsewhere in the section before giving up.
    fallback_patterns = (
        r"((?:up to\s+)?\$[\d,]+(?:\s+in)?\s+(?:bonus bets?|free bets?|FanCash|site credit)[^.!?]{0,40})",
        r"((?:first bet|risk[- ]?free)[^.!?]{0,80}\$[\d,]+[^.!?]{0,40})",
        r"(\d+%\s+(?:deposit\s+)?match[^.!?]{0,60})",
    )
    for pat in fallback_patterns:
        m = re.search(pat, text, re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip(" .")
            if len(title) >= 10:
                return title[:160], text[:500]
    return None


def _ongoing_lines(text: str) -> list[str]:
    # Strip expiration footnotes so they do not glue onto the last bullet.
    text = re.split(r"\s*⌛\s*Expiration:|\s*Expiration:", text, maxsplit=1)[0]
    # Split jammed promo catalogue lines on known starters.
    pieces = re.split(
        r"(?=(?:NBA (?:Playoff|\d|SGP)|NHL \d|MLB |NFL |WNBA |Golf |"
        r"Daily |Refer-A-Friend|Refer a friend|Second-Chance|Double Your|"
        r"Bonus Bet Club|Early Payouts|Up to \d|Earn up to))",
        text,
    )
    out: list[str] = []
    seen: set[str] = set()
    for chunk in pieces:
        line = re.sub(r"\s+", " ", chunk).strip(" -–—\t")
        # Keep a short trailing clause after a colon when present.
        if ":" in line:
            head, tail = line.split(":", 1)
            tail = tail.strip()
            if len(tail) > 60:
                tail = tail[:57] + "…"
            line = f"{head.strip()}: {tail}" if tail else head.strip()
        if len(line) < 14 or len(line) > 140:
            continue
        if not re.search(
            r"boost|bonus|refer|no[- ]sweat|free bet|profit|odds|payout|fancash|"
            r"reward|double your|second-chance|deposit|bet \$|early payout",
            line,
            re.I,
        ):
            continue
        norm = re.sub(r"[^a-z0-9]+", "", line.lower())
        if norm in seen:
            continue
        seen.add(norm)
        out.append(line)
    return out[:10]


def _finder_rows(text: str, aliases: Sequence[str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    # "BetMGM Bonus Code Bet $10, Get $150…"
    for alias in aliases:
        pattern = re.compile(
            rf"({re.escape(alias)}\s+(?:bonus|promo)\s+code)\s+([^.]{{10,120}})",
            re.I,
        )
        for m in pattern.finditer(text):
            rows.append((f"{m.group(1).strip()}: {m.group(2).strip()}", ""))
    return rows


def _append_offer(
    offers: list[PromoOffer],
    seen: set[str],
    *,
    source_key: str,
    brand_key: str,
    title: str,
    description: str,
    observed_at,
    raw_ref: str,
    page_url: str,
    raw_kind: str,
) -> None:
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) < 8:
        return
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())
    if norm in seen:
        return
    seen.add(norm)
    code_m = _CODE.search(title) or _CODE.search(description)
    promo_code = code_m.group(1).upper() if code_m else ""
    # Title only: the description is TheLines' own *review* prose and names
    # the brand's other promos — "profit-boost tokens and odds boosts
    # routinely available" stamped kind=odds_boost on the $250 bonus-bet
    # welcome, and a parlay note on a straight-bet offer.
    kind = classify_kind(title)
    if kind is PromoKind.OTHER and re.search(r"welcome|sign[- ]?up|new (?:player|customer)", title, re.I):
        kind = PromoKind.SIGNUP_BONUS
    offer_id = hashlib.sha1(f"{brand_key}|{norm}".encode()).hexdigest()[:16]
    offers.append(
        PromoOffer(
            source=source_key,
            offer_id=offer_id,
            kind=kind,
            title=title[:200],
            description=(description or "")[:4000],
            url=page_url,
            observed_at=observed_at,
            raw_ref=raw_ref,
            raw_kind=raw_kind,
            product="sportsbook",
            metadata={
                "feed": "thelines",
                "brand_key": brand_key,
                "promo_code": promo_code,
            },
        )
    )


__all__ = [
    "BRAND_ALIASES",
    "DEFAULT_URL",
    "TheLinesPromoAdapter",
    "parse_thelines_offers",
]
