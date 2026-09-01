"""Strictly read-only Kraken Spot REST adapter.

The module deliberately exposes a closed set of public/account-reading calls.
It has no arbitrary private-method escape hatch.  Private requests are serialized
per API-key fingerprint across this process so nonce order and request arrival
order cannot diverge.
The adapter never retries a request: after bytes leave the process, the nonce is
consumed even when the outcome is unknown.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

KRAKEN_API_ORIGIN = "https://api.kraken.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_UNSIGNED_64 = (1 << 64) - 1
BTC_CAD_ALIASES = frozenset({"BTC/CAD", "XBT/CAD", "XBTCAD", "XXBTZCAD"})
CONSERVATIVE_PRIVATE_COST_LIMIT = 20
PRIVATE_ENDPOINT_COSTS: Mapping[str, int] = MappingProxyType(
    {
        "BalanceEx": 1,
        "ClosedOrders": 4,
        "GetApiKeyInfo": 1,
        "Ledgers": 4,
        "ListWalletAccounts": 1,
        "OpenOrders": 1,
        "QueryOrders": 1,
        "TradesHistory": 4,
        "TradeVolume": 1,
    }
)
_API_KEY_INFO_CAMEL_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "api_key": "apiKey",
        "api_key_name": "apiKeyName",
        "created_time": "createdTime",
        "iban": "iban",
        "ip_allowlist": "ipAllowlist",
        "last_used": "lastUsed",
        "modified_time": "modifiedTime",
        "nonce": "nonce",
        "nonce_window": "nonceWindow",
        "permissions": "permissions",
        "query_from": "queryFrom",
        "query_to": "queryTo",
        "valid_until": "validUntil",
    }
)
_API_KEY_INFO_SNAKE_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "api_key": "api_key",
        "api_key_name": "api_key_name",
        "created_time": "created_time",
        "iban": "iban",
        "ip_allowlist": "ip_allowlist",
        "last_used": "last_used",
        "modified_time": "modified_time",
        "nonce": "nonce",
        "nonce_window": "nonce_window",
        "permissions": "permissions",
        "query_from": "query_from",
        "query_to": "query_to",
        "valid_until": "valid_until",
    }
)
_API_KEY_INFO_FIELD_PROFILES = (
    _API_KEY_INFO_CAMEL_FIELDS,
    _API_KEY_INFO_SNAKE_FIELDS,
)
_API_KEY_INFO_SCHEMA_ERROR = "Kraken GetApiKeyInfo response schema is unsupported"

type JsonMapping = Mapping[str, Any]
type PrivateEndpoint = Literal[
    "BalanceEx",
    "ClosedOrders",
    "GetApiKeyInfo",
    "Ledgers",
    "ListWalletAccounts",
    "OpenOrders",
    "QueryOrders",
    "TradesHistory",
    "TradeVolume",
]
type RequestValue = str | int
Clock = Callable[[], datetime]
NonceSource = Callable[[], int]
PacingHook = Callable[[str, int], None]


class KrakenReadError(RuntimeError):
    """Base class for safe, credential-free adapter failures."""


class KrakenTransportError(KrakenReadError):
    """The request failed or returned an unsafe transport response."""


class KrakenResponseError(KrakenReadError):
    """The response did not satisfy the expected Kraken contract."""


class KrakenApiError(KrakenReadError):
    """Kraken returned one or more top-level errors.

    Raw error strings are intentionally not retained.  Although Kraken's normal
    errors are safe codes, an upstream response must never be able to inject a
    request value or credential into logs through an exception.
    """

    def __init__(self, categories: tuple[str, ...]) -> None:
        super().__init__("Kraken rejected the read-only request")
        self.categories = categories


@dataclass(frozen=True, slots=True, repr=False)
class KrakenRequest:
    """Transport envelope whose string representation is always redacted."""

    method: Literal["GET", "POST"]
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    timeout_seconds: float
    endpoint_label: str
    max_response_bytes: int

    def __repr__(self) -> str:
        return (
            f"KrakenRequest(method={self.method!r}, endpoint={self.endpoint_label!r}, "
            "url=<redacted>, headers=<redacted>, body=<redacted>)"
        )

    __str__ = __repr__


Transport = Callable[[KrakenRequest], bytes]


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward authenticated headers to a redirected destination."""

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        return None


def _default_transport(request: KrakenRequest) -> bytes:
    target = urlsplit(request.url)
    if (
        target.scheme != "https"
        or target.hostname != "api.kraken.com"
        or target.username is not None
        or target.password is not None
        or target.port not in {None, 443}
    ):
        raise KrakenTransportError("Kraken request target is outside the pinned HTTPS origin")
    outbound = Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(outbound, timeout=request.timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    raise KrakenTransportError(
                        "Kraken returned an invalid response length"
                    ) from None
                if declared_length < 0 or declared_length > request.max_response_bytes:
                    raise KrakenTransportError("Kraken response exceeds the configured size limit")
            raw = cast(bytes, response.read(request.max_response_bytes + 1))
    except KrakenTransportError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError):
        # Do not chain exceptions: urllib exceptions may contain the full URL.
        raise KrakenTransportError("Kraken request failed") from None
    if len(raw) > request.max_response_bytes:
        raise KrakenTransportError("Kraken response exceeds the configured size limit")
    return raw


class MonotonicNonce:
    """Thread-safe millisecond nonce source for one API key in one process."""

    def __init__(self, *, time_ns: Callable[[], int] = time.time_ns) -> None:
        self._time_ns = time_ns
        self._last = -1
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            candidate = self._time_ns() // 1_000_000
            nonce = max(candidate, self._last + 1)
            if not 0 <= nonce <= MAX_UNSIGNED_64:
                raise KrakenReadError("nonce source produced an out-of-range value")
            self._last = nonce
            return nonce


