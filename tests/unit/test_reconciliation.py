from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kraken_knight.reconciliation import (
    AccountSnapshot,
    AssetBalance,
    AuthoritativeOrder,
    AuthoritativeTrade,
    LegacyMatchBasis,
    LegacySubmissionHint,
    Liability,
    OpeningInventoryClassification,
    OrderOwnership,
    OrderState,
    ReasonCode,
    ReconciliationStatus,
    Side,
    TradeFee,
    ZeroWriteProof,
    reconcile_account,
)

OBSERVED_AT = datetime(2026, 8, 31, 12, tzinfo=UTC)
OPENED_AT = OBSERVED_AT - timedelta(days=1)
CLOSED_AT = OPENED_AT + timedelta(minutes=10)


def _balances(
    *,
    cad_available: str = "1000",
    cad_held: str = "0",
    btc_available: str = "0",
    btc_held: str = "0",
) -> tuple[AssetBalance, ...]:
    return (
        AssetBalance("CAD", Decimal(cad_available), Decimal(cad_held)),
        AssetBalance("BTC", Decimal(btc_available), Decimal(btc_held)),
    )


def _order(
    order_id: str = "order-1",
    *,
    side: Side = Side.BUY,
    state: OrderState = OrderState.FILLED,
    quantity: str = "0.01",
    filled: str | None = None,
    opened_at: datetime = OPENED_AT,
    pair: str = "BTC/CAD",
    client_order_id: str | None = None,
    ownership: OrderOwnership = OrderOwnership.UNKNOWN,
    price: str | None = "100000",
) -> AuthoritativeOrder:
    filled_quantity = quantity if filled is None and state is OrderState.FILLED else (filled or "0")
    closed_at = None if state.is_open else CLOSED_AT
    return AuthoritativeOrder(
        order_id=order_id,
        pair=pair,
        side=side,
        state=state,
        quantity_btc=Decimal(quantity),
        filled_quantity_btc=Decimal(filled_quantity),
        opened_at=opened_at,
        closed_at=closed_at,
        limit_price_cad=None if price is None else Decimal(price),
        client_order_id=client_order_id,
        ownership=ownership,
    )


def _trade(
    trade_id: str = "trade-1",
    *,
    order_id: str = "order-1",
    side: Side = Side.BUY,
    quantity: str = "0.01",
    price: str = "100000",
    pair: str = "BTC/CAD",
) -> AuthoritativeTrade:
    return AuthoritativeTrade(
        trade_id=trade_id,
        order_id=order_id,
        pair=pair,
        side=side,
        quantity_btc=Decimal(quantity),
        price_cad=Decimal(price),
        executed_at=OPENED_AT + timedelta(minutes=5),
    )


def _hint(
    hint_id: str = "hint-1",
    *,
    order_id: str | None = "order-1",
    client_order_id: str | None = None,
    side: Side = Side.BUY,
    quantity: str = "0.01",
    pair: str = "BTC/CAD",
    price: str | None = "100000",
) -> LegacySubmissionHint:
    return LegacySubmissionHint(
        hint_id=hint_id,
        pair=pair,
        side=side,
        quantity_btc=Decimal(quantity),
        window_start=OPENED_AT - timedelta(minutes=1),
        window_end=OPENED_AT + timedelta(minutes=1),
        limit_price_cad=None if price is None else Decimal(price),
        order_id=order_id,
        client_order_id=client_order_id,
    )


def _snapshot(
    *,
    balances: tuple[AssetBalance, ...] | None = None,
    orders: tuple[AuthoritativeOrder, ...] = (),
    trades: tuple[AuthoritativeTrade, ...] = (),
    fees: tuple[TradeFee, ...] = (),
    liabilities: tuple[Liability, ...] = (),
    hints: tuple[LegacySubmissionHint, ...] = (),
    inventory_history_complete: bool = True,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="account-a",
        observed_at=OBSERVED_AT,
        balances=_balances() if balances is None else balances,
        orders=orders,
        trades=trades,
        fees=fees,
        liabilities=liabilities,
        legacy_hints=hints,
        inventory_history_complete=inventory_history_complete,
    )


def _reason_codes(snapshot: AccountSnapshot) -> set[ReasonCode]:
    return set(reconcile_account(snapshot).reason_codes)


