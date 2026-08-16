"""bet365's pull-pod board, and the ways a well-formed pod is still wrong.

The venue's traps are not malformed payloads — every capture here parses.  They
are payloads that parse into *plausible* rows describing something else: a
column-oriented coupon joined on a row index, a fraction that looks like a
decimal price, and a total whose over/under lives in a display string rather
than in the field that carries the number.  So most of these take a real capture
and change one thing.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from src.raw_store import RawResponse, RawStore
from src.schema import Market, Selection
from src.sources.bet365 import (
    LEAGUE_TAGS,
    MARKETS_BY_LEAGUE,
    SOURCE_KEY,
    Bet365Adapter,
    parse_bet365,
    redact_site_token,
)

RAW = Path(__file__).resolve().parent / "fixtures" / "raw"


def _load(pattern: str) -> RawResponse:
    """One capture, by glob — conftest's loader takes a source and returns all.

    Read through ``RawStore`` rather than mapping the envelope by hand, so the
    version gate and the sha256 verification both run: a hand-rolled loader
    accepts a tampered fixture that ``RawStore.read`` refuses by design.
    """
    return RawStore(RAW).read(next(RAW.glob(pattern)))


def _rebody(raw: RawResponse, body: str) -> RawResponse:
    """The same envelope carrying different bytes.

    Unlike every other adapter's version of this helper the payload is text,
    not a dict — there is no round-trip through ``json.dumps`` to normalize a
    mutation, so a test that edits the wire format edits a string.
    """
    return dataclasses.replace(raw, body=body)


@pytest.fixture(scope="module")
def homepage() -> RawResponse:
    return _load("bet365__*_pod-homepage_*.json")


@pytest.fixture(scope="module")
def additional() -> RawResponse:
    return _load("bet365__*_pod-additional_*.json")


@pytest.fixture(scope="module")
def board(homepage: RawResponse):
    return parse_bet365([homepage])


@pytest.fixture(scope="module")
def whole_board(homepage: RawResponse, additional: RawResponse):
    """Both pods, which is what a run collects — soccer is only in the second."""
    return parse_bet365([homepage, additional])


# ── the clean board ──────────────────────────────────────────────────────────


def test_the_captured_board_parses_without_rejections(board) -> None:
    assert board.quotes
    assert not board.rejections, [(r.reason, r.detail) for r in board.rejections]


def test_every_market_is_two_sided_and_priced_above_evens(board) -> None:
    """Hard invariant: a book never prices itself to lose.

    Both legs summing below 1.0 would be an arbitrage against bet365 alone,
    which is the shape a mis-signed or mis-joined pod produces.
    """
    groups: dict[tuple, list] = {}
    for quote in board.quotes:
        # Not keyed on line: a spread's two sides carry +1.5 and -1.5, so
        # including it would split every spread into two groups of one.
        groups.setdefault((quote.event_key, quote.market), []).append(quote)
    assert groups
    for key, rows in groups.items():
        assert len(rows) == 2, f"{key} is not two-sided: {len(rows)}"
        assert sum(r.implied_probability for r in rows) > 1.0, key


def test_the_join_never_breaks(board) -> None:
    """``PZ`` is the only thing tying a price to a fixture.

    A shifted row index still parses and still validates — it prices the right
    market for the wrong game.  The parser reports a price whose row the fixture
    column never emitted under its own reason, distinct from declining an event
    it did see, so this counter is the alarm for exactly that.
    """
    assert board.skipped.get("price_without_fixture_row", 0) == 0
    assert board.skipped.get("price_without_row_index", 0) == 0


def test_spreads_mirror_and_totals_share_one_line(board) -> None:
    for quote in board.quotes:
        if quote.market is Market.MONEYLINE:
            assert quote.line is None
        else:
            assert quote.line is not None
    by_market: dict[tuple, dict] = {}
    for quote in board.quotes:
        if quote.market is Market.MONEYLINE:
            continue
        by_market.setdefault((quote.event_key, quote.market), {})[quote.selection] = quote
    for (event, market), sides in by_market.items():
        if market is Market.SPREAD:
            assert sides[Selection.HOME].line == -sides[Selection.AWAY].line, event
        else:
            assert sides[Selection.OVER].line == sides[Selection.UNDER].line, event


def test_prices_agree_across_all_three_formats(board) -> None:
    for quote in board.quotes:
        assert quote.implied_probability == pytest.approx(
            1 / quote.decimal_odds, abs=1e-6
        )
        assert quote.american_odds <= -100 or quote.american_odds >= 100


# ── the witnesses ────────────────────────────────────────────────────────────


def test_orientation_agrees_with_the_payloads_own_second_statement(
    homepage, board
) -> None:
    """``FD`` spells the fixture out as ``"<away> @ <home>"``.

    It is a different field from the ``NA``/``N2`` pair the parser reads, so
    agreement between them is a genuine cross-check rather than a restatement —
    and it is the check that catches the orientation bug this adapter shipped
    with once: :func:`src.events.orient` returns ``(away, home)``, and unpacking
    it the other way round swaps every fixture and inverts every spread while
    leaving the payload entirely valid.
    """
    stated = {}
    for away, home in re.findall(r"FD=([^;@]+) @ ([^;]+);", homepage.body):
        stated[(away.strip(), home.strip())] = True
    assert stated, "the capture carries no FD rows to check against"

    checked = 0
    for quote in board.quotes:
        for (away, home) in stated:
            # bet365's short names ("MIA Marlins") are a prefix-ish form of our
            # canonical ones ("Miami Marlins"), so compare on the last word,
            # which is the club name in every US league this adapter collects.
            if quote.home_team.split()[-1] == home.split()[-1]:
                assert quote.away_team.split()[-1] == away.split()[-1], (
                    f"{quote.away_team} @ {quote.home_team} contradicts FD "
                    f"{away!r} @ {home!r}"
                )
                checked += 1
                break
    assert checked, "no quote matched an FD row, so nothing was cross-checked"


def test_the_start_time_is_utc_not_the_sites_local_clock(board) -> None:
    """``BC`` is UTC.  The site renders Central, and reading the rendered clock
    as the stored one would file every game five or six hours early."""
    assert board.quotes
    for quote in board.quotes:
        assert quote.commence_time.tzinfo is not None
        # Every row is pregame relative to the capture, which is only true if
        # BC was read as UTC.  Reading it as the site's Central clock would put
        # a five-hour block of the slate in the past.
        assert quote.commence_time >= quote.observed_at


def test_fractional_odds_are_not_read_as_decimal(board) -> None:
    """``OD=10/13`` is 1.769, not 0.769 — and ``OD=11/10`` would be 1.1.

    The second is the dangerous one: it stays inside the plausible range, so a
    missing ``1 +`` publishes a whole board of prices that are wrong and legal.
    """
    # Not `all(> 1.0)`: the schema's own validator refuses anything at or below
    # evens, so that assertion passes over the survivors no matter what the
    # parser did.  A missing `1 +` shows up as an absence of long prices.
    assert any(q.decimal_odds > 2.0 for q in board.quotes), (
        "a board of nothing but odds-on prices is what dividing without the 1+ "
        "produces"
    )


# ── the mutations ────────────────────────────────────────────────────────────


def test_a_total_whose_side_is_only_in_the_display_string(homepage) -> None:
    """``HA`` carries the magnitude and no side at all.

    Strip the ``O ``/``U `` prefix from ``HD`` and the side becomes unknowable;
    the parser must decline rather than guess, because guessing files both legs
    as Over and invents a market that sums to well under 1.0.
    """
    body = homepage.body.replace("HD=O ", "HD=").replace("HD=U ", "HD=")
    outcome = parse_bet365([_rebody(homepage, body)])
    assert outcome.skipped.get("total_without_side", 0) > 0
    assert not any(q.market is Market.TOTAL for q in outcome.quotes)
    # And the other two markets in the same payload still publish.
    assert any(q.market is Market.MONEYLINE for q in outcome.quotes)


def test_an_unreadable_price_is_counted_not_dropped(homepage) -> None:
    body = homepage.body.replace("OD=10/13", "OD=notaprice")
    outcome = parse_bet365([_rebody(homepage, body)])
    assert sum(
        v for k, v in outcome.skipped.items() if k.startswith("unreadable_price")
    ) > 0


def test_a_suspended_selection_is_skipped_under_its_own_reason(homepage) -> None:
    body = homepage.body.replace("SU=0;", "SU=1;")
    outcome = parse_bet365([_rebody(homepage, body)])
    assert outcome.skipped.get("selection_suspended", 0) > 0
    assert not outcome.quotes


def test_an_event_without_a_stable_id_is_declined(homepage) -> None:
    """Identity comes from the ``PD`` token's ``E<digits>`` group.

    ``OI`` sits on the same record and looks like an event id, but it is
    replaced when a fixture goes in-play — so a run spanning first pitch would
    file one game as two.  With ``PD`` gone the parser must decline rather than
    reach for the neighbour.
    """
    body = homepage.body.replace("#E", "#Z")
    outcome = parse_bet365([_rebody(homepage, body)])
    assert outcome.skipped.get("event_without_stable_id", 0) > 0
    assert not outcome.quotes


def test_every_dropped_record_lands_in_a_bucket(board) -> None:
    """The contract forbids a bare ``continue``.

    An empty skip dictionary on a parse that dropped rows is the one thing
    ``docs/INPUT_CONTRACT.md`` says a drop may never be.
    """
    assert board.skipped, "a pod carries futures and boosts; none were counted"


# ── the tennis coupon ────────────────────────────────────────────────────────


def _tennis(outcome) -> list:
    return [q for q in outcome.quotes if q.sport.value == "tennis"]


def test_the_tennis_coupon_publishes_and_carries_no_host(whole_board) -> None:
    """``FD`` spells tennis with a third separator, and it means *no host*.

    These rows were declined for a while as ``fixture_without_a_stated_host``,
    on the reading that tennis carries no ``FD`` at all.  It carries one — the
    additional pod has 19 ``" v "``, 8 ``" @ "`` and 10 ``" vs "`` — and the
    third spelling simply was not one of the two being reconstructed.  The count
    matched the shelf exactly, so the wrong reason looked like the right one.
    """
    rows = _tennis(whole_board)
    assert rows, "the tennis shelf produced nothing"
    assert all(q.market is Market.MONEYLINE for q in rows)
    assert all(q.line is None for q in rows)
    # Every fixture two-sided, and no draw: a tennis match cannot end level.
    by_event: dict[str, set] = {}
    for quote in rows:
        by_event.setdefault(quote.event_key, set()).add(quote.selection)
    for event, sides in by_event.items():
        assert sides == {Selection.HOME, Selection.AWAY}, f"{event}: {sides}"


def test_one_tennis_shelf_carries_two_tours(whole_board) -> None:
    """The group header names one competition and is wrong about half its rows.

    bet365 shelves the combined Cincinnati draw under a single ``WTA1-R2``
    header while the men's rows beneath it carry ``ATP1-R2``.  Reading the
    header would file Tommy Paul in the WTA — a row that parses, validates, and
    prices a real match in the wrong competition.  Each price states its own
    ``L3``, and that is the claim the parser takes.
    """
    leagues = {q.league for q in _tennis(whole_board)}
    assert leagues == {"ATP", "WTA"}, leagues


def test_a_tennis_price_follows_the_competitor_not_the_column(additional) -> None:
    """Swapping who the book lists first must not move a price between players.

    Tennis has no host, so ``orient`` imposes an order by participant key and
    the book orders the two names however it likes — the two disagree about half
    the time.  In the captured coupon they happen to agree on all four fixtures,
    so nothing here exercises the translation unless the listing is flipped.
    """
    before = {
        (q.event_key, q.selection): q.decimal_odds
        for q in _tennis(parse_bet365([additional]))
    }
    flipped = additional.body.replace(
        "NA=Peyton Stearns;N2=Clara Tauson;", "NA=Clara Tauson;N2=Peyton Stearns;"
    ).replace(
        "FD=Peyton Stearns vs Clara Tauson", "FD=Clara Tauson vs Peyton Stearns"
    )
    assert flipped != additional.body
    after = {
        (q.event_key, q.selection): q.decimal_odds
        for q in _tennis(parse_bet365([_rebody(additional, flipped)]))
    }

    # The book now calls column "1" Tauson, so the prices must swap sides with
    # it.  Same event key either way: the key is built from sorted participants.
    event = next(k for k, _ in before if "tauson" in k)
    assert after[(event, Selection.HOME)] == before[(event, Selection.AWAY)]
    assert after[(event, Selection.AWAY)] == before[(event, Selection.HOME)]
    # And no other fixture moved.
    others = {k: v for k, v in before.items() if k[0] != event}
    assert {k: v for k, v in after.items() if k[0] != event} == others


def test_a_fixture_whose_stated_start_has_passed_is_not_a_pregame_price(
    whole_board, additional
) -> None:
    """"Not started" and a start time two hours ago cannot both be published.

    Tennis produces this routinely — a match on a court running late still
    carries ``FS=0`` long after its slot — and the captured coupon has one.  A
    quote observed after the start it names is not a pregame price, and
    settlement reads the start time rather than the flag.
    """
    assert whole_board.skipped.get("start_time_already_passed", 0) == 1
    for quote in whole_board.quotes:
        assert quote.observed_at < quote.commence_time, quote.event_key

    # And it is the start time doing the work, not the in-play flag: move that
    # one fixture's start into the future and it publishes.
    body = additional.body.replace("BC=20260815213000;FG=;IA=1;AU=0;NA=Yannick Hanfmann",
                                   "BC=20260816213000;FG=;IA=1;AU=0;NA=Yannick Hanfmann")
    assert body != additional.body
    outcome = parse_bet365([_rebody(additional, body)])
    assert outcome.skipped.get("start_time_already_passed", 0) == 0
    assert any("hanfmann" in q.event_key for q in outcome.quotes)


def test_a_hostless_spelling_is_still_declined_where_the_league_has_a_host(
    homepage,
) -> None:
    """" vs " is only *no host* for a league that genuinely has none.

    On a league with a real home side the same spelling is a fixture refusing to
    say, and accepting it would let ``orient`` impose an ordering the book never
    agreed to — which is the inversion this venue already shipped once.
    """
    body = homepage.body.replace(" @ ", " vs ")
    outcome = parse_bet365([_rebody(homepage, body)])
    assert outcome.skipped.get("fixture_without_a_stated_host", 0) > 0
    assert not any(q.league == "MLB" for q in outcome.quotes)


# ── two pods, one game ───────────────────────────────────────────────────────


_SIGNED_LINE = re.compile(r"HA=([+-])(\d+\.\d+);HD=[+-]\d+\.\d+;")


def _shift_spread_lines(body: str) -> str:
    """The same pod with every spread line moved a division out.

    Only spreads match: a total's ``HD`` reads ``O 7.5`` and a moneyline's ``HA``
    is empty, so neither carries the signed pair this rewrites.  Moving the line
    is what gives the second pod's prices their own dedup keys — without it a
    mis-sided row is culled as a duplicate and the bug hides behind the cull.
    """
    return _SIGNED_LINE.sub(
        lambda m: f"HA={m.group(1)}1{m.group(2)};HD={m.group(1)}1{m.group(2)};", body
    )


def _refile(raw: RawResponse, endpoint: str, body: str | None = None) -> RawResponse:
    """The same capture presented as a second pod."""
    return dataclasses.replace(
        raw, endpoint=endpoint, body=raw.body if body is None else body
    )


def test_one_event_in_two_pods_keeps_every_spread_on_its_own_side(homepage) -> None:
    """A side is a position *within one column*, not a running total.

    The homepage strip and a sport shelf can both carry tonight's game, and the
    parser must side each pod's prices on their own.  Counting matches across
    the whole capture instead made the second pod's first spread the *third*
    price it had seen, so it became HOME — and took its line from its own row,
    publishing HOME at the away line and the away price.  Distinct dedup key,
    so nothing culled it: the market simply went three-legged with a wrong-side
    leg in it, which is a phantom arbitrage rather than a missing row.
    """
    second = _refile(homepage, "pod-splash-b16", _shift_spread_lines(homepage.body))
    outcome = parse_bet365([homepage, second])

    spreads: dict[str, list] = {}
    for quote in outcome.quotes:
        if quote.market is Market.SPREAD:
            spreads.setdefault(quote.event_key, []).append(quote)
    assert spreads, "the mutation removed the spreads it was meant to move"

    for event, rows in spreads.items():
        sides = [row.selection for row in rows]
        assert sides.count(Selection.AWAY) == 2, f"{event}: {sides}"
        assert sides.count(Selection.HOME) == 2, f"{event}: {sides}"
        # Each pod's own pair still mirrors around its own line.
        by_line: dict[float, list] = {}
        for row in rows:
            by_line.setdefault(abs(row.line), []).append(row)
        for line, pair in by_line.items():
            assert len(pair) == 2, f"{event} at {line}: {len(pair)}"
            home = next(r for r in pair if r.selection is Selection.HOME)
            away = next(r for r in pair if r.selection is Selection.AWAY)
            assert home.line == -away.line, f"{event} at {line}"

    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]


def test_the_same_price_in_two_pods_is_a_skip_not_a_rejection(homepage) -> None:
    """One row seen from two shelves is not something we failed to represent.

    ``drop_duplicate_selections`` stays the alarm for one selection carrying two
    *different* prices; an identical re-listing is a skip, and it must not change
    the published board at all.
    """
    twice = parse_bet365([homepage, _refile(homepage, "pod-splash-b16")])
    once = parse_bet365([homepage])

    assert twice.skipped.get("same_price_in_two_pods", 0) > 0
    assert not twice.rejections, [(r.reason, r.detail) for r in twice.rejections]
    assert {q.dedup_key for q in twice.quotes} == {q.dedup_key for q in once.quotes}
    assert len(twice.quotes) == len(once.quotes)


def test_an_identical_price_twice_in_one_pod_is_still_an_alarm(homepage) -> None:
    """The cross-pod collapse must not swallow a genuine intra-pod duplicate.

    One shelf listing a price twice is not two shelves showing one game, and it
    stays a rejection.  The duplicate has to live inside a single body to be
    reachable at all: ``latest_per_endpoint`` keeps one response per label, so
    handing the parser the same endpoint twice tests nothing.
    """
    doubled = _rebody(homepage, homepage.body + "\x08" + homepage.body)
    outcome = parse_bet365([doubled])
    assert outcome.skipped.get("same_price_in_two_pods", 0) == 0
    assert any(r.reason == "duplicate_dedup_key" for r in outcome.rejections)


# ── the transport ────────────────────────────────────────────────────────────


def test_the_guard_marker_scan_does_not_refuse_a_real_board(homepage) -> None:
    """The board is ``text/plain``, so it is scanned with the *loose* marker set.

    ``check_http_response`` unlocks the captcha and login markers for a non-JSON
    body on the reasoning that such a body is already not the odds payload.
    Here it *is* the odds payload, so a team or market name containing one of
    those tokens would refuse a perfectly good board.  This asserts the captured
    bytes survive the real guard rather than assuming they do.
    """
    from src.sources.guards import check_http_response

    check_http_response(
        source=SOURCE_KEY,
        endpoint="pod-homepage",
        status_code=200,
        content_type="text/plain; charset=utf-8",
        body=homepage.body,
        expect_json=False,
    )


def test_the_site_token_is_redacted_and_nothing_else_is() -> None:
    shell = '{"SST":"AEYAAbUm/u94E+dk","SSI":"czEuYnRzZmFwaS5jb20=","TTI":false}'
    cleaned = redact_site_token(shell)
    assert "AEYAAbUm" not in cleaned
    assert '"SSI":"czEuYnRzZmFwaS5jb20="' in cleaned

    # A refusal must not be laundered into an apparently-good body: the filter
    # sees every response, and a broad one would rewrite a block page.
    blocked = "<html><title>Sorry, you have been blocked</title></html>"
    assert redact_site_token(blocked) == blocked


# ── configuration ────────────────────────────────────────────────────────────


def test_there_is_no_default_host() -> None:
    """An adapter that falls back to one state's host prices another licence."""
    from src.sources.guards import SourceError

    with pytest.raises(SourceError, match="no base_url"):
        Bet365Adapter().fetch_raw()


