"""How many independent ways this application saw each book it must see.

The rule, for every book a jurisdiction declares it requires:

**Either the book was fetched from the book itself, or at least two independent
republishers *of that state's own licence* saw it and they agree.**

One republished feed is not evidence.  ``vi_hardrock`` alone produced all three
mispaired sportsbook markets in the 2026-08-06 Pennsylvania run — one summing to
0.5167, which the report printed as "Hard Rock Bet keeps -48.3%" beside eleven
venues keeping 1.5-4.4% — and nothing contradicted it, because nothing else was
watching that book.  Two feeds that agree would have caught it; two feeds that
disagree would at least have said so.  A single feed can only be believed or
ignored, and both are guesses.

Locality is the second half of the rule, and it is not a refinement of the first
— it is the part that decides what the number *is*.  VegasInsider publishes one
Las Vegas column per operator; Action Network files a separate book id per state
licence.  ``an_fanduel`` in Pennsylvania is FanDuel PA (id 255) while
``vi_fanduel`` is FanDuel's Nevada-facing number, and they are two different
books wearing one brand.  A Nevada column cannot corroborate a Pennsylvania
price: when the two differ, neither of them is wrong, so the comparison carries
no information about whether the PA number is right.

Hence the two axes, and neither one substitutes for the other:

* :attr:`Corroboration.SAME_LICENCE` — this feed carries a book id scoped to the
  state being collected.  Only these count towards the two, and only these are
  compared against each other.
* :attr:`Corroboration.OTHER_LICENCE` — the same brand under a different
  licence.  Recorded and shown as context, **never** counted towards the two and
  never price-compared.  Holding it to same-licence agreement would fault every
  book forever; counting it towards the two would let a Las Vegas number stand in
  for the state licence, which is the failure the rule exists to prevent.

Three things this module deliberately refuses to do.

It does not treat *silence* as compliance.  A state with no declared requirement
reports ``book_coverage_not_declared`` rather than passing, because an empty
checklist rendered as a row of ticks is worse than no checklist at all.

It does not let a book with no local observation at all read as merely thin.
:attr:`Access.NON_LOCAL_ONLY` is its own state, reported at ``ERROR``: a book
watched only from out of state is not under-corroborated, it is unobserved in the
jurisdiction the run claims to describe.

It does not read "no disagreement found" as agreement.  Below
``MIN_SHARED_SELECTIONS`` the comparison is undecided, and two feeds nobody could
compare are :attr:`Access.AGREEMENT_UNPROVEN` — a ``WARNING``, because a thin
overlap is the slate's fault, but not a tick, because the rule's second half says
*and they agree*.

**Two things about the name.**  :mod:`src.validation` already had a "coverage"
vocabulary before this module existed — its private ``_check_coverage`` and the
"coverage grid" report which sport each *source* priced — and both write findings
into the same :class:`~src.validation.ValidationReport`.  They are unrelated
questions: that one asks which sports a source covered, this one asks how many
independent ways one book was seen.  The public entry point here is therefore
:func:`check_book_coverage`, so that grepping either name lands in one layer.

And the module is *state locality*, not only corroboration.  Counting feeds
(:func:`check_book_coverage`) is rule (c); :func:`locality_marking` and
:func:`locality_applies` are the same rule (a) applied to arb **output** rather
than to inputs — never present a price as this state's when it cannot be reached
from here.  A position with an unreachable leg is shown, but every surface labels
that leg so nobody mistakes it for the state's own price.  They live beside the
corroboration rule because both answer from the same tables and must not drift
apart, and because a reader who finds one has to find the other.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Collection, Mapping, Protocol, Sequence

from src.jurisdictions import (
    JURISDICTIONS,
    RouteStatus,
    files_per_state_book_id,
    jurisdiction,
    normalize_state,
    republishes_state_licence,
)

if TYPE_CHECKING:
    from src.schema import Quote
    from src.validation import ValidationReport


class _HasSources(Protocol):
    """The one thing :class:`LocalityMarking` needs of an ``Opportunity``.

    Structural rather than imported, for cost rather than for a cycle: importing
    :mod:`src.arb` to name one attribute would pull :mod:`src.validation`,
    :mod:`src.distinctness` and :mod:`src.sources.registry` — every adapter — into
    every importer of this module.  There is no import cycle to avoid here; an
    earlier version of this docstring claimed one via ``distinctness ->
    validation``, and that edge does not exist.
    """

    @property
    def sources(self) -> Collection[str]: ...


class Corroboration(StrEnum):
    """Whether a republisher watches the same licence as the state's own book."""

    SAME_LICENCE = "same_licence"
    OTHER_LICENCE = "other_licence"

    @property
    def is_local(self) -> bool:
        """Does this feed carry the collected state's own licence?

        The two-feed requirement counts local feeds and nothing else.  Reading
        this off the enum rather than re-deriving it at each call site is what
        keeps :func:`coverage_for_state` and :func:`_agreement` from drifting
        into counting one set and comparing another.
        """
        return self is Corroboration.SAME_LICENCE


class Access(StrEnum):
    """How well a required book is observed, best first."""

    DIRECT = "direct"
    CROSS_CHECKED = "cross_checked"
    NOT_REQUESTED = "not_requested"
    AGREEMENT_UNPROVEN = "agreement_unproven"
    DISAGREEING = "disagreeing"
    SINGLE_SOURCE = "single_source"
    NON_LOCAL_ONLY = "non_local_only"
    MISSING = "missing"

    @property
    def satisfies_rule(self) -> bool:
        """Two feeds that were never compared do not satisfy "and they agree".

        ``AGREEMENT_UNPROVEN`` is deliberately outside this set.  Folding it into
        ``CROSS_CHECKED`` made the rule's second half satisfiable by two feeds
        nobody had compared: below ``MIN_SHARED_SELECTIONS`` the comparison
        returns "no disagreement found", which is not the same claim, and the
        caveat was attached to a ``detail`` string no surface renders.  Reported
        at ``WARNING`` rather than ``ERROR`` because a thin overlap is a thin
        slate's fault, not the book's.
        """
        return self in (Access.DIRECT, Access.CROSS_CHECKED)

    @property
    def is_unobserved_locally(self) -> bool:
        """Did nothing carrying this state's licence price this book?

        ``NON_LOCAL_ONLY`` and ``MISSING`` differ in what the report can show a
        reader — one has an out-of-state number beside it and the other has
        nothing — but they are the same fact about the jurisdiction, so both are
        errors rather than warnings.
        """
        return self in (Access.NON_LOCAL_ONLY, Access.MISSING)

    @property
    def is_judgeable(self) -> bool:
        """Was this book's coverage actually put to the test on this run?

        ``NOT_REQUESTED`` is neither pass nor fail.  A run narrowed with
        ``--source`` collected a deliberate subset, and faulting the books whose
        feeds were never asked for is the same mistake ``validation``'s
        *capabilities* argument exists to avoid: it reported all eleven PA books
        missing on a run that asked for three sources.
        """
        return self is not Access.NOT_REQUESTED


