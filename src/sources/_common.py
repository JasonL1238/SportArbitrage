"""What every adapter does identically, in one place.

Three books grew three copies of the same fetch scaffolding, and the copies had
already drifted: only one of them paced its requests, none of them retried, none
sent a User-Agent, and each had its own spelling of "an empty league is an off
day but an empty book is a failure".  Ten sources would have been ten copies.

What is shared here is the *mechanics* — a session, pacing, retry, capture, and
the empty-scope policy.  What is deliberately **not** shared is the vocabulary:
the declarative market tables and ``_build_quote`` differ in kind between books
(FanDuel states a line two incompatible ways, Pinnacle's periods are numbers
whose meaning depends on the sport, Kambi scales odds *and* lines by 1000), and
forcing those into one abstraction is how a per-book trap gets lost.

Reachability first.  The default session impersonates Chrome's TLS stack
(``curl_cffi``) and will use ``ODDS_HTTP_PROXY`` when set.  Requests are still
paced and retried so a run does not hammer a host into a ban, but identity and
geo walls are something to route around, not accept.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from src.raw_store import RawResponse
from src.schema import Market, Selection
from src.sources.guards import (
    EmptyResponseError,
    RateLimitedError,
    SourceError,
    TransportError,
    check_http_response,
)

log = logging.getLogger(__name__)

#: Fallback User-Agent when the transport is not impersonating a browser.
#: The default :class:`~src.sources.transport.ImpersonatedSession` sends the
#: Chrome UA that matches its TLS fingerprint; that pair is what opens edges
#: that reject a research-string UA on a Chrome ClientHello.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: Mapping[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

#: Minimum gap between two requests to the same host, seconds.  Pinnacle already
#: used 0.1; the others used nothing at all, which is what produced Kalshi's 429
#: during the source probe.
DEFAULT_MIN_REQUEST_INTERVAL = 0.25


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try again, and how long to wait before doing so.

    Two attempts after the first is the whole budget.  More is not resilience:
    a source that has failed three times in ten seconds is not going to succeed
    on the fourth, and continuing to ask is a small denial of service aimed at
    somebody's public endpoint.
    """

    attempts: int = 3
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 20.0

    def delay_for(self, attempt: int, stated: float | None) -> float:
        """Seconds to wait before *attempt* (1-based), honouring the server.

        A ``Retry-After`` the source stated always wins over our own guess, and
        is capped only by :attr:`max_backoff_seconds` so a hostile or mistaken
        header cannot park a run for an hour.
        """
        if stated is not None:
            return min(stated, self.max_backoff_seconds)
        return min(self.backoff_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds)


