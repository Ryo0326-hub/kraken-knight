"""Pure, deterministic reconciliation of authoritative account observations.

This module deliberately has no network, database, clock, or exchange adapter.
It accepts immutable observations, validates their internal relationships, and
returns a content-addressed report.  Legacy submission hints are evidence to be
matched against authoritative orders; they are never promoted to exchange facts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from kraken_knight.provenance import sha256_json

SUPPORTED_ASSETS = frozenset({"BTC", "CAD"})
SUPPORTED_PAIR = "BTC/CAD"
RECONCILIATION_SCHEMA_VERSION = 1


class ReconciliationStatus(StrEnum):
    """Overall account disposition after reconciliation."""

    CLEAN = "CLEAN"
    UNRESOLVED = "UNRESOLVED"
    DISARMED = "DISARMED"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderState(StrEnum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self in {OrderState.OPEN, OrderState.PARTIALLY_FILLED}


class OrderOwnership(StrEnum):
    """Ownership established by a trusted local ledger or an operator review."""

    BOT = "bot"
    LEGACY = "legacy"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class LegacyMatchBasis(StrEnum):
    ORDER_ID = "order_id"
    CLIENT_ORDER_ID = "client_order_id"
    EXACT_ATTRIBUTES = "exact_attributes"


class OpeningInventoryClassification(StrEnum):
    CASH_ONLY = "cash_only"
    CONFIRMED_LEGACY_BTC = "confirmed_legacy_btc"
    EXTERNAL_OR_UNATTRIBUTED_BTC = "external_or_unattributed_btc"
    MIXED_LEGACY_AND_UNATTRIBUTED_BTC = "mixed_legacy_and_unattributed_btc"
    LEGACY_BTC_BALANCE_SHORTFALL = "legacy_btc_balance_shortfall"
    PREEXISTING_BTC_REQUIRED = "preexisting_btc_required"
    INDETERMINATE = "indeterminate"


class ReasonCode(StrEnum):
    UNKNOWN_ASSET = "unknown_asset"
    UNKNOWN_PAIR = "unknown_pair"
    MISSING_REQUIRED_BALANCE = "missing_required_balance"
    DUPLICATE_BALANCE_ASSET = "duplicate_balance_asset"
    NEGATIVE_BALANCE = "negative_balance"
    DUPLICATE_LIABILITY_ASSET = "duplicate_liability_asset"
    INVALID_LIABILITY = "invalid_liability"
    NONZERO_LIABILITY = "nonzero_liability"
    DUPLICATE_ORDER_ID = "duplicate_order_id"
    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
    INVALID_ORDER = "invalid_order"
    DUPLICATE_TRADE_ID = "duplicate_trade_id"
    INVALID_TRADE = "invalid_trade"
    UNKNOWN_TRADE_ORDER = "unknown_trade_order"
    INCONSISTENT_TRADE = "inconsistent_trade"
    INCONSISTENT_FILL_TOTAL = "inconsistent_fill_total"
    DUPLICATE_FEE_ID = "duplicate_fee_id"
    INVALID_FEE = "invalid_fee"
    UNKNOWN_FEE_TRADE = "unknown_fee_trade"
    DUPLICATE_LEGACY_HINT_ID = "duplicate_legacy_hint_id"
    INVALID_LEGACY_HINT = "invalid_legacy_hint"
    UNMATCHED_LEGACY_HINT = "unmatched_legacy_hint"
    AMBIGUOUS_LEGACY_HINT = "ambiguous_legacy_hint"
    LEGACY_HINT_CONFLICT = "legacy_hint_conflict"
    UNVERIFIED_ATTRIBUTE_MATCH = "unverified_attribute_match"
    ORDER_MATCHED_BY_MULTIPLE_HINTS = "order_matched_by_multiple_hints"
    OPEN_MANUAL_ORDER = "open_manual_order"
    OPEN_UNKNOWN_ORDER = "open_unknown_order"
    UNKNOWN_CLOSED_ORDER = "unknown_closed_order"
    UNEXPLAINED_HELD_BALANCE = "unexplained_held_balance"
    UNATTRIBUTED_BTC_INVENTORY = "unattributed_btc_inventory"
    LEGACY_BTC_BALANCE_MISMATCH = "legacy_btc_balance_mismatch"
    PREEXISTING_BTC_REQUIRED = "preexisting_btc_required"
    INCOMPLETE_INVENTORY_PROVENANCE = "incomplete_inventory_provenance"


_DISARMING_CODES = frozenset(
    {
        ReasonCode.UNKNOWN_ASSET,
        ReasonCode.UNKNOWN_PAIR,
        ReasonCode.MISSING_REQUIRED_BALANCE,
        ReasonCode.DUPLICATE_BALANCE_ASSET,
        ReasonCode.NEGATIVE_BALANCE,
        ReasonCode.DUPLICATE_LIABILITY_ASSET,
        ReasonCode.INVALID_LIABILITY,
        ReasonCode.NONZERO_LIABILITY,
        ReasonCode.DUPLICATE_ORDER_ID,
        ReasonCode.DUPLICATE_CLIENT_ORDER_ID,
        ReasonCode.INVALID_ORDER,
        ReasonCode.DUPLICATE_TRADE_ID,
        ReasonCode.INVALID_TRADE,
        ReasonCode.UNKNOWN_TRADE_ORDER,
        ReasonCode.INCONSISTENT_TRADE,
        ReasonCode.INCONSISTENT_FILL_TOTAL,
        ReasonCode.DUPLICATE_FEE_ID,
        ReasonCode.INVALID_FEE,
        ReasonCode.UNKNOWN_FEE_TRADE,
        ReasonCode.DUPLICATE_LEGACY_HINT_ID,
        ReasonCode.INVALID_LEGACY_HINT,
        ReasonCode.AMBIGUOUS_LEGACY_HINT,
        ReasonCode.LEGACY_HINT_CONFLICT,
        ReasonCode.ORDER_MATCHED_BY_MULTIPLE_HINTS,
        ReasonCode.OPEN_MANUAL_ORDER,
        ReasonCode.OPEN_UNKNOWN_ORDER,
        ReasonCode.UNEXPLAINED_HELD_BALANCE,
    }
)

_INVENTORY_UNRELIABLE_CODES = frozenset(
    {
        ReasonCode.UNKNOWN_ASSET,
        ReasonCode.UNKNOWN_PAIR,
        ReasonCode.MISSING_REQUIRED_BALANCE,
        ReasonCode.DUPLICATE_BALANCE_ASSET,
        ReasonCode.NEGATIVE_BALANCE,
        ReasonCode.DUPLICATE_LIABILITY_ASSET,
        ReasonCode.INVALID_LIABILITY,
        ReasonCode.NONZERO_LIABILITY,
        ReasonCode.DUPLICATE_ORDER_ID,
        ReasonCode.DUPLICATE_CLIENT_ORDER_ID,
        ReasonCode.INVALID_ORDER,
        ReasonCode.DUPLICATE_TRADE_ID,
        ReasonCode.INVALID_TRADE,
        ReasonCode.UNKNOWN_TRADE_ORDER,
        ReasonCode.INCONSISTENT_TRADE,
        ReasonCode.INCONSISTENT_FILL_TOTAL,
        ReasonCode.DUPLICATE_FEE_ID,
        ReasonCode.INVALID_FEE,
        ReasonCode.UNKNOWN_FEE_TRADE,
        ReasonCode.DUPLICATE_LEGACY_HINT_ID,
        ReasonCode.INVALID_LEGACY_HINT,
        ReasonCode.UNMATCHED_LEGACY_HINT,
        ReasonCode.AMBIGUOUS_LEGACY_HINT,
        ReasonCode.LEGACY_HINT_CONFLICT,
        ReasonCode.ORDER_MATCHED_BY_MULTIPLE_HINTS,
        ReasonCode.OPEN_UNKNOWN_ORDER,
        ReasonCode.UNKNOWN_CLOSED_ORDER,
    }
)


def _require_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    return normalized


def _require_decimal(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _require_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value


@dataclass(frozen=True, slots=True)
class AssetBalance:
    asset: str
    available: Decimal
    held: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", _require_identifier(self.asset, field="asset"))
        object.__setattr__(
            self,
            "available",
            _require_decimal(self.available, field="available"),
        )
        object.__setattr__(self, "held", _require_decimal(self.held, field="held"))

    @property
    def total(self) -> Decimal:
        return self.available + self.held


@dataclass(frozen=True, slots=True)
class Liability:
    asset: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", _require_identifier(self.asset, field="asset"))
        object.__setattr__(self, "amount", _require_decimal(self.amount, field="amount"))


@dataclass(frozen=True, slots=True)
class AuthoritativeOrder:
    order_id: str
    pair: str
    side: Side
    state: OrderState
    quantity_btc: Decimal
    filled_quantity_btc: Decimal
    opened_at: datetime
    closed_at: datetime | None = None
    limit_price_cad: Decimal | None = None
    client_order_id: str | None = None
    ownership: OrderOwnership = OrderOwnership.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _require_identifier(self.order_id, field="order_id"))
        object.__setattr__(self, "pair", _require_identifier(self.pair, field="pair"))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "state", OrderState(self.state))
        object.__setattr__(self, "ownership", OrderOwnership(self.ownership))
        object.__setattr__(
            self,
            "quantity_btc",
            _require_decimal(self.quantity_btc, field="quantity_btc"),
        )
        object.__setattr__(
            self,
            "filled_quantity_btc",
            _require_decimal(self.filled_quantity_btc, field="filled_quantity_btc"),
        )
        object.__setattr__(self, "opened_at", _require_utc(self.opened_at, field="opened_at"))
        if self.closed_at is not None:
            object.__setattr__(
                self,
                "closed_at",
                _require_utc(self.closed_at, field="closed_at"),
            )
        if self.limit_price_cad is not None:
            object.__setattr__(
                self,
                "limit_price_cad",
                _require_decimal(self.limit_price_cad, field="limit_price_cad"),
            )
        if self.client_order_id is not None:
            object.__setattr__(
                self,
                "client_order_id",
                _require_identifier(self.client_order_id, field="client_order_id"),
            )


@dataclass(frozen=True, slots=True)
class AuthoritativeTrade:
    trade_id: str
    order_id: str
    pair: str
    side: Side
    quantity_btc: Decimal
    price_cad: Decimal
    executed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", _require_identifier(self.trade_id, field="trade_id"))
        object.__setattr__(self, "order_id", _require_identifier(self.order_id, field="order_id"))
        object.__setattr__(self, "pair", _require_identifier(self.pair, field="pair"))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(
            self,
            "quantity_btc",
            _require_decimal(self.quantity_btc, field="quantity_btc"),
        )
        object.__setattr__(self, "price_cad", _require_decimal(self.price_cad, field="price_cad"))
        object.__setattr__(
            self,
            "executed_at",
            _require_utc(self.executed_at, field="executed_at"),
        )


@dataclass(frozen=True, slots=True)
class TradeFee:
    fee_id: str
    trade_id: str
    asset: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "fee_id", _require_identifier(self.fee_id, field="fee_id"))
        object.__setattr__(self, "trade_id", _require_identifier(self.trade_id, field="trade_id"))
        object.__setattr__(self, "asset", _require_identifier(self.asset, field="asset"))
        object.__setattr__(self, "amount", _require_decimal(self.amount, field="amount"))


@dataclass(frozen=True, slots=True)
class LegacySubmissionHint:
    """Non-authoritative evidence describing one legacy submission attempt."""

    hint_id: str
    pair: str
    side: Side
    quantity_btc: Decimal
    window_start: datetime
    window_end: datetime
    limit_price_cad: Decimal | None = None
    order_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hint_id", _require_identifier(self.hint_id, field="hint_id"))
        object.__setattr__(self, "pair", _require_identifier(self.pair, field="pair"))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(
            self,
            "quantity_btc",
            _require_decimal(self.quantity_btc, field="quantity_btc"),
        )
        object.__setattr__(
            self,
            "window_start",
            _require_utc(self.window_start, field="window_start"),
        )
        object.__setattr__(
            self,
            "window_end",
            _require_utc(self.window_end, field="window_end"),
        )
        if self.window_end < self.window_start:
            raise ValueError("window_end cannot precede window_start")
        if self.limit_price_cad is not None:
            object.__setattr__(
                self,
                "limit_price_cad",
                _require_decimal(self.limit_price_cad, field="limit_price_cad"),
            )
        if self.order_id is not None:
            object.__setattr__(
                self,
                "order_id",
                _require_identifier(self.order_id, field="order_id"),
            )
        if self.client_order_id is not None:
            object.__setattr__(
                self,
                "client_order_id",
                _require_identifier(self.client_order_id, field="client_order_id"),
            )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: str
    observed_at: datetime
    balances: tuple[AssetBalance, ...]
    orders: tuple[AuthoritativeOrder, ...] = ()
    trades: tuple[AuthoritativeTrade, ...] = ()
    fees: tuple[TradeFee, ...] = ()
    liabilities: tuple[Liability, ...] = ()
    legacy_hints: tuple[LegacySubmissionHint, ...] = ()
    inventory_history_complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_id",
            _require_identifier(self.account_id, field="account_id"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_utc(self.observed_at, field="observed_at"),
        )
        if not isinstance(self.inventory_history_complete, bool):
            raise TypeError("inventory_history_complete must be a bool")
        collections: tuple[tuple[str, object, type[object]], ...] = (
            ("balances", self.balances, AssetBalance),
            ("orders", self.orders, AuthoritativeOrder),
            ("trades", self.trades, AuthoritativeTrade),
            ("fees", self.fees, TradeFee),
            ("liabilities", self.liabilities, Liability),
            ("legacy_hints", self.legacy_hints, LegacySubmissionHint),
        )
        for field_name, raw_items, expected_type in collections:
            if not isinstance(raw_items, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            if any(not isinstance(item, expected_type) for item in raw_items):
                raise TypeError(f"{field_name} contains an invalid item")


@dataclass(frozen=True, slots=True)
class Discrepancy:
    code: ReasonCode
    entity_kind: str
    entity_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class LegacySubmissionMatch:
    hint_id: str
    order_id: str
    trade_ids: tuple[str, ...]
    basis: LegacyMatchBasis


@dataclass(frozen=True, slots=True)
class OpeningInventory:
    cad_available: Decimal
    cad_held: Decimal
    cad_total: Decimal
    btc_available: Decimal
    btc_held: Decimal
    btc_total: Decimal
    legacy_net_btc: Decimal
    btc_variance_from_legacy: Decimal
    classification: OpeningInventoryClassification


@dataclass(frozen=True, slots=True)
class ZeroWriteProof:
    """Declarative proof of this pure core's intentionally absent I/O surface."""

    exchange_writes: bool = False
    network_calls: int = 0
    persistence_writes: int = 0
    implementation: str = "exchange_independent_reconciliation_v1"

    def __post_init__(self) -> None:
        if self.exchange_writes or self.network_calls != 0 or self.persistence_writes != 0:
            raise ValueError("zero-write proof fields are invariant")


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    report_id: str
    content_hash: str
    schema_version: int
    source_hash: str
    account_id: str
    observed_at: datetime
    status: ReconciliationStatus
    reason_codes: tuple[ReasonCode, ...]
    discrepancies: tuple[Discrepancy, ...]
    legacy_matches: tuple[LegacySubmissionMatch, ...]
    opening_inventory: OpeningInventory
    open_order_ids: tuple[str, ...]
    total_fees_cad: Decimal
    total_fees_btc: Decimal
    zero_write_proof: ZeroWriteProof


