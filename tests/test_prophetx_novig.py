"""The credentialed-exchange adapters: ProphetX and Novig.

Both ship **unregistered** — per the operator's decision they stay out of the
registry until credentials are supplied and a genuine capture exists — so
nothing here touches ``tests/fixtures/raw``.  The payloads below are built
in-test from the venues' *documented* schemas (docs.prophetx.co and
docs.novig.com, read 2026-08-09) purely to exercise the parsers' logic; they
are not captures, are committed nowhere near the fixture tree, and the first
genuine capture supersedes both the payloads and any assumption they encode.

What is load-bearing here, in order of consequence:

1. **No secret ever enters an envelope.**  The login/token responses are the
   secrets, so they must never appear among the raws ``fetch_raw`` returns;
   the credentials themselves must never appear in any envelope's URL, params,
   body or headers.
2. **A missing credential refuses before the first socket**, as
   ``login_required`` — not after an unauthenticated probe of a partner API.
3. **Both keys stay out of the registry** while being reserved on the
   credentialed axis, so registration is a decision and not a drift.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src import settings
from src.raw_store import RawResponse
from src.schema import Market, Selection
from src.sources import registry
from src.sources.novig import NovigAdapter, parse_novig
from src.sources.prophetx import (
    ProphetXAdapter,
    _match_tournaments,
    parse_prophetx,
)

# Two clocks, on purpose.  The parser-edge tests build both the envelopes and
# the payloads, so they pin a *fixed* moment and stay deterministic forever.
# The fetch tests go through the adapters, which stamp ``fetched_at`` with the
# real wall clock — their handlers must therefore serve a game that is in the
# future *relative to the run*, or the pregame gate correctly skips everything
# and the test rots on its own schedule.
NOW = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)
GAME_TIME = NOW + timedelta(hours=3)
GAME_ISO = GAME_TIME.strftime("%Y-%m-%dT%H:%M:%S.000Z")
LIVE_GAME_ISO = (datetime.now(UTC) + timedelta(hours=3)).strftime(
    "%Y-%m-%dT%H:%M:%S.000Z"
)


def _raw(source: str, endpoint: str, body: object, *, fetched_at: datetime = NOW) -> RawResponse:
    return RawResponse(
        source=source,
        endpoint=endpoint,
        url=f"https://example.invalid/{endpoint}",
        status_code=200,
        body=json.dumps(body),
        fetched_at=fetched_at,
        content_type="application/json",
    )


class _NetworkAttempted(BaseException):
    """Deliberately not an ``Exception``: the adapters' login paths fold any
    ``Exception`` into ``LoginRequiredError``, so an ``Exception``-derived
    sentinel would be laundered into exactly the kind the refusal tests
    assert — which is how a guard-weakening mutation (``or`` → ``and`` on the
    credential check) once survived the whole file.  A ``BaseException``
    escapes every except-clause an adapter may legitimately have."""


class _FormAwareClient(httpx.Client):
    """httpx client speaking the transport contract the adapters target.

    The real clients (``ImpersonatedSession``, the browser client) accept a
    pre-encoded string as ``data=``; httpx deprecates that spelling in favour
    of ``content=``.  Mapping one to the other here keeps the adapters written
    against the production contract while the tests stay warning-clean —
    silencing the deprecation instead would let a future httpx turn it into a
    hard error at the worst moment.
    """

    def post(self, url, *, data=None, content=None, **kwargs):  # type: ignore[override]
        if isinstance(data, (str, bytes)) and content is None:
            content, data = data, None
        return super().post(url, data=data, content=content, **kwargs)


class _RefusingClient:
    """A client whose first use fails the test: proof no socket was opened."""

    def get(self, *args, **kwargs):  # pragma: no cover - reaching here IS the failure
        raise _NetworkAttempted("network I/O was attempted without credentials")

    post = get

    def close(self) -> None:
        pass


# ── refusal before network ───────────────────────────────────────────────────


class TestMissingCredentialsRefuseBeforeAnyNetwork:
    def test_prophetx(self, monkeypatch):
        monkeypatch.setattr(settings, "PROPHETX_ACCESS_KEY", "")
        monkeypatch.setattr(settings, "PROPHETX_SECRET_KEY", "")
        adapter = ProphetXAdapter(client=_RefusingClient())
        with pytest.raises(Exception) as caught:
            adapter.fetch_raw()
        assert caught.value.kind == "login_required"
        assert "ODDS_PROPHETX_ACCESS_KEY" in str(caught.value)

    def test_novig(self, monkeypatch):
        monkeypatch.setattr(settings, "NOVIG_CLIENT_ID", "")
        monkeypatch.setattr(settings, "NOVIG_CLIENT_SECRET", "")
        adapter = NovigAdapter(client=_RefusingClient())
        with pytest.raises(Exception) as caught:
            adapter.fetch_raw()
        assert caught.value.kind == "login_required"
        assert "ODDS_NOVIG_CLIENT_ID" in str(caught.value)

    def test_half_a_key_pair_refuses_before_any_network_prophetx(self, monkeypatch):
        """Half a key pair must refuse exactly like none of it — and the
        assertion is on the env-var message, which only the pre-login guard
        produces, so weakening the guard to ``and`` cannot pass by way of a
        downstream login failure that shares the error kind."""
        monkeypatch.setattr(settings, "PROPHETX_ACCESS_KEY", "AK-ONLY")
        monkeypatch.setattr(settings, "PROPHETX_SECRET_KEY", "")
        with pytest.raises(Exception) as caught:
            ProphetXAdapter(client=_RefusingClient()).fetch_raw()
        assert caught.value.kind == "login_required"
        assert "ODDS_PROPHETX_ACCESS_KEY" in str(caught.value)

    def test_half_a_key_pair_refuses_before_any_network_novig(self, monkeypatch):
        monkeypatch.setattr(settings, "NOVIG_CLIENT_ID", "ID-ONLY")
        monkeypatch.setattr(settings, "NOVIG_CLIENT_SECRET", "")
        with pytest.raises(Exception) as caught:
            NovigAdapter(client=_RefusingClient()).fetch_raw()
        assert caught.value.kind == "login_required"
        assert "ODDS_NOVIG_CLIENT_ID" in str(caught.value)

    def test_a_failed_login_maps_to_login_required_and_captures_nothing(
        self, monkeypatch
    ):
        """The kind pins the diagnosis; ``raw is None`` pins that the auth
        exchange bypassed the capture path — rerouting the login through
        SourceClient would attach the refusal envelope (credential-fingerprint
        params included) to the error, and the collector persists those."""
        monkeypatch.setattr(settings, "PROPHETX_ACCESS_KEY", "AK")
        monkeypatch.setattr(settings, "PROPHETX_SECRET_KEY", "SK")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/auth/login")
            return httpx.Response(401, json={"message": "bad key"})

        client = _FormAwareClient(transport=httpx.MockTransport(handler))
        with pytest.raises(Exception) as caught:
            ProphetXAdapter(client=client).fetch_raw()
        assert caught.value.kind == "login_required"
        assert "401" in str(caught.value)
        assert caught.value.raw is None

    def test_a_failed_token_exchange_maps_to_login_required_and_captures_nothing(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "NOVIG_CLIENT_ID", "ID")
        monkeypatch.setattr(settings, "NOVIG_CLIENT_SECRET", "SEC")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/nbx/v1/auth/emm-token"
            return httpx.Response(403, json={"error": "invalid_client"})

        client = _FormAwareClient(transport=httpx.MockTransport(handler))
        with pytest.raises(Exception) as caught:
            NovigAdapter(client=client).fetch_raw()
        assert caught.value.kind == "login_required"
        assert "403" in str(caught.value)
        assert caught.value.raw is None


# ── the login exchange is never captured ─────────────────────────────────────

PX_ACCESS, PX_SECRET = "PX-ACCESS-LIVE-1", "PX-SECRET-LIVE-2"
PX_TOKEN = "PX-SESSION-TOKEN-LIVE-3"


def _prophetx_handler(seen: list[httpx.Request]):
    event = {
        "event_id": 7,
        "name": "Cincinnati Reds @ Philadelphia Phillies",
        "scheduled": LIVE_GAME_ISO,
        "status": "not_started",
        "tournament_id": 101,
        "competitors": [
            {"id": 1, "name": "Cincinnati Reds", "side": "away"},
            {"id": 2, "name": "Philadelphia Phillies", "side": "home"},
        ],
    }
    market = {
        "id": 55,
        "type": "moneyline",
        "name": "Moneyline",
        "status": "open",
        "selections": [
            [
                {"name": "Cincinnati Reds", "odds": 2.1, "stake": 250.0,
                 "competitor_id": 1, "outcome_id": 901},
                {"name": "Philadelphia Phillies", "odds": 1.8, "stake": 100.0,
                 "competitor_id": 2, "outcome_id": 902},
            ]
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/auth/login"):
            sent = json.loads(request.content.decode())
            assert sent == {"access_key": PX_ACCESS, "secret_key": PX_SECRET}
            return httpx.Response(
                200, json={"data": {"access_token": PX_TOKEN, "refresh_token": "R"}}
            )
        assert request.headers.get("authorization") == f"Bearer {PX_TOKEN}"
        if path.endswith("/mm/get_tournaments"):
            # Two tournaments both named exactly "MLB", both listing the same
            # event: the shape that used to fetch one event's market batch
            # twice.  The event_ids assertion below is the tripwire — a
            # duplicated id would arrive as "7,7".
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tournaments": [
                            {"id": 101, "name": "MLB"},
                            {"id": 102, "name": "MLB"},
                        ]
                    }
                },
            )
        if path.endswith("/mm/get_sport_events"):
            assert request.url.params["tournament_id"] in {"101", "102"}
            return httpx.Response(200, json={"data": {"sport_events": [event]}})
        if path.endswith("/mm/get_multiple_markets"):
            assert request.url.params["event_ids"] == "7"
            return httpx.Response(200, json={"data": {"7": [market]}})
        raise AssertionError(f"unexpected request {request.url}")

    return handler


class TestProphetXFetchKeepsTheSecretsOut:
    @pytest.fixture()
    def raws(self, monkeypatch):
        monkeypatch.setattr(settings, "PROPHETX_ACCESS_KEY", PX_ACCESS)
        monkeypatch.setattr(settings, "PROPHETX_SECRET_KEY", PX_SECRET)
        seen: list[httpx.Request] = []
        client = _FormAwareClient(transport=httpx.MockTransport(_prophetx_handler(seen)))
        adapter = ProphetXAdapter(leagues=("MLB",), client=client)
        try:
            yield adapter.fetch_raw()
        finally:
            adapter.close()

    def test_the_login_response_is_not_among_the_captures(self, raws):
        assert len(raws) == 4
        assert [raw.endpoint for raw in raws] == [
            "tournaments",
            "events:MLB:101",
            "events:MLB:102",
            "markets:MLB:00",
        ]
        assert not any("auth" in raw.endpoint or "auth" in raw.url for raw in raws)

    def test_no_secret_appears_in_any_envelope_field(self, raws):
        for raw in raws:
            # The haystack is the envelope exactly as it would be persisted —
            # every field, not a hand-picked subset that a new leak could miss.
            haystack = json.dumps(raw.to_envelope())
            for secret in (PX_ACCESS, PX_SECRET, PX_TOKEN):
                assert secret not in haystack, (raw.endpoint, secret)

    def test_the_captured_pass_parses_end_to_end(self, raws):
        outcome = parse_prophetx(raws)
        assert not outcome.rejections
        assert len(outcome.quotes) == 2
        by_selection = {quote.selection: quote for quote in outcome.quotes}
        away, home = by_selection[Selection.AWAY], by_selection[Selection.HOME]
        assert away.event_key == home.event_key
        assert away.event_key.startswith("MLB-CIN@MLB-PHI:")
        assert away.decimal_odds == pytest.approx(2.1)
        assert home.decimal_odds == pytest.approx(1.8)
        # ``stake`` has no documented semantics (available vs already-matched),
        # so no limit is published until the first credentialed session.
        assert away.limit_amount is None
        assert away.market is Market.MONEYLINE
        assert away.source_market_id == "55"
        assert away.source_selection_id == "901"


NV_ID, NV_SECRET = "NV-CLIENT-LIVE-1", "NV-CLIENT-SECRET-LIVE-2"
NV_TOKEN = "NV-BEARER-TOKEN-LIVE-3"


def _novig_handler(seen: list[httpx.Request]):
    event = {
        "id": "ev1",
        "type": "Game",
        "status": "OPEN_PREGAME",
        "description": "Cincinnati Reds @ Philadelphia Phillies",
        "league": "MLB",
        "scheduledStart": LIVE_GAME_ISO,
        "game": {
            "homeTeam": {"name": "Philadelphia Phillies"},
            "awayTeam": {"name": "Cincinnati Reds"},
        },
        "marketIds": ["m1"],
    }
    market = {
        "id": "m1",
        "description": "CIN v PHI Moneyline",
        "type": "MONEY",
        "league": "MLB",
        "eventId": "ev1",
        "strike": None,
        "volume": 1000,
        # ``last`` is a trade-history fact.  The values are deliberately set
        # so a parser that publishes them is caught: no bid below derives to
        # 2.0 from the complement rule.
        "outcomes": [
            {"id": "o1", "description": "Cincinnati Reds", "last": 0.5},
            {"id": "o2", "description": "Philadelphia Phillies", "last": 0.5},
        ],
    }
    book = {
        "marketId": "m1",
        "marketDescription": "CIN v PHI Moneyline",
        "outcomeLadders": [
            {
                "outcomeId": "o1",
                "bids": [
                    {"id": "b1", "price": 0.44, "qty": 5000, "currency": "CASH",
                     "status": "RESTING", "outcomeId": "o1", "marketId": "m1"},
                ],
            },
            {
                "outcomeId": "o2",
                "bids": [
                    {"id": "b2", "price": 0.52, "qty": 4000, "currency": "CASH",
                     "status": "RESTING", "outcomeId": "o2", "marketId": "m1"},
                ],
            },
        ],
    }

    # Page 0 is FULL (100 rows) and deliberately carries one malformed row:
    # page-fullness must be judged on the payload's own row count, because
    # judging it on the rows that survive the dict filter ends pagination a
    # page early and silently drops the slate behind it.  Page 1 is short,
    # which is the genuine stop condition.
    fillers = [
        {"id": f"filler-{index}", "type": "Game", "status": "OPEN_PREGAME",
         "league": "MLB", "scheduledStart": LIVE_GAME_ISO}
        for index in range(98)
    ]
    page_zero = [event, *fillers, "malformed-row"]
    page_one = [fillers[0] | {"id": "filler-last"}]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/nbx/v1/auth/emm-token":
            sent = dict(
                pair.split("=", 1)
                for pair in request.content.decode().split("&")
            )
            assert sent["grant_type"] == "client_credentials"
            assert sent["client_id"] == NV_ID
            assert sent["client_secret"] == NV_SECRET
            return httpx.Response(
                200,
                json={"access_token": NV_TOKEN, "token_type": "bearer", "expires_in": 1800},
            )
        assert request.headers.get("authorization") == f"Bearer {NV_TOKEN}"
        if path == "/nbx/v2/emm/events":
            assert request.url.params["league"] == "MLB"
            assert request.url.params["status"] == "OPEN_PREGAME"
            if request.url.params["offset"] == "0":
                return httpx.Response(200, json=page_zero)
            assert request.url.params["offset"] == "100"
            return httpx.Response(200, json=page_one)
        if path == "/nbx/v2/emm/markets/open":
            return httpx.Response(200, json=[market])
        if path == "/nbx/v2/emm/book/m1":
            assert request.url.params["currency"] == "CASH"
            return httpx.Response(200, json=book)
        raise AssertionError(f"unexpected request {request.url}")

    return handler


class TestNovigFetchKeepsTheSecretsOut:
    @pytest.fixture()
    def raws(self, monkeypatch):
        monkeypatch.setattr(settings, "NOVIG_CLIENT_ID", NV_ID)
        monkeypatch.setattr(settings, "NOVIG_CLIENT_SECRET", NV_SECRET)
        seen: list[httpx.Request] = []
        client = _FormAwareClient(transport=httpx.MockTransport(_novig_handler(seen)))
        adapter = NovigAdapter(leagues=("MLB",), client=client)
        try:
            yield adapter.fetch_raw()
        finally:
            adapter.close()

    def test_the_token_response_is_not_among_the_captures(self, raws):
        assert [raw.endpoint for raw in raws] == [
            "events:MLB:00",
            "events:MLB:01",
            "markets:MLB",
            "book:MLB:m1",
        ]
        assert not any("auth" in raw.endpoint or "token" in raw.url for raw in raws)

    def test_no_secret_appears_in_any_envelope_field(self, raws):
        for raw in raws:
            haystack = json.dumps(raw.to_envelope())
            for secret in (NV_ID, NV_SECRET, NV_TOKEN):
                assert secret not in haystack, (raw.endpoint, secret)

    def test_the_takeable_price_is_the_complement_of_the_other_sides_bid(self, raws):
        """Buying A matches B's resting bid, so A's price is 1 − best_bid(B) —
        and the last-trade mark, deliberately set to derive to a different
        number, must appear nowhere."""
        outcome = parse_novig(raws)
        assert not outcome.rejections
        by_selection = {quote.selection: quote for quote in outcome.quotes}
        reds = by_selection[Selection.AWAY]  # buying CIN matches PHI's bid at 0.52
        phils = by_selection[Selection.HOME]  # buying PHI matches CIN's bid at 0.44
        assert reds.decimal_odds == pytest.approx(1.0 / (1.0 - 0.52))
        assert phils.decimal_odds == pytest.approx(1.0 / (1.0 - 0.44))
        assert all(quote.decimal_odds != pytest.approx(2.0) for quote in outcome.quotes)
        # 100 qty = one $1.00-payout contract (the fees page), so PHI's resting
        # 4000 qty at 0.52 lets a CIN buyer stake 40 × 0.48 dollars, and CIN's
        # 5000 at 0.44 lets a PHI buyer stake 50 × 0.56.
        assert reds.limit_amount == pytest.approx(19.2)
        assert phils.limit_amount == pytest.approx(28.0)


# ── parser edges, on documented shapes ───────────────────────────────────────


class TestProphetXParserEdges:
    def _fixture_raws(
        self,
        *,
        market: dict,
        fetched_at: datetime = NOW,
        events_fetched_at: datetime | None = None,
    ):
        events = _raw(
            "prophetx",
            "events:MLB:101",
            {
                "data": {
                    "sport_events": [
                        {
                            "event_id": 7,
                            "scheduled": GAME_ISO,
                            "status": "not_started",
                            "competitors": [
                                {"id": 1, "name": "Cincinnati Reds", "side": "away"},
                                {"id": 2, "name": "Philadelphia Phillies", "side": "home"},
                            ],
                        }
                    ]
                }
            },
        )
        if events_fetched_at is not None:
            events = _raw(
                "prophetx",
                "events:MLB:101",
                json.loads(events.body),
                fetched_at=events_fetched_at,
            )
        markets = _raw(
            "prophetx", "markets:MLB:00", {"data": {"7": [market]}}, fetched_at=fetched_at
        )
        return [events, markets]

    def test_parse_is_deterministic(self):
        raws = self._fixture_raws(
            market={
                "id": 55,
                "type": "total",
                "name": "Total Runs",
                "line": 8.5,
                "selections": [
                    [
                        {"name": "Over 8.5", "odds": 1.95, "outcome_id": 1},
                        {"name": "Under 8.5", "odds": 1.95, "outcome_id": 2},
                    ]
                ],
            }
        )
        first, second = parse_prophetx(raws), parse_prophetx(raws)
        assert [quote.model_dump() for quote in first.quotes] == [
            quote.model_dump() for quote in second.quotes
        ]
        assert {quote.selection for quote in first.quotes} == {
            Selection.OVER,
            Selection.UNDER,
        }
        assert all(quote.line == 8.5 for quote in first.quotes)

    def test_a_market_fetched_after_first_pitch_is_skipped(self):
        # Both envelopes belong to one pass (two minutes apart) — the events
        # call ran just before first pitch, the markets call just after, which
        # is exactly the window the per-response gate exists for.
        raws = self._fixture_raws(
            market={
                "id": 55,
                "type": "moneyline",
                "name": "Moneyline",
                "selections": [[{"name": "Cincinnati Reds", "odds": 2.0,
                                 "competitor_id": 1, "outcome_id": 1}]],
            },
            fetched_at=GAME_TIME + timedelta(minutes=1),
            events_fetched_at=GAME_TIME - timedelta(minutes=1),
        )
        outcome = parse_prophetx(raws)
        assert not outcome.quotes
        assert outcome.skipped["event_already_started"] == 1

    def test_alternate_ladders_are_flagged_and_period_markets_are_skipped(self):
        raws = self._fixture_raws(
            market={
                "id": 55,
                "type": "spread",
                "name": "Run Line",
                "selections": [
                    [
                        {"name": "Cincinnati Reds", "odds": 2.4, "line": 1.5,
                         "competitor_id": 1, "outcome_id": 1},
                        {"name": "Philadelphia Phillies", "odds": 1.6, "line": -1.5,
                         "competitor_id": 2, "outcome_id": 2},
                    ]
                ],
                "market_lines": [
                    {
                        "id": 56,
                        "type": "spread",
                        "name": "Run Line",
                        "selections": [
                            [
                                {"name": "Cincinnati Reds", "odds": 3.1, "line": 2.5,
                                 "competitor_id": 1, "outcome_id": 3},
                            ]
                        ],
                    },
                    {
                        "id": 57,
                        "type": "spread",
                        "name": "1st Inning Run Line",
                        "selections": [
                            [
                                {"name": "Cincinnati Reds", "odds": 2.0, "line": 0.5,
                                 "competitor_id": 1, "outcome_id": 4},
                            ]
                        ],
                    },
                ],
            }
        )
        outcome = parse_prophetx(raws)
        mains = [quote for quote in outcome.quotes if not quote.is_alternate]
        alternates = [quote for quote in outcome.quotes if quote.is_alternate]
        assert len(mains) == 2 and len(alternates) == 1
        assert alternates[0].line == 2.5
        assert outcome.skipped["non_full_game_market"] == 1

    def test_a_spread_with_only_a_market_level_line_is_rejected_not_signed(self):
        """The market's one number is an unsigned magnitude serving two
        opposite handicaps.  Stamping it on both sides fabricates the sign of
        one of them, and a wrong-signed leg pairs with the complementary line
        at other books into a phantom guaranteed-profit signal — so a spread
        selection prices its own signed line or it is rejected, exactly as the
        Novig parser refuses the same trap."""
        raws = self._fixture_raws(
            market={
                "id": 55,
                "type": "spread",
                "name": "Run Line",
                "line": -1.5,
                "selections": [
                    [
                        {"name": "Cincinnati Reds", "odds": 2.4,
                         "competitor_id": 1, "outcome_id": 1},
                        {"name": "Philadelphia Phillies", "odds": 1.6,
                         "competitor_id": 2, "outcome_id": 2},
                    ]
                ],
            }
        )
        outcome = parse_prophetx(raws)
        assert not outcome.quotes
        assert sorted(rejection.reason for rejection in outcome.rejections) == [
            "spread_sign_unresolved",
            "spread_sign_unresolved",
        ]

    def test_the_same_event_batched_twice_dedupes_instead_of_aborting(self):
        """An event listed by two matched tournaments used to fetch its market
        batch twice, and two identical batches parse into colliding dedup keys
        — which storage answers by aborting the whole run's insert.  The fetch
        now dedupes event ids, and the parser guards the invariant the way
        every peer does: one row per key, the duplicates filed as rejections."""
        market = {
            "id": 55,
            "type": "moneyline",
            "name": "Moneyline",
            "selections": [
                [
                    {"name": "Cincinnati Reds", "odds": 2.1,
                     "competitor_id": 1, "outcome_id": 901},
                    {"name": "Philadelphia Phillies", "odds": 1.8,
                     "competitor_id": 2, "outcome_id": 902},
                ]
            ],
        }
        events, markets_00 = self._fixture_raws(market=market)
        markets_01 = _raw("prophetx", "markets:MLB:01", {"data": {"7": [market]}})
        outcome = parse_prophetx([events, markets_00, markets_01])
        assert len(outcome.quotes) == 2
        assert {quote.selection for quote in outcome.quotes} == {
            Selection.HOME,
            Selection.AWAY,
        }
        assert sorted(rejection.reason for rejection in outcome.rejections) == [
            "duplicate_dedup_key",
            "duplicate_dedup_key",
        ]

    def test_a_directory_holding_two_runs_parses_only_the_newest(self):
        """``markets:MLB:00`` is a batch counter, so yesterday's run shares
        labels with today's — and an older run's trailing batch (``:01`` here)
        has no newer counterpart at all, which is exactly the case only
        ``latest_capture`` can drop.  Parsing everything used to emit both
        runs' prices side by side."""
        stale_time = NOW - timedelta(hours=2)
        market_now = {
            "id": 55, "type": "moneyline", "name": "Moneyline",
            "selections": [[
                {"name": "Cincinnati Reds", "odds": 2.3,
                 "competitor_id": 1, "outcome_id": 901},
                {"name": "Philadelphia Phillies", "odds": 1.7,
                 "competitor_id": 2, "outcome_id": 902},
            ]],
        }
        market_stale = {
            "id": 55, "type": "moneyline", "name": "Moneyline",
            "selections": [[
                {"name": "Cincinnati Reds", "odds": 3.5,
                 "competitor_id": 1, "outcome_id": 901},
            ]],
        }
        events, markets_new = self._fixture_raws(market=market_now)
        stale_events = _raw(
            "prophetx", "events:MLB:101",
            json.loads(events.body), fetched_at=stale_time,
        )
        stale_00 = _raw(
            "prophetx", "markets:MLB:00", {"data": {"7": [market_stale]}},
            fetched_at=stale_time,
        )
        stale_01 = _raw(
            "prophetx", "markets:MLB:01", {"data": {"7": [market_stale]}},
            fetched_at=stale_time,
        )
        outcome = parse_prophetx([stale_events, stale_00, stale_01, events, markets_new])
        assert not outcome.rejections
        assert sorted(quote.decimal_odds for quote in outcome.quotes) == [1.7, 2.3]

    def test_tournament_matching_is_exact_name_equality(self):
        """"NBA Summer League" and "NBA G League" contain the token NBA and
        are not the NBA — Summer League fields the real franchises and G
        League affiliates carry parent nicknames, so admitting either mints
        event keys that join real same-night fixtures and feeds development
        prices into the comparison as NBA legs.  Exact equality with the key
        or the league's display name, nothing else."""
        raw = _raw(
            "prophetx",
            "tournaments",
            {
                "data": {
                    "tournaments": [
                        {"id": 1, "name": "WNBA"},
                        {"id": 2, "name": "NBA"},
                        {"id": 3, "name": "NBA Summer League"},
                        {"id": 4, "name": "NBA G League"},
                        {"id": 5, "name": "National Basketball Association"},
                    ]
                }
            },
        )
        matched = _match_tournaments(raw, ("NBA", "WNBA"), source="prophetx")
        assert matched["WNBA"] == [1]
        assert matched["NBA"] == [2, 5]


