from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.config import FrozenRiskSettings, RunMode
from kraken_knight.ledger import Ledger, LedgerConflict, LedgerError

CONFIGURATION_HASH = FrozenRiskSettings().fingerprint
INPUT_DATA_HASH = "a" * 64
RECORDED_AT = datetime(2026, 8, 31, 0, 15, tzinfo=UTC)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    result = Ledger(tmp_path / "state" / "kraken-knight.sqlite3")
    result.initialize()
    return result


def append_decision(
    ledger: Ledger,
    *,
    account_id: str = "dedicated-btc-cad",
    code_version: str = "test-code-version",
    configuration_hash: str = CONFIGURATION_HASH,
    details: Mapping[str, object] | None = None,
    input_data_hash: str = INPUT_DATA_HASH,
    outcome: str = "BUY",
    pair: str = "BTC/CAD",
    recorded_at: datetime = RECORDED_AT,
    run_mode: RunMode = RunMode.SHADOW,
    strategy_date: date = date(2026, 8, 30),
    strategy_id: str = "btc_cad_daily_momentum_v1",
) -> str:
    decision_details = (
        {"momentum": "0.12", "reason_codes": ["ELIGIBLE"]} if details is None else details
    )
    return ledger.append_daily_decision(
        account_id=account_id,
        code_version=code_version,
        configuration_hash=configuration_hash,
        details=decision_details,
        input_data_hash=input_data_hash,
        outcome=outcome,
        pair=pair,
        recorded_at=recorded_at,
        run_mode=run_mode,
        strategy_date=strategy_date,
        strategy_id=strategy_id,
    )


def test_initialize_enables_wal_and_is_idempotent(ledger: Ledger) -> None:
    ledger.initialize()
    status = ledger.status()

    assert status["initialized"] is True
    assert status["schema_version"] == 2
    assert status["journal_mode"] == "wal"
    assert status["integrity"] == "ok"


