"""The source contract.

One protocol, deliberately: fetching and parsing are separate so that parsing
is a pure function of stored bytes.  ``fetch_raw`` touches the network and
returns captured responses; ``parse`` turns those responses into normalized
rows and never performs I/O.  Replay is then just ``parse`` over responses
loaded from disk, which is what the regression tests do.

Adapters do not keep health state.  The collector derives health from what
actually happened during a run, so there is one implementation of that logic
instead of one per adapter.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from src.raw_store import RawResponse
from src.schema import Market, Quote

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # Imported lazily: ``src.sources._common`` pulls in httpx, and the contract
    # this module states must be readable without a network stack present.
    from src.sources._common import Tier


@dataclass(frozen=True)
class Rejection:
    """A record that should have parsed but did not.

    Rejections are failures — they mean the source offered something in scope
    that this parser could not faithfully represent.  They are counted and
    stored so a format change shows up as a spike rather than as silence.
    """

    source: str
    reason: str
    """Stable machine-readable code, e.g. ``"unknown_team"``."""
    detail: str
    context: dict[str, Any] = field(default_factory=dict)


class ReasonCounter(Counter):
    """A :class:`Counter` that refuses to materialize a reason that counted nothing.

    ``counter[reason] += n`` with ``n == 0`` inserts the key at zero on a plain
    ``Counter`` — a label asserting that out-of-scope input was seen when none
    was.  Several call sites increment by *computed* row counts
    (``len(rows) - len(usable)``, ``_v2_row_count(...)``), and the v2 Action
    Network parser shipped exactly this: ``odds_not_an_object: 0`` on every
    capture whose rows were all objects, which
    ``test_out_of_scope_input_is_counted_rather_than_ignored`` refuses.

    "By construction" has to cover more than the ``+=`` idiom, because an
    adversarial pass demonstrated three other doors: ``Counter.update`` (and
    therefore the constructor and ``copy``) takes a dict-level fast path that
    bypasses ``__setitem__`` when the counter is empty, and an increment
    followed by a matching decrement leaves an existing key at zero.  So
    ``__setitem__`` *removes* a key set to zero rather than keeping it, and
    ``update`` purges zeros after delegating.  Runs 13–27 in the working store
    predate this class and hold the old zero-count rows; stored records are
    history and are not rewritten.
    """

    def __setitem__(self, key: str, value: int) -> None:
        if value == 0:
            super().pop(key, None)
            return
        super().__setitem__(key, value)

    def update(self, iterable=None, /, **kwds) -> None:  # type: ignore[override]
        # Only the empty-counter dict-level fast path escapes __setitem__;
        # subtract() needs no twin because it assigns per element and the
        # zero-delete in __setitem__ already catches it (mutation-verified).
        super().update(iterable, **kwds)
        for key in [key for key, count in self.items() if count == 0]:
            del self[key]

    def setdefault(self, key: str, default: int | None = None) -> int | None:
        # dict.setdefault inserts at the C level without calling __setitem__,
        # so a zero default would materialize the key; answer 0 without holding it.
        if default == 0 and key not in self:
            return 0
        return super().setdefault(key, default)


@dataclass
class ParseOutcome:
    """Everything a parse produced: rows, failures, and deliberate omissions."""

    quotes: list[Quote] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=ReasonCounter)
    """Counts of *out-of-scope* input, keyed by reason.  Player props, futures
    and pitcher-conditional markets are expected and not errors — but they are
    counted so "we collected everything" is never assumed."""
    repaired: Counter[str] = field(default_factory=Counter)
    """Counts of rows that were **published after a field was reconstructed**.

    Neither a skip nor a rejection: the row is in :attr:`quotes`.  These were
    being counted in :attr:`skipped`, which the dashboard renders as "seen but
    out of scope" and whose note read "so the row is not kept" — the exact
    opposite of what happens.  118 Kambi rows across the two tenants were
    reported as discarded and were in fact published, carrying a derived
    American price because the feed's own two formats disagreed.

    Worth counting on its own: a venue whose formats stop agreeing is a
    data-quality signal, and burying it in a bucket labelled "not collected"
    loses it twice over."""

    def reject(self, source: str, reason: str, detail: str, **context: Any) -> None:
        self.rejections.append(Rejection(source=source, reason=reason, detail=detail, context=context))

    @property
    def event_keys(self) -> set[str]:
        return {quote.event_key for quote in self.quotes}


@dataclass(frozen=True)
class SourceHealth:
    """Outcome of one source's participation in one collection run."""

    source_key: str
    ok: bool
    checked_at: datetime
    request_count: int = 0
    raw_bytes: int = 0
    latency_ms: float | None = None
    quote_count: int = 0
    event_count: int = 0
    rejection_count: int = 0
    skipped_count: int = 0
    repaired_count: int = 0
    unchanged_payloads: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    scopes_requested: int = 0
    """How many scopes this source asked for, so a refusal has a denominator."""
    scopes_failed: int = 0
    """How many **distinct** scopes were refused, which is the numerator.

    Not ``len(failed_scopes)``: that is a list of messages, and one scope can
    produce two of them — SX Bet's metadata half and its order-book half are one
    scope and two requests — while the denominator de-duplicates.  Counting
    messages printed "was refused 2 of the 1 scope(s) it asked for".
    """
    truncated_scopes: tuple[str, ...] = ()
    """Scopes that answered and then stopped short of their whole slate.

    Reported, never graded against the denominator.  A self-imposed page cap
    leaves behind a fraction of *one* scope, of unknown size, and calling that
    "the whole scope was lost" reported a Polymarket run that collected 41 MLB
    events as one that "returned none of the rest".
    """
    failed_scopes: tuple[str, ...] = ()
    """Scopes this source asked for and was refused, on a run it survived.

    A source that loses one league and keeps the rest is neither healthy nor
    failed, and reporting it as either is wrong.  Before this it read as
    healthy: the refusal reached a log line and stopped there, so a book that
    lost 14% of its rows to a 429 summarised as ``OK``.
    """

    def summary(self) -> str:
        if not self.ok:
            return f"{self.source_key}: FAILED [{self.error_kind}] {self.error_message}"
        parts = [
            f"{self.quote_count} quotes",
            f"{self.event_count} events",
            f"{self.request_count} requests",
            f"{self.raw_bytes / 1024:.0f} KiB",
        ]
        if self.latency_ms is not None:
            parts.append(f"{self.latency_ms:.0f} ms")
        if self.failed_scopes:
            parts.append(f"{self.scopes_failed or len(self.failed_scopes)} SCOPE(S) REFUSED")
        if self.truncated_scopes:
            parts.append(f"{len(self.truncated_scopes)} scope(s) cut short")
        if self.rejection_count:
            parts.append(f"{self.rejection_count} REJECTED")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} out of scope")
        if self.repaired_count:
            parts.append(f"{self.repaired_count} repaired")
        if self.unchanged_payloads:
            parts.append(
                f"{self.unchanged_payloads}/{self.request_count} payloads byte-identical to "
                "the previous run"
            )
        return f"{self.source_key}: OK ({', '.join(parts)})"