def _stable_records[T](records: Iterable[T], identifier: Callable[[T], str]) -> tuple[T, ...]:
    return tuple(sorted(records, key=lambda record: (identifier(record), sha256_json(record))))


def _duplicate_values(values: Iterable[str]) -> tuple[str, ...]:
    counts = Counter(values)
    return tuple(sorted(value for value, count in counts.items() if count > 1))


def _add(
    discrepancies: list[Discrepancy],
    code: ReasonCode,
    entity_kind: str,
    entity_id: str,
    detail: str,
) -> None:
    discrepancies.append(
        Discrepancy(
            code=code,
            entity_kind=entity_kind,
            entity_id=entity_id,
            detail=detail,
        )
    )


def _validate_order(
    order: AuthoritativeOrder,
    *,
    observed_at: datetime,
    discrepancies: list[Discrepancy],
) -> bool:
    if order.pair != SUPPORTED_PAIR:
        _add(
            discrepancies,
            ReasonCode.UNKNOWN_PAIR,
            "order",
            order.order_id,
            f"unsupported pair {order.pair}",
        )
    invalid = (
        order.quantity_btc <= 0
        or order.filled_quantity_btc < 0
        or order.filled_quantity_btc > order.quantity_btc
        or (order.limit_price_cad is not None and order.limit_price_cad <= 0)
        or (order.closed_at is not None and order.closed_at < order.opened_at)
        or order.opened_at > observed_at
        or (order.closed_at is not None and order.closed_at > observed_at)
    )
    if order.state is OrderState.OPEN:
        invalid = invalid or order.filled_quantity_btc != 0 or order.closed_at is not None
    elif order.state is OrderState.PARTIALLY_FILLED:
        invalid = invalid or not (Decimal("0") < order.filled_quantity_btc < order.quantity_btc)
        invalid = invalid or order.closed_at is not None
    elif order.state is OrderState.FILLED:
        invalid = invalid or order.filled_quantity_btc != order.quantity_btc
        invalid = invalid or order.closed_at is None
    elif order.state in {OrderState.CANCELED, OrderState.EXPIRED}:
        invalid = invalid or order.filled_quantity_btc >= order.quantity_btc
        invalid = invalid or order.closed_at is None
    elif order.state is OrderState.REJECTED:
        invalid = invalid or order.filled_quantity_btc != 0 or order.closed_at is None
    if invalid:
        _add(
            discrepancies,
            ReasonCode.INVALID_ORDER,
            "order",
            order.order_id,
            "quantity, fill, price, timestamp, or state invariants failed",
        )
    return not invalid and order.pair == SUPPORTED_PAIR