class _PrivateSequenceState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_nonce = -1
        self.default_nonce = MonotonicNonce()


_SEQUENCE_STATES_LOCK = threading.Lock()
_SEQUENCE_STATES: dict[str, _PrivateSequenceState] = {}


def _sequence_state_for(api_key: str) -> _PrivateSequenceState:
    fingerprint = hashlib.sha256(api_key.encode("ascii")).hexdigest()
    with _SEQUENCE_STATES_LOCK:
        state = _SEQUENCE_STATES.get(fingerprint)
        if state is None:
            state = _PrivateSequenceState()
            _SEQUENCE_STATES[fingerprint] = state
        return state


class KrakenRequestBudget:
    """Thread-safe conservative private REST cost budget for one workflow.

    A fresh default budget is owned by each client.  Long-running orchestrators
    should create one client (or inject a fresh budget) per bounded
    reconciliation workflow.  Failed and rejected requests still consume cost.
    """

    def __init__(self, *, private_cost_limit: int = CONSERVATIVE_PRIVATE_COST_LIMIT) -> None:
        if isinstance(private_cost_limit, bool) or private_cost_limit <= 0:
            raise ValueError("private_cost_limit must be positive")
        self._private_cost_limit = private_cost_limit
        self._private_cost_spent = 0
        self._lock = threading.Lock()

    @property
    def private_cost_limit(self) -> int:
        return self._private_cost_limit

    @property
    def private_cost_spent(self) -> int:
        with self._lock:
            return self._private_cost_spent

    @property
    def private_cost_remaining(self) -> int:
        with self._lock:
            return self._private_cost_limit - self._private_cost_spent

    def consume_private(self, endpoint: PrivateEndpoint) -> None:
        cost = PRIVATE_ENDPOINT_COSTS[endpoint]
        with self._lock:
            if self._private_cost_spent + cost > self._private_cost_limit:
                raise KrakenReadError("Kraken private request cost budget is exhausted")
            self._private_cost_spent += cost


@dataclass(frozen=True, slots=True)
class ServerTimeSnapshot:
    server_time: datetime
    observed_at: datetime
    clock_skew: timedelta


class KrakenSystemStatus(StrEnum):
    ONLINE = "online"
    MAINTENANCE = "maintenance"
    CANCEL_ONLY = "cancel_only"
    POST_ONLY = "post_only"


@dataclass(frozen=True, slots=True)
class SystemStatusSnapshot:
    status: KrakenSystemStatus
    status_at: datetime
    observed_at: datetime

    @property
    def is_online(self) -> bool:
        return self.status is KrakenSystemStatus.ONLINE


@dataclass(frozen=True, slots=True)
class FeeBracket:
    volume: Decimal
    fee_percent: Decimal


@dataclass(frozen=True, slots=True)
class AssetPair:
    exchange_pair: str
    alternate_name: str
    websocket_name: str
    base_asset: str
    quote_asset: str
    status: str
    order_minimum: Decimal
    cost_minimum: Decimal
    tick_size: Decimal
    cost_decimals: int
    pair_decimals: int
    lot_decimals: int
    taker_schedule: tuple[FeeBracket, ...]
    maker_schedule: tuple[FeeBracket, ...]


@dataclass(frozen=True, slots=True)
class AssetPairSnapshot:
    pair: AssetPair
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CurrentFee:
    pair: str
    fee_percent: Decimal
    minimum_fee_percent: Decimal
    maximum_fee_percent: Decimal
    tier_volume: Decimal
    next_fee_percent: Decimal | None
    next_volume: Decimal | None