def test_cash_only_account_is_clean_content_addressed_and_zero_write() -> None:
    report = reconcile_account(_snapshot())

    assert report.status is ReconciliationStatus.CLEAN
    assert report.reason_codes == ()
    assert report.opening_inventory.classification is OpeningInventoryClassification.CASH_ONLY
    assert report.opening_inventory.cad_available == Decimal("1000")
    assert report.opening_inventory.cad_held == 0
    assert report.opening_inventory.cad_total == Decimal("1000")
    assert report.opening_inventory.btc_total == 0
    assert report.report_id == f"reconciliation_{report.content_hash}"
    assert len(report.content_hash) == 64
    assert len(report.source_hash) == 64
    assert report.zero_write_proof.exchange_writes is False
    assert report.zero_write_proof.network_calls == 0
    assert report.zero_write_proof.persistence_writes == 0
    with pytest.raises(FrozenInstanceError):
        report.status = ReconciliationStatus.DISARMED  # type: ignore[misc]


def test_report_is_independent_of_input_tuple_order() -> None:
    first_order = _order(
        "order-a",
        state=OrderState.CANCELED,
        filled="0",
        ownership=OrderOwnership.BOT,
    )
    second_order = _order(
        "order-b",
        state=OrderState.REJECTED,
        filled="0",
        ownership=OrderOwnership.BOT,
    )
    first = _snapshot(
        balances=_balances(),
        orders=(first_order, second_order),
        liabilities=(Liability("BTC", Decimal("0")), Liability("CAD", Decimal("0"))),
    )
    second = _snapshot(
        balances=tuple(reversed(_balances())),
        orders=(second_order, first_order),
        liabilities=(Liability("CAD", Decimal("0")), Liability("BTC", Decimal("0"))),
    )

    assert reconcile_account(first) == reconcile_account(second)


def test_legacy_buy_matches_authoritative_order_and_trade_by_order_id() -> None:
    fee = TradeFee("fee-1", "trade-1", "CAD", Decimal("0.40"))
    report = reconcile_account(
        _snapshot(
            balances=_balances(btc_available="0.01"),
            orders=(_order(),),
            trades=(_trade(),),
            fees=(fee,),
            hints=(_hint(),),
        )
    )

    assert report.status is ReconciliationStatus.CLEAN
    assert report.legacy_matches[0].basis is LegacyMatchBasis.ORDER_ID
    assert report.legacy_matches[0].order_id == "order-1"
    assert report.legacy_matches[0].trade_ids == ("trade-1",)
    assert report.opening_inventory.legacy_net_btc == Decimal("0.01")
    assert (
        report.opening_inventory.classification
        is OpeningInventoryClassification.CONFIRMED_LEGACY_BTC
    )
    assert report.total_fees_cad == Decimal("0.40")
    assert report.total_fees_btc == 0


def test_btc_fee_is_deducted_from_legacy_opening_inventory() -> None:
    fee = TradeFee("fee-1", "trade-1", "BTC", Decimal("0.0001"))
    report = reconcile_account(
        _snapshot(
            balances=_balances(btc_available="0.0099"),
            orders=(_order(),),
            trades=(_trade(),),
            fees=(fee,),
            hints=(_hint(),),
        )
    )

    assert report.status is ReconciliationStatus.CLEAN
    assert report.total_fees_btc == Decimal("0.0001")
    assert report.opening_inventory.legacy_net_btc == Decimal("0.0099")
    assert (
        report.opening_inventory.classification
        is OpeningInventoryClassification.CONFIRMED_LEGACY_BTC
    )


@pytest.mark.parametrize(
    ("hint", "expected_basis"),
    [
        (
            _hint(order_id=None, client_order_id="legacy-client-1"),
            LegacyMatchBasis.CLIENT_ORDER_ID,
        ),
        (_hint(order_id=None, client_order_id=None), LegacyMatchBasis.EXACT_ATTRIBUTES),
    ],
)
def test_legacy_hint_matches_only_on_complete_unique_evidence(
    hint: LegacySubmissionHint,
    expected_basis: LegacyMatchBasis,
) -> None:
    client_order_id = (
        "legacy-client-1" if expected_basis is LegacyMatchBasis.CLIENT_ORDER_ID else None
    )
    order = _order(
        state=OrderState.CANCELED,
        filled="0",
        client_order_id=client_order_id,
    )

    report = reconcile_account(_snapshot(orders=(order,), hints=(hint,)))

    if expected_basis is LegacyMatchBasis.EXACT_ATTRIBUTES:
        assert report.status is ReconciliationStatus.UNRESOLVED
        assert report.legacy_matches == ()
        assert ReasonCode.UNVERIFIED_ATTRIBUTE_MATCH in report.reason_codes
    else:
        assert report.status is ReconciliationStatus.CLEAN
        assert report.legacy_matches[0].basis is expected_basis


