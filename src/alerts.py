"""Alert dispatchers for new arbitrage opportunities.

Supports terminal bell, Discord webhooks, and Telegram Bot API (all free).
All dispatch is gated behind ``config.ENABLE_ALERTS``.
"""
from __future__ import annotations

import logging
import os
import sys

import httpx

from src import config
from src.models import ArbOpportunity

log = logging.getLogger(__name__)


def _format_alert(opp: ArbOpportunity) -> str:
    lines = [
        f"Arb found: {opp.home_team} vs {opp.away_team}",
        f"  Sport: {opp.sport_key}",
        f"  Market: {opp.market_key}" + (f" ({opp.point})" if opp.point else ""),
        f"  Margin: {opp.margin_pct}",
        f"  Profit on ${opp.total_stake:.0f}: ${opp.guaranteed_profit:.2f}",
    ]
    for b, s in zip(opp.best_outcomes, opp.stakes):
        lines.append(f"  {b.outcome_name} @ {b.bookmaker_title}: {b.decimal_odds:.3f} → ${s:.2f}")
    return "\n".join(lines)


def alert_terminal(opp: ArbOpportunity) -> None:
    if os.getenv("CI"):
        return
    msg = _format_alert(opp)
    print(f"\a\n{'='*50}\n{msg}\n{'='*50}\n", file=sys.stderr)


def alert_discord(opp: ArbOpportunity) -> bool:
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return True
    try:
        httpx.post(url, json={"content": f"```\n{_format_alert(opp)}\n```"}, timeout=10)
        return True
    except Exception:
        log.exception("Failed to send Discord alert")
        return False


def alert_telegram(opp: ArbOpportunity) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(
            url,
            json={"chat_id": chat_id, "text": _format_alert(opp), "parse_mode": ""},
            timeout=10,
        )
        return True
    except Exception:
        log.exception("Failed to send Telegram alert")
        return False


def send_alerts(opp: ArbOpportunity) -> bool:
    """Dispatch all configured alert channels.

    Returns True if all channels succeeded, False if any failed.
    """
    if not config.ENABLE_ALERTS:
        return True

    alert_terminal(opp)

    success = True
    if config.DISCORD_WEBHOOK_URL:
        if not alert_discord(opp):
            success = False
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        if not alert_telegram(opp):
            success = False
    return success