@dataclass(frozen=True)
class RequiredBook:
    """A book a jurisdiction must see, and every way it can be seen.

    ``direct`` is the first-party adapter key, or ``None`` when this application
    has no route to the book itself.  ``republishers`` maps a republisher source
    key to whether it watches the same state licence.
    """

    book: str
    direct: str | None
    republishers: Mapping[str, Corroboration]


def _required(
    book: str,
    direct: str | None = None,
    *,
    same: Sequence[str] = (),
    other: Sequence[str] = (),
) -> RequiredBook:
    return RequiredBook(
        book=book,
        direct=direct,
        republishers={
            **{key: Corroboration.SAME_LICENCE for key in same},
            **{key: Corroboration.OTHER_LICENCE for key in other},
        },
    )


#: Books required per state.  A state absent from this table is **unchecked**,
#: not compliant — see :func:`check_book_coverage`.
#:
#: Pennsylvania's list is the eleven books the operator named.  Three entries
#: record the feed that exists rather than the feed one would want, and say so
#: here rather than looking clean in the report:
#:
#: * ``theScore Bet`` resolves to id 4623, and that is now **settled by name**.
#:   ``https://api.actionnetwork.com/web/v1/books`` returns Action Network's own
#:   catalogue — 456 books, each with a ``display_name`` and a ``source_name`` —
#:   and 4623 is ``'theScore Bet PA'`` / ``thescorebetpa``.  An earlier note here
#:   said the payload carries no catalogue and the mapping could not be settled
#:   from the bytes we collect; that was true of the *scoreboard* payload and
#:   false of the API, and it is the reason the question stayed open for so long.
#:   The catalogue also disposes of the claim that theScore Bet had withdrawn
#:   from the United States: it lists twenty-one live per-state theScore Bet
#:   books.  Still ``SINGLE_SOURCE``, because one feed is one feed — but the
#:   uncertainty is now about redundancy, not identity.
#: * ``Mohegan Pennsylvania`` is watched through id 246, which the same catalogue
#:   names ``'UnibetPA'`` / ``paunibet``.  **No book in the catalogue is named
#:   Mohegan** — not under ``mohegan``, ``sun`` or ``pocono``.  So the feed is
#:   Unibet Pennsylvania, and whether that is Mohegan's price is a question about
#:   which company holds the licence behind the brand, not about which id to ask
#:   for.  Mohegan Sun Pocono's online skin did run as Unibet in Pennsylvania, so
#:   the mapping is plausible and stays; what has changed is that the doubt is
#:   now located precisely, and it is no longer a doubt this repository can close
#:   with more scraping.  It needs the operator's own licence record.
#:   Reported as ``SINGLE_SOURCE`` and labelled unverified, as before.
#: * ``PlaySugarHouse`` has **no** feed.  Action Network's only SugarHouse book
#:   (id 708) returns nothing even when named explicitly — re-measured
#:   2026-08-08 on both endpoint versions, MLB and soccer, absent from all four —
#:   and VegasInsider has no such column.  The catalogue lists it as
#:   ``'SugarHouse'`` / ``sugarhouse`` with no state suffix, while the book it
#:   names ``'BetRivers NJ'`` carries ``source_name`` ``njsugarhouse`` — Action
#:   Network's own record of the two brands being one platform, which is the
#:   independent corroboration for treating PlaySugarHouse as a mirror rather
#:   than a source.  The entry exists to fail loudly instead of being forgotten.
#:
#: Every other Pennsylvania id in this table was confirmed against that catalogue
#: on 2026-08-08 and none needed changing: 74 ``'Parx'``/``paparx``, 122
#: ``'BetRivers PA'``, 255 ``'FanDuel PA'``, 280 ``'BetMGM PA'``, 1534 ``'DK PA'``,
#: 1906 ``'Caesars PA'``, 2791 ``'Fanatics PA'``, 3547 ``'bet365 PA'``.  The one
#: Pennsylvania book Action Network carries and this table does not ask for is 912
#: ``'Betway PA'``, which the operator did not name.
#:
#: A first-party Kambi route would satisfy the rule outright for the first three,
#: since Rush Street's own books are Kambi tenants.  It does not exist under the
#: obvious tokens: ``parx``, ``betparx``, ``sugarhouse``, ``playsugarhouse`` and
#: ``mohegansun`` all answer the offering API with HTTP 429, and so does the
#: deliberately fake ``zzznotarealbook``, while ``rsiuspa`` answers 200 both
#: before and after in the same session.  **429 is this API's reply to an unknown
#: operator, not throttling** — which is worth writing down, because reading those
#: 429s as rate limiting is what makes the guessing look unfinished rather than
#: answered.  Finding these books first-party needs the token from their own web
#: application (:mod:`src.sources.research`), not more guesses.
#:
#: Illinois's list is the ten online operators on the IGB's authorized-sportsbook
#: page (recorded 2026-08-03 in ``docs/evidence/state-routing.md`` § "Illinois
#: re-probe").  All nine Action
#: Network ids were confirmed by name against the 2026-08-08 catalogue capture —
#: 3915 ``'bet365 IL'``, 282 ``'BetMGM IL'``, 262 ``'BetRivers IL'``, 279
#: ``'Caesars IL'``/``williamhillil``, 1538 ``'DK IL'``, 270 ``'FanDuel IL'``,
#: 2990 ``'Fanatics IL'``, 3646 ``'HardRock IL'``, 4601 ``'theScore Bet IL'`` —
#: and those nine are the catalogue's *complete* Illinois-tagged set.  Two entries
#: are shaped by what exists rather than what one would want:
#:
#: * ``Circa`` has **no same-licence feed at all**.  Action Network's only Circa
#:   book is the stateless id 78, re-measured 2026-08-03 returning zero rows in
#:   every league asked, and the catalogue lists no ``Circa IL``.  Circa's own
#:   site names VSiN as its odds aggregator, so ``vsin_circa`` is declared here —
#:   as ``OTHER_LICENCE``, because it is the Las Vegas line tracker, not an
#:   Illinois price.  The entry fails loudly and is meant to: ``NON_LOCAL_ONLY``
#:   on any run where VSiN answers, ``MISSING`` when it does not — both ERROR
#:   severity, both unobserved-locally.  An IGB book the operator named, with no
#:   Illinois-licence observation path, should never be quietly absent from the
#:   table.
#: * ``theScore Bet`` has **both** paths, and is the only entry here that does.
#:   The republisher is id 4601, proven on 2026-08-10 — the first request from
#:   Illinois returned 74 soccer rows across 11 fixtures (run 35, replay PASS,
#:   capture committed as ``an_thescore`` contract evidence;
#:   docs/evidence/action-network.md § "theScore Bet's Illinois id answers").
#:   The first-party route landed on 2026-08-13: ``thescore``, anonymous over
#:   plain HTTP against ``sportsbook.us-il.thescore.bet``, 573 quotes over 125
#:   events in run 11 with 0 rejections and replay PASS
#:   (docs/evidence/state-routing.md § "theScore Bet is registered").  An earlier
#:   version of this note dated that surface's first probe 2026-08-04; the
#:   evidence sits under § "Illinois re-probe — 2026-08-03" and commit 44b080f
#:   carries that date.
#:
#:   Pennsylvania names the same key on a ``TEMPLATE`` route that has never been
#:   asked over HTTP.  That cannot inflate PA's grade — ``Access.DIRECT`` is
#:   earned on ``direct_rows > 0`` — so PA stays ``SINGLE_SOURCE`` until the
#:   route actually produces from a Pennsylvania egress.
REQUIRED_BOOKS: Mapping[str, tuple[RequiredBook, ...]] = {
    "IL": (
        _required("bet365", "bet365", same=("an_bet365",), other=("vi_bet365",)),
        _required("BetMGM", "betmgm", same=("an_betmgm",), other=("vi_betmgm",)),
        _required(
            "BetRivers",
            "betrivers_kambi",
            same=("an_betrivers",),
            other=("vi_betrivers",),
        ),
        _required("Caesars", "caesars", same=("an_caesars",), other=("vi_caesars",)),
        _required("Circa", other=("vsin_circa",)),
        _required(
            "DraftKings",
            "draftkings",
            same=("an_draftkings",),
            other=("vi_draftkings",),
        ),
        _required("FanDuel", "fanduel", same=("an_fanduel",), other=("vi_fanduel",)),
        _required("Fanatics", same=("an_fanatics",), other=("vi_fanatics",)),
        _required(
            "Hard Rock Bet",
            "hardrock",
            same=("an_hardrock",),
            other=("vi_hardrock",),
        ),
        _required("theScore Bet", "thescore", same=("an_thescore",)),
    ),
    "PA": (
        _required("bet365", "bet365", same=("an_bet365",), other=("vi_bet365",)),
        _required("BetMGM", "betmgm", same=("an_betmgm",), other=("vi_betmgm",)),
        _required("betPARX", same=("an_parx",)),
        _required(
            "BetRivers",
            "betrivers_kambi",
            same=("an_betrivers",),
            other=("vi_betrivers",),
        ),
        _required("Caesars", "caesars", same=("an_caesars",), other=("vi_caesars",)),
        _required(
            "DraftKings",
            "draftkings",
            same=("an_draftkings",),
            other=("vi_draftkings",),
        ),
        _required("FanDuel", "fanduel", same=("an_fanduel",), other=("vi_fanduel",)),
        _required("Fanatics", same=("an_fanatics",), other=("vi_fanatics",)),
        _required("Mohegan Pennsylvania", same=("an_unibet",)),
        _required("PlaySugarHouse"),
        _required("theScore Bet", "thescore", same=("an_thescore",)),
    ),
}


