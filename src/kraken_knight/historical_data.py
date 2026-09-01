"""Causal, resumable BTC/CAD trade-history acquisition and normalization.

The module deliberately uses Kraken's public ``Trades`` endpoint.  It never
accepts credentials, starts the cursor at zero, fixes an exclusive UTC cutoff,
and archives every successful response before advancing the durable cursor.
Daily bars are derived from trades without inventing rows for quiet UTC days.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KRAKEN_TRADES_URL = "https://api.kraken.com/0/public/Trades"
REQUEST_PAIR = "XBTCAD"
RESPONSE_PAIRS = frozenset({"XBTCAD", "XXBTZCAD"})
RAW_ARCHIVE_FILENAME = "kraken_xbtcad_trades.pages.ndjson"
STATE_FILENAME = "kraken_xbtcad_trades.state.json"
RAW_PAGE_SCHEMA = "kraken-trades-page-v1"
STATE_SCHEMA = "kraken-trades-state-v1"
DATASET_SCHEMA = "kraken-xbtcad-daily-v1"
MAX_PAGE_SIZE = 1000
NANOSECONDS_PER_SECOND = 1_000_000_000
CURSOR_TIMESTAMP_TOLERANCE_NS = 1_000
EXECUTION_WINDOW_START_MINUTE = 15
EXECUTION_WINDOW_END_MINUTE = 20


class HistoricalDataError(RuntimeError):
    """Raised when historical evidence is unavailable, malformed, or inconsistent."""


class RetryableHistoricalDataError(HistoricalDataError):
    """Raised for a transient public request failure that may safely be retried."""


class KrakenTradesApiError(HistoricalDataError):
    """A schema-valid Kraken API error response."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"Kraken API error: {', '.join(self.errors)}")

    @property
    def retryable(self) -> bool:
        return any(
            error.startswith(("EAPI:Rate limit exceeded", "EService:Throttled"))
            for error in self.errors
        )


