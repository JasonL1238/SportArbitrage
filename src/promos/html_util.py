"""Small HTML helpers for promo pages — stdlib only, no BeautifulSoup."""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_META_DESC_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
    re.I,
)
_OG_DESC = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_NEXT_DATA = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

#: Terms wording accepted in anchor labels (broad — "T&Cs apply" links out).
_TERMS_LABEL = re.compile(
    r"terms\s*(?:&(?:amp;)?|and)\s*conditions|\bt\s*&(?:amp;)?\s*cs?\b|"
    r"full\s+terms|significant\s+terms|terms\s+apply",
    re.I,
)
#: Terms wording accepted in *headings* (narrow — the ubiquitous bolded
#: "Terms apply." / "T&Cs apply" boilerplate on promo cards must not turn
#: everything after it into a marked terms slice).
_TERMS_HEADING_LABEL = re.compile(
    r"terms\s*(?:&(?:amp;)?|and)\s*conditions|full\s+terms|significant\s+terms",
    re.I,
)
_TERMS_HEADING = re.compile(
    r"<(h[1-6]|strong|b|summary|button)\b[^>]*>(.{0,120}?)</\1>", re.I | re.S
)
_SCRIPT_STYLE_OPEN = re.compile(r"<(script|style)\b", re.I)
_SCRIPT_STYLE_CLOSE = {
    "script": re.compile(r"</script\s*>", re.I),
    "style": re.compile(r"</style\s*>", re.I),
}
_ANCHOR = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.{0,160}?)</a>', re.I | re.S)
#: Sitewide legal pages a promo T&C link must not be confused with.  A bare
#: ``…/terms`` path or a ``/legal`` section is the house rules — LeoVegas'
#: ``/en-ca/terms`` replaced four offers' genuine terms with "Terms and
#: Conditions of Use of Services" before this shape was excluded.  The
#: second alternation denies every common house-rules slug ending a path —
#: ``/terms``, ``/terms.html``, ``/terms-conditions``,
#: ``/terms-and-conditions``, ``/terms_and_conditions``,
#: ``/general-terms-and-conditions`` — with query/fragment tails allowed.
#: A genuine promo T&C page under one of these names is sacrificed, which
#: fails safe: the offer keeps its parsed terms and the link is simply not
#: fetched.  Tested against the *resolved* URL, not the raw href, so a
#: slash-less relative ``href="terms"`` cannot slip past the path anchor.
_GENERIC_LEGAL_HREF = re.compile(
    r"terms-of-(?:service|use)|privacy|cookie|/legal(?:/|$|[?#])|"
    r"/(?:general[-_])?terms(?:[-_]?(?:and[-_]?)?conditions)?"
    r"(?:\.(?:html?|php|aspx?))?/?(?:$|[?#])",
    re.I,
)


def strip_tags(text: str) -> str:
    """Tag-stripped, entity-decoded text — in that order, like a browser.

    Unescaping first turned an ``&gt;`` inside a quoted attribute value
    into a literal ``>`` that terminated the tag early, and the rest of
    the attribute (Tailwind class soup, data attributes) survived as
    "visible" text — stored as promo terms in 92 rows across runs #7–#58.
    """
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def page_title(body: str) -> str | None:
    match = _TITLE.search(body)
    return strip_tags(match.group(1)) if match else None


def meta_description(body: str) -> str | None:
    for pattern in (_META_DESC, _META_DESC_REV, _OG_DESC):
        match = pattern.search(body)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def next_data(body: str) -> dict[str, Any] | None:
    match = _NEXT_DATA.search(body)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def hrefs(body: str, *, prefix: str | None = None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _HREF.finditer(body):
        href = html.unescape(match.group(1)).strip()
        if prefix is not None and not href.startswith(prefix):
            continue
        if href in seen:
            continue
        seen.add(href)
        found.append(href)
    return found


def absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)