def _hint_candidates(
    hint: LegacySubmissionHint,
    orders: tuple[AuthoritativeOrder, ...],
) -> tuple[tuple[AuthoritativeOrder, ...], LegacyMatchBasis, bool]:
    """Return attribute-consistent candidates, match basis, and identity conflict."""

    identity_pool: tuple[AuthoritativeOrder, ...]
    if hint.order_id is not None:
        identity_pool = tuple(order for order in orders if order.order_id == hint.order_id)
        basis = LegacyMatchBasis.ORDER_ID
    elif hint.client_order_id is not None:
        identity_pool = tuple(
            order for order in orders if order.client_order_id == hint.client_order_id
        )
        basis = LegacyMatchBasis.CLIENT_ORDER_ID
    else:
        identity_pool = orders
        basis = LegacyMatchBasis.EXACT_ATTRIBUTES

    candidates = tuple(
        order
        for order in identity_pool
        if (hint.order_id is None or order.order_id == hint.order_id)
        and (hint.client_order_id is None or order.client_order_id == hint.client_order_id)
        and order.pair == hint.pair
        and order.side is hint.side
        and order.quantity_btc == hint.quantity_btc
        and hint.window_start <= order.opened_at <= hint.window_end
        and (hint.limit_price_cad is None or order.limit_price_cad == hint.limit_price_cad)
    )
    explicit_identity = hint.order_id is not None or hint.client_order_id is not None
    identity_conflict = explicit_identity and bool(identity_pool) and not candidates
    return candidates, basis, identity_conflict


