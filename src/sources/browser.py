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
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlencode, urlsplit

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


@dataclass(frozen=True)
class PageObservation:
    """One sanitized response initiated by a real application page."""

    method: str
    url: str
    status_code: int
    resource_type: str
    request_headers: Mapping[str, str]
    response_headers: Mapping[str, str]
    request_body: str | None
    response_body: str


@dataclass(frozen=True)
class WebSocketObservation:
    """Sanitized text frames observed on one application WebSocket."""

    url: str
    sent: tuple[str, ...]
    received: tuple[str, ...]


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
        persistent_dir: str | Path | None = None,
        device_name: str | None = None,
        channel: str | None = None,
    ) -> None:
        from playwright.sync_api import sync_playwright

        self._timeout_ms = timeout_ms
        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if channel:
            launch_kwargs["channel"] = channel
        if proxy:
            launch_kwargs["proxy"] = _playwright_proxy(proxy)
        context_kwargs: dict[str, Any] = {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
        }
        if device_name:
            try:
                device = dict(self._pw.devices[device_name])
            except KeyError:
                self._pw.stop()
                raise ValueError(f"unknown Playwright device {device_name!r}") from None
            device.pop("default_browser_type", None)
            context_kwargs.update(device)
        if persistent_dir is not None:
            Path(persistent_dir).mkdir(parents=True, exist_ok=True)
            self._browser = None
            self._context = self._pw.chromium.launch_persistent_context(
                str(persistent_dir),
                **launch_kwargs,
                **context_kwargs,
            )
        else:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context(**context_kwargs)
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )
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

    def observe_page(
        self,
        url: str,
        *,
        wait_ms: float = 8_000,
        include: Callable[[str, str], bool] | None = None,
        click_text: str | None = None,
    ) -> tuple[list[PageObservation], list[WebSocketObservation]]:
        """Navigate the real page and capture its XHR/fetch/GraphQL traffic.

        This is deliberately a research primitive, not a parser.  It never
        returns cookies, authorization headers, or unsanitized tokens.  Venue
        adapters may later reproduce a proven public request directly.
        """
        from src.sources.research import (
            safe_request_headers,
            safe_response_headers,
            safe_websocket_frame,
            sanitize_body,
            sanitize_url,
        )

        responses: list[PageObservation] = []
        sockets: list[dict[str, Any]] = []

        def wanted(request_url: str, resource_type: str) -> bool:
            if include is not None:
                return include(request_url, resource_type)
            return resource_type in {"xhr", "fetch"}

        def on_response(response: Any) -> None:
            request = response.request
            resource_type = str(request.resource_type)
            request_url = str(request.url)
            if not wanted(request_url, resource_type):
                return
            try:
                body = response.text()
            except Exception:
                body = ""
            responses.append(
                PageObservation(
                    method=str(request.method),
                    url=sanitize_url(request_url),
                    status_code=int(response.status),
                    resource_type=resource_type,
                    request_headers=safe_request_headers(request.headers),
                    response_headers=safe_response_headers(response.headers),
                    request_body=sanitize_body(request.post_data),
                    response_body=sanitize_body(body) or "",
                )
            )

        def on_websocket(socket: Any) -> None:
            record: dict[str, Any] = {
                "url": sanitize_url(str(socket.url)),
                "sent": [],
                "received": [],
            }
            sockets.append(record)
            socket.on(
                "framesent",
                lambda payload: record["sent"].append(
                    safe_websocket_frame(payload)
                ),
            )
            socket.on(
                "framereceived",
                lambda payload: record["received"].append(
                    safe_websocket_frame(payload)
                ),
            )

        self._page.on("response", on_response)
        self._page.on("websocket", on_websocket)
        self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        remaining = max(0, wait_ms)
        if click_text:
            before_click = min(4_000, remaining // 3)
            self._page.wait_for_timeout(before_click)
            remaining -= before_click
            self._page.get_by_text(click_text, exact=False).first.click(
                timeout=self._timeout_ms
            )
        self._page.wait_for_timeout(remaining)
        websocket_observations = [
            WebSocketObservation(
                url=record["url"],
                sent=tuple(record["sent"]),
                received=tuple(record["received"]),
            )
            for record in sockets
        ]
        return responses, websocket_observations

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                self._pw.stop()

    def dom_snapshot(self) -> PageObservation:
        """Return rendered body text without the application's large scripts."""
        from src.sources.research import sanitize_body, sanitize_url

        return PageObservation(
            method="DOM",
            url=sanitize_url(str(self._page.url)),
            status_code=200,
            resource_type="dom",
            request_headers={},
            response_headers={"content-type": "text/plain"},
            request_body=None,
            response_body=sanitize_body(self._page.locator("body").inner_text()) or "",
        )


def _playwright_proxy(proxy: str) -> dict[str, str]:
    """Translate an authenticated proxy URL without logging its credentials."""
    parsed = urlsplit(proxy)
    if not parsed.hostname:
        return {"server": proxy}
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parsed.scheme or 'http'}://{host}"
    if parsed.port is not None:
        server += f":{parsed.port}"
    configured = {"server": server}
    if parsed.username is not None:
        configured["username"] = unquote(parsed.username)
    if parsed.password is not None:
        configured["password"] = unquote(parsed.password)
    return configured


def build_browser_client(
    *,
    timeout: float = 20.0,
    seed_url: str | None = None,
    proxy: str | None = None,
    state: str | None = None,
    persistent_dir: str | Path | None = None,
    device_name: str | None = None,
    channel: str | None = None,
) -> BrowserSession:
    from src.sources.transport import proxy_url

    return BrowserSession(
        timeout_ms=timeout * 1000,
        seed_url=seed_url,
        proxy=proxy if proxy is not None else proxy_url(state),
        headless=os.environ.get("ODDS_BROWSER_HEADED", "").strip() not in {"1", "true"},
        persistent_dir=persistent_dir,
        device_name=device_name,
        channel=channel or os.environ.get("ODDS_BROWSER_CHANNEL") or None,
    )
