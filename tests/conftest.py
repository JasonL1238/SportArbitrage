"""Fixtures backed by real captured sportsbook responses.

``tests/fixtures/raw`` holds verbatim envelopes written by
:class:`src.raw_store.RawStore` during a real live run on 2026-07-28, so the
parser tests run against bytes a sportsbook actually sent rather than against a
hand-written approximation of them.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.normalize import decimal_to_american
from src.raw_store import RawResponse, RawStore
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Sport

FIXTURE_RAW_DIR = Path(__file__).parent / "fixtures" / "raw"

#: The slate captured in the fixtures.
FIXTURE_DATE = "2026-07-28"


def _load(source: str) -> list[RawResponse]:
    store = RawStore(FIXTURE_RAW_DIR)
    paths = sorted(FIXTURE_RAW_DIR.glob(f"{source}__*.json"))
    if not paths:
        raise AssertionError(
            f"no captured payloads for {source} in {FIXTURE_RAW_DIR}.\n"
            "Every registered source needs a fixture: without one it escapes the "
            "contract test entirely, which is the check that says an adapter is "
            "finished.  Capture one with a short live run and commit the envelopes."
        )
    return [store.read(path) for path in paths]


@pytest.fixture(scope="session")
def registered_raws() -> dict[str, list[RawResponse]]:
    """Captured payloads for **every** registered source, keyed by source key.

    Derived from :data:`src.sources.registry.SOURCES` rather than from a list
    kept alongside it.  A hand-maintained list is a list that drifts: the file
    that used to hold one said, in its own docstring, that an adapter is finished
    when the contract test passes with it added — and the only thing that made
    that true was somebody remembering to add it.  A registered source with no
    fixture now fails loudly here instead of being quietly skipped.
    """
    from src.sources import registry

    return {key: _load(key) for key in registry.keys()}


@pytest.fixture(scope="session")
def fanduel_raw() -> list[RawResponse]:
    return _load("fanduel")


@pytest.fixture(scope="session")
def pinnacle_raw() -> list[RawResponse]:
    return _load("pinnacle")


@pytest.fixture(scope="session")
def kambi_raw() -> list[RawResponse]:
    return _load("betrivers_kambi")


@pytest.fixture(scope="session")
def all_fixture_quotes(registered_raws: dict[str, list[RawResponse]]) -> list[Quote]:
    """Every normalized row the registered sources' fixtures produce."""
    from src.sources import registry

    quotes: list[Quote] = []
    for key, raws in registered_raws.items():
        adapter = registry.descriptor(key).replay_instance()
        try:
            quotes.extend(adapter.parse(raws).quotes)
        finally:
            adapter.close()
    return quotes


def make_quote(**overrides) -> Quote:
    """A valid quote, for tests that need to perturb one field."""
    observed = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    defaults = dict(
        source="testbook",
        observed_at=observed,
        raw_ref="testbook/20260728T070000Z/x/abc123",
        sport=Sport.BASEBALL,
        league="MLB",
        event_key=f"MLB-PHI@MLB-MIA:{FIXTURE_DATE}",
        source_event_id="evt-1",
        home_participant="MLB-MIA",
        away_participant="MLB-PHI",
        home_team="Miami Marlins",
        away_team="Philadelphia Phillies",
        commence_time=datetime(2026, 7, 28, 22, 41, tzinfo=UTC),
        market=Market.MONEYLINE,
        period=Period.FULL_GAME,
        selection=Selection.HOME,
        decimal_odds=1.9,
        american_odds=-111,
        implied_probability=1 / 1.9,
        source_market_id="mkt-1",
        status=QuoteStatus.ACTIVE,
    )
    defaults.update(overrides)
    # Overriding the price re-derives everything derived from it, so a test that
    # sets decimal_odds gets a self-consistent row rather than one that trips
    # validation's price-encoding checks for reasons the test never intended.
    # An explicit override always wins, so a test can still build a row that is
    # deliberately inconsistent.
    # An impossible price is left exactly as given, so that a test feeding one in
    # gets the schema's rejection rather than a conversion error from this helper.
    if "decimal_odds" in overrides and defaults["decimal_odds"] > 1.0:
        if "implied_probability" not in overrides:
            defaults["implied_probability"] = 1 / defaults["decimal_odds"]
        if "american_odds" not in overrides:
            defaults["american_odds"] = decimal_to_american(defaults["decimal_odds"])
    return Quote(**defaults)


def make_raw(body: str, *, source: str = "testbook", endpoint: str = "test", status: int = 200) -> RawResponse:
    return RawResponse(
        source=source,
        endpoint=endpoint,
        url="https://example.invalid/test",
        status_code=status,
        body=body,
        fetched_at=datetime(2026, 7, 28, 7, 0, tzinfo=UTC),
        content_type="application/json",
    )