class TestNovigParserEdges:
    def _raws(self, market: dict, book: dict):
        events = _raw(
            "novig",
            "events:MLB:00",
            [
                {
                    "id": "ev1",
                    "status": "OPEN_PREGAME",
                    "league": "MLB",
                    "scheduledStart": GAME_ISO,
                    "game": {
                        "homeTeam": {"name": "Philadelphia Phillies"},
                        "awayTeam": {"name": "Cincinnati Reds"},
                    },
                }
            ],
        )
        return [
            events,
            _raw("novig", "markets:MLB", [market]),
            _raw("novig", f"book:MLB:{market['id']}", book),
        ]

    def test_a_spread_outcome_without_a_signed_line_is_rejected_not_guessed(self):
        """The strike is one unsigned magnitude for two opposite handicaps;
        guessing the favourite flips the sign of every wrong guess."""
        market = {
            "id": "m2",
            "type": "SPREAD",
            "eventId": "ev1",
            "strike": 1.5,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies -1.5"},
            ],
        }
        book = {
            "marketId": "m2",
            "outcomeLadders": [
                {"outcomeId": "o1",
                 "bids": [{"price": 0.4, "currency": "CASH", "status": "RESTING"}]},
                {"outcomeId": "o2",
                 "bids": [{"price": 0.55, "currency": "CASH", "status": "RESTING"}]},
            ],
        }
        outcome = parse_novig(self._raws(market, book))
        assert len(outcome.quotes) == 1
        assert outcome.quotes[0].selection is Selection.HOME
        assert outcome.quotes[0].line == -1.5
        assert [rejection.reason for rejection in outcome.rejections] == [
            "spread_sign_unresolved"
        ]

    def test_an_empty_opposite_ladder_yields_no_quote(self):
        market = {
            "id": "m3",
            "type": "MONEY",
            "eventId": "ev1",
            "strike": None,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies"},
            ],
        }
        book = {
            "marketId": "m3",
            "outcomeLadders": [
                {"outcomeId": "o1",
                 "bids": [{"price": 0.5, "currency": "CASH", "status": "RESTING"}]},
                {"outcomeId": "o2", "bids": []},
            ],
        }
        outcome = parse_novig(self._raws(market, book))
        # Only PHI is quotable: buying PHI matches CIN's resting bid.  Buying
        # CIN would need a PHI bid and there is none.
        assert [quote.selection for quote in outcome.quotes] == [Selection.HOME]
        assert outcome.skipped["no_resting_order_for_outcome"] == 1

    def test_a_near_certain_bid_publishes_its_price_with_no_limit(self):
        """A resting bid at 0.996 derives to a valid 250.0-decimal price whose
        stake ceiling rounds below one cent.  That row must publish with
        ``limit_amount=None`` — the Quote validator refuses 0.0, and filing a
        working extreme price as invalid_quote spends a parser-failure signal
        on a book behaving normally."""
        market = {
            "id": "m5",
            "type": "MONEY",
            "eventId": "ev1",
            "strike": None,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies"},
            ],
        }
        book = {
            "marketId": "m5",
            "outcomeLadders": [
                {"outcomeId": "o1",
                 "bids": [{"price": 0.5, "qty": 1000, "currency": "CASH",
                           "status": "RESTING"}]},
                {"outcomeId": "o2",
                 "bids": [{"price": 0.996, "qty": 100, "currency": "CASH",
                           "status": "RESTING"}]},
            ],
        }
        outcome = parse_novig(self._raws(market, book))
        assert not outcome.rejections
        by_selection = {quote.selection: quote for quote in outcome.quotes}
        reds = by_selection[Selection.AWAY]  # matches PHI's bid at 0.996
        phils = by_selection[Selection.HOME]  # matches CIN's bid at 0.5
        assert reds.decimal_odds == pytest.approx(250.0)
        assert reds.limit_amount is None
        assert phils.limit_amount == pytest.approx(5.0)

    def test_two_money_markets_on_one_event_dedupe_instead_of_aborting(self):
        """``dedup_key`` carries no market id, so a venue listing two MONEY
        markets for one event collides on it — and storage answers a collision
        by aborting the whole run's insert.  One row per key survives; the
        duplicates are filed as rejections, the way every peer parser does."""
        base = {
            "type": "MONEY",
            "eventId": "ev1",
            "strike": None,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies"},
            ],
        }
        ladders = {
            "outcomeLadders": [
                {"outcomeId": "o1",
                 "bids": [{"price": 0.44, "qty": 1000, "currency": "CASH",
                           "status": "RESTING"}]},
                {"outcomeId": "o2",
                 "bids": [{"price": 0.52, "qty": 1000, "currency": "CASH",
                           "status": "RESTING"}]},
            ]
        }
        events = _raw(
            "novig",
            "events:MLB:00",
            [
                {
                    "id": "ev1",
                    "status": "OPEN_PREGAME",
                    "league": "MLB",
                    "scheduledStart": GAME_ISO,
                    "game": {
                        "homeTeam": {"name": "Philadelphia Phillies"},
                        "awayTeam": {"name": "Cincinnati Reds"},
                    },
                }
            ],
        )
        raws = [
            events,
            _raw("novig", "markets:MLB", [base | {"id": "m1"}, base | {"id": "m1b"}]),
            _raw("novig", "book:MLB:m1", ladders | {"marketId": "m1"}),
            _raw("novig", "book:MLB:m1b", ladders | {"marketId": "m1b"}),
        ]
        outcome = parse_novig(raws)
        assert len(outcome.quotes) == 2
        assert sorted(rejection.reason for rejection in outcome.rejections) == [
            "duplicate_dedup_key",
            "duplicate_dedup_key",
        ]

    def test_two_runs_of_one_book_parse_only_the_newest(self):
        """``book:MLB:m1`` keeps its label across runs, so a directory holding
        yesterday's book beside today's used to emit both — and on an order
        book a stale row is a phantom price: a resting order that has since
        been filled outbids the live book."""
        market = {
            "id": "m1",
            "type": "MONEY",
            "eventId": "ev1",
            "strike": None,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies"},
            ],
        }
        book_stale = {
            "marketId": "m1",
            "outcomeLadders": [
                {"outcomeId": "o2",
                 "bids": [{"price": 0.30, "qty": 1000, "currency": "CASH",
                           "status": "RESTING"}]},
            ],
        }
        book_now = {
            "marketId": "m1",
            "outcomeLadders": [
                {"outcomeId": "o2",
                 "bids": [{"price": 0.52, "qty": 4000, "currency": "CASH",
                           "status": "RESTING"}]},
            ],
        }
        raws = self._raws(market, book_now)
        stale = _raw(
            "novig", "book:MLB:m1", book_stale, fetched_at=NOW - timedelta(hours=2)
        )
        outcome = parse_novig([stale, *raws])
        prices = [quote.decimal_odds for quote in outcome.quotes]
        assert prices == [pytest.approx(1.0 / (1.0 - 0.52))]

    def test_non_resting_and_coin_bids_are_not_prices(self):
        market = {
            "id": "m4",
            "type": "MONEY",
            "eventId": "ev1",
            "strike": None,
            "outcomes": [
                {"id": "o1", "description": "Cincinnati Reds"},
                {"id": "o2", "description": "Philadelphia Phillies"},
            ],
        }
        book = {
            "marketId": "m4",
            "outcomeLadders": [
                {"outcomeId": "o1", "bids": [
                    {"price": 0.9, "currency": "COIN", "status": "RESTING"},
                    {"price": 0.8, "currency": "CASH", "status": "CANCELLED"},
                    {"price": 0.5, "currency": "CASH", "status": "RESTING"},
                ]},
                {"outcomeId": "o2", "bids": []},
            ],
        }
        outcome = parse_novig(self._raws(market, book))
        assert len(outcome.quotes) == 1
        assert outcome.quotes[0].decimal_odds == pytest.approx(1.0 / (1.0 - 0.5))