@dataclass(frozen=True)
class BookCoverage:
    """What was actually observed for one required book in one state.

    ``republishers`` is every feed that produced a row, and
    ``local_republishers`` is the subset carrying this state's own licence.  Both
    are kept because they answer different questions: the first is what a reader
    can see on the page, the second is what the rule actually counted.  Reporting
    only the first is how a Las Vegas column came to read as corroboration.
    """

    book: str
    state: str
    access: Access
    direct_rows: int
    republishers: tuple[str, ...]
    detail: str
    local_republishers: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.access.satisfies_rule

    @property
    def non_local_republishers(self) -> tuple[str, ...]:
        local = frozenset(self.local_republishers)
        return tuple(key for key in self.republishers if key not in local)


def _agreement(quotes: Sequence[Quote], keys: Sequence[str]) -> tuple[bool, str]:
    """Do same-licence republishers of one book agree where they overlap?

    :mod:`src.distinctness` is imported inside the call, not because of a cycle:
    nothing in the ``distinctness``/``validation`` chain imports this module.  It
    used to buy a cheap import as well, and no longer does —
    :func:`_check_direct_route` pulls :mod:`src.sources.registry` at module scope
    to validate the declared routes, so importing this module already costs every
    adapter (about 15 ms to 180 ms).  That is the price of checking the table at
    import rather than at first use, which is where it has to be checked: the
    failure it catches is silent and the table is read once, at start-up.

    Too little overlap to judge is reported as agreement *with the reason
    attached* rather than as a pass or a failure: refusing the book would fault a
    thin slate, and swallowing it would let "cross-checked" mean "two feeds exist
    and nobody compared them".
    """
    from src.distinctness import MIN_SHARED_SELECTIONS, Verdict, compare_sources

    unproven: list[str] = []
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            measured = compare_sources(quotes, first, second)
            if measured.verdict is Verdict.UNDECIDED:
                unproven.append(
                    f"{first} vs {second}: {measured.compared} shared selection(s), "
                    f"fewer than the {MIN_SHARED_SELECTIONS} needed to judge"
                )
                continue
            if measured.verdict is Verdict.DISTINCT:
                return False, measured.summary()
    return True, "; ".join(unproven)