def test_status_does_not_create_an_uninitialized_database(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "kraken-knight.sqlite3"
    status = Ledger(path).status()

    assert status["initialized"] is False
    assert not path.exists()
    assert not path.parent.exists()


def test_daily_decision_retry_is_deterministic_and_idempotent(ledger: Ledger) -> None:
    first_id = append_decision(ledger)
    second_id = append_decision(
        ledger,
        recorded_at=datetime(2026, 8, 31, 0, 16, tzinfo=UTC),
    )

    assert first_id == second_id
    assert first_id.startswith("decision_")
    assert len(first_id) == len("decision_") + 64
    assert ledger.status()["decision_count"] == 1


def test_conflicting_replay_for_same_daily_scope_fails_closed(ledger: Ledger) -> None:
    append_decision(ledger)

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        append_decision(ledger, outcome="NO_REBALANCE")


def test_changed_input_cannot_create_a_second_daily_economic_action(ledger: Ledger) -> None:
    append_decision(ledger)

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        append_decision(ledger, input_data_hash="b" * 64)


def test_run_mode_cannot_create_a_second_daily_economic_action(ledger: Ledger) -> None:
    append_decision(ledger, run_mode=RunMode.SHADOW)

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        append_decision(ledger, run_mode=RunMode.LIVE)


def test_pair_label_cannot_create_a_second_daily_economic_action(ledger: Ledger) -> None:
    append_decision(ledger, pair="BTC/CAD")

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        append_decision(ledger, pair="XBTCAD")


def test_order_intent_retry_is_deterministic_and_idempotent(ledger: Ledger) -> None:
    decision_id = append_decision(ledger)
    first_id = ledger.append_order_intent(
        decision_id=decision_id,
        details={"reason_code": "REBALANCE"},
        intent_index=0,
        limit_price=Decimal("123456.70"),
        order_type="limit",
        post_only=True,
        quantity=Decimal("0.001"),
        recorded_at=RECORDED_AT,
        side="buy",
    )
    second_id = ledger.append_order_intent(
        decision_id=decision_id,
        details={"reason_code": "REBALANCE"},
        intent_index=0,
        limit_price="123456.70",
        order_type="limit",
        post_only=True,
        quantity="0.001",
        recorded_at=datetime(2026, 8, 31, 0, 16, tzinfo=UTC),
        side="buy",
    )

    assert first_id == second_id
    assert first_id.startswith("intent_")
    assert ledger.status()["intent_count"] == 1
    with sqlite3.connect(ledger.path) as connection:
        client_order_id = str(
            connection.execute(
                "SELECT client_order_id FROM order_intents WHERE intent_id = ?",
                (first_id,),
            ).fetchone()[0]
        )
    assert client_order_id.startswith("kk")
    assert client_order_id.isascii()
    assert len(client_order_id) == 18


def test_conflicting_order_intent_index_fails_closed(ledger: Ledger) -> None:
    decision_id = append_decision(ledger)
    ledger.append_order_intent(
        decision_id=decision_id,
        intent_index=0,
        limit_price="100000",
        order_type="limit",
        quantity="0.001",
        side="buy",
    )

    with pytest.raises(LedgerConflict, match="different immutable order intent"):
        ledger.append_order_intent(
            decision_id=decision_id,
            intent_index=0,
            limit_price="99999",
            order_type="limit",
            quantity="0.001",
            side="buy",
        )


def test_v1_rejects_a_second_economic_intent_index(ledger: Ledger) -> None:
    decision_id = append_decision(ledger)

    with pytest.raises(ValueError, match="exactly one economic intent"):
        ledger.append_order_intent(
            decision_id=decision_id,
            intent_index=1,
            limit_price="100000",
            order_type="limit",
            quantity="0.001",
            side="buy",
        )


@pytest.mark.parametrize(
    ("order_type", "limit_price", "time_in_force", "post_only", "message"),
    [
        ("market", None, "GTC", False, "bounded limit"),
        ("limit", None, "GTC", False, "limit_price is required"),
        ("limit", "100000", "GTD", False, "GTC or IOC"),
        ("limit", "100000", "IOC", True, "post-only"),
    ],
)
def test_order_intent_rejects_unsupported_or_unbounded_combinations(
    ledger: Ledger,
    order_type: str,
    limit_price: str | None,
    time_in_force: str,
    post_only: bool,
    message: str,
) -> None:
    decision_id = append_decision(ledger)

    with pytest.raises(ValueError, match=message):
        ledger.append_order_intent(
            decision_id=decision_id,
            intent_index=0,
            limit_price=limit_price,
            order_type=order_type,
            post_only=post_only,
            quantity="0.001",
            side="buy",
            time_in_force=time_in_force,
        )


def test_initialize_rejects_a_weakened_database_claiming_current_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "weak.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE daily_decisions (decision_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(LedgerError, match="schema"):
        Ledger(path).initialize()


def test_order_intent_must_reference_a_stored_decision(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="unknown decision"):
        ledger.append_order_intent(
            decision_id="decision_" + ("f" * 64),
            intent_index=0,
            limit_price="100000",
            order_type="limit",
            quantity="0.001",
            side="buy",
        )


def test_database_triggers_reject_update_and_delete(ledger: Ledger) -> None:
    decision_id = append_decision(ledger)
    intent_id = ledger.append_order_intent(
        decision_id=decision_id,
        intent_index=0,
        limit_price="100000",
        order_type="limit",
        quantity="0.001",
        side="buy",
    )

    with sqlite3.connect(ledger.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE daily_decisions SET outcome = 'SELL' WHERE decision_id = ?",
                (decision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM order_intents WHERE intent_id = ?", (intent_id,))


@pytest.mark.parametrize("field_name", ["api_key", "auth-token", "password_hint"])
def test_secret_shaped_details_are_rejected(ledger: Ledger, field_name: str) -> None:
    with pytest.raises(ValueError, match="secret-bearing"):
        append_decision(ledger, details={field_name: "not-stored"})


def test_secret_bearing_url_value_is_rejected_recursively(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="secret-bearing values"):
        append_decision(
            ledger,
            details={"source": {"request_urls": ["https://example.invalid/data?key=redacted"]}},
        )


def test_hashes_and_utc_timestamp_are_validated(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="configuration_hash"):
        append_decision(ledger, configuration_hash="not-a-digest")
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        append_decision(ledger, recorded_at=datetime(2026, 8, 31, 0, 15))
