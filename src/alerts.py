"""SMS alerts when a takeable arbitrage clears the configured floor.

Opt-in and fail-soft: with no Twilio credentials the collector keeps running and
nothing is sent.  Credentials live in ``ODDS_TWILIO_*`` env vars — never in the
repo.  The default destination is the number configured in :mod:`src.settings`.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import httpx

from src import settings
from src.arb import Opportunity
from src.coverage import LocalityMarking
from src.betlinks import Precision, bet_link
from src.vocab import Market, Selection

log = logging.getLogger("alerts")

#: Twilio Messages endpoint template.
_TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

#: Soft cap so a bizarre multi-leg message still fits a few concatenated SMS.
_MAX_SMS_CHARS = 1400


def twilio_ready() -> bool:
    """True when Twilio credentials and a destination number are configured."""
    return bool(
        settings.TWILIO_ACCOUNT_SID
        and settings.TWILIO_AUTH_TOKEN
        and settings.TWILIO_FROM_NUMBER
        and settings.ALERT_TO
    )


def messages_ready() -> bool:
    """True when the local Messages app could plausibly deliver.

    Only checks what is knowable without sending: this is a Mac, ``osascript``
    exists, and there is a destination.  Whether Automation permission has been
    granted is not knowable in advance — the first send is what asks, and
    :func:`send_via_messages` turns that refusal into an actionable error.
    """
    return bool(
        sys.platform == "darwin"
        and shutil.which("osascript")
        and settings.ALERT_TO
    )


def alert_ready() -> bool:
    """True when the configured transport can deliver."""
    if settings.ALERT_TRANSPORT == "twilio":
        return twilio_ready()
    return messages_ready()


def unready_reason() -> str:
    """Why alerts are off, phrased as the thing to go fix."""
    if settings.ALERT_TRANSPORT == "twilio":
        missing = [
            name
            for name, value in (
                ("ODDS_TWILIO_ACCOUNT_SID", settings.TWILIO_ACCOUNT_SID),
                ("ODDS_TWILIO_AUTH_TOKEN", settings.TWILIO_AUTH_TOKEN),
                ("ODDS_TWILIO_FROM_NUMBER", settings.TWILIO_FROM_NUMBER),
                ("ODDS_ALERT_TO", settings.ALERT_TO),
            )
            if not value
        ]
        return f"twilio transport is missing {', '.join(missing)}"
    if sys.platform != "darwin":
        return (
            f"the messages transport needs macOS (this is {sys.platform}); "
            "set ODDS_ALERT_TRANSPORT=twilio to send over HTTP instead"
        )
    if not shutil.which("osascript"):
        return "the messages transport needs osascript, which is not on PATH"
    if not settings.ALERT_TO:
        return "no destination number — set ODDS_ALERT_TO"
    return ""


def opportunity_alert_key(opportunity: Opportunity) -> str:
    """Stable id for dedupe: same books, selections, and prices → one text.

    Stakes are deliberately NOT part of the key.  They are an output of
    whatever bankroll the command was asked to size for, not part of the
    arb's identity — with them in the key, ``arb --run N --stake 200``
    re-minted every already-claimed position under a fresh key and the
    ledger waved it through, quietly scoping "at most one text per arb
    ever" to "per arb per bankroll".  Keys already claimed under the old
    stake-bearing spelling stay claimed but can never match again; a
    position still live across that upgrade may text once more, which is
    the acceptable direction of the error.
    """
    legs = "|".join(
        f"{leg.source}:{leg.selection.value}:{leg.decimal_odds:.4f}"
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


def format_alert(
    opportunity: Opportunity, *, marking: LocalityMarking | None = None
) -> str:
    """Compact SMS body with enough to place both legs by hand.

    With a governing *marking*, any leg unreachable from the run's state carries
    the same ``[not reachable from ST]`` tag the CLI prints, and a disclaimer
    line sits in the head — the head rather than the tail, because the body is
    truncated from the end and a warning that can be cut off is not a warning.
    """
    roi_pct = opportunity.roi * 100.0
    line = "" if opportunity.line is None else f" @ {opportunity.line:g}"
    side = f" ({opportunity.side.value})" if opportunity.side else ""
    kickoff = opportunity.commence_time.strftime("%Y-%m-%d %H:%M %Z").strip() or (
        opportunity.commence_time.strftime("%Y-%m-%d %H:%M UTC")
    )
    disclaimer = ""
    if marking is not None and marking.marking:
        foreign = marking.non_local_sources(opportunity)
        if foreign and not marking.has_local_leg(opportunity):
            disclaimer = (
                f"\nNO LEG reachable from {marking.state} — informational, "
                f"not {marking.state} prices"
            )
        elif foreign:
            disclaimer = (
                f"\nNON-LOCAL: {', '.join(foreign)} not reachable from "
                f"{marking.state} — not {marking.state} prices"
            )
    head = (
        f"ARB {roi_pct:.1f}% | "
        f"+${opportunity.guaranteed_profit:.2f} on ${opportunity.total_stake:.0f}"
        f"{disclaimer}\n"
        f"{opportunity.away_team} @ {opportunity.home_team}\n"
        f"{opportunity.sport.value} {opportunity.market.value}/"
        f"{opportunity.period.value}{side}{line}"
    )
    legs = []
    for i, leg in enumerate(opportunity.legs, start=1):
        label = _selection_label(opportunity, leg)
        tag = (
            ""
            if marking is None or marking.leg_is_local(leg.source)
            else f" [{marking.label()}]"
        )
        line = (
            f"{i}) {leg.source} {label} "
            f"{leg.quote.american_odds:+d} (${leg.decimal_odds:.3f}) "
            f"stake ${leg.stake:.2f}{tag}"
        )
        # The link is the point of the text: a 3% edge is only takeable if both
        # slips are one tap away.  An event link goes bare; a league page says so,
        # because sending someone to an index and calling it the bet wastes the
        # seconds the edge is made of.  The governed state rides along so a
        # state-partitioned book links its *own* door — a PA text carried
        # il.betrivers.com until it did.
        link = bet_link(
            leg.quote, state=(marking.state or None) if marking is not None else None
        )
        if link is not None:
            if link.precision is Precision.EVENT:
                line += f"\n   {link.url}"
            else:
                where = "league page" if link.precision is Precision.LEAGUE else "site"
                mirror = f", price via {leg.source}" if link.mirrored else ""
                line += f"\n   {link.book} {where}{mirror}: {link.url}"
        legs.append(line)
    cap = ""
    if opportunity.max_total_stake is not None:
        cap = f"\nMax stake ~${opportunity.max_total_stake:.0f}"
    notes = "".join(f"\nNote: {n}" for n in opportunity.notes[:2])
    # The call to action must not contradict the disclaimer: a message whose
    # head says "informational, not PA prices" cannot end by instructing the
    # reader to place both legs now.
    takeable = marking is None or marking.has_local_leg(opportunity)
    action = (
        "\nPLACE BOTH NOW — prices move"
        if takeable
        else f"\nINFO ONLY — not takeable from {marking.state}"
    )
    body = (
        f"{head}\n"
        + "\n".join(legs)
        + f"\nKickoff {kickoff}"
        + cap
        + notes
        + action
    )
    if len(body) > _MAX_SMS_CHARS:
        body = body[: _MAX_SMS_CHARS - 1] + "…"
    return body


#: AppleScript for one outgoing message.
#:
#: The destination and the body arrive as ``argv``, never interpolated into the
#: script text.  That is not tidiness: a team name with a double quote in it, or
#: any promo copy that reached a note, would otherwise terminate the AppleScript
#: string and the remainder would be *executed*.  ``osascript -`` reads the
#: program from stdin and hands everything after it to ``on run``, so no value
#: from the odds feed is ever parsed as code.
#:
#: The service is chosen first and ``send`` is then called **exactly once**.
#:
#: An earlier version sent through iMessage inside a ``try`` and, on error,
#: looped over every remaining service sending again — meant as SMS-relay
#: fallback for a number iMessage does not know.  It delivers twice: the
#: scripting bridge can report a failure for a ``send`` that already went out,
#: and ``services`` includes the iMessage service the first attempt just used, so
#: the retry re-sends the same message to the same person.  There is no way to
#: ask Messages whether the first one landed, so the only safe number of sends is
#: one.
_MESSAGES_SCRIPT = """
on run argv
  set dest to item 1 of argv
  set msg to item 2 of argv
  tell application "Messages"
    set svc to missing value
    try
      set svc to 1st service whose service type = iMessage
    end try
    if svc is missing value then
      if (count of services) is 0 then
        error "Messages has no configured service to send from"
      end if
      set svc to item 1 of services
    end if
    send msg to buddy dest of svc
  end tell
  return "sent"