def coverage_for_state(
    quotes: Sequence[Quote],
    state: str,
    configured: Collection[str] | None = None,
) -> tuple[BookCoverage, ...]:
    """Measure every required book for ``state`` against one run's rows.

    *configured* names the sources this run actually asked for.  Left ``None``
    every feed is assumed to have been requested, which is the right reading for
    a full pass and the wrong one for a narrowed one.
    """
    key = normalize_state(state)
    required = REQUIRED_BOOKS.get(key, ())
    if not required:
        return ()

    counts: dict[str, int] = defaultdict(int)
    for quote in quotes:
        counts[quote.source] += 1
    licensing = jurisdiction(key)

    asked = None if configured is None else frozenset(configured)
    measured: list[BookCoverage] = []
    for entry in required:
        # Judgeability is decided on the feeds that could *satisfy* the rule —
        # the direct route and the local republishers — not on every feed named.
        # A run narrowed to ``--source vi_fanatics`` asked for out-of-state
        # context and nothing else, so it was never given the chance to observe
        # Fanatics locally; faulting it is the same mistake ``NOT_REQUESTED``
        # exists to prevent, and after locality became a first-class distinction
        # it would fault that run at ERROR rather than at WARNING.
        #
        # ``feeds`` being *empty* is not the same condition and must not share
        # the exemption.  ``PlaySugarHouse`` declares no direct route and no
        # republisher — the entry exists precisely to fail loudly — so an empty
        # intersection there means "nothing can be requested", not "nothing was".
        # Narrowing this set without that guard made the book ``NOT_REQUESTED``
        # on every production run, because ``collector`` always passes a
        # ``configured`` list and never ``None``: the one book in the table that
        # is guaranteed to be broken became the one book that could not report it.
        feeds = {
            *(key_ for key_, how in entry.republishers.items() if how.is_local),
            *((entry.direct,) if entry.direct else ()),
        }
        if asked is not None and feeds and not (feeds & asked):
            measured.append(
                BookCoverage(
                    book=entry.book,
                    state=key,
                    access=Access.NOT_REQUESTED,
                    direct_rows=0,
                    republishers=(),
                    detail="no feed for this book was requested on this run",
                )
            )
            continue
        direct_rows = counts.get(entry.direct, 0) if entry.direct else 0
        seen = tuple(key_ for key_ in entry.republishers if counts.get(key_, 0) > 0)
        # Only feeds carrying *this state's* licence count towards the two, and
        # they are the only ones compared against each other.  A brand's Las
        # Vegas column differing from its Pennsylvania one is not evidence about
        # either, so counting it would let an out-of-state number stand in for
        # the licence the run claims to describe.
        same = [k for k in seen if entry.republishers[k].is_local]
        elsewhere = [k for k in seen if not entry.republishers[k].is_local]
        aside = (
            f"; {', '.join(elsewhere)} saw this brand under another licence and "
            "is context only"
            if elsewhere
            else ""
        )

        if direct_rows > 0:
            access = Access.DIRECT
            detail = f"{entry.direct} stored {direct_rows} rows"
        elif len(same) >= 2:
            agree, note = _agreement(quotes, same)
            if agree and note:
                # Two local feeds, and no pair of them shared enough selections to
                # be compared.  "No disagreement found" is not "they agree", so
                # this is its own state rather than a caveat inside a tick.
                access = Access.AGREEMENT_UNPROVEN
                detail = (
                    f"{', '.join(same)} both carry {key}'s licence for this book, "
                    f"but their prices were never compared — {note}{aside}"
                )
            elif agree:
                access = Access.CROSS_CHECKED
                detail = f"corroborated by {', '.join(same)}{aside}"
            else:
                access = Access.DISAGREEING
                # ``aside`` belongs here most of all: the reader adjudicating a
                # disagreement between two local feeds needs to know a third
                # column exists, and the finding's detail is the only surface
                # that can tell them.
                detail = f"{note}{aside}"
        elif same:
            access = Access.SINGLE_SOURCE
            detail = (
                f"only {same[0]} carries {key}'s licence for this book, so nothing "
                f"can contradict it{aside}"
            )
        elif elsewhere:
            access = Access.NON_LOCAL_ONLY
            detail = (
                f"{', '.join(elsewhere)} priced this brand under another licence "
                f"and no feed carrying {key}'s licence saw it at all, so there is "
                f"no {key} price here to corroborate"
            )
        else:
            access = Access.MISSING
            unlicensed = sorted(
                source_key
                for source_key in entry.republishers
                if (route := licensing.republished.get(source_key)) is not None
                and route.status is RouteStatus.UNAVAILABLE
            )
            detail = (
                f"no {key} licence is republished for {', '.join(unlicensed)}"
                if unlicensed
                else "no configured feed produced a row"
            )
        measured.append(
            BookCoverage(
                book=entry.book,
                state=key,
                access=access,
                direct_rows=direct_rows,
                republishers=seen,
                detail=detail,
                local_republishers=tuple(same),
            )
        )
    return tuple(measured)