def test_nonzero_btc_without_complete_inventory_history_is_unresolved() -> None:
    report = reconcile_account(
        _snapshot(
            balances=_balances(btc_available="0.01"),
            inventory_history_complete=False,
        )
    )

    assert report.status is ReconciliationStatus.UNRESOLVED
    assert report.opening_inventory.classification is OpeningInventoryClassification.INDETERMINATE
    assert ReasonCode.INCOMPLETE_INVENTORY_PROVENANCE in report.reason_codes


def test_unmatched_hint_is_unresolved_and_never_promoted_to_a_fill() -> None:
    report = reconcile_account(_snapshot(hints=(_hint(),)))

    assert report.status is ReconciliationStatus.UNRESOLVED
    assert report.legacy_matches == ()
    assert report.reason_codes == (ReasonCode.UNMATCHED_LEGACY_HINT,)
    assert report.opening_inventory.legacy_net_btc == 0


def test_ambiguous_attribute_match_disarms_without_guessing() -> None:
    orders = (
        _order("order-a", state=OrderState.CANCELED, filled="0"),
        _order("order-b", state=OrderState.CANCELED, filled="0"),
    )
    report = reconcile_account(
        _snapshot(orders=orders, hints=(_hint(order_id=None, client_order_id=None),))
    )

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.AMBIGUOUS_LEGACY_HINT in report.reason_codes
    assert report.legacy_matches == ()
    assert report.opening_inventory.classification is OpeningInventoryClassification.INDETERMINATE


def test_explicit_legacy_identity_with_conflicting_attributes_disarms() -> None:
    report = reconcile_account(
        _snapshot(
            orders=(_order(state=OrderState.CANCELED, filled="0", quantity="0.02"),),
            hints=(_hint(quantity="0.01"),),
        )
    )

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.LEGACY_HINT_CONFLICT in report.reason_codes
    assert report.legacy_matches == ()


def test_all_supplied_legacy_identifiers_must_resolve_to_the_same_order() -> None:
    order = _order(
        state=OrderState.CANCELED,
        filled="0",
        client_order_id="authoritative-client-id",
    )
    hint = _hint(client_order_id="conflicting-client-id")

    report = reconcile_account(_snapshot(orders=(order,), hints=(hint,)))

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.LEGACY_HINT_CONFLICT in report.reason_codes
    assert report.legacy_matches == ()


def test_multiple_hints_cannot_claim_the_same_authoritative_order() -> None:
    order = _order(state=OrderState.CANCELED, filled="0")
    report = reconcile_account(
        _snapshot(
            orders=(order,),
            hints=(_hint("hint-a"), _hint("hint-b")),
        )
    )

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.ORDER_MATCHED_BY_MULTIPLE_HINTS in report.reason_codes
    assert tuple(match.order_id for match in report.legacy_matches) == ("order-1", "order-1")


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            _snapshot(
                orders=(
                    _order(
                        state=OrderState.CANCELED,
                        filled="0",
                        ownership=OrderOwnership.BOT,
                    ),
                    _order(
                        state=OrderState.CANCELED,
                        filled="0",
                        ownership=OrderOwnership.BOT,
                    ),
                )
            ),
            ReasonCode.DUPLICATE_ORDER_ID,
        ),
        (
            _snapshot(
                orders=(_order(ownership=OrderOwnership.BOT),),
                trades=(_trade(), _trade()),
            ),
            ReasonCode.DUPLICATE_TRADE_ID,
        ),
        (
            _snapshot(
                balances=_balances(btc_available="0.01"),
                orders=(_order(ownership=OrderOwnership.LEGACY),),
                trades=(_trade(),),
                fees=(
                    TradeFee("fee-1", "trade-1", "CAD", Decimal("1")),
                    TradeFee("fee-1", "trade-1", "CAD", Decimal("1")),
                ),
            ),
            ReasonCode.DUPLICATE_FEE_ID,
        ),
        (
            _snapshot(
                orders=(_order(state=OrderState.CANCELED, filled="0"),),
                hints=(_hint(), _hint()),
            ),
            ReasonCode.DUPLICATE_LEGACY_HINT_ID,
        ),
    ],
)
def test_duplicate_authoritative_or_hint_identifiers_disarm(
    snapshot: AccountSnapshot,
    reason: ReasonCode,
) -> None:
    report = reconcile_account(snapshot)

    assert report.status is ReconciliationStatus.DISARMED
    assert reason in report.reason_codes


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            _snapshot(balances=(*_balances(), AssetBalance("ETH", Decimal("1"), Decimal("0")))),
            ReasonCode.UNKNOWN_ASSET,
        ),
        (
            _snapshot(
                orders=(
                    _order(
                        pair="ETH/CAD",
                        state=OrderState.CANCELED,
                        filled="0",
                        ownership=OrderOwnership.BOT,
                    ),
                )
            ),
            ReasonCode.UNKNOWN_PAIR,
        ),
        (
            _snapshot(liabilities=(Liability("CAD", Decimal("0.01")),)),
            ReasonCode.NONZERO_LIABILITY,
        ),
        (
            _snapshot(balances=(AssetBalance("CAD", Decimal("1000"), Decimal("0")),)),
            ReasonCode.MISSING_REQUIRED_BALANCE,
        ),
    ],
)
def test_unsupported_assets_pairs_liabilities_and_incomplete_balances_disarm(
    snapshot: AccountSnapshot,
    reason: ReasonCode,
) -> None:
    report = reconcile_account(snapshot)

    assert report.status is ReconciliationStatus.DISARMED
    assert reason in report.reason_codes