class HostPacer:
    """A minimum interval between requests to one host.

    Keyed on host rather than on source because the constraint belongs to the
    server: two Kambi instances pointed at the same CDN are one host's worth of
    load, however many adapters are involved.
    """

    def __init__(
        self, min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.min_interval = max(min_interval, 0.0)
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def wait(
        self,
        url: str,
        *,
        minimum: float = 0.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold off until this host may be asked again.

        *minimum* raises the gap for one caller without lowering it for anyone
        else, which is what lets a source that has actually been rate-limited ask
        for more room while still sharing the host's budget.  Kalshi is the live
        case: it answered ``429 too_many_requests`` to a probe running at the
        default pace.
        """
        interval = max(self.min_interval, minimum)
        if interval <= 0:
            return
        host = urlsplit(url).netloc or url
        previous = self._last.get(host)
        current = now()
        if previous is not None:
            gap = interval - (current - previous)
            if gap > 0:
                self._sleep(gap)
                current = now()
        self._last[host] = current


#: One pacer for the whole process.  Instances of the same adapter pointed at one
#: CDN — the Kambi tenants are the live case — otherwise pace independently and
#: hit the host at N times the intended rate.
_SHARED_PACER = HostPacer()


class SourceClient:
    """One book's HTTP session: paced, retried, identified, and captured.

    Every response becomes a :class:`~src.raw_store.RawResponse` *before* it is
    interpreted, including the ones that turn out to be refusals: the failure
    carries its own capture on :attr:`SourceError.raw`, so the collector can
    store the bytes that *explain* the failure rather than only the sentence
    describing it.  A block page whose markers changed is unreadable from a log
    line and obvious from the payload.
    """

    def __init__(
        self,
        source_key: str,
        *,
        timeout: float = 20.0,
        client: Any | None = None,
        headers: Mapping[str, str] | None = None,
        retry: RetryPolicy | None = None,
        pacer: HostPacer | None = None,
        min_request_interval: float | None = None,
        host_interval: float = 0.0,
        proxy_state: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from src.sources.transport import build_default_client

        self.source_key = source_key
        self.retry = retry or RetryPolicy()
        self.host_interval = host_interval
        self._sleep = sleep
        if pacer is not None:
            self._pacer = pacer
        elif min_request_interval is not None:
            self._pacer = HostPacer(min_request_interval, sleep=sleep)
        else:
            self._pacer = _SHARED_PACER
        self._headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._owns_client = client is None
        # Default: Chrome TLS impersonation (+ optional proxy).  Injected
        # clients (httpx.MockTransport in tests) are left alone.
        self._client = (
            client
            if client is not None
            else build_default_client(timeout=timeout, state=proxy_state)
        )

    def get(
        self,
        url: str,
        *,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        record_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
    ) -> RawResponse:
        """Fetch one URL, capture it, and validate it.

        Returns the captured response.  Raises the most specific
        :class:`~src.sources.guards.SourceError` that applies, after exhausting
        the retry budget for the kinds of failure that are worth retrying.

        *record_params* overrides what is stored in the envelope's
        ``request_params``, which is how a static application key stays out of
        the capture while still being sent.

        *expect_json* is True for odds payloads.  Promo pages are often HTML;
        pass False there so a 200 HTML body is captured rather than refused as
        :class:`~src.sources.guards.NotJsonError`.
        """
        return self._request(
            "GET",
            url,
            endpoint=endpoint,
            params=params,
            record_params=record_params,
            headers=headers,
            expect_json=expect_json,
        )

    def post(
        self,
        url: str,
        *,
        endpoint: str,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
        params: Mapping[str, Any] | None = None,
        record_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
    ) -> RawResponse:
        """POST one URL, capture it, and validate it.

        Used by promo adapters whose public catalog is a JSON query (DraftKings).
        Odds adapters stay on :meth:`get`.
        """
        return self._request(
            "POST",
            url,
            endpoint=endpoint,
            params=params,
            record_params=record_params,
            headers=headers,
            expect_json=expect_json,
            json_body=json_body,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        record_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> RawResponse:
        last: SourceError | None = None
        for attempt in range(1, self.retry.attempts + 1):
            self._pacer.wait(url, minimum=self.host_interval)
            try:
                request_headers = {**self._headers, **(headers or {})}
                # ``None`` and ``{}`` are different requests.  httpx treats any
                # non-None ``params`` as a *replacement* of the URL's own query,
                # so collapsing None to {} strips the query off a URL that
                # already carries one — which is exactly the shape of a cursor
                # a venue hands back.  Smarkets' ``pagination.next_page`` arrived
                # complete with ``type``, ``state``, ``limit`` and ``offset``,
                # and every one of them was deleted before the request went out:
                # the cursor never advanced, and the unfiltered reply pulled in
                # other sports' events, which were then paid for in contracts
                # and quotes batches.
                request_params = None if params is None else dict(params)
                if method.upper() == "POST":
                    if json_body is None:
                        payload: Any = None
                    elif isinstance(json_body, Mapping):
                        payload = dict(json_body)
                    elif isinstance(json_body, (str, bytes)):
                        raise TypeError(
                            "json_body must be a mapping or list, not str/bytes"
                        )
                    else:
                        payload = list(json_body)
                    response = self._client.post(
                        url,
                        params=request_params,
                        headers=request_headers,
                        json=payload,
                    )
                else:
                    response = self._client.get(
                        url,
                        params=request_params,
                        headers=request_headers,
                    )
            except Exception as exc:
                # httpx, curl_cffi, and proxy stacks each raise their own
                # hierarchy; anything that prevented a response is transport.
                if isinstance(exc, (SourceError, KeyboardInterrupt, SystemExit)):
                    raise
                last = TransportError(f"{self.source_key}:{endpoint}: {type(exc).__name__}: {exc}")
                if attempt >= self.retry.attempts:
                    raise last from exc
                self._sleep(self.retry.delay_for(attempt, None))
                continue

            stored_params = dict(
                record_params if record_params is not None else (params or {})
            )
            if json_body is not None:
                import hashlib
                import json as _json

                # Fingerprint only — never store the full POST body in the
                # envelope params (DraftKings catalogs are large).
                blob = _json.dumps(json_body, sort_keys=True, default=str)
                stored_params = {
                    **stored_params,
                    "method": method.upper(),
                    "body_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16],
                }

            raw = RawResponse(
                source=self.source_key,
                endpoint=endpoint,
                url=str(response.request.url),
                status_code=response.status_code,
                body=response.text,
                fetched_at=datetime.now(UTC),
                content_type=response.headers.get("content-type"),
                headers=RawResponse.clean_headers(response.headers),
                request_params=stored_params,
            )
            try:
                check_http_response(
                    source=self.source_key,
                    endpoint=endpoint,
                    status_code=raw.status_code,
                    body=raw.body,
                    content_type=raw.content_type,
                    url=raw.url,
                    retry_after=response.headers.get("retry-after"),
                    expect_json=expect_json,
                )
            except SourceError as exc:
                exc.raw = raw
                last = exc
                if not getattr(exc, "retryable", False) or attempt >= self.retry.attempts:
                    raise
                stated = getattr(exc, "retry_after", None) if isinstance(exc, RateLimitedError) else None
                delay = self.retry.delay_for(attempt, stated)
                log.info(
                    "%s: %s (%s); retrying in %.1fs (attempt %d/%d)",
                    self.source_key,
                    endpoint,
                    exc.kind,
                    delay,
                    attempt,
                    self.retry.attempts,
                )
                self._sleep(delay)
                continue
            return raw

        # Unreachable: every path above either returns or raises on the last
        # attempt.  Kept so a future edit to the loop cannot silently return None.
        raise last or SourceError(f"{self.source_key}:{endpoint}: no attempt was made")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ── response selection ───────────────────────────────────────────────────────


def latest_per_endpoint(
    raws: Iterable[RawResponse], *, key: Callable[[RawResponse], str] | None = None
) -> list[RawResponse]:
    """One response per endpoint label: the most recently fetched.

    Two runs of the same endpoint describe the *same* markets at two instants.
    Parsing both emits every row twice, and since ``dedup_key`` is enforced by a
    UNIQUE constraint that aborts the entire run's insert — so replaying a
    directory holding yesterday and today used to produce nothing at all.

    Ties on ``fetched_at`` (two captures inside the same clock reading) are broken
    by body hash rather than by iteration order, so the choice is stable no matter
    how the caller happened to list the files.  Ordering of the result is by the
    grouping key, which is likewise independent of input order.

    *key* exists because "one response per endpoint" is not always the right
    grain: Kambi's batch labels are position counters that cover different events
    from run to run, so it groups on something else and then resolves ownership
    per event id.
    """
    grouping = key or (lambda raw: raw.endpoint)
    best: dict[str, RawResponse] = {}
    for raw in raws:
        label = grouping(raw)
        current = best.get(label)
        if current is None or (raw.fetched_at, raw.sha256) > (current.fetched_at, current.sha256):
            best[label] = raw
    return [best[label] for label in sorted(best)]


def response_order(raw: RawResponse) -> tuple[datetime, str, str]:
    """Total order over responses: newest last, then stable on content."""
    return (raw.fetched_at, raw.endpoint, raw.sha256)


# ── time ─────────────────────────────────────────────────────────────────────


def parse_iso_time(value: Any) -> datetime | None:
    """An ISO-8601 instant, or ``None`` when the field cannot be one.

    Returning ``None`` rather than raising is deliberate: a missing or malformed
    time is a fact about one record, and the caller decides whether that record
    is a rejection or a counted skip.  A naive timestamp is read as UTC, which is
    what every source that omits the zone means.
    """
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    # Some feeds spell the zone "Z", some " +00", some omit it entirely.
    text = text.replace("Z", "+00:00")
    if text.endswith("+00"):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_epoch_time(value: Any, *, unit: str = "s") -> datetime | None:
    """An epoch timestamp in seconds or milliseconds, or ``None``.

    The unit is stated by the caller rather than guessed from magnitude: a
    heuristic that reads 1785260400 as seconds and 1785260400000 as milliseconds
    works until a feed sends microseconds, and then it silently files a fixture
    in the year 58000.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    divisor = {"s": 1.0, "ms": 1000.0}.get(unit)
    if divisor is None:
        raise ValueError(f"unknown epoch unit {unit!r}")
    try:
        return datetime.fromtimestamp(number / divisor, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# ── the empty-scope policy ───────────────────────────────────────────────────


@dataclass
class ScopeTally:
    """How many of a book's configured scopes actually had a slate.

    "This league has no games today" and "this book is broken" produce the same
    zero, and the only thing that separates them is whether *every* scope came
    back empty.  Three adapters had three spellings of that rule; this is one.
    """

    source_key: str
    scopes_requested: int = 0
    scopes_with_data: int = 0
    empty_scopes: list[str] = field(default_factory=list)
    failures: list[SourceError] = field(default_factory=list)
    failed_scopes: list[str] = field(default_factory=list)
    """``"scope: reason"`` per refusal, for the health row.

    ``failures`` alone was write-only: its single reader is
    :meth:`require_something`, which consults it **only when every scope came
    back empty**.  One surviving scope therefore discarded every refusal — a
    league answering 429 cost 14% of a book's rows and the run reported
    ``OK (701 quotes, 12 requests)`` with no finding, no error and exit 0.  The
    tally knew; nothing could ask it.
    """

    truncated_scopes: list[str] = field(default_factory=list)
    """``"scope: reason"`` per scope that produced a slate and then stopped early.

    A third state, and it needs to be one.  "Refused" and "produced" cannot
    express a scope that answered, handed over most of a league, and hit a
    self-imposed page cap — and filing it as a refusal says something false in
    the direction that matters: Polymarket returning 41 of its MLB events was
    reported as ``was refused 1 of the 1 scope(s) it asked for and returned none
    of the rest``, graded ERROR, and set a non-zero exit code on a run that had
    collected 2,393 rows from ten venues.

    So a truncation is reported on its own line and never enters the
    share-of-scopes-lost arithmetic.  How much a cap left behind is not knowable
    from the count of scopes — it is a fraction of *one* scope, of unknown size —
    and the honest reading of "one scope truncated of one scope asked for" is not
    100% lost.
    """

    _requested: set[str] = field(default_factory=set, repr=False)
    """Which scope names have been counted, so the count matches the names.

    ``scopes_requested`` is the denominator the collector grades against and
    ``failed_scopes`` is the numerator, and nothing tied them to the same unit:
    Pinnacle counted requests in **sports** and refusals in **leagues**, so five
    blocked soccer leagues out of six configured sports gave ``share_lost =
    6/2 = 3.0`` and a message reading "refused 6 of the scopes it asked for" —
    it asked for two.  It graded ERROR there by arithmetic accident, and graded
    the reverse case (losing a whole sport) a WARNING for the same reason.

    Registering on first mention makes the two agree whatever an adapter does:
    a scope that produces or fails without having been requested is requested by
    definition — it was asked for, or there would be nothing to report about it.
    """

    def requested(self, scope: str) -> None:
        if scope in self._requested:
            return
        self._requested.add(scope)
        self.scopes_requested += 1

    def produced(self, scope: str, count: int) -> None:
        self.requested(scope)
        if count:
            self.scopes_with_data += 1
        else:
            self.empty_scopes.append(scope)

    _failed: set[str] = field(default_factory=set, repr=False)
    """Distinct scope names refused, which is the numerator.

    ``failed_scopes`` is a list of *messages* and one scope can produce two of
    them — SX Bet's metadata half and its order-book half are one scope and two
    requests — while ``requested`` de-duplicates.  Counting the messages put the
    numerator above the denominator and printed "was refused 2 of the 1 scope(s)
    it asked for", which is the failure this class's own docstring says the
    design eliminates, reached by a different route.
    """

    def failed(self, scope: str, error: SourceError) -> None:
        self.requested(scope)
        self._failed.add(scope)
        self.failures.append(error)
        self.failed_scopes.append(f"{scope}: {error}")

    def truncated(self, scope: str, error: SourceError) -> None:
        """Record that *scope* answered but stopped short of its whole slate.

        Deliberately does **not** call :meth:`requested`, and does not touch
        ``failures``: a cap is a note about a scope, not a scope of its own, and
        the scope it is about has either already been registered by the caller
        or — in Pinnacle's league-index fallback — names leagues that were never
        asked for at all.  Registering either kind moves the denominator, which
        is how the invented scope names of round 28 diluted every share.
        """
        self.truncated_scopes.append(f"{scope}: {error}")

    @property
    def scopes_failed(self) -> int:
        """How many distinct scopes were refused outright."""
        return len(self._failed)

    def require_something(self, *, what: str = "pregame event") -> None:
        """Raise unless at least one scope produced data.

        Re-raises the first real failure when there was one, because "every
        league was empty" and "every league was refused" are different diagnoses
        and only the second one names a cause.
        """
        if self.scopes_with_data:
            return
        if self.failures:
            raise self.failures[0]
        raise EmptyResponseError(
            f"{self.source_key}: none of {self.scopes_requested} configured scope(s) "
            f"returned a {what} ({', '.join(self.empty_scopes) or 'no scopes requested'})"
        )


# ── capability declaration ───────────────────────────────────────────────────


def capabilities_from(
    league_markets: Mapping[str, Iterable[Market]], leagues: Sequence[str]
) -> dict[str, frozenset[Market]]:
    """Narrow a declared capability table to the leagues one instance collects."""
    return {
        key: frozenset(league_markets[key]) for key in leagues if key in league_markets
    }


#: Tokens in a market or selection label that mark a **sub-period** — a window
#: narrower than the whole game.
#:
#: One list, shared, because two adapters keeping their own drift apart: the
#: credentialed exchanges arrived with ProphetX guarding this and Novig not
#: guarding it at all, so a "1st Half Over 4.5" would have published as a
#: full-game total on one venue and been skipped on the other.  A sub-period
#: market published as full-game corrupts the comparison silently — it joins
#: real full-game rows at the same line and prices a different bet.
#:
#: Includes the compact spellings (``1H``, ``H1``, ``Q1``, ``F5``) because a
#: word-only list reads "1H Moneyline" as a full game.  Deliberately **not**
#: here: ``OT`` and ``overtime``, which mark whether extra time *counts toward*
#: a whole-game price rather than naming a narrower window — treating them as
#: sub-period markers would skip ordinary full-game markets that merely say so.
#:
#: Provisional for any venue whose vocabulary has not been seen in a genuine
#: capture; erring toward skipping is the recoverable direction (a visible
#: coverage gap rather than a corrupted comparison).
PERIOD_MARKERS: frozenset[str] = frozenset(
    {
        "half", "halves", "halftime", "ht", "quarter", "quarters",
        "inning", "innings", "period", "periods", "frame", "set", "sets",
        "map", "maps",
        "1st", "2nd", "3rd", "4th", "first", "second", "third", "fourth",
        # Both orders of every compact spelling.  Carrying ``p1`` without
        # ``1p`` is not a smaller version of this list, it is a hole in the
        # shape of one league: ``1P`` is the standard spelling of a hockey
        # first period, and NHL is a default league for both exchanges.
        "1h", "2h", "h1", "h2",
        "1q", "2q", "3q", "4q", "q1", "q2", "q3", "q4",
        "1p", "2p", "3p", "p1", "p2", "p3",
        "f5", "1i", "i1",
        # Regulation is *not* the whole game wherever overtime exists, and the
        # schema has a separate ``Period.REGULATION`` saying so.  A hockey
        # "Moneyline (Regulation Time)" or "60 Minute Line" filed as full-game
        # is a systematically longer price — three-way legs always are —
        # so it pairs with a genuine full-game price at another book into an
        # apparent arbitrage that a single overtime winner loses on both legs.
        "regulation", "regulationtime", "minute", "minutes", "reg",
    }
)

#: Punctuation that separates words in a market label.  Underscore earns its
#: place from the venues themselves — Novig's own enums are SCREAMING_SNAKE
#: (``OPEN_PREGAME``), so ``FIRST_HALF_TOTAL`` is the spelling to expect from
#: one — and the dash family because a label is as likely to read
#: ``"Total—1H"`` as ``"Total - 1H"``.
_LABEL_SEPARATORS = ("-", "–", "—", "/", "|", "(", ")", ",", ":", "_")


#: Phrases that name the **whole** game while containing a period word.  A
#: venue spelling full-game as ``ALL_PERIODS`` is saying the opposite of what
#: the marker ``periods`` alone would imply, and dropping that market loses the
#: class silently.
_WHOLE_GAME_PHRASES = (
    "all periods", "all quarters", "all halves", "all innings",
    # Extra innings are to baseball what overtime is to hockey: *included in*
    # a whole-game price, so saying so does not name a narrower window.  But
    # only when the label says they are included — the inclusion word carries
    # the whole meaning.  Stripping a bare "extra innings" read "Extra Innings
    # Only", a market settled on the extra frames alone, as a full game.
    "incl extra innings", "including extra innings", "inc extra innings",
    "with extra innings", "includes extra innings",
    "incl extra inning", "including extra inning",
    "incl extra time", "including extra time", "with extra time",
    "includes extra time",
    "incl overtime", "including overtime", "inc overtime",
    "with overtime", "includes overtime", "incl ot", "including ot",
    # The inclusion word trails as often as it leads — "Total Runs (Extra
    # Innings Included)" says exactly what "Incl. Extra Innings" says, and
    # reading only the prefix form made the postfix one a sub-period market.
    "extra innings included", "extra inning included", "extra time included",
    "overtime included", "ot included", "et included",
    "overtime shootout included", "overtime and shootout included",
)

#: Windows that only a *phrase* names: no single token in "extra time" or
#: "extra innings" is a period marker, and adding "time" or "extra" to the
#: token list would flag half the full-game markets in the world.  Checked
#: after the whole-game phrases are stripped, so "incl. extra innings" is
#: already gone by the time "extra innings" is looked for — the inclusion
#: word is what separates "counted toward the whole game" from "settled on
#: these frames alone".
_SUB_PERIOD_PHRASES = ("extra innings", "extra inning", "extra time", "overtime")


def _screening_text(labels: Sequence[Any]) -> str:
    text = " ".join(str(label or "") for label in labels).lower()
    for separator in _LABEL_SEPARATORS:
        text = text.replace(separator, " ")
    return " ".join(text.split())


def mentions_a_sub_period(*labels: Any) -> bool:
    """Does any of *labels* name a window narrower than the whole game?

    Tokenised rather than substring-matched: ``"Setanta"`` contains ``"set"``
    and names no period, and a substring rule would skip it.
    """
    # Tokens lose their trailing punctuation *before* the phrase pass, so
    # "Incl. Extra Innings" and "Incl Extra Innings" are one phrase to match
    # rather than two spellings to enumerate.
    text = " ".join(
        token.strip(".") for token in _screening_text(labels).split()
    )
    for phrase in _WHOLE_GAME_PHRASES:
        text = text.replace(phrase, " ")
    # Padded, so a phrase matches on word boundaries: an unpadded ``in`` test
    # reads "extra time" inside "extra timeout", which is the substring
    # mistake this module's own tokenising exists to avoid.
    padded = f" {text} "
    if any(f" {phrase} " in padded for phrase in _SUB_PERIOD_PHRASES):
        return True
    return bool(set(text.split()) & PERIOD_MARKERS)


def resolve_over_under(description: str) -> Selection | None:
    """``OVER``/``UNDER`` from a total's label, or ``None`` if it says neither.

    Whole tokens, and both checked before either is believed.  A substring
    test in the obvious order — ``"over" in text`` first — reads the ordinary
    total label ``"Under 220.5 (Incl. Overtime)"`` as an **Over**, because
    "overtime" contains "over": the Under's price is then published under the
    Over's identity, and the genuine Over row collides with it.  That is a
    wrong number published from a payload that is not malformed at all.
    """
    tokens = {token.strip(".") for token in _screening_text((description,)).split()}
    over, under = "over" in tokens, "under" in tokens
    if over is under:  # neither, or a label claiming both
        return None
    return Selection.OVER if over else Selection.UNDER


def signed_handicap(description: str, *, price_shaped: float = 100.0) -> float | None:
    """The one explicitly signed handicap in *description*, or ``None``.

    Exactly one handicap-shaped token, or nothing.  Two of them ("Phils -1.5
    -2.5") do not say which is the line — taking the first, or the last, is a
    coin flip wearing a rule's clothes — and a token of ``±price_shaped`` or
    more is an American price, not a handicap: exchange rows display prices
    ("Phillies -110"), and reading one as a line publishes a -110 handicap
    that validation faults on MLB and accepts as junk in the high-total
    leagues.  A price *beside* a handicap is unambiguous once discounted.
    """
    found: set[float] = set()
    for token in description.replace("(", " ").replace(")", " ").split():
        if token[:1] in "+-" and len(token) > 1:
            try:
                value = float(token)
            except ValueError:
                continue
            if abs(value) >= price_shaped:
                continue
            found.add(value)
    # A *set*: two tokens saying the same number agree about the line, and
    # counting them as a disagreement is how a corroboration check defeats
    # itself.  ProphetX joins ``display_name`` and ``name`` before asking, and
    # a venue that fills both fields identically — an ordinary API shape —
    # produced "+1.5 +1.5", two tokens, and the guard read that as ambiguous
    # and published the market as a pick'em.
    return found.pop() if len(found) == 1 else None


def refuse_one_sided_market(
    source: str, outcome: Any, first_index: int, *, market_id: Any, event_id: str
) -> None:
    """Refuse the rows one market just produced if they all name one side.

    *first_index* is ``len(outcome.quotes)`` taken **before** the market was
    parsed, so ``outcome.quotes[first_index:]`` is exactly that market's
    output.  Judging by parse call rather than by reconstructing the grouping
    from the finished rows is the whole point: two attempts to infer it from
    quote fields both failed in the ways an inference does.  Keying on
    ``source_market_id`` alone put every row from every *id-less* market in an
    event into one bucket and judged unrelated markets as a single contract;
    exempting the id-less ones instead then let a genuine same-side pair from
    such a market publish unjudged.  And a venue that reuses one id across a
    ladder's rungs defeats any key built from ids, however many fields are
    bolted on — the parser knows which rows came from which market, so it is
    the parser that asks.

    A market prices opposite sides by construction, so two or more rows all
    agreeing on ``selection`` means the payload named one club (or one
    competitor id) on both outcomes.  :func:`drop_duplicate_selections` cannot
    see it — sign-opposed handicaps give the rows different lines and
    therefore different dedup keys — and published, they are two bets on the
    same team presented as a hedge: paired against another book's genuine
    other side they read as an arbitrage while both legs lose together.
    """
    produced = outcome.quotes[first_index:]
    if len(produced) < 2 or len({quote.selection for quote in produced}) > 1:
        return
    outcome.reject(
        source,
        "market_prices_one_side_twice",
        f"{event_id}: market {market_id} published {len(produced)} legs all "
        f"on {produced[0].selection.value}",
        event_id=event_id,
    )
    del outcome.quotes[first_index:]


#: Field names that carry a market's **label** — the human-readable name of
#: what is being priced.  Curated, and deliberately not "every string field".
#:
#: Reading one chosen field is too narrow: Novig's first period guard read
#: ``description``, a field absent from the shape its own docs document, so on
#: the documented payload it was inert.  But reading *everything* is worse in
#: the other direction, because settlement prose is prose: a ``rules`` field
#: saying "void if the game is suspended before the end of the regulation
#: **period**", or a note reading "the **first** listed team is the away team",
#: tokenises straight into a period marker and deletes an ordinary full-game
#: market.  At fetch time that loss is invisible — no counter, no rejection,
#: just a book never requested — and one such field on every market emptied a
#: whole league and failed the pass.
#:
#: So: the fields a venue puts a *label* in, and no others.  A venue naming its
#: window somewhere else entirely is a gap the first genuine capture closes by
#: adding the key here, which is a smaller and louder failure than silently
#: dropping live markets.
MARKET_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "name", "label", "title", "caption", "heading",
        "description", "short_description", "shortdescription",
        "group_name", "groupname", "group", "market_group", "marketgroup",
        "market_name", "marketname", "market_label",
        "display_name", "displayname",
        "sub_type", "subtype", "sub_market", "submarket",
        "category", "segment", "scope",
        # ``period`` and not only ``period_name``: a venue naming its window in
        # the bare field and nowhere in prose slipped both screens, and
        # including one spelling while excluding the other is not a line
        # anybody could defend.
        "period", "period_name", "periodname",
    }
)


def market_label_text(entry: Any) -> str:
    """The label-bearing string fields of a market payload, joined.

    See :data:`MARKET_LABEL_KEYS` for why this is a curated set rather than
    every string on the record.
    """
    if not isinstance(entry, Mapping):
        return ""
    return " ".join(
        value
        for key, value in entry.items()
        if isinstance(value, str) and str(key).strip().lower() in MARKET_LABEL_KEYS
    )


def drop_duplicate_selections(source: str, outcome: Any) -> None:
    """Keep one row per ``dedup_key``, rejecting the rest.

    Storage enforces ``dedup_key`` with a UNIQUE constraint, so a single
    collision aborts that source's insert.  Nothing in a payload guarantees two
    markets cannot land on the same line, so the invariant is enforced here
    rather than hoped for — and a collision is a parser failure worth surfacing,
    not a silent drop.
    """
    seen: dict[tuple[str, ...], Any] = {}
    kept = []
    for quote in outcome.quotes:
        first = seen.get(quote.dedup_key)
        if first is not None:
            outcome.reject(
                source,
                "duplicate_dedup_key",
                f"{quote.dedup_key} priced twice: markets {first.source_market_id} and "
                f"{quote.source_market_id} on event {quote.source_event_id}",
                event_id=quote.source_event_id,
            )
            continue
        seen[quote.dedup_key] = quote
        kept.append(quote)
    outcome.quotes = kept


def within_schedule_horizon(commence_time, captured_at, competition) -> bool:
    """Is this fixture near enough to be worth collecting a pregame price for?

    A venue may list a real fixture months out — Polymarket prices a September
    baseball game in July — and such a row is not a futures leak: it resolves to
    two competitors and carries a real start time.  It is simply not *comparable*.
    No other source here has listed it yet, so it can never join, and the league's
    own ``max_schedule_horizon`` is the pipeline's existing statement of how far
    ahead one of its fixtures is expected to be.

    Collecting it anyway would fail validation's ``commence_time_too_far`` check
    on every run, which grades a real fixture as corruption and buries the
    findings that mean something.  So it is a counted omission instead, and the
    horizon stays a tight guard for the books that genuinely do not schedule
    that far ahead.
    """
    return commence_time <= captured_at + competition.max_schedule_horizon


#: How far apart two captures must be before they are treated as different runs,
#: **when the envelopes do not say which pass they belong to**.
#:
#: Generous on purpose: the slowest source here takes about 2½ minutes for one
#: pass (Smarkets, paced to its own rate limit), so anything under that is one
#: run's responses arriving over time rather than two runs.
#:
#: This threshold cannot be made to separate two passes of a watch loop, and
#: nothing smaller can either: at the default five-minute cadence
#: (:data:`src.settings.DEFAULT_INTERVAL_SECONDS`) consecutive passes are closer
#: together than one slow pass takes to finish, so no single number splits them
#: without also splitting a pass in half.  That is why
#: :attr:`~src.raw_store.RawResponse.capture_id` exists and why this is only the
#: fallback for envelopes written before it did.
RUN_SEPARATION = timedelta(minutes=20)


def latest_capture(
    raws: Sequence[RawResponse], *, separation: timedelta = RUN_SEPARATION
) -> list[RawResponse]:
    """Only the responses belonging to the most recent collection pass.

    Necessary wherever an endpoint label carries a **position counter** — page or
    batch index — which is most of the newer sources.  ``latest_per_endpoint``
    keeps the newest response *per label*, and that is only sound within one run:
    across two, ``page 03`` covers different items each time, so a shorter newer
    slate leaves the older run's trailing page standing as the "latest" for its
    label and yesterday's markets are mixed into today's rows.  On an order-book
    source that is not a stale row but a phantom price: ``_best_offers`` takes
    the maximum across the merged set, so a resting order that has since been
    filled outbids the live book.

    Kambi solves this precisely, by resolving which response owns which event id.
    That works because its payloads carry event ids; a page of markets from an
    API that does not is not so easily attributed.  The collector therefore
    stamps every response it persists with the pass that produced it, and this
    function groups on that stamp — an exact answer rather than an inference.

    Timestamps are the fallback, for envelopes captured before the stamp
    existed.  They are a genuinely weaker answer: two passes of ``collect
    --watch`` at the default cadence overlap any threshold wide enough to hold
    one slow pass together.

    A mixed batch — some stamped, some not — keeps **only** the stamped newest
    pass and discards every unstamped response outright.  The time window is not
    applied to the remainder; this said that it was, which overstated what the
    code does in the one direction a reader would rely on.  Discarding is the
    safe reading — an unstamped page cannot be shown to belong to the stamped
    pass, and a page from an older capture must never smuggle itself into a new
    one — but a mixed batch only arises while a stored run predates the stamp,
    and what it costs there is coverage, not correctness.

    Single-run input, which is what the collector and ``replay_run`` both hand
    over, passes through unchanged either way.
    """
    if not raws:
        return []
    newest = max(raws, key=lambda raw: raw.fetched_at)
    if newest.capture_id:
        return [raw for raw in raws if raw.capture_id == newest.capture_id]
    return [
        raw
        for raw in raws
        if not raw.capture_id and newest.fetched_at - raw.fetched_at <= separation
    ]


# ── which instance produced these bytes ──────────────────────────────────────


def envelope_source(raws: Sequence[RawResponse], *, fallback: str) -> str:
    """The source key the captured responses were fetched under.

    This is what makes a *parse* independent of a *configuration*.  ``source`` is
    the first element of ``dedup_key``, so two instances of one adapter — two
    Kambi tenants, say — must emit different values for it or they collide on the
    storage layer's UNIQUE constraint and abort the whole run's insert.  Reading
    it off the envelope rather than off the instance means ``parse`` stays a pure
    function of bytes, and replay reproduces the right key without knowing any
    operator config: :func:`src.collector.replay_run` constructs an adapter with
    no arguments at all.

    A mixed batch is a caller error rather than a data fault — every code path
    hands one source's responses at a time — so it raises instead of guessing.
    """
    keys = {raw.source for raw in raws if raw.source}
    if not keys:
        return fallback
    if len(keys) > 1:
        from src.sources.guards import FormatChangeError

        raise FormatChangeError(
            f"responses from several sources were handed to one parser: {sorted(keys)}"
        )
    return next(iter(keys))


# ── fetch tiers ──────────────────────────────────────────────────────────────


class Tier(str, Enum):
    """How deep a collection pass goes.

    ``core`` is the slate: every source's moneyline, spread and total for a
    league, from the endpoints that return a whole league at once.  It is a few
    requests per source, which is what makes a short polling interval defensible.

    ``full`` adds the per-event follow-ups — FanDuel's soccer detail page is 126
    of its 133 requests — which are worth an order of magnitude more requests and
    belong on a longer interval.

    The split is a **request budget**.  What a source may narrow under ``core``
    is what it *asks for*; what it may not do is quietly change the meaning of
    what it returns.  Concretely, three things are allowed and one is not:

    * deferring a per-event follow-up (FanDuel's soccer detail page), which
      removes whole *markets* — and the source must then narrow
      :meth:`~src.sources.base.OddsSource.capabilities` to match, so the absence
      is a declared scope rather than a market that appears to have vanished;
    * asking only for main lines and deferring the alternate *ladder* (SX Bet,
      where every alternate is a share of an order-book request: 47 requests
      against 27).  The market set is unchanged, so ``capabilities`` is too — it
      is the number of lines that differs, not what a line means;
    * collecting identically in both tiers, which most sources do.

    What is **not** allowed is a source that returns a different *kind* of row
    under one tier — a different period, a different settlement rule — because
    then the two tiers are not comparable and neither is a run against its own
    history.  Each adapter says in its ``fetch_raw`` docstring which of these it
    does, so the answer is written down rather than inferred from a row count.
    """

    CORE = "core"
    FULL = "full"

    @property
    def includes_depth(self) -> bool:
        return self is Tier.FULL