def check_book_coverage(
    quotes: Sequence[Quote],
    state: str | None,
    report: ValidationReport,
    configured: Collection[str] | None = None,
) -> tuple[BookCoverage, ...]:
    """Report every required book neither fetched first-party nor cross-checked.

    A state with no declared requirement warns rather than passing quietly: the
    rule is only worth having if a missing checklist is as visible as a failing
    one.

    ``GLOBAL`` is the exception, and it is silent rather than warned: it is not a
    jurisdiction, licenses nothing and has no required books to declare, so asking
    the operator to declare a checklist for it is asking for a list that cannot
    exist.  Every global run in every batch carried that warning.  An empty state
    is the same case as ``None`` — there is no jurisdiction to hold to a list.
    """
    from src.validation import Severity

    if state is None:
        return ()
    key = normalize_state(state)
    if key in ("", "GLOBAL"):
        return ()
    if key not in REQUIRED_BOOKS:
        report.add(
            Severity.WARNING,
            "book_coverage_not_declared",
            f"no required-book list is declared for {key}, so the rule that every "
            "book is either fetched first-party or seen by two agreeing "
            "republishers is not enforced there — an unchecked jurisdiction, not "
            "a clean one",
        )
        return ()

    measured = coverage_for_state(quotes, key, configured)
    # A slate-wide outage is one fact, not eleven.  Action Network's scoreboard
    # carries *today's* games and the adapters correctly refuse anything already
    # under way, so overnight there is nothing left to price: measured at 02:20
    # ET, all seven of the Pennsylvania book ids known at the time still returned
    # prices while all eighteen games were ``complete`` or ``inprogress``.  (Seven,
    # not ten: ``an_parx``, ``an_unibet`` and ``an_thescore`` were added later and
    # have never been shown to return a row — see ``src/jurisdictions.py``.  The
    # measurement must not be described as covering feeds it could not have.)
    # Reported per book that reads as eleven broken feeds, and a check that cries
    # wolf nightly is a check nobody reads.
    #
    # It collapses on the **local** feeds, because those are the only ones whose
    # silence it can explain.  ``seen`` includes the out-of-state columns, and
    # those are a different site on a different schedule: VegasInsider serves a
    # Las Vegas page whether or not Action Network's scoreboard still has a
    # pre-match game on it.  Keyed on every feed, one live ``vi_*`` column held
    # the collapse open while every ``an_*`` feed was dark.
    #
    # But collapsing is only honest while the stated cause can still be true, and
    # two kinds of evidence falsify it:
    #
    # * **First-party rows.**  Five routes storing 300 rows is proof the slate is
    #   live, so the remaining books are genuinely unobserved rather than
    #   unobservable.  ``direct_rows`` therefore breaks the collapse outright and
    #   every gap is reported per book.
    # * **Any other rows in the same run.**  Weaker evidence than a first-party
    #   row — a different board on a different clock — but enough to refuse the
    #   sentence "no pre-match games left".  Measured against *the run*, not
    #   against the declared out-of-state columns: Pinnacle, Kalshi, Polymarket,
    #   Bovada and sxbet list days ahead, which is exactly the overnight window
    #   this guard was written for, and none of them appears in
    #   ``REQUIRED_BOOKS``.  Falsifying only against the ``vi_*`` columns let a
    #   total Pennsylvania blackout print "nothing else priced the slate either"
    #   with sixty contradicting rows in the same list — and kept it a WARNING, so
    #   the run still exited 0.
    #
    #   Reporting it per book is still the eleven-findings noise this guard exists
    #   to stop, so it stays one finding and changes what that finding says: an
    #   ERROR naming the condition, rather than a WARNING asserting a cause the
    #   rows on the page contradict.
    judged = [entry for entry in measured if entry.access.is_judgeable]
    watched = {
        source_key
        for entry in REQUIRED_BOOKS.get(key, ())
        for source_key, how in entry.republishers.items()
        if how.is_local and (configured is None or source_key in configured)
    }
    # A book with no configured feed of any kind is not explained by a feed
    # outage: there is nothing to be dark.  ``PlaySugarHouse`` is the case —
    # declared required, watched by nothing — and collapsing over it swallowed the
    # one finding whose whole purpose is to stay loud.
    unfeedable = {
        entry.book
        for entry in REQUIRED_BOOKS.get(key, ())
        if entry.direct is None and not entry.republishers
    }
    locally_dark = not any(
        entry.direct_rows or entry.local_republishers for entry in judged
    )
    if judged and watched and locally_dark:
        local_feeds = watched | {
            entry.direct for entry in REQUIRED_BOOKS.get(key, ()) if entry.direct
        }
        elsewhere = sorted({quote.source for quote in quotes} - local_feeds)
        if elsewhere:
            named = ", ".join(elsewhere[:5])
            if len(elsewhere) > 5:
                named += f" and {len(elsewhere) - 5} more"
            report.add(
                Severity.ERROR,
                "book_coverage_local_feeds_dark",
                f"none of the {len(watched)} feed(s) carrying {key}'s own licence "
                f"produced a row for its {len(judged)} required book(s), while "
                f"{named} priced the same slate from outside {key}. "
                "The slate is live and this state's feeds are not, so every "
                f"required book here is unobserved in {key} — the out-of-state "
                "prices on the board are context and cannot stand in for them",
            )
        else:
            # Reachable only when the run holds **no rows from any source at all**,
            # which is what ``elsewhere`` being empty now means.  The overnight
            # window this branch was originally written for does *not* reach it:
            # Pinnacle and the exchanges list days ahead, so a real 02:20 pass
            # takes the ERROR above.  The message says the narrower true thing
            # rather than the old, broader guess about the slate.
            report.add(
                Severity.WARNING,
                "book_coverage_unobservable",
                f"none of the {len(watched)} feed(s) carrying {key}'s own licence "
                f"for its {len(judged)} required book(s) produced a row, and "
                "neither did anything else in this run — the run is empty, so "
                "corroboration could not be measured at all rather than each book "
                "having failed",
            )
        # The collapse explains the feeds that went dark.  It does not explain a
        # book nothing was ever pointed at, so those are still reported one by one.
        if not unfeedable:
            return measured
        remaining = [entry for entry in measured if entry.book in unfeedable]
    else:
        remaining = list(measured)
    for entry in remaining:
        if entry.satisfied or not entry.access.is_judgeable:
            continue
        if entry.access is Access.DISAGREEING:
            report.add(
                Severity.ERROR,
                "required_book_observations_disagree",
                f"{entry.book} in {key}: two feeds watch this book and they do "
                f"not agree — {entry.detail}. Neither is the price until the "
                "disagreement is explained",
            )
            continue
        # The locality half of the sentence is attached only where a cross-licence
        # feed actually produced a row.  On a book no feed reached at all it
        # answers a question nobody asked, and reads as though an out-of-state
        # number had been considered and set aside — which is a different, less
        # alarming state than "nothing looked at this book".
        rule = (
            f"The rule is one first-party route into {key} or two agreeing "
            f"republishers of {key}'s own licence"
        )
        if entry.non_local_republishers:
            rule += " — a feed carrying another state's licence never counts towards the two"
        report.add(
            Severity.ERROR
            if entry.access.is_unobserved_locally
            else Severity.WARNING,
            f"required_book_{entry.access.value}",
            f"{entry.book} in {key}: {entry.detail}. {rule}",
        )
    return measured