end run
"""


def send_via_messages(body: str, *, to: str | None = None, timeout: float = 30.0) -> str:
    """Send one message through the local Messages app.  Returns the route used.

    Raises ``RuntimeError`` with the remedy when macOS refuses — an ungranted
    Automation permission is the common first failure and its raw AppleScript
    error says nothing a reader could act on.
    """
    dest = to or settings.ALERT_TO
    if not dest:
        raise RuntimeError("no destination number — set ODDS_ALERT_TO")
    if sys.platform != "darwin":
        raise RuntimeError(
            f"the messages transport needs macOS (this is {sys.platform}); "
            "set ODDS_ALERT_TRANSPORT=twilio to send over HTTP instead"
        )
    try:
        completed = subprocess.run(
            ["osascript", "-", dest, body],
            input=_MESSAGES_SCRIPT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("osascript is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Messages did not answer within {timeout:g}s — it may be showing a "
            "permission prompt; approve it once and the next send is immediate"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if "-1743" in detail or "not allowed" in detail.lower():
            raise RuntimeError(
                "macOS denied Automation access to Messages. Grant it in System "
                "Settings → Privacy & Security → Automation, for the terminal or "
                f"app running this. Raw error: {detail[:160]}"
            )
        raise RuntimeError(f"osascript failed: {detail[:200]}")
    return (completed.stdout or "").strip() or "sent"


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


def send_alert(body: str, *, to: str | None = None) -> str:
    """Deliver one alert over the configured transport."""
    if settings.ALERT_TRANSPORT == "twilio":
        return send_sms(body, to=to)
    return send_via_messages(body, to=to)


@dataclass
class AlertBook:
    """Tracks which opportunities already texted, in this process and before it.

    ``sent_keys`` is the fast in-process half.  ``path`` is the durable half: a
    one-table sqlite ledger shared by every process, because "text once" with a
    per-process memory was a hazard, not a policy — every fresh ``arb --run N``
    invocation re-texted a stored run's stale opportunities, demonstrated when
    an offline review probe of run 32 delivered a real SMS.  ``None`` keeps the
    book purely in-memory, which is what tests construct.
    """

    sent_keys: set[str] = field(default_factory=set)
    send: Callable[[str], str] = field(default=send_alert)
    path: Path | None = None

    def _claim(self, key: str) -> bool:
        """Record *key* as texted; ``False`` if any process already had.

        ``INSERT OR IGNORE`` under sqlite's own locking, so two processes
        racing on one key resolve to exactly one sender.  A ledger that cannot
        be opened or written falls back to the in-process set with one log
        line: the failure mode of a broken disk should be at worst the old
        behaviour, never a crashed watch loop and never a silent no-alert.

        The in-memory half is check-then-add and is **not** thread-safe: two
        threads sharing one path-less book can both claim a key.  No caller
        threads today — the collector is single-threaded around ``notify`` and
        the default book is path-backed, where sqlite arbitrates — so this is
        a documented boundary, not a latent race in shipping code.
        """
        if key in self.sent_keys:
            return False
        self.sent_keys.add(key)
        if self.path is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # ``connect`` as a context manager only ends the transaction;
            # ``closing`` is what actually closes the handle.
            with closing(sqlite3.connect(self.path, timeout=10.0)) as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS sent_alert ("
                        "key TEXT PRIMARY KEY, sent_at TEXT NOT NULL)"
                    )
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO sent_alert (key, sent_at) VALUES (?, ?)",
                        (key, datetime.now(timezone.utc).isoformat()),
                    ).rowcount
            return inserted == 1
        except (OSError, sqlite3.Error):
            log.exception(
                "alert ledger %s unavailable; dedupe is in-process only for %s",
                self.path,
                key,
            )
            return True

    def notify(
        self,
        opportunities: Sequence[Opportunity],
        *,
        min_roi: float | None = None,
        marking: LocalityMarking | None = None,
    ) -> list[Opportunity]:
        """Text each new qualifying opportunity.  Returns those that were sent."""
        if not alert_ready():
            log.debug("arb alerts skipped — %s", unready_reason())
            return []

        sent: list[Opportunity] = []
        # The one clock on the alert path, deliberately here rather than in
        # any caller: ``arb --run N`` reads an old run as history (the
        # MAX_PRICE_AGE gate is explicitly bypassed) and prints "a
        # historical study, not positions anyone can take" — then handed
        # the same opportunities to this method, which texted "PLACE BOTH
        # NOW — prices move" about games that had settled days earlier.
        # Every caller that can reach a phone goes through here, so the
        # fixture-has-started judgement lives here and no caller can
        # forget it.
        now = datetime.now(timezone.utc)
        for opportunity in opportunities:
            if opportunity.commence_time <= now:
                log.info(
                    "arb alert suppressed for %s: kickoff %s has passed — a "
                    "call to action about a started or settled fixture is "
                    "never actionable",
                    opportunity.event_key,
                    opportunity.commence_time.isoformat(),
                )
                continue
            if not qualifies(opportunity, min_roi=min_roi):
                continue
            key = opportunity_alert_key(opportunity)
            body = format_alert(opportunity, marking=marking)
            # Claimed *before* the send, so a delivery that reports failure
            # cannot be retried into a second text.  ``collect_batch_once`` is
            # why this matters rather than being theoretical: its per-state
            # passes re-analyze the *same cached* global payloads, so an arb
            # between two global books is offered to this book once per state
            # with a byte-identical key (the GLOBAL pass itself no longer
            # alerts when state passes follow — its ungoverned marking would
            # claim the key with an unlabelled body).  Recording only on
            # success meant an ambiguous first send left the key unclaimed and
            # the next pass sent it again.
            #
            # The trade is deliberate: at most one text per arb ever, even when
            # that means a genuinely failed send is not retried.  A missed
            # alert is visible in the log; a duplicate at 3am is not
            # recoverable and is what the dedupe book exists to prevent.
            if not self._claim(key):
                continue
            try:
                sid = self.send(body)
            except Exception:  # noqa: BLE001 — watch loop must not die on SMS
                log.exception(
                    "failed to send arb alert for %s (roi %.1f%%); not retried, "
                    "because a send that fails after delivering would double-text",
                    opportunity.event_key,
                    opportunity.roi * 100.0,
                )
                continue
            sent.append(opportunity)
            log.info(
                "sent arb SMS sid=%s roi=%.1f%% %s",
                sid or "?",
                opportunity.roi * 100.0,
                opportunity.event_key,
            )
        return sent


#: Process-wide book, backed by the on-disk ledger so ``--watch`` does not
#: re-text the same arb every pass and a *second process* does not either.
DEFAULT_BOOK = AlertBook(path=settings.ALERT_BOOK_PATH)


def notify_opportunities(
    opportunities: Sequence[Opportunity],
    *,
    min_roi: float | None = None,
    book: AlertBook | None = None,
    marking: LocalityMarking | None = None,
) -> list[Opportunity]:
    """Entry point used by the collector and ``arb`` command."""
    return (book or DEFAULT_BOOK).notify(
        opportunities, min_roi=min_roi, marking=marking
    )