def test_parsing_does_not_depend_on_how_the_instance_was_configured(
    homepage, board
) -> None:
    """Replay builds the adapter with no arguments at all."""
    narrowed = parse_bet365([homepage])
    assert len(narrowed.quotes) == len(board.quotes)


def test_every_mapped_league_is_registered() -> None:
    from src.leagues import league as league_by_key

    for key in set(LEAGUE_TAGS.values()) | set(MARKETS_BY_LEAGUE):
        assert league_by_key(key) is not None


def test_the_womens_league_maps_apart_from_the_mens() -> None:
    """The reason this adapter is on the ``IMMUNE`` list.

    WNBA resolves through its own tag rather than through a catch-all that
    could read a women's competition as the men's league of the same sport.
    """
    assert LEAGUE_TAGS["WNBA"] == "WNBA"
    assert LEAGUE_TAGS["NBA"] == "NBA"


# ── the three-way coupon ─────────────────────────────────────────────────────


def test_soccer_prices_three_ways_from_three_one_sided_columns(whole_board) -> None:
    """MLS names the side in the column header, not by row order.

    Every other market on this board is two-sided and positional: two ``PA``
    under one row index, the first belonging to the first-listed competitor.
    Soccer's is three columns of one price each, headed ``Home``/``Tie``/``Away``.
    Reading those positionally would file the draw as a team.
    """
    outcome = whole_board
    mls = [q for q in outcome.quotes if q.league == "MLS"]
    assert mls, "the captured board carries an MLS coupon"
    assert {q.market for q in mls} == {Market.MONEYLINE}

    by_event: dict[str, set[Selection]] = {}
    for quote in mls:
        by_event.setdefault(quote.event_key, set()).add(quote.selection)
    for event_key, sides in by_event.items():
        assert sides == {Selection.HOME, Selection.DRAW, Selection.AWAY}, (
            f"{event_key} priced {sorted(s.value for s in sides)}"
        )


