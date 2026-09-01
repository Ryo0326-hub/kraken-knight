import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.config import SecretValue, Settings
from kraken_knight.kraken_read import (
    ApiKeyInfoSnapshot,
    AssetPair,
    AssetPairSnapshot,
    BalanceSnapshot,
    ClosedOrdersPage,
    CurrentFee,
    ExtendedBalance,
    KrakenSystemStatus,
    LedgerPage,
    LedgerRecord,
    OpenOrdersSnapshot,
    OrderQuerySnapshot,
    OrderRecord,
    ServerTimeSnapshot,
    SystemStatusSnapshot,
    TradeHistoryPage,
    TradeRecord,
    TradeVolumeSnapshot,
    WalletAccount,
    WalletAccountsSnapshot,
)
from kraken_knight.ledger import Ledger
from kraken_knight.provenance import sha256_json
from kraken_knight.reconcile_job import (
    MAX_LEGACY_HINT_BYTES,
    MAX_LEGACY_HINTS,
    READ_ONLY_PERMISSIONS,
    PublicRequestPacer,
    ReconciliationJobError,
    _ledger_balances_match,
    _linked_trade_fees,
    _reconciliation_lease,
    discover_read_only_account_id,
    execute_read_only_reconciliation,
    funding_manifest_hash,
    legacy_manifest_hash,
    load_legacy_hints,
)
from kraken_knight.reconciliation import LegacySubmissionHint, ReconciliationStatus, Side

OBSERVED_AT = datetime(2026, 8, 31, 16, tzinfo=UTC)
EXPECTED_IP = "203.0.113.10"
EXPECTED_KEY_NAME = "kraken-knight-read-only"
EXPECTED_ACCOUNT_ID = "WX6V-JUKW-KKPB-QE36"
RAW_API_KEY = "raw-api-key-must-never-be-persisted"
RAW_API_SECRET = "raw-api-secret-must-never-be-persisted"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path,
        kraken_api_key=SecretValue(RAW_API_KEY),
        kraken_api_secret=SecretValue(RAW_API_SECRET),
        expected_kraken_key_name=EXPECTED_KEY_NAME,
        expected_kraken_ip=EXPECTED_IP,
        expected_kraken_account_id=EXPECTED_ACCOUNT_ID,
        expected_legacy_manifest_hash=legacy_manifest_hash(_legacy_hints()),
        expected_funding_manifest_hash=funding_manifest_hash((_deposit_entry(),)),
        cutover_quiesced=True,
    )


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "kraken-knight.sqlite3")
    ledger.initialize()
    return ledger


def _order(index: int, *, status: str = "closed") -> OrderRecord:
    opened_at = OBSERVED_AT - timedelta(days=10 - index)
    is_open = status in {"open", "pending"}
    quantity = Decimal("0.001")
    return OrderRecord(
        order_id=f"ORDER-{index}",
        client_order_id=f"legacy-client-{index}",
        reference_id=None,
        user_reference=None,
        status=status,
        reason=None,
        opened_at=opened_at,
        closed_at=None if is_open else opened_at + timedelta(minutes=10),
        starts_at=None,
        expires_at=None,
        pair="XXBTZCAD",
        side="buy",
        order_type="limit",
        requested_price=Decimal("100000"),
        secondary_price=Decimal("0"),
        leverage="none",
        volume=quantity,
        executed_volume=Decimal("0") if is_open else quantity,
        cost=Decimal("0") if is_open else Decimal("100"),
        fee=Decimal("0") if is_open else Decimal("0.26"),
        average_price=Decimal("0") if is_open else Decimal("100000"),
        stop_price=Decimal("0"),
        limit_price=Decimal("0"),
        flags=(),
        trade_ids=() if is_open else (f"TRADE-{index}",),
    )


def _trade(index: int) -> TradeRecord:
    order = _order(index)
    return TradeRecord(
        trade_id=f"TRADE-{index}",
        order_id=order.order_id,
        position_id=None,
        pair="XXBTZCAD",
        executed_at=order.opened_at + timedelta(minutes=5),
        side="buy",
        order_type="limit",
        price=Decimal("100000"),
        cost=Decimal("100"),
        fee=Decimal("0.26"),
        volume=Decimal("0.001"),
        margin=Decimal("0"),
        maker=True,
        exchange_trade_id=10_000 + index,
        ledger_ids=(f"LEDGER-BTC-{index}", f"LEDGER-CAD-{index}"),
    )


def _trade_ledger_entries(index: int) -> tuple[LedgerRecord, LedgerRecord]:
    trade = _trade(index)
    btc_balance = Decimal(index + 1) * trade.volume
    cad_balance = Decimal("1001.30") - Decimal(index + 1) * (trade.cost + trade.fee)
    return (
        LedgerRecord(
            ledger_id=f"LEDGER-BTC-{index}",
            reference_id=trade.trade_id,
            recorded_at=trade.executed_at,
            entry_type="trade",
            subtype="",
            asset_class="currency",
            asset="XXBT",
            amount=trade.volume,
            fee=Decimal("0"),
            balance=btc_balance,
        ),
        LedgerRecord(
            ledger_id=f"LEDGER-CAD-{index}",
            reference_id=trade.trade_id,
            recorded_at=trade.executed_at,
            entry_type="trade",
            subtype="",
            asset_class="currency",
            asset="ZCAD",
            amount=-trade.cost,
            fee=trade.fee,
            balance=cad_balance,
        ),
    )


