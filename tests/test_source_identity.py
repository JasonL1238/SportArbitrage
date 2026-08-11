"""The identity half of a row, shared by every adapter.

Twelve of a quote's fields say *which game this is* and where the bytes came
from, and every adapter used to write all twelve out longhand.  That was twelve
chances per venue to read the home side into ``away_team`` — a swap the schema
cannot catch, because both values are valid strings.  These pins hold the shared
builder to the one assignment each field is allowed to have.
"""

from __future__ import annotations

import ast
import re
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.leagues import league as get_league
from src.participants import canonical_participant
from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection, Sport
from src.sources._common import Fixture, priced_quote

COMMENCE = datetime(2026, 7, 28, 22, 41, tzinfo=UTC)
FETCHED = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

#: `_common.py` declares the shared field; pinnacle keeps a struct of its own, which
#: is not a `Fixture`, does not satisfy `FixtureIdentity` (its identifier is
#: `matchup_id`), and whose own side field is required and so can never be absent.
_NOT_VENUE_VOCABULARY = {"_common.py", "pinnacle.py"}


def _adapter_sources() -> list[tuple[str, ast.Module]]:
    """Every venue adapter, parsed."""
    sources = Path(__file__).resolve().parents[1] / "src" / "sources"
    parsed = [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(sources.glob("*.py"))
        if path.name not in _NOT_VENUE_VOCABULARY
    ]
    assert len(parsed) > 20, "the adapter sweep found almost nothing; it would pass empty"
    return parsed


def _fixture() -> Fixture:
    competition = get_league("MLB")
    home = canonical_participant("Miami Marlins", competition)
    away = canonical_participant("Philadelphia Phillies", competition)
    assert home is not None and away is not None
    return Fixture(
        event_id="evt-1",
        sport=Sport.BASEBALL,
        competition=competition,
        home=home,
        away=away,
        book_home_key=home.key,
        commence_time=COMMENCE,
        base_key="MLB-PHI@MLB-MIA",
    )


def _raw(endpoint: str = "odds", body: str = "{}") -> RawResponse:
    """``ref`` is computed from the envelope, so vary what it is computed from."""
    return RawResponse(
        source="testbook",
        endpoint=endpoint,
        url="https://example.invalid/odds",
        status_code=200,
        body=body,
        fetched_at=FETCHED,
    )


def _priced(**overrides):
    priced = dict(
        market=Market.MONEYLINE,
        period=Period.FULL_GAME,
        selection=Selection.HOME,
        decimal_odds=1.9,
        american_odds=-111,
        implied_probability=1 / 1.9,
        status=QuoteStatus.ACTIVE,
    )
    priced.update(overrides)
    return priced