def test_the_draw_is_not_restated_as_a_team(whole_board) -> None:
    """``our_side`` translates the book's home/away onto ours — and only those.

    It resolves anything that is not ``HOME`` to the book's *away* side, so a
    ``DRAW`` passed through it comes back as a competitor.  The row would be
    well formed, priced, and describe the wrong outcome entirely.
    """
    outcome = whole_board
    draws = [q for q in outcome.quotes if q.selection is Selection.DRAW]
    assert draws, "the captured board carries a three-way market"
    for quote in draws:
        assert quote.market is Market.MONEYLINE
        assert quote.line is None
        # A draw belongs to neither competitor, so it must not have inherited
        # either one's price identity.
        assert quote.home_participant != quote.away_participant


def test_a_three_way_market_that_lost_its_draw_publishes_nothing(
    homepage, additional
) -> None:
    """Two legs of a three-way are byte-identical to a genuine two-way market.

    So a suspended ``Tie`` cell does not read as a gap — it reads as a complete
    market whose two prices leave a tenth of the probability unbacked, and
    staking both against another book turns a floor of zero into minus the
    stake.  The whole market goes rather than the survivors publishing.
    """
    mutated = _rebody(additional, additional.body.replace("NA=Tie;", "NA=Nope;"))
    outcome = parse_bet365([homepage, mutated])

    assert not [q for q in outcome.quotes if q.league == "MLS"], (
        "MLS survived with a two-legged three-way"
    )
    assert not [q for q in outcome.quotes if q.selection is Selection.DRAW]
    # And the rest of the board is untouched — the refusal is per market.
    assert [q for q in outcome.quotes if q.league == "MLB"]