def _scrub_scripts(body: str) -> str:
    """HTML with script, style, and comment bodies removed — a tag strip
    alone leaves their contents in (`<!-- <div>` reads as one tag, so a
    commented-out block's *text*, stale state lists included, survived as
    "visible").  Constructs are consumed left to right by first opener,
    matching browser tokenization: a `"<!--"` inside script code is JS, and
    a `"<script"` inside a comment is comment text — each ordering of two
    blanket passes had a mirror hole where one construct's opener inside
    the other truncated everything after it.  An *unclosed* construct still
    truncates: nothing after it is trustworthy text."""
    out: list[str] = []
    pos = 0
    # Each pattern's next hit is cached and re-searched only once the scan
    # passes it — re-searching both every iteration made the scan quadratic
    # when one construct dominates (a 300KB SSR page with 20k hydration
    # comment markers took ~4 s; 1MB of scripts ~11 s).
    comment = body.find("<!--")
    script = _SCRIPT_STYLE_OPEN.search(body)
    while True:
        if comment != -1 and comment < pos:
            comment = body.find("<!--", pos)
        if script is not None and script.start() < pos:
            script = _SCRIPT_STYLE_OPEN.search(body, pos)
        starts = [s for s in (comment, script.start() if script else -1) if s != -1]
        if not starts:
            out.append(body[pos:])
            break
        nxt = min(starts)
        out.append(body[pos:nxt])
        out.append(" ")
        if nxt == comment:
            # From nxt+2, not past the opener: HTML5 treats "<!-->" and
            # "<!--->" as *complete* empty comments, and searching beyond
            # them read the whole page as an unclosed comment.
            close = body.find("-->", nxt + 2)
            if close == -1:
                break
            pos = close + 3
        else:
            closer = _SCRIPT_STYLE_CLOSE[script.group(1).lower()].search(
                body, script.end()
            )
            if closer is None:
                break
            pos = closer.end()
    return "".join(out)


def visible_text(body: str, limit: int | None = None) -> str:
    """Tag-stripped page text with script/style bodies removed first.

    The promo store briefly carried ``@font-face`` CSS and inline JS as offer
    terms because a fallback used a bare tag strip; every whole-page text
    fallback should come through here instead.
    """
    if not body:
        return ""
    text = strip_tags(_scrub_scripts(body))
    return text[:limit] if limit is not None else text


def terms_slice(body: str, limit: int = 6000) -> str | None:
    """Text after a Terms/T&C *heading* — the slice that carries conditions.

    Heading tags only, deliberately: a plain-text fallback measured against
    the committed fixtures anchored on footer links, i18n labels, and script
    content, replacing signal-bearing page text with junk. Script and style
    bodies are removed first — a tag strip alone leaves their contents in.
    None when no heading exists, so callers keep their whole-page fallback.
    """
    if not body:
        return None
    cleaned = _scrub_scripts(body)
    start: int | None = None
    for match in _TERMS_HEADING.finditer(cleaned):
        label = strip_tags(match.group(2))
        # A consent control ("Accept Terms & Conditions" on a cookie banner)
        # names the terms without heading them — everything after it is the
        # page, not the conditions.
        if re.search(r"\b(?:accept|agree|consent)\b", label, re.I):
            continue
        if _TERMS_HEADING_LABEL.search(label):
            start = match.end()
            break
    if start is None:
        return None
    sliced = strip_tags(cleaned[start:])
    if len(sliced) < 40:
        return None
    return sliced[:limit]


def terms_links(base: str, body: str) -> list[str]:
    """Absolute URLs of anchors that read as T&C links (by label or href).

    Sitewide legal pages (terms-of-service, privacy) are excluded — a promo's
    terms link points at offer conditions, not the house rules.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _ANCHOR.finditer(body or ""):
        href = html.unescape(match.group(1)).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        label = strip_tags(match.group(2))
        if not (_TERMS_LABEL.search(label) or _TERMS_LABEL.search(href)):
            continue
        url = absolute_url(base, href)
        # The deny list runs on the resolved URL: a slash-less relative
        # href ("terms") resolves to exactly the house-rules path shape
        # the raw-href check let through.
        if _GENERIC_LEGAL_HREF.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


__all__ = [
    "absolute_url",
    "hrefs",
    "meta_description",
    "next_data",
    "page_title",
    "strip_tags",
    "terms_links",
    "terms_slice",
    "visible_text",
]