class TestTheIdentityComesFromTheFixture:
    def test_every_identity_field_is_read_from_the_one_resolved_event(self) -> None:
        fixture = _fixture()
        raw = _raw()

        quote = priced_quote(
            fixture, source="testbook", raw=raw, event_key="MLB-PHI@MLB-MIA:2026-07-28",
            **_priced(),
        )

        assert quote.source == "testbook"
        assert quote.observed_at == FETCHED
        assert quote.raw_ref == raw.ref
        assert quote.sport is Sport.BASEBALL
        assert quote.league == "MLB"
        assert quote.event_key == "MLB-PHI@MLB-MIA:2026-07-28"
        assert quote.source_event_id == "evt-1"
        # The pair that a longhand copy gets to transpose.
        assert quote.home_participant == fixture.home.key
        assert quote.away_participant == fixture.away.key
        assert quote.home_team == fixture.home.name
        assert quote.away_team == fixture.away.name
        assert quote.home_team == "Miami Marlins"
        assert quote.away_team == "Philadelphia Phillies"
        assert quote.commence_time == COMMENCE

    def test_the_stored_bytes_named_are_the_ones_handed_in(self) -> None:
        """sxbet prices one envelope while resolving identity from another, so
        which ``RawResponse`` supplies ``observed_at``/``raw_ref`` is a choice
        the caller makes and this helper must not quietly make for it."""
        priced_raw = _raw("prices", body='{"price": 1}')
        identity_raw = _raw("events", body='{"event": 1}')

        quote = priced_quote(
            _fixture(), source="testbook", raw=priced_raw, event_key="k",
            identity_raw_ref=identity_raw.ref, **_priced(),
        )

        assert quote.raw_ref == priced_raw.ref
        assert quote.identity_raw_ref == identity_raw.ref

    def test_sport_defaults_to_the_fixtures_own_and_is_overridable(self) -> None:
        """The exchanges' fixtures carry no ``sport`` of their own — theirs comes
        off the competition — so the default must be exactly a default.

        Asserted against a sport the fixture does *not* carry, because a passed
        value that merely agrees with ``fixture.sport`` cannot tell an honoured
        override apart from an ignored one.
        """
        fixture = _fixture()

        assert priced_quote(
            fixture, source="s", raw=_raw(), event_key="k", **_priced()
        ).sport is Sport.BASEBALL
        assert priced_quote(
            fixture, source="s", raw=_raw(), event_key="k", sport=Sport.SOCCER,
            **_priced(),
        ).sport is Sport.SOCCER

    def test_a_fixture_without_a_sport_fails_where_the_caller_can_see_it(self) -> None:
        @dataclass(frozen=True)
        class _Sportless:
            event_id = "evt-1"
            competition = get_league("MLB")
            home = canonical_participant("Miami Marlins", get_league("MLB"))
            away = canonical_participant("Philadelphia Phillies", get_league("MLB"))
            commence_time = COMMENCE

        with pytest.raises(AttributeError, match="sport"):
            priced_quote(_Sportless(), source="s", raw=_raw(), event_key="k", **_priced())

    def test_an_identity_field_passed_as_a_price_is_refused_loudly(self) -> None:
        """Written longhand, naming a field twice was a ``SyntaxError`` that CI's
        ``compileall`` caught.  Through ``**priced`` it would be a ``TypeError``
        — which every adapter catches and files as one ``invalid_quote`` row, so
        a whole venue could reject itself quietly.  Raise past their handlers."""
        with pytest.raises(RuntimeError, match="fixture's to fill"):
            priced_quote(
                _fixture(), source="s", raw=_raw(), event_key="k",
                **_priced(home_team="Somebody Else"),
            )

    def test_the_guard_covers_every_field_that_can_reach_it(self) -> None:
        """The set is exactly what ``**priced`` can carry — no more, no less.

        A field that is also a named parameter never lands in ``**priced`` at all
        (Python raises its own duplicate-argument ``TypeError`` at the call site),
        and listing one here would claim a protection this cannot give.
        """
        import ast
        from inspect import getsource, signature

        from src.sources._common import _IDENTITY_FIELDS

        # Read both halves off the function itself.  Restating either one here would
        # let a thirteenth identity field be added with the guard silently not
        # covering it — and for a venue with no committed capture, nothing else
        # would notice.
        call = next(
            node
            for node in ast.walk(ast.parse(getsource(priced_quote)))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Quote"
        )
        filled = {keyword.arg for keyword in call.keywords if keyword.arg}
        named = set(signature(priced_quote).parameters)

        assert filled, "the Quote call was not found; this test would pass vacuously"
        assert _IDENTITY_FIELDS == filled - named

        for field in sorted(_IDENTITY_FIELDS):
            with pytest.raises(RuntimeError):
                priced_quote(
                    _fixture(), source="s", raw=_raw(), event_key="k",
                    **_priced(**{field: "whatever"}),
                )

    def test_the_refusal_names_the_venue_and_the_field(self) -> None:
        with pytest.raises(RuntimeError) as caught:
            priced_quote(
                _fixture(), source="draftkings", raw=_raw(), event_key="k",
                **_priced(league="NBA"),
            )

        assert "draftkings" in str(caught.value)
        assert "league" in str(caught.value)

    def test_a_misspelled_pricing_keyword_still_fails_at_construction(self) -> None:
        """The pricing half goes through ``**priced`` untouched, so a typo must
        raise here exactly as it did when the call was written out longhand —
        adapters catch that as ``invalid_quote`` and reject the row."""
        with pytest.raises((TypeError, ValueError)):
            priced_quote(
                _fixture(), source="s", raw=_raw(), event_key="k",
                **_priced(decimal_oddz=1.9),
            )


class TestTheSharedFixture:
    def test_it_is_frozen_so_a_price_cannot_edit_the_event_it_hangs_on(self) -> None:
        fixture = _fixture()
        with pytest.raises(FrozenInstanceError):
            fixture.event_id = "evt-2"  # type: ignore[misc]

    def test_a_venue_may_add_its_own_field_without_restating_the_shared_ones(self) -> None:
        @dataclass(frozen=True)
        class _VenueFixture(Fixture):
            market_prefix: str

        fixture = _fixture()
        extended = _VenueFixture(
            **{f: getattr(fixture, f) for f in Fixture.__dataclass_fields__},
            market_prefix="baseball",
        )

        assert extended.market_prefix == "baseball"
        assert isinstance(extended, Fixture)
        # And it still builds a row whose identity is the shared one.
        quote = priced_quote(extended, source="s", raw=_raw(), event_key="k", **_priced())
        assert quote.home_participant == fixture.home.key

    def test_the_venues_that_add_a_field_inherit_the_shared_ones(self) -> None:
        """Each of these restated the whole class to carry one extra field.

        Asserted on the field names rather than on ``issubclass`` alone: a venue
        that subclasses and then redeclares ``home``/``away`` locally is a
        subclass by inheritance and a copy in fact, which is the thing being
        prevented.
        """
        from src.sources import betmgm, betrivers_kambi, cloudbet, matchbook

        shared = set(Fixture.__dataclass_fields__)
        extra = {
            betmgm: {"participant_ids"},
            betrivers_kambi: {"marker"},
            cloudbet: {"market_prefix"},
            matchbook: {"marker"},
        }
        for module, added in extra.items():
            venue_fixture = module._Fixture
            assert issubclass(venue_fixture, Fixture), module.__name__
            fields = set(venue_fixture.__dataclass_fields__)
            assert fields - shared == added, module.__name__
            for name in shared:
                assert venue_fixture.__dataclass_fields__[name] is (
                    Fixture.__dataclass_fields__[name]
                ), f"{module.__name__} redeclares {name}"


