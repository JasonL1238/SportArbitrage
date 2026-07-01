from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from src.alerts import alert_discord, send_alerts
from src.models import ArbOpportunity, BestOutcome


def _opp() -> ArbOpportunity:
    return ArbOpportunity(
        sport_key="basketball_nba",
        event_id="evt_1",
        home_team="Team A",
        away_team="Team B",
        commence_time=datetime.now(UTC),
        market_key="h2h",
        outcome_count=2,
        margin=0.03,
        implied_prob_sum=0.97,
        best_outcomes=[
            BestOutcome(outcome_name="Team A", bookmaker_key="bk_a", bookmaker_title="Book A", decimal_odds=2.20),
            BestOutcome(outcome_name="Team B", bookmaker_key="bk_b", bookmaker_title="Book B", decimal_odds=2.10),
        ],
        stakes=[48.0, 52.0],
        guaranteed_profit=3.0,
        total_stake=100.0,
        detected_at=datetime.now(UTC),
    )


class TestSendAlerts:
    def test_alerts_disabled_skips_dispatch(self):
        with patch("src.alerts.config") as mock_cfg:
            mock_cfg.ENABLE_ALERTS = False
            result = send_alerts(_opp())
        assert result is True

    def test_alerts_enabled_calls_discord(self):
        with (
            patch("src.alerts.config") as mock_cfg,
            patch("src.alerts.alert_discord", return_value=True) as mock_discord,
            patch("src.alerts.alert_terminal"),
        ):
            mock_cfg.ENABLE_ALERTS = True
            mock_cfg.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
            mock_cfg.TELEGRAM_BOT_TOKEN = ""
            mock_cfg.TELEGRAM_CHAT_ID = ""
            result = send_alerts(_opp())
        assert result is True
        mock_discord.assert_called_once()


class TestAlertDiscord:
    def test_http_error_returns_false(self):
        with patch("src.alerts.config") as mock_cfg:
            mock_cfg.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
            with patch("src.alerts.httpx.post", side_effect=httpx.ConnectError("fail")):
                result = alert_discord(_opp())
        assert result is False

    def test_no_webhook_url_returns_true(self):
        with patch("src.alerts.config") as mock_cfg:
            mock_cfg.DISCORD_WEBHOOK_URL = ""
            result = alert_discord(_opp())
        assert result is True