def _deposit_entry() -> LedgerRecord:
    return LedgerRecord(
        ledger_id="LEDGER-DEPOSIT-CAD",
        reference_id="DEPOSIT-CAD",
        recorded_at=OBSERVED_AT - timedelta(days=30),
        entry_type="deposit",
        subtype="",
        asset_class="currency",
        asset="ZCAD",
        amount=Decimal("1001.30"),
        fee=Decimal("0"),
        balance=Decimal("1001.30"),
    )


def _legacy_hint(index: int) -> LegacySubmissionHint:
    order = _order(index)
    return LegacySubmissionHint(
        hint_id=f"legacy-hint-{index}",
        pair="BTC/CAD",
        side=Side.BUY,
        quantity_btc=order.volume,
        window_start=order.opened_at - timedelta(minutes=1),
        window_end=order.opened_at + timedelta(minutes=1),
        limit_price_cad=order.requested_price,
        order_id=order.order_id,
        client_order_id=order.client_order_id,
    )


def _legacy_hints() -> tuple[LegacySubmissionHint, ...]:
    return tuple(_legacy_hint(index) for index in range(5))


def _balances(*, btc: str = "0.005", cad: str = "500") -> BalanceSnapshot:
    return BalanceSnapshot(
        balances=(
            ExtendedBalance(
                asset="XXBT",
                balance=Decimal(btc),
                credit=Decimal("0"),
                credit_used=Decimal("0"),
                hold_trade=Decimal("0"),
            ),
            ExtendedBalance(
                asset="ZCAD",
                balance=Decimal(cad),
                credit=Decimal("0"),
                credit_used=Decimal("0"),
                hold_trade=Decimal("0"),
            ),
        ),
        observed_at=OBSERVED_AT,
    )


def _api_key_info() -> ApiKeyInfoSnapshot:
    return ApiKeyInfoSnapshot(
        key_name=EXPECTED_KEY_NAME,
        permissions=tuple(sorted(READ_ONLY_PERMISSIONS)),
        exchange_nonce=1,
        nonce_window=0,
        ip_allowlist=(f"{EXPECTED_IP}/32",),
        created_at=OBSERVED_AT - timedelta(days=365),
        modified_at=OBSERVED_AT - timedelta(days=1),
        last_used_at=OBSERVED_AT - timedelta(minutes=1),
        valid_until=None,
        query_from=None,
        query_to=None,
        observed_at=OBSERVED_AT,
    )


def _current_fee(fee_percent: str) -> CurrentFee:
    return CurrentFee(
        pair="XXBTZCAD",
        fee_percent=Decimal(fee_percent),
        minimum_fee_percent=Decimal("0"),
        maximum_fee_percent=Decimal("0.40"),
        tier_volume=Decimal("0"),
        next_fee_percent=Decimal("0.23"),
        next_volume=Decimal("10000"),
    )