@dataclass(frozen=True, slots=True)
class TradeVolumeSnapshot:
    currency: str
    rolling_volume: Decimal
    taker_fees: tuple[CurrentFee, ...]
    maker_fees: tuple[CurrentFee, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ExtendedBalance:
    asset: str
    balance: Decimal
    credit: Decimal
    credit_used: Decimal
    hold_trade: Decimal

    @property
    def available(self) -> Decimal:
        return self.balance + self.credit - self.credit_used - self.hold_trade


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    balances: tuple[ExtendedBalance, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    client_order_id: str | None
    reference_id: str | None
    user_reference: int | None
    status: str
    reason: str | None
    opened_at: datetime
    closed_at: datetime | None
    starts_at: datetime | None
    expires_at: datetime | None
    pair: str
    side: Literal["buy", "sell"]
    order_type: str
    requested_price: Decimal
    secondary_price: Decimal
    leverage: str
    volume: Decimal
    executed_volume: Decimal
    cost: Decimal
    fee: Decimal
    average_price: Decimal
    stop_price: Decimal
    limit_price: Decimal
    flags: tuple[str, ...]
    trade_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenOrdersSnapshot:
    orders: tuple[OrderRecord, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ClosedOrdersPage:
    orders: tuple[OrderRecord, ...]
    total_count: int
    offset: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OrderQuerySnapshot:
    orders: tuple[OrderRecord, ...]
    requested_order_ids: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: str
    order_id: str
    position_id: str | None
    pair: str
    executed_at: datetime
    side: Literal["buy", "sell"]
    order_type: str
    price: Decimal
    cost: Decimal
    fee: Decimal
    volume: Decimal
    margin: Decimal
    maker: bool | None
    exchange_trade_id: int | None
    ledger_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TradeHistoryPage:
    trades: tuple[TradeRecord, ...]
    total_count: int | None
    offset: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    ledger_id: str
    reference_id: str
    recorded_at: datetime
    entry_type: str
    subtype: str
    asset_class: str
    asset: str
    amount: Decimal
    fee: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class LedgerPage:
    entries: tuple[LedgerRecord, ...]
    total_count: int | None
    offset: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WalletAccount:
    account_id: str
    status: str
    account_type: str
    active: bool
    user_defined: bool


@dataclass(frozen=True, slots=True)
class WalletAccountsSnapshot:
    accounts: tuple[WalletAccount, ...]
    complete: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ApiKeyInfoSnapshot:
    """Sanitized API-key metadata; the raw key returned by Kraken is discarded."""

    key_name: str
    permissions: tuple[str, ...]
    exchange_nonce: int
    nonce_window: int
    ip_allowlist: tuple[str, ...]
    created_at: datetime
    modified_at: datetime
    last_used_at: datetime | None
    valid_until: datetime | None
    query_from: datetime | None
    query_to: datetime | None
    observed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _mapping(value: object, *, field: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise KrakenResponseError(f"Kraken response field {field!r} must be an object")
    return cast(JsonMapping, value)


def _sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise KrakenResponseError(f"Kraken response field {field!r} must be an array")
    return value


def _string(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise KrakenResponseError(f"Kraken response field {field!r} must be a string")
    return value


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None or value == "None":
        return None
    return _string(value, field=field, allow_empty=True)


def _api_key_identifier(value: object) -> str:
    """Validate an echoed public key without reflecting it into an error."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or not value.isascii()
        or not all(0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise KrakenResponseError("Kraken response API-key identifier is invalid")
    return value


def _api_key_info_field_profile(row: Mapping[str, object]) -> Mapping[str, str]:
    """Select one complete, unambiguous Kraken API-key metadata schema."""

    observed_fields = frozenset(row)
    matches = tuple(
        profile
        for profile in _API_KEY_INFO_FIELD_PROFILES
        if observed_fields == frozenset(profile.values())
    )
    if len(matches) != 1:
        raise KrakenResponseError(_API_KEY_INFO_SCHEMA_ERROR)
    return matches[0]


def _decimal(value: object, *, field: str, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise KrakenResponseError(f"Kraken response field {field!r} is not a decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise KrakenResponseError(f"Kraken response field {field!r} is not a decimal") from None
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise KrakenResponseError(f"Kraken response field {field!r} is outside its domain")
    return parsed


def _integer(value: object, *, field: str, nonnegative: bool = False) -> int:
    parsed = _decimal(value, field=field, nonnegative=nonnegative)
    integral = parsed.to_integral_value()
    if parsed != integral:
        raise KrakenResponseError(f"Kraken response field {field!r} must be an integer")
    return int(integral)


def _bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise KrakenResponseError(f"Kraken response field {field!r} must be a boolean")
    return value


def _timestamp(value: object, *, field: str, zero_is_none: bool = False) -> datetime | None:
    seconds = _decimal(value, field=field, nonnegative=True)
    if zero_is_none and seconds == 0:
        return None
    whole_seconds = seconds.to_integral_value(rounding=ROUND_FLOOR)
    exact_microseconds = (seconds - whole_seconds) * Decimal("1000000")
    if exact_microseconds != exact_microseconds.to_integral_value():
        raise KrakenResponseError(
            f"Kraken response field {field!r} exceeds microsecond timestamp precision"
        )
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            seconds=int(whole_seconds),
            microseconds=int(exact_microseconds),
        )
    except (OverflowError, ValueError):
        raise KrakenResponseError(f"Kraken response field {field!r} is not a timestamp") from None


def _optional_timestamp(value: object, *, field: str) -> datetime | None:
    if value is None or value == "" or value == "0" or value == 0:
        return None
    return _timestamp(value, field=field)


def _optional_decimal(value: object, *, field: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field=field, nonnegative=True)


def _iso_timestamp(value: object, *, field: str) -> datetime:
    text = _string(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise KrakenResponseError(f"Kraken response field {field!r} is not a timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KrakenResponseError(f"Kraken response field {field!r} is not timezone-aware")
    return parsed.astimezone(UTC)


def _classify_api_error(value: str) -> str:
    lowered = value.lower()
    if "nonce" in lowered:
        return "nonce"
    if "permission" in lowered or "denied" in lowered:
        return "permission"
    if "key" in lowered or "auth" in lowered:
        return "authentication"
    if "rate" in lowered or "throttle" in lowered:
        return "rate_limit"
    if "service" in lowered or "unavailable" in lowered or "busy" in lowered:
        return "service"
    if "invalid" in lowered or "query" in lowered:
        return "request"
    return "unknown"


def _parse_envelope(raw: bytes, *, max_response_bytes: int) -> JsonMapping:
    if len(raw) > max_response_bytes:
        raise KrakenTransportError("Kraken response exceeds the configured size limit")
    try:
        decoded = json.loads(
            raw,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise KrakenResponseError("Kraken returned invalid JSON") from None
    root = _mapping(decoded, field="response")
    errors = _sequence(root.get("error"), field="error")
    parsed_errors: list[str] = []
    for error in errors:
        parsed_errors.append(_string(error, field="error item"))
    if parsed_errors:
        categories = tuple(dict.fromkeys(_classify_api_error(error) for error in parsed_errors))
        raise KrakenApiError(categories)
    if "result" not in root:
        raise KrakenResponseError("Kraken response is missing result")
    return _mapping(root["result"], field="result")


def _validate_clock(clock: Clock) -> datetime:
    try:
        observed_at = clock()
    except Exception:
        raise KrakenReadError("clock failed") from None
    if not isinstance(observed_at, datetime):
        raise KrakenReadError("clock must return a datetime")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise KrakenReadError("clock must return a timezone-aware datetime")
    return observed_at.astimezone(UTC)


def _validate_pair_alias(pair: str) -> str:
    if not isinstance(pair, str) or pair.upper() not in BTC_CAD_ALIASES:
        raise ValueError("pair must identify BTC/CAD")
    return "XBTCAD"


def _validate_optional_epoch(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a nonnegative Unix timestamp")
    return value


def _validate_offset(offset: int) -> int:
    if isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    return offset


def _parse_fee_brackets(value: object, *, field: str) -> tuple[FeeBracket, ...]:
    result: list[FeeBracket] = []
    prior_volume: Decimal | None = None
    for index, item in enumerate(_sequence(value, field=field)):
        row = _sequence(item, field=f"{field}[{index}]")
        if len(row) != 2:
            raise KrakenResponseError(f"Kraken response field {field!r} has an invalid row")
        volume = _decimal(row[0], field=f"{field} volume", nonnegative=True)
        fee = _decimal(row[1], field=f"{field} fee", nonnegative=True)
        if prior_volume is not None and volume <= prior_volume:
            raise KrakenResponseError(f"Kraken response field {field!r} is not ordered")
        prior_volume = volume
        result.append(FeeBracket(volume=volume, fee_percent=fee))
    if not result:
        raise KrakenResponseError(f"Kraken response field {field!r} cannot be empty")
    return tuple(result)


def _parse_asset_pair(result: JsonMapping) -> AssetPair:
    pair_keys = tuple(result)
    if len(pair_keys) != 1:
        raise KrakenResponseError("Kraken AssetPairs result must contain exactly one pair")
    exchange_pair = pair_keys[0]
    if exchange_pair.upper() not in BTC_CAD_ALIASES:
        raise KrakenResponseError("Kraken AssetPairs result is not BTC/CAD")
    row = _mapping(result[exchange_pair], field="asset pair record")
    cost_decimals = _integer(row.get("cost_decimals"), field="cost_decimals", nonnegative=True)
    pair_decimals = _integer(row.get("pair_decimals"), field="pair_decimals", nonnegative=True)
    lot_decimals = _integer(row.get("lot_decimals"), field="lot_decimals", nonnegative=True)
    if cost_decimals > 18 or pair_decimals > 18 or lot_decimals > 18:
        raise KrakenResponseError("Kraken pair precision exceeds the supported bound")
    order_minimum = _decimal(row.get("ordermin"), field="ordermin", nonnegative=True)
    cost_minimum = _decimal(row.get("costmin"), field="costmin", nonnegative=True)
    tick_size = _decimal(row.get("tick_size"), field="tick_size", nonnegative=True)
    if order_minimum <= 0 or cost_minimum <= 0 or tick_size <= 0:
        raise KrakenResponseError("Kraken pair minimums and tick size must be positive")
    return AssetPair(
        exchange_pair=exchange_pair,
        alternate_name=_string(row.get("altname"), field="altname"),
        websocket_name=_string(row.get("wsname"), field="wsname"),
        base_asset=_string(row.get("base"), field="base"),
        quote_asset=_string(row.get("quote"), field="quote"),
        status=_string(row.get("status"), field="status"),
        order_minimum=order_minimum,
        cost_minimum=cost_minimum,
        tick_size=tick_size,
        cost_decimals=cost_decimals,
        pair_decimals=pair_decimals,
        lot_decimals=lot_decimals,
        taker_schedule=_parse_fee_brackets(row.get("fees"), field="fees"),
        maker_schedule=_parse_fee_brackets(row.get("fees_maker"), field="fees_maker"),
    )


def _parse_current_fees(value: object, *, field: str) -> tuple[CurrentFee, ...]:
    rows = _mapping(value, field=field)
    result: list[CurrentFee] = []
    for pair in sorted(rows):
        row = _mapping(rows[pair], field="fee record")
        next_fee = _optional_decimal(row.get("nextfee"), field="nextfee")
        next_volume = _optional_decimal(row.get("nextvolume"), field="nextvolume")
        result.append(
            CurrentFee(
                pair=pair,
                fee_percent=_decimal(row.get("fee"), field="fee", nonnegative=True),
                minimum_fee_percent=_decimal(row.get("minfee"), field="minfee", nonnegative=True),
                maximum_fee_percent=_decimal(row.get("maxfee"), field="maxfee", nonnegative=True),
                tier_volume=_decimal(row.get("tiervolume"), field="tiervolume", nonnegative=True),
                next_fee_percent=next_fee,
                next_volume=next_volume,
            )
        )
    if not result:
        raise KrakenResponseError(f"Kraken response field {field!r} cannot be empty")
    return tuple(result)


def _parse_order(order_id: str, value: object) -> OrderRecord:
    row = _mapping(value, field="order record")
    description = _mapping(row.get("descr"), field="descr")
    side = _string(description.get("type"), field="descr.type")
    if side not in {"buy", "sell"}:
        raise KrakenResponseError("Kraken order side is unsupported")
    status = _string(row.get("status"), field="status")
    if status not in {"pending", "open", "closed", "canceled", "expired"}:
        raise KrakenResponseError("Kraken order status is unsupported")
    trade_ids = tuple(
        _string(item, field="trades item")
        for item in _sequence(row.get("trades", ()), field="trades")
    )
    raw_flags = _string(row.get("oflags", ""), field="oflags", allow_empty=True)
    user_reference_value = row.get("userref")
    user_reference = (
        None if user_reference_value is None else _integer(user_reference_value, field="userref")
    )
    return OrderRecord(
        order_id=order_id,
        client_order_id=_optional_string(row.get("cl_ord_id"), field="cl_ord_id"),
        reference_id=_optional_string(row.get("refid"), field="refid"),
        user_reference=user_reference,
        status=status,
        reason=_optional_string(row.get("reason"), field="reason"),
        opened_at=cast(datetime, _timestamp(row.get("opentm"), field="opentm")),
        closed_at=_timestamp(row.get("closetm", 0), field="closetm", zero_is_none=True),
        starts_at=_timestamp(row.get("starttm", 0), field="starttm", zero_is_none=True),
        expires_at=_timestamp(row.get("expiretm", 0), field="expiretm", zero_is_none=True),
        pair=_string(description.get("pair"), field="descr.pair"),
        side=cast(Literal["buy", "sell"], side),
        order_type=_string(description.get("ordertype"), field="descr.ordertype"),
        requested_price=_decimal(description.get("price"), field="descr.price", nonnegative=True),
        secondary_price=_decimal(
            description.get("price2", "0"), field="descr.price2", nonnegative=True
        ),
        leverage=_string(description.get("leverage", "none"), field="descr.leverage"),
        volume=_decimal(row.get("vol"), field="vol", nonnegative=True),
        executed_volume=_decimal(row.get("vol_exec"), field="vol_exec", nonnegative=True),
        cost=_decimal(row.get("cost"), field="cost", nonnegative=True),
        fee=_decimal(row.get("fee"), field="fee", nonnegative=True),
        average_price=_decimal(row.get("price"), field="price", nonnegative=True),
        stop_price=_decimal(row.get("stopprice", "0"), field="stopprice", nonnegative=True),
        limit_price=_decimal(row.get("limitprice", "0"), field="limitprice", nonnegative=True),
        flags=tuple(flag for flag in raw_flags.split(",") if flag),
        trade_ids=trade_ids,
    )


def _parse_orders(value: object, *, field: str) -> tuple[OrderRecord, ...]:
    rows = _mapping(value, field=field)
    return tuple(_parse_order(order_id, rows[order_id]) for order_id in sorted(rows))


def _parse_trade(trade_id: str, value: object) -> TradeRecord:
    row = _mapping(value, field="trade record")
    side = _string(row.get("type"), field="type")
    if side not in {"buy", "sell"}:
        raise KrakenResponseError("Kraken trade side is unsupported")
    maker_value = row.get("maker")
    maker = None if maker_value is None else _bool(maker_value, field="maker")
    exchange_trade_value = row.get("trade_id")
    exchange_trade_id = (
        None
        if exchange_trade_value is None
        else _integer(exchange_trade_value, field="trade_id", nonnegative=True)
    )
    ledger_ids = tuple(
        _string(item, field="ledgers item")
        for item in _sequence(row.get("ledgers", ()), field="ledgers")
    )
    return TradeRecord(
        trade_id=trade_id,
        order_id=_string(row.get("ordertxid"), field="ordertxid"),
        position_id=_optional_string(row.get("postxid"), field="postxid"),
        pair=_string(row.get("pair"), field="pair"),
        executed_at=cast(datetime, _timestamp(row.get("time"), field="time")),
        side=cast(Literal["buy", "sell"], side),
        order_type=_string(row.get("ordertype"), field="ordertype"),
        price=_decimal(row.get("price"), field="price", nonnegative=True),
        cost=_decimal(row.get("cost"), field="cost", nonnegative=True),
        fee=_decimal(row.get("fee"), field="fee", nonnegative=True),
        volume=_decimal(row.get("vol"), field="vol", nonnegative=True),
        margin=_decimal(row.get("margin", "0"), field="margin", nonnegative=True),
        maker=maker,
        exchange_trade_id=exchange_trade_id,
        ledger_ids=ledger_ids,
    )


def _parse_ledger(ledger_id: str, value: object) -> LedgerRecord:
    row = _mapping(value, field="ledger record")
    return LedgerRecord(
        ledger_id=ledger_id,
        reference_id=_string(row.get("refid"), field="refid", allow_empty=True),
        recorded_at=cast(datetime, _timestamp(row.get("time"), field="time")),
        entry_type=_string(row.get("type"), field="type"),
        subtype=_string(row.get("subtype", ""), field="subtype", allow_empty=True),
        asset_class=_string(row.get("aclass"), field="aclass"),
        asset=_string(row.get("asset"), field="asset"),
        amount=_decimal(row.get("amount"), field="amount"),
        fee=_decimal(row.get("fee"), field="fee", nonnegative=True),
        balance=_decimal(row.get("balance"), field="balance"),
    )


def sign_read_only_request(
    *, endpoint: PrivateEndpoint, body: bytes, nonce: int, secret: bytes
) -> str:
    """Create a signature for one of the adapter's frozen read endpoints."""

    if endpoint not in PRIVATE_ENDPOINT_COSTS:
        raise ValueError("endpoint is not in the read-only allowlist")
    if not 0 <= nonce <= MAX_UNSIGNED_64:
        raise ValueError("nonce must be an unsigned 64-bit integer")
    path = f"/0/private/{endpoint}"
    digest = hashlib.sha256(str(nonce).encode("ascii") + body).digest()
    message = path.encode("ascii") + digest
    return base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode("ascii")


class KrakenReadClient:
    """Closed-surface Kraken Spot REST reader with strict response validation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        transport: Transport | None = None,
        clock: Clock = _utc_now,
        nonce: NonceSource | None = None,
        request_budget: KrakenRequestBudget | None = None,
        pacing_hook: PacingHook | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if (api_key is None) != (api_secret is None):
            raise ValueError("api_key and api_secret must be supplied together")
        secret_bytes: bytes | None = None
        if api_key is not None and api_secret is not None:
            if (
                not api_key
                or len(api_key) > 512
                or not api_key.isascii()
                or not all(0x21 <= ord(character) <= 0x7E for character in api_key)
            ):
                raise ValueError("api_key must be bounded printable ASCII without spaces")
            if len(api_secret) > 4096:
                raise ValueError("api_secret is too long")
            try:
                secret_bytes = base64.b64decode(api_secret, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("api_secret must be valid base64") from None
            if not secret_bytes:
                raise ValueError("api_secret must decode to non-empty bytes")
        self._api_key = api_key
        self._secret = secret_bytes
        self._private_state = None if api_key is None else _sequence_state_for(api_key)
        self._transport = transport or _default_transport
        self._clock = clock
        self._nonce = (
            nonce
            if nonce is not None
            else None
            if self._private_state is None
            else self._private_state.default_nonce
        )
        self._request_budget = request_budget or KrakenRequestBudget()
        self._pacing_hook = pacing_hook
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return (
            "KrakenReadClient(api_key=<redacted>, api_secret=<redacted>, "
            f"timeout_seconds={self._timeout_seconds!r})"
        )

    @property
    def private_cost_spent(self) -> int:
        return self._request_budget.private_cost_spent

    @property
    def private_cost_remaining(self) -> int:
        return self._request_budget.private_cost_remaining

    def _send(self, request: KrakenRequest) -> tuple[JsonMapping, datetime]:
        try:
            raw = self._transport(request)
        except KrakenReadError:
            raise
        except Exception:
            # Third-party/injected transport exceptions are untrusted too.
            raise KrakenTransportError("Kraken request failed") from None
        if not isinstance(raw, bytes):
            raise KrakenTransportError("Kraken transport must return bytes")
        result = _parse_envelope(raw, max_response_bytes=self._max_response_bytes)
        return result, _validate_clock(self._clock)

    def _public_get(
        self,
        endpoint: Literal["Time", "SystemStatus", "AssetPairs"],
        params: Mapping[str, RequestValue],
    ) -> tuple[JsonMapping, datetime]:
        self._pace(f"public:{endpoint}", 1)
        path = f"/0/public/{endpoint}"
        encoded = urlencode(tuple(params.items()))
        url = f"{KRAKEN_API_ORIGIN}{path}"
        if encoded:
            url = f"{url}?{encoded}"
        request = KrakenRequest(
            method="GET",
            url=url,
            headers=(("Accept", "application/json"), ("User-Agent", "kraken-knight/0.2")),
            body=None,
            timeout_seconds=self._timeout_seconds,
            endpoint_label=f"public:{endpoint}",
            max_response_bytes=self._max_response_bytes,
        )
        return self._send(request)

    def _private_post(
        self,
        endpoint: PrivateEndpoint,
        params: Mapping[str, RequestValue],
        *,
        query_params: Mapping[str, RequestValue] = MappingProxyType({}),
    ) -> tuple[JsonMapping, datetime]:
        if (
            self._api_key is None
            or self._secret is None
            or self._private_state is None
            or self._nonce is None
        ):
            raise KrakenReadError("private Kraken credentials are not configured")
        with self._private_state.lock:
            self._request_budget.consume_private(endpoint)
            self._pace(f"private:{endpoint}", PRIVATE_ENDPOINT_COSTS[endpoint])
            try:
                nonce = self._nonce()
            except KrakenReadError:
                raise
            except Exception:
                raise KrakenReadError("nonce source failed") from None
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or not 0 <= nonce <= MAX_UNSIGNED_64
            ):
                raise KrakenReadError("nonce source produced an out-of-range value")
            if nonce <= self._private_state.last_nonce:
                raise KrakenReadError("nonce source did not increase")
            self._private_state.last_nonce = nonce
            body = urlencode((("nonce", str(nonce)), *params.items())).encode("ascii")
            path = f"/0/private/{endpoint}"
            encoded_query = urlencode(tuple(query_params.items()))
            url = f"{KRAKEN_API_ORIGIN}{path}"
            if encoded_query:
                url = f"{url}?{encoded_query}"
            signature = sign_read_only_request(
                endpoint=endpoint,
                body=body,
                nonce=nonce,
                secret=self._secret,
            )
            request = KrakenRequest(
                method="POST",
                url=url,
                headers=(
                    ("Accept", "application/json"),
                    ("Content-Type", "application/x-www-form-urlencoded"),
                    ("API-Key", self._api_key),
                    ("API-Sign", signature),
                    ("User-Agent", "kraken-knight/0.2"),
                ),
                body=body,
                timeout_seconds=self._timeout_seconds,
                endpoint_label=f"private:{endpoint}",
                max_response_bytes=self._max_response_bytes,
            )
            # The lock covers transport so a later nonce cannot arrive first.
            return self._send(request)

    def _pace(self, endpoint_label: str, cost: int) -> None:
        if self._pacing_hook is None:
            return
        try:
            self._pacing_hook(endpoint_label, cost)
        except Exception:
            raise KrakenReadError("Kraken request pacing hook failed") from None

    def get_server_time(self) -> ServerTimeSnapshot:
        result, observed_at = self._public_get("Time", MappingProxyType({}))
        unixtime = _integer(result.get("unixtime"), field="unixtime", nonnegative=True)
        server_time = cast(datetime, _timestamp(unixtime, field="unixtime"))
        return ServerTimeSnapshot(
            server_time=server_time,
            observed_at=observed_at,
            clock_skew=server_time - observed_at,
        )

    def get_system_status(self) -> SystemStatusSnapshot:
        result, observed_at = self._public_get("SystemStatus", MappingProxyType({}))
        raw_status = _string(result.get("status"), field="status")
        try:
            status = KrakenSystemStatus(raw_status)
        except ValueError:
            raise KrakenResponseError("Kraken system status is unsupported") from None
        return SystemStatusSnapshot(
            status=status,
            status_at=_iso_timestamp(result.get("timestamp"), field="timestamp"),
            observed_at=observed_at,
        )

    def get_asset_pair(self, *, pair: str = "XBTCAD") -> AssetPairSnapshot:
        requested_pair = _validate_pair_alias(pair)
        result, observed_at = self._public_get("AssetPairs", {"pair": requested_pair})
        return AssetPairSnapshot(pair=_parse_asset_pair(result), observed_at=observed_at)

    def get_trade_volume(self, *, pair: str = "XBTCAD") -> TradeVolumeSnapshot:
        requested_pair = _validate_pair_alias(pair)
        result, observed_at = self._private_post("TradeVolume", {"pair": requested_pair})
        return TradeVolumeSnapshot(
            currency=_string(result.get("currency"), field="currency"),
            rolling_volume=_decimal(result.get("volume"), field="volume", nonnegative=True),
            taker_fees=_parse_current_fees(result.get("fees"), field="fees"),
            maker_fees=_parse_current_fees(result.get("fees_maker"), field="fees_maker"),
            observed_at=observed_at,
        )

    def get_extended_balances(self) -> BalanceSnapshot:
        result, observed_at = self._private_post("BalanceEx", {})
        balances: list[ExtendedBalance] = []
        for asset in sorted(result):
            row = _mapping(result[asset], field="balance record")
            balances.append(
                ExtendedBalance(
                    asset=asset,
                    balance=_decimal(row.get("balance"), field="balance"),
                    credit=_decimal(row.get("credit", "0"), field="credit"),
                    credit_used=_decimal(row.get("credit_used", "0"), field="credit_used"),
                    hold_trade=_decimal(
                        row.get("hold_trade", "0"), field="hold_trade", nonnegative=True
                    ),
                )
            )
        return BalanceSnapshot(balances=tuple(balances), observed_at=observed_at)

    def get_open_orders(self, *, client_order_id: str | None = None) -> OpenOrdersSnapshot:
        params: dict[str, RequestValue] = {"trades": "true"}
        if client_order_id is not None:
            if (
                not client_order_id
                or not client_order_id.isascii()
                or not client_order_id.isprintable()
                or client_order_id.isspace()
                or len(client_order_id) > 36
            ):
                raise ValueError("client_order_id must be 1-36 ASCII characters")
            params["cl_ord_id"] = client_order_id
        result, observed_at = self._private_post("OpenOrders", params)
        return OpenOrdersSnapshot(
            orders=_parse_orders(result.get("open"), field="open"),
            observed_at=observed_at,
        )

    def get_closed_orders(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        client_order_id: str | None = None,
    ) -> ClosedOrdersPage:
        start = _validate_optional_epoch(start, field="start")
        end = _validate_optional_epoch(end, field="end")
        offset = _validate_offset(offset)
        if start is not None and end is not None and start > end:
            raise ValueError("start cannot be later than end")
        params: dict[str, RequestValue] = {
            "trades": "true",
            "consolidate_taker": "false",
            "closetime": "both",
            "ofs": offset,
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if client_order_id is not None:
            if (
                not client_order_id
                or not client_order_id.isascii()
                or not client_order_id.isprintable()
                or client_order_id.isspace()
                or len(client_order_id) > 36
            ):
                raise ValueError("client_order_id must be 1-36 ASCII characters")
            params["cl_ord_id"] = client_order_id
        result, observed_at = self._private_post("ClosedOrders", params)
        return ClosedOrdersPage(
            orders=_parse_orders(result.get("closed"), field="closed"),
            total_count=_integer(result.get("count"), field="count", nonnegative=True),
            offset=offset,
            observed_at=observed_at,
        )

    def query_orders(self, order_ids: Sequence[str]) -> OrderQuerySnapshot:
        if isinstance(order_ids, (str, bytes, bytearray)):
            raise TypeError("order_ids must be a sequence of order IDs")
        requested = tuple(order_ids)
        if not requested or len(requested) > 50 or len(set(requested)) != len(requested):
            raise ValueError("order_ids must contain 1-50 unique IDs")
        for order_id in requested:
            if not isinstance(order_id, str) or not order_id or not order_id.isascii():
                raise ValueError("each order ID must be non-empty ASCII")
            if (
                not order_id.isprintable()
                or order_id.isspace()
                or "," in order_id
                or len(order_id) > 64
            ):
                raise ValueError("each order ID must be a valid Kraken identifier")
        result, observed_at = self._private_post(
            "QueryOrders",
            {
                "trades": "true",
                "consolidate_taker": "false",
                "txid": ",".join(requested),
            },
        )
        return OrderQuerySnapshot(
            orders=_parse_orders(result, field="result"),
            requested_order_ids=requested,
            observed_at=observed_at,
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
        start = _validate_optional_epoch(start, field="start")
        end = _validate_optional_epoch(end, field="end")
        offset = _validate_offset(offset)
        if start is not None and end is not None and start > end:
            raise ValueError("start cannot be later than end")
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params: dict[str, RequestValue] = {
            "type": "all",
            "trades": "false",
            "consolidate_taker": "false",
            "ledgers": "true",
            "ofs": offset,
            "limit": limit,
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if pair is not None:
            params["pair"] = _validate_pair_alias(pair)
        result, observed_at = self._private_post("TradesHistory", params)
        rows = _mapping(result.get("trades"), field="trades")
        trades = tuple(_parse_trade(trade_id, rows[trade_id]) for trade_id in sorted(rows))
        total_count = (
            None
            if "count" not in result
            else _integer(result["count"], field="count", nonnegative=True)
        )
        return TradeHistoryPage(
            trades=trades,
            total_count=total_count,
            offset=offset,
            observed_at=observed_at,
        )

    def get_ledgers(
        self,
        *,
        account_id: str,
        entry_type: str = "all",
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
    ) -> LedgerPage:
        normalized_account_id = account_id.strip().upper()
        account_id_parts = normalized_account_id.split("-")
        if len(account_id_parts) != 4 or any(
            len(part) != 4 or not part.isascii() or not part.isalnum() for part in account_id_parts
        ):
            raise ValueError("account_id must use Kraken's public wallet-account format")
        allowed_types = frozenset(
            {
                "all",
                "deposit",
                "withdrawal",
                "trade",
                "margin",
                "rollover",
                "credit",
                "transfer",
                "settled",
                "staking",
                "sale",
                "dividend",
            }
        )
        if entry_type not in allowed_types:
            raise ValueError("entry_type is not supported")
        start = _validate_optional_epoch(start, field="start")
        end = _validate_optional_epoch(end, field="end")
        offset = _validate_offset(offset)
        if start is not None and end is not None and start > end:
            raise ValueError("start cannot be later than end")
        params: dict[str, RequestValue] = {"type": entry_type, "ofs": offset}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        result, observed_at = self._private_post(
            "Ledgers",
            params,
            query_params={"account_id": normalized_account_id},
        )
        rows = _mapping(result.get("ledger"), field="ledger")
        entries = tuple(_parse_ledger(ledger_id, rows[ledger_id]) for ledger_id in sorted(rows))
        total_count = (
            None
            if "count" not in result
            else _integer(result["count"], field="count", nonnegative=True)
        )
        return LedgerPage(
            entries=entries,
            total_count=total_count,
            offset=offset,
            observed_at=observed_at,
        )

    def get_api_key_info(self) -> ApiKeyInfoSnapshot:
        result, observed_at = self._private_post("GetApiKeyInfo", {})
        fields = _api_key_info_field_profile(result)
        # Kraken returns the public API key in this payload. Validate its shape,
        # then intentionally discard it so it cannot enter a model or repr.
        returned_key = _api_key_identifier(result[fields["api_key"]])
        if self._api_key is None or not hmac.compare_digest(returned_key, self._api_key):
            raise KrakenResponseError("Kraken API-key identity does not match the request")
        _string(result[fields["iban"]], field="iban", allow_empty=True)
        permissions = tuple(
            _string(item, field="permissions item")
            for item in _sequence(result[fields["permissions"]], field="permissions")
        )
        ip_allowlist = tuple(
            _string(item, field="ipAllowlist item")
            for item in _sequence(result[fields["ip_allowlist"]], field="ipAllowlist")
        )
        last_used = _optional_timestamp(
            result[fields["last_used"]],
            field="lastUsed",
        )
        return ApiKeyInfoSnapshot(
            key_name=_string(
                result[fields["api_key_name"]],
                field="apiKeyName",
            ),
            permissions=permissions,
            exchange_nonce=_integer(
                result[fields["nonce"]],
                field="nonce",
                nonnegative=True,
            ),
            nonce_window=_integer(
                result[fields["nonce_window"]],
                field="nonceWindow",
                nonnegative=True,
            ),
            ip_allowlist=ip_allowlist,
            created_at=cast(
                datetime,
                _timestamp(
                    result[fields["created_time"]],
                    field="createdTime",
                ),
            ),
            modified_at=cast(
                datetime,
                _timestamp(
                    result[fields["modified_time"]],
                    field="modifiedTime",
                ),
            ),
            last_used_at=last_used,
            valid_until=_optional_timestamp(
                result[fields["valid_until"]],
                field="validUntil",
            ),
            query_from=_optional_timestamp(
                result[fields["query_from"]],
                field="queryFrom",
            ),
            query_to=_optional_timestamp(
                result[fields["query_to"]],
                field="queryTo",
            ),
            observed_at=observed_at,
        )

    def get_wallet_accounts(self) -> WalletAccountsSnapshot:
        result, observed_at = self._private_post("ListWalletAccounts", {})
        accounts: list[WalletAccount] = []
        for index, value in enumerate(_sequence(result.get("accounts"), field="accounts")):
            row = _mapping(value, field=f"accounts[{index}]")
            flags = _mapping(row.get("flags"), field=f"accounts[{index}].flags")
            account_id = _string(row.get("account_id"), field="account_id")
            if (
                len(account_id) > 64
                or not account_id.isascii()
                or not account_id.isprintable()
                or account_id.isspace()
            ):
                raise KrakenResponseError("Kraken wallet account ID is invalid")
            accounts.append(
                WalletAccount(
                    account_id=account_id,
                    status=_string(row.get("status"), field="status"),
                    account_type=_string(row.get("type"), field="type"),
                    active=_bool(flags.get("active"), field="flags.active"),
                    user_defined=_bool(flags.get("user_defined"), field="flags.user_defined"),
                )
            )
        if len({account.account_id for account in accounts}) != len(accounts):
            raise KrakenResponseError("Kraken wallet account IDs are not unique")
        cursor = _mapping(result.get("cursor"), field="cursor")
        next_cursor = cursor.get("next")
        if next_cursor is not None:
            _string(next_cursor, field="cursor.next")
        return WalletAccountsSnapshot(
            accounts=tuple(accounts),
            complete=next_cursor is None,
            observed_at=observed_at,
        )
