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
        then_hash: str | None = None,
    ) -> tuple[list[PageObservation], list[WebSocketObservation]]:
        """Navigate the real page and capture its XHR/fetch/GraphQL traffic.

        This is deliberately a research primitive, not a parser.  It never
        returns cookies, authorization headers, or unsanitized tokens.  Venue
        adapters may later reproduce a proven public request directly.

        *then_hash* sets ``location.hash`` **after** the app has had time to
        boot, which is the only way to reach a route inside a hash-routed
        single-page app.  A deep link supplied in *url* does not work on one:
        the fragment is read by the app's own router once, during a boot that
        has not happened yet when ``goto`` resolves, so the initial hash is
        simply ignored — bet365's Illinois board answered a deep-linked MLB
        route with the home page and no league request at all.  Clicking is not
        a substitute either, since the nav text may be present before the
        handler that routes on it.  This performs the same **same-document**
        navigation the app's own menu performs.
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

        def diagnostic(
            method: str, url: str, resource_type: str, detail: str
        ) -> PageObservation:
            """A non-response event, in the shape the manifest already stores.

            ``status_code=0`` is what marks these apart from a real response:
            no HTTP exchange completed, so there is no status to report.  Using
            :class:`PageObservation` rather than a new type is deliberate —
            ``write_observations`` serializes whatever it is handed, so these
            reach the manifest with no schema change on either side.
            """
            return PageObservation(
                method=method,
                url=sanitize_url(url),
                status_code=0,
                resource_type=resource_type,
                request_headers={},
                response_headers={},
                request_body=None,
                response_body=sanitize_body(detail) or "",
            )

        def on_response(response: Any) -> None:
            request = response.request
            resource_type = str(request.resource_type)
            request_url = str(request.url)
            if not wanted(request_url, resource_type):
                return
            try:
                body = response.text()
            except Exception:  # noqa: BLE001 - an unreadable body is recorded as empty
                body = ""
            try:
                post_data = request.post_data
            except Exception:  # noqa: BLE001 - a gzip/protobuf body is not text, and
                # Playwright raises rather than returning bytes.  Recording it as
                # absent keeps the response in the manifest; letting it raise here
                # dropped every later response on the page, because this handler
                # runs inside Playwright's event emitter and the error propagates
                # out of the capture rather than into it.  Observed 2026-08-12 on
                # Caesars IL, where telemetry beacons post gzip.
                post_data = None
            responses.append(
                PageObservation(
                    method=str(request.method),
                    url=sanitize_url(request_url),
                    status_code=int(response.status),
                    resource_type=resource_type,
                    request_headers=safe_request_headers(request.headers),
                    response_headers=safe_response_headers(response.headers),
                    request_body=sanitize_body(post_data),
                    response_body=sanitize_body(body) or "",
                )
            )

        def on_request_failed(request: Any) -> None:
            """Record a request that never completed.

            Without this a page whose preloader stalls *waiting on a resource
            that failed* produces no evidence at all: the ``response`` handler
            only ever sees exchanges that finished, so the manifest of a stalled
            session and the manifest of a session that simply had nothing to
            fetch are indistinguishable.  The failure reason names the cause.
            """
            try:
                failure = str(request.failure or "")
            except Exception:  # noqa: BLE001 - the reason is best-effort evidence
                failure = ""
            responses.append(
                diagnostic(
                    "FAILED",
                    str(request.url),
                    str(request.resource_type),
                    failure or "request failed with no reason reported",
                )
            )

        def on_console(message: Any) -> None:
            """Record console errors and warnings, sanitized.

            An application that refuses to boot usually says why here first.
            Only error and warning levels are kept: ``log``/``debug`` on a
            sportsbook shell is high-volume telemetry, and the point is a
            manifest a human can read.
            """
            try:
                level = str(message.type)
                if level not in {"error", "warning"}:
                    return
                text = str(message.text)
                location = message.location or {}
                url = str(location.get("url", "")) if location else ""
            except Exception:  # noqa: BLE001 - a malformed message is not fatal
                return
            responses.append(diagnostic("CONSOLE", url, level, text))

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

        handlers = (
            ("response", on_response),
            ("requestfailed", on_request_failed),
            ("console", on_console),
            ("websocket", on_websocket),
        )
        for event, handler in handlers:
            self._page.on(event, handler)
        try:
            self._page.goto(
                url, wait_until="domcontentloaded", timeout=self._timeout_ms
            )
            remaining = max(0, wait_ms)
            if then_hash:
                # A third of the budget to boot, then route, then the rest to
                # let the route's own traffic arrive.  Split rather than fixed
                # so a caller can buy more of either by raising ``wait_ms``.
                before_route = min(20_000, remaining // 3)
                self._page.wait_for_timeout(before_route)
                remaining -= before_route
                try:
                    self._page.evaluate("h => { location.hash = h; }", then_hash)
                except Exception as exc:  # noqa: BLE001 - recorded, never fatal,
                    # for the same reason a missed click is: the traffic captured
                    # before the route is still evidence.
                    responses.append(
                        diagnostic(
                            "ROUTE",
                            url,
                            "hash",
                            f"{then_hash!r} not applied: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
            if click_text:
                before_click = min(4_000, remaining // 3)
                self._page.wait_for_timeout(before_click)
                remaining -= before_click
                try:
                    self._page.get_by_text(click_text, exact=False).first.click(
                        timeout=self._timeout_ms
                    )
                except Exception as exc:  # noqa: BLE001 - a missed selector is a
                    # *finding*, not a reason to discard the session.  Letting it
                    # propagate returned before the caller wrote anything, so a
                    # three-minute capture was thrown away because one piece of
                    # navigation text did not match — and the whole point of this
                    # primitive is trial and error on navigation text.  Record
                    # which text missed and keep waiting: the traffic captured
                    # before the click is still evidence.
                    responses.append(
                        diagnostic(
                            "CLICK",
                            url,
                            "click",
                            f"{click_text!r} not found: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
            self._page.wait_for_timeout(remaining)
        finally:
            # Handlers are per-call, so a session observed twice would otherwise
            # count every response of the second pass once per previous call.
            for event, handler in handlers:
                try:
                    self._page.remove_listener(event, handler)
                except Exception:  # noqa: BLE001 - removal is best-effort cleanup
                    pass
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
