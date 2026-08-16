"""The durable, hand-entered bet ledger."""
from __future__ import annotations

import sqlite3

import pytest

from src.betlog import BetLog, BetLogError, slip_from_payload


def _arb_payload() -> dict:
    return {
        "kind": "arb",
        "sport": "baseball",
        "league": "mlb",
        "event_key": "mlb:chc-stl:2026-08-06",
        "away_team": "Chicago Cubs",
        "home_team": "St. Louis Cardinals",
        "market": "moneyline",
        "period": "full_game",
        "margin_pct": 1.25,
        "expected_profit": 2.50,
        "source_run_id": 17,
        "placed_at": "2026-08-06T15:30:00Z",
        "legs": [
            {
                "book": "fanduel",
                "selection": "Chicago Cubs",
                "american_odds": 120,
                "stake": 45.45,
            },
            {
                "book": "draftkings",
                "selection": "St. Louis Cardinals",
                "decimal_odds": 2.0,
                "stake": 50.00,
            },
        ],
    }


def test_records_scraped_snapshot_and_exact_totals(tmp_path) -> None:
    path = tmp_path / "bets.sqlite3"
    with BetLog(path) as log:
        slip_id = log.record(slip_from_payload(_arb_payload()))
        payload = log.payload()

    assert path.exists()
    slip = payload["slips"][0]
    assert slip["id"] == slip_id
    assert slip["placed_at"] == "2026-08-06T15:30:00Z"
    assert slip["home_team"] == "St. Louis Cardinals"
    assert slip["source_run_id"] == 17
    assert slip["stake"] == 95.45
    assert slip["expected_profit"] == 2.50
    assert slip["status"] == "pending"
    assert slip["legs"][0]["decimal_odds"] == pytest.approx(2.2)
    assert slip["legs"][0]["to_return"] == 99.99
    assert slip["legs"][1]["american_odds"] == 100
    assert payload["summary"] == {
        "slip_count": 1,
        "leg_count": 2,
        "pending_legs": 2,
        "settled_legs": 0,
        "unpriced_legs": 0,
        "staked": 95.45,
        "settled_stake": 0.0,
        "returned": 0.0,
        "profit": 0.0,
        "open_stake": 95.45,
        "roi_pct": None,
        "record": {"won": 0, "lost": 0, "push": 0, "void": 0, "cashout": 0},
        "by_book": [
            {"book": "draftkings", "bets": 1, "staked": 50.0,
             "returned": 0.0, "profit": 0.0, "open": 50.0, "pending": 1},
            {"book": "fanduel", "bets": 1, "staked": 45.45,
             "returned": 0.0, "profit": 0.0, "open": 45.45, "pending": 1},
        ],
    }


def test_editing_and_settlement_recompute_money(tmp_path) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_arb_payload()))
        first, second = log.slip(slip_id)["legs"]

        won = log.update_leg(first["id"], {"status": "won"})
        assert won["legs"][0]["returned"] == 99.99
        assert won["legs"][0]["profit"] == 54.54

        resized = log.update_leg(first["id"], {"stake": 50, "american_odds": 150})
        assert resized["legs"][0]["to_return"] == 125.0
        assert resized["legs"][0]["returned"] == 125.0

        lost = log.update_leg(second["id"], {"status": "lost"})
        assert lost["status"] == "settled"
        assert lost["profit"] == 25.0
        assert lost["open_stake"] == 0.0

        custom = log.update_leg(first["id"], {"returned": 117.25})
        repriced = log.update_leg(first["id"], {"american_odds": 200})
        assert custom["legs"][0]["returned"] == 117.25
        assert repriced["legs"][0]["to_return"] == 150.0
        assert repriced["legs"][0]["returned"] == 117.25


def test_cashout_without_amount_is_named_but_not_invented(tmp_path) -> None:
    payload = _arb_payload()
    payload["kind"] = "single"
    payload["legs"] = [payload["legs"][0]]
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(payload))
        leg_id = log.slip(slip_id)["legs"][0]["id"]
        slip = log.update_leg(leg_id, {"status": "cashout"})
        summary = log.payload()["summary"]

    assert slip["unpriced_legs"] == 1
    assert slip["profit"] is None
    assert summary["unpriced_legs"] == 1
    assert summary["settled_stake"] == 0.0
    assert summary["profit"] == 0.0


def test_delete_removes_slip_and_legs(tmp_path) -> None:
    path = tmp_path / "bets.sqlite3"
    with BetLog(path) as log:
        slip_id = log.record(slip_from_payload(_arb_payload()))
        assert log.delete_slip(slip_id)
        assert log.slip(slip_id) is None
        assert not log.delete_slip(slip_id)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM bet_leg").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change, message",
    [
        ({"stake": 0}, "stake must be more than zero"),
        ({"american_odds": 0}, "american odds of 0"),
        ({"status": "maybe"}, "unknown status"),
    ],
)
def test_rejects_invalid_edits_without_changing_the_leg(tmp_path, change, message) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_arb_payload()))
        before = log.slip(slip_id)["legs"][0]
        with pytest.raises(BetLogError, match=message):
            log.update_leg(before["id"], change)
        assert log.slip(slip_id)["legs"][0] == before


