"""Legacy adapter wrapping the existing OddsClient behind the SourceAdapter protocol."""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from src.models import Event
from src.odds_client import OddsClient
from src.sources.base import SourceHealth, SourceSnapshot

log = logging.getLogger(__name__)


class OddsApiAdapter:
    """Wraps :class:`OddsClient` to satisfy :class:`SourceAdapter`."""

    def __init__(self, client: OddsClient | None = None) -> None:
        self._client = client or OddsClient()
        self._owns_client = client is None
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._failure_count = 0

    @property
    def source_key(self) -> str:
        return "odds_api"

    def discover_events(self, sport_key: str) -> list[dict[str, Any]]:
        try:
            events = self._client.get_events(sport_key)
            self._record_success()
            return events
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def fetch_odds(self, sport_key: str) -> SourceSnapshot:
        self._fetch_count += 1
        t0 = time.monotonic()
        try:
            raw_odds, remaining = self._client.get_odds(sport_key)
            elapsed = time.monotonic() - t0
            self._record_success()
            return SourceSnapshot(
                source_key=self.source_key,
                sport_key=sport_key,
                raw_payload=json.dumps(raw_odds),
                fetched_at=datetime.now(UTC),
                url=f"{self._client.base_url}/sports/{sport_key}/odds/",
                status_code=200,
                content_type="application/json",
                parser_version="1",
                credits_used=self._estimate_credits(sport_key),
                credits_remaining=remaining,
            )
        except Exception as exc:
            self._record_failure(str(exc))
            raise

    def parse_events(self, raw: list[dict[str, Any]]) -> list[Event]:
        return OddsClient.parse_events(raw)

    def healthcheck(self) -> SourceHealth:
        return SourceHealth(
            source_key=self.source_key,
            is_healthy=self._last_failure is None or (
                self._last_success is not None
                and self._last_success > self._last_failure
            ),
            last_success=self._last_success,
            last_failure=self._last_failure,
            error_message=self._last_error,
            fetch_count=self._fetch_count,
            failure_count=self._failure_count,
        )

    def supports_delta(self) -> bool:
        return False

    def confidence_score(self) -> float:
        return 0.95

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OddsApiAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── internals ────────────────────────────────────────────────────────

    def _record_success(self) -> None:
        self._last_success = datetime.now(UTC)

    def _record_failure(self, msg: str) -> None:
        self._last_failure = datetime.now(UTC)
        self._last_error = msg
        self._failure_count += 1

    @staticmethod
    def _estimate_credits(sport_key: str) -> int:
        from src import config
        return len(config.MARKETS.split(",")) * len(config.REGIONS.split(","))
