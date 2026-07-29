"""The shared HTTP layer: pacing, retry, identification, capture.

Three books grew three copies of this and the copies had already drifted — only
one paced its requests, none retried, none sent a User-Agent.  Ten sources would
have been ten copies, and the one that mattered would have been the one nobody
looked at.

Politeness is the design property under test here.  Every source is somebody
else's public endpoint: a refusal that says "slow down" is honoured, a refusal
that says "not from there" is not retried at all, and every request says who is
asking.
"""
from __future__ import annotations

import httpx
import pytest

from src.sources._common import (
    USER_AGENT,
    HostPacer,
    RetryPolicy,
    SourceClient,
    Tier,
    parse_epoch_time,
    parse_iso_time,
)
from src.sources.guards import (
    BlockedError,
    GeoRestrictedError,
    RateLimitedError,
    ServerError,
    TransportError,
)


class _Recorder:
    """A sleep that records instead of sleeping."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _client(handler, *, sleep=None, retry=None, min_interval=0.0) -> SourceClient:
    return SourceClient(
        "testbook",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep or _Recorder(),
        retry=retry or RetryPolicy(),
        min_request_interval=min_interval,
    )


class TestRetry:
    def test_a_rate_limit_is_retried_and_the_servers_own_delay_is_honoured(self) -> None:
        """Kalshi rate-limited the source probe on its second request, and Kambi
        answers an unknown tenant with a literal ``429 "No access"``.  Guessing a
        backoff when the server has stated one is both ruder and slower."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.path)
            if len(attempts) < 3:
                return httpx.Response(429, json={"error": "slow down"},
                                      headers={"retry-after": "3"})
            return httpx.Response(200, json={"ok": True})

        sleep = _Recorder()
        client = _client(handler, sleep=sleep)
        raw = client.get("https://example.invalid/odds", endpoint="odds")
        assert raw.json() == {"ok": True}
        assert len(attempts) == 3
        assert sleep.slept == [3.0, 3.0]

    def test_a_stated_delay_is_capped_so_one_header_cannot_park_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "no"},
                                  headers={"retry-after": "86400"})

        sleep = _Recorder()
        client = _client(handler, sleep=sleep, retry=RetryPolicy(attempts=2, max_backoff_seconds=20.0))
        with pytest.raises(RateLimitedError):
            client.get("https://example.invalid/odds", endpoint="odds")
        assert sleep.slept == [20.0]

    def test_a_server_error_is_retried_with_exponential_backoff(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(503, text="oops", headers={"content-type": "text/plain"})
            return httpx.Response(200, json={"ok": True})

        sleep = _Recorder()
        client = _client(handler, sleep=sleep)
        client.get("https://example.invalid/odds", endpoint="odds")
        assert sleep.slept == [1.0, 2.0]

    def test_the_budget_is_finite_and_the_last_failure_is_raised(self) -> None:
        """A source that has failed three times in ten seconds is not going to
        succeed on the fourth, and continuing to ask is a small denial of service
        aimed at somebody's public endpoint."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, text="down", headers={"content-type": "text/plain"})

        client = _client(handler, retry=RetryPolicy(attempts=3))
        with pytest.raises(ServerError):
            client.get("https://example.invalid/odds", endpoint="odds")
        assert len(attempts) == 3

    def test_a_transport_failure_is_retried(self) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ConnectError("connection reset")
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        client.get("https://example.invalid/odds", endpoint="odds")
        assert len(attempts) == 2

    def test_a_transport_failure_that_never_clears_is_reported_as_one(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        client = _client(handler, retry=RetryPolicy(attempts=2))
        with pytest.raises(TransportError):
            client.get("https://example.invalid/odds", endpoint="odds")


class TestPermanentRefusalsAreNotRetried:
    """Retrying a refusal that can never be satisfied is not persistence."""

    @pytest.mark.parametrize(
        "status, body, expected",
        [
            (451, '{"error":"unavailable for legal reasons"}', GeoRestrictedError),
            (403, "denied", BlockedError),
            (200, '{"error":"not available in your region"}', GeoRestrictedError),
        ],
    )
    def test_permanent_refusals_are_asked_once(self, status, body, expected) -> None:
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status, text=body, headers={"content-type": "text/plain"})

        client = _client(handler)
        with pytest.raises(expected):
            client.get("https://example.invalid/odds", endpoint="odds")
        assert len(attempts) == 1


class TestIdentificationAndCapture:
    def test_every_request_says_who_is_asking(self) -> None:
        """An honest identifier is the minimum a public endpoint is owed: it says
        who is asking and how to make them stop.  It is not a disguise — nothing
        here pretends to be a browser to get past a check meant to exclude us."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"ok": True})

        _client(handler).get("https://example.invalid/odds", endpoint="odds")
        assert seen["user-agent"] == USER_AGENT
        assert "SportArbitrage" in USER_AGENT and "github.com" in USER_AGENT

    def test_the_response_is_captured_before_it_is_interpreted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"events": [1]},
                                  headers={"age": "12", "set-cookie": "secret=1"})

        raw = _client(handler).get(
            "https://example.invalid/odds", endpoint="odds", params={"market": "US-IL"}
        )
        assert raw.source == "testbook" and raw.endpoint == "odds"
        assert raw.json() == {"events": [1]}
        assert raw.headers["age"] == "12"
        assert "set-cookie" not in raw.headers, "credentials are never stored"
        assert raw.request_params == {"market": "US-IL"}

    def test_a_sent_parameter_can_be_kept_out_of_the_capture(self) -> None:
        """FanDuel's ``_ak`` is a static public application key rather than a
        credential, but a capture is a document that gets shared."""
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(dict(request.url.params))
            return httpx.Response(200, json={"ok": True})

        raw = _client(handler).get(
            "https://example.invalid/odds",
            endpoint="odds",
            params={"page": "SPORT", "_ak": "secret"},
            record_params={"page": "SPORT"},
        )
        assert sent["_ak"] == "secret"
        assert raw.request_params == {"page": "SPORT"}


