"""Authenticated read-only Kraken collection and reconciliation orchestration.

The adapter in :mod:`kraken_knight.kraken_read` owns HTTP and authentication;
the core in :mod:`kraken_knight.reconciliation` owns deterministic accounting.
This module is the narrow bridge between them.  Its endpoint sequence is fixed,
its private-request budget is bounded, and it has no exchange-write dependency.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import math
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from kraken_knight.config import Settings
from kraken_knight.kraken_read import (
    BTC_CAD_ALIASES,
    ApiKeyInfoSnapshot,
    AssetPairSnapshot,
    BalanceSnapshot,
    ClosedOrdersPage,
    KrakenReadClient,
    KrakenRequestBudget,
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
    WalletAccountsSnapshot,
)
from kraken_knight.ledger import Ledger
from kraken_knight.provenance import canonical_json_bytes, sha256_json
from kraken_knight.reconciliation import (
    AccountSnapshot,
    AssetBalance,
    AuthoritativeOrder,
    AuthoritativeTrade,
    LegacySubmissionHint,
    Liability,
    OrderOwnership,
    OrderState,
    ReconciliationReport,
    ReconciliationStatus,
    Side,
    TradeFee,
    reconcile_account,
)

READ_ONLY_PERMISSIONS = frozenset(
    {
        "query-closed-trades",
        "query-funds",
        "query-ledger",
        "query-open-trades",
    }
)
EXPECTED_LEGACY_HINTS = 5
MAX_LEGACY_HINT_BYTES = 256 * 1024
MAX_LEGACY_HINTS = 50
ACCOUNT_HISTORY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_CLOCK_SKEW_SECONDS = 30
PUBLIC_REQUEST_INTERVAL_SECONDS = 1.05
PRIVATE_COUNTER_CEILING = 19.0
PRIVATE_COUNTER_DECAY_PER_SECOND = 0.5
RECONCILIATION_PRIVATE_COST_LIMIT = 33
IDENTITY_DISCOVERY_PRIVATE_COST_LIMIT = 2

_ASSET_ALIASES = {
    "BTC": "BTC",
    "CAD": "CAD",
    "XBT": "BTC",
    "XXBT": "BTC",
    "ZCAD": "CAD",
}


class ReconciliationJobError(RuntimeError):
    """A safe, operator-facing orchestration failure."""


class KrakenReadPort(Protocol):
    """The exact read surface used by this workflow."""

    @property
    def private_cost_spent(self) -> int: ...

    def get_server_time(self) -> ServerTimeSnapshot: ...

    def get_system_status(self) -> SystemStatusSnapshot: ...

    def get_asset_pair(self, *, pair: str = "XBTCAD") -> AssetPairSnapshot: ...

    def get_api_key_info(self) -> ApiKeyInfoSnapshot: ...

    def get_wallet_accounts(self) -> WalletAccountsSnapshot: ...

    def get_trade_volume(self, *, pair: str = "XBTCAD") -> TradeVolumeSnapshot: ...

    def get_extended_balances(self) -> BalanceSnapshot: ...

    def get_open_orders(self, *, client_order_id: str | None = None) -> OpenOrdersSnapshot: ...

    def get_closed_orders(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        client_order_id: str | None = None,
    ) -> ClosedOrdersPage: ...

    def query_orders(self, order_ids: Sequence[str]) -> OrderQuerySnapshot: ...

    def get_trades_history(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        limit: int = 50,
        pair: str | None = None,
    ) -> TradeHistoryPage: ...

    def get_ledgers(
        self,
        *,
        account_id: str,
        entry_type: str = "all",
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
    ) -> LedgerPage: ...


@dataclass(frozen=True, slots=True)
class OperationalGate:
    """One deterministic precondition and its fail-closed disposition."""

    name: str
    passed: bool
    failure_status: ReconciliationStatus
    detail: str


@dataclass(frozen=True, slots=True)
class InstrumentEvidence:
    exchange_pair: str
    alternate_name: str
    websocket_name: str
    status: str
    order_minimum_btc: Decimal
    cost_minimum_cad: Decimal
    tick_size_cad: Decimal
    cost_decimals: int
    pair_decimals: int
    lot_decimals: int
    rolling_fee_volume: Decimal | None
    maker_fee_percent: Decimal | None
    taker_fee_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class HistoryEvidence:
    start_utc: datetime
    end_utc: datetime
    order_fence_utc: datetime
    closed_order_count: int
    trade_count: int
    ledger_entry_count: int
    queried_order_count: int
    tail_closed_order_count: int
    tail_trade_count: int
    tail_ledger_entry_count: int
    complete: bool
    collection_quiet: bool


@dataclass(frozen=True, slots=True)
class ReadOnlyReconciliation:
    account_id: str
    pair: str
    observed_at: datetime
    status: ReconciliationStatus
    source_data_hash: str
    account_binding_hash: str
    account_binding_verified: bool
    access_permissions: tuple[str, ...]
    gates: tuple[OperationalGate, ...]
    instrument: InstrumentEvidence
    history: HistoryEvidence | None
    legacy_hint_count: int
    private_request_cost_spent: int
    core_report: ReconciliationReport | None
    evidence: dict[str, object]
    exchange_writes: bool = False

    def __post_init__(self) -> None:
        if self.exchange_writes:
            raise ValueError("read-only reconciliation cannot contain exchange writes")

    def operator_payload(self) -> dict[str, object]:
        """Return a deterministic JSON-safe payload with no raw access material."""

        encoded = canonical_json_bytes(asdict(self))
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise TypeError("reconciliation payload is malformed")
        return cast(dict[str, object], decoded)


class PublicRequestPacer:
    """Pace public calls and the conservative private counter in one workflow."""

    def __init__(
        self,
        *,
        interval_seconds: float = PUBLIC_REQUEST_INTERVAL_SECONDS,
        private_counter_ceiling: float = PRIVATE_COUNTER_CEILING,
        private_decay_per_second: float = PRIVATE_COUNTER_DECAY_PER_SECOND,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        if not math.isfinite(private_counter_ceiling) or private_counter_ceiling <= 0:
            raise ValueError("private_counter_ceiling must be finite and positive")
        if not math.isfinite(private_decay_per_second) or private_decay_per_second <= 0:
            raise ValueError("private_decay_per_second must be finite and positive")
        self._interval_seconds = interval_seconds
        self._private_counter_ceiling = private_counter_ceiling
        self._private_decay_per_second = private_decay_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_public_at: float | None = None
        self._last_private_at: float | None = None
        self._private_counter = 0.0

    def __call__(self, endpoint_label: str, cost: int) -> None:
        if endpoint_label.startswith("private:"):
            if isinstance(cost, bool) or cost <= 0 or cost > self._private_counter_ceiling:
                raise ValueError("private request cost is outside the pacing domain")
            now = self._monotonic()
            if self._last_private_at is not None:
                elapsed = now - self._last_private_at
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError("monotonic clock moved backwards")
                self._private_counter = max(
                    0.0,
                    self._private_counter - elapsed * self._private_decay_per_second,
                )
            excess = self._private_counter + cost - self._private_counter_ceiling
            if excess > 0:
                self._sleep(excess / self._private_decay_per_second)
                resumed = self._monotonic()
                if resumed < now:
                    raise ValueError("monotonic clock moved backwards")
                self._private_counter = max(
                    0.0,
                    self._private_counter - (resumed - now) * self._private_decay_per_second,
                )
                now = resumed
            self._private_counter += cost
            self._last_private_at = now
            return
        if not endpoint_label.startswith("public:"):
            raise ValueError("pacing endpoint label is unsupported")
        now = self._monotonic()
        if self._last_public_at is not None:
            delay = self._interval_seconds - (now - self._last_public_at)
            if delay > 0:
                self._sleep(delay)
                now = self._monotonic()
        self._last_public_at = now


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationJobError(f"legacy hint {field} must be an ISO-8601 string")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ReconciliationJobError(f"legacy hint {field} is not a timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReconciliationJobError(f"legacy hint {field} must be UTC")
    return parsed.astimezone(UTC)


def _parse_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ReconciliationJobError(f"legacy hint {field} must be an exact decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ReconciliationJobError(f"legacy hint {field} is not a decimal") from None
    if not result.is_finite():
        raise ReconciliationJobError(f"legacy hint {field} must be finite")
    return result


def _optional_identifier(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ReconciliationJobError(f"legacy hint {field} is invalid")
    return value.strip()


def _normalize_pair(pair: str) -> str:
    normalized = pair.strip().upper()
    if normalized in BTC_CAD_ALIASES:
        return "BTC/CAD"
    return normalized


def _normalize_asset(asset: str) -> str:
    return _ASSET_ALIASES.get(asset.strip().upper(), asset.strip().upper())


def load_legacy_hints(path: Path) -> tuple[LegacySubmissionHint, ...]:
    """Load strict, bounded, non-authoritative legacy submission claims."""

    if not isinstance(path, Path):
        raise TypeError("legacy hint path must be a pathlib.Path")
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ReconciliationJobError("legacy hint path must be a regular non-symlink file")
        if before.st_mode & 0o022:
            raise ReconciliationJobError("legacy hint file must not be group/other writable")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReconciliationJobError("legacy hint file changed while it was opened")
        if opened.st_size > MAX_LEGACY_HINT_BYTES:
            raise ReconciliationJobError("legacy hint file exceeds the size limit")
        chunks: list[bytes] = []
        remaining = MAX_LEGACY_HINT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        if len(raw_bytes) > MAX_LEGACY_HINT_BYTES:
            raise ReconciliationJobError("legacy hint file exceeds the size limit")
        after = os.fstat(descriptor)
        if (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReconciliationJobError("legacy hint file changed while it was read")
        raw = raw_bytes.decode("utf-8")
    except ReconciliationJobError:
        raise
    except (OSError, UnicodeDecodeError):
        raise ReconciliationJobError("legacy hint file could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        decoded = json.loads(
            raw,
            parse_float=str,
            parse_int=str,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ReconciliationJobError("legacy hint file is not valid JSON") from None
    if not isinstance(decoded, list) or len(decoded) > MAX_LEGACY_HINTS:
        raise ReconciliationJobError("legacy hint file must contain a bounded JSON array")

    allowed_fields = {
        "client_order_id",
        "hint_id",
        "limit_price_cad",
        "order_id",
        "pair",
        "quantity_btc",
        "side",
        "window_end",
        "window_start",
    }
    required_fields = {
        "hint_id",
        "pair",
        "quantity_btc",
        "side",
        "window_end",
        "window_start",
    }
    hints: list[LegacySubmissionHint] = []
    for index, value in enumerate(decoded):
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise ReconciliationJobError(f"legacy hint {index} must be an object")
        row = cast(Mapping[str, object], value)
        if set(row) - allowed_fields or not required_fields <= set(row):
            raise ReconciliationJobError(f"legacy hint {index} has an invalid field set")
        hint_id = _optional_identifier(row.get("hint_id"), field="hint_id")
        pair = _optional_identifier(row.get("pair"), field="pair")
        side = _optional_identifier(row.get("side"), field="side")
        if hint_id is None or pair is None or side is None:
            raise ReconciliationJobError(f"legacy hint {index} is missing an identifier")
        try:
            parsed_side = Side(side.lower())
        except ValueError:
            raise ReconciliationJobError("legacy hint side must be buy or sell") from None
        limit_price = row.get("limit_price_cad")
        hints.append(
            LegacySubmissionHint(
                hint_id=hint_id,
                pair=_normalize_pair(pair),
                side=parsed_side,
                quantity_btc=_parse_decimal(row["quantity_btc"], field="quantity_btc"),
                window_start=_parse_utc(row["window_start"], field="window_start"),
                window_end=_parse_utc(row["window_end"], field="window_end"),
                limit_price_cad=(
                    None
                    if limit_price is None
                    else _parse_decimal(limit_price, field="limit_price_cad")
                ),
                order_id=_optional_identifier(row.get("order_id"), field="order_id"),
                client_order_id=_optional_identifier(
                    row.get("client_order_id"), field="client_order_id"
                ),
            )
        )
    return tuple(hints)


@contextmanager
def _reconciliation_lease(ledger: Ledger) -> Iterator[None]:
    """Hold a non-blocking host-level lease across API reads and persistence."""

    lock_path = ledger.path.with_name("kraken-knight-reconcile.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReconciliationJobError(
                "another reconciliation process holds the host lease"
            ) from None
        yield
    except ReconciliationJobError:
        raise
    except OSError:
        raise ReconciliationJobError("reconciliation host lease could not be acquired") from None
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _gate(
    name: str,
    passed: bool,
    *,
    failure_status: ReconciliationStatus = ReconciliationStatus.DISARMED,
    detail: str,
) -> OperationalGate:
    return OperationalGate(
        name=name,
        passed=passed,
        failure_status=failure_status,
        detail=detail,
    )


def _status_from_gates(
    gates: Sequence[OperationalGate],
    core_status: ReconciliationStatus | None,
) -> ReconciliationStatus:
    failed = tuple(gate for gate in gates if not gate.passed)
    if core_status is ReconciliationStatus.DISARMED or any(
        gate.failure_status is ReconciliationStatus.DISARMED for gate in failed
    ):
        return ReconciliationStatus.DISARMED
    if core_status is ReconciliationStatus.UNRESOLVED or failed:
        return ReconciliationStatus.UNRESOLVED
    if core_status is None:
        return ReconciliationStatus.DISARMED
    return ReconciliationStatus.CLEAN


def _ip_is_allowlisted(expected_ip: str, allowlist: Sequence[str]) -> bool:
    expected = ipaddress.ip_address(expected_ip)
    if len(allowlist) != 1:
        return False
    try:
        network = ipaddress.ip_network(allowlist[0].strip(), strict=False)
    except ValueError:
        return False
    host_prefix = 32 if expected.version == 4 else 128
    return network.prefixlen == host_prefix and network.network_address == expected


def _history_bounds(
    server_time: datetime,
    hints: Sequence[LegacySubmissionHint],
) -> tuple[datetime, datetime]:
    del hints
    end = server_time.astimezone(UTC).replace(microsecond=0)
    return ACCOUNT_HISTORY_EPOCH, end


def _server_time_source(snapshot: ServerTimeSnapshot) -> dict[str, object]:
    total_microseconds = (
        snapshot.clock_skew.days * 86_400 + snapshot.clock_skew.seconds
    ) * 1_000_000 + snapshot.clock_skew.microseconds
    return {
        "clock_skew_seconds": format(
            Decimal(total_microseconds) / Decimal("1000000"),
            "f",
        ),
        "observed_at": snapshot.observed_at,
        "server_time": snapshot.server_time,
    }


def _instrument_evidence(
    pair: AssetPairSnapshot,
    fees: TradeVolumeSnapshot | None,
) -> InstrumentEvidence:
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    rolling_volume: Decimal | None = None
    if fees is not None:
        rolling_volume = fees.rolling_volume
        maker = tuple(item for item in fees.maker_fees if _normalize_pair(item.pair) == "BTC/CAD")
        taker = tuple(item for item in fees.taker_fees if _normalize_pair(item.pair) == "BTC/CAD")
        if len(maker) == 1:
            maker_fee = maker[0].fee_percent
        if len(taker) == 1:
            taker_fee = taker[0].fee_percent
    instrument = pair.pair
    return InstrumentEvidence(
        exchange_pair=instrument.exchange_pair,
        alternate_name=instrument.alternate_name,
        websocket_name=instrument.websocket_name,
        status=instrument.status,
        order_minimum_btc=instrument.order_minimum,
        cost_minimum_cad=instrument.cost_minimum,
        tick_size_cad=instrument.tick_size,
        cost_decimals=instrument.cost_decimals,
        pair_decimals=instrument.pair_decimals,
        lot_decimals=instrument.lot_decimals,
        rolling_fee_volume=rolling_volume,
        maker_fee_percent=maker_fee,
        taker_fee_percent=taker_fee,
    )


def _balances(snapshot: BalanceSnapshot) -> tuple[AssetBalance, ...]:
    balances: list[AssetBalance] = []
    present: set[str] = set()
    for item in snapshot.balances:
        normalized = _normalize_asset(item.asset)
        total_components = item.balance + item.credit + item.credit_used + item.hold_trade
        if normalized not in {"BTC", "CAD"} and total_components == 0:
            continue
        present.add(normalized)
        balances.append(
            AssetBalance(
                asset=normalized,
                available=item.available,
                held=item.hold_trade,
            )
        )
    for required in ("BTC", "CAD"):
        if required not in present:
            balances.append(AssetBalance(asset=required, available=Decimal("0"), held=Decimal("0")))
    return tuple(balances)


def _liabilities(snapshot: BalanceSnapshot) -> tuple[Liability, ...]:
    return tuple(
        Liability(asset=_normalize_asset(item.asset), amount=item.credit_used)
        for item in snapshot.balances
        if item.credit_used != 0
    )


def _order_state(order: OrderRecord) -> OrderState:
    if order.status in {"pending", "open"}:
        if order.executed_volume > 0:
            return OrderState.PARTIALLY_FILLED
        return OrderState.OPEN
    if order.status == "closed":
        return OrderState.FILLED
    if order.status == "canceled":
        return OrderState.CANCELED
    if order.status == "expired":
        return OrderState.EXPIRED
    raise ReconciliationJobError("Kraken order state cannot be reconciled")


def _authoritative_order(order: OrderRecord) -> AuthoritativeOrder:
    return AuthoritativeOrder(
        order_id=order.order_id,
        pair=_normalize_pair(order.pair),
        side=Side(order.side),
        state=_order_state(order),
        quantity_btc=order.volume,
        filled_quantity_btc=order.executed_volume,
        opened_at=order.opened_at,
        closed_at=order.closed_at,
        limit_price_cad=(None if order.requested_price == 0 else order.requested_price),
        client_order_id=order.client_order_id,
        ownership=OrderOwnership.UNKNOWN,
    )


def _authoritative_trade(trade: TradeRecord) -> AuthoritativeTrade:
    return AuthoritativeTrade(
        trade_id=trade.trade_id,
        order_id=trade.order_id,
        pair=_normalize_pair(trade.pair),
        side=Side(trade.side),
        quantity_btc=trade.volume,
        price_cad=trade.price,
        executed_at=trade.executed_at,
    )


def _merge_orders(groups: Sequence[Sequence[OrderRecord]]) -> tuple[tuple[OrderRecord, ...], bool]:
    by_id: dict[str, OrderRecord] = {}
    consistent = True
    for group in groups:
        for order in group:
            existing = by_id.get(order.order_id)
            if existing is not None and existing != order:
                consistent = False
            else:
                by_id[order.order_id] = order
    return tuple(by_id[key] for key in sorted(by_id)), consistent


def _merge_trades(groups: Sequence[Sequence[TradeRecord]]) -> tuple[tuple[TradeRecord, ...], bool]:
    by_id: dict[str, TradeRecord] = {}
    consistent = True
    for group in groups:
        for trade in group:
            existing = by_id.get(trade.trade_id)
            if existing is not None and existing != trade:
                consistent = False
            else:
                by_id[trade.trade_id] = trade
    return tuple(by_id[key] for key in sorted(by_id)), consistent


def _merge_ledgers(
    groups: Sequence[Sequence[LedgerRecord]],
) -> tuple[tuple[LedgerRecord, ...], bool]:
    by_id: dict[str, LedgerRecord] = {}
    consistent = True
    for group in groups:
        for entry in group:
            existing = by_id.get(entry.ledger_id)
            if existing is not None and existing != entry:
                consistent = False
            else:
                by_id[entry.ledger_id] = entry
    return tuple(by_id[key] for key in sorted(by_id)), consistent


def _history_complete(
    closed: ClosedOrdersPage,
    trades: TradeHistoryPage,
    ledgers: LedgerPage,
) -> bool:
    closed_complete = closed.total_count == len(closed.orders)
    trades_complete = (
        trades.total_count == len(trades.trades)
        if trades.total_count is not None
        else len(trades.trades) < 100
    )
    ledgers_complete = (
        ledgers.total_count == len(ledgers.entries)
        if ledgers.total_count is not None
        else len(ledgers.entries) < 50
    )
    return closed_complete and trades_complete and ledgers_complete


def _tail_is_quiet(
    *,
    closed: ClosedOrdersPage,
    trades: TradeHistoryPage,
    ledgers: LedgerPage,
    tail_closed: ClosedOrdersPage,
    tail_trades: TradeHistoryPage,
    tail_ledgers: LedgerPage,
) -> bool:
    return (
        {item.order_id for item in tail_closed.orders} <= {item.order_id for item in closed.orders}
        and {item.trade_id for item in tail_trades.trades}
        <= {item.trade_id for item in trades.trades}
        and {item.ledger_id for item in tail_ledgers.entries}
        <= {item.ledger_id for item in ledgers.entries}
    )


def _linked_trade_fees(
    trades: Sequence[TradeRecord],
    ledgers: Sequence[LedgerRecord],
    orders: Sequence[OrderRecord],
    *,
    cost_decimals: int,
    base_decimals: int,
) -> tuple[tuple[TradeFee, ...], bool]:
    entries_by_id = {entry.ledger_id: entry for entry in ledgers}
    trades_by_id = {trade.trade_id: trade for trade in trades}
    orders_by_id = {order.order_id: order for order in orders}
    referenced_entries: set[str] = set()
    fees: list[TradeFee] = []
    consistent = (
        len(entries_by_id) == len(ledgers)
        and len(trades_by_id) == len(trades)
        and len(orders_by_id) == len(orders)
    )
    # Kraken reports cost at cost_decimals. Correct rounding can differ from
    # price * volume by at most half one reported cost quantum.
    cost_tolerance = Decimal(1).scaleb(-cost_decimals) / 2
    base_tolerance = Decimal(1).scaleb(-base_decimals) / 2
    for trade in trades:
        if (
            len(trade.ledger_ids) != 2
            or len(set(trade.ledger_ids)) != len(trade.ledger_ids)
            or abs(trade.price * trade.volume - trade.cost) > cost_tolerance
        ):
            consistent = False
            continue
        linked = tuple(entries_by_id.get(ledger_id) for ledger_id in trade.ledger_ids)
        if any(entry is None for entry in linked):
            consistent = False
            continue
        concrete = tuple(entry for entry in linked if entry is not None)
        normalized_by_asset = {_normalize_asset(entry.asset): entry for entry in concrete}
        if (
            len(normalized_by_asset) != 2
            or set(normalized_by_asset) != {"BTC", "CAD"}
            or any(
                entry.entry_type != "trade"
                or entry.reference_id != trade.trade_id
                or entry.asset_class != "currency"
                or entry.recorded_at != trade.executed_at
                for entry in concrete
            )
        ):
            consistent = False
            continue
        btc_entry = normalized_by_asset["BTC"]
        cad_entry = normalized_by_asset["CAD"]
        expected_btc = trade.volume if trade.side == "buy" else -trade.volume
        expected_cad = -trade.cost if trade.side == "buy" else trade.cost
        if btc_entry.amount != expected_btc or cad_entry.amount != expected_cad:
            consistent = False
        fee_entries = tuple(entry for entry in concrete if entry.fee > 0)
        fee_assets = {_normalize_asset(entry.asset) for entry in fee_entries}
        actual_fee = sum((entry.fee for entry in fee_entries), start=Decimal("0"))
        if not fee_entries:
            fee_consistent = trade.fee == 0
        elif fee_assets == {"CAD"}:
            fee_consistent = abs(actual_fee - trade.fee) <= cost_tolerance
        elif fee_assets == {"BTC"}:
            fee_consistent = abs(actual_fee * trade.price - trade.fee) <= (
                cost_tolerance + base_tolerance * trade.price
            )
        else:
            fee_consistent = False
        if not fee_consistent:
            consistent = False
        order = orders_by_id.get(trade.order_id)
        if order is None:
            consistent = False
        for entry in concrete:
            referenced_entries.add(entry.ledger_id)
            if entry.fee > 0:
                fees.append(
                    TradeFee(
                        fee_id=entry.ledger_id,
                        trade_id=trade.trade_id,
                        asset=_normalize_asset(entry.asset),
                        amount=entry.fee,
                    )
                )
    for entry in ledgers:
        if entry.entry_type == "trade" and (
            entry.reference_id not in trades_by_id or entry.ledger_id not in referenced_entries
        ):
            consistent = False
    return tuple(fees), consistent


def _ledger_balances_match(
    entries: Sequence[LedgerRecord],
    balances: BalanceSnapshot,
) -> bool:
    balance_by_asset: dict[str, Decimal] = {}
    for item in balances.balances:
        asset = _normalize_asset(item.asset)
        if asset in balance_by_asset or item.balance < 0:
            return False
        balance_by_asset[asset] = item.balance

    entries_by_asset: dict[str, list[LedgerRecord]] = {}
    for entry in entries:
        asset = _normalize_asset(entry.asset)
        if entry.asset_class != "currency" or entry.balance < 0:
            return False
        entries_by_asset.setdefault(asset, []).append(entry)

    for asset in set(balance_by_asset) | set(entries_by_asset):
        current = Decimal("0")
        remaining = sorted(
            entries_by_asset.get(asset, ()),
            key=lambda item: (item.recorded_at, item.ledger_id),
        )
        while remaining:
            earliest = remaining[0].recorded_at
            same_time = [entry for entry in remaining if entry.recorded_at == earliest]
            later = [entry for entry in remaining if entry.recorded_at != earliest]
            while same_time:
                candidates = [
                    entry
                    for entry in same_time
                    if current + entry.amount - entry.fee == entry.balance
                ]
                if len(candidates) != 1:
                    return False
                selected = candidates[0]
                current = selected.balance
                same_time.remove(selected)
            remaining = later
        if current != balance_by_asset.get(asset, Decimal("0")):
            return False
    return True


def funding_manifest_hash(entries: Sequence[LedgerRecord]) -> str:
    """Hash the exact non-trade funding facts an operator must review and pin."""

    rows = tuple(
        {
            "amount": entry.amount,
            "asset": _normalize_asset(entry.asset),
            "asset_class": entry.asset_class,
            "fee": entry.fee,
            "ledger_id": entry.ledger_id,
            "recorded_at": entry.recorded_at,
            "reference_id": entry.reference_id,
            "subtype": entry.subtype,
            "type": entry.entry_type,
        }
        for entry in entries
        if entry.entry_type != "trade"
    )
    return sha256_json(tuple(sorted(rows, key=canonical_json_bytes)))


def _external_entries_are_classified(
    entries: Sequence[LedgerRecord],
    *,
    expected_manifest_hash: str | None,
) -> bool:
    """Accept only an exact operator-pinned set of inbound CAD deposits."""

    external = tuple(entry for entry in entries if entry.entry_type != "trade")
    supported = all(
        entry.entry_type == "deposit"
        and _normalize_asset(entry.asset) == "CAD"
        and entry.amount > 0
        and entry.fee >= 0
        for entry in external
    )
    return (
        supported
        and expected_manifest_hash is not None
        and funding_manifest_hash(external) == expected_manifest_hash
    )


def _order_trade_totals_match(
    orders: Sequence[OrderRecord],
    trades: Sequence[TradeRecord],
    *,
    cost_decimals: int,
    tick_size: Decimal,
) -> bool:
    if len({trade.trade_id for trade in trades}) != len(trades):
        return False
    trades_by_order: dict[str, list[TradeRecord]] = {}
    cost_tolerance = Decimal(1).scaleb(-cost_decimals) / 2
    for trade in trades:
        trades_by_order.setdefault(trade.order_id, []).append(trade)
    for order in orders:
        linked = tuple(trades_by_order.get(order.order_id, ()))
        if len(order.trade_ids) != len(set(order.trade_ids)):
            return False
        if set(order.trade_ids) != {trade.trade_id for trade in linked}:
            return False
        if sum((trade.volume for trade in linked), start=Decimal("0")) != order.executed_volume:
            return False
        if sum((trade.cost for trade in linked), start=Decimal("0")) != order.cost:
            return False
        if sum((trade.fee for trade in linked), start=Decimal("0")) != order.fee:
            return False
        if any(trade.order_type != order.order_type for trade in linked):
            return False
        if order.executed_volume == 0:
            if order.average_price != 0:
                return False
        elif abs(order.average_price * order.executed_volume - order.cost) > (
            cost_tolerance + (tick_size / 2) * order.executed_volume
        ):
            return False
    if any(trade.order_id not in {order.order_id for order in orders} for trade in trades):
        return False
    return True


def _restricted_access_gates(
    *,
    info: ApiKeyInfoSnapshot,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> tuple[OperationalGate, ...]:
    if settings.expected_kraken_key_name is None or settings.expected_kraken_ip is None:
        raise ReconciliationJobError("Kraken read binding is not fully configured")
    permissions = tuple(info.permissions)
    return (
        _gate(
            "permissions_exactly_read_only",
            len(permissions) == len(set(permissions))
            and frozenset(permissions) == READ_ONLY_PERMISSIONS,
            detail="access permissions must equal the four reviewed read permissions",
        ),
        _gate(
            "expected_key_name",
            info.key_name == settings.expected_kraken_key_name,
            detail="access profile name must match the protected configuration",
        ),
        _gate(
            "expected_ip_allowlisted",
            _ip_is_allowlisted(settings.expected_kraken_ip, info.ip_allowlist),
            detail="the allowlist must equal the protected host's single /32 or /128",
        ),
        _gate(
            "nonce_window_zero",
            info.nonce_window == 0,
            detail="the read key must retain the default zero nonce window",
        ),
        _gate(
            "access_not_expired",
            info.valid_until is None or info.valid_until > end,
            detail="the read key must remain valid through the observation",
        ),
        _gate(
            "history_scope_start",
            info.query_from is None or info.query_from <= start,
            detail="the read key must cover the complete requested history start",
        ),
        _gate(
            "history_scope_end",
            info.query_to is None or info.query_to >= end,
            detail="the read key must cover the complete requested history end",
        ),
    )


def _account_binding_gates(
    *,
    wallets: WalletAccountsSnapshot,
    expected_account_id: str,
    account_binding_hash: str,
    prior_binding_hashes: frozenset[str],
) -> tuple[OperationalGate, ...]:
    active_accounts = tuple(
        account for account in wallets.accounts if account.status == "active" and account.active
    )
    active_main = tuple(account for account in active_accounts if account.account_type == "main")
    return (
        _gate(
            "wallet_accounts_page_complete",
            wallets.complete,
            detail="the wallet-account identity page must be complete",
        ),
        _gate(
            "one_active_wallet",
            len(active_accounts) == 1,
            detail=(
                "the authenticated user must expose exactly one active wallet so unscoped "
                "balance, order, and trade reads cannot refer to another default wallet"
            ),
        ),
        _gate(
            "one_active_main_wallet",
            len(active_main) == 1,
            detail="the authenticated Kraken user must expose exactly one active main wallet",
        ),
        _gate(
            "expected_wallet_account",
            len(active_main) == 1 and active_main[0].account_id == expected_account_id,
            detail="the active main wallet must equal the operator-pinned public account ID",
        ),
        _gate(
            "account_binding_continuity",
            not prior_binding_hashes or prior_binding_hashes == {account_binding_hash},
            detail="the wallet-account fingerprint must match every prior local snapshot",
        ),
    )


def _json_evidence(value: Mapping[str, object]) -> dict[str, object]:
    decoded = json.loads(canonical_json_bytes(value))
    if not isinstance(decoded, dict):
        raise TypeError("normalized evidence must encode as a JSON object")
    return cast(dict[str, object], decoded)


def _access_evidence(
    *,
    info: ApiKeyInfoSnapshot,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    return {
        "created_at": info.created_at,
        "expected_host_allowlist_exact": (
            settings.expected_kraken_ip is not None
            and _ip_is_allowlisted(settings.expected_kraken_ip, info.ip_allowlist)
        ),
        "expected_profile_name_matches": info.key_name == settings.expected_kraken_key_name,
        "last_used_at": info.last_used_at,
        "modified_at": info.modified_at,
        "nonce_window": info.nonce_window,
        "permissions": tuple(sorted(info.permissions)),
        "query_from": info.query_from,
        "query_to": info.query_to,
        "requested_history_end": end,
        "requested_history_start": start,
        "valid_until": info.valid_until,
    }


def _fee_tier_is_valid(fees: TradeVolumeSnapshot) -> bool:
    maker = tuple(item for item in fees.maker_fees if _normalize_pair(item.pair) == "BTC/CAD")
    taker = tuple(item for item in fees.taker_fees if _normalize_pair(item.pair) == "BTC/CAD")
    rows = (*maker, *taker)
    return (
        _normalize_asset(fees.currency) == "CAD"
        and len(maker) == 1
        and len(taker) == 1
        and all(
            item.minimum_fee_percent <= item.fee_percent <= item.maximum_fee_percent
            for item in rows
        )
    )


def _persist(
    *,
    ledger: Ledger,
    result: ReadOnlyReconciliation,
) -> dict[str, object]:
    payload = result.operator_payload()
    snapshot_id = ledger.append_reconciliation_snapshot(
        account_binding_hash=result.account_binding_hash,
        account_binding_verified=result.account_binding_verified,
        account_id=result.account_id,
        exchange_writes=False,
        observed_at=result.observed_at,
        pair=result.pair,
        report=payload,
        source_data_hash=result.source_data_hash,
        status=result.status.value,
    )
    return {**payload, "ledger_snapshot_id": snapshot_id}


def discover_read_only_account_id(
    *,
    settings: Settings,
    ledger: Ledger,
    client: KrakenReadPort | None = None,
) -> dict[str, object]:
    """Explicitly reveal the public wallet-account ID after read-only gates pass.

    This helper exists only to bootstrap the operator-pinned account binding. It
    does not persist the ID and is never called by a timer or reconciliation.
    """

    if settings.kraken_api_key is None or settings.kraken_api_secret is None:
        raise ReconciliationJobError("Kraken authenticated read credentials are not configured")
    if settings.expected_kraken_key_name is None or settings.expected_kraken_ip is None:
        raise ReconciliationJobError(
            "Kraken key-name and host-IP bindings are required for account discovery"
        )
    reader: KrakenReadPort
    if client is None:
        reader = KrakenReadClient(
            api_key=settings.kraken_api_key.reveal(),
            api_secret=settings.kraken_api_secret.reveal(),
            pacing_hook=PublicRequestPacer(),
            request_budget=KrakenRequestBudget(
                private_cost_limit=IDENTITY_DISCOVERY_PRIVATE_COST_LIMIT
            ),
        )
    else:
        reader = client

    with _reconciliation_lease(ledger):
        server = reader.get_server_time()
        end = server.server_time.astimezone(UTC).replace(microsecond=0)
        info = reader.get_api_key_info()
        gates = (
            _gate(
                "clock_skew_within_limit",
                abs(server.clock_skew.total_seconds()) <= MAX_CLOCK_SKEW_SECONDS,
                detail="Kraken/local clock skew must be at most 30 seconds",
            ),
            *_restricted_access_gates(
                info=info,
                settings=settings,
                start=ACCOUNT_HISTORY_EPOCH,
                end=end,
            ),
        )
        if any(not gate.passed for gate in gates):
            raise ReconciliationJobError(
                "Kraken access profile did not pass read-only account-discovery gates"
            )
        wallets = reader.get_wallet_accounts()
        active_main = tuple(
            account
            for account in wallets.accounts
            if account.account_type == "main" and account.status == "active" and account.active
        )
        active_accounts = tuple(
            account for account in wallets.accounts if account.status == "active" and account.active
        )
        if not wallets.complete or len(active_accounts) != 1 or len(active_main) != 1:
            raise ReconciliationJobError(
                "Kraken did not return one complete active main wallet identity"
            )
        account_id = active_main[0].account_id
        if (
            settings.expected_kraken_account_id is not None
            and account_id != settings.expected_kraken_account_id
        ):
            raise ReconciliationJobError(
                "Kraken wallet identity does not match the protected account binding"
            )
        return {
            "exchange_writes": False,
            "observed_at": end.isoformat().replace("+00:00", "Z"),
            "private_request_cost_spent": reader.private_cost_spent,
            "read_only_profile_verified": True,
            "wallet_account_id": account_id,
        }


def _legacy_manifest_digest(hints: Sequence[LegacySubmissionHint]) -> str:
    rows = []
    for hint in hints:
        rows.append(
            {
                "client_order_id": hint.client_order_id,
                "hint_id": hint.hint_id,
                "limit_price_cad": (
                    None
                    if hint.limit_price_cad is None
                    else format(hint.limit_price_cad.normalize(), "f")
                ),
                "order_id": hint.order_id,
                "pair": hint.pair,
                "quantity_btc": format(hint.quantity_btc.normalize(), "f"),
                "side": hint.side.value,
                "window_end": hint.window_end.isoformat(),
                "window_start": hint.window_start.isoformat(),
            }
        )
    rows.sort(key=canonical_json_bytes)
    return sha256_json(tuple(rows))


def legacy_manifest_hash(hints: Sequence[LegacySubmissionHint]) -> str:
    """Return the order-independent semantic digest for five identified claims."""

    normalized = tuple(hints)
    if (
        len(normalized) != EXPECTED_LEGACY_HINTS
        or any(item.order_id is None for item in normalized)
        or len({item.order_id for item in normalized}) != EXPECTED_LEGACY_HINTS
    ):
        raise ReconciliationJobError(
            "legacy manifest requires exactly five unique Kraken order IDs"
        )
    return _legacy_manifest_digest(normalized)


def _execute_read_only_reconciliation_unleased(
    *,
    settings: Settings,
    ledger: Ledger,
    legacy_hints: Sequence[LegacySubmissionHint] = (),
    client: KrakenReadPort | None = None,
) -> dict[str, object]:
    """Collect one bounded read set, reconcile it, and append its report."""

    if settings.kraken_api_key is None or settings.kraken_api_secret is None:
        raise ReconciliationJobError("Kraken authenticated read credentials are not configured")
    if (
        settings.expected_kraken_key_name is None
        or settings.expected_kraken_ip is None
        or settings.expected_kraken_account_id is None
        or settings.expected_legacy_manifest_hash is None
    ):
        raise ReconciliationJobError("Kraken read binding is not fully configured")
    normalized_hints = tuple(legacy_hints)
    if any(not isinstance(item, LegacySubmissionHint) for item in normalized_hints):
        raise TypeError("legacy_hints contains an invalid item")

    raw_key = settings.kraken_api_key.reveal()
    account_binding_hash = sha256_json(
        {"kraken_wallet_account_id": settings.expected_kraken_account_id}
    )
    reader: KrakenReadPort
    if client is None:
        reader = KrakenReadClient(
            api_key=raw_key,
            api_secret=settings.kraken_api_secret.reveal(),
            pacing_hook=PublicRequestPacer(),
            request_budget=KrakenRequestBudget(
                private_cost_limit=RECONCILIATION_PRIVATE_COST_LIMIT
            ),
        )
    else:
        reader = client

    server = reader.get_server_time()
    system = reader.get_system_status()
    pair = reader.get_asset_pair(pair="XBTCAD")
    start, end = _history_bounds(server.server_time, normalized_hints)
    info = reader.get_api_key_info()

    instrument = _instrument_evidence(pair, None)
    public_gates = (
        _gate(
            "clock_skew_within_limit",
            abs(server.clock_skew.total_seconds()) <= MAX_CLOCK_SKEW_SECONDS,
            detail="Kraken/local clock skew must be at most 30 seconds",
        ),
        _gate(
            "system_online",
            system.is_online,
            detail="Kraken system status must be online",
        ),
        _gate(
            "pair_online",
            pair.pair.status == "online",
            detail="BTC/CAD instrument status must be online",
        ),
        _gate(
            "pair_identity",
            pair.pair.exchange_pair.upper() in BTC_CAD_ALIASES
            and pair.pair.alternate_name.upper() in BTC_CAD_ALIASES
            and pair.pair.websocket_name.upper() in BTC_CAD_ALIASES
            and _normalize_asset(pair.pair.base_asset) == "BTC"
            and _normalize_asset(pair.pair.quote_asset) == "CAD",
            detail="all instrument aliases and base/quote assets must identify BTC/CAD",
        ),
    )
    access_gates = _restricted_access_gates(
        info=info,
        settings=settings,
        start=start,
        end=end,
    )
    initial_gates = public_gates + access_gates
    initial_evidence = _json_evidence(
        {
            "server": _server_time_source(server),
            "system": system,
            "instrument": pair,
            "access": _access_evidence(
                info=info,
                settings=settings,
                start=start,
                end=end,
            ),
        }
    )
    if any(not gate.passed for gate in initial_gates):
        result = ReadOnlyReconciliation(
            account_id=settings.account_id,
            pair=settings.pair,
            observed_at=max(
                server.observed_at,
                system.observed_at,
                pair.observed_at,
                info.observed_at,
            ),
            status=_status_from_gates(initial_gates, None),
            source_data_hash=sha256_json(initial_evidence),
            account_binding_hash=account_binding_hash,
            account_binding_verified=False,
            access_permissions=tuple(sorted(info.permissions)),
            gates=initial_gates,
            instrument=instrument,
            history=None,
            legacy_hint_count=len(normalized_hints),
            private_request_cost_spent=reader.private_cost_spent,
            core_report=None,
            evidence=initial_evidence,
        )
        return _persist(ledger=ledger, result=result)

    wallets = reader.get_wallet_accounts()
    prior_binding_hashes = ledger.reconciliation_binding_hashes(settings.account_id)
    account_gates = _account_binding_gates(
        wallets=wallets,
        expected_account_id=settings.expected_kraken_account_id,
        account_binding_hash=account_binding_hash,
        prior_binding_hashes=prior_binding_hashes,
    )
    identity_gates = initial_gates + account_gates
    wallet_evidence = {
        "account_count": len(wallets.accounts),
        "expected_active_main_matches": any(
            account.account_id == settings.expected_kraken_account_id
            and account.account_type == "main"
            and account.status == "active"
            and account.active
            for account in wallets.accounts
        ),
        "page_complete": wallets.complete,
        "prior_binding_count": len(prior_binding_hashes),
    }
    if any(not gate.passed for gate in account_gates):
        evidence = _json_evidence({**initial_evidence, "wallet_accounts": wallet_evidence})
        result = ReadOnlyReconciliation(
            account_id=settings.account_id,
            pair=settings.pair,
            observed_at=max(
                server.observed_at,
                system.observed_at,
                pair.observed_at,
                info.observed_at,
                wallets.observed_at,
            ),
            status=_status_from_gates(identity_gates, None),
            source_data_hash=sha256_json(evidence),
            account_binding_hash=account_binding_hash,
            account_binding_verified=False,
            access_permissions=tuple(sorted(info.permissions)),
            gates=identity_gates,
            instrument=instrument,
            history=None,
            legacy_hint_count=len(normalized_hints),
            private_request_cost_spent=reader.private_cost_spent,
            core_report=None,
            evidence=evidence,
        )
        return _persist(ledger=ledger, result=result)

    fee_tier = reader.get_trade_volume(pair="XBTCAD")
    opening_balances = reader.get_extended_balances()
    opening_orders = reader.get_open_orders()
    order_ids = tuple(
        sorted({hint.order_id for hint in normalized_hints if hint.order_id is not None})
    )
    queried = None if not order_ids else reader.query_orders(order_ids)
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())
    closed = reader.get_closed_orders(start=start_epoch, end=end_epoch)
    trades = reader.get_trades_history(
        start=start_epoch,
        end=end_epoch,
        limit=100,
        pair="XBTCAD",
    )
    ledgers = reader.get_ledgers(
        account_id=settings.expected_kraken_account_id,
        entry_type="all",
        start=start_epoch,
        end=end_epoch,
    )
    # Capture closing account state before asking Kraken for the server-time
    # fence. Tail history is queried only after that fence, so any activity
    # between the state snapshots and the fence prevents a CLEAN result.
    closing_balances = reader.get_extended_balances()
    closing_orders = reader.get_open_orders()
    closing_server = reader.get_server_time()
    tail_end_epoch = int(closing_server.server_time.timestamp())
    tail_trades = reader.get_trades_history(
        start=end_epoch,
        end=tail_end_epoch,
        limit=100,
        pair="XBTCAD",
    )
    tail_ledgers = reader.get_ledgers(
        account_id=settings.expected_kraken_account_id,
        entry_type="all",
        start=end_epoch,
        end=tail_end_epoch,
    )
    final_orders = reader.get_open_orders()
    # Fence the post-state open-order read, then read closed orders through that
    # later fence. This catches a zero-fill order that opened before the account
    # snapshot fence but was canceled while the tail reads were in flight.
    order_fence_server = reader.get_server_time()
    order_fence_epoch = int(order_fence_server.server_time.timestamp())
    tail_closed = reader.get_closed_orders(start=end_epoch, end=order_fence_epoch)

    queried_orders = () if queried is None else queried.orders
    merged_closed, closed_versions_consistent = _merge_orders((closed.orders, tail_closed.orders))
    merged_orders, order_versions_consistent = _merge_orders(
        (
            opening_orders.orders,
            closing_orders.orders,
            final_orders.orders,
            merged_closed,
            queried_orders,
        )
    )
    merged_trades, trade_versions_consistent = _merge_trades((trades.trades, tail_trades.trades))
    merged_ledgers, ledger_versions_consistent = _merge_ledgers(
        (ledgers.entries, tail_ledgers.entries)
    )
    history_complete = _history_complete(closed, trades, ledgers) and _history_complete(
        tail_closed,
        tail_trades,
        tail_ledgers,
    )
    collection_quiet = _tail_is_quiet(
        closed=closed,
        trades=trades,
        ledgers=ledgers,
        tail_closed=tail_closed,
        tail_trades=tail_trades,
        tail_ledgers=tail_ledgers,
    )
    snapshot_stable = (
        opening_balances.balances == closing_balances.balances
        and opening_orders.orders == closing_orders.orders
        and closing_orders.orders == final_orders.orders
    )
    fee_evidence = _instrument_evidence(pair, fee_tier)
    fee_pair_present = _fee_tier_is_valid(fee_tier)
    margin_credit_zero = (
        all(item.credit == 0 and item.credit_used == 0 for item in closing_balances.balances)
        and all(trade.margin == 0 and trade.position_id is None for trade in merged_trades)
        and all(order.leverage == "none" for order in merged_orders)
    )
    linked_fees, trade_ledger_links_consistent = _linked_trade_fees(
        merged_trades,
        merged_ledgers,
        merged_orders,
        cost_decimals=pair.pair.cost_decimals,
        base_decimals=pair.pair.lot_decimals,
    )
    order_trade_totals_consistent = _order_trade_totals_match(
        merged_orders,
        merged_trades,
        cost_decimals=pair.pair.cost_decimals,
        tick_size=pair.pair.tick_size,
    )
    ledger_balances_consistent = _ledger_balances_match(
        merged_ledgers,
        closing_balances,
    )
    all_history_versions_consistent = (
        closed_versions_consistent
        and order_versions_consistent
        and trade_versions_consistent
        and ledger_versions_consistent
    )
    observed_legacy_manifest_hash = _legacy_manifest_digest(normalized_hints)
    observed_funding_manifest_hash = funding_manifest_hash(merged_ledgers)
    legacy_identity_complete = (
        len(normalized_hints) == EXPECTED_LEGACY_HINTS
        and all(hint.order_id is not None for hint in normalized_hints)
        and len({hint.order_id for hint in normalized_hints}) == EXPECTED_LEGACY_HINTS
    )
    gates = (
        *identity_gates,
        _gate(
            "authenticated_fee_tier_present",
            fee_pair_present,
            detail="the authenticated BTC/CAD maker and taker fees must be present",
        ),
        _gate(
            "history_complete_within_budget",
            history_complete,
            detail="both account-lifetime and fenced-tail pages must fit one complete page",
        ),
        _gate(
            "collection_tail_quiet",
            collection_quiet,
            detail="no new order, trade, or ledger ID may appear during collection",
        ),
        _gate(
            "snapshot_stable",
            snapshot_stable,
            detail=(
                "opening/closing balances and opening, closing, and post-fence open orders "
                "must be identical"
            ),
        ),
        _gate(
            "closing_clock_skew_within_limit",
            abs(closing_server.clock_skew.total_seconds()) <= MAX_CLOCK_SKEW_SECONDS,
            detail="closing Kraken/local clock skew must be at most 30 seconds",
        ),
        _gate(
            "order_fence_clock_skew_within_limit",
            abs(order_fence_server.clock_skew.total_seconds()) <= MAX_CLOCK_SKEW_SECONDS,
            detail="post-state order-fence Kraken/local clock skew must be at most 30 seconds",
        ),
        _gate(
            "closing_history_scope_end",
            info.query_to is None or info.query_to >= order_fence_server.server_time,
            detail="the read key must cover the post-state order-history fence",
        ),
        _gate(
            "server_time_monotonic",
            closing_server.server_time >= server.server_time
            and order_fence_server.server_time >= closing_server.server_time,
            detail="Kraken server-time fences must remain monotonic",
        ),
        _gate(
            "no_open_orders_at_cutover",
            not final_orders.orders,
            detail="cutover reconciliation requires no resting account orders",
        ),
        _gate(
            "history_versions_consistent",
            all_history_versions_consistent,
            detail="repeated order, trade, and ledger IDs must have identical facts",
        ),
        _gate(
            "order_trade_totals_consistent",
            order_trade_totals_consistent,
            detail="order fills, costs, fees, and trade IDs must equal their trade totals",
        ),
        _gate(
            "trade_ledger_links_consistent",
            trade_ledger_links_consistent,
            detail="every trade and fee must link to complete Kraken ledger entries",
        ),
        _gate(
            "ledger_balance_chain_consistent",
            ledger_balances_consistent,
            detail="account-lifetime ledger deltas must start at zero and end at BalanceEx",
        ),
        _gate(
            "margin_credit_absent",
            margin_credit_zero,
            detail="the spot-only account must have no margin credit or margin trade",
        ),
        _gate(
            "cutover_quiescence_attested",
            settings.cutover_quiesced,
            failure_status=ReconciliationStatus.UNRESOLVED,
            detail="the legacy writer and manual trading must be stopped for supervised cutover",
        ),
        _gate(
            "legacy_order_identity_complete",
            legacy_identity_complete,
            failure_status=ReconciliationStatus.UNRESOLVED,
            detail="all five pinned legacy claims must contain unique Kraken order IDs",
        ),
        _gate(
            "legacy_manifest_pinned",
            observed_legacy_manifest_hash == settings.expected_legacy_manifest_hash,
            failure_status=ReconciliationStatus.UNRESOLVED,
            detail="the supplied legacy claims must equal the operator-pinned manifest digest",
        ),
        _gate(
            "external_cash_flows_classified",
            _external_entries_are_classified(
                merged_ledgers,
                expected_manifest_hash=settings.expected_funding_manifest_hash,
            ),
            failure_status=ReconciliationStatus.UNRESOLVED,
            detail=(
                "the exact operator-pinned funding manifest must contain only positive "
                "inbound CAD deposits"
            ),
        ),
    )

    core_report: ReconciliationReport | None = None
    if (
        history_complete
        and snapshot_stable
        and all_history_versions_consistent
        and order_trade_totals_consistent
        and trade_ledger_links_consistent
        and ledger_balances_consistent
    ):
        account = AccountSnapshot(
            account_id=settings.account_id,
            observed_at=closing_server.server_time.astimezone(UTC).replace(microsecond=0),
            balances=_balances(closing_balances),
            orders=tuple(_authoritative_order(order) for order in merged_orders),
            trades=tuple(_authoritative_trade(trade) for trade in merged_trades),
            fees=linked_fees,
            liabilities=_liabilities(closing_balances),
            legacy_hints=normalized_hints,
            inventory_history_complete=True,
        )
        core_report = reconcile_account(account)

    history = HistoryEvidence(
        start_utc=start,
        end_utc=closing_server.server_time.astimezone(UTC).replace(microsecond=0),
        order_fence_utc=order_fence_server.server_time.astimezone(UTC).replace(microsecond=0),
        closed_order_count=len(merged_closed),
        trade_count=len(merged_trades),
        ledger_entry_count=len(merged_ledgers),
        queried_order_count=len(queried_orders),
        tail_closed_order_count=len(tail_closed.orders),
        tail_trade_count=len(tail_trades.trades),
        tail_ledger_entry_count=len(tail_ledgers.entries),
        complete=history_complete,
        collection_quiet=collection_quiet,
    )
    evidence = _json_evidence(
        {
            **initial_evidence,
            "wallet_accounts": wallet_evidence,
            "fee_tier": fee_tier,
            "opening_balances": opening_balances,
            "opening_orders": opening_orders,
            "queried_orders": queried,
            "account_lifetime_closed_orders": closed,
            "account_lifetime_trades": trades,
            "account_lifetime_ledgers": ledgers,
            "tail_closed_orders": tail_closed,
            "tail_trades": tail_trades,
            "tail_ledgers": tail_ledgers,
            "closing_balances": closing_balances,
            "closing_orders": closing_orders,
            "post_fence_open_orders": final_orders,
            "closing_server": _server_time_source(closing_server),
            "order_fence_server": _server_time_source(order_fence_server),
            "legacy_hints": normalized_hints,
            "legacy_manifest_hash": observed_legacy_manifest_hash,
            "funding_manifest_hash": observed_funding_manifest_hash,
        }
    )
    observed_at = closing_server.server_time.astimezone(UTC).replace(microsecond=0)
    result = ReadOnlyReconciliation(
        account_id=settings.account_id,
        pair=settings.pair,
        observed_at=observed_at,
        status=_status_from_gates(
            gates,
            None if core_report is None else core_report.status,
        ),
        source_data_hash=sha256_json(evidence),
        account_binding_hash=account_binding_hash,
        account_binding_verified=True,
        access_permissions=tuple(sorted(info.permissions)),
        gates=gates,
        instrument=fee_evidence,
        history=history,
        legacy_hint_count=len(normalized_hints),
        private_request_cost_spent=reader.private_cost_spent,
        core_report=core_report,
        evidence=evidence,
    )
    return _persist(ledger=ledger, result=result)


def execute_read_only_reconciliation(
    *,
    settings: Settings,
    ledger: Ledger,
    legacy_hints: Sequence[LegacySubmissionHint] = (),
    client: KrakenReadPort | None = None,
) -> dict[str, object]:
    """Run one host-serialized, authenticated, exchange-read-only reconciliation."""

    with _reconciliation_lease(ledger):
        return _execute_read_only_reconciliation_unleased(
            settings=settings,
            ledger=ledger,
            legacy_hints=legacy_hints,
            client=client,
        )