def _check_locality_declarations(
    table: Mapping[str, Sequence[RequiredBook]],
) -> None:
    """Refuse a required-book table whose locality labels cannot be true.

    ``SAME_LICENCE`` is a claim about the *feed in this state*, not a preference
    about the book: it says this republisher will be asked for a book id scoped
    to the state being collected, so what it returns is that state's own licence.

    Two ways the claim can be false, and the check has to make both impossible:

    * The feed files no per-state id at all.  VegasInsider serves
      ``/odds/las-vegas/`` with no state parameter, VSiN serves the Vegas line
      tracker, and an Action Network feed pinned to a fixed id (Circa, Fliff,
      Bovada) carries that id everywhere.  Labelling one of those ``SAME_LICENCE``
      would not make it local; it would only let a Las Vegas column count towards
      the two-feed requirement.
    * The feed files per-state ids but **not one for this state**.
      :data:`~src.jurisdictions.AN_BOOK_KEYS` is every key that files *some*
      per-state id, which is a weaker property than the one being claimed:
      ``an_bally`` has only a New Jersey book and ``an_hardrock`` only Illinois
      and New Jersey, so declaring either local in Pennsylvania asserts a licence
      that does not exist.  Membership is therefore checked against the state's
      own republished routes, not against the tuple of keys.

    Checked at import because the failure is silent and cheap to introduce: a
    one-word edit in :data:`REQUIRED_BOOKS` turns a book that is really on one
    feed into a book the report ticks as cross-checked.
    """
    errors: list[str] = []
    for state, required in table.items():
        # Checked before anything reads the jurisdiction, because this runs at
        # module scope: an unknown state would otherwise raise ``jurisdiction``'s
        # own ``KeyError`` out of ``import src.coverage``, taking the application
        # down with a message about jurisdictions and preventing
        # ``test_every_declared_state_is_a_known_jurisdiction`` from ever running.
        if normalize_state(state) not in JURISDICTIONS:
            errors.append(
                f"{state}: required books are declared for a state with no "
                "jurisdiction, so none of its routes or book ids exist"
            )
            continue
        # The key itself, not just its normalized form.  ``coverage_for_state``
        # looks the table up by ``normalize_state(state)``, so a key written
        # ``"pa"`` would satisfy every check here and then match nothing at run
        # time — the state would report ``book_coverage_not_declared`` while this
        # invariant read its entries and pronounced them consistent.
        if state not in JURISDICTIONS:
            errors.append(
                f"{state!r}: required books must be keyed exactly as the "
                f"jurisdiction is ({normalize_state(state)!r}), or "
                "coverage_for_state will not find them"
            )
            continue
        for entry in required:
            _check_direct_route(state, entry, errors)
            for source_key, corroboration in entry.republishers.items():
                carries_this_state = republishes_state_licence(state, source_key)
                if corroboration.is_local and not files_per_state_book_id(source_key):
                    errors.append(
                        f"{state}/{entry.book}: {source_key} is declared "
                        "SAME_LICENCE but files no per-state book id, so it "
                        "publishes one number for every state and cannot "
                        "corroborate a state licence"
                    )
                elif corroboration.is_local and not carries_this_state:
                    errors.append(
                        f"{state}/{entry.book}: {source_key} is declared "
                        f"SAME_LICENCE but republishes no {state} licence, so it "
                        f"can never carry a {state} price for this book"
                    )
                elif not corroboration.is_local and carries_this_state:
                    errors.append(
                        f"{state}/{entry.book}: {source_key} republishes {state}'s "
                        "own licence but is declared OTHER_LICENCE, which discards "
                        "a real local observation and can only under-report coverage"
                    )
    if errors:
        raise RuntimeError(
            "required-book locality declarations are inconsistent with the "
            "jurisdiction table:\n- " + "\n- ".join(errors)
        )


def _check_direct_route(state: str, entry: RequiredBook, errors: list[str]) -> None:
    """Is ``entry.direct`` a first-party route into this state's own book?

    ``direct`` is the **stronger** claim in a :class:`RequiredBook` and for a long
    time it was the unchecked one.  :func:`coverage_for_state` awards
    :attr:`Access.DIRECT` — which satisfies the rule outright — on ``direct_rows >
    0`` alone: no corroboration, no second feed, no locality test.  So whatever key
    is written here is believed.

    An adversarial review demonstrated the cost with two one-word edits that the
    surrounding invariant accepted: naming ``vi_fanatics`` as Fanatics' direct
    route turned ``single_source`` into ``direct``, and naming ``vi_bet365`` as
    theScore Bet's turned ``missing`` into ``direct`` — grading Pennsylvania
    coverage of one brand off a *different brand's* Las Vegas column, with zero
    findings emitted.  That is precisely the cross-licence substitution the module
    exists to refuse, entering through the one field it did not read.

    Three ways the claim can be false, all silent:

    * the key is not registered at all — nothing will ever store a row under it,
      so the book is permanently ``MISSING`` for a reason no message explains;
    * the key is a republisher — a republished board is somebody else watching the
      book, which is the definition of *not* first-party;
    * the key is not reachable from this state — offshore venues and operators
      holding no licence in this jurisdiction are the same error, and
      :func:`registry.takeable_from_state` is the one classifier that answers it.
      Without this clause the invariant accepted ``pinnacle`` as theScore Bet's
      Pennsylvania route and graded the book ``DIRECT``.  Note that reachability
      is built from licences, not from ``Jurisdiction.view_only_sources``: today
      every view-only retail book also has an ``UNAVAILABLE`` route, so the two
      agree, but a state that ever declares a live route for a book it also lists
      view-only would slip past this.

    **What it still cannot check: whether the key is the book's own venue.**
    ``entry.book`` is a display name and ``direct`` is a source key; nothing maps
    one to the other, so declaring ``kalshi`` as theScore Bet's route passes every
    clause here.  This narrows the hole to reachable first-party venues rather
    than closing it, and the remaining check is a human reading the table.
    """
    key = entry.direct
    if key is None:
        return

    from src.sources import registry

    if key not in registry.keys():
        errors.append(
            f"{state}/{entry.book}: direct route {key!r} is not a registered "
            "source, so no row can ever be stored under it and the book reports "
            "MISSING with no explanation"
        )
        return
    if key in registry.REPUBLISHED_SOURCE_KEYS:
        errors.append(
            f"{state}/{entry.book}: {key} is a republisher, which is somebody "
            "else watching this book rather than the book itself — Access.DIRECT "
            "would satisfy the rule on one republished feed, the exact thing the "
            "two-feed requirement exists to prevent"
        )
        return
    if key not in registry.takeable_from_state(state):
        errors.append(
            f"{state}/{entry.book}: {key} is not reachable from {state}, so it "
            f"cannot be a {state} leg — declaring it the direct route grades this "
            f"book DIRECT off a venue the operator cannot use from {state}"
        )