@dataclass(frozen=True, slots=True)
class HistoricalTrade:
    """One strictly parsed public Kraken BTC/CAD execution."""

    price: Decimal
    volume: Decimal
    timestamp_ns: int
    side: str
    order_type: str
    misc: str
    trade_id: int

    @property
    def opened_at(self) -> datetime:
        seconds, nanoseconds = divmod(self.timestamp_ns, NANOSECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(
            microseconds=nanoseconds // 1_000
        )


@dataclass(frozen=True, slots=True)
class ParsedTradesPage:
    """A validated page plus its strictly advancing Kraken cursor."""

    pair: str
    request_cursor: str
    next_cursor: str
    trades: tuple[HistoricalTrade, ...]


@dataclass(frozen=True, slots=True)
class TradeArchive:
    """Durable raw-page archive returned by a completed or resumable download."""

    archive_path: Path
    state_path: Path
    cutoff: datetime
    page_size: int
    page_count: int
    final_cursor: str
    included_trade_count: int
    complete: bool
    raw_sha256: str
    first_retrieved_at: datetime | None
    last_retrieved_at: datetime | None
    last_trade: HistoricalTrade | None


@dataclass(frozen=True, slots=True)
class DailyTradeBar:
    """One observed UTC day aggregated directly from public executions."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    execution_minute: datetime | None
    execution_vwap: Decimal | None
    execution_volume: Decimal | None
    execution_trade_count: int | None


@dataclass(frozen=True, slots=True)
class NormalizedDataset:
    """Hashes and coverage metadata for deterministic normalized artifacts."""

    csv_path: Path
    manifest_path: Path
    csv_sha256: str
    manifest_sha256: str
    row_count: int
    first_date: date
    last_date: date
    gap_dates: tuple[date, ...]


Transport = Callable[[str, float], bytes]
Sleeper = Callable[[float], None]
Clock = Callable[[], datetime]
Progress = Callable[[TradeArchive], None]


def _default_transport(url: str, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "kraken-knight/0.2"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return cast(bytes, response.read())
    except HTTPError as exc:
        if exc.code == 429 or 500 <= exc.code < 600:
            raise RetryableHistoricalDataError("transient Kraken Trades HTTP failure") from exc
        raise HistoricalDataError("Kraken Trades HTTP request failed") from exc
    except (URLError, TimeoutError) as exc:
        raise RetryableHistoricalDataError("Kraken Trades request failed") from exc


def _utc_midnight(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be timezone-aware UTC")
    normalized = value.astimezone(UTC)
    if normalized.time() != datetime_time.min:
        raise ValueError(f"{field} must be UTC midnight")
    return normalized


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_z(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoricalDataError(f"{field} must be a UTC ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise HistoricalDataError(f"{field} is not valid ISO-8601") from exc
    if parsed.utcoffset() != timedelta(0):
        raise HistoricalDataError(f"{field} must be UTC")
    return parsed.astimezone(UTC)


def _datetime_ns(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value.astimezone(UTC) - epoch
    return (
        delta.days * 86_400 * NANOSECONDS_PER_SECOND
        + delta.seconds * NANOSECONDS_PER_SECOND
        + delta.microseconds * 1_000
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalDataError(f"JSON object contains duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise HistoricalDataError(f"JSON contains invalid numeric constant: {value}")


def _decode_json(raw: bytes) -> object:
    if not isinstance(raw, bytes):
        raise TypeError("raw payload must be bytes")
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("Kraken returned invalid JSON") from exc


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalDataError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise HistoricalDataError(f"{field} must be an array")
    return value


def _positive_decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise HistoricalDataError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HistoricalDataError(f"{field} is not decimal-compatible") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise HistoricalDataError(f"{field} must be finite and positive")
    return parsed


def _timestamp_ns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise HistoricalDataError("trade timestamp must be a JSON number")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise HistoricalDataError("trade timestamp must be finite and nonnegative")
    scaled = parsed * NANOSECONDS_PER_SECOND
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise HistoricalDataError("trade timestamp has greater than nanosecond precision")
    return int(integral)


def _cursor(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
        raise HistoricalDataError(f"{field} must be an unsigned decimal string")
    if len(value) > 1 and value.startswith("0"):
        raise HistoricalDataError(f"{field} must use canonical decimal form")
    return value


def _parse_trade(row: object) -> HistoricalTrade:
    values = _sequence(row, field="trade row")
    if len(values) != 7:
        raise HistoricalDataError("trade row must contain exactly seven fields")
    side = values[3]
    order_type = values[4]
    misc = values[5]
    trade_id = values[6]
    if side not in {"b", "s"}:
        raise HistoricalDataError("trade side must be b or s")
    if order_type not in {"l", "m"}:
        raise HistoricalDataError("trade order type must be l or m")
    if not isinstance(misc, str):
        raise HistoricalDataError("trade misc field must be a string")
    if isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id <= 0:
        raise HistoricalDataError("trade id must be a positive integer")
    return HistoricalTrade(
        price=_positive_decimal(values[0], field="trade price"),
        volume=_positive_decimal(values[1], field="trade volume"),
        timestamp_ns=_timestamp_ns(values[2]),
        side=cast(str, side),
        order_type=cast(str, order_type),
        misc=misc,
        trade_id=trade_id,
    )


def parse_trades_payload(
    payload: object,
    *,
    request_cursor: str,
    page_size: int = MAX_PAGE_SIZE,
) -> ParsedTradesPage:
    """Strictly bind one Kraken Trades response to BTC/CAD and its request cursor."""

    request_cursor = _cursor(request_cursor, field="request cursor")
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise TypeError("page_size must be an integer")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 1000")
    root = _mapping(payload, field="response")
    errors_raw = _sequence(root.get("error"), field="error")
    if not all(isinstance(error, str) for error in errors_raw):
        raise HistoricalDataError("Kraken error entries must be strings")
    errors = cast(Sequence[str], errors_raw)
    if errors:
        raise KrakenTradesApiError(errors)
    if set(root) != {"error", "result"}:
        raise HistoricalDataError("response must contain exactly error and result")
    result = _mapping(root["result"], field="result")
    pair_keys = [key for key in result if key != "last"]
    if len(pair_keys) != 1 or set(result) != {"last", *pair_keys}:
        raise HistoricalDataError("Trades result must contain exactly one pair and last")
    pair = pair_keys[0]
    if pair.upper() not in RESPONSE_PAIRS:
        raise HistoricalDataError("Trades response pair is not BTC/CAD")
    next_cursor = _cursor(result["last"], field="response cursor")
    if int(next_cursor) <= int(request_cursor):
        raise HistoricalDataError("Trades response cursor did not advance")
    rows = _sequence(result[pair], field=pair)
    if len(rows) > page_size:
        raise HistoricalDataError("Trades response exceeds requested page size")
    trades = tuple(_parse_trade(row) for row in rows)
    for previous, current in pairwise(trades):
        if current.timestamp_ns < previous.timestamp_ns:
            raise HistoricalDataError("Trades page is not timestamp ordered")
    if len({trade.trade_id for trade in trades}) != len(trades):
        raise HistoricalDataError("Trades page contains duplicate trade ids")
    request_ns = int(request_cursor)
    if trades and trades[0].timestamp_ns + CURSOR_TIMESTAMP_TOLERANCE_NS < request_ns:
        raise HistoricalDataError("Trades page moved backward before the request cursor")
    if trades and trades[-1].timestamp_ns > int(next_cursor) + CURSOR_TIMESTAMP_TOLERANCE_NS:
        raise HistoricalDataError("Trades response cursor precedes its final trade")
    return ParsedTradesPage(
        pair=pair,
        request_cursor=request_cursor,
        next_cursor=next_cursor,
        trades=trades,
    )


def parse_trades_bytes(
    raw: bytes,
    *,
    request_cursor: str,
    page_size: int = MAX_PAGE_SIZE,
) -> ParsedTradesPage:
    """Decode JSON with decimal-preserving rules, then validate the page."""

    return parse_trades_payload(
        _decode_json(raw),
        request_cursor=request_cursor,
        page_size=page_size,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as target:
        temporary_path = Path(target.name)
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _append_raw_record(path: Path, record: Mapping[str, object]) -> bytes:
    line = _canonical_json_bytes(record) + b"\n"
    with path.open("ab") as target:
        target.write(line)
        target.flush()
        os.fsync(target.fileno())
    return line


def _page_record(
    *,
    page_index: int,
    request_cursor: str,
    page: ParsedTradesPage,
    raw: bytes,
    cutoff: datetime,
    page_size: int,
    retrieved_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": RAW_PAGE_SCHEMA,
        "page_index": page_index,
        "request_pair": REQUEST_PAIR,
        "response_pair": page.pair,
        "request_cursor": request_cursor,
        "response_cursor": page.next_cursor,
        "page_size": page_size,
        "cutoff_exclusive": _iso_z(cutoff),
        "retrieved_at": _iso_z(retrieved_at),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
    }


_RAW_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "page_index",
        "request_pair",
        "response_pair",
        "request_cursor",
        "response_cursor",
        "page_size",
        "cutoff_exclusive",
        "retrieved_at",
        "raw_sha256",
        "raw_base64",
    }
)


def _decode_raw_record(
    value: object,
    *,
    expected_index: int,
    expected_cursor: str,
    cutoff: datetime,
    page_size: int,
) -> tuple[ParsedTradesPage, bytes, datetime]:
    record = _mapping(value, field="raw archive record")
    if set(record) != _RAW_RECORD_FIELDS:
        raise HistoricalDataError("raw archive record schema does not match")
    if record["schema_version"] != RAW_PAGE_SCHEMA:
        raise HistoricalDataError("raw archive schema version does not match")
    if record["page_index"] != expected_index:
        raise HistoricalDataError("raw archive page index is not contiguous")
    if record["request_pair"] != REQUEST_PAIR:
        raise HistoricalDataError("raw archive request pair does not match")
    if record["request_cursor"] != expected_cursor:
        raise HistoricalDataError("raw archive cursor chain does not match")
    if record["page_size"] != page_size:
        raise HistoricalDataError("raw archive page size does not match")
    if record["cutoff_exclusive"] != _iso_z(cutoff):
        raise HistoricalDataError("raw archive cutoff does not match")
    raw_base64 = record["raw_base64"]
    if not isinstance(raw_base64, str):
        raise HistoricalDataError("raw archive payload must be base64 text")
    try:
        raw = base64.b64decode(raw_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HistoricalDataError("raw archive payload is invalid base64") from exc
    raw_sha256 = record["raw_sha256"]
    if not isinstance(raw_sha256, str) or hashlib.sha256(raw).hexdigest() != raw_sha256:
        raise HistoricalDataError("raw archive payload hash does not match")
    page = parse_trades_bytes(raw, request_cursor=expected_cursor, page_size=page_size)
    if record["response_pair"] != page.pair or record["response_cursor"] != page.next_cursor:
        raise HistoricalDataError("raw archive metadata does not match its payload")
    retrieved_at = _parse_iso_z(record["retrieved_at"], field="retrieved_at")
    return page, raw, retrieved_at


def _read_record(line: bytes, *, line_number: int) -> object:
    if not line.endswith(b"\n"):
        raise HistoricalDataError("raw archive has a truncated final record")
    try:
        return json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalDataError(f"raw archive line {line_number} is invalid JSON") from exc


def _new_page_trades(
    page: ParsedTradesPage,
    previous_trade: HistoricalTrade | None,
) -> tuple[HistoricalTrade, ...]:
    """Return new executions after validating Kraken's inclusive page boundary.

    Kraken's public ``Trades`` endpoint repeats the trade addressed by ``since``
    as the first row of the next response.  The overlap is accepted only when
    every field is identical; it is then removed from counts and normalization.
    Any other backward or repeated boundary is evidence corruption.
    """

    if previous_trade is None or not page.trades:
        return page.trades
    first = page.trades[0]
    if first == previous_trade:
        return page.trades[1:]
    if any(trade.trade_id == previous_trade.trade_id for trade in page.trades):
        raise HistoricalDataError("Trades page contains a conflicting boundary trade")
    if first.timestamp_ns < previous_trade.timestamp_ns:
        raise HistoricalDataError("Trades page moved backward across its boundary")
    return page.trades


@dataclass(frozen=True, slots=True)
class _StoredState:
    page_count: int
    next_cursor: str
    included_trade_count: int
    complete: bool
    archive_prefix_sha256: str


def _load_state(path: Path, *, cutoff: datetime, page_size: int) -> _StoredState | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("historical download state is unreadable") from exc
    state = _mapping(value, field="state")
    expected = {
        "schema_version",
        "request_pair",
        "cutoff_exclusive",
        "page_size",
        "page_count",
        "next_cursor",
        "included_trade_count",
        "complete",
        "archive_prefix_sha256",
    }
    if set(state) != expected:
        raise HistoricalDataError("historical download state schema does not match")
    if (
        state["schema_version"] != STATE_SCHEMA
        or state["request_pair"] != REQUEST_PAIR
        or state["cutoff_exclusive"] != _iso_z(cutoff)
        or state["page_size"] != page_size
    ):
        raise HistoricalDataError("historical download state binding does not match")
    page_count = state["page_count"]
    included = state["included_trade_count"]
    complete = state["complete"]
    digest = state["archive_prefix_sha256"]
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or isinstance(included, bool)
        or not isinstance(included, int)
        or included < 0
        or not isinstance(complete, bool)
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise HistoricalDataError("historical download state values are invalid")
    return _StoredState(
        page_count=page_count,
        next_cursor=_cursor(state["next_cursor"], field="state cursor"),
        included_trade_count=included,
        complete=complete,
        archive_prefix_sha256=digest,
    )


def _state_bytes(archive: TradeArchive) -> bytes:
    return (
        _canonical_json_bytes(
            {
                "schema_version": STATE_SCHEMA,
                "request_pair": REQUEST_PAIR,
                "cutoff_exclusive": _iso_z(archive.cutoff),
                "page_size": archive.page_size,
                "page_count": archive.page_count,
                "next_cursor": archive.final_cursor,
                "included_trade_count": archive.included_trade_count,
                "complete": archive.complete,
                "archive_prefix_sha256": archive.raw_sha256,
            }
        )
        + b"\n"
    )


def _inspect_archive(
    archive_path: Path,
    state_path: Path,
    *,
    cutoff: datetime,
    page_size: int,
) -> TradeArchive:
    stored = _load_state(state_path, cutoff=cutoff, page_size=page_size)
    if not archive_path.exists():
        if stored is not None:
            raise HistoricalDataError("historical state exists without its raw archive")
        empty_hash = hashlib.sha256(b"").hexdigest()
        return TradeArchive(
            archive_path=archive_path,
            state_path=state_path,
            cutoff=cutoff,
            page_size=page_size,
            page_count=0,
            final_cursor="0",
            included_trade_count=0,
            complete=False,
            raw_sha256=empty_hash,
            first_retrieved_at=None,
            last_retrieved_at=None,
            last_trade=None,
        )

    digest = hashlib.sha256()
    cursor = "0"
    page_count = 0
    included = 0
    complete = False
    first_retrieved: datetime | None = None
    last_retrieved: datetime | None = None
    previous_trade: HistoricalTrade | None = None
    stored_prefix: tuple[str, str, int, bool] | None = None
    cutoff_ns = _datetime_ns(cutoff)
    with archive_path.open("rb") as source:
        for line_number, line in enumerate(source, start=1):
            if complete:
                raise HistoricalDataError("raw archive contains pages after its fixed cutoff")
            digest.update(line)
            record = _read_record(line, line_number=line_number)
            page, _raw, retrieved_at = _decode_raw_record(
                record,
                expected_index=page_count,
                expected_cursor=cursor,
                cutoff=cutoff,
                page_size=page_size,
            )
            new_trades = _new_page_trades(page, previous_trade)
            if new_trades:
                previous_trade = new_trades[-1]
            if last_retrieved is not None and retrieved_at < last_retrieved:
                raise HistoricalDataError("raw archive retrieval times moved backward")
            first_retrieved = first_retrieved or retrieved_at
            last_retrieved = retrieved_at
            included += sum(trade.timestamp_ns < cutoff_ns for trade in new_trades)
            cursor = page.next_cursor
            page_count += 1
            complete = int(cursor) >= cutoff_ns or any(
                trade.timestamp_ns >= cutoff_ns for trade in page.trades
            )
            if stored is not None and page_count == stored.page_count:
                stored_prefix = (digest.hexdigest(), cursor, included, complete)

    if stored is not None:
        if stored.page_count == 0:
            stored_prefix = (hashlib.sha256(b"").hexdigest(), "0", 0, False)
        if stored_prefix is None:
            raise HistoricalDataError("historical state is ahead of its raw archive")
        if stored_prefix != (
            stored.archive_prefix_sha256,
            stored.next_cursor,
            stored.included_trade_count,
            stored.complete,
        ):
            raise HistoricalDataError("historical state does not match its raw archive prefix")
    return TradeArchive(
        archive_path=archive_path,
        state_path=state_path,
        cutoff=cutoff,
        page_size=page_size,
        page_count=page_count,
        final_cursor=cursor,
        included_trade_count=included,
        complete=complete,
        raw_sha256=digest.hexdigest(),
        first_retrieved_at=first_retrieved,
        last_retrieved_at=last_retrieved,
        last_trade=previous_trade,
    )


def _is_retryable_api_error(exc: KrakenTradesApiError) -> bool:
    return exc.retryable


def download_btc_cad_trades(
    *,
    directory: Path,
    cutoff: datetime,
    page_size: int = MAX_PAGE_SIZE,
    pace_seconds: float = 1.0,
    timeout_seconds: float = 20.0,
    max_attempts: int = 3,
    transport: Transport | None = None,
    sleeper: Sleeper = time.sleep,
    clock: Clock | None = None,
    max_pages: int | None = None,
    progress: Progress | None = None,
) -> TradeArchive:
    """Download and durably archive BTC/CAD trades through ``cutoff``.

    ``cutoff`` is exclusive and must be UTC midnight so every normalized daily
    row is complete.  All request attempts after the first are separated by at
    least ``pace_seconds``; callers may choose a slower value but never less
    than Kraken's documented safe public cadence of one request per second.
    """

    cutoff = _utc_midnight(cutoff, field="cutoff")
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise TypeError("page_size must be an integer")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 1000")
    if isinstance(pace_seconds, bool) or pace_seconds < 1.0:
        raise ValueError("pace_seconds must be at least 1.0")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts must be an integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if max_pages is not None and (
        isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1
    ):
        raise ValueError("max_pages must be a positive integer when supplied")
    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory / RAW_ARCHIVE_FILENAME
    state_path = directory / STATE_FILENAME
    archive = _inspect_archive(
        archive_path,
        state_path,
        cutoff=cutoff,
        page_size=page_size,
    )
    if archive.complete:
        _write_atomic(state_path, _state_bytes(archive))
        return archive

    actual_transport = transport or _default_transport
    actual_clock = clock or (lambda: datetime.now(UTC))
    request_count = 0
    pages_this_run = 0
    running_digest = hashlib.sha256()
    if archive_path.exists():
        with archive_path.open("rb") as existing_archive:
            while existing_chunk := existing_archive.read(1024 * 1024):
                running_digest.update(existing_chunk)
    cutoff_ns = _datetime_ns(cutoff)
    while True:
        query = urlencode({"pair": REQUEST_PAIR, "since": archive.final_cursor, "count": page_size})
        url = f"{KRAKEN_TRADES_URL}?{query}"
        raw: bytes | None = None
        page: ParsedTradesPage | None = None
        for attempt in range(max_attempts):
            if request_count:
                sleeper(pace_seconds)
            request_count += 1
            try:
                raw = actual_transport(url, timeout_seconds)
                page = parse_trades_bytes(
                    raw,
                    request_cursor=archive.final_cursor,
                    page_size=page_size,
                )
                break
            except KrakenTradesApiError as exc:
                if not _is_retryable_api_error(exc) or attempt + 1 == max_attempts:
                    raise
            except RetryableHistoricalDataError:
                if attempt + 1 == max_attempts:
                    raise
        if raw is None or page is None:  # pragma: no cover - loop invariants above prove this
            raise AssertionError("request retry loop completed without a result")
        retrieved_at = actual_clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() != timedelta(0):
            raise HistoricalDataError("clock must return a timezone-aware UTC datetime")
        new_trades = _new_page_trades(page, archive.last_trade)
        line = _append_raw_record(
            archive_path,
            _page_record(
                page_index=archive.page_count,
                request_cursor=archive.final_cursor,
                page=page,
                raw=raw,
                cutoff=cutoff,
                page_size=page_size,
                retrieved_at=retrieved_at.astimezone(UTC),
            ),
        )
        running_digest.update(line)
        included = sum(trade.timestamp_ns < cutoff_ns for trade in new_trades)
        complete = int(page.next_cursor) >= cutoff_ns or any(
            trade.timestamp_ns >= cutoff_ns for trade in page.trades
        )
        archive = replace(
            archive,
            page_count=archive.page_count + 1,
            final_cursor=page.next_cursor,
            included_trade_count=archive.included_trade_count + included,
            complete=complete,
            raw_sha256=running_digest.hexdigest(),
            first_retrieved_at=archive.first_retrieved_at or retrieved_at.astimezone(UTC),
            last_retrieved_at=retrieved_at.astimezone(UTC),
            last_trade=new_trades[-1] if new_trades else archive.last_trade,
        )
        _write_atomic(state_path, _state_bytes(archive))
        pages_this_run += 1
        if progress is not None:
            progress(archive)
        if archive.complete:
            return archive
        if max_pages is not None and pages_this_run >= max_pages:
            return archive


def _iter_archive_trades(archive: TradeArchive) -> Iterator[HistoricalTrade]:
    verified = _inspect_archive(
        archive.archive_path,
        archive.state_path,
        cutoff=archive.cutoff,
        page_size=archive.page_size,
    )
    if not verified.complete or verified.raw_sha256 != archive.raw_sha256:
        raise HistoricalDataError("raw archive is incomplete or changed")
    cursor = "0"
    cutoff_ns = _datetime_ns(archive.cutoff)
    previous_trade: HistoricalTrade | None = None
    with archive.archive_path.open("rb") as source:
        for page_index, line in enumerate(source):
            page, _raw, _retrieved_at = _decode_raw_record(
                _read_record(line, line_number=page_index + 1),
                expected_index=page_index,
                expected_cursor=cursor,
                cutoff=archive.cutoff,
                page_size=archive.page_size,
            )
            new_trades = _new_page_trades(page, previous_trade)
            for trade in new_trades:
                if trade.timestamp_ns < cutoff_ns:
                    yield trade
            if new_trades:
                previous_trade = new_trades[-1]
            cursor = page.next_cursor


@dataclass(slots=True)
class _DailyAccumulator:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    execution_minute: datetime | None = None
    execution_notional: Decimal = Decimal("0")
    execution_volume: Decimal = Decimal("0")
    execution_trade_count: int = 0

    @classmethod
    def start(cls, trade: HistoricalTrade) -> _DailyAccumulator:
        opened_at = trade.opened_at
        accumulator = cls(
            day=opened_at.date(),
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.volume,
            trade_count=1,
        )
        accumulator._observe_execution(trade)
        return accumulator

    def add(self, trade: HistoricalTrade) -> None:
        if trade.opened_at.date() != self.day:
            raise HistoricalDataError("trade was added to the wrong UTC day")
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += trade.volume
        self.trade_count += 1
        self._observe_execution(trade)

    def _observe_execution(self, trade: HistoricalTrade) -> None:
        opened_at = trade.opened_at
        minute = opened_at.replace(second=0, microsecond=0)
        if minute.hour != 0 or not (
            EXECUTION_WINDOW_START_MINUTE <= minute.minute < EXECUTION_WINDOW_END_MINUTE
        ):
            return
        if self.execution_minute is None:
            self.execution_minute = minute
        if minute == self.execution_minute:
            self.execution_notional += trade.price * trade.volume
            self.execution_volume += trade.volume
            self.execution_trade_count += 1

    def finish(self) -> DailyTradeBar:
        execution_vwap: Decimal | None = None
        if self.execution_minute is not None:
            if self.execution_volume <= 0:
                raise HistoricalDataError("execution minute volume must be positive")
            with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
                execution_vwap = self.execution_notional / self.execution_volume
        return DailyTradeBar(
            day=self.day,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            trade_count=self.trade_count,
            execution_minute=self.execution_minute,
            execution_vwap=execution_vwap,
            execution_volume=(self.execution_volume if self.execution_minute else None),
            execution_trade_count=(self.execution_trade_count if self.execution_minute else None),
        )


def iter_daily_bars(archive: TradeArchive) -> Iterator[DailyTradeBar]:
    """Yield only observed UTC days; missing days remain absent."""

    accumulator: _DailyAccumulator | None = None
    for trade in _iter_archive_trades(archive):
        if accumulator is None:
            accumulator = _DailyAccumulator.start(trade)
        elif trade.opened_at.date() == accumulator.day:
            accumulator.add(trade)
        else:
            yield accumulator.finish()
            accumulator = _DailyAccumulator.start(trade)
    if accumulator is not None:
        yield accumulator.finish()


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _gap_dates(previous: date, current: date) -> Iterator[date]:
    candidate = previous + timedelta(days=1)
    while candidate < current:
        yield candidate
        candidate += timedelta(days=1)


def _manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return _canonical_json_bytes(manifest) + b"\n"


def write_normalized_dataset(
    archive: TradeArchive,
    *,
    csv_path: Path,
    manifest_path: Path,
) -> NormalizedDataset:
    """Write deterministic daily CSV and a provenance manifest for ``archive``."""

    resolved_targets = {
        csv_path.resolve(),
        manifest_path.resolve(),
        archive.archive_path.resolve(),
        archive.state_path.resolve(),
    }
    if len(resolved_targets) != 4:
        raise ValueError("normalized outputs must not overlap each other or raw evidence")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    gap_dates: list[date] = []
    row_count = 0
    first_date: date | None = None
    last_date: date | None = None
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=csv_path.parent,
        prefix=f".{csv_path.name}.",
        delete=False,
    ) as target:
        temporary_csv = Path(target.name)
        try:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(
                (
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume_btc",
                    "trade_count",
                    "execution_minute_utc",
                    "execution_vwap_cad",
                    "execution_volume_btc",
                    "execution_trade_count",
                )
            )
            for bar in iter_daily_bars(archive):
                if last_date is not None:
                    gap_dates.extend(_gap_dates(last_date, bar.day))
                first_date = first_date or bar.day
                last_date = bar.day
                writer.writerow(
                    (
                        bar.day.isoformat(),
                        _decimal_text(bar.open),
                        _decimal_text(bar.high),
                        _decimal_text(bar.low),
                        _decimal_text(bar.close),
                        _decimal_text(bar.volume),
                        str(bar.trade_count),
                        _iso_z(bar.execution_minute) if bar.execution_minute else "",
                        _decimal_text(bar.execution_vwap) if bar.execution_vwap else "",
                        _decimal_text(bar.execution_volume) if bar.execution_volume else "",
                        str(bar.execution_trade_count) if bar.execution_trade_count else "",
                    )
                )
                row_count += 1
            target.flush()
            os.fsync(target.fileno())
            if row_count == 0 or first_date is None or last_date is None:
                raise HistoricalDataError("raw archive contains no trades before its cutoff")
            csv_sha256 = _sha256_file(temporary_csv)
            os.replace(temporary_csv, csv_path)
        except BaseException:
            temporary_csv.unlink(missing_ok=True)
            raise

    manifest: dict[str, object] = {
        "schema_version": DATASET_SCHEMA,
        "source": {
            "provider": "Kraken",
            "endpoint": KRAKEN_TRADES_URL,
            "request_pair": REQUEST_PAIR,
            "start_cursor": "0",
            "cutoff_exclusive": _iso_z(archive.cutoff),
            "page_size": archive.page_size,
        },
        "raw_archive": {
            "filename": archive.archive_path.name,
            "sha256": archive.raw_sha256,
            "complete": archive.complete,
            "page_count": archive.page_count,
            "final_cursor": archive.final_cursor,
            "included_trade_count": archive.included_trade_count,
            "first_retrieved_at": (
                _iso_z(archive.first_retrieved_at) if archive.first_retrieved_at else None
            ),
            "last_retrieved_at": (
                _iso_z(archive.last_retrieved_at) if archive.last_retrieved_at else None
            ),
        },
        "normalized_csv": {
            "filename": csv_path.name,
            "sha256": csv_sha256,
            "columns": [
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume_btc",
                "trade_count",
                "execution_minute_utc",
                "execution_vwap_cad",
                "execution_volume_btc",
                "execution_trade_count",
            ],
            "row_count": row_count,
            "first_date": first_date.isoformat(),
            "last_date": last_date.isoformat(),
            "gap_count": len(gap_dates),
            "gap_dates": [day.isoformat() for day in gap_dates],
            "gap_policy": "preserve_missing_days_without_forward_fill",
            "execution_reference": {
                "window": "[00:15,00:20) UTC",
                "selection": "first_positive_volume_minute",
                "price": "trade_volume_weighted_vwap",
                "interval_timestamp": "minute_open",
                "available_at": "minute_close",
                "missing_policy": "blank",
            },
        },
    }
    manifest_content = _manifest_bytes(manifest)
    _write_atomic(manifest_path, manifest_content)
    return NormalizedDataset(
        csv_path=csv_path,
        manifest_path=manifest_path,
        csv_sha256=csv_sha256,
        manifest_sha256=hashlib.sha256(manifest_content).hexdigest(),
        row_count=row_count,
        first_date=first_date,
        last_date=last_date,
        gap_dates=tuple(gap_dates),
    )
