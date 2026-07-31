"""Headless Chromium fetch for edges that beat TLS impersonation alone.

Akamai / some Cloudflare setups need a real browser context (JS challenges,
sensor cookies) before API JSON will answer.  This module opens Chromium via
Playwright, optionally loads a seed page so the edge can mint cookies, then
``fetch``es the API URL inside that page.

Used when ``ODDS_FETCH_MODE=browser`` or when a source opts into browser fetch.
Requires ``playwright`` and a installed Chromium (``python -m playwright install chromium``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

# Bound for the POST helper so a parameter named ``json`` cannot shadow the
# stdlib module used to serialize the body.
_json = json


@dataclass(frozen=True)
class BrowserResponse:
    status_code: int
    text: str
    headers: Mapping[str, str]
    url: str

    @property
    def request(self) -> Any:
        return type("Req", (), {"url": self.url})()


def browser_enabled() -> bool:
    return os.environ.get("ODDS_FETCH_MODE", "").strip().lower() in {
        "browser",
        "playwright",
        "chromium",
    }


class BrowserSession:
    """One Chromium context reused across GETs for a source."""

    def __init__(
        self,
        *,
        timeout_ms: float = 30_000,
        seed_url: str | None = None,
        proxy: str | None = None,
        headless: bool = True,
    ) -> None:
        from playwright.sync_api import sync_playwright

        self._timeout_ms = timeout_ms
        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        self._page = self._context.new_page()
        if seed_url:
            self._page.goto(seed_url, wait_until="domcontentloaded", timeout=timeout_ms)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> BrowserResponse:
        return self._request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
    ) -> BrowserResponse:
        return self._request(
            "POST", url, params=params, headers=headers, json=json, data=data
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
    ) -> BrowserResponse:
        final = url
        if params:
            parts = urlsplit(url)
            query = urlencode({k: v for k, v in params.items() if v is not None})
            if parts.query:
                query = f"{parts.query}&{query}" if query else parts.query
            final = parts._replace(query=query).geturl()

        # context.request shares the browser cookie jar but is not subject to
        # page CORS — in-page fetch() fails on sportsbook-nash.* from the www
        # origin even when a real XHR from the app would succeed.
        request_headers = dict(headers or {})
        if method.upper() == "POST":
            body: Any
            if json is not None:
                body = _json.dumps(json)
                request_headers.setdefault("content-type", "application/json")
            else:
                body = data
            response = self._context.request.post(
                final,
                headers=request_headers,
                data=body,
                timeout=self._timeout_ms,
                fail_on_status_code=False,
            )
        else:
            response = self._context.request.get(
                final,
                headers=request_headers,
                timeout=self._timeout_ms,
                fail_on_status_code=False,
            )
        header_map = {str(k): str(v) for k, v in response.headers.items()}
        return BrowserResponse(
            status_code=int(response.status),
            text=response.text() or "",
            headers=header_map,
            url=str(response.url),
        )

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._pw.stop()


def build_browser_client(
    *,
    timeout: float = 20.0,
    seed_url: str | None = None,
) -> BrowserSession:
    from src.sources.transport import proxy_url

    return BrowserSession(
        timeout_ms=timeout * 1000,
        seed_url=seed_url,
        proxy=proxy_url(),
        headless=os.environ.get("ODDS_BROWSER_HEADED", "").strip() not in {"1", "true"},
    )