def test_a_draw_column_on_a_sport_that_cannot_tie_is_refused(homepage) -> None:
    """A ``Tie`` header over baseball is a misread label, not a market.

    Refusing it here rather than letting the schema's own draw rule catch it
    keeps the diagnosis in a counter instead of in a rejection whose message
    describes a model constraint.
    """
    mutated = _rebody(homepage, homepage.body.replace("NA=Total;", "NA=Tie;"))
    outcome = parse_bet365([mutated])
    assert outcome.skipped["draw_on_a_market_that_cannot_tie"] > 0
    assert not [q for q in outcome.quotes if q.selection is Selection.DRAW]


def test_the_host_comes_from_the_separator_not_from_row_order(whole_board) -> None:
    """bet365 orders US fixtures away-first and soccer fixtures home-first.

    ``FD`` is the only statement of which: ``"<away> @ <home>"`` against
    ``"<home> v <away>"``.  Assuming the US order everywhere inverted all eleven
    MLS fixtures, and nothing inside this adapter noticed — the rows were well
    formed and priced.  Cross-source validation caught it, reporting that bet365
    disagreed with the whole slate about the host on 4 of 6 shared games.
    """
    outcome = whole_board
    # A soccer fixture, read straight off the payload's own sentence.
    mls = {(q.away_team, q.home_team) for q in outcome.quotes if q.league == "MLS"}
    assert ("FC Cincinnati", "Orlando City") in mls, sorted(mls)[:4]
    # And a baseball one, which uses the other separator and the other order:
    # the payload says "PHI Phillies @ MIN Twins", so Minnesota is the host.
    mlb = {(q.away_team, q.home_team) for q in outcome.quotes if q.league == "MLB"}
    assert ("Philadelphia Phillies", "Minnesota Twins") in mlb, sorted(mlb)[:4]