@pytest.mark.parametrize(
    ("ownership", "reason"),
    [
        (OrderOwnership.MANUAL, ReasonCode.OPEN_MANUAL_ORDER),
        (OrderOwnership.UNKNOWN, ReasonCode.OPEN_UNKNOWN_ORDER),
    ],
)
def test_unattributed_open_orders_disarm(
    ownership: OrderOwnership,
    reason: ReasonCode,
) -> None:
    order = _order(
        state=OrderState.OPEN,
        filled="0",
        ownership=ownership,
    )
    report = reconcile_account(_snapshot(orders=(order,)))

    assert report.status is ReconciliationStatus.DISARMED
    assert reason in report.reason_codes
    assert report.open_order_ids == ("order-1",)


def test_uniquely_matched_legacy_open_order_can_explain_held_cash() -> None:
    order = _order(state=OrderState.OPEN, filled="0")
    report = reconcile_account(
        _snapshot(
            balances=_balances(cad_available="900", cad_held="100"),
            orders=(order,),
            hints=(_hint(),),
        )
    )

    assert report.status is ReconciliationStatus.CLEAN
    assert report.open_order_ids == ("order-1",)
    assert report.opening_inventory.cad_total == Decimal("1000")


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            _snapshot(
                orders=(_order(ownership=OrderOwnership.BOT),),
            ),
            ReasonCode.INCONSISTENT_FILL_TOTAL,
        ),
        (
            _snapshot(
                orders=(_order(ownership=OrderOwnership.BOT),),
                trades=(_trade(side=Side.SELL),),
            ),
            ReasonCode.INCONSISTENT_TRADE,
        ),
        (
            _snapshot(trades=(_trade(order_id="missing-order"),)),
            ReasonCode.UNKNOWN_TRADE_ORDER,
        ),
    ],
)
def test_inconsistent_or_orphaned_fills_disarm(
    snapshot: AccountSnapshot,
    reason: ReasonCode,
) -> None:
    report = reconcile_account(snapshot)

    assert report.status is ReconciliationStatus.DISARMED
    assert reason in report.reason_codes


def test_invalid_trade_and_its_fee_never_enter_derived_accounting() -> None:
    future_trade = replace(_trade(), executed_at=OBSERVED_AT + timedelta(seconds=1))
    fee = TradeFee("fee-1", "trade-1", "BTC", Decimal("0.001"))
    report = reconcile_account(
        _snapshot(
            balances=_balances(btc_available="0.01"),
            orders=(_order(ownership=OrderOwnership.LEGACY),),
            trades=(future_trade,),
            fees=(fee,),
        )
    )

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.INVALID_TRADE in report.reason_codes
    assert ReasonCode.INVALID_FEE in report.reason_codes
    assert report.opening_inventory.legacy_net_btc == 0
    assert report.total_fees_btc == 0
    assert report.opening_inventory.classification is OpeningInventoryClassification.INDETERMINATE


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            _snapshot(
                orders=(
                    replace(
                        _order(
                            state=OrderState.CANCELED,
                            filled="0",
                            ownership=OrderOwnership.BOT,
                        ),
                        opened_at=OBSERVED_AT + timedelta(seconds=1),
                        closed_at=OBSERVED_AT + timedelta(seconds=2),
                    ),
                )
            ),
            ReasonCode.INVALID_ORDER,
        ),
        (
            _snapshot(
                hints=(
                    replace(
                        _hint(),
                        window_start=OBSERVED_AT + timedelta(seconds=1),
                        window_end=OBSERVED_AT + timedelta(seconds=2),
                    ),
                )
            ),
            ReasonCode.INVALID_LEGACY_HINT,
        ),
    ],
)
def test_future_dated_evidence_disarms(
    snapshot: AccountSnapshot,
    reason: ReasonCode,
) -> None:
    report = reconcile_account(snapshot)

    assert report.status is ReconciliationStatus.DISARMED
    assert reason in report.reason_codes
    assert report.opening_inventory.classification is OpeningInventoryClassification.INDETERMINATE


