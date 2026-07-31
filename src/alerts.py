"""SMS alerts when a takeable arbitrage clears the configured floor.

Opt-in and fail-soft: with no Twilio credentials the collector keeps running and
nothing is sent.  Credentials live in ``ODDS_TWILIO_*`` env vars — never in the
repo.  The default destination is the number configured in :mod:`src.settings`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

import httpx

from src import settings
from src.arb import Opportunity
from src.vocab import Market, Selection

log = logging.getLogger("alerts")

#: Twilio Messages endpoint template.
_TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

#: Soft cap so a bizarre multi-leg message still fits a few concatenated SMS.
_MAX_SMS_CHARS = 1400


def alert_ready() -> bool:
    """True when Twilio credentials and a destination number are configured."""
    return bool(
        settings.TWILIO_ACCOUNT_SID
        and settings.TWILIO_AUTH_TOKEN
        and settings.TWILIO_FROM_NUMBER
        and settings.ALERT_TO
    )


def opportunity_alert_key(opportunity: Opportunity) -> str:
    """Stable id for dedupe: same books, selections, and prices → one text."""
    legs = "|".join(
        f"{leg.source}:{leg.selection.value}:{leg.decimal_odds:.4f}:{leg.stake:.2f}"
        for leg in opportunity.legs
    )
    line = "" if opportunity.line is None else f"{opportunity.line:g}"
    return (
        f"{opportunity.event_key}|{opportunity.market.value}|"
        f"{opportunity.period.value}|{line}|{legs}"
    )


def qualifies(opportunity: Opportunity, *, min_roi: float | None = None) -> bool:
    """Risk-free and at least *min_roi* return on bankroll (ideal stakes)."""
    floor = settings.ALERT_MIN_ROI if min_roi is None else min_roi
    return opportunity.is_risk_free and opportunity.roi + 1e-12 >= floor


def _selection_label(opportunity: Opportunity, leg) -> str:
    """Human label for the bet slip: team name / over-under, plus line."""
    sel = leg.selection
    line = leg.quote.line
    if sel is Selection.HOME:
        base = opportunity.home_team
        if line is not None and opportunity.market is Market.SPREAD:
            # Quote.line is already from this selection's perspective.
            return f"{base} {line:+g}"
        return base
    if sel is Selection.AWAY:
        base = opportunity.away_team
        if line is not None and opportunity.market is Market.SPREAD:
            return f"{base} {line:+g}"
        return base
    if sel is Selection.DRAW:
        return "Draw"
    if sel is Selection.OVER:
        return f"Over {line:g}" if line is not None else "Over"
    if sel is Selection.UNDER:
        return f"Under {line:g}" if line is not None else "Under"
    return sel.value


def format_alert(opportunity: Opportunity) -> str:
    """Compact SMS body with enough to place both legs by hand."""
    roi_pct = opportunity.roi * 100.0
    line = "" if opportunity.line is None else f" @ {opportunity.line:g}"
    side = f" ({opportunity.side.value})" if opportunity.side else ""
    kickoff = opportunity.commence_time.strftime("%Y-%m-%d %H:%M %Z").strip() or (
        opportunity.commence_time.strftime("%Y-%m-%d %H:%M UTC")
    )
    head = (
        f"ARB {roi_pct:.1f}% | "
        f"+${opportunity.guaranteed_profit:.2f} on ${opportunity.total_stake:.0f}\n"
        f"{opportunity.away_team} @ {opportunity.home_team}\n"
        f"{opportunity.sport.value} {opportunity.market.value}/"
        f"{opportunity.period.value}{side}{line}"
    )
    legs = []
    for i, leg in enumerate(opportunity.legs, start=1):
        label = _selection_label(opportunity, leg)
        legs.append(
            f"{i}) {leg.source} {label} "
            f"{leg.quote.american_odds:+d} (${leg.decimal_odds:.3f}) "
            f"stake ${leg.stake:.2f}"
        )
    cap = ""
    if opportunity.max_total_stake is not None:
        cap = f"\nMax stake ~${opportunity.max_total_stake:.0f}"
    notes = "".join(f"\nNote: {n}" for n in opportunity.notes[:2])
    body = (
        f"{head}\n"
        + "\n".join(legs)
        + f"\nKickoff {kickoff}"
        + cap
        + notes
        + "\nPLACE BOTH NOW — prices move"
    )
    if len(body) > _MAX_SMS_CHARS:
        body = body[: _MAX_SMS_CHARS - 1] + "…"
    return body


def send_sms(
    body: str,
    *,
    to: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Send one SMS via Twilio.  Returns the message SID.

    Raises ``RuntimeError`` when credentials are missing, or propagates HTTP
    errors from Twilio so the caller can log and keep collecting.
    """
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    from_number = settings.TWILIO_FROM_NUMBER
    dest = to or settings.ALERT_TO
    if not (sid and token and from_number and dest):
        raise RuntimeError(
            "SMS not configured — set ODDS_TWILIO_ACCOUNT_SID, "
            "ODDS_TWILIO_AUTH_TOKEN, ODDS_TWILIO_FROM_NUMBER "
            "(and ODDS_ALERT_TO if needed)"
        )
    url = _TWILIO_URL.format(sid=sid)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0)
    try:
        response = client.post(
            url,
            data={"To": dest, "From": from_number, "Body": body},
            auth=(sid, token),
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("sid") or "")
    finally:
        if owns_client:
            client.close()


@dataclass
class AlertBook:
    """Tracks which opportunities already texted in this process."""

    sent_keys: set[str] = field(default_factory=set)
    send: Callable[[str], str] = field(default=send_sms)

    def notify(
        self,
        opportunities: Sequence[Opportunity],
        *,
        min_roi: float | None = None,
    ) -> list[Opportunity]:
        """Text each new qualifying opportunity.  Returns those that were sent."""
        if not alert_ready():
            log.debug(
                "SMS alerts skipped — Twilio not configured "
                "(set ODDS_TWILIO_ACCOUNT_SID / AUTH_TOKEN / FROM_NUMBER)"
            )
            return []

        sent: list[Opportunity] = []
        for opportunity in opportunities:
            if not qualifies(opportunity, min_roi=min_roi):
                continue
            key = opportunity_alert_key(opportunity)
            if key in self.sent_keys:
                continue
            body = format_alert(opportunity)
            try:
                sid = self.send(body)
            except Exception:  # noqa: BLE001 — watch loop must not die on SMS
                log.exception(
                    "failed to send arb SMS for %s (roi %.1f%%)",
                    opportunity.event_key,
                    opportunity.roi * 100.0,
                )
                continue
            self.sent_keys.add(key)
            sent.append(opportunity)
            log.info(
                "sent arb SMS sid=%s roi=%.1f%% %s",
                sid or "?",
                opportunity.roi * 100.0,
                opportunity.event_key,
            )
        return sent


#: Process-wide book so ``--watch`` does not re-text the same arb every pass.
DEFAULT_BOOK = AlertBook()


def notify_opportunities(
    opportunities: Sequence[Opportunity],
    *,
    min_roi: float | None = None,
    book: AlertBook | None = None,
) -> list[Opportunity]:
    """Entry point used by the collector and ``arb`` command."""
    return (book or DEFAULT_BOOK).notify(opportunities, min_roi=min_roi)
