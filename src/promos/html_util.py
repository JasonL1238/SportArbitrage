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


def strip_tags(text: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html.unescape(text))).strip()


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


__all__ = [
    "absolute_url",
    "hrefs",
    "meta_description",
    "next_data",
    "page_title",
    "strip_tags",
]
