"""Strict importer for Kraken's official downloadable BTC/CAD trades CSV.

Kraken's downloadable Time-and-Sales member contains headerless rows in the
order ``timestamp,price,volume``.  This module treats that extracted member as
immutable evidence: its content identity is verified before parsing, rows must
be chronological and strictly before a fixed UTC cutoff, and no missing day is
invented.  The normalized output has the same daily schema as the public API
backfill, including a causal execution reference from the first positive-volume
minute in ``[00:15, 00:20) UTC``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlparse

from kraken_knight.historical_data import DATASET_SCHEMA, DailyTradeBar, NormalizedDataset

KRAKEN_TIME_AND_SALES_DOCUMENTATION_URL = (
    "https://support.kraken.com/articles/"
    "360047543791-downloadable-historical-market-data-time-and-sales-"
)
ARCHIVE_SOURCE_METHOD = "official_downloadable_time_and_sales_csv"
REQUEST_PAIR = "XBTCAD"
EXECUTION_WINDOW_START_MINUTE = 15
EXECUTION_WINDOW_END_MINUTE = 20
NORMALIZED_COLUMNS = (
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

_UNSIGNED_INTEGER = re.compile(r"[0-9]+\Z")
_DECIMAL_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class KrakenArchiveCsvError(RuntimeError):
    """Raised when the official archive member or its metadata is inconsistent."""


@dataclass(frozen=True, slots=True)
class KrakenArchiveSource:
    """Pinned identity and provenance supplied with an extracted archive member."""

    archive_url: str
    archive_file_id: str
    entry_name: str
    expected_csv_sha256: str
    zip_member_crc32: int | None = None
    zip_compressed_size_bytes: int | None = None
    zip_uncompressed_size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    sha256: str
    crc32: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ArchiveTrade:
    timestamp: int
    opened_at: datetime
    price: Decimal
    volume: Decimal


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
    def start(cls, trade: _ArchiveTrade) -> _DailyAccumulator:
        accumulator = cls(
            day=trade.opened_at.date(),
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.volume,
            trade_count=1,
        )
        accumulator._observe_execution(trade)
        return accumulator

    def add(self, trade: _ArchiveTrade) -> None:
        if trade.opened_at.date() != self.day:
            raise KrakenArchiveCsvError("trade was added to the wrong UTC day")
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            self.volume += trade.volume
        self.trade_count += 1
        self._observe_execution(trade)

    def _observe_execution(self, trade: _ArchiveTrade) -> None:
        minute = trade.opened_at.replace(second=0, microsecond=0)
        if minute.hour != 0 or not (
            EXECUTION_WINDOW_START_MINUTE <= minute.minute < EXECUTION_WINDOW_END_MINUTE
        ):
            return
        if self.execution_minute is None:
            self.execution_minute = minute
        if minute == self.execution_minute:
            with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
                self.execution_notional += trade.price * trade.volume
                self.execution_volume += trade.volume
            self.execution_trade_count += 1

    def finish(self) -> DailyTradeBar:
        execution_vwap: Decimal | None = None
        if self.execution_minute is not None:
            if self.execution_volume <= 0:  # pragma: no cover - rows require positive volume
                raise KrakenArchiveCsvError("execution minute volume must be positive")
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


def _validate_cutoff(cutoff: datetime) -> datetime:
    if cutoff.tzinfo is None or cutoff.utcoffset() != timedelta(0):
        raise ValueError("cutoff must be timezone-aware UTC")
    normalized = cutoff.astimezone(UTC)
    if normalized.time() != time.min:
        raise ValueError("cutoff must be UTC midnight")
    return normalized


def _validate_source(source: KrakenArchiveSource) -> None:
    parsed_url = urlparse(source.archive_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("archive_url must be an absolute HTTPS URL")
    if not source.archive_file_id or source.archive_file_id.strip() != source.archive_file_id:
        raise ValueError("archive_file_id must be non-empty without surrounding whitespace")
    if any(character.isspace() for character in source.archive_file_id):
        raise ValueError("archive_file_id must not contain whitespace")
    entry = PurePosixPath(source.entry_name)
    if (
        not source.entry_name
        or entry.is_absolute()
        or ".." in entry.parts
        or entry.name.upper() != f"{REQUEST_PAIR}.CSV"
    ):
        raise ValueError("entry_name must be a safe archive path ending in XBTCAD.csv")
    if _SHA256.fullmatch(source.expected_csv_sha256) is None:
        raise ValueError("expected_csv_sha256 must be 64 hexadecimal characters")
    for field, value in (
        ("zip_member_crc32", source.zip_member_crc32),
        ("zip_compressed_size_bytes", source.zip_compressed_size_bytes),
        ("zip_uncompressed_size_bytes", source.zip_uncompressed_size_bytes),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{field} must be non-negative")
    if source.zip_member_crc32 is not None and source.zip_member_crc32 > 0xFFFFFFFF:
        raise ValueError("zip_member_crc32 must fit in 32 bits")


def _file_identity(path: Path) -> _FileIdentity:
    digest = hashlib.sha256()
    checksum = 0
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                checksum = zlib.crc32(chunk, checksum)
                size += len(chunk)
    except OSError as exc:
        raise KrakenArchiveCsvError("source CSV is not readable") from exc
    return _FileIdentity(sha256=digest.hexdigest(), crc32=checksum, size_bytes=size)


def _verify_file_identity(
    identity: _FileIdentity,
    source: KrakenArchiveSource,
) -> None:
    if identity.sha256 != source.expected_csv_sha256.lower():
        raise KrakenArchiveCsvError("source CSV SHA-256 does not match the pinned archive member")
    if source.zip_member_crc32 is not None and identity.crc32 != source.zip_member_crc32:
        raise KrakenArchiveCsvError("source CSV CRC32 does not match the ZIP central directory")
    if (
        source.zip_uncompressed_size_bytes is not None
        and identity.size_bytes != source.zip_uncompressed_size_bytes
    ):
        raise KrakenArchiveCsvError("source CSV size does not match the ZIP central directory")


def _parse_decimal(value: str, *, field: str, line_number: int) -> Decimal:
    if _DECIMAL_NUMBER.fullmatch(value) is None:
        raise KrakenArchiveCsvError(f"source CSV line {line_number} {field} is not a decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the expression
        raise KrakenArchiveCsvError(f"source CSV line {line_number} {field} is invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise KrakenArchiveCsvError(
            f"source CSV line {line_number} {field} must be finite and positive"
        )
    return parsed


def _iter_trades(source_csv: Path, *, cutoff: datetime) -> Iterator[_ArchiveTrade]:
    cutoff_timestamp = int(cutoff.timestamp())
    previous_timestamp: int | None = None
    try:
        raw_source = source_csv.open(encoding="utf-8", newline="")
    except OSError as exc:
        raise KrakenArchiveCsvError("source CSV is not readable") from exc
    with raw_source:
        reader = csv.reader(raw_source, strict=True)
        try:
            for line_number, row in enumerate(reader, start=1):
                if len(row) != 3:
                    raise KrakenArchiveCsvError(
                        f"source CSV line {line_number} must contain exactly three fields"
                    )
                timestamp_text, price_text, volume_text = row
                if _UNSIGNED_INTEGER.fullmatch(timestamp_text) is None:
                    raise KrakenArchiveCsvError(
                        f"source CSV line {line_number} timestamp must be integer UTC seconds"
                    )
                timestamp = int(timestamp_text)
                if timestamp >= cutoff_timestamp:
                    raise KrakenArchiveCsvError(
                        f"source CSV line {line_number} is at or after the exclusive cutoff"
                    )
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    raise KrakenArchiveCsvError(
                        f"source CSV line {line_number} timestamp moved backward"
                    )
                price = _parse_decimal(price_text, field="price", line_number=line_number)
                volume = _parse_decimal(volume_text, field="volume", line_number=line_number)
                opened_at = datetime.fromtimestamp(timestamp, tz=UTC)
                yield _ArchiveTrade(
                    timestamp=timestamp,
                    opened_at=opened_at,
                    price=price,
                    volume=volume,
                )
                previous_timestamp = timestamp
        except (csv.Error, UnicodeError, OSError) as exc:
            raise KrakenArchiveCsvError("source CSV is not valid RFC 4180 data") from exc


def iter_archive_daily_bars(
    source_csv: Path,
    *,
    cutoff: datetime,
) -> Iterator[DailyTradeBar]:
    """Yield observed daily bars from a verified-format extracted archive member.

    This lower-level iterator validates the row schema, ordering, values, and
    cutoff.  :func:`import_kraken_time_and_sales_csv` additionally pins the
    member's SHA-256/CRC/size before calling it.
    """

    normalized_cutoff = _validate_cutoff(cutoff)
    accumulator: _DailyAccumulator | None = None
    for trade in _iter_trades(source_csv, cutoff=normalized_cutoff):
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


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _gap_dates(previous: date, current: date) -> Iterator[date]:
    candidate = previous + timedelta(days=1)
    while candidate < current:
        yield candidate
        candidate += timedelta(days=1)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_atomic(path: Path, content: bytes) -> None:
    with NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as target:
        temporary = Path(target.name)
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def import_kraken_time_and_sales_csv(
    source_csv: Path,
    *,
    cutoff: datetime,
    source: KrakenArchiveSource,
    normalized_csv_path: Path,
    manifest_path: Path,
) -> NormalizedDataset:
    """Verify and normalize an extracted official Kraken XBTCAD archive member."""

    normalized_cutoff = _validate_cutoff(cutoff)
    _validate_source(source)
    resolved_paths = {
        source_csv.resolve(),
        normalized_csv_path.resolve(),
        manifest_path.resolve(),
    }
    if len(resolved_paths) != 3:
        raise ValueError("source and output paths must not overlap")

    identity = _file_identity(source_csv)
    _verify_file_identity(identity, source)
    normalized_csv_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    row_count = 0
    source_row_count = 0
    first_date: date | None = None
    last_date: date | None = None
    gap_dates: list[date] = []
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=normalized_csv_path.parent,
        prefix=f".{normalized_csv_path.name}.",
        delete=False,
    ) as target:
        temporary_csv = Path(target.name)
        try:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(NORMALIZED_COLUMNS)
            for bar in iter_archive_daily_bars(source_csv, cutoff=normalized_cutoff):
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
                source_row_count += bar.trade_count
            target.flush()
            os.fsync(target.fileno())
            if row_count == 0 or first_date is None or last_date is None:
                raise KrakenArchiveCsvError("source CSV contains no trades before its cutoff")
            expected_last_date = normalized_cutoff.date() - timedelta(days=1)
            if last_date != expected_last_date:
                raise KrakenArchiveCsvError(
                    "source CSV does not reach the final UTC day before the cutoff"
                )
            normalized_sha256 = hashlib.sha256(temporary_csv.read_bytes()).hexdigest()
            os.replace(temporary_csv, normalized_csv_path)
        except BaseException:
            temporary_csv.unlink(missing_ok=True)
            raise

    manifest: dict[str, Any] = {
        "schema_version": DATASET_SCHEMA,
        "source": {
            "provider": "Kraken",
            "method": ARCHIVE_SOURCE_METHOD,
            "documentation_url": KRAKEN_TIME_AND_SALES_DOCUMENTATION_URL,
            "archive_url": source.archive_url,
            "archive_file_id": source.archive_file_id,
            "archive_entry_name": source.entry_name,
            "request_pair": REQUEST_PAIR,
            "cutoff_exclusive": _iso_z(normalized_cutoff),
        },
        "raw_archive": {
            "filename": source_csv.name,
            "entry_name": source.entry_name,
            "sha256": identity.sha256,
            "crc32": f"{identity.crc32:08x}",
            "size_bytes": identity.size_bytes,
            "zip_compressed_size_bytes": source.zip_compressed_size_bytes,
            "zip_uncompressed_size_bytes": source.zip_uncompressed_size_bytes,
            "row_count": source_row_count,
            "included_trade_count": source_row_count,
            "cutoff_exclusive": _iso_z(normalized_cutoff),
            "complete": True,
            "completeness_basis": (
                "all_rows_strictly_before_cutoff_and_last_observed_utc_day_is_cutoff_minus_one"
            ),
        },
        "normalized_csv": {
            "filename": normalized_csv_path.name,
            "sha256": normalized_sha256,
            "columns": list(NORMALIZED_COLUMNS),
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
    manifest_content = _canonical_json_bytes(manifest)
    _write_atomic(manifest_path, manifest_content)
    return NormalizedDataset(
        csv_path=normalized_csv_path,
        manifest_path=manifest_path,
        csv_sha256=normalized_sha256,
        manifest_sha256=hashlib.sha256(manifest_content).hexdigest(),
        row_count=row_count,
        first_date=first_date,
        last_date=last_date,
        gap_dates=tuple(gap_dates),
    )