@runtime_checkable
class OddsSource(Protocol):
    """A sportsbook that can supply pregame game markets.

    One adapter per **book**, not per book-and-sport: the adapter owns the book's
    HTTP session, its raw-capture conventions and its parsing rules, and is
    parameterized by which leagues to collect.  Splitting it per sport would
    duplicate all of that three to six times over and open a new session per
    sport for no benefit.
    """

    @property
    def source_key(self) -> str:
        """Stable identifier, e.g. ``"pinnacle"``."""
        ...

    @property
    def leagues(self) -> tuple[str, ...]:
        """Canonical league keys this instance is configured to collect.

        The collector reports coverage against this, so a league that is
        configured but returns nothing is visible as a gap rather than being
        indistinguishable from one that was never requested.
        """
        ...

    def capabilities(self, *, tier: "Tier" = ...) -> Mapping[str, frozenset[Market]]:
        """Markets this instance claims to price, per league, at the full-game window.

        Declaring this is what lets coverage reporting say "this source has no
        totals for soccer" instead of inferring it from an absence — and it stops
        validation faulting a book for a market it never claimed.  The claim
        depends on the *tier*, because deferring a per-event call genuinely
        collects less: FanDuel's soccer spreads and totals exist only on its
        event page, so a ``core`` pass claims the moneyline alone and an absence
        of the other two is a request budget rather than a renamed label.
        """
        ...

    def fetch_raw(self, *, tier: "Tier" = ...) -> list[RawResponse]:
        """Perform the network calls and return every captured response.

        Must raise a :class:`src.sources.guards.SourceError` subclass on
        blocked, rate-limited, geo-restricted, empty, non-JSON, or structurally
        changed responses rather than returning nothing.

        *tier* is a **request budget**, not a market filter.  A source with no
        per-event follow-up returns the same responses for both tiers and says so
        in its docstring; one that has such a follow-up omits it under ``core``
        and narrows :meth:`capabilities` to match, so that the smaller row set is
        a stated consequence rather than a mystery.
        """
        ...

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        """Turn captured responses into normalized rows.  No I/O."""
        ...

    def close(self) -> None:
        """Release held resources."""
        ...