_check_locality_declarations(REQUIRED_BOOKS)


#: Scopes that widen a run past its own jurisdiction *on purpose*.
#:
#: A deny-list rather than an allow-list, so the money-safe answer is the default:
#: a scope value this module has never heard of gets the marking, and the cost of
#: being wrong is a labelled position with its count reported, not an unlabelled
#: text naming a book the operator cannot reach.
WIDENED_SCOPES: frozenset[str] = frozenset({"global", "all"})


def locality_applies(state: str | None, route_scope: str) -> bool:
    """Does rule (a) govern the positions of a run recorded like this?

    One answer for every surface.  Sharing only the *predicate* was not enough:
    ``arb``, the dashboard and ``lines`` each tested their own condition, and the
    same stored run got two position counts with nothing accounting for the gap.
    ``lines`` then grew a fourth condition — the jurisdiction alone, with no scope
    test — so ``arb`` offered a position that ``lines``, one command later, marked
    as having no leg reachable from the state.

    Two things decide it.  The state has to be a jurisdiction this build knows:
    that is what stops an unrecognised one reaching ``jurisdiction("XX")`` and
    aborting a whole pass with a ``KeyError``, and it is why ``GLOBAL`` and an
    empty jurisdiction pass through untouched.  And the scope must not be one the
    operator widened deliberately: ``--scope global`` or ``all`` from Pennsylvania
    is a request to *see* the whole board, and marking it against one state answers
    a question nobody asked.

    Everything else — including ``legacy``, the value the store backfills onto
    every row predating the ``route_scope`` column — is governed.  This is the
    reverse of the first version, which required ``route_scope == "state"``
    exactly, and the reversal is a money-path fix rather than a preference.
    ``jurisdiction`` and ``route_scope`` were added in different commits, so any
    database migrated across that gap holds rows with a real ``jurisdiction`` and
    ``route_scope='legacy'``; those are the rows ``arb --run <old>`` and the
    dashboard read.  Under the old test, one such row printed **and texted** a
    "PA" arbitrage whose two legs were an offshore book and a book that geoblocks
    the United States.  Whether a leg is reachable from a state is a fact about the
    book and the state, not about what the run claimed to have collected: a
    forgotten scope is a reason to be careful, not a reason to let an untakeable
    position reach the alert path unmarked.
    """
    return (
        normalize_state(state or "") in JURISDICTIONS
        and route_scope.strip().lower() not in WIDENED_SCOPES
    )


@dataclass(frozen=True)
class LegOrigin:
    """Where one leg's venue sits relative to the run's jurisdiction.

    :class:`LocalityMarking` answers one bit — reachable from here or not — and
    that bit is what the money rule needs.  It is not what a reader needs: "not
    reachable from IL" reads the same for a book licensed one state over, a
    federally regulated venue, and an offshore exchange no US customer can open an
    account with, and those are three different reasons to ignore a price.

    So this states the *kind* and, where the repository already knows it, the
    place.  Nothing here is a new claim about a venue: :attr:`kind` is read off
    the four reachability sets the registry already refuses to let a source skip,
    and ``other_states`` names states out of the same per-jurisdiction route
    tables that decide the collection itself.  A venue's country is deliberately
    **not** guessed — ``offshore`` is exactly what ``US_UNAVAILABLE_SOURCE_KEYS``
    asserts and no more.
    """

    kind: str
    """``in_state``, ``national``, ``other_states``, ``offshore``, ``republished``
    or ``unknown``."""

    label: str
    """Short enough for a pill beside the price: ``"IL"``, ``"national"``,
    ``"NJ, PA"``, ``"offshore"``."""

    detail: str
    """One sentence, for a tooltip or a CLI line."""

    local: bool
    """Whether this leg is takeable from the run's state — the same answer
    :meth:`LocalityMarking.leg_is_local` gives, carried alongside so a surface
    reads one object rather than joining two."""