# ── the registry stance ──────────────────────────────────────────────────────


class TestTheCredentialedAxis:
    def test_both_keys_are_reserved_and_neither_is_registered(self):
        """Registration is a decision the operator makes with keys in hand —
        conftest demands a committed fixture per registered key, and no
        sanctioned path to a genuine capture exists before credentials do."""
        assert registry.CREDENTIALED_SOURCE_KEYS == frozenset({"prophetx", "novig"})
        assert "prophetx" not in registry.keys()
        assert "novig" not in registry.keys()

    def test_the_credential_env_names_go_through_settings(self):
        """Env-only credentials: the four names resolve through
        ``settings._lookup``, so they honour the prefix scheme and the
        deprecated-alias reporting like every other setting."""
        for suffix in (
            "PROPHETX_ACCESS_KEY",
            "PROPHETX_SECRET_KEY",
            "NOVIG_CLIENT_ID",
            "NOVIG_CLIENT_SECRET",
        ):
            assert suffix in settings.ENV_NAMES
            assert settings.ENV_NAMES[suffix][0] == f"ODDS_{suffix}"

    def test_token_shaped_response_headers_never_reach_an_envelope(self):
        """Request headers are never stored, which keeps the outbound bearer
        token out of envelopes — but a venue that echoes a refreshed token in
        a *response* header would be persisted into every envelope of the
        pass.  The denylist grew the token-shaped names when the first
        credentialed venues made them load-bearing."""
        cleaned = RawResponse.clean_headers(
            {
                "Content-Type": "application/json",
                "X-Auth-Token": "LIVE-A",
                "X-Access-Token": "LIVE-B",
                "X-Refresh-Token": "LIVE-C",
                "X-Api-Key": "LIVE-D",
                "Authorization": "Bearer LIVE-E",
                "Set-Cookie": "sid=LIVE-F",
            }
        )
        assert cleaned == {"content-type": "application/json"}

    def test_the_recon_sanitizer_knows_these_venues_credential_field_names(self):
        """The Phase-1e leak closure and this feature meet here: ProphetX's own
        field names are ``access_key``/``secret_key`` and Novig's are
        ``client_id``/``client_secret``, and all four must stay in the
        sanitizer's vocabulary."""
        from src.sources.research import sanitize_body

        cleaned = sanitize_body(
            '{"access_key":"LIVE1","secret_key":"LIVE2",'
            '"client_id":"LIVE3","client_secret":"LIVE4"}'
        )
        for value in ("LIVE1", "LIVE2", "LIVE3", "LIVE4"):
            assert value not in cleaned