class TestTheSideTheBookCalledHomeIsNeverGuessed:
    """``book_home_key`` is optional, and absent must not read as *home*.

    Making it optional — so an aggregator column that takes no side could use this
    class at all — turned "this venue never set it" from a ``TypeError`` at
    construction into a comparison against ``None``.  That comparison is False, the
    expression falls through to the home competitor, and the price lands on the
    wrong player: the exact fault the venues' own notes are written to prevent.
    """

    def test_asking_for_an_absent_side_refuses_and_names_the_event(self) -> None:
        sideless = replace(_fixture(), book_home_key=None)

        with pytest.raises(ValueError, match="evt-1"):
            _ = sideless.book_home

    def test_a_stated_side_is_returned_unchanged(self) -> None:
        fixture = _fixture()

        assert fixture.book_home == fixture.home.key

    def test_the_other_competitor_is_the_one_the_book_did_not_name(self) -> None:
        fixture = _fixture()

        assert fixture.book_away == fixture.away.key
        # And with the book naming *our* away side as its home, it is the other one.
        assert replace(fixture, book_home_key=fixture.away.key).book_away == (
            fixture.home.key
        )

        with pytest.raises(ValueError, match="evt-1"):
            _ = replace(fixture, book_home_key=None).book_away

    @pytest.mark.parametrize("selection", [Selection.HOME, Selection.AWAY])
    def test_translating_a_side_refuses_rather_than_falling_through_to_home(
        self, selection: Selection
    ) -> None:
        """Six adapters wrote this translation out by hand and read the raw
        attribute.  On an absent side the comparison against ``None`` is False, the
        expression falls through to the home competitor, and the price lands on the
        wrong player — with no rejected row and no skip count to notice it by."""
        fixture = _fixture()

        assert fixture.our_side(selection) is selection

        with pytest.raises(ValueError, match="evt-1"):
            replace(fixture, book_home_key=None).our_side(selection)

    def test_a_book_that_orders_the_two_sides_the_other_way_is_translated(self) -> None:
        """The case the whole method exists for: tennis, where the league has no
        real home side, `orient` imposes one by participant key, and the book
        orders the two names however it likes."""
        flipped = replace(_fixture(), book_home_key=_fixture().away.key)

        assert flipped.our_side(Selection.HOME) is Selection.AWAY
        assert flipped.our_side(Selection.AWAY) is Selection.HOME

    def test_an_adapter_sets_the_raw_field_and_never_reads_it(self) -> None:
        """An adapter states which side the book called home; it asks through the
        property.  That is the whole rule, and it is what makes the refusal reachable.

        The translation this replaced was written six different ways across seven
        adapters.  Left in place, a seventh spelling is what a new adapter copies — and
        the fault it produces is not a missing row but two mirrored prices that the
        report publishes as a guaranteed profit.
        """
        # Read as syntax rather than as text: the rule is about *reading the
        # attribute*, and a comment that names the field while explaining the rule is
        # not a violation of it.
        offenders = sorted(
            name
            for name, module in _adapter_sources()
            for node in ast.walk(module)
            if isinstance(node, ast.Attribute)
            and node.attr == "book_home_key"
            and isinstance(node.ctx, ast.Load)
        )

        assert offenders == []

    def test_no_venue_keeps_a_second_name_for_the_side_the_book_called_home(self) -> None:
        """Pinning one field name only enforces the rule for that field name.

        SX spelled the same fact ``book_one_key`` on a subclass of its own, so its
        hand-written translation was invisible to the pin above while being exactly
        what that pin exists to prevent.  Pinnacle is the one venue outside all of
        this: its struct is not a `Fixture`, its own field is required and so can
        never be absent, and it is tested separately.
        """
        alias = re.compile(r"^book_(?!home_key$)\w*_key$")
        offenders = sorted(
            f"{name}:{spelling}"
            for name, module in _adapter_sources()
            for node in ast.walk(module)
            for spelling in [getattr(node, "attr", None) or getattr(node, "id", None) or ""]
            if alias.match(spelling)
        )

        assert offenders == []