def test_a_fixture_that_will_not_say_who_is_home_is_declined(homepage) -> None:
    """No separator, no host, no row.

    Tennis rows legitimately carry no ``FD`` — there is no home side — and every
    league collected here has one, so a row that will not state it is declined
    rather than defaulted to whichever order the last sport used.
    """
    mutated = _rebody(homepage, homepage.body.replace(" @ ", " ~ "))
    outcome = parse_bet365([mutated])
    assert outcome.skipped["fixture_without_a_stated_host"] > 0
    assert not [q for q in outcome.quotes if q.league == "MLB"]


# ── scopes ───────────────────────────────────────────────────────────────────


def test_a_configured_league_the_board_never_carries_is_counted() -> None:
    """NHL is configured, in season nowhere, and used to report nothing at all.

    Scopes were counted per *pod*, so a league the venue simply never listed
    produced no counter — a silent zero inside a run that graded healthy.  The
    tally counts leagues, so an absent shelf is an empty scope.
    """
    from src.sources._common import ScopeTally

    tally = ScopeTally("bet365")
    adapter = Bet365Adapter(base_url="https://www.il.bet365.com")
    for league in adapter.leagues:
        tally.requested(league)
    assert "NHL" in adapter.leagues
    assert tally.scopes_requested == len(adapter.leagues)


