"""bet365 pregame markets, from the state host's pull-pod API.

``https://www.{state}.bet365.com/pullpodapi/gethomepagepods`` and
``…/gethomepageadditionalpods`` answer a compact delimited board over **plain
HTTP** — no browser, no cookie, no token.  Measured 2026-08-15 from Illinois
egress: the repository's ordinary client and a warm real Chrome returned
byte-identical 88,298-byte bodies (254 prices), so nothing here needs a browser.
Written up in ``docs/evidence/state-routing.md`` § bet365 (2026-08-15).

The wire format, decoded from 4,169 captured prices:

* ``\\x08`` separates frames, ``|`` separates records, ``;`` separates fields,
  and each field is ``KEY=value``.  A frame's first record is a type marker —
  ``F`` for a full snapshot.  Keys repeat within one record (``XD`` twice), and
  first-wins is what reproduces the rendered page.
* A record's type is its leading token: ``PS PD XL CL CS CO EV MG MA PA``.

**The coupon is column-oriented, not nested.**  There are no parent ids joining a
selection to its event.  A market group (``MG;SY=cpmg``) is followed by a fixture
column (``MA;SY=cpma``) and then one market column per type
(``MA;SY=cpce;NA=Spread|Total|Money``), and a coupon ``PA`` carries ``PZ`` — its
row index **within that market group**, which restarts at 0 in the next group
(hence the reset in ``_collect_pod``) and is empty on the companion row named in
the fifth trap below.  **``PZ`` is the join key**, and the two ``PA`` under one
``PZ`` in a market column are the two sides, the first belonging to ``NA``.

Six traps, each of which produces plausible wrong rows rather than an error:

* **Row order does not state the host, and the ordering is not constant.**
  ``FD`` states it, in its separator: the US boards read ``"<away> @ <home>"``
  and soccer reads ``"<home> v <away>"``.  Assuming one everywhere inverts every
  fixture of the other — rows that parse, validate and price the wrong team.
  There is a **third** separator, ``" vs "``, and it means something neither of
  the others does: two names and *no host*, which is tennis.  Reading it as an
  absent ``FD`` declined the whole shelf under a reason whose count matched
  exactly, so the wrong diagnosis looked like the right one.
* **A group header names one competition and can be wrong about half its rows.**
  A combined tournament is shelved under a single tag — the captured Cincinnati
  coupon is headed ``WTA1-R2`` with men's rows carrying ``ATP1-R2`` — so the
  league comes from each *price* row's own ``L3``, not from the header.
* ``OI`` looks like an event id and is not one — it changes at kickoff, when a
  fixture acquires a separate in-play id.  ``FI`` on a market column is per
  *market*, not per event.  Identity comes from the ``E<digits>`` group of the
  fixture row's ``PD`` navigation token.
* ``HA`` is signed per selection on a spread but carries **no side at all** on a
  total — there, over/under lives only in the ``HD`` display string's prefix.
* The fixture column emits a companion ``PA`` with an empty ``PZ`` after each
  real row.  Counting it shifts every subsequent join by one.

Prices are **fractional** (``OD=10/13``), which is why
:func:`src.normalize.fractional_to_decimal` exists — see its note on the
neighbouring helper that divides without the ``1 +``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

import httpx

from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import league as league_by_key
from src.normalize import (
    decimal_to_american,
    fractional_to_decimal,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import canonical_participant
from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection
from src.vocab import draw_is_priced
from src.sources._common import (
    Fixture,
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    priced_quote,
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import GeoRestrictedError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "bet365"

#: No default host, deliberately.  Hard Rock's default names New Jersey, and an
#: adapter that silently falls back to one state's edge is exactly what the
#: state check below exists to catch.  ``src.jurisdictions`` routes this per
#: state; an unrouted build refuses rather than pricing another licence.
DEFAULT_BASE_URL = ""

HOST_INTERVAL = 0.6

#: The query the application itself sends, captured verbatim.  ``csid`` is the
#: licence scope — the app's own ``USStateID`` table reads 28 IL, 56 PA, 3 NJ,
#: 20 DC — and ``lid`` is the language, not a region.  These travel together
#: with the state host; neither alone routes a price.
BASE_PARAMS: dict[str, str] = {
    "lid": "32",
    "zid": "0",
    "cid": "198",
    "cstid": "1",
    "tcstid": "1",
    "crid": "54",
    "cgid": "3",
    "ctid": "198",
}


@dataclass(frozen=True)
class _Pod:
    """One shelf: its stored label, its own path, and the token that selects it.

    The path is carried rather than derived.  It used to be chosen inline as
    ``"pods" if label == "homepage" else "additionalpods"``, which is correct for
    exactly the two entries that existed and silently routes every later one to
    ``gethomepageadditionalpods`` — a pod that answers, with the wrong shelf in
    it, under the label of the shelf that was asked for.

    *label* becomes part of the stored filename and of every row's ``raw_ref``
    via :func:`src.raw_store._slug`, which folds each run of non-alphanumerics to
    a single ``-``.  Two labels differing only in punctuation would therefore
    name one file and make a row's provenance ambiguous, so they are constrained
    to ``[a-z0-9-]`` and checked for collisions at import.
    """

    label: str
    path: str
    token: str


#: The pods that carry a two-team board.  Both are ``#HO#`` shelves: the
#: endpoint is bound to that token — ``#AS#`` and ``#AC#`` tokens against it
#: return 200 with an empty body, measured control-first on 2026-08-16.
PODS: tuple[_Pod, ...] = (
    _Pod("homepage", "/pullpodapi/gethomepagepods", "#HO#COL1#"),
    _Pod("additional", "/pullpodapi/gethomepageadditionalpods", "#HO#COL1#1#"),
)

#: Column header (``MA;SY=cpce;NA=``) to the market it prices.
MARKET_NAMES: dict[str, Market] = {
    "spread": Market.SPREAD,
    "total": Market.TOTAL,
    "money": Market.MONEYLINE,
}

#: Soccer's coupon prices a moneyline as three one-sided columns rather than one
#: two-sided ``Money`` column, and names the side in the column header instead of
#: implying it from row order.  Each column carries exactly one ``PA`` per row and
#: no ``HA``.
#:
#: These labels are the *book's* sides, and they are the only statement of which
#: is which — row order is not, and on soccer it is the reverse of the US boards
#: (see ``_accept_fixture``).  They go through ``Fixture.our_side`` like any
#: other stated side rather than being trusted to mean ours.
#:
#: Tennis joins the same shape from the other direction: with no host to name,
#: its coupon numbers the two competitors instead.  ``1`` is the one the fixture
#: row lists first (``NA``) and ``2`` the second (``N2``) — again the *book's*
#: ordering, which :func:`src.events.orient` disagrees with about half the time
#: because it sorts a hostless league by participant key.
_STATED_SIDE_COLUMNS: dict[str, Selection] = {
    "home": Selection.HOME,
    "tie": Selection.DRAW,
    "draw": Selection.DRAW,
    "away": Selection.AWAY,
    "1": Selection.HOME,
    "2": Selection.AWAY,
}

#: ``L3`` league tag to this repository's canonical key.  bet365 publishes
#: preseason under its own tag; it is the same competition to us, and event
#: identity is participants plus date either way.
LEAGUE_TAGS: dict[str, str] = {
    "MLB": "MLB",
    "NFL": "NFL",
    "NFL-EXHIBITION": "NFL",
    "NBA": "NBA",
    "WNBA": "WNBA",
    "NHL": "NHL",
    "MLS": "MLS",
    # Tennis tags carry a tour-and-round suffix (``ATP1-R2``) that _league_key
    # strips.  The tour is the competition; the round is not.
    "ATP": "ATP",
    "WTA": "WTA",
}

#: Capabilities are what this venue's coupon actually carries, per league.  The
#: US boards are three columns — Spread / Total / Money — while soccer's is
#: Home / Tie / Away and carries no handicap or total at all.  Declaring the US
#: shape for soccer made validation report ``core_market_absent`` on every run:
#: a market that never arrives is indistinguishable from a label that changed,
#: which is exactly the alarm that check exists to raise.
MARKETS_BY_LEAGUE: dict[str, frozenset[Market]] = {
    **{
        key: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL})
        for key in ("MLB", "NFL", "NBA", "WNBA", "NHL")
    },
    "MLS": frozenset({Market.MONEYLINE}),
    # Tennis prices two numbered columns and nothing else — no handicap, no
    # total.  Measured on the captured coupon, not assumed from the sport.
    **{key: frozenset({Market.MONEYLINE}) for key in ("ATP", "WTA")},
}

#: Renderer templates whose records are a two-team coupon.  Everything else in
#: these pods is deliberately out of scope: ``fho``/``fhh`` are futures,
#: ``pbd``/``pba``/``pbc`` parlay boosts, ``pmm``/``pom``/``pps`` player props.
_FIXTURE_COLUMN = "cpma"
_MARKET_COLUMN = "cpce"
_MARKET_GROUP = "cpmg"

_LABEL = re.compile(r"^[a-z0-9-]+$")
if len({_pod.label for _pod in PODS}) != len(PODS) or not all(
    _LABEL.match(_pod.label) for _pod in PODS
):  # pragma: no cover - import-time invariant
    raise AssertionError("bet365 pod labels must be distinct and [a-z0-9-]")

_EVENT_ID = re.compile(r"#E(\d+)#")
#: A tennis ``L3`` head, before its tour number and round: ``ATP1-R2`` -> ``ATP``.
_TOUR_TAG = re.compile(r"^([A-Z]+)\d")
_FRAME = "\x08"

#: The shell's ``SST`` config value, which is the one token-shaped string in
#: anything this adapter fetches.  Whether it is per-deploy or per-session was
#: not established, and a capture is committed to the repository — so it is
#: redacted before the body is ever constructed into a ``RawResponse``, which
#: is the only point at which that is possible without invalidating the
#: envelope's own hash.
_SITE_TOKEN = re.compile(r'("SST"\s*:\s*")[^"]+(")')


def redact_site_token(body: str) -> str:
    """Blank the shell's ``SST`` value, and nothing else.

    Deliberately anchored to that one key.  A filter broad enough to catch
    "anything token-shaped" would also rewrite a block page or a challenge
    interstitial, and :func:`src.sources.guards.check_http_response` reads the
    filtered body — so a refusal would be laundered into an apparently good
    200.  The pods carry no token at all and are untouched.
    """
    return _SITE_TOKEN.sub(r"\1[redacted]\2", body)


# ── the wire format ──────────────────────────────────────────────────────────


def _fields(record: str) -> dict[str, str]:
    """One record's ``KEY=value`` pairs, first occurrence winning.

    Keys genuinely repeat — a coupon fixture row carries ``XD`` twice — and
    first-wins is what reproduces the page.  Last-wins silently preferred an
    empty second copy.
    """
    out: dict[str, str] = {}
    for part in record.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out.setdefault(key, value)
    return out


def _records(body: str) -> Iterable[tuple[str, dict[str, str]]]:
    """Every record in every frame, as ``(type, fields)``."""
    for frame in body.split(_FRAME):
        for record in frame.split("|"):
            record = record.strip()
            if not record:
                continue
            kind = record.split(";", 1)[0]
            yield kind, _fields(record)


def _start_time(value: str) -> datetime | None:
    """``BC`` — ``YYYYMMDDHHMMSS``, UTC.

    Verified against the rendered page: ``20260815210500`` displays as 3:05 PM
    in the site's own Central timezone, which is 21:05 UTC.

    **This venue disagrees with the rest of the slate on some fixtures, and the
    disagreement is real rather than a parsing fault.**  On the 2026-08-15
    Illinois run, three of nine fixtures came back exactly **+60 minutes**
    against a consensus of thirty-two other feeds — and all three were at
    Pacific-timezone venues (Angels, Athletics, Las Vegas Aces) while the six
    that agreed were not.  The bytes say what this returns: bet365's own
    ``BC`` for KC @ LAA is ``20260816023800`` where every other book, including
    Action Network's view *of bet365*, says 01:38.

    Nothing is subtracted here.  An adapter that "corrects" a venue towards the
    consensus is inventing data and would hide the next real change; the
    cross-source clock check reports the disagreement as a warning, which is
    the surface that should own it.  Worth knowing if it ever matters for event
    identity: an hour can move a late game across
    :func:`src.events.scheduling_date`'s boundary, though on that run every
    affected fixture still keyed to the same day and reconciled normally.
    """
    if len(value) != 14 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _decimal(odds: str) -> float | None:
    """``OD`` — a true fraction of profit over stake, or nothing."""
    if "/" not in odds:
        return None
    numerator, _, denominator = odds.partition("/")
    try:
        value = fractional_to_decimal(float(numerator), float(denominator))
    except (TypeError, ValueError):
        return None
    return value if is_plausible_decimal_odds(value) else None


def _row_leagues(records: Sequence[tuple[str, Mapping[str, str]]]) -> list[dict[str, str]]:
    """Per coupon group, the ``L3`` each *row* is priced under.

    The group header names one competition and is not always right about every
    row beneath it.  bet365 shelves a combined tournament under a single tag:
    the captured Cincinnati coupon is headed ``WTA1-R2`` and half its rows are
    men's matches carrying ``ATP1-R2``.  Filing those under the header would
    put Tommy Paul in the WTA — a row that parses, validates, and prices a real
    match in the wrong competition.

    Every price row states its own ``L3``, which is the specific claim, so this
    indexes it by ``PZ`` for the fixture walk to prefer.  Where a header is
    right the two agree, which is every other group in the captured pods.
    """
    groups: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    style = ""
    for kind, fields in records:
        if kind == "MG" and fields.get("SY") == _MARKET_GROUP:
            current = {}
            groups.append(current)
            style = ""
        elif kind == "MA":
            style = fields.get("SY", "")
        elif kind == "PA" and style == _MARKET_COLUMN and current is not None:
            position, tag = fields.get("PZ", ""), fields.get("L3", "")
            if position and tag:
                current.setdefault(position, tag)
    return groups


def _leagues_present(body: str) -> set[str]:
    """Canonical league keys this body carries a two-team coupon for.

    Read from the group headers rather than from parsed rows, so a league whose
    every fixture is already in play still counts as *carried* — the shelf was
    there and had nothing pregame on it, which is a different fact from the
    shelf being absent.

    The rows' own tags are unioned in for the reason :func:`_row_leagues`
    gives: a header can name one of the two competitions on its shelf, and
    counting only headers reported the other as an empty shelf while it was
    publishing prices.
    """
    records = list(_records(body))
    present: set[str] = set()
    for group in _row_leagues(records):
        for tag in group.values():
            key = _league_key(tag)
            if key is not None:
                present.add(key)
    tag = ""
    for kind, fields in records:
        if kind in {"EV", "MG"} and fields.get("L3"):
            tag = fields["L3"]
        if kind == "MG" and fields.get("SY") == _MARKET_GROUP:
            key = _league_key(tag)
            if key is not None:
                present.add(key)
    return present


def _league_key(tag: str) -> str | None:
    """``L3`` to a canonical league key.

    ``L3`` is comma-separated on records that span competitions, so the first
    segment is the tag.  Tennis then appends a tour-and-round suffix to it —
    ``ATP1-R2``, ``WTA1-R2`` — where only the alphabetic head names a
    competition this repository knows; the digits are the tour's own numbering
    and the ``-R2`` is the round.

    The suffix is stripped only when the head is followed by a digit, which is
    what keeps ``NFL-EXHIBITION`` and ``MMA-UFC`` whole: both are hyphenated and
    neither has a digit to strip back to.
    """
    first = (tag or "").split(",")[0].strip().upper()
    if first not in LEAGUE_TAGS:
        tour = _TOUR_TAG.match(first)
        if tour is not None:
            first = tour.group(1)
    return LEAGUE_TAGS.get(first)


# ── the adapter ──────────────────────────────────────────────────────────────


class Bet365Adapter:
    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        csid: str = "",
        timeout: float = 25.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = (
            frozenset(key.upper() for key in leagues)
            if leagues is not None
            else frozenset(MARKETS_BY_LEAGUE)
        )
        unknown = sorted(wanted - set(MARKETS_BY_LEAGUE))
        if unknown:
            raise ValueError(f"Bet365Adapter has no competition for {unknown!r}")
        self._wanted = wanted
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.csid = csid
        self.routed_state = proxy_state
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
            proxy_state=proxy_state,
            body_filter=redact_site_token,
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(sorted(self._wanted))

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(MARKETS_BY_LEAGUE, self.leagues)

    def _params(self, token: str) -> dict[str, str]:
        params = dict(BASE_PARAMS)
        params["pd"] = token
        if self.csid:
            params["csid"] = self.csid
        return params

    def _require_expected_state(self, raw: RawResponse) -> None:
        """Refuse when the host reports a licence other than the routed one.

        The pull-pod bodies carry no state marker of any kind — this was
        searched for across every captured payload — so the check has to read
        the shell, which states its own licence as ``STATE_LOCALE:"USIL"``.
        That is the one fact no route configuration can assert for itself: the
        host names a state and the request carries an egress, and only the
        response says which licence actually answered.

        Permissive when either side is unknown, strict only on disagreement —
        an unrouted instance (replay, a contract test) must not raise here.
        """
        if self.routed_state is None:
            return
        found = re.search(r'"STATE_LOCALE"\s*:\s*"US([A-Z]{2})"', raw.body or "")
        if found is None:
            # An absent marker used to be permissive, on the reasoning that a
            # missing field is not a disagreement.  It is here: every shell this
            # host serves carries STATE_LOCALE, so a body without one is not a
            # shell — it is a redirect, a splash or an interstitial that cleared
            # check_http_response.  Being permissive there means a route the
            # operator cannot reach fetches a board anyway and labels it with
            # the state that was asked for rather than the one that answered.
            raise GeoRestrictedError(
                f"{self._source_key}:shell: routed to {self.routed_state.upper()} but "
                "the response carries no STATE_LOCALE, so no licence answered for it"
            )
        reported = found.group(1)
        if reported != self.routed_state.upper():
            raise GeoRestrictedError(
                f"{self._source_key}:shell: routed to {self.routed_state.upper()} but "
                f"the host answered {reported!r} — this egress is not in the routed state"
            )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Collect every configured pod.

        *tier* is unused: a pod arrives whole in one call, so there is no
        per-event follow-up to defer and both tiers collect identically.
        """
        del tier
        if not self.base_url:
            raise SourceError(
                f"{self._source_key}: no base_url — this adapter is routed per state "
                "by src.jurisdictions and has no default host to fall back to"
            )
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)

        # The shell is a prerequisite rather than a scope with a slate: it
        # answers from any egress, so counting it as produced would let
        # require_something pass on a run where every pod came back empty.
        shell = self._http.get(
            f"{self.base_url}/", endpoint="shell", expect_json=False
        )
        raws.append(shell)
        self._require_expected_state(shell)

        # Scopes are the configured *leagues*, not the pods.  Counting pods made
        # a league that this venue simply never carried indistinguishable from
        # one it stopped carrying: NHL is configured, appears in no pod, and
        # under a per-pod tally fired no counter at all — a silent zero inside a
        # run that reported healthy.
        for league in self.leagues:
            tally.requested(league)

        found: Counter[str] = Counter()
        failures: list[SourceError] = []
        for pod in PODS:
            try:
                raw = self._http.get(
                    f"{self.base_url}{pod.path}",
                    endpoint=f"pod-{pod.label}",
                    params=self._params(pod.token),
                    # The board is text/plain, not JSON.  See the module note in
                    # tests/test_bet365.py on what this does *not* disable.
                    expect_json=False,
                )
            except SourceError as exc:
                log.info("%s: pod %s unavailable: %s", self._source_key, pod.label, exc)
                failures.append(exc)
                continue
            raws.append(raw)
            found.update(_leagues_present(raw.body) & self._wanted)

        # Attribution waits for every pod, because a shelf carries whichever
        # leagues it happens to carry and a dead one could have held any of them
        # — but only until another pod answers for that league.  Marking every
        # league failed regardless counted leagues that *did* publish as
        # refused, and scopes_failed is the numerator _check_source_health
        # divides by scopes_requested: one dead pod of two graded the whole book
        # "collapsed" at ERROR while the surviving pod handed over a full slate.
        for league in self.leagues:
            if found[league]:
                tally.produced(league, found[league])
                if failures:
                    # It published, and a shelf that might have carried more of
                    # it died.  That is the third state — a note about a scope,
                    # deliberately outside the share arithmetic.
                    tally.truncated(league, failures[0])
            elif failures:
                tally.failed(league, failures[0])
            else:
                tally.produced(league, 0)

        tally.require_something(what="pregame bet365 event")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_bet365(raws)

    def close(self) -> None:
        self._http.close()


