#!/usr/bin/env python3
"""Ask each book whether its candidate event-URL grammar actually resolves.

A **script**, deliberately not a test, for the same reason as
``probe_sources.py``: what it measures is a fact about the live web on the day it
runs, and it changes when a book redesigns.  Its output is what sets
``verified=True`` on the entries in :data:`src.betlinks.EVENT_URL`; nothing else
may.

An event link is only counted as resolving when the page comes back **and names
the event**.  A 200 alone proves nothing here — every book in the list answers
its own 404 with a styled 200 page, and several redirect an unknown id to the
league index, which looks identical to success from the status code.  So the
check is: did both team names (or their last words, for books that abbreviate)
survive into the body?

Event ids come from a stored run rather than a live fetch, and only for games
that have **not started yet** — an id for a finished game can 404 for a reason
that has nothing to do with the grammar being wrong, and that ambiguity is the
whole thing this script exists to remove.

**A state-partitioned book is probed against one licence and proves one.**  Its
host comes from the stored run's own jurisdiction, so a CONFIRMED verdict earns
``verified_states={that state}`` and nothing wider; an ungoverned run skips
those books rather than probing whichever host happened to be written down.

    python scripts/verify_betlinks.py                    # every candidate, latest run
    python scripts/verify_betlinks.py --source fanduel   # one book
    python scripts/verify_betlinks.py --per-book 5       # more samples each
    python scripts/verify_betlinks.py --verbose          # show the URLs tried
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings
from src.betlinks import EVENT_URL, MIRROR_BOOK, STATE_SITE, state_host, team_slug
from src.sources._common import USER_AGENT

TIMEOUT = 25.0


@dataclass(frozen=True)
class Sample:
    source: str
    event_id: str
    league: str
    away_team: str
    home_team: str
    commence_time: str
    state: str | None = None
    """The jurisdiction that governed the run this row came from.

    Carried because a state-partitioned book's grammar is only proven for the
    licence it was probed against: ``il.betrivers.com`` answering says nothing
    about ``pa.betrivers.com``, which is why :attr:`src.betlinks._Event.
    verified_states` is a set of states rather than one flag.
    """


def samples(db_path: Path, *, per_book: int, only: str | None) -> list[Sample]:
    """Future-dated events from the most recent run that has any."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("select max(run_id) from quote").fetchone()
        run_id = row[0] if row else None
        if run_id is None:
            return []
        now = datetime.now(UTC).isoformat()
        # The run's own jurisdiction, which is what a state-partitioned book's
        # verdict is *about*.  A GLOBAL run governs nothing, so it leaves this
        # None and those books are skipped rather than probed against a state
        # nobody chose.
        governed = con.execute(
            "select jurisdiction from collection_run where id = ?", (run_id,)
        ).fetchone()
        state = (governed["jurisdiction"] or "").strip().upper() if governed else ""
        if state in {"", "GLOBAL"}:
            state = None
        wanted = [key for key in EVENT_URL if key not in MIRROR_BOOK]
        if only:
            wanted = [key for key in wanted if key == only]
        out: list[Sample] = []
        for source in wanted:
            rows = con.execute(
                "select source, source_event_id, league, away_team, home_team,"
                "       min(commence_time) as ct "
                "from quote where run_id = ? and source = ? and commence_time > ? "
                "group by source_event_id order by ct limit ?",
                (run_id, source, now, per_book),
            ).fetchall()
            for r in rows:
                out.append(
                    Sample(
                        r["source"], r["source_event_id"], r["league"],
                        r["away_team"], r["home_team"], r["ct"], state,
                    )
                )
        return out
    finally:
        con.close()


def _client():
    """Chrome-impersonating client when available, plain httpx otherwise."""
    try:
        from src.sources.transport import build_default_client

        return build_default_client(timeout=TIMEOUT), "impersonated"
    except Exception:  # noqa: BLE001 — the plain baseline is still informative
        import httpx

        return (
            httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ),
            "plain httpx",
        )


_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META = re.compile(
    r'<meta[^>]+(?:property|name)="(?:og:title|og:description|description|twitter:title)"'
    r'[^>]+content="([^"]*)"',
    re.I,
)


def names_event(body: str, sample: Sample) -> bool:
    """Did the page actually come back describing *this* game?

    Checks the server-rendered head as well as the visible text: a
    client-rendered book leaves the body empty on first byte but still fills
    ``<title>`` and the Open Graph tags, which is the only part of an SPA that
    can confirm the id resolved to the right event.
    """
    head = " ".join(_TITLE.findall(body or "") + _META.findall(body or ""))
    text = _TAG.sub(" ", body or "").lower() + " " + head.lower()
    for team in (sample.away_team, sample.home_team):
        full = team.lower()
        # "Los Angeles Rams" may render as "LA Rams" or "Rams"; the last word is
        # the nickname and is the part every book keeps.
        nickname = full.split()[-1] if full.split() else full
        if full not in text and nickname not in text:
            return False
    return True


def _fingerprint(body: str) -> tuple[str, int]:
    """What a page is, coarsely — its title and rough size.

    Used to compare a real id against a deliberately invalid one.  When a book
    serves byte-identical shells for both, its HTML cannot answer the question
    and saying "FAIL" would be a guess dressed as a measurement.
    """
    titles = _TITLE.findall(body or "")
    title = _TAG.sub(" ", titles[0]).strip().lower() if titles else ""
    return title, round(len(body or "") / 2048)