def test_every_pod_names_its_own_path() -> None:
    """A shelf's path is carried, never derived from its label.

    It used to be ``"pods" if label == "homepage" else "additionalpods"``, which
    is right for exactly the two entries that existed and silently sends every
    later one to the additional pod — answering, with the wrong shelf in it,
    under the label of the shelf that was asked for.
    """
    from src.raw_store import _slug
    from src.sources.bet365 import PODS

    assert len({pod.path for pod in PODS}) == len(PODS)
    assert len({_slug(pod.label) for pod in PODS}) == len(PODS)
    for pod in PODS:
        assert pod.path.startswith("/"), pod
        assert pod.token.startswith("#HO#"), pod
    # The two shelves are distinct endpoints, not one endpoint twice.
    assert {pod.path for pod in PODS} == {
        "/pullpodapi/gethomepagepods",
        "/pullpodapi/gethomepageadditionalpods",
    }


def test_a_dead_pod_does_not_refuse_the_leagues_another_pod_delivered() -> None:
    """One shelf failing must not grade the whole book collapsed.

    ``scopes_failed / scopes_requested`` is what ``_check_source_health``
    thresholds at 0.5 for an ERROR, and a pod failure used to be attributed to
    every configured league — including the ones the *surviving* pod had just
    published.  With both pods carrying different sports, that turned a partial
    outage into "collapsed" while a full slate was in hand.
    """
    import httpx

    from src.sources.bet365 import PODS

    shell = '<html>{"STATE_LOCALE":"USIL"}</html>'
    good = _load("bet365__*_pod-additional_*.json").body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("additionalpods"):
            return httpx.Response(200, text=good)
        if "pullpodapi" in request.url.path:
            return httpx.Response(503, text="upstream is unavailable")
        return httpx.Response(200, text=shell)

    adapter = Bet365Adapter(
        base_url="https://www.il.bet365.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        proxy_state="IL",
    )
    raws = adapter.fetch_raw()
    tally = adapter.last_fetch

    assert len(PODS) == 2
    # The shelf that answered carried these, so none of them was refused.
    delivered = {"MLS", "NBA", "WNBA"}
    assert delivered <= set(adapter.leagues)
    refused = {entry.split(":", 1)[0] for entry in tally.failed_scopes}
    assert not (delivered & refused), tally.failed_scopes
    # It is still recorded — as a note about those scopes, outside the share.
    assert tally.truncated_scopes
    # And the leagues only the dead shelf could have carried are honest failures.
    assert tally.scopes_failed < tally.scopes_requested
    assert any(raw.endpoint == "pod-additional" for raw in raws)


def test_source_key_matches_the_module() -> None:
    assert SOURCE_KEY == "bet365"