# ── promo awareness ──────────────────────────────────────────────────────────
# A bonus bet is stake-not-returned credit.  Before these columns existed the
# ledger booked it as cash: payout overstated by the whole stake, credit
# counted as bankroll, and a converted $150 promo reading as a $150 loss.


def _promo_payload() -> dict:
    return {
        "kind": "promo",
        "sport": "soccer",
        "market": "moneyline",
        "promo_source": "tl_betmgm",
        "promo_offer_id": "bet-10-get-150",
        "legs": [
            {"book": "betmgm", "selection": "Away", "decimal_odds": 8.5,
             "stake": 150.00, "stake_kind": "bonus"},
            {"book": "smarkets", "selection": "Draw", "decimal_odds": 6.4,
             "stake": 178.74, "stake_kind": "cash"},
        ],
    }


def test_a_bonus_leg_returns_winnings_only(tmp_path) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_promo_payload()))
        slip = log.slip(slip_id)
        bonus, cash = slip["legs"]
        # 150 × (8.5 − 1) = 1125, not 150 × 8.5 = 1275.
        assert bonus["stake_kind"] == "bonus"
        assert bonus["to_return"] == 1125.00
        assert cash["stake_kind"] == "cash"
        assert cash["to_return"] == round(178.74 * 6.4, 2)


def test_bonus_stakes_are_not_bankroll(tmp_path) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_promo_payload()))
        slip = log.slip(slip_id)
        # Only the cash hedge counts as money at risk.
        assert slip["stake"] == 178.74
        assert slip["open_stake"] == 178.74
        assert slip["promo_source"] == "tl_betmgm"
        assert slip["promo_offer_id"] == "bet-10-get-150"

        payload = log.payload()
        assert payload["summary"]["staked"] == 178.74


def test_a_won_bonus_legs_whole_return_is_profit(tmp_path) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_promo_payload()))
        legs = log.slip(slip_id)["legs"]
        updated = log.update_leg(legs[0]["id"], {"status": "won"})
        bonus = updated["legs"][0]
        assert bonus["returned"] == 1125.00
        assert bonus["profit"] == 1125.00  # nothing of the operator's was staked


def test_a_pushed_bonus_leg_returns_no_cash(tmp_path) -> None:
    """The stake was credit; a push hands the credit back, not money.  If the
    book re-credits the bonus bet, the re-run is logged as its own slip."""
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_promo_payload()))
        legs = log.slip(slip_id)["legs"]
        updated = log.update_leg(legs[0]["id"], {"status": "push"})
        assert updated["legs"][0]["returned"] == 0.0
        # The cash hedge's push still returns its stake.
        updated = log.update_leg(legs[1]["id"], {"status": "push"})
        assert updated["legs"][1]["returned"] == 178.74


def test_editing_a_bonus_legs_price_rederives_the_bonus_payout(tmp_path) -> None:
    with BetLog(tmp_path / "bets.sqlite3") as log:
        slip_id = log.record(slip_from_payload(_promo_payload()))
        legs = log.slip(slip_id)["legs"]
        updated = log.update_leg(legs[0]["id"], {"decimal_odds": 5.0})
        assert updated["legs"][0]["to_return"] == 600.00  # 150 × (5 − 1)


def test_unknown_stake_kind_is_refused(tmp_path) -> None:
    payload = _promo_payload()
    payload["legs"][0]["stake_kind"] = "voucher"
    with BetLog(tmp_path / "bets.sqlite3") as log:
        with pytest.raises(BetLogError, match="unknown stake kind"):
            log.record(slip_from_payload(payload))


def test_v1_ledger_grows_the_promo_columns(tmp_path) -> None:
    """A file written before promo awareness opens, migrates, and records."""
    db = tmp_path / "bets.sqlite3"
    with BetLog(db) as log:
        log.record(slip_from_payload(_arb_payload()))
    # Regress the file to v1 shape: drop the new columns, stamp the version.
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE bet_leg DROP COLUMN stake_kind")
    conn.execute("ALTER TABLE bet_slip DROP COLUMN promo_source")
    conn.execute("ALTER TABLE bet_slip DROP COLUMN promo_offer_id")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with BetLog(db) as log:
        # The old row reads as cash, and a promo slip records cleanly.
        old = log.slips()[-1]
        assert all(leg["stake_kind"] == "cash" for leg in old["legs"])
        slip_id = log.record(slip_from_payload(_promo_payload()))
        assert log.slip(slip_id)["legs"][0]["stake_kind"] == "bonus"