class FakeKrakenReadPort:
    def __init__(
        self,
        *,
        api_key_info: ApiKeyInfoSnapshot | None = None,
        opening_balances: BalanceSnapshot | None = None,
        closing_balances: BalanceSnapshot | None = None,
        opening_orders: tuple[OrderRecord, ...] = (),
        closing_orders: tuple[OrderRecord, ...] = (),
        closed_total_count: int = 5,
        trade_total_count: int | None = 5,
        ledger_total_count: int | None = 11,
        wallet_accounts: WalletAccountsSnapshot | None = None,
    ) -> None:
        orders = tuple(_order(index) for index in range(5))
        trades = tuple(_trade(index) for index in range(5))
        self.api_key_info = _api_key_info() if api_key_info is None else api_key_info
        self.wallet_accounts = wallet_accounts or WalletAccountsSnapshot(
            accounts=(
                WalletAccount(
                    account_id=EXPECTED_ACCOUNT_ID,
                    status="active",
                    account_type="main",
                    active=True,
                    user_defined=False,
                ),
            ),
            complete=True,
            observed_at=OBSERVED_AT,
        )
        self.opening_balances = _balances() if opening_balances is None else opening_balances
        self.closing_balances = (
            self.opening_balances if closing_balances is None else closing_balances
        )
        self.opening_orders = opening_orders
        self.closing_orders = closing_orders
        self.closed = ClosedOrdersPage(
            orders=orders,
            total_count=closed_total_count,
            offset=0,
            observed_at=OBSERVED_AT,
        )
        self.trades = TradeHistoryPage(
            trades=trades,
            total_count=trade_total_count,
            offset=0,
            observed_at=OBSERVED_AT,
        )
        ledger_entries = (
            _deposit_entry(),
            *(entry for index in range(5) for entry in _trade_ledger_entries(index)),
        )
        self.ledgers = LedgerPage(
            entries=ledger_entries,
            total_count=ledger_total_count,
            offset=0,
            observed_at=OBSERVED_AT,
        )
        self.tail_closed = ClosedOrdersPage(
            orders=(), total_count=0, offset=0, observed_at=OBSERVED_AT
        )
        self.tail_trades = TradeHistoryPage(
            trades=(), total_count=0, offset=0, observed_at=OBSERVED_AT
        )
        self.tail_ledgers = LedgerPage(entries=(), total_count=0, offset=0, observed_at=OBSERVED_AT)
        self.calls: list[str] = []
        self._private_cost_spent = 0
        self._server_calls = 0
        self._balance_calls = 0
        self._open_order_calls = 0
        self._closed_calls = 0
        self._trade_calls = 0
        self._ledger_calls = 0

    @property
    def private_cost_spent(self) -> int:
        return self._private_cost_spent

    def _private(self, label: str, cost: int) -> None:
        self.calls.append(label)
        self._private_cost_spent += cost

    def get_server_time(self) -> ServerTimeSnapshot:
        self.calls.append("get_server_time")
        self._server_calls += 1
        offset = timedelta(seconds=self._server_calls - 1)
        return ServerTimeSnapshot(
            server_time=OBSERVED_AT + offset,
            observed_at=OBSERVED_AT + offset,
            clock_skew=timedelta(0),
        )

    def get_system_status(self) -> SystemStatusSnapshot:
        self.calls.append("get_system_status")
        return SystemStatusSnapshot(
            status=KrakenSystemStatus.ONLINE,
            status_at=OBSERVED_AT,
            observed_at=OBSERVED_AT,
        )

    def get_asset_pair(self, *, pair: str = "XBTCAD") -> AssetPairSnapshot:
        self.calls.append(f"get_asset_pair:{pair}")
        return AssetPairSnapshot(
            pair=AssetPair(
                exchange_pair="XXBTZCAD",
                alternate_name="XBTCAD",
                websocket_name="XBT/CAD",
                base_asset="XXBT",
                quote_asset="ZCAD",
                status="online",
                order_minimum=Decimal("0.0001"),
                cost_minimum=Decimal("5"),
                tick_size=Decimal("0.1"),
                cost_decimals=5,
                pair_decimals=1,
                lot_decimals=8,
                taker_schedule=(),
                maker_schedule=(),
            ),
            observed_at=OBSERVED_AT,
        )

    def get_api_key_info(self) -> ApiKeyInfoSnapshot:
        self._private("get_api_key_info", 1)
        return self.api_key_info

    def get_wallet_accounts(self) -> WalletAccountsSnapshot:
        self._private("get_wallet_accounts", 1)
        return self.wallet_accounts

    def get_trade_volume(self, *, pair: str = "XBTCAD") -> TradeVolumeSnapshot:
        self._private(f"get_trade_volume:{pair}", 1)
        return TradeVolumeSnapshot(
            currency="ZCAD",
            rolling_volume=Decimal("500"),
            taker_fees=(_current_fee("0.40"),),
            maker_fees=(_current_fee("0.25"),),
            observed_at=OBSERVED_AT,
        )

    def get_extended_balances(self) -> BalanceSnapshot:
        self._private("get_extended_balances", 1)
        self._balance_calls += 1
        return self.opening_balances if self._balance_calls == 1 else self.closing_balances

    def get_open_orders(self, *, client_order_id: str | None = None) -> OpenOrdersSnapshot:
        self._private("get_open_orders", 1)
        self._open_order_calls += 1
        orders = self.opening_orders if self._open_order_calls == 1 else self.closing_orders
        return OpenOrdersSnapshot(orders=orders, observed_at=OBSERVED_AT)

    def get_closed_orders(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        client_order_id: str | None = None,
    ) -> ClosedOrdersPage:
        self._private("get_closed_orders", 4)
        self._closed_calls += 1
        assert start is not None
        assert end is not None
        assert offset == 0
        assert client_order_id is None
        return self.closed if self._closed_calls == 1 else self.tail_closed

    def query_orders(self, order_ids: Sequence[str]) -> OrderQuerySnapshot:
        self._private("query_orders", 1)
        by_id = {order.order_id: order for order in self.closed.orders}
        requested = tuple(order_ids)
        return OrderQuerySnapshot(
            orders=tuple(by_id[order_id] for order_id in requested),
            requested_order_ids=requested,
            observed_at=OBSERVED_AT,
        )

    def get_trades_history(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        limit: int = 50,
        pair: str | None = None,
    ) -> TradeHistoryPage:
        self._private("get_trades_history", 4)
        self._trade_calls += 1
        assert start is not None
        assert end is not None
        assert offset == 0
        assert limit == 100
        assert pair == "XBTCAD"
        return self.trades if self._trade_calls == 1 else self.tail_trades

    def get_ledgers(
        self,
        *,
        account_id: str | None,
        entry_type: str = "all",
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
    ) -> LedgerPage:
        self._private("get_ledgers", 4)
        self._ledger_calls += 1
        assert account_id is None
        assert entry_type == "all"
        assert start is not None
        assert end is not None
        assert offset == 0
        return self.ledgers if self._ledger_calls == 1 else self.tail_ledgers