class TestPacing:
    def test_requests_to_one_host_are_spaced(self) -> None:
        sleep = _Recorder()
        # The first call reads the clock once; the second reads it, sleeps the
        # remaining gap, and reads it again to record when it actually went.
        clock = iter([0.0, 0.05, 0.25])
        pacer = HostPacer(0.25, sleep=sleep)
        pacer.wait("https://a.invalid/one", now=lambda: next(clock))
        pacer.wait("https://a.invalid/two", now=lambda: next(clock))
        assert sleep.slept == [pytest.approx(0.20)]

    def test_two_hosts_do_not_pace_against_each_other(self) -> None:
        """The constraint belongs to the server, so it is keyed on host — but two
        different hosts have nothing to do with one another."""
        sleep = _Recorder()
        clock = iter([0.0, 0.0])
        pacer = HostPacer(0.25, sleep=sleep)
        pacer.wait("https://a.invalid/one", now=lambda: next(clock))
        pacer.wait("https://b.invalid/one", now=lambda: next(clock))
        assert sleep.slept == []

    def test_two_instances_of_one_adapter_share_the_hosts_budget(self) -> None:
        """Two Kambi tenants are one CDN's worth of load however many adapters
        are involved.  Pacing per instance would hit the host at N times the
        intended rate — which is exactly what adding tenants does."""
        from src.sources import _common

        assert _common._SHARED_PACER is not None
        first = SourceClient("a", client=httpx.Client())
        second = SourceClient("b", client=httpx.Client())
        try:
            assert first._pacer is second._pacer
        finally:
            first.close()
            second.close()


class TestTime:
    def test_iso_forms_the_feeds_actually_send(self) -> None:
        assert parse_iso_time("2026-07-28T22:41:00Z").hour == 22
        assert parse_iso_time("2026-07-28T22:41:00+00:00").hour == 22
        # Polymarket sends "2026-07-28 17:40:00+00", which is neither.
        assert parse_iso_time("2026-07-28 17:40:00+00").hour == 17
        # A naive stamp is read as UTC, which is what every feed omitting a zone
        # means; leaving it naive would fail the schema's own check instead.
        assert parse_iso_time("2026-07-28T22:41:00").tzinfo is not None
        assert parse_iso_time("") is None
        assert parse_iso_time("not a time") is None

    def test_the_epoch_unit_is_stated_rather_than_guessed(self) -> None:
        """A heuristic that reads 1785260400 as seconds and 1785260400000 as
        milliseconds works until a feed sends microseconds, and then it silently
        files a fixture in the year 58000."""
        assert parse_epoch_time(1785260400).year == 2026
        assert parse_epoch_time(1785260400000, unit="ms").year == 2026
        assert parse_epoch_time(None) is None
        assert parse_epoch_time("nonsense") is None
        with pytest.raises(ValueError):
            parse_epoch_time(1, unit="fortnights")


class TestTiers:
    def test_core_is_the_slate_and_full_adds_the_depth(self) -> None:
        assert not Tier.CORE.includes_depth
        assert Tier.FULL.includes_depth
        assert Tier("core") is Tier.CORE
