"""The single normalized schema for collected betting data.

Every source adapter must emit :class:`Quote` rows and nothing else.  The
market/period/selection vocabularies are **closed enums** defined in
:mod:`src.vocab`: a source value that cannot be mapped to one of them is rejected
upstream rather than being passed through as free text.  That is deliberate — a
free-text fallback makes unnormalized data look normalized, which is the failure
this schema exists to prevent.

One row = one priced selection, at one line, from one book, observed at one
instant.  ``line`` is always expressed from the perspective of that row's
selection (home -1.5 / away +1.5), so a two-sided market always satisfies
``home.line == -away.line`` for spreads and ``over.line == under.line`` for
totals.  :mod:`src.validation` enforces this.

The row carries both a display name and a resolved identity for each
participant.  ``home_team``/``away_team`` are for humans; ``home_participant``/
``away_participant`` are the keys everything joins on.  Keeping the identity on
the row means reconciliation never has to re-resolve a name it already resolved
once, and a book's spelling can change without moving an event.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.vocab import (
    CORE_MARKETS_BY_SPORT,
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    PERIOD_RULES,
    SELECTIONS_BY_MARKET,
    Market,
    Period,
    PeriodRules,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    draw_is_priced,
    is_collectable,
    period_rules,
    scoring_unit,
    tie_possible,
)

__all__ = [
    "CORE_MARKETS_BY_SPORT",
    "MARKETS_REQUIRING_LINE",
    "MARKETS_REQUIRING_SIDE",
    "PERIOD_RULES",
    "SELECTIONS_BY_MARKET",
    "Market",
    "Period",
    "PeriodRules",
    "Quote",
    "QuoteStatus",
    "Selection",
    "Side",
    "Sport",
    "draw_is_priced",
    "is_collectable",
    "period_rules",
    "scoring_unit",
    "tie_possible",
]


class Quote(BaseModel):
    """One priced selection from one sportsbook."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)

    # ── provenance ───────────────────────────────────────────────────────────
    source: str
    observed_at: datetime
    """When *this collector* fetched the payload.  Never a source-supplied
    time — those live in :attr:`last_change_at`."""
    raw_ref: str
    """Reference to the stored raw response **the price** was parsed from.

    Single-valued, always.  It is the response whose ``fetched_at`` is
    :attr:`observed_at`, so the two are mechanically checkable against each
    other."""

    identity_raw_ref: str | None = None
    """Reference to the stored response that supplied the *event identity*, when
    that came from a different call than the price.

    Pinnacle needs two requests — ``matchups`` gives the participants and start
    time, ``markets/straight`` gives the prices — so a row parsed from the second
    owes its teams to the first.  Without this the identity provenance is not
    traceable from the row at all.

    It is a separate field rather than a second value packed into
    :attr:`raw_ref` because a compound ``"a+b"`` string would give that field an
    undeclared grammar: every reader and every check would have to know to split
    it, and the ones that did not would silently compare a concatenation against a
    set of real references and conclude the row was orphaned."""

    # ── event identity ───────────────────────────────────────────────────────
    sport: Sport
    league: str
    """Canonical league key from :mod:`src.leagues`.  Carried for coverage
    reporting; deliberately *not* part of :attr:`event_key`, because books
    disagree about classification and that must not break a join."""
    event_key: str
    """Cross-source event identity: ``AWAY@HOME:YYYY-MM-DD`` built from
    participant keys on the league's scheduling date, with ``#n`` appended when
    the same pair meets twice in a day."""
    source_event_id: str
    home_participant: str
    away_participant: str
    """Resolved participant keys — what everything joins on."""
    home_team: str
    away_team: str
    """Canonical display names, for humans."""
    commence_time: datetime

    # ── market identity ──────────────────────────────────────────────────────
    market: Market
    period: Period
    selection: Selection
    side: Side | None = None
    line: float | None = None
    is_alternate: bool = False

    # ── price ────────────────────────────────────────────────────────────────
    decimal_odds: float
    american_odds: int
    implied_probability: float

    # ── source detail ────────────────────────────────────────────────────────
    source_market_id: str | None = None
    source_selection_id: str | None = None
    limit_amount: float | None = None
    status: QuoteStatus = QuoteStatus.ACTIVE
    last_change_at: datetime | None = None
    """Source-reported time the price last changed, when the source provides
    one.  Distinct from :attr:`observed_at` so freshness is never inferred from a
    source's own clock."""

    @field_validator("observed_at", "commence_time", "last_change_at")
    @classmethod
    def _require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator(
        "source",
        "league",
        "event_key",
        "source_event_id",
        "home_participant",
        "away_participant",
        "home_team",
        "away_team",
    )
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("decimal_odds")
    @classmethod
    def _check_decimal_odds(cls, value: float) -> float:
        # 1.0 means "risk everything to win nothing" — never a real price.
        if not 1.0 < value <= 1000.0:
            raise ValueError(f"decimal_odds out of range: {value}")
        return value

    @field_validator("implied_probability")
    @classmethod
    def _check_implied_probability(cls, value: float) -> float:
        if not 0.0 < value < 1.0:
            raise ValueError(f"implied_probability must be a probability, got {value}")
        return value

    @field_validator("line", "limit_amount")
    @classmethod
    def _require_finite(cls, value: float | None) -> float | None:
        """Refuse ``nan`` and ``±inf``, which are reachable and destructive.

        JSON has no such literals, but :func:`json.loads` accepts the bare words
        ``NaN``, ``Infinity`` and ``-Infinity`` by default, so a feed emitting
        one decodes silently.  From there:

        * ``inf`` passes an adapter's ``available > 0`` liquidity test and
          becomes a ``limit_amount``, which reaches the stake-capping arithmetic
          in :mod:`src.arb` and raises ``OverflowError`` — the run dies inside
          the detector rather than at the row that caused it.
        * ``nan`` is stored by SQLite as **NULL**, so a total's line round-trips
          to ``None`` and the row that loaded cleanly on the way in raises
          "market total requires a line" on the way out, from inside
          ``load_quotes`` where there is no row to point at.

        Both are caught here instead, where the offending row can be named.
        """
        if value is None:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"must be a finite number, got {value}")
        return value

    @field_validator("limit_amount")
    @classmethod
    def _check_limit_amount(cls, value: float | None) -> float | None:
        # ``None`` means "the venue did not say", which is not the same claim as
        # "you may stake nothing" and must stay distinguishable from it.
        if value is not None and value <= 0.0:
            raise ValueError(f"limit_amount must be positive when stated, got {value}")
        return value

    @model_validator(mode="after")
    def _check_market_shape(self) -> Quote:
        if not is_collectable(self.sport, self.period):
            raise ValueError(
                f"{self.sport.value}/{self.period.value} has no recorded settlement "
                "rules, so it must not be collected"
            )
        legal = SELECTIONS_BY_MARKET[self.market]
        if self.selection not in legal:
            raise ValueError(
                f"selection {self.selection} illegal for market {self.market} "
                f"(legal: {sorted(s.value for s in legal)})"
            )
        if self.market in MARKETS_REQUIRING_LINE and self.line is None:
            raise ValueError(f"market {self.market} requires a line")
        if self.market not in MARKETS_REQUIRING_LINE and self.line is not None:
            raise ValueError(f"market {self.market} must not carry a line")
        if self.market in MARKETS_REQUIRING_SIDE and self.side is None:
            raise ValueError(f"market {self.market} requires a side")
        if self.market not in MARKETS_REQUIRING_SIDE and self.side is not None:
            raise ValueError(f"market {self.market} must not carry a side")
        # A draw is only legal where the books actually price one.  A full-game
        # baseball or hockey moneyline cannot push — extra innings and the
        # shootout decide them — so a "draw" there is a misparsed third runner.
        if self.selection is Selection.DRAW and not draw_is_priced(self.sport, self.period):
            raise ValueError(
                f"draw is not a priced outcome for {self.sport.value}/{self.period.value}"
            )
        if self.home_participant == self.away_participant:
            raise ValueError(
                f"home and away are the same participant: {self.home_participant}"
            )
        return self

    # ── derived identity ─────────────────────────────────────────────────────

    @property
    def settlement(self) -> PeriodRules:
        """The settlement facts for this row's scoring window."""
        return period_rules(self.sport, self.period)

    @property
    def scoring_unit(self) -> str:
        return scoring_unit(self.sport, self.period)

    @property
    def market_key(self) -> tuple[str, str, str]:
        """Identity of the market this row belongs to.

        Keyed on the *source's own* market id, because that is what defines one
        market at the book.  The synthesized fallback (for venues that publish
        no market id — the HTML line trackers) must reconstruct that identity
        from row fields, and a spread's two rows differ on exactly the fields a
        naive key would read: the home row is at -1.5 and the away row at +1.5,
        and ``side`` names the team rather than the market.  The first version
        embedded both, so every id-less spread landed in a group of one — no
        completeness check, no overround check, and ``refuse_mid_move_pairings``
        structurally blind to a third of what the trackers publish (a sub-fair
        -1.5/+1.5 pairing on the committed 2026-08-03 vi_hardrock capture,
        summing 0.9915, sailed through while the same capture's total was
        caught).  A spread therefore keys on the **unsigned** line with no
        side; ``is_alternate`` still separates a ladder's rungs from the main
        line, which is as much as can be reconstructed without the venue's id.
        The residual ambiguity is owned, not hidden: two *distinct* id-less
        spread offers sharing an absolute line (home −1.5 and away −1.5 as
        separate markets, or a ladder holding both signs of one rung) would
        share a key.  No id-less venue publishes that shape; if one ever does,
        the failure is loud, never silence — the two-offers shape fires
        ``spread_not_mirrored`` and ``negative_overround`` ERRORs, and the
        both-signs ladder (four rows under one key) fires
        ``repeated_selection_within_market`` — and
        ``refuse_mid_move_pairings`` refuses to judge a spread group whose
        lines are not sign-opposed for the same reason.
        """
        if self.market is Market.SPREAD:
            side_part = ""
            line_part = abs(self.line) if self.line is not None else None
        else:
            side_part = self.side.value if self.side else ""
            line_part = self.line
        market_id = self.source_market_id or (
            f"{self.market.value}|{self.period.value}|"
            f"{side_part}|{line_part}|{self.is_alternate}"
        )
        return (self.source, self.source_event_id, market_id)

    @property
    def dedup_key(self) -> tuple[str, ...]:
        """Identity of this exact priced selection at one observation.

        ``is_alternate`` is part of the identity because books genuinely offer the
        same bet twice: the main market at 8.5 and an alternate-line market that
        also happens to sit at 8.5, at slightly different prices and limits.  Both
        rows are real.  Leaving the flag out made them collide, which reported
        legitimate data as a ``conflicting_duplicate`` error and — because the
        storage layer enforces this key — aborted the insert of the entire run's
        quotes.
        """
        return (
            self.source,
            self.event_key,
            self.market.value,
            self.period.value,
            self.side.value if self.side else "",
            self.selection.value,
            "" if self.line is None else f"{self.line:g}",
            "alt" if self.is_alternate else "main",
        )