def _corrupt(event_id: str) -> str:
    """An id of the same shape that cannot exist, for the control probe."""
    digits = [c for c in event_id if c.isdigit()]
    if digits:
        # Prefix a 9 so length and character class survive but the value cannot
        # be a live event; books mint ids far below this.
        return "9" + event_id[1:] if event_id[0].isdigit() else event_id + "9"
    return event_id + "ZZ"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/verify_betlinks.py",
        description="Probe candidate event-URL grammars against the live sites.",
    )
    parser.add_argument("--source", help="only this book key")
    parser.add_argument("--per-book", type=int, default=3, help="samples per book")
    parser.add_argument("--verbose", action="store_true", help="print each URL tried")
    args = parser.parse_args(argv)

    rows = samples(settings.DB_PATH, per_book=args.per_book, only=args.source)
    if not rows:
        print(
            "no future-dated events in the stored runs — collect once first, or "
            "the stored slate has already started"
        )
        return 1

    client, transport = _client()
    print(f"transport: {transport}\n")
    verdicts: dict[str, list[tuple[bool, int, str]]] = {}
    skipped: set[str] = set()
    try:
        for sample in rows:
            candidate = EVENT_URL[sample.source]
            host = ""
            if sample.source in STATE_SITE:
                # Which licence's board this grammar is being asked about.  With
                # no governing state there is no answer, and probing some other
                # state's host would file its result under this book as though
                # it meant something here.
                host = state_host(sample.source, sample.state or "") or ""
                if not host:
                    why = (
                        "run governs no state — collect with --state first"
                        if not sample.state
                        else f"{sample.source} has no door for {sample.state}, "
                        "so there is no host to probe"
                    )
                    print(
                        f"  [SKIP] {sample.source:16} {sample.event_id:16} "
                        f"state-partitioned book: {why}"
                    )
                    skipped.add(sample.source)
                    continue
            url = candidate.build(
                event_id=sample.event_id,
                slug=team_slug(sample.away_team, sample.home_team),
                host=host,
            )
            status = 0
            verdict = "FAIL"
            detail = ""
            try:
                response = client.get(url)
                status = int(response.status_code)
                body = response.text or ""
                final = str(getattr(response, "url", "") or "")
                if status in (403, 429) or "/block" in final:
                    verdict = "BLOK"
                    detail = f"HTTP {status} / geo or rate block — grammar untested"
                elif status >= 400:
                    detail = f"HTTP {status}"
                elif names_event(body, sample):
                    verdict = "ok"
                    detail = f"names {sample.away_team} @ {sample.home_team}"
                else:
                    # The page came back but did not name the game.  Ask a
                    # deliberately invalid id the same question: if the book
                    # answers identically, its HTML cannot tell us anything.
                    control_url = candidate.build(
                        event_id=_corrupt(sample.event_id),
                        slug=team_slug(sample.away_team, sample.home_team),
                        host=host,
                    )
                    try:
                        control = client.get(control_url)
                        same = _fingerprint(control.text or "") == _fingerprint(body)
                        control_status = int(control.status_code)
                    except Exception:  # noqa: BLE001
                        same, control_status = False, 0
                    if same and control_status == status:
                        verdict = "????"
                        detail = (
                            "client-rendered: a bogus id returns the same shell, "
                            "so the HTML cannot confirm or deny the grammar"
                        )
                    else:
                        detail = "200 but the page does not name the game" + (
                            f" (landed on {final[:56]})" if final and final != url else ""
                        )
            except Exception as exc:  # noqa: BLE001 — a refusal is a result
                detail = f"{type(exc).__name__}: {str(exc)[:70]}"
            verdicts.setdefault(sample.source, []).append((verdict, status, detail))
            print(f"  [{verdict:4}] {sample.source:16} {sample.event_id:16} {detail}")
            if args.verbose:
                print(f"         {url}")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    governed = next((s.state for s in rows if s.state), None)
    print("\n" + "=" * 72)
    print("verdict per book — record a CONFIRMED in src/betlinks.py, nothing less")
    print(
        "  a book served from one domain everywhere:  verified=True\n"
        "  a book in STATE_SITE (per-licence hosts):  verified_states={...}\n"
        "  — the flat flag on a state-partitioned book is refused at import"
    )
    if governed:
        print(
            f"probed from the {governed} run.  A state-partitioned book earns "
            f"verified_states={{'{governed}'}} — that state and no other; its "
            "other licences are separate hosts and separate evidence."
        )
    print("=" * 72)
    exit_code = 0
    for source in sorted(verdicts):
        results = verdicts[source]
        good = sum(1 for v, _, _ in results if v == "ok")
        unknown = sum(1 for v, _, _ in results if v in ("????", "BLOK"))
        if good == len(results):
            outcome, note = "CONFIRMED", "every sample resolved and named its game"
            if source in STATE_SITE:
                note += f" (in {governed} only)"
        elif unknown == len(results):
            outcome, note = "UNKNOWN", "the site never answers in HTML — cannot verify"
            exit_code = 2
        elif good:
            outcome, note = "PARTIAL", f"{good}/{len(results)} resolved"
            exit_code = 2
        else:
            outcome, note = "WRONG", "no sample resolved"
            exit_code = 2
        print(f"  {source:18} {outcome:9} {note}")
    for source in sorted(skipped - set(verdicts)):
        print(f"  {source:18} {'SKIPPED':9} never probed — see the SKIP lines above")
    if not verdicts:
        # An empty table is not a clean bill of health, and printing nothing
        # under a "verdict per book" header reads exactly like one.  This is
        # reachable today: with a GLOBAL run stored, every state-partitioned
        # book skips and no other book need have future-dated rows.
        print("\n  nothing was probed — no verdict was reached for any book")
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