def test_clean_five_hint_run_persists_schema_v3_zero_write_snapshot(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.CLEAN.value
    assert result["exchange_writes"] is False
    assert result["legacy_hint_count"] == 5
    assert result["private_request_cost_spent"] == 33
    assert result["history"]["complete"] is True  # type: ignore[index]
    core_report = result["core_report"]
    assert isinstance(core_report, dict)
    assert core_report["status"] == ReconciliationStatus.CLEAN.value
    assert core_report["total_fees_cad"] == "1.30"
    assert result["source_data_hash"] == sha256_json(result["evidence"])
    legacy_matches = core_report["legacy_matches"]
    assert isinstance(legacy_matches, list)
    assert len(legacy_matches) == 5
    assert core_report["zero_write_proof"] == {
        "exchange_writes": False,
        "implementation": "exchange_independent_reconciliation_v1",
        "network_calls": 0,
        "persistence_writes": 0,
    }
    status = ledger.status()
    assert status["schema_version"] == 3
    assert status["reconciliation_count"] == 1
    assert status["latest_reconciliation"]["exchange_writes"] is False  # type: ignore[index]
    assert status["latest_reconciliation"]["status"] == "CLEAN"  # type: ignore[index]
    assert str(result["ledger_snapshot_id"]).startswith("reconciliation_")


def test_account_id_discovery_requires_read_only_gates_and_does_not_persist_identity(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    settings = replace(_settings(tmp_path), expected_kraken_account_id=None)

    result = discover_read_only_account_id(
        settings=settings,
        ledger=ledger,
        client=client,
    )

    assert result == {
        "exchange_writes": False,
        "observed_at": "2026-08-31T16:00:00Z",
        "private_request_cost_spent": 2,
        "read_only_profile_verified": True,
        "wallet_account_id": EXPECTED_ACCOUNT_ID,
    }
    assert client.calls == [
        "get_server_time",
        "get_api_key_info",
        "get_wallet_accounts",
    ]
    assert ledger.status()["reconciliation_count"] == 0


def test_account_id_discovery_stops_before_identity_on_overprivileged_key(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    info = replace(_api_key_info(), permissions=(*tuple(READ_ONLY_PERMISSIONS), "trade"))
    client = FakeKrakenReadPort(api_key_info=info)

    with pytest.raises(ReconciliationJobError, match="did not pass"):
        discover_read_only_account_id(
            settings=replace(_settings(tmp_path), expected_kraken_account_id=None),
            ledger=ledger,
            client=client,
        )

    assert client.calls == ["get_server_time", "get_api_key_info"]
    assert ledger.status()["reconciliation_count"] == 0


def test_missing_legacy_hints_remain_unresolved_and_are_persisted(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path), ledger=ledger, client=client
    )

    assert result["status"] == ReconciliationStatus.UNRESOLVED.value
    assert result["legacy_hint_count"] == 0
    gates = result["gates"]
    assert isinstance(gates, list)
    assert any(
        gate["name"] == "legacy_order_identity_complete" and gate["passed"] is False
        for gate in gates
    )
    assert ledger.status()["latest_reconciliation"]["status"] == "UNRESOLVED"  # type: ignore[index]


def _assert_access_attestation_disarms(
    tmp_path: Path,
    api_key_info: ApiKeyInfoSnapshot,
    *,
    failed_gate: str,
) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort(api_key_info=api_key_info)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["history"] is None
    assert result["core_report"] is None
    assert result["private_request_cost_spent"] == 1
    assert client.calls == [
        "get_server_time",
        "get_system_status",
        "get_asset_pair:XBTCAD",
        "get_api_key_info",
    ]
    gates = result["gates"]
    assert isinstance(gates, list)
    assert any(gate["name"] == failed_gate and gate["passed"] is False for gate in gates)
    assert ledger.status()["latest_reconciliation"]["status"] == "DISARMED"  # type: ignore[index]


def test_overprivileged_key_stops_after_access_attestation(tmp_path: Path) -> None:
    info = replace(
        _api_key_info(),
        permissions=(*tuple(sorted(READ_ONLY_PERMISSIONS)), "trade"),
    )
    _assert_access_attestation_disarms(
        tmp_path,
        info,
        failed_gate="permissions_exactly_read_only",
    )


def test_key_name_mismatch_stops_after_access_attestation(tmp_path: Path) -> None:
    info = replace(_api_key_info(), key_name="unexpected-key-profile")
    _assert_access_attestation_disarms(tmp_path, info, failed_gate="expected_key_name")


def test_ip_mismatch_stops_after_access_attestation(tmp_path: Path) -> None:
    info = replace(_api_key_info(), ip_allowlist=("192.0.2.0/24",))
    _assert_access_attestation_disarms(tmp_path, info, failed_gate="expected_ip_allowlisted")


@pytest.mark.parametrize(
    "allowlist",
    [
        ("0.0.0.0/0",),
        (f"{EXPECTED_IP}/24",),
        (f"{EXPECTED_IP}/32", "192.0.2.10/32"),
    ],
)
def test_broad_or_additional_ip_allowlist_entries_fail_closed(
    tmp_path: Path,
    allowlist: tuple[str, ...],
) -> None:
    info = replace(_api_key_info(), ip_allowlist=allowlist)
    _assert_access_attestation_disarms(
        tmp_path,
        info,
        failed_gate="expected_ip_allowlisted",
    )


def test_wrong_kraken_wallet_account_stops_before_history_reads(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    wrong_wallet = WalletAccountsSnapshot(
        accounts=(
            WalletAccount(
                account_id="ABCD-EFGH-IJKL-MNOP",
                status="active",
                account_type="main",
                active=True,
                user_defined=False,
            ),
        ),
        complete=True,
        observed_at=OBSERVED_AT,
    )
    client = FakeKrakenReadPort(wallet_accounts=wrong_wallet)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["history"] is None
    assert result["private_request_cost_spent"] == 2
    assert _gate_value(result, "expected_wallet_account") is False
    assert EXPECTED_ACCOUNT_ID not in json.dumps(result)
    assert "ABCD-EFGH-IJKL-MNOP" not in json.dumps(result)


def test_multiple_active_wallets_stop_before_default_scoped_reads(tmp_path: Path) -> None:
    wallets = WalletAccountsSnapshot(
        accounts=(
            WalletAccount(
                account_id=EXPECTED_ACCOUNT_ID,
                status="active",
                account_type="main",
                active=True,
                user_defined=False,
            ),
            WalletAccount(
                account_id="ABCD-EFGH-IJKL-MNOP",
                status="active",
                account_type="user",
                active=True,
                user_defined=True,
            ),
        ),
        complete=True,
        observed_at=OBSERVED_AT,
    )
    client = FakeKrakenReadPort(wallet_accounts=wallets)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["history"] is None
    assert _gate_value(result, "one_active_wallet") is False
    assert "get_extended_balances" not in client.calls
    assert "get_ledgers" not in client.calls


def test_incomplete_wallet_page_stops_before_default_scoped_reads(tmp_path: Path) -> None:
    wallets = WalletAccountsSnapshot(
        accounts=(
            WalletAccount(
                account_id=EXPECTED_ACCOUNT_ID,
                status="active",
                account_type="main",
                active=True,
                user_defined=False,
            ),
        ),
        complete=False,
        observed_at=OBSERVED_AT,
    )
    client = FakeKrakenReadPort(wallet_accounts=wallets)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["history"] is None
    assert _gate_value(result, "wallet_accounts_page_complete") is False
    assert "get_extended_balances" not in client.calls
    assert "get_ledgers" not in client.calls


def test_unverified_mistyped_account_binding_does_not_poison_continuity(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    mistyped = replace(
        _settings(tmp_path),
        expected_kraken_account_id="ABCD-EFGH-IJKL-MNOP",
    )

    rejected = execute_read_only_reconciliation(
        settings=mistyped,
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=FakeKrakenReadPort(),
    )

    assert rejected["status"] == ReconciliationStatus.DISARMED.value
    assert rejected["account_binding_verified"] is False
    assert ledger.reconciliation_binding_hashes(mistyped.account_id) == frozenset()

    accepted = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=FakeKrakenReadPort(),
    )

    assert accepted["status"] == ReconciliationStatus.CLEAN.value
    assert accepted["account_binding_verified"] is True
    assert ledger.reconciliation_binding_hashes(mistyped.account_id) == {
        str(accepted["account_binding_hash"])
    }


def test_unstable_balances_disarm_without_core_report(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort(closing_balances=_balances(btc="0.006"))

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == "DISARMED"
    assert result["core_report"] is None
    assert _gate_value(result, "snapshot_stable") is False


def test_unstable_open_orders_disarm_without_core_report(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort(closing_orders=(_order(99, status="open"),))

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == "DISARMED"
    assert result["core_report"] is None
    assert _gate_value(result, "snapshot_stable") is False
    assert _gate_value(result, "no_open_orders_at_cutover") is False


def test_incomplete_history_page_disarms_without_core_report(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort(closed_total_count=6)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == "DISARMED"
    assert result["core_report"] is None
    assert result["history"]["complete"] is False  # type: ignore[index]
    assert _gate_value(result, "history_complete_within_budget") is False


def test_access_history_scope_must_cover_the_closing_fence(tmp_path: Path) -> None:
    client = FakeKrakenReadPort(
        api_key_info=replace(_api_key_info(), query_to=OBSERVED_AT),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert _gate_value(result, "history_scope_end") is True
    assert _gate_value(result, "closing_history_scope_end") is False


def test_new_tail_activity_disarms_the_collection(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    new_order = replace(
        _order(99, status="canceled"),
        executed_volume=Decimal("0"),
        cost=Decimal("0"),
        fee=Decimal("0"),
        average_price=Decimal("0"),
        trade_ids=(),
    )
    client.tail_closed = replace(client.tail_closed, orders=(new_order,), total_count=1)

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert _gate_value(result, "collection_tail_quiet") is False
    assert result["history"]["collection_quiet"] is False  # type: ignore[index]
    final_open_index = max(
        index for index, call in enumerate(client.calls) if call == "get_open_orders"
    )
    final_server_index = max(
        index for index, call in enumerate(client.calls) if call == "get_server_time"
    )
    tail_closed_index = max(
        index for index, call in enumerate(client.calls) if call == "get_closed_orders"
    )
    assert final_open_index < final_server_index < tail_closed_index


def test_zero_fill_order_canceled_after_fence_cannot_escape_both_order_reads(
    tmp_path: Path,
) -> None:
    class OpenThenCancelClient(FakeKrakenReadPort):
        def __init__(self) -> None:
            super().__init__()
            self.final_open_read_complete = False

        def get_open_orders(
            self,
            *,
            client_order_id: str | None = None,
        ) -> OpenOrdersSnapshot:
            snapshot = super().get_open_orders(client_order_id=client_order_id)
            if self._open_order_calls == 3:
                self.final_open_read_complete = True
            return snapshot

        def get_closed_orders(
            self,
            *,
            start: int | None = None,
            end: int | None = None,
            offset: int = 0,
            client_order_id: str | None = None,
        ) -> ClosedOrdersPage:
            snapshot = super().get_closed_orders(
                start=start,
                end=end,
                offset=offset,
                client_order_id=client_order_id,
            )
            if self._closed_calls == 2 and self.final_open_read_complete:
                canceled = replace(
                    _order(99, status="canceled"),
                    executed_volume=Decimal("0"),
                    cost=Decimal("0"),
                    fee=Decimal("0"),
                    average_price=Decimal("0"),
                    trade_ids=(),
                )
                return ClosedOrdersPage(
                    orders=(canceled,),
                    total_count=1,
                    offset=0,
                    observed_at=OBSERVED_AT + timedelta(seconds=2),
                )
            return snapshot

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=OpenThenCancelClient(),
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert _gate_value(result, "collection_tail_quiet") is False
    assert _gate_value(result, "no_open_orders_at_cutover") is True


def test_zero_fill_order_appearing_at_fence_is_caught_by_post_fence_read(
    tmp_path: Path,
) -> None:
    class LateOpenOrderClient(FakeKrakenReadPort):
        def get_open_orders(
            self,
            *,
            client_order_id: str | None = None,
        ) -> OpenOrdersSnapshot:
            snapshot = super().get_open_orders(client_order_id=client_order_id)
            if self._open_order_calls == 3:
                return OpenOrdersSnapshot(
                    orders=(_order(99, status="open"),),
                    observed_at=OBSERVED_AT + timedelta(seconds=1),
                )
            return snapshot

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=LateOpenOrderClient(),
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert _gate_value(result, "snapshot_stable") is False
    assert _gate_value(result, "no_open_orders_at_cutover") is False
    assert result["private_request_cost_spent"] == 33


def test_missing_trade_ledger_link_disarms_without_core_report(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    client.ledgers = replace(
        client.ledgers,
        entries=client.ledgers.entries[:-1],
        total_count=10,
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["core_report"] is None
    assert _gate_value(result, "trade_ledger_links_consistent") is False


def test_order_fee_mismatch_disarms_without_core_report(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    mismatched = replace(client.closed.orders[0], fee=Decimal("0.27"))
    client.closed = replace(
        client.closed,
        orders=(mismatched, *client.closed.orders[1:]),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["core_report"] is None
    assert _gate_value(result, "order_trade_totals_consistent") is False


def test_order_average_price_must_match_linked_fill_cost(tmp_path: Path) -> None:
    client = FakeKrakenReadPort()
    mismatched = replace(client.closed.orders[0], average_price=Decimal("99900"))
    client.closed = replace(
        client.closed,
        orders=(mismatched, *client.closed.orders[1:]),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["core_report"] is None
    assert _gate_value(result, "order_trade_totals_consistent") is False


def test_btc_ledger_fee_reconciles_to_quote_currency_trade_fee() -> None:
    trade = _trade(0)
    order = _order(0)
    btc_entry, cad_entry = _trade_ledger_entries(0)
    btc_fee = Decimal("0.00000260")
    btc_entry = replace(
        btc_entry,
        fee=btc_fee,
        balance=btc_entry.balance - btc_fee,
    )
    cad_entry = replace(
        cad_entry,
        fee=Decimal("0"),
        balance=cad_entry.balance + trade.fee,
    )

    fees, consistent = _linked_trade_fees(
        (trade,),
        (btc_entry, cad_entry),
        (order,),
        cost_decimals=5,
        base_decimals=8,
    )

    assert consistent is True
    assert [(fee.asset, fee.amount) for fee in fees] == [("BTC", btc_fee)]


def test_ledger_chain_rejects_negative_intermediate_or_current_balance() -> None:
    first = LedgerRecord(
        ledger_id="L-SELL-FIRST",
        reference_id="T-SELL-FIRST",
        recorded_at=OBSERVED_AT - timedelta(minutes=2),
        entry_type="trade",
        subtype="",
        asset_class="currency",
        asset="XXBT",
        amount=Decimal("-0.001"),
        fee=Decimal("0"),
        balance=Decimal("-0.001"),
    )
    second = replace(
        first,
        ledger_id="L-BUY-LATER",
        reference_id="T-BUY-LATER",
        recorded_at=OBSERVED_AT - timedelta(minutes=1),
        amount=Decimal("0.002"),
        balance=Decimal("0.001"),
    )

    assert _ledger_balances_match((first, second), _balances(btc="0.001", cad="0")) is False


@pytest.mark.parametrize("expected_hash", [None, "0" * 64])
def test_funding_manifest_requires_explicit_operator_pin(
    tmp_path: Path,
    expected_hash: str | None,
) -> None:
    settings = replace(
        _settings(tmp_path),
        expected_funding_manifest_hash=expected_hash,
    )

    result = execute_read_only_reconciliation(
        settings=settings,
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=FakeKrakenReadPort(),
    )

    assert result["status"] == ReconciliationStatus.UNRESOLVED.value
    assert _gate_value(result, "external_cash_flows_classified") is False
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["funding_manifest_hash"] == funding_manifest_hash((_deposit_entry(),))


def test_wrong_trade_ledger_amount_disarms_dimension_check(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    btc_entry = client.ledgers.entries[1]
    client.ledgers = replace(
        client.ledgers,
        entries=(
            client.ledgers.entries[0],
            replace(btc_entry, amount=Decimal("0.002")),
            *client.ledgers.entries[2:],
        ),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["core_report"] is None
    assert _gate_value(result, "trade_ledger_links_consistent") is False


def test_trade_cost_product_uses_cost_precision_not_price_precision(
    tmp_path: Path,
) -> None:
    client = FakeKrakenReadPort()
    inconsistent = replace(client.trades.trades[0], price=Decimal("99950"))
    client.trades = replace(
        client.trades,
        trades=(inconsistent, *client.trades.trades[1:]),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=_ledger(tmp_path),
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert result["core_report"] is None
    assert _gate_value(result, "trade_ledger_links_consistent") is False


def test_margin_leverage_and_unresolved_hints_cannot_downgrade_disarmed(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    client = FakeKrakenReadPort()
    leveraged = replace(client.closed.orders[0], leverage="2")
    client.closed = replace(
        client.closed,
        orders=(leveraged, *client.closed.orders[1:]),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=(),
        client=client,
    )

    assert result["status"] == ReconciliationStatus.DISARMED.value
    assert _gate_value(result, "margin_credit_absent") is False
    assert _gate_value(result, "legacy_order_identity_complete") is False


def test_unattested_quiescence_never_returns_clean(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    settings = replace(_settings(tmp_path), cutover_quiesced=False)

    result = execute_read_only_reconciliation(
        settings=settings,
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=FakeKrakenReadPort(),
    )

    assert result["status"] == ReconciliationStatus.UNRESOLVED.value
    assert _gate_value(result, "cutover_quiescence_attested") is False


def test_stable_open_order_disarms_even_when_snapshot_is_otherwise_coherent(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    open_order = _order(99, status="open")
    client = FakeKrakenReadPort(
        opening_orders=(open_order,),
        closing_orders=(open_order,),
    )

    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=client,
    )

    assert result["status"] == "DISARMED"
    assert _gate_value(result, "snapshot_stable") is True
    assert _gate_value(result, "no_open_orders_at_cutover") is False
    core_report = result["core_report"]
    assert isinstance(core_report, dict)
    assert core_report["status"] == "DISARMED"
    assert core_report["open_order_ids"] == [open_order.order_id]


def _gate_value(result: dict[str, object], name: str) -> bool:
    gates = result["gates"]
    assert isinstance(gates, list)
    matches = [gate for gate in gates if gate["name"] == name]
    assert len(matches) == 1
    return bool(matches[0]["passed"])


def test_raw_key_secret_key_name_and_ip_never_cross_persistence_boundary(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    result = execute_read_only_reconciliation(
        settings=_settings(tmp_path),
        ledger=ledger,
        legacy_hints=_legacy_hints(),
        client=FakeKrakenReadPort(),
    )
    encoded_result = json.dumps(result, sort_keys=True)

    with sqlite3.connect(ledger.path) as connection:
        row = connection.execute("SELECT report_json FROM reconciliation_snapshots").fetchone()
    assert row is not None
    persisted_report = str(row[0])
    for forbidden in (
        RAW_API_KEY,
        RAW_API_SECRET,
        EXPECTED_KEY_NAME,
        EXPECTED_IP,
        EXPECTED_ACCOUNT_ID,
        f"{EXPECTED_IP}/32",
    ):
        assert forbidden not in encoded_result
        assert forbidden not in persisted_report


def _hint_json(index: int) -> dict[str, str]:
    order = _order(index)
    return {
        "client_order_id": str(order.client_order_id),
        "hint_id": f"legacy-hint-{index}",
        "limit_price_cad": "100000.0",
        "order_id": order.order_id,
        "pair": "XBT/CAD",
        "quantity_btc": "0.00100000",
        "side": "buy",
        "window_end": (order.opened_at + timedelta(minutes=1)).isoformat(),
        "window_start": (order.opened_at - timedelta(minutes=1)).isoformat(),
    }


def test_load_legacy_hints_accepts_exact_bounded_schema(tmp_path: Path) -> None:
    path = tmp_path / "hints.json"
    path.write_text(json.dumps([_hint_json(index) for index in range(5)]), encoding="utf-8")

    hints = load_legacy_hints(path)

    assert hints == _legacy_hints()
    assert legacy_manifest_hash(hints) == legacy_manifest_hash(_legacy_hints())


def test_load_legacy_hints_rejects_unknown_fields_and_nonstandard_numbers(
    tmp_path: Path,
) -> None:
    unknown = _hint_json(0)
    unknown["credential"] = "must-not-be-accepted"
    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_text(json.dumps([unknown]), encoding="utf-8")
    nan_path = tmp_path / "nan.json"
    nan_path.write_text(
        json.dumps([{**_hint_json(0), "quantity_btc": "NaN"}]),
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationJobError, match="invalid field set"):
        load_legacy_hints(unknown_path)
    with pytest.raises(ReconciliationJobError, match="finite"):
        load_legacy_hints(nan_path)


def test_load_legacy_hints_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-key.json"
    path.write_text(
        '[{"hint_id":"first","hint_id":"second",'
        '"pair":"BTC/CAD","quantity_btc":"0.001",'
        '"side":"buy","window_start":"2026-08-01T00:00:00Z",'
        '"window_end":"2026-08-01T00:01:00Z"}]',
        encoding="utf-8",
    )

    with pytest.raises(ReconciliationJobError, match="not valid JSON"):
        load_legacy_hints(path)


def test_load_legacy_hints_rejects_oversized_and_overlong_arrays(tmp_path: Path) -> None:
    too_many_path = tmp_path / "too-many.json"
    too_many_path.write_text(
        json.dumps([_hint_json(index) for index in range(MAX_LEGACY_HINTS + 1)]),
        encoding="utf-8",
    )
    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_text(" " * (MAX_LEGACY_HINT_BYTES + 1), encoding="utf-8")

    with pytest.raises(ReconciliationJobError, match="bounded JSON array"):
        load_legacy_hints(too_many_path)
    with pytest.raises(ReconciliationJobError, match="size limit"):
        load_legacy_hints(oversized_path)


def test_load_legacy_hints_rejects_symlink_fifo_and_writable_file(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps([_hint_json(index) for index in range(5)]), encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    fifo = tmp_path / "hints.fifo"
    os.mkfifo(fifo)
    writable = tmp_path / "writable.json"
    writable.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    writable.chmod(0o666)

    for unsafe in (symlink, fifo):
        with pytest.raises(ReconciliationJobError, match="regular non-symlink"):
            load_legacy_hints(unsafe)
    with pytest.raises(ReconciliationJobError, match="group/other writable"):
        load_legacy_hints(writable)


def test_load_legacy_hints_reads_until_eof_after_partial_os_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "hints.json"
    path.write_text(json.dumps([_hint_json(index) for index in range(5)]), encoding="utf-8")
    real_read = os.read

    def partial_read(descriptor: int, count: int) -> bytes:
        return real_read(descriptor, min(count, 7))

    monkeypatch.setattr("kraken_knight.reconcile_job.os.read", partial_read)

    assert load_legacy_hints(path) == _legacy_hints()


def test_host_lease_rejects_overlapping_reconciliation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    with _reconciliation_lease(ledger):
        with pytest.raises(ReconciliationJobError, match="another reconciliation"):
            execute_read_only_reconciliation(
                settings=_settings(tmp_path),
                ledger=ledger,
                legacy_hints=_legacy_hints(),
                client=FakeKrakenReadPort(),
            )


def test_public_request_pacer_delays_only_subsequent_public_calls() -> None:
    monotonic_values = iter((10.0, 10.2, 10.2, 11.05))
    sleeps: list[float] = []
    pacer = PublicRequestPacer(
        interval_seconds=1.05,
        monotonic=lambda: next(monotonic_values),
        sleep=sleeps.append,
    )

    pacer("public:Time", 0)
    pacer("private:BalanceEx", 1)
    pacer("public:SystemStatus", 0)

    assert sleeps == pytest.approx([0.85])


def test_request_pacer_waits_for_private_counter_decay() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    pacer = PublicRequestPacer(monotonic=lambda: now[0], sleep=sleep)

    pacer("private:first", 19)
    pacer("private:second", 4)

    assert sleeps == pytest.approx([8.0])


@pytest.mark.parametrize("interval", [0.0, -1.0, float("nan"), float("inf")])
def test_public_request_pacer_rejects_nonpositive_or_nonfinite_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        PublicRequestPacer(interval_seconds=interval)