def test_held_balance_without_corresponding_open_order_disarms() -> None:
    report = reconcile_account(_snapshot(balances=_balances(cad_available="900", cad_held="100")))

    assert report.status is ReconciliationStatus.DISARMED
    assert ReasonCode.UNEXPLAINED_HELD_BALANCE in report.reason_codes


@pytest.mark.parametrize(
    ("btc_balance", "legacy_quantity", "classification", "reason"),
    [
        (
            "0.01",
            None,
            OpeningInventoryClassification.EXTERNAL_OR_UNATTRIBUTED_BTC,
            ReasonCode.UNATTRIBUTED_BTC_INVENTORY,
        ),
        (
            "0.02",
            "0.01",
            OpeningInventoryClassification.MIXED_LEGACY_AND_UNATTRIBUTED_BTC,
            ReasonCode.UNATTRIBUTED_BTC_INVENTORY,
        ),
        (
            "0.005",
            "0.01",
            OpeningInventoryClassification.LEGACY_BTC_BALANCE_SHORTFALL,
            ReasonCode.LEGACY_BTC_BALANCE_MISMATCH,
        ),
    ],
)
def test_opening_btc_inventory_is_classified_without_assumption(
    btc_balance: str,
    legacy_quantity: str | None,
    classification: OpeningInventoryClassification,
    reason: ReasonCode,
) -> None:
    if legacy_quantity is None:
        snapshot = _snapshot(balances=_balances(btc_available=btc_balance))
    else:
        snapshot = _snapshot(
            balances=_balances(btc_available=btc_balance),
            orders=(_order(quantity=legacy_quantity),),
            trades=(_trade(quantity=legacy_quantity),),
            hints=(_hint(quantity=legacy_quantity),),
        )

    report = reconcile_account(snapshot)

    assert report.status is ReconciliationStatus.UNRESOLVED
    assert report.opening_inventory.classification is classification
    assert reason in report.reason_codes


def test_legacy_net_sale_requires_preexisting_btc_inventory() -> None:
    report = reconcile_account(
        _snapshot(
            orders=(_order(side=Side.SELL),),
            trades=(_trade(side=Side.SELL),),
            hints=(_hint(side=Side.SELL),),
        )
    )

    assert report.status is ReconciliationStatus.UNRESOLVED
    assert (
        report.opening_inventory.classification
        is OpeningInventoryClassification.PREEXISTING_BTC_REQUIRED
    )
    assert report.opening_inventory.legacy_net_btc == Decimal("-0.01")
    assert ReasonCode.PREEXISTING_BTC_REQUIRED in report.reason_codes


def test_unknown_closed_order_remains_unresolved() -> None:
    report = reconcile_account(_snapshot(orders=(_order(state=OrderState.CANCELED, filled="0"),)))

    assert report.status is ReconciliationStatus.UNRESOLVED
    assert report.reason_codes == (ReasonCode.UNKNOWN_CLOSED_ORDER,)


def test_content_address_changes_when_an_authoritative_fact_changes() -> None:
    first = reconcile_account(_snapshot())
    second = reconcile_account(_snapshot(balances=_balances(cad_available="1000.01")))

    assert first.source_hash != second.source_hash
    assert first.content_hash != second.content_hash
    assert first.report_id != second.report_id


def test_domain_inputs_require_decimal_utc_and_immutable_tuples() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        AssetBalance("CAD", 1, Decimal("0"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="UTC"):
        replace(_snapshot(), observed_at=datetime(2026, 8, 31, 12))
    with pytest.raises(TypeError, match="tuple"):
        replace(_snapshot(), balances=list(_balances()))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="zero-write"):
        ZeroWriteProof(network_calls=1)
