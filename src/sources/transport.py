"""HTTP transport tuned for reachability, not honesty about identity.

Default path: ``curl_cffi`` with a Chrome TLS fingerprint.  That is what opens
Akamai / CloudFront / Cloudflare edges that reject plain ``httpx``.  An optional
proxy (``ODDS_HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``) is applied when set.

Callers that pass an explicit client (tests with ``httpx.MockTransport``) keep
that client unchanged.  Only the *default* session is impersonated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlencode, urlsplit, urlunsplit


#: Chrome profile curl_cffi should mimic.  Bump when a fingerprint ages out.
DEFAULT_IMPERSONATE = os.environ.get("ODDS_IMPERSONATE", "chrome131")


def proxy_url() -> str | None:
    """First non-empty proxy env var, or None."""
    for key in ("ODDS_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


@dataclass
class _HttpResponse:
    """Minimal response shape :class:`SourceClient` already consumes."""

    status_code: int
    text: str
    headers: Mapping[str, str]
    url: str

    @property
    def request(self) -> Any:
        return type("Req", (), {"url": self.url})()


class ImpersonatedSession:
    """Chrome-fingerprinted session with an httpx-like ``get`` / ``close``."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        impersonate: str = DEFAULT_IMPERSONATE,
        proxy: str | None = None,
    ) -> None:
        from curl_cffi import requests as curl_requests

        self._timeout = timeout
        kwargs: dict[str, Any] = {
            "impersonate": impersonate,
            "timeout": timeout,
            "allow_redirects": True,
        }
        resolved = proxy if proxy is not None else proxy_url()
        if resolved:
            kwargs["proxy"] = resolved
        self._session = curl_requests.Session(**kwargs)
        self.impersonate = impersonate
        self.proxy = resolved

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _HttpResponse:
        response = self._session.get(
            url,
            params=None if params is None else dict(params),
            headers=dict(headers or {}),
            timeout=self._timeout,
            allow_redirects=True,
        )
        # curl_cffi may leave the request URL without the encoded query; rebuild
        # so envelopes store what was actually asked for.
        final_url = str(response.url)
        if params and "?" not in final_url:
            final_url = _with_query(final_url, params)
        header_map: MutableMapping[str, str] = {
            str(k): str(v) for k, v in response.headers.items()
        }
        return _HttpResponse(
            status_code=int(response.status_code),
            text=response.text or "",
            headers=header_map,
            url=final_url,
        )

    def close(self) -> None:
        self._session.close()


def build_default_client(*, timeout: float = 20.0, seed_url: str | None = None) -> Any:
    """Session used when an adapter does not inject its own client.

    Order of preference:
    1. ``ODDS_FETCH_MODE=browser`` → Playwright Chromium (hardest edges)
    2. Otherwise → ``curl_cffi`` Chrome TLS impersonation (+ optional proxy)
    """
    from src.sources.browser import browser_enabled, build_browser_client

    if browser_enabled():
        return build_browser_client(timeout=timeout, seed_url=seed_url)
    return ImpersonatedSession(timeout=timeout)


def _with_query(url: str, params: Mapping[str, Any]) -> str:
    parts = urlsplit(url)
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