def reconcile_account(snapshot: AccountSnapshot) -> ReconciliationReport:
    """Reconcile one immutable snapshot without performing I/O or guessing."""

    balances = _stable_records(snapshot.balances, lambda item: item.asset)
    orders = _stable_records(snapshot.orders, lambda item: item.order_id)
    trades = _stable_records(snapshot.trades, lambda item: item.trade_id)
    fees = _stable_records(snapshot.fees, lambda item: item.fee_id)
    liabilities = _stable_records(snapshot.liabilities, lambda item: item.asset)
    hints = _stable_records(snapshot.legacy_hints, lambda item: item.hint_id)

    source_hash = sha256_json(
        {
            "account_id": snapshot.account_id,
            "observed_at": snapshot.observed_at,
            "balances": balances,
            "orders": orders,
            "trades": trades,
            "fees": fees,
            "liabilities": liabilities,
            "legacy_hints": hints,
            "inventory_history_complete": snapshot.inventory_history_complete,
        }
    )
    discrepancies: list[Discrepancy] = []

    for asset in _duplicate_values(balance.asset for balance in balances):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_BALANCE_ASSET,
            "balance",
            asset,
            "more than one authoritative balance exists for the asset",
        )
    for balance in balances:
        if balance.asset not in SUPPORTED_ASSETS:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_ASSET,
                "balance",
                balance.asset,
                "asset is outside the BTC/CAD account boundary",
            )
        if balance.available < 0 or balance.held < 0:
            _add(
                discrepancies,
                ReasonCode.NEGATIVE_BALANCE,
                "balance",
                balance.asset,
                "available and held balances must be nonnegative",
            )
    balance_by_asset: dict[str, AssetBalance] = {}
    for balance in balances:
        balance_by_asset.setdefault(balance.asset, balance)
    for required_asset in sorted(SUPPORTED_ASSETS):
        if required_asset not in balance_by_asset:
            _add(
                discrepancies,
                ReasonCode.MISSING_REQUIRED_BALANCE,
                "balance",
                required_asset,
                "a complete reconciliation requires BTC and CAD balances",
            )

    for asset in _duplicate_values(liability.asset for liability in liabilities):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_LIABILITY_ASSET,
            "liability",
            asset,
            "more than one liability observation exists for the asset",
        )
    for liability in liabilities:
        if liability.asset not in SUPPORTED_ASSETS:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_ASSET,
                "liability",
                liability.asset,
                "liability asset is outside the BTC/CAD account boundary",
            )
        if liability.amount < 0:
            _add(
                discrepancies,
                ReasonCode.INVALID_LIABILITY,
                "liability",
                liability.asset,
                "liability amount cannot be negative",
            )
        elif liability.amount > 0:
            _add(
                discrepancies,
                ReasonCode.NONZERO_LIABILITY,
                "liability",
                liability.asset,
                "spot-only operation requires zero liabilities",
            )

    duplicate_order_ids = set(_duplicate_values(order.order_id for order in orders))
    for order_id in sorted(duplicate_order_ids):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_ORDER_ID,
            "order",
            order_id,
            "authoritative order identifiers must be unique",
        )
    client_ids = tuple(
        order.client_order_id for order in orders if order.client_order_id is not None
    )
    duplicate_client_order_ids = set(_duplicate_values(client_ids))
    for client_order_id in sorted(duplicate_client_order_ids):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_CLIENT_ORDER_ID,
            "order",
            client_order_id,
            "client order identifiers must be globally unique in the local ledger",
        )
    valid_order_ids: set[str] = set()
    for order in orders:
        is_valid = _validate_order(
            order,
            observed_at=snapshot.observed_at,
            discrepancies=discrepancies,
        )
        if (
            is_valid
            and order.order_id not in duplicate_order_ids
            and order.client_order_id not in duplicate_client_order_ids
        ):
            valid_order_ids.add(order.order_id)
    order_by_id: dict[str, AuthoritativeOrder] = {}
    for order in orders:
        order_by_id.setdefault(order.order_id, order)

    duplicate_trade_ids = set(_duplicate_values(trade.trade_id for trade in trades))
    for trade_id in sorted(duplicate_trade_ids):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_TRADE_ID,
            "trade",
            trade_id,
            "authoritative trade identifiers must be unique",
        )
    trade_by_id: dict[str, AuthoritativeTrade] = {}
    valid_trade_ids: set[str] = set()
    for trade in trades:
        trade_by_id.setdefault(trade.trade_id, trade)
        if trade.pair != SUPPORTED_PAIR:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_PAIR,
                "trade",
                trade.trade_id,
                f"unsupported pair {trade.pair}",
            )
        if (
            trade.quantity_btc <= 0
            or trade.price_cad <= 0
            or trade.executed_at > snapshot.observed_at
        ):
            _add(
                discrepancies,
                ReasonCode.INVALID_TRADE,
                "trade",
                trade.trade_id,
                "trade quantity and price must be positive",
            )
            continue
        referenced_order = order_by_id.get(trade.order_id)
        if referenced_order is None:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_TRADE_ORDER,
                "trade",
                trade.trade_id,
                f"referenced order {trade.order_id} is absent",
            )
            continue
        inconsistent = (
            trade.pair != referenced_order.pair
            or trade.side is not referenced_order.side
            or trade.executed_at < referenced_order.opened_at
            or (
                referenced_order.closed_at is not None
                and trade.executed_at > referenced_order.closed_at
            )
        )
        if inconsistent:
            _add(
                discrepancies,
                ReasonCode.INCONSISTENT_TRADE,
                "trade",
                trade.trade_id,
                "trade disagrees with its authoritative order",
            )
            continue
        if (
            trade.trade_id not in duplicate_trade_ids
            and referenced_order.order_id in valid_order_ids
            and trade.pair == SUPPORTED_PAIR
        ):
            valid_trade_ids.add(trade.trade_id)

    for order in orders:
        if order.order_id not in valid_order_ids:
            continue
        observed_fill = sum(
            (
                trade.quantity_btc
                for trade in trades
                if trade.trade_id in valid_trade_ids and trade.order_id == order.order_id
            ),
            start=Decimal("0"),
        )
        if observed_fill != order.filled_quantity_btc:
            _add(
                discrepancies,
                ReasonCode.INCONSISTENT_FILL_TOTAL,
                "order",
                order.order_id,
                "sum of unique authoritative trades does not equal filled quantity",
            )

    duplicate_fee_ids = set(_duplicate_values(fee.fee_id for fee in fees))
    for fee_id in sorted(duplicate_fee_ids):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_FEE_ID,
            "fee",
            fee_id,
            "fee identifiers must be unique",
        )
    total_fees = {"BTC": Decimal("0"), "CAD": Decimal("0")}
    valid_fee_ids: set[str] = set()
    for fee in fees:
        if fee.asset not in SUPPORTED_ASSETS:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_ASSET,
                "fee",
                fee.fee_id,
                f"unsupported fee asset {fee.asset}",
            )
        if fee.amount < 0:
            _add(
                discrepancies,
                ReasonCode.INVALID_FEE,
                "fee",
                fee.fee_id,
                "fee amount cannot be negative",
            )
        if fee.trade_id not in trade_by_id:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_FEE_TRADE,
                "fee",
                fee.fee_id,
                f"referenced trade {fee.trade_id} is absent",
            )
        elif fee.trade_id not in valid_trade_ids:
            _add(
                discrepancies,
                ReasonCode.INVALID_FEE,
                "fee",
                fee.fee_id,
                "fee does not reference a unique valid trade",
            )
        if (
            fee.fee_id not in duplicate_fee_ids
            and fee.asset in SUPPORTED_ASSETS
            and fee.amount >= 0
            and fee.trade_id in valid_trade_ids
        ):
            total_fees[fee.asset] += fee.amount
            valid_fee_ids.add(fee.fee_id)

    for hint_id in _duplicate_values(hint.hint_id for hint in hints):
        _add(
            discrepancies,
            ReasonCode.DUPLICATE_LEGACY_HINT_ID,
            "legacy_hint",
            hint_id,
            "legacy hint identifiers must be unique",
        )
    matches: list[LegacySubmissionMatch] = []
    hints_by_order: defaultdict[str, list[str]] = defaultdict(list)
    matchable_orders = tuple(order for order in orders if order.order_id in valid_order_ids)
    for hint in hints:
        if hint.pair != SUPPORTED_PAIR:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_PAIR,
                "legacy_hint",
                hint.hint_id,
                f"unsupported pair {hint.pair}",
            )
            continue
        if (
            hint.quantity_btc <= 0
            or (hint.limit_price_cad is not None and hint.limit_price_cad <= 0)
            or hint.window_end > snapshot.observed_at
        ):
            _add(
                discrepancies,
                ReasonCode.INVALID_LEGACY_HINT,
                "legacy_hint",
                hint.hint_id,
                "hint quantity and optional price must be positive",
            )
            continue
        candidates, basis, identity_conflict = _hint_candidates(hint, matchable_orders)
        if identity_conflict:
            _add(
                discrepancies,
                ReasonCode.LEGACY_HINT_CONFLICT,
                "legacy_hint",
                hint.hint_id,
                "explicit identity exists but authoritative attributes disagree",
            )
        elif not candidates:
            _add(
                discrepancies,
                ReasonCode.UNMATCHED_LEGACY_HINT,
                "legacy_hint",
                hint.hint_id,
                "no authoritative order satisfies all supplied evidence",
            )
        elif len(candidates) > 1:
            candidate_ids = ",".join(sorted(order.order_id for order in candidates))
            _add(
                discrepancies,
                ReasonCode.AMBIGUOUS_LEGACY_HINT,
                "legacy_hint",
                hint.hint_id,
                f"multiple authoritative orders match: {candidate_ids}",
            )
        else:
            order = candidates[0]
            if basis is LegacyMatchBasis.EXACT_ATTRIBUTES:
                _add(
                    discrepancies,
                    ReasonCode.UNVERIFIED_ATTRIBUTE_MATCH,
                    "legacy_hint",
                    hint.hint_id,
                    "attribute-only similarity cannot establish legacy ownership",
                )
                continue
            if order.ownership in {OrderOwnership.BOT, OrderOwnership.MANUAL}:
                _add(
                    discrepancies,
                    ReasonCode.LEGACY_HINT_CONFLICT,
                    "legacy_hint",
                    hint.hint_id,
                    f"matched order is classified as {order.ownership.value}",
                )
                continue
            trade_ids = tuple(
                sorted(
                    trade.trade_id
                    for trade in trades
                    if trade.order_id == order.order_id and trade.trade_id in valid_trade_ids
                )
            )
            matches.append(
                LegacySubmissionMatch(
                    hint_id=hint.hint_id,
                    order_id=order.order_id,
                    trade_ids=trade_ids,
                    basis=basis,
                )
            )
            hints_by_order[order.order_id].append(hint.hint_id)

    for order_id, hint_ids in sorted(hints_by_order.items()):
        if len(hint_ids) > 1:
            _add(
                discrepancies,
                ReasonCode.ORDER_MATCHED_BY_MULTIPLE_HINTS,
                "order",
                order_id,
                f"matched by legacy hints: {','.join(sorted(hint_ids))}",
            )

    matched_order_ids = {match.order_id for match in matches}
    legacy_order_ids = matched_order_ids | {
        order.order_id for order in orders if order.ownership is OrderOwnership.LEGACY
    }
    for order in orders:
        if order.state.is_open:
            if order.ownership is OrderOwnership.MANUAL:
                _add(
                    discrepancies,
                    ReasonCode.OPEN_MANUAL_ORDER,
                    "order",
                    order.order_id,
                    "manual open orders are prohibited in the dedicated account",
                )
            elif (
                order.ownership is OrderOwnership.UNKNOWN
                and order.order_id not in matched_order_ids
            ):
                _add(
                    discrepancies,
                    ReasonCode.OPEN_UNKNOWN_ORDER,
                    "order",
                    order.order_id,
                    "open order is not linked to bot or legacy evidence",
                )
        elif order.ownership is OrderOwnership.UNKNOWN and order.order_id not in matched_order_ids:
            _add(
                discrepancies,
                ReasonCode.UNKNOWN_CLOSED_ORDER,
                "order",
                order.order_id,
                "closed order is not attributed to bot, legacy, or manual activity",
            )

    cad_balance = balance_by_asset.get("CAD")
    btc_balance = balance_by_asset.get("BTC")
    if cad_balance is not None and cad_balance.held > 0:
        if not any(order.state.is_open and order.side is Side.BUY for order in orders):
            _add(
                discrepancies,
                ReasonCode.UNEXPLAINED_HELD_BALANCE,
                "balance",
                "CAD",
                "held CAD has no corresponding open buy order",
            )
    if btc_balance is not None and btc_balance.held > 0:
        if not any(order.state.is_open and order.side is Side.SELL for order in orders):
            _add(
                discrepancies,
                ReasonCode.UNEXPLAINED_HELD_BALANCE,
                "balance",
                "BTC",
                "held BTC has no corresponding open sell order",
            )

    unique_trades = tuple(trade for trade in trades if trade.trade_id in valid_trade_ids)
    legacy_trades = tuple(trade for trade in unique_trades if trade.order_id in legacy_order_ids)
    legacy_net_btc = sum(
        (
            trade.quantity_btc if trade.side is Side.BUY else -trade.quantity_btc
            for trade in legacy_trades
        ),
        start=Decimal("0"),
    )
    legacy_trade_ids = {trade.trade_id for trade in legacy_trades}
    legacy_btc_fees = sum(
        (
            fee.amount
            for fee in fees
            if fee.fee_id in valid_fee_ids
            and fee.trade_id in legacy_trade_ids
            and fee.asset == "BTC"
        ),
        start=Decimal("0"),
    )
    legacy_net_btc -= legacy_btc_fees

    cad_available = Decimal("0") if cad_balance is None else cad_balance.available
    cad_held = Decimal("0") if cad_balance is None else cad_balance.held
    btc_available = Decimal("0") if btc_balance is None else btc_balance.available
    btc_held = Decimal("0") if btc_balance is None else btc_balance.held
    btc_total = btc_available + btc_held
    variance = btc_total - legacy_net_btc

    current_codes = {item.code for item in discrepancies}
    if not snapshot.inventory_history_complete and btc_total != 0:
        classification = OpeningInventoryClassification.INDETERMINATE
        _add(
            discrepancies,
            ReasonCode.INCOMPLETE_INVENTORY_PROVENANCE,
            "opening_inventory",
            "BTC",
            "BTC provenance requires complete account-lifetime history and balance continuity",
        )
    elif current_codes & _INVENTORY_UNRELIABLE_CODES:
        classification = OpeningInventoryClassification.INDETERMINATE
    elif legacy_net_btc < 0:
        classification = OpeningInventoryClassification.PREEXISTING_BTC_REQUIRED
        _add(
            discrepancies,
            ReasonCode.PREEXISTING_BTC_REQUIRED,
            "opening_inventory",
            "BTC",
            "legacy net sales require BTC inventory that predates the matched trades",
        )
    elif legacy_net_btc == 0 and btc_total == 0:
        classification = OpeningInventoryClassification.CASH_ONLY
    elif legacy_net_btc == 0:
        classification = OpeningInventoryClassification.EXTERNAL_OR_UNATTRIBUTED_BTC
        _add(
            discrepancies,
            ReasonCode.UNATTRIBUTED_BTC_INVENTORY,
            "opening_inventory",
            "BTC",
            "BTC balance is not explained by matched legacy fills",
        )
    elif variance == 0:
        classification = OpeningInventoryClassification.CONFIRMED_LEGACY_BTC
    elif variance > 0:
        classification = OpeningInventoryClassification.MIXED_LEGACY_AND_UNATTRIBUTED_BTC
        _add(
            discrepancies,
            ReasonCode.UNATTRIBUTED_BTC_INVENTORY,
            "opening_inventory",
            "BTC",
            "BTC balance exceeds net BTC from matched legacy fills",
        )
    else:
        classification = OpeningInventoryClassification.LEGACY_BTC_BALANCE_SHORTFALL
        _add(
            discrepancies,
            ReasonCode.LEGACY_BTC_BALANCE_MISMATCH,
            "opening_inventory",
            "BTC",
            "BTC balance is below net BTC from matched legacy fills",
        )

    opening_inventory = OpeningInventory(
        cad_available=cad_available,
        cad_held=cad_held,
        cad_total=cad_available + cad_held,
        btc_available=btc_available,
        btc_held=btc_held,
        btc_total=btc_total,
        legacy_net_btc=legacy_net_btc,
        btc_variance_from_legacy=variance,
        classification=classification,
    )

    normalized_discrepancies = tuple(
        sorted(
            set(discrepancies),
            key=lambda item: (
                item.code.value,
                item.entity_kind,
                item.entity_id,
                item.detail,
            ),
        )
    )
    reason_codes = tuple(
        sorted({item.code for item in normalized_discrepancies}, key=lambda item: item.value)
    )
    if any(code in _DISARMING_CODES for code in reason_codes):
        status = ReconciliationStatus.DISARMED
    elif normalized_discrepancies:
        status = ReconciliationStatus.UNRESOLVED
    else:
        status = ReconciliationStatus.CLEAN

    normalized_matches = tuple(
        sorted(matches, key=lambda item: (item.hint_id, item.order_id, item.basis.value))
    )
    zero_write_proof = ZeroWriteProof()
    open_order_ids = tuple(sorted(order.order_id for order in orders if order.state.is_open))
    report_body = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "source_hash": source_hash,
        "account_id": snapshot.account_id,
        "observed_at": snapshot.observed_at,
        "status": status,
        "reason_codes": reason_codes,
        "discrepancies": normalized_discrepancies,
        "legacy_matches": normalized_matches,
        "opening_inventory": opening_inventory,
        "open_order_ids": open_order_ids,
        "total_fees_cad": total_fees["CAD"],
        "total_fees_btc": total_fees["BTC"],
        "zero_write_proof": zero_write_proof,
    }
    content_hash = sha256_json(report_body)
    return ReconciliationReport(
        report_id=f"reconciliation_{content_hash}",
        content_hash=content_hash,
        schema_version=RECONCILIATION_SCHEMA_VERSION,
        source_hash=source_hash,
        account_id=snapshot.account_id,
        observed_at=snapshot.observed_at,
        status=status,
        reason_codes=reason_codes,
        discrepancies=normalized_discrepancies,
        legacy_matches=normalized_matches,
        opening_inventory=opening_inventory,
        open_order_ids=open_order_ids,
        total_fees_cad=total_fees["CAD"],
        total_fees_btc=total_fees["BTC"],
        zero_write_proof=zero_write_proof,
    )