@dataclass(frozen=True)
class LocalityMarking:
    """Which legs of a run's positions are reachable from its jurisdiction.

    The same rule as the rest of this module, applied to the output instead of the
    input: a leg unreachable from this jurisdiction is somewhere else's price, and
    every surface must say so.  Positions are shown whole — an earlier version
    (``withhold_non_local``) deleted any position with no reachable leg, which hid
    a real number behind a bare count; now the position appears with each foreign
    leg labelled, and the surfaces report how many positions have no local leg at
    all.

    "Reachable" is :func:`registry.takeable_from_state`, **not** the state's retail
    licences.  Built on the licences alone this rule called Kalshi out-of-state in
    every jurisdiction and withheld a legal, takeable position from the report, the
    dashboard and the SMS — inverting the rule it serves.  Rule (a) stops an
    unreachable price being shown as the state's own; it is not a reason to hide,
    or mark, a reachable one.

    The inverse error is just as live, which is why reachability is a set and not a
    kind: ``matchbook``, ``smarkets`` and ``sxbet`` are exchanges and unreachable,
    while ``kalshi`` and ``polymarket_us`` are CFTC-regulated venues and takeable.
    Same shape of venue, opposite answers — so the answer cannot be read off the
    kind, only off the set.

    One implementation, because there were three call sites with three different
    answers and the most permissive one was the live path that sends the text.
    Every surface builds this object through :func:`locality_marking` and asks it,
    rather than re-deriving reachability at the call site.
    """

    state: str  #: normalized; "" when the rule does not govern
    reachable: frozenset[str] | None  #: None => rule (a) does not govern this run

    licences: tuple[tuple[str, frozenset[str]], ...] = ()
    """Every jurisdiction's retail licences, as ``(state, keys)`` pairs.

    Carried rather than looked up per leg because ``state_licensed_keys`` builds
    descriptors on each call, and :meth:`leg_origin` runs once per leg per
    position.  A tuple rather than a mapping so the dataclass stays hashable.
    Empty on a marking built before this field existed, which :meth:`leg_origin`
    reads as "no licence table" and reports as ``unknown`` rather than as an
    absence of licences.
    """

    @property
    def marking(self) -> bool:
        return self.reachable is not None

    def leg_origin(self, source: str) -> LegOrigin:
        """Where *source* is, relative to this run's state.

        Ordered by what beats what.  Nationwide is tested before the state's own
        licence list because Kalshi is reachable everywhere *without* holding one,
        and calling it "IL" would state a licence that does not exist.  Offshore
        is tested before the other-states search so a republished mirror of an
        offshore book — the one overlap the registry's cover check permits —
        reports the reason its price is unusable rather than the reason it is
        second-hand.

        Works on an ungoverned run too (a ``GLOBAL`` scope, or a jurisdiction this
        build does not know).  There is no state to be inside, so ``in_state``
        cannot be the answer, but "national" and "offshore" are facts about the
        venue and stay true with nothing to compare them against.
        """
        from src.sources import registry

        local = self.leg_is_local(source)
        if source in registry.NATIONWIDE_SOURCE_KEYS:
            return LegOrigin(
                kind="national",
                label="national",
                detail=(
                    "Federally regulated and reachable from every state, holding "
                    "no state sportsbook licence."
                ),
                local=local,
            )
        licences = dict(self.licences)
        if self.state and source in licences.get(self.state, frozenset()):
            return LegOrigin(
                kind="in_state",
                label=self.state,
                detail=f"Licensed in {self.state}; this is {self.state}'s own price.",
                local=local,
            )
        if source in registry.US_UNAVAILABLE_SOURCE_KEYS:
            return LegOrigin(
                kind="offshore",
                label="offshore",
                detail=(
                    "No US access — the price is real, but not one a US customer "
                    "can take."
                ),
                local=local,
            )
        elsewhere = tuple(
            state for state, keys in self.licences
            if source in keys and state != self.state
        )
        if elsewhere:
            joined = ", ".join(elsewhere)
            return LegOrigin(
                kind="other_states",
                label=joined,
                detail=(
                    f"A US book licensed in {joined}"
                    + (f", not in {self.state}." if self.state else ".")
                ),
                local=local,
            )
        if source in registry.REPUBLISHED_SOURCE_KEYS:
            return LegOrigin(
                kind="republished",
                label="republished",
                detail=(
                    "Somebody else's board rather than a venue — no counterparty "
                    "to take the other side."
                ),
                local=local,
            )
        return LegOrigin(
            kind="unknown",
            label="unplaced",
            detail=(
                "This build's registry does not classify this venue, so where it "
                "can be reached from is unstated rather than known to be nowhere."
            ),
            local=local,
        )

    def leg_is_local(self, source: str) -> bool:
        return self.reachable is None or source in self.reachable

    def non_local_sources(self, position: _HasSources) -> tuple[str, ...]:
        """The position's foreign legs, sorted; empty when not marking."""
        if self.reachable is None:
            return ()
        return tuple(
            sorted({s for s in position.sources if s not in self.reachable})
        )

    def has_local_leg(self, position: _HasSources) -> bool:
        return self.reachable is None or bool(
            self.reachable.intersection(position.sources)
        )

    def count_without_local_leg(self, positions: Sequence[_HasSources]) -> int:
        """How many positions have no reachable leg at all.

        The count has to be reportable: labels on a page a reader has not opened
        do not warn anyone, so every surface applying the marking states how many
        positions are wholly foreign rather than leaving the labels to speak.
        """
        return sum(1 for position in positions if not self.has_local_leg(position))

    def label(self) -> str:
        """The one phrase every surface uses for a foreign leg."""
        return f"not reachable from {self.state}"


def locality_marking(state: str | None, *, route_scope: str) -> LocalityMarking:
    """Build the run's marking; the decision *whether* to mark lives here.

    Sharing only the predicate was not enough: the three callers tested three
    different conditions, and a run recorded ``jurisdiction="PA"`` with
    ``route_scope="legacy"`` — ``collect_once``'s own default — was filtered by
    ``arb`` and not by the dashboard, so one run had two position counts and no
    explanation.  Both arguments are therefore required, and neither caller may
    decide for itself.  Which runs are governed is :func:`locality_applies`, asked
    here so no surface spells the condition again.

    An empty reachable set is possible in principle — a jurisdiction licensing no
    retail book at all — and needs no branch: nothing intersects with it, so every
    leg is foreign, which is the same sentence the general case says.  In practice
    the nationwide venues are in every state's set.
    """
    from src.sources import registry

    # Built for both answers.  A run the rule does not govern still renders legs,
    # and ``leg_origin`` can say "national" or "offshore" about a venue with no
    # jurisdiction to compare it against — so the licence table is not part of
    # what ``locality_applies`` decides.
    licences = tuple(
        (state_key, registry.state_licensed_keys(state_key))
        for state_key in JURISDICTIONS
    )
    if not locality_applies(state, route_scope):
        return LocalityMarking(state="", reachable=None, licences=licences)

    normalized = normalize_state(state or "")
    return LocalityMarking(
        state=normalized,
        reachable=registry.takeable_from_state(normalized),
        licences=licences,
    )


__all__ = [
    "Access",
    "BookCoverage",
    "Corroboration",
    "LegOrigin",
    "LocalityMarking",
    "REQUIRED_BOOKS",
    "RequiredBook",
    "check_book_coverage",
    "coverage_for_state",
    "locality_applies",
    "locality_marking",
]
