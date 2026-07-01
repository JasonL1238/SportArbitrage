"""Normalize adapter events into source-agnostic quote rows."""
from __future__ import annotations

from datetime import UTC, datetime

from src.canonical import canonical_market
from src.models import Event, PriceQuote
from src.normalize import implied_probability


def quotes_from_events(
    events: list[Event],
    *,
    source: str,
    raw_payload_ref: str | None = None,
    fetched_at: datetime | None = None,
) -> list[PriceQuote]:
    """Flatten ``Event`` models into the shared ``PriceQuote`` schema."""
    ts = fetched_at or datetime.now(UTC)
    quotes: list[PriceQuote] = []
    for event in events:
        event_name = f"{event.away_team} @ {event.home_team}"
        league = event.sport_title or None
        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                market_type = canonical_market(market.key)
                for outcome in market.outcomes:
                    try:
                        prob = implied_probability(outcome.price)
                    except ValueError:
                        continue
                    quotes.append(
                        PriceQuote(
                            source=source,
                            sport=event.sport_key,
                            league=league,
                            event_name=event_name,
                            home_team=event.home_team,
                            away_team=event.away_team,
                            participant=outcome.name,
                            market_type=market_type,
                            line=outcome.point,
                            selection=outcome.name,
                            decimal_odds=outcome.price,
                            implied_probability=prob,
                            timestamp=market.last_update,
                            event_start_time=event.commence_time,
                            source_event_id=event.id,
                            source_market_id=outcome.source_market_id or f"{event.id}:{market.key}:{outcome.point}",
                            source_selection_id=outcome.source_selection_id,
                            liquidity=outcome.liquidity,
                            status=outcome.status,
                            raw_payload_ref=raw_payload_ref,
                        )
                    )
    return quotes