# ── parsing ──────────────────────────────────────────────────────────────────


def parse_bet365(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured pods into normalized rows.  Pure; no I/O, no clock."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    fixtures: dict[str, Fixture] = {}
    identity: dict[str, tuple[str, datetime]] = {}
    # (event id, raw, market, selection, decimal, line, market id, selection id)
    pending: list[
        tuple[str, RawResponse, Market, Selection, float, float | None, str, str]
    ] = []

    for raw in latest_per_endpoint(raws):
        if not raw.endpoint.startswith("pod-"):
            continue
        _collect_pod(source, raw, outcome, fixtures, identity, pending)

    # Over the whole capture rather than per pod: a doubleheader's ordinals must
    # be assigned across every pod that saw the matchup, or the same pair of
    # games gets two different answers in one run.
    keys = resolve_doubleheaders(identity)

    # One fixture can legitimately sit in two pods — the homepage strip and a
    # sport shelf can both carry tonight's game — and then every price arrives
    # twice, identically.  That is not a rejection: nothing was offered that we
    # failed to represent, it is one row seen from two shelves.  Collapse it
    # here, and *only* across pods: two identical prices inside one pod stay a
    # genuine anomaly and still reach drop_duplicate_selections, which remains
    # the alarm for one selection carrying two different prices.
    first_seen: dict[tuple[Any, ...], str] = {}
    deduped: list[Any] = []
    for entry in pending:
        event_id, raw, market, selection, decimal, line, market_id, sel_id = entry
        signature = (event_id, market, selection, decimal, line, market_id, sel_id)
        origin = first_seen.get(signature)
        if origin is not None and origin != raw.endpoint:
            outcome.skipped["same_price_in_two_pods"] += 1
            continue
        first_seen.setdefault(signature, raw.endpoint)
        deduped.append(entry)
    pending = deduped

    for event_id, raw, market, selection, decimal, line, market_id, sel_id in pending:
        fixture = fixtures.get(event_id)
        event_key = keys.get(event_id)
        if fixture is None or event_key is None:
            outcome.skipped["fixture_missing_for_price"] += 1
            continue
        try:
            outcome.quotes.append(
                priced_quote(
                    fixture,
                    source=source,
                    raw=raw,
                    event_key=event_key,
                    market=market,
                    period=Period.FULL_GAME,
                    # our_side() restates the *book's* home/away against ours.
                    # OVER/UNDER and DRAW belong to no competitor, and passing
                    # either through it resolves to a team — silently pricing a
                    # draw as one side of the match.
                    selection=(
                        fixture.our_side(selection)
                        if selection in (Selection.HOME, Selection.AWAY)
                        else selection
                    ),
                    line=line,
                    decimal_odds=decimal,
                    american_odds=decimal_to_american(decimal),
                    implied_probability=implied_probability(decimal),
                    status=QuoteStatus.ACTIVE,
                    source_market_id=market_id or None,
                    source_selection_id=sel_id or None,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(source, "invalid_quote", str(exc), event_id=event_id)

    _require_the_draw_leg(source, outcome)
    drop_duplicate_selections(source, outcome)
    return outcome


def _require_the_draw_leg(source: str, outcome: ParseOutcome) -> None:
    """Refuse a three-way moneyline that lost its draw, rather than shipping two legs.

    This is the one incompleteness that is not merely a missing row.  A soccer
    moneyline priced on two outcomes is **byte-identical** to a genuine two-way
    market, so a dropped draw does not look like a gap — it looks like a
    complete market whose two prices happen to leave a tenth of the probability
    unbacked, and staking both legs against another book turns a floor of zero
    into minus the stake.

    Here the draw arrives as its own column, so losing it is exactly what a
    suspended ``Tie`` cell or a renamed header produces — neither of which is
    visible from the two surviving rows.  Two-sided markets are deliberately not
    held to this: a suspended leg there leaves one honest price that still pairs
    against another book.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, quote in enumerate(outcome.quotes):
        if quote.market is not Market.MONEYLINE:
            continue
        if not draw_is_priced(quote.sport, quote.period):
            continue
        groups.setdefault((quote.event_key, quote.source_event_id), []).append(index)

    doomed: set[int] = set()
    for (event_key, event_id), indexes in groups.items():
        legs = [outcome.quotes[i] for i in indexes]
        if any(leg.selection is Selection.DRAW for leg in legs):
            continue
        outcome.reject(
            source,
            "three_way_moneyline_without_a_draw",
            f"event {event_id} ({event_key}) produced "
            f"{sorted(leg.selection.value for leg in legs)} and no draw",
            event_id=event_id,
        )
        doomed.update(indexes)

    if doomed:
        outcome.quotes[:] = [
            quote for index, quote in enumerate(outcome.quotes) if index not in doomed
        ]


def _collect_pod(
    source: str,
    raw: RawResponse,
    outcome: ParseOutcome,
    fixtures: dict[str, Fixture],
    identity: dict[str, tuple[str, datetime]],
    pending: list[Any],
) -> None:
    """Walk one pod, joining market columns onto fixture rows by ``PZ``."""
    records = list(_records(raw.body))
    # Read ahead: a row's own league is stated by its *prices*, which arrive
    # after the fixture row that needs it.  See _row_leagues.
    group_leagues = _row_leagues(records)
    group_index = -1

    league_tag = ""
    column: dict[str, str] | None = None
    # PZ -> event id, or None where the row was seen and declined.  The
    # difference matters: an absent key means the join itself broke.
    rows: dict[str, str | None] = {}
    # PZ -> how many prices this column has already emitted for that row.  A
    # positional market reads its side off that ordinal, so the counter is
    # per column and is cleared with the column: the payload states position
    # only *within* one MA, and any wider scope answers a different question.
    column_seen: dict[str, int] = {}
    saw_group = False

    for kind, fields in records:
        if kind == "EV" and fields.get("L3"):
            league_tag = fields["L3"]
        elif kind == "MG":
            if fields.get("SY") == _MARKET_GROUP:
                saw_group = True
                rows = {}
                group_index += 1
                if fields.get("L3"):
                    league_tag = fields["L3"]
        elif kind == "MA":
            column = fields
            column_seen = {}
        elif kind == "PA":
            style = (column or {}).get("SY", "")
            if style == _FIXTURE_COLUMN:
                stated = (
                    group_leagues[group_index]
                    if 0 <= group_index < len(group_leagues)
                    else {}
                )
                _accept_fixture(
                    source,
                    raw,
                    fields,
                    stated.get(fields.get("PZ", "")) or league_tag,
                    outcome,
                    fixtures,
                    identity,
                    rows,
                )
            elif style == _MARKET_COLUMN:
                _accept_price(
                    raw,
                    fields,
                    column or {},
                    rows,
                    fixtures,
                    outcome,
                    pending,
                    column_seen,
                )

    if not saw_group:
        # A pod with no coupon at all is a legitimate shape — the additional pod
        # is mostly futures and boosts.  Record it so a pod that *stopped*
        # carrying one is visible rather than silent.
        outcome.skipped[f"no_coupon_in_pod:{raw.endpoint}"] += 1


def _accept_fixture(
    source: str,
    raw: RawResponse,
    fields: Mapping[str, str],
    league_tag: str,
    outcome: ParseOutcome,
    fixtures: dict[str, Fixture],
    identity: dict[str, tuple[str, datetime]],
    rows: dict[str, str | None],
) -> None:
    position = fields.get("PZ", "")
    if not position:
        # The companion row the fixture column emits after each real one.  It
        # carries live state and no names; counting it shifts every later join.
        return
    rows[position] = None  # declined until proven otherwise
    first, second = fields.get("NA", ""), fields.get("N2", "")
    if not first or not second:
        outcome.skipped["fixture_without_both_names"] += 1
        return

    # **Row order does not state the host, and it is not constant across sports.**
    # ``FD`` does, in its separator: US leagues read "<away> @ <home>" while
    # soccer reads "<home> v <away>", the European convention.  Assuming the
    # baseball order everywhere inverted every MLS fixture — rows that parsed,
    # validated and priced the wrong team, caught only because cross-source
    # validation saw bet365 disagree with the whole slate about the host on 4 of
    # 6 shared games.
    #
    # Matched by reconstructing the whole string rather than searching for a
    # separator, so a club whose own name contains " v " cannot decide this.
    hosted = True
    if fields.get("FD") == f"{first} @ {second}":
        away_name, home_name = first, second
    elif fields.get("FD") == f"{first} v {second}":
        home_name, away_name = first, second
    elif fields.get("FD") == f"{first} vs {second}":
        # A third separator, and it means something the other two do not: two
        # names and *no host*, which is what tennis is.  It was read as "no FD"
        # for a while — the rows were declined under the reason below, which
        # matched the count exactly and so looked correct.  The field is there;
        # this spelling was simply not one of the two being reconstructed.
        hosted = False
        away_name, home_name = first, second
    else:
        # No statement of the host from a league that has one.  Declined rather
        # than guessed: row order does not state it and is not constant.
        outcome.skipped["fixture_without_a_stated_host"] += 1
        return
    if fields.get("FS") == "1":
        # In-play.  These rows carry no prices in a coupon pod, and this
        # repository collects pregame.
        outcome.skipped["event_already_started"] += 1
        return

    key = _league_key(league_tag)
    if key is None or key not in MARKETS_BY_LEAGUE:
        outcome.skipped[f"league_out_of_scope:{league_tag or 'unknown'}"] += 1
        return
    competition = league_by_key(key)
    if not hosted and competition.has_home_away:
        # " vs " on a league that really does have a host is not tennis' shape;
        # it is a fixture refusing to say, and orient() would impose an order by
        # key that the book never agreed to.
        outcome.skipped["fixture_without_a_stated_host"] += 1
        return

    commence = _start_time(fields.get("BC", ""))
    if commence is None:
        outcome.skipped["event_without_start_time"] += 1
        return
    if not within_schedule_horizon(commence, raw.fetched_at, competition):
        outcome.skipped["beyond_schedule_horizon"] += 1
        return
    if commence <= raw.fetched_at:
        # The row calls itself pregame (FS=0) and its own stated start has
        # already passed.  Tennis produces this routinely — a match on a court
        # running late is still "not started" hours after its slot — and the two
        # statements cannot both be published: a quote observed after the start
        # it names is not a pregame price, and settlement reads the start time
        # rather than the flag.  Distinct from event_already_started, which is
        # the book telling us plainly.
        outcome.skipped["start_time_already_passed"] += 1
        return

    found = _EVENT_ID.search(fields.get("PD", ""))
    if found is None:
        # Never fall back to OI: it is replaced by a separate in-play id at
        # kickoff, so a run spanning first pitch would file one game twice.
        outcome.skipped["event_without_stable_id"] += 1
        return
    event_id = found.group(1)

    away = canonical_participant(away_name, competition)
    home = canonical_participant(home_name, competition)
    if away is None or home is None:
        outcome.reject(
            source,
            "unresolved_participants",
            f"{away_name!r} @ {home_name!r} in {key}",
            event_id=event_id,
        )
        return

    # orient returns (away, home) — in that order.  Unpacking it the other way
    # round silently swaps every fixture and inverts every spread sign, which
    # still parses, still validates, and prices the wrong team.
    #
    # Where the league has no host, orient ignores the hint and imposes an order
    # by participant key, so what the book called first is not what we call
    # home.  The book's own numbered columns are stated against *its* order, so
    # book_home_key has to name the competitor it listed first — which is the
    # one FD spelled before " vs ".
    ordered_away, ordered_home = orient(
        away, home, competition, home=home if hosted else None
    )
    # ``away`` is the NA competitor on the hostless path — that branch binds
    # ``away_name, home_name = first, second`` — so this is the one the coupon
    # numbers "1".
    book_home = home if hosted else away
    base_key = build_event_key(
        ordered_away.key, ordered_home.key, commence, competition
    )
    fixtures[event_id] = Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=ordered_home,
        away=ordered_away,
        commence_time=commence,
        base_key=base_key,
        # Which competitor the *book* calls home, per separator — and it is not
        # the same one each time, which is the whole reason this field exists:
        #   "<NA> @ <N2>"   US boards — N2 is the host
        #   "<NA> v <N2>"   soccer    — NA is the host, the reverse
        #   "<NA> vs <N2>"  tennis    — no host; this names NA, the competitor
        #                               the coupon's "1" column prices
        book_home_key=book_home.key,
    )
    identity[event_id] = (base_key, commence)
    rows[position] = event_id


def _accept_price(
    raw: RawResponse,
    fields: Mapping[str, str],
    column: Mapping[str, str],
    rows: Mapping[str, str | None],
    fixtures: Mapping[str, Fixture],
    outcome: ParseOutcome,
    pending: list[Any],
    column_seen: dict[str, int],
) -> None:
    position = fields.get("PZ", "")
    if not position:
        outcome.skipped["price_without_row_index"] += 1
        return
    # Count the row's place in this column *before* any of the declines below.
    # Position is a fact about the payload, not about what we chose to keep: if
    # the away leg is suspended, the home leg is still the second price, and
    # counting only accepted rows would file it as the first — i.e. as away.
    ordinal = column_seen.get(position, 0)
    column_seen[position] = ordinal + 1
    if position not in rows:
        # The join broke: a priced column names a row the fixture column never
        # emitted.  Distinct from the line below because this one is a fault —
        # it is what a shifted PZ would look like — while declining an event we
        # saw is routine.
        outcome.skipped["price_without_fixture_row"] += 1
        return
    event_id = rows[position]
    if event_id is None:
        outcome.skipped["price_for_declined_event"] += 1
        return
    sport = fixtures[event_id].sport

    label = (column.get("NA") or "").strip()
    stated = _STATED_SIDE_COLUMNS.get(label.lower())
    market = Market.MONEYLINE if stated is not None else MARKET_NAMES.get(label.lower())
    if market is None:
        outcome.skipped[f"market_out_of_scope:{label or 'unnamed'}"] += 1
        return
    if stated is Selection.DRAW and not draw_is_priced(sport, Period.FULL_GAME):
        # A draw column on a sport whose full game cannot end level is a label
        # this adapter has misread, not a market.  Refusing it here keeps the
        # schema's own draw rule from becoming the first thing that notices.
        outcome.skipped["draw_on_a_market_that_cannot_tie"] += 1
        return

    if fields.get("SU") == "1":
        outcome.skipped["selection_suspended"] += 1
        return

    decimal = _decimal(fields.get("OD", ""))
    if decimal is None:
        outcome.skipped[f"unreadable_price:{market.value}"] += 1
        return

    display = fields.get("HD", "")
    handicap = fields.get("HA", "")
    line: float | None = None

    if market is Market.TOTAL:
        # HA carries the magnitude and no side whatsoever; over/under exists
        # only in HD's prefix.  Reading the side off HA's sign silently files
        # both legs as Over.
        prefix = display[:1].upper()
        if prefix == "O":
            selection = Selection.OVER
        elif prefix == "U":
            selection = Selection.UNDER
        else:
            outcome.skipped["total_without_side"] += 1
            return
        try:
            line = abs(float(handicap))
        except (TypeError, ValueError):
            outcome.skipped["total_without_line"] += 1
            return
    elif stated is not None:
        # The column header states the side outright, so there is nothing
        # positional to infer — and nothing to infer it from, since each of the
        # three columns carries a single price per row.
        selection = stated
    else:
        # Spread and the two-sided moneyline are positional: the first selection
        # under a PZ belongs to NA, the competitor listed first, which FD
        # confirms is away.
        #
        # The ordinal is scoped to this column, and that scope is load bearing.
        # This used to count matching entries across the whole capture, which
        # spans every pod and every coupon group in them: once one event was
        # listed in two pods, the second pod's first spread saw a count of two,
        # became HOME, and took its line from its own row — emitting HOME at the
        # away line and price.  Distinct dedup key, so nothing culled it; the
        # market simply went three-legged with a wrong-side leg in it.
        selection = Selection.AWAY if ordinal == 0 else Selection.HOME
        if market is Market.SPREAD:
            try:
                line = float(handicap)
            except (TypeError, ValueError):
                outcome.skipped["spread_without_line"] += 1
                return

    # FI on a market column is the (event x market) fixture id, so it separates
    # this event's three markets from each other — which is what market_key
    # needs.  It is *not* an event id; that comes from the fixture row.
    pending.append(
        (
            event_id,
            raw,
            market,
            selection,
            decimal,
            line,
            fields.get("FI", ""),
            fields.get("ID", ""),
        )
    )
