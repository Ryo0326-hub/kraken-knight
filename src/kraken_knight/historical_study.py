"""Deterministic orchestration for the pre-registered BTC/CAD price study.

This module is intentionally separate from the live-trading entry point.  It
turns normalized public Kraken trade bars into a causal replay and an immutable
research bundle; it never accepts exchange credentials or places orders.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from itertools import pairwise, product
from pathlib import Path
from typing import Any, cast

from .backtest import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    ExecutionCosts,
    ExecutionReference,
    InstrumentRules,
    Liquidity,
    RiskEvent,
    Trade,
    run_backtest,
)
from .domain import Candle
from .historical_data import (
    DATASET_SCHEMA,
    KRAKEN_TRADES_URL,
    REQUEST_PAIR,
    DailyTradeBar,
)
from .research_charts import (
    ChartPoint,
    ChartSeries,
    RobustnessCell,
    write_line_chart,
    write_robustness_heatmap,
)
from .research_metrics import (
    EquityObservation,
    ResearchMetrics,
    TradeCostAggregate,
    calculate_research_metrics,
    metrics_to_jsonable,
)
from .strategy import (
    DecisionReason,
    MomentumTrendStrategy,
    PositionState,
    StrategyDecision,
    StrategyPolicy,
)

STUDY_SCHEMA = "kraken-knight-price-study-v1"
WARMUP_DAYS = 250
INITIAL_CAD = Decimal("1000")
SELECTED_POLICY = (90, 200, 30)
COMPARATOR_ORDER = (
    "cad_cash",
    "fee_aware_btc_buy_and_hold",
    "close_above_sma200",
    "positive_90_day_momentum",
    "combined_momentum_and_sma200_unsized",
    "frozen_v1",
)
METRIC_COLUMNS = (
    "initial_equity_cad",
    "final_equity_cad",
    "net_profit_cad",
    "gross_profit_cad",
    "total_return",
    "cagr",
    "annualized_volatility",
    "sharpe",
    "downside_deviation",
    "sortino",
    "max_drawdown",
    "max_drawdown_duration_days",
    "calmar",
    "average_exposure",
    "turnover",
    "total_fees_cad",
    "total_slippage_cad",
    "trade_count",
)


class HistoricalStudyError(RuntimeError):
    """Raised when the research contract cannot be satisfied honestly."""


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    dirty: bool
    dirty_override_used: bool


@dataclass(frozen=True, slots=True)
class SplitBoundary:
    name: str
    first_day: date
    last_day: date
    observation_count: int


@dataclass(frozen=True, slots=True)
class CurveRun:
    name: str
    curve: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]
    result: BacktestResult | None
    synthetic_trade_costs: TradeCostAggregate | None = None


@dataclass(frozen=True, slots=True)
class BuyAndHoldEntryAttempt:
    """One causal accumulation attempt for the fee-aware BTC benchmark."""

    decision_time: datetime
    execution_time: datetime
    reference_price_cad: Decimal
    execution_price_cad: Decimal
    execution_volume_btc: Decimal
    maximum_participation_fraction: Decimal
    maximum_participating_quantity_btc: Decimal
    affordable_quantity_btc: Decimal
    executed_quantity_btc: Decimal
    volume_cap_applied: bool
    gross_notional_cad: Decimal
    fee_cad: Decimal
    slippage_cad: Decimal
    cash_after_cad: Decimal
    btc_after: Decimal
    outcome: str


@dataclass(frozen=True, slots=True)
class StudyResult:
    output_dir: Path
    summary_path: Path
    manifest_path: Path
    checksums_path: Path
    config_sha256: str
    input_data_sha256: str
    clean_data_sha256: str
    git_state: GitState
    selected_first_day: date
    selected_last_day: date
    selected_day_count: int
    discarded_day_count: int


@dataclass(frozen=True, slots=True)
class _LoadedConfig:
    document: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class _CostCase:
    name: str
    buy_fee: Decimal
    sell_fee: Decimal
    slippage: Decimal


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise HistoricalStudyError(f"{field} must be a decimal string or integer")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(cast(Any, value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HistoricalStudyError(f"{field} is not decimal-compatible") from exc
    if not parsed.is_finite():
        raise HistoricalStudyError(f"{field} must be finite")
    return parsed


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalStudyError(f"{field} must be a positive integer")
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalStudyError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise HistoricalStudyError(f"{field} must be an array")
    return value


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise HistoricalStudyError("artifact timestamps must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, _canonical_json_bytes(value))


def _git_state(repository_root: Path, *, allow_dirty: bool) -> GitState:
    try:
        commit_result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        status_result = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HistoricalStudyError("repository_root must be a readable Git worktree") from exc
    commit = commit_result.stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise HistoricalStudyError("Git HEAD did not resolve to a full lowercase commit hash")
    dirty = bool(status_result.stdout)
    if dirty and not allow_dirty:
        raise HistoricalStudyError(
            "refusing to run a final historical study from a dirty worktree; "
            "commit the frozen code/config first or pass allow_dirty=True for development only"
        )
    return GitState(commit=commit, dirty=dirty, dirty_override_used=dirty and allow_dirty)


def _validate_sha256(value: str, *, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise HistoricalStudyError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _validate_commit(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise HistoricalStudyError("expected_commit must be a full lowercase Git SHA-1")
    return value


def _default_code_identity_paths() -> tuple[Path, ...]:
    package = Path(__file__).resolve().parent
    return tuple(
        package / name
        for name in (
            "backtest.py",
            "domain.py",
            "historical_data.py",
            "historical_study.py",
            "kraken_archive_csv.py",
            "research_charts.py",
            "research_metrics.py",
            "strategy.py",
        )
    )


def _code_identity(
    repository_root: Path,
    *,
    paths: Sequence[Path],
    allow_dirty: bool,
) -> tuple[bool, dict[str, str]]:
    root = repository_root.resolve()
    identities: dict[str, str] = {}
    mismatches: list[str] = []
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            mismatches.append(f"outside_repository:{resolved.name}")
            continue
        relative = resolved.relative_to(root).as_posix()
        try:
            tracked = subprocess.run(
                ("git", "ls-files", "--error-unmatch", "--", relative),
                cwd=root,
                check=True,
                capture_output=True,
            )
            del tracked
            committed = subprocess.run(
                ("git", "show", f"HEAD:{relative}"),
                cwd=root,
                check=True,
                capture_output=True,
            ).stdout
            current = resolved.read_bytes()
        except (OSError, subprocess.CalledProcessError) as exc:
            if not allow_dirty:
                raise HistoricalStudyError(
                    f"research input is not a readable Git-tracked HEAD file: {relative}"
                ) from exc
            mismatches.append(f"untracked_or_unreadable:{relative}")
            continue
        identities[relative] = _sha256_bytes(current)
        if current != committed:
            mismatches.append(f"differs_from_head:{relative}")
    if mismatches and not allow_dirty:
        raise HistoricalStudyError(
            "research code/config identity does not match HEAD: " + ", ".join(mismatches)
        )
    return not mismatches, identities


def _load_config(path: Path) -> _LoadedConfig:
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalStudyError("pre-registration config must be readable JSON") from exc
    root = _mapping(document, field="config")
    if root.get("study_id") != "btc_cad_price_only_v1_preregistered":
        raise HistoricalStudyError("unexpected study_id in pre-registration config")
    if root.get("pair") != "BTC/CAD" or root.get("blockchair_included") is not False:
        raise HistoricalStudyError("this runner accepts only the price-only BTC/CAD study")
    dataset = _mapping(root.get("dataset"), field="dataset")
    if dataset.get("clean_history_rule") != (
        "longest_contiguous_utc_daily_sequence_then_earliest_on_tie"
    ):
        raise HistoricalStudyError("clean-history rule does not match the frozen protocol")
    if dataset.get("common_warmup_days") != WARMUP_DAYS:
        raise HistoricalStudyError("common warm-up must remain frozen at 250 days")
    source_method = dataset.get("source_method")
    if source_method == "official_downloadable_time_and_sales_csv":
        expected_text = {
            "provider": "Kraken",
            "documentation_url": (
                "https://support.kraken.com/articles/"
                "360047543791-downloadable-historical-market-data-time-and-sales-"
            ),
            "request_pair": REQUEST_PAIR,
        }
        if any(dataset.get(field) != value for field, value in expected_text.items()):
            raise HistoricalStudyError("official Kraken archive source identity is not frozen")
        for field in ("archive_url", "archive_file_id", "archive_entry_name"):
            value = dataset.get(field)
            if not isinstance(value, str) or not value:
                raise HistoricalStudyError(f"dataset {field} must be a non-empty string")
        raw_sha256 = dataset.get("raw_csv_sha256")
        if not isinstance(raw_sha256, str):
            raise HistoricalStudyError("dataset raw_csv_sha256 must be a string")
        _validate_sha256(raw_sha256, field="raw_csv_sha256")
        raw_crc32 = dataset.get("raw_csv_crc32")
        if (
            not isinstance(raw_crc32, str)
            or len(raw_crc32) != 8
            or any(character not in "0123456789abcdef" for character in raw_crc32)
        ):
            raise HistoricalStudyError("dataset raw_csv_crc32 must be lowercase CRC-32 hex")
        for field in (
            "raw_csv_size_bytes",
            "raw_csv_row_count",
            "zip_compressed_size_bytes",
            "zip_uncompressed_size_bytes",
        ):
            _positive_integer(dataset.get(field), field=field)
    elif source_method == "public_trades_api":
        if (
            dataset.get("provider") != "Kraken"
            or dataset.get("endpoint") != KRAKEN_TRADES_URL
            or dataset.get("request_pair") != REQUEST_PAIR
            or dataset.get("start_cursor") != "0"
        ):
            raise HistoricalStudyError("Kraken public Trades source identity is not frozen")
    else:
        raise HistoricalStudyError("dataset source_method is not supported")
    evaluation = _mapping(root.get("chronological_evaluation"), field="chronological_evaluation")
    if (
        evaluation.get("development_fraction"),
        evaluation.get("validation_fraction"),
        evaluation.get("frozen_holdout_fraction"),
    ) != ("0.60", "0.20", "0.20"):
        raise HistoricalStudyError("chronological split must remain frozen at 60/20/20")
    execution = _mapping(root.get("execution"), field="execution")
    if execution.get("buy_and_hold_entry") != (
        "accumulate_on_each_available_reference_until_cash_below_minimum_or_cutoff"
    ):
        raise HistoricalStudyError("buy-and-hold entry rule differs from the frozen protocol")
    strategy = _mapping(root.get("strategy"), field="strategy")
    if (
        strategy.get("momentum_days"),
        strategy.get("trend_days"),
        strategy.get("volatility_days"),
    ) != SELECTED_POLICY:
        raise HistoricalStudyError("selected strategy must remain frozen at 90/200/30")
    if tuple(root.get("comparators", ())) != COMPARATOR_ORDER:
        raise HistoricalStudyError("comparator set or ordering differs from the frozen protocol")
    return _LoadedConfig(document=root, sha256=_sha256_bytes(raw))


def _normalized_manifest_hash(
    path: Path,
    *,
    expected_data_sha256: str,
    expected_filename: str,
    expected_row_count: int,
    expected_first_date: date,
    expected_last_date: date,
    expected_dataset: Mapping[str, Any],
    expected_execution: Mapping[str, Any],
) -> str:
    try:
        raw = path.read_bytes()
        root = _mapping(json.loads(raw.decode("utf-8")), field="normalized manifest")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalStudyError("normalized provenance manifest is not readable JSON") from exc
    if root.get("schema_version") != DATASET_SCHEMA:
        raise HistoricalStudyError("normalized provenance manifest schema is not supported")
    source = _mapping(root.get("source"), field="source")
    raw_archive = _mapping(root.get("raw_archive"), field="raw_archive")
    expected_cutoff = expected_dataset.get("cutoff_exclusive_utc")
    source_method = expected_dataset.get("source_method")
    if source_method == "official_downloadable_time_and_sales_csv":
        archive_entry_name = expected_dataset.get("archive_entry_name")
        if not isinstance(archive_entry_name, str):
            raise HistoricalStudyError("frozen archive entry name is invalid")
        expected_source = {
            "provider": expected_dataset.get("provider"),
            "method": source_method,
            "documentation_url": expected_dataset.get("documentation_url"),
            "archive_url": expected_dataset.get("archive_url"),
            "archive_file_id": expected_dataset.get("archive_file_id"),
            "archive_entry_name": archive_entry_name,
            "request_pair": expected_dataset.get("request_pair"),
            "cutoff_exclusive": expected_cutoff,
        }
        if dict(source) != expected_source:
            raise HistoricalStudyError(
                "normalized provenance source does not match the frozen official Kraken archive"
            )
        raw_size = expected_dataset.get("raw_csv_size_bytes")
        raw_row_count = expected_dataset.get("raw_csv_row_count")
        expected_raw_archive = {
            "filename": archive_entry_name.rsplit("/", 1)[-1],
            "entry_name": archive_entry_name,
            "sha256": expected_dataset.get("raw_csv_sha256"),
            "crc32": expected_dataset.get("raw_csv_crc32"),
            "size_bytes": raw_size,
            "zip_compressed_size_bytes": expected_dataset.get("zip_compressed_size_bytes"),
            "zip_uncompressed_size_bytes": expected_dataset.get("zip_uncompressed_size_bytes"),
            "row_count": raw_row_count,
            "included_trade_count": raw_row_count,
            "cutoff_exclusive": expected_cutoff,
            "complete": True,
            "completeness_basis": (
                "all_rows_strictly_before_cutoff_and_last_observed_utc_day_is_cutoff_minus_one"
            ),
        }
        if dict(raw_archive) != expected_raw_archive:
            raise HistoricalStudyError(
                "normalized provenance raw archive does not match the frozen CSV identity"
            )
    elif source_method == "public_trades_api":
        if (
            source.get("provider") != expected_dataset.get("provider", "Kraken")
            or source.get("endpoint") != expected_dataset.get("endpoint", KRAKEN_TRADES_URL)
            or source.get("request_pair") != expected_dataset.get("request_pair", REQUEST_PAIR)
            or source.get("start_cursor") != expected_dataset.get("start_cursor", "0")
            or source.get("cutoff_exclusive") != expected_cutoff
        ):
            raise HistoricalStudyError(
                "normalized provenance source does not match frozen Kraken API data"
            )
        if raw_archive.get("complete") is not True:
            raise HistoricalStudyError("normalized provenance raw archive is not complete")
    else:
        raise HistoricalStudyError("frozen dataset source method is not supported")
    normalized = _mapping(root.get("normalized_csv"), field="normalized_csv")
    expected_fields = (
        normalized.get("filename") == expected_filename,
        normalized.get("sha256") == expected_data_sha256,
        normalized.get("row_count") == expected_row_count,
        normalized.get("first_date") == expected_first_date.isoformat(),
        normalized.get("last_date") == expected_last_date.isoformat(),
    )
    if not all(expected_fields):
        raise HistoricalStudyError(
            "normalized provenance manifest does not bind the supplied normalized CSV"
        )
    expected_reference = {
        "window": "[00:15,00:20) UTC",
        "selection": "first_positive_volume_minute",
        "price": "trade_volume_weighted_vwap",
        "interval_timestamp": "minute_open",
        "available_at": "minute_close",
        "missing_policy": "blank",
    }
    if (
        expected_execution.get("window_start_utc") != "00:15:00"
        or expected_execution.get("window_end_exclusive_utc") != "00:20:00"
        or expected_execution.get("reference") != "vwap_of_first_positive_volume_utc_minute"
        or expected_execution.get("reference_interval_timestamp") != "minute_open"
        or expected_execution.get("reference_available_at")
        != "minute_close_one_minute_after_interval_open"
        or dict(
            _mapping(
                normalized.get("execution_reference"),
                field="normalized_csv.execution_reference",
            )
        )
        != expected_reference
    ):
        raise HistoricalStudyError(
            "normalized provenance execution reference does not match the frozen causal clock"
        )
    return _sha256_bytes(raw)


def load_daily_bars(path: Path) -> tuple[DailyTradeBar, ...]:
    """Strictly read the normalized CSV emitted by :mod:`historical_data`."""

    expected = (
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
    try:
        source = path.open(encoding="utf-8", newline="")
    except OSError as exc:
        raise HistoricalStudyError("normalized daily CSV is not readable") from exc
    bars: list[DailyTradeBar] = []
    with source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != expected:
            raise HistoricalStudyError("normalized daily CSV columns do not match schema v1")
        for line_number, row in enumerate(reader, start=2):
            try:
                day = date.fromisoformat(row["date"])
                trade_count = int(row["trade_count"])
                execution_fields = (
                    row["execution_minute_utc"],
                    row["execution_vwap_cad"],
                    row["execution_volume_btc"],
                    row["execution_trade_count"],
                )
                if any(execution_fields) and not all(execution_fields):
                    raise ValueError("partial execution reference")
                execution_minute = (
                    datetime.fromisoformat(execution_fields[0].replace("Z", "+00:00"))
                    if execution_fields[0]
                    else None
                )
                bar = DailyTradeBar(
                    day=day,
                    open=_decimal(row["open"], field="open"),
                    high=_decimal(row["high"], field="high"),
                    low=_decimal(row["low"], field="low"),
                    close=_decimal(row["close"], field="close"),
                    volume=_decimal(row["volume_btc"], field="volume_btc"),
                    trade_count=trade_count,
                    execution_minute=execution_minute,
                    execution_vwap=(
                        _decimal(execution_fields[1], field="execution_vwap_cad")
                        if execution_fields[1]
                        else None
                    ),
                    execution_volume=(
                        _decimal(execution_fields[2], field="execution_volume_btc")
                        if execution_fields[2]
                        else None
                    ),
                    execution_trade_count=(
                        int(execution_fields[3]) if execution_fields[3] else None
                    ),
                )
            except (ValueError, KeyError) as exc:
                raise HistoricalStudyError(
                    f"normalized daily CSV contains an invalid row at line {line_number}"
                ) from exc
            bars.append(bar)
    return tuple(bars)


def _validate_bar(bar: DailyTradeBar) -> None:
    if not isinstance(bar, DailyTradeBar):
        raise TypeError("bars must contain DailyTradeBar instances")
    for name in ("open", "high", "low", "close", "volume"):
        value = _decimal(getattr(bar, name), field=name)
        if value <= 0:
            raise HistoricalStudyError(f"{name} must be positive")
    if (
        bar.low > bar.high
        or not bar.low <= bar.open <= bar.high
        or not bar.low <= bar.close <= bar.high
    ):
        raise HistoricalStudyError("daily bar violates OHLC bounds")
    _positive_integer(bar.trade_count, field="trade_count")
    execution_values = (
        bar.execution_minute,
        bar.execution_vwap,
        bar.execution_volume,
        bar.execution_trade_count,
    )
    if any(value is not None for value in execution_values) and not all(
        value is not None for value in execution_values
    ):
        raise HistoricalStudyError("execution evidence must be entirely present or entirely absent")
    if bar.execution_minute is not None:
        minute = bar.execution_minute
        if minute.tzinfo is None or minute.utcoffset() != timedelta(0):
            raise HistoricalStudyError("execution minute must be timezone-aware UTC")
        if minute.date() != bar.day or minute.hour != 0 or not 15 <= minute.minute < 20:
            raise HistoricalStudyError(
                "execution minute must be on its bar day in [00:15,00:20) UTC"
            )
        if minute.second or minute.microsecond:
            raise HistoricalStudyError("execution minute must be minute aligned")
        if cast(Decimal, bar.execution_vwap) <= 0 or cast(Decimal, bar.execution_volume) <= 0:
            raise HistoricalStudyError("execution price and volume must be positive")
        _positive_integer(bar.execution_trade_count, field="execution_trade_count")


def select_longest_contiguous_sequence(
    bars: Sequence[DailyTradeBar],
) -> tuple[DailyTradeBar, ...]:
    """Select the longest consecutive UTC-day run, choosing the earliest tie."""

    if not bars:
        raise HistoricalStudyError("at least one daily trade bar is required")
    clean = tuple(bars)
    for bar in clean:
        _validate_bar(bar)
    for left, right in pairwise(clean):
        if right.day <= left.day:
            raise HistoricalStudyError("daily bars must be strictly date ordered and unique")

    sequences: list[tuple[DailyTradeBar, ...]] = []
    start = 0
    for index in range(1, len(clean)):
        if clean[index].day - clean[index - 1].day != timedelta(days=1):
            sequences.append(clean[start:index])
            start = index
    sequences.append(clean[start:])
    # max keeps the first encountered item on equal keys, which is the earliest.
    return max(sequences, key=len)


def _bars_hash(bars: Sequence[DailyTradeBar]) -> str:
    rows = [
        {
            "date": bar.day.isoformat(),
            "open": _decimal_text(bar.open),
            "high": _decimal_text(bar.high),
            "low": _decimal_text(bar.low),
            "close": _decimal_text(bar.close),
            "volume_btc": _decimal_text(bar.volume),
            "trade_count": bar.trade_count,
            "execution_minute_utc": _iso_z(bar.execution_minute) if bar.execution_minute else None,
            "execution_vwap_cad": (
                _decimal_text(bar.execution_vwap) if bar.execution_vwap is not None else None
            ),
            "execution_volume_btc": (
                _decimal_text(bar.execution_volume) if bar.execution_volume is not None else None
            ),
            "execution_trade_count": bar.execution_trade_count,
        }
        for bar in bars
    ]
    return _sha256_bytes(_canonical_json_bytes(rows))


def bars_to_causal_inputs(
    bars: Sequence[DailyTradeBar],
) -> tuple[tuple[Candle, ...], tuple[ExecutionReference, ...]]:
    """Bind bar *d*'s completed close to bar *d+1*'s post-decision trades."""

    clean = tuple(bars)
    if not clean:
        raise HistoricalStudyError("at least one daily trade bar is required")
    candles = tuple(
        Candle(
            open_time=datetime.combine(bar.day, datetime.min.time(), tzinfo=UTC),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            complete=True,
        )
        for bar in clean
    )
    references: list[ExecutionReference] = []
    for index in range(1, len(clean)):
        execution_bar = clean[index]
        if execution_bar.execution_minute is None:
            continue
        references.append(
            ExecutionReference(
                decision_time=candles[index - 1].close_time + timedelta(minutes=15),
                # The minute label is the interval start. Its complete VWAP is
                # only causal at the exclusive end of [minute, minute + 1m).
                execution_time=execution_bar.execution_minute + timedelta(minutes=1),
                reference_price=cast(Decimal, execution_bar.execution_vwap),
                volume_btc=cast(Decimal, execution_bar.execution_volume),
                trade_count=cast(int, execution_bar.execution_trade_count),
            )
        )
    return candles, tuple(references)


def _split_boundaries(evaluation_bars: Sequence[DailyTradeBar]) -> tuple[SplitBoundary, ...]:
    count = len(evaluation_bars)
    development_count = count * 60 // 100
    validation_count = count * 20 // 100
    holdout_count = count - development_count - validation_count
    counts = (development_count, validation_count, holdout_count)
    if any(value <= 0 for value in counts):
        raise HistoricalStudyError("evaluation history is too short for non-empty 60/20/20 splits")
    boundaries: list[SplitBoundary] = []
    start = 0
    for name, size in zip(("development", "validation", "frozen_holdout"), counts, strict=True):
        selected = evaluation_bars[start : start + size]
        boundaries.append(
            SplitBoundary(
                name=name,
                first_day=selected[0].day,
                last_day=selected[-1].day,
                observation_count=size,
            )
        )
        start += size
    return tuple(boundaries)


def _split_for_day(day: date, boundaries: Sequence[SplitBoundary]) -> str:
    for boundary in boundaries:
        if boundary.first_day <= day <= boundary.last_day:
            return boundary.name
    raise HistoricalStudyError("equity date falls outside chronological split boundaries")


def _cost_cases(document: Mapping[str, Any]) -> tuple[_CostCase, ...]:
    raw_cases = _sequence(document.get("cost_sensitivities"), field="cost_sensitivities")
    cases: list[_CostCase] = []
    names: set[str] = set()
    for raw in raw_cases:
        item = _mapping(raw, field="cost_sensitivity")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise HistoricalStudyError("cost-case names must be unique non-empty strings")
        names.add(name)
        cases.append(
            _CostCase(
                name=name,
                buy_fee=_decimal(item.get("buy_fee_fraction"), field="buy_fee_fraction"),
                sell_fee=_decimal(item.get("sell_fee_fraction"), field="sell_fee_fraction"),
                slippage=_decimal(item.get("adverse_slippage_fraction_per_side"), field="slippage"),
            )
        )
    primary_name = _mapping(document.get("primary_cost_case"), field="primary_cost_case").get(
        "name"
    )
    if primary_name != "taker_taker_plus_10bps" or primary_name not in names:
        raise HistoricalStudyError("frozen primary cost case is missing")
    if len(cases) != 7:
        raise HistoricalStudyError("the frozen protocol requires seven named cost cases")
    return tuple(cases)


def _execution_costs(cost_case: _CostCase) -> ExecutionCosts:
    if cost_case.name.startswith("maker_maker"):
        if cost_case.buy_fee != cost_case.sell_fee:
            raise HistoricalStudyError("maker/maker fees must match")
        return ExecutionCosts(
            maker_fee_rate=cost_case.buy_fee,
            taker_fee_rate=cost_case.buy_fee,
            slippage_rate=cost_case.slippage,
            buy_liquidity=Liquidity.MAKER,
            sell_liquidity=Liquidity.MAKER,
        )
    if cost_case.name.startswith("maker_taker"):
        return ExecutionCosts(
            maker_fee_rate=cost_case.buy_fee,
            taker_fee_rate=cost_case.sell_fee,
            slippage_rate=cost_case.slippage,
            buy_liquidity=Liquidity.MAKER,
            sell_liquidity=Liquidity.TAKER,
        )
    if cost_case.name.startswith("taker_taker") or cost_case.name == "double_primary_costs":
        if cost_case.buy_fee != cost_case.sell_fee:
            raise HistoricalStudyError("taker/taker fees must match")
        return ExecutionCosts(
            maker_fee_rate=cost_case.buy_fee,
            taker_fee_rate=cost_case.buy_fee,
            slippage_rate=cost_case.slippage,
            buy_liquidity=Liquidity.TAKER,
            sell_liquidity=Liquidity.TAKER,
        )
    raise HistoricalStudyError(f"unsupported frozen cost case: {cost_case.name}")


def _backtest_config(document: Mapping[str, Any], cost_case: _CostCase) -> BacktestConfig:
    portfolio = _mapping(document.get("portfolio"), field="portfolio")
    execution = _mapping(document.get("execution"), field="execution")
    instrument = _mapping(document.get("instrument_snapshot"), field="instrument_snapshot")
    quantity_decimals = _positive_integer(
        instrument.get("quantity_decimals"), field="quantity_decimals"
    )
    return BacktestConfig(
        initial_cash=_decimal(portfolio.get("initial_cad"), field="initial_cad"),
        costs=_execution_costs(cost_case),
        instrument_rules=InstrumentRules(
            price_tick_cad=_decimal(instrument.get("price_tick_cad"), field="price_tick_cad"),
            quantity_increment_btc=Decimal("1").scaleb(-quantity_decimals),
            minimum_quantity_btc=_decimal(
                instrument.get("minimum_quantity_btc"), field="minimum_quantity_btc"
            ),
            minimum_cost_cad=_decimal(instrument.get("minimum_cost_cad"), field="minimum_cost_cad"),
        ),
        max_execution_volume_fraction=_decimal(
            execution.get("maximum_execution_minute_volume_participation"),
            field="maximum_execution_minute_volume_participation",
        ),
        cash_reserve_cad=_decimal(portfolio.get("cash_reserve_cad"), field="cash_reserve_cad"),
        absolute_btc_cap_cad=_decimal(
            portfolio.get("absolute_btc_cap_cad"), field="absolute_btc_cap_cad"
        ),
        max_post_cost_exposure=_decimal(
            portfolio.get("maximum_post_cost_exposure"), field="maximum_post_cost_exposure"
        ),
        rebalance_min_cad=_decimal(
            portfolio.get("minimum_rebalance_cad"), field="minimum_rebalance_cad"
        ),
        rebalance_equity_fraction=_decimal(
            portfolio.get("minimum_rebalance_equity_fraction"),
            field="minimum_rebalance_equity_fraction",
        ),
        rolling_24h_loss_threshold=_decimal(
            portfolio.get("rolling_24h_loss_gate"), field="rolling_24h_loss_gate"
        ),
        max_drawdown_threshold=_decimal(
            portfolio.get("maximum_drawdown_before_persistent_disarm"),
            field="maximum_drawdown_before_persistent_disarm",
        ),
        decision_delay_minutes=_positive_integer(
            execution.get("decision_delay_minutes_after_daily_close"),
            field="decision_delay_minutes_after_daily_close",
        ),
    )


def _selected_strategy(
    document: Mapping[str, Any], *, windows: tuple[int, int, int]
) -> MomentumTrendStrategy:
    strategy = _mapping(document.get("strategy"), field="strategy")
    return MomentumTrendStrategy(
        StrategyPolicy(
            momentum_days=windows[0],
            trend_days=windows[1],
            volatility_days=windows[2],
            annualization_days=_positive_integer(
                strategy.get("annualization_days"), field="annualization_days"
            ),
            volatility_target=_decimal(
                strategy.get("target_annual_volatility"), field="target_annual_volatility"
            ),
            max_weight=_decimal(strategy.get("maximum_weight"), field="maximum_weight"),
        )
    )


class _BinaryComparatorStrategy(MomentumTrendStrategy):
    """Causal fixed-weight comparator run through the same execution engine."""

    def __init__(self, name: str, *, momentum_days: int = 90, trend_days: int = 200) -> None:
        super().__init__(
            StrategyPolicy(momentum_days=momentum_days, trend_days=trend_days, volatility_days=30)
        )
        self.name = name

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        signal = candles[-1] if candles else None
        needed = 201 if self.name != "positive_90_day_momentum" else 91
        usable = signal is not None and len(candles) >= needed
        close = signal.close if signal is not None else None
        momentum: Decimal | None = None
        sma: Decimal | None = None
        long_signal = False
        if usable and close is not None:
            momentum = close / candles[-91].close - Decimal("1")
            sma = sum((item.close for item in candles[-200:]), start=Decimal("0")) / Decimal("200")
            if self.name == "close_above_sma200":
                long_signal = close > sma
            elif self.name == "positive_90_day_momentum":
                long_signal = momentum > 0
            elif self.name == "combined_momentum_and_sma200_unsized":
                long_signal = momentum > 0 and close > sma
            else:
                raise HistoricalStudyError(f"unsupported comparator strategy: {self.name}")
        reason = (
            DecisionReason.INSUFFICIENT_HISTORY
            if not usable
            else (
                DecisionReason.LONG_SIGNAL
                if long_signal
                else (
                    DecisionReason.NON_POSITIVE_MOMENTUM
                    if momentum is not None and momentum <= 0
                    else DecisionReason.BELOW_TREND
                )
            )
        )
        target = Decimal("0.80") if long_signal else Decimal("0")
        fields = {
            "comparator": self.name,
            "signal_close_time": signal.close_time.isoformat() if signal else None,
            "target_weight": str(target),
            "reason": reason.value,
            "policy_hash": self.policy.fingerprint,
        }
        decision_id = _sha256_bytes(_canonical_json_bytes(fields))
        return StrategyDecision(
            decision_id=decision_id,
            signal_open_time=signal.open_time if signal else None,
            signal_close_time=signal.close_time if signal else None,
            state=PositionState.BTC if long_signal else PositionState.CASH,
            target_weight=target,
            reason=reason,
            usable_data=usable,
            policy_hash=self.policy.fingerprint,
            input_data_hash=decision_id,
            close=close,
            momentum=momentum,
            sma=sma,
            annualized_volatility=None,
        )


def _run_engine(
    *,
    candles: Sequence[Candle],
    references: Sequence[ExecutionReference],
    evaluation_start: datetime,
    strategy: MomentumTrendStrategy,
    config: BacktestConfig,
) -> BacktestResult:
    return run_backtest(
        candles,
        strategy,
        config,
        execution_references=references,
        evaluation_start=evaluation_start,
    )


def _cash_curve(template: Sequence[EquityPoint]) -> tuple[EquityPoint, ...]:
    return tuple(
        EquityPoint(
            close_time=point.close_time,
            close=point.close,
            cash=INITIAL_CAD,
            btc=Decimal("0"),
            equity=INITIAL_CAD,
            btc_mark_value_cad=Decimal("0"),
            estimated_liquidation_fee_cad=Decimal("0"),
            estimated_liquidation_slippage_cad=Decimal("0"),
            cumulative_fees=Decimal("0"),
        )
        for point in template
    )


def _buy_and_hold_curve(
    *,
    template: Sequence[EquityPoint],
    evaluation_start: datetime,
    references: Sequence[ExecutionReference],
    config: BacktestConfig,
) -> tuple[
    tuple[EquityPoint, ...],
    TradeCostAggregate,
    dict[str, object],
    tuple[BuyAndHoldEntryAttempt, ...],
]:
    """Build the fee-aware benchmark through causal volume-capped accumulation.

    An instantaneous all-in fill would claim liquidity that the frozen minute
    may not contain. This comparator therefore retries on each later available
    reference, buying at most the strategy's participation cap until the cash
    remainder is below Kraken's cost minimum or the sample ends.
    """

    if len(template) < 2:
        raise HistoricalStudyError("buy-and-hold requires at least one evaluation close")
    costs = config.costs
    rules = config.instrument_rules
    if rules is None:
        raise HistoricalStudyError("fee-aware buy-and-hold requires frozen instrument rules")
    participation_fraction = config.max_execution_volume_fraction
    if participation_fraction is None:
        raise HistoricalStudyError("fee-aware buy-and-hold requires a participation cap")
    buy_fee_rate = (
        costs.maker_fee_rate if costs.buy_liquidity is Liquidity.MAKER else costs.taker_fee_rate
    )
    sell_fee_rate = (
        costs.maker_fee_rate if costs.sell_liquidity is Liquidity.MAKER else costs.taker_fee_rate
    )
    eligible_references = tuple(
        reference
        for reference in references
        if reference.decision_time >= evaluation_start
        and reference.execution_time <= template[-1].close_time
    )
    empty_costs = TradeCostAggregate(
        traded_notional=Decimal("0"),
        fees=Decimal("0"),
        slippage=Decimal("0"),
        trade_count=0,
    )
    if not eligible_references:
        return (
            _cash_curve(template),
            empty_costs,
            {
                "status": "unavailable_no_post_boundary_execution_reference",
                "comparator_definition": "causal_accumulation_under_execution_volume_cap",
                "entry_decision_time_utc": None,
                "entry_execution_time_utc": None,
                "last_entry_execution_time_utc": None,
                "wait_days": None,
                "participation_capped": None,
                "maximum_participation_fraction": _decimal_text(participation_fraction),
                "entry_attempt_count": 0,
                "entry_fill_count": 0,
                "entry_volume_capped_fill_count": 0,
                "entry_quantity_btc": None,
                "remaining_cash_cad": _decimal_text(INITIAL_CAD),
                "entry_attempts_artifact": "buy_and_hold_entries.csv",
            },
            (),
        )

    cash = INITIAL_CAD
    btc = Decimal("0")
    cumulative_fees = Decimal("0")
    attempts: list[BuyAndHoldEntryAttempt] = []
    next_reference_index = 0
    curve: list[EquityPoint] = [
        EquityPoint(
            close_time=template[0].close_time,
            close=template[0].close,
            cash=INITIAL_CAD,
            btc=Decimal("0"),
            equity=INITIAL_CAD,
            btc_mark_value_cad=Decimal("0"),
            estimated_liquidation_fee_cad=Decimal("0"),
            estimated_liquidation_slippage_cad=Decimal("0"),
            cumulative_fees=Decimal("0"),
        )
    ]

    for point in template[1:]:
        while (
            next_reference_index < len(eligible_references)
            and eligible_references[next_reference_index].execution_time <= point.close_time
            and cash >= rules.minimum_cost_cad
        ):
            reference = eligible_references[next_reference_index]
            next_reference_index += 1
            buy_price_unrounded = reference.reference_price * (Decimal("1") + costs.slippage_rate)
            buy_price = (buy_price_unrounded / rules.price_tick_cad).to_integral_value(
                rounding=ROUND_CEILING
            ) * rules.price_tick_cad
            affordable_quantity = cash / (buy_price * (Decimal("1") + buy_fee_rate))
            maximum_participating_quantity = reference.volume_btc * participation_fraction
            volume_cap_applied = affordable_quantity > maximum_participating_quantity
            quantity_before_rounding = min(
                affordable_quantity,
                maximum_participating_quantity,
            )
            quantity = (quantity_before_rounding / rules.quantity_increment_btc).to_integral_value(
                rounding=ROUND_DOWN
            ) * rules.quantity_increment_btc
            gross_notional = quantity * buy_price
            if quantity < rules.minimum_quantity_btc or gross_notional < rules.minimum_cost_cad:
                attempts.append(
                    BuyAndHoldEntryAttempt(
                        decision_time=reference.decision_time,
                        execution_time=reference.execution_time,
                        reference_price_cad=reference.reference_price,
                        execution_price_cad=buy_price,
                        execution_volume_btc=reference.volume_btc,
                        maximum_participation_fraction=participation_fraction,
                        maximum_participating_quantity_btc=(maximum_participating_quantity),
                        affordable_quantity_btc=affordable_quantity,
                        executed_quantity_btc=Decimal("0"),
                        volume_cap_applied=volume_cap_applied,
                        gross_notional_cad=Decimal("0"),
                        fee_cad=Decimal("0"),
                        slippage_cad=Decimal("0"),
                        cash_after_cad=cash,
                        btc_after=btc,
                        outcome="no_fill_below_exchange_minimum",
                    )
                )
                continue
            fee = gross_notional * buy_fee_rate
            slippage = quantity * abs(buy_price - reference.reference_price)
            cash -= gross_notional + fee
            btc += quantity
            cumulative_fees += fee
            if cash < 0 and abs(cash) < Decimal("1e-20"):
                cash = Decimal("0")
            if cash < 0:
                raise HistoricalStudyError("buy-and-hold accumulation overspent cash")
            attempts.append(
                BuyAndHoldEntryAttempt(
                    decision_time=reference.decision_time,
                    execution_time=reference.execution_time,
                    reference_price_cad=reference.reference_price,
                    execution_price_cad=buy_price,
                    execution_volume_btc=reference.volume_btc,
                    maximum_participation_fraction=participation_fraction,
                    maximum_participating_quantity_btc=maximum_participating_quantity,
                    affordable_quantity_btc=affordable_quantity,
                    executed_quantity_btc=quantity,
                    volume_cap_applied=volume_cap_applied,
                    gross_notional_cad=gross_notional,
                    fee_cad=fee,
                    slippage_cad=slippage,
                    cash_after_cad=cash,
                    btc_after=btc,
                    outcome=(
                        "filled_volume_capped" if volume_cap_applied else "filled_remaining_cash"
                    ),
                )
            )

        sell_price_unrounded = point.close * (Decimal("1") - costs.slippage_rate)
        sell_price = (sell_price_unrounded / rules.price_tick_cad).to_integral_value(
            rounding=ROUND_FLOOR
        ) * rules.price_tick_cad
        gross = btc * point.close
        liquidation_value = btc * sell_price * (Decimal("1") - sell_fee_rate)
        curve.append(
            EquityPoint(
                close_time=point.close_time,
                close=point.close,
                cash=cash,
                btc=btc,
                equity=cash + liquidation_value,
                btc_mark_value_cad=gross,
                estimated_liquidation_fee_cad=(btc * sell_price * sell_fee_rate),
                estimated_liquidation_slippage_cad=(gross - btc * sell_price),
                cumulative_fees=cumulative_fees,
            )
        )

    filled_attempts = tuple(attempt for attempt in attempts if attempt.executed_quantity_btc > 0)
    terminal_reference = template[-1].close
    terminal_price_unrounded = terminal_reference * (Decimal("1") - costs.slippage_rate)
    terminal_price = (terminal_price_unrounded / rules.price_tick_cad).to_integral_value(
        rounding=ROUND_FLOOR
    ) * rules.price_tick_cad
    terminal_notional = btc * terminal_price
    terminal_fee = terminal_notional * sell_fee_rate
    entry_notional = sum(
        (attempt.gross_notional_cad for attempt in filled_attempts),
        start=Decimal("0"),
    )
    entry_fees = sum((attempt.fee_cad for attempt in filled_attempts), start=Decimal("0"))
    entry_slippage = sum(
        (attempt.slippage_cad for attempt in filled_attempts),
        start=Decimal("0"),
    )
    terminal_slippage = btc * abs(terminal_price - terminal_reference)
    first_fill = filled_attempts[0] if filled_attempts else None
    last_fill = filled_attempts[-1] if filled_attempts else None
    participation_capped = any(attempt.volume_cap_applied for attempt in filled_attempts)
    if first_fill is None:
        status = "unavailable_no_fill_before_cutoff"
    elif cash < rules.minimum_cost_cad:
        status = "accumulated_until_remaining_cash_below_exchange_minimum"
    else:
        status = "accumulated_through_last_available_reference"
    return (
        tuple(curve),
        TradeCostAggregate(
            traded_notional=entry_notional + terminal_notional,
            fees=entry_fees + terminal_fee,
            slippage=entry_slippage + terminal_slippage,
            trade_count=len(filled_attempts) + (1 if btc > 0 else 0),
        ),
        {
            "status": status,
            "comparator_definition": "causal_accumulation_under_execution_volume_cap",
            "entry_decision_time_utc": (
                _iso_z(first_fill.decision_time) if first_fill is not None else None
            ),
            "entry_execution_time_utc": (
                _iso_z(first_fill.execution_time) if first_fill is not None else None
            ),
            "last_entry_execution_time_utc": (
                _iso_z(last_fill.execution_time) if last_fill is not None else None
            ),
            "wait_days": (
                (first_fill.decision_time.date() - evaluation_start.date()).days
                if first_fill is not None
                else None
            ),
            "participation_capped": participation_capped,
            "maximum_participation_fraction": _decimal_text(participation_fraction),
            "entry_attempt_count": len(attempts),
            "entry_fill_count": len(filled_attempts),
            "entry_volume_capped_fill_count": sum(
                attempt.volume_cap_applied for attempt in filled_attempts
            ),
            "entry_quantity_btc": _decimal_text(btc),
            "remaining_cash_cad": _decimal_text(cash),
            "entry_attempts_artifact": "buy_and_hold_entries.csv",
        },
        tuple(attempts),
    )


def _trade_costs(trades: Sequence[Trade], *, terminal: EquityPoint) -> TradeCostAggregate:
    terminal_notional = terminal.btc_mark_value_cad - terminal.estimated_liquidation_slippage_cad
    return TradeCostAggregate(
        traded_notional=sum((trade.gross_notional_cad for trade in trades), start=Decimal("0"))
        + terminal_notional,
        fees=sum((trade.fee_cad for trade in trades), start=Decimal("0"))
        + terminal.estimated_liquidation_fee_cad,
        slippage=sum((trade.slippage_cad for trade in trades), start=Decimal("0"))
        + terminal.estimated_liquidation_slippage_cad,
        trade_count=len(trades),
    )


def _study_metrics(
    curve: Sequence[EquityPoint],
    trades: Sequence[Trade],
    *,
    synthetic_trade_costs: TradeCostAggregate | None = None,
    include_cost_attribution: bool = True,
) -> ResearchMetrics:
    observations = tuple(
        EquityObservation(
            observed_at=point.close_time,
            equity=point.equity,
            # Equity is already expressed on a liquidation basis. Subtracting
            # cash therefore isolates the net BTC liquidation value, including
            # the synthetic buy-and-hold comparator's adverse exit slippage.
            exposure_fraction=(point.equity - point.cash) / point.equity,
        )
        for point in curve
    )
    return calculate_research_metrics(
        observations,
        trade_costs=(
            synthetic_trade_costs or _trade_costs(trades, terminal=curve[-1])
            if include_cost_attribution
            else None
        ),
    )


def _metric_dict(metrics: ResearchMetrics) -> dict[str, object]:
    exact = metrics_to_jsonable(metrics)
    return {
        "initial_equity_cad": exact["initial_equity"],
        "final_equity_cad": exact["final_equity"],
        "net_profit_cad": exact["net_pnl"],
        "gross_profit_cad": exact["gross_pnl"],
        "total_return": exact["total_return"],
        "cagr": exact["cagr"],
        "annualized_volatility": exact["annualized_volatility"],
        "sharpe": exact["sharpe"],
        "downside_deviation": exact["downside_deviation"],
        "sortino": exact["sortino"],
        "max_drawdown": exact["max_drawdown"],
        "max_drawdown_duration_days": exact["max_drawdown_duration_days"],
        "calmar": exact["calmar"],
        "average_exposure": exact["exposure_fraction"],
        "turnover": exact["turnover"],
        "total_fees_cad": exact["fees"],
        "total_slippage_cad": exact["slippage"],
        "trade_count": exact["trade_count"],
        "calendar_year_returns": exact["calendar_year_returns"],
        "calendar_month_returns": exact["calendar_month_returns"],
    }


def _curve_slice(curve: Sequence[EquityPoint], start: int, size: int) -> tuple[EquityPoint, ...]:
    # Curve point zero is the shared pre-evaluation capital boundary.
    selected = tuple(curve[start : start + size + 1])
    if len(selected) != size + 1:
        raise HistoricalStudyError("curve does not align with chronological split")
    return selected


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_metrics(
    path: Path,
    *,
    runs: Sequence[CurveRun],
    cost_results: Sequence[tuple[str, str, BacktestResult]],
    boundaries: Sequence[SplitBoundary],
) -> list[dict[str, object]]:
    rows: list[list[object]] = []
    summary_rows: list[dict[str, object]] = []
    boundary_offsets: dict[str, tuple[int, int]] = {}
    start = 0
    for boundary in boundaries:
        boundary_offsets[boundary.name] = (start, boundary.observation_count)
        start += boundary.observation_count
    for run in runs:
        scopes = [("full_evaluation", run.curve)]
        scopes.extend(
            (
                boundary.name,
                _curve_slice(run.curve, *boundary_offsets[boundary.name]),
            )
            for boundary in boundaries
        )
        for scope, curve in scopes:
            if scope == "full_evaluation":
                selected_trades = run.trades
                synthetic_costs = run.synthetic_trade_costs
            else:
                boundary = next(item for item in boundaries if item.name == scope)
                selected_trades = tuple(
                    trade
                    for trade in run.trades
                    if boundary.first_day <= trade.execution_time.date() <= boundary.last_day
                )
                synthetic_costs = None
            metrics = _study_metrics(
                curve,
                selected_trades,
                synthetic_trade_costs=synthetic_costs,
                include_cost_attribution=scope == "full_evaluation",
            )
            values = _metric_dict(metrics)
            rows.append(
                ["comparator", run.name, scope, *(values[column] for column in METRIC_COLUMNS)]
            )
            summary_rows.append(
                {"category": "comparator", "name": run.name, "scope": scope, **values}
            )
    for name, scope, result in cost_results:
        metrics = _study_metrics(result.equity_curve, result.trades)
        values = _metric_dict(metrics)
        rows.append(
            [
                "cost_sensitivity",
                name,
                scope,
                *(values[column] for column in METRIC_COLUMNS),
            ]
        )
        summary_rows.append(
            {"category": "cost_sensitivity", "name": name, "scope": scope, **values}
        )
    _write_csv(path, ("category", "name", "scope", *METRIC_COLUMNS), rows)
    return summary_rows


def _write_calendar_returns(path: Path, runs: Sequence[CurveRun]) -> None:
    rows: list[list[object]] = []
    for run in runs:
        metrics = _study_metrics(
            run.curve,
            run.trades,
            synthetic_trade_costs=run.synthetic_trade_costs,
        )
        for frequency, values in (
            ("year", metrics.calendar_year_returns),
            ("month", metrics.calendar_month_returns),
        ):
            rows.extend(
                [run.name, frequency, item.period, _decimal_text(item.return_fraction)]
                for item in values
            )
    _write_csv(
        path,
        ("strategy", "frequency", "period_utc", "return_fraction"),
        rows,
    )


def _write_equity(
    path: Path, *, runs: Sequence[CurveRun], boundaries: Sequence[SplitBoundary]
) -> None:
    rows: list[list[object]] = []
    for run in runs:
        for index, point in enumerate(run.curve):
            if index == 0:
                split = "shared_initial_boundary"
            else:
                split = _split_for_day(
                    (point.close_time - timedelta(microseconds=1)).date(), boundaries
                )
            rows.append(
                [
                    run.name,
                    index,
                    _iso_z(point.close_time),
                    split,
                    _decimal_text(point.close),
                    _decimal_text(point.cash),
                    _decimal_text(point.btc),
                    _decimal_text(point.equity),
                    _decimal_text(point.btc_mark_value_cad),
                    _decimal_text(point.estimated_liquidation_fee_cad),
                    _decimal_text(point.estimated_liquidation_slippage_cad),
                    _decimal_text(point.cumulative_fees),
                ]
            )
    _write_csv(
        path,
        (
            "strategy",
            "observation_index",
            "observed_at_utc",
            "split",
            "reference_price_cad",
            "cash_cad",
            "btc",
            "equity_cad",
            "btc_mark_value_cad",
            "estimated_liquidation_fee_cad",
            "estimated_liquidation_slippage_cad",
            "cumulative_fees_cad",
        ),
        rows,
    )


def _write_decisions(
    path: Path, result: BacktestResult, boundaries: Sequence[SplitBoundary]
) -> None:
    rows = [
        [
            item.intent_id,
            item.strategy_decision_id,
            _iso_z(item.signal_time) if item.signal_time else "",
            _iso_z(item.decision_time),
            _iso_z(item.execution_time),
            _split_for_day(item.decision_time.date(), boundaries),
            item.strategy_reason,
            _decimal_text(item.target_weight),
            _decimal_text(item.pre_trade_equity),
            _decimal_text(item.requested_delta_cad),
            _decimal_text(item.rebalance_band_cad),
            item.outcome.value,
            item.trade_id or "",
            _decimal_text(item.remaining_btc) if item.remaining_btc is not None else "",
        ]
        for item in result.decisions
    ]
    _write_csv(
        path,
        (
            "intent_id",
            "strategy_decision_id",
            "signal_close_utc",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "strategy_reason",
            "target_weight",
            "pre_trade_equity_cad",
            "requested_delta_cad",
            "rebalance_band_cad",
            "outcome",
            "trade_id",
            "remaining_btc_after_attempt",
        ),
        rows,
    )


def _write_fills(path: Path, trades: Sequence[Trade], boundaries: Sequence[SplitBoundary]) -> None:
    rows = [
        [
            trade.trade_id,
            trade.intent_id,
            trade.strategy_decision_id,
            _iso_z(trade.decision_time),
            _iso_z(trade.execution_time),
            _split_for_day(trade.decision_time.date(), boundaries),
            trade.side.value,
            trade.liquidity.value,
            _decimal_text(trade.quantity_btc),
            (
                _decimal_text(trade.execution_volume_btc)
                if trade.execution_volume_btc is not None
                else ""
            ),
            (
                _decimal_text(trade.volume_participation_fraction)
                if trade.volume_participation_fraction is not None
                else ""
            ),
            trade.volume_cap_applied,
            _decimal_text(trade.reference_price),
            _decimal_text(trade.execution_price),
            _decimal_text(trade.gross_notional_cad),
            _decimal_text(trade.fee_cad),
            _decimal_text(trade.slippage_cad),
            _decimal_text(trade.cash_after),
            _decimal_text(trade.btc_after),
        ]
        for trade in trades
    ]
    _write_csv(
        path,
        (
            "trade_id",
            "intent_id",
            "strategy_decision_id",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "side",
            "liquidity",
            "quantity_btc",
            "execution_volume_btc",
            "volume_participation_fraction",
            "volume_cap_applied",
            "reference_price_cad",
            "execution_price_cad",
            "gross_notional_cad",
            "fee_cad",
            "slippage_cad",
            "cash_after_cad",
            "btc_after",
        ),
        rows,
    )


def _write_buy_and_hold_entries(
    path: Path,
    attempts: Sequence[BuyAndHoldEntryAttempt],
    boundaries: Sequence[SplitBoundary],
) -> None:
    rows = [
        [
            index,
            _iso_z(attempt.decision_time),
            _iso_z(attempt.execution_time),
            _split_for_day(attempt.decision_time.date(), boundaries),
            _decimal_text(attempt.reference_price_cad),
            _decimal_text(attempt.execution_price_cad),
            _decimal_text(attempt.execution_volume_btc),
            _decimal_text(attempt.maximum_participation_fraction),
            _decimal_text(attempt.maximum_participating_quantity_btc),
            _decimal_text(attempt.affordable_quantity_btc),
            _decimal_text(attempt.executed_quantity_btc),
            _decimal_text(attempt.executed_quantity_btc / attempt.execution_volume_btc),
            attempt.volume_cap_applied,
            _decimal_text(attempt.gross_notional_cad),
            _decimal_text(attempt.fee_cad),
            _decimal_text(attempt.slippage_cad),
            _decimal_text(attempt.cash_after_cad),
            _decimal_text(attempt.btc_after),
            attempt.outcome,
        ]
        for index, attempt in enumerate(attempts, start=1)
    ]
    _write_csv(
        path,
        (
            "attempt_index",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "reference_price_cad",
            "execution_price_cad",
            "execution_volume_btc",
            "maximum_participation_fraction",
            "maximum_participating_quantity_btc",
            "affordable_quantity_btc",
            "executed_quantity_btc",
            "volume_participation_fraction",
            "volume_cap_applied",
            "gross_notional_cad",
            "fee_cad",
            "slippage_cad",
            "cash_after_cad",
            "btc_after",
            "outcome",
        ),
        rows,
    )


def _write_risk(
    path: Path, events: Sequence[RiskEvent], boundaries: Sequence[SplitBoundary]
) -> None:
    rows = [
        [
            event.event_id,
            event.event_type.value,
            _iso_z(event.observed_at),
            _split_for_day(event.observed_at.date(), boundaries),
            event.strategy_decision_id or "",
            _decimal_text(event.equity),
            _decimal_text(event.reference_equity),
            _decimal_text(event.observed_fraction),
            _decimal_text(event.threshold),
            event.action,
        ]
        for event in events
    ]
    _write_csv(
        path,
        (
            "event_id",
            "event_type",
            "observed_at_utc",
            "split",
            "strategy_decision_id",
            "equity_cad",
            "reference_equity_cad",
            "observed_fraction",
            "threshold",
            "action",
        ),
        rows,
    )


def _drawdown_curve(curve: Sequence[EquityPoint]) -> tuple[ChartPoint, ...]:
    peak = curve[0].equity
    points: list[ChartPoint] = []
    for point in curve:
        peak = max(peak, point.equity)
        points.append(ChartPoint(point.close_time, point.equity / peak - Decimal("1")))
    return tuple(points)


def _write_charts(
    output_dir: Path,
    *,
    runs: Mapping[str, CurveRun],
    robustness_rows: Sequence[Mapping[str, object]],
) -> tuple[Path, ...]:
    charts_dir = output_dir / "charts"
    chart_runs = (
        ("Frozen V1", "#2dd4bf", runs["frozen_v1"]),
        ("BTC buy & hold", "#60a5fa", runs["fee_aware_btc_buy_and_hold"]),
        ("CAD cash", "#9fb0c6", runs["cad_cash"]),
    )
    equity_path = write_line_chart(
        charts_dir / "equity.svg",
        title="BTC/CAD causal historical equity",
        subtitle="Shared C$1,000 boundary; frozen primary trading costs",
        series=tuple(
            ChartSeries(
                name,
                color,
                tuple(ChartPoint(point.close_time, point.equity) for point in run.curve),
            )
            for name, color, run in chart_runs
        ),
        y_label="Liquidation equity (CAD)",
    )
    drawdown_path = write_line_chart(
        charts_dir / "drawdown.svg",
        title="Historical drawdown",
        subtitle="Peak-to-current decline; zero is a new high-water mark",
        series=tuple(
            ChartSeries(name, color, _drawdown_curve(run.curve))
            for name, color, run in chart_runs[:2]
        ),
        y_label="Drawdown",
        percent_axis=True,
    )
    cells = tuple(
        RobustnessCell(
            momentum_days=cast(int, row["momentum_days"]),
            trend_days=cast(int, row["trend_days"]),
            volatility_days=cast(int, row["volatility_days"]),
            value=_decimal(row["pre_holdout_total_return"], field="pre_holdout_total_return"),
            selected=cast(bool, row["pre_registered_selected"]),
        )
        for row in robustness_rows
    )
    robustness_path = write_robustness_heatmap(
        charts_dir / "robustness.svg",
        title="Parameter-neighborhood robustness",
        subtitle="Development + validation only; frozen holdout is hidden from neighboring cells",
        cells=cells,
        metric_label="pre-holdout total return",
    )
    return equity_path, drawdown_path, robustness_path


def _write_report(
    path: Path,
    *,
    study_id: str,
    git_state: GitState,
    config_sha256: str,
    input_hash: str,
    clean_hash: str,
    selected: Sequence[DailyTradeBar],
    discarded_count: int,
    boundaries: Sequence[SplitBoundary],
    primary_metrics: ResearchMetrics,
    holdout_metrics: ResearchMetrics,
    missing_references: int,
    buy_hold_status: Mapping[str, object],
    research_validated: bool,
    engine_override_used: bool,
) -> None:
    split_lines = "\n".join(
        f"- {boundary.name}: {boundary.first_day.isoformat()} through "
        f"{boundary.last_day.isoformat()} ({boundary.observation_count} days)"
        for boundary in boundaries
    )
    if git_state.dirty:
        dirty_note = "DIRTY DEVELOPMENT OVERRIDE — not an admissible final holdout run."
    elif engine_override_used:
        dirty_note = "NON-PRODUCTION ENGINE OVERRIDE — not an admissible final holdout run."
    else:
        dirty_note = "Clean committed worktree using the production backtest engine."
    evidence_statement = (
        "ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED."
        if research_validated
        else (
            "RESEARCH_INVALIDATED — development override, engine override, "
            "or code identity mismatch."
        )
    )

    def optional_decimal(value: Decimal | None) -> str:
        return "undefined" if value is None else _decimal_text(value)

    buy_hold_attempt_count = buy_hold_status["entry_attempt_count"]
    buy_hold_fill_count = buy_hold_status["entry_fill_count"]
    buy_hold_capped_fill_count = buy_hold_status["entry_volume_capped_fill_count"]
    payload = f"""# BTC/CAD causal historical study

## Interpretation

This is historical price evidence, not evidence of future profitability or statistical
significance. The frozen holdout reports only the pre-specified selected V1 primary-cost replay
and the pre-specified comparator evaluations. Neighboring parameters and non-primary cost cases
see development plus validation only; they diagnose fragility and do not select an optimum.

## Engineering status

- Evidence statement: **{evidence_statement}**
- Deterministic causal replay completed.
- {dirty_note}
- Git commit: `{git_state.commit}`
- Pre-registration SHA-256: `{config_sha256}`
- Full input-data SHA-256: `{input_hash}`
- Selected clean-sequence SHA-256: `{clean_hash}`
- Missing post-decision execution references: {missing_references} (recorded as no-fill)

## Dataset boundary

- Study: `{study_id}`
- Selected history: {selected[0].day.isoformat()} through {selected[-1].day.isoformat()}
- Selected contiguous days: {len(selected)}
- Information-only warm-up: {WARMUP_DAYS} days
- Discarded days outside the selected contiguous sequence: {discarded_count}

{split_lines}

## Frozen V1 result

- Full-period final equity: C${_decimal_text(primary_metrics.final_equity)}
- Full-period net P&L: C${_decimal_text(primary_metrics.net_pnl)}
- Full-period return: {_decimal_text(primary_metrics.total_return)}
- Full-period Sharpe: {optional_decimal(primary_metrics.sharpe)}
- Full-period maximum drawdown: {_decimal_text(primary_metrics.max_drawdown)}
- Frozen-holdout return: {_decimal_text(holdout_metrics.total_return)}
- Frozen-holdout Sharpe: {optional_decimal(holdout_metrics.sharpe)}
- Fee-aware buy-and-hold accumulation status: {buy_hold_status["status"]}
- First buy-and-hold fill time: {buy_hold_status["entry_execution_time_utc"]}
- Last buy-and-hold fill time: {buy_hold_status["last_entry_execution_time_utc"]}
- Buy-and-hold entry attempts / fills: {buy_hold_attempt_count} / {buy_hold_fill_count}
- Buy-and-hold volume-capped fills: {buy_hold_capped_fill_count}

## Profitability status

No profitability claim is made automatically. Read `metrics.csv` together with the split labels,
cost sensitivities, drawdown chart, risk events, and robustness grid. A profitable historical row
does not authorize paper or live trading.

## Reproduction artifacts

The machine-readable source of every graph is included in this directory. `checksums.sha256`
binds the report, CSVs, JSON summary, and SVG charts; `manifest.json` records the code, config,
and input-data identities used for this run.
"""
    path.write_text(payload, encoding="utf-8", newline="\n")


def run_historical_study(
    bars: Sequence[DailyTradeBar],
    *,
    preregistration_path: Path,
    output_dir: Path,
    repository_root: Path,
    normalized_manifest_path: Path,
    source_data_filename: str,
    expected_commit: str,
    expected_preregistration_sha256: str,
    allow_dirty: bool = False,
    source_data_sha256: str | None = None,
    engine: Callable[..., BacktestResult] = run_backtest,
    _code_identity_paths: Sequence[Path] | None = None,
) -> StudyResult:
    """Run the frozen study and write its deterministic audit bundle.

    ``allow_dirty`` exists only for development and synthetic tests; using it is
    recorded prominently in both summary and report artifacts.
    """

    if output_dir.exists() and any(output_dir.iterdir()):
        raise HistoricalStudyError(
            "output_dir must be absent or empty; stale artifacts are refused"
        )
    engine_override_used = engine is not run_backtest
    if engine_override_used and not allow_dirty:
        raise HistoricalStudyError(
            "a final historical study must use the production run_backtest engine; "
            "engine overrides require allow_dirty=True and invalidate the result"
        )
    git_state = _git_state(repository_root, allow_dirty=allow_dirty)
    _validate_commit(expected_commit)
    if git_state.commit != expected_commit:
        raise HistoricalStudyError("Git HEAD does not match expected_commit")
    loaded = _load_config(preregistration_path)
    _validate_sha256(
        expected_preregistration_sha256,
        field="expected_preregistration_sha256",
    )
    if loaded.sha256 != expected_preregistration_sha256:
        raise HistoricalStudyError(
            "pre-registration content does not match expected_preregistration_sha256"
        )
    code_identity_validated, code_file_hashes = _code_identity(
        repository_root,
        paths=(
            preregistration_path,
            repository_root / "pyproject.toml",
            repository_root / "uv.lock",
            *(_code_identity_paths or _default_code_identity_paths()),
        ),
        allow_dirty=allow_dirty,
    )
    research_validated = (
        not git_state.dirty and code_identity_validated and not engine_override_used
    )
    uv_lock_path = repository_root / "uv.lock"
    uv_lock_sha256 = _sha256_file(uv_lock_path) if uv_lock_path.is_file() else None
    runtime = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
    input_bars = tuple(bars)
    input_hash = source_data_sha256 or _bars_hash(input_bars)
    _validate_sha256(input_hash, field="source_data_sha256")
    if not source_data_filename or Path(source_data_filename).name != source_data_filename:
        raise HistoricalStudyError("source_data_filename must be one plain filename")
    selected = select_longest_contiguous_sequence(input_bars)
    dataset_config = _mapping(loaded.document.get("dataset"), field="dataset")
    cutoff = dataset_config.get("cutoff_exclusive_utc")
    if not isinstance(cutoff, str):
        raise HistoricalStudyError("dataset cutoff_exclusive_utc must be a string")
    execution_config = _mapping(loaded.document.get("execution"), field="execution")
    normalized_manifest_sha256 = _normalized_manifest_hash(
        normalized_manifest_path,
        expected_data_sha256=input_hash,
        expected_filename=source_data_filename,
        expected_row_count=len(input_bars),
        expected_first_date=input_bars[0].day,
        expected_last_date=input_bars[-1].day,
        expected_dataset=dataset_config,
        expected_execution=execution_config,
    )
    if len(selected) <= WARMUP_DAYS + 4:
        raise HistoricalStudyError(
            "selected clean history needs 250 warm-up days plus split evidence"
        )
    clean_hash = _bars_hash(selected)
    candles, references = bars_to_causal_inputs(selected)
    evaluation_bars = selected[WARMUP_DAYS:]
    boundaries = _split_boundaries(evaluation_bars)
    evaluation_start = candles[WARMUP_DAYS - 1].close_time + timedelta(minutes=15)
    costs = _cost_cases(loaded.document)
    primary_case = next(case for case in costs if case.name == "taker_taker_plus_10bps")
    primary_config = _backtest_config(loaded.document, primary_case)
    primary_strategy = _selected_strategy(loaded.document, windows=SELECTED_POLICY)

    def replay(
        strategy: MomentumTrendStrategy,
        config: BacktestConfig,
        *,
        candle_count: int | None = None,
    ) -> BacktestResult:
        selected_candles = candles if candle_count is None else candles[:candle_count]
        last_decision = selected_candles[-2].close_time + timedelta(minutes=15)
        selected_references = tuple(
            reference for reference in references if reference.decision_time <= last_decision
        )
        return engine(
            selected_candles,
            strategy,
            config,
            execution_references=selected_references,
            evaluation_start=evaluation_start,
        )

    primary = replay(primary_strategy, primary_config)
    comparator_results = {
        name: replay(_BinaryComparatorStrategy(name), primary_config)
        for name in COMPARATOR_ORDER[2:-1]
    }
    buy_hold_curve, buy_hold_costs, buy_hold_status, buy_hold_attempts = _buy_and_hold_curve(
        template=primary.equity_curve,
        evaluation_start=evaluation_start,
        references=references,
        config=primary_config,
    )
    runs: list[CurveRun] = [
        CurveRun("cad_cash", _cash_curve(primary.equity_curve), (), None),
        CurveRun(
            "fee_aware_btc_buy_and_hold",
            buy_hold_curve,
            (),
            None,
            buy_hold_costs,
        ),
    ]
    runs.extend(
        CurveRun(
            name,
            comparator_results[name].equity_curve,
            comparator_results[name].trades,
            comparator_results[name],
        )
        for name in COMPARATOR_ORDER[2:-1]
    )
    runs.append(CurveRun("frozen_v1", primary.equity_curve, primary.trades, primary))

    holdout_offset = boundaries[0].observation_count + boundaries[1].observation_count
    holdout_size = boundaries[2].observation_count
    pre_holdout_evaluation_count = holdout_offset
    pre_holdout_candle_count = WARMUP_DAYS + pre_holdout_evaluation_count
    cost_results = tuple(
        (
            case.name,
            ("full_evaluation_primary_frozen" if case.name == primary_case.name else "pre_holdout"),
            (
                primary
                if case.name == primary_case.name
                else replay(
                    primary_strategy,
                    _backtest_config(loaded.document, case),
                    candle_count=pre_holdout_candle_count,
                )
            ),
        )
        for case in costs
    )
    robustness = _mapping(loaded.document.get("robustness_grid"), field="robustness_grid")
    momentum_values = tuple(_sequence(robustness.get("momentum_days"), field="momentum_days"))
    trend_values = tuple(_sequence(robustness.get("trend_days"), field="trend_days"))
    volatility_values = tuple(_sequence(robustness.get("volatility_days"), field="volatility_days"))
    if (momentum_values, trend_values, volatility_values) != (
        (60, 90, 120),
        (150, 200, 250),
        (20, 30, 60),
    ):
        raise HistoricalStudyError("robustness grid differs from the frozen 27-point protocol")
    robustness_rows: list[dict[str, object]] = []
    for momentum, trend, volatility in product(momentum_values, trend_values, volatility_values):
        windows = (cast(int, momentum), cast(int, trend), cast(int, volatility))
        result = replay(
            _selected_strategy(loaded.document, windows=windows),
            primary_config,
            candle_count=pre_holdout_candle_count,
        )
        pre_holdout_metrics = _study_metrics(result.equity_curve, result.trades)
        exact = metrics_to_jsonable(pre_holdout_metrics)
        robustness_rows.append(
            {
                "momentum_days": windows[0],
                "trend_days": windows[1],
                "volatility_days": windows[2],
                "pre_registered_selected": windows == SELECTED_POLICY,
                "selection_performed": False,
                "pre_holdout_observation_count": pre_holdout_evaluation_count,
                "pre_holdout_total_return": exact["total_return"],
                "pre_holdout_sharpe": exact["sharpe"],
                "pre_holdout_max_drawdown": exact["max_drawdown"],
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_rows = _write_metrics(
        output_dir / "metrics.csv",
        runs=runs,
        cost_results=cost_results,
        boundaries=boundaries,
    )
    _write_calendar_returns(output_dir / "calendar_returns.csv", runs)
    _write_equity(output_dir / "daily_equity.csv", runs=runs, boundaries=boundaries)
    _write_decisions(output_dir / "decisions.csv", primary, boundaries)
    _write_fills(output_dir / "fills.csv", primary.trades, boundaries)
    _write_buy_and_hold_entries(
        output_dir / "buy_and_hold_entries.csv",
        buy_hold_attempts,
        boundaries,
    )
    _write_risk(output_dir / "risk_events.csv", primary.risk_events, boundaries)
    _write_csv(
        output_dir / "robustness.csv",
        (
            "momentum_days",
            "trend_days",
            "volatility_days",
            "pre_registered_selected",
            "selection_performed",
            "pre_holdout_observation_count",
            "pre_holdout_total_return",
            "pre_holdout_sharpe",
            "pre_holdout_max_drawdown",
        ),
        [
            [
                row[column]
                for column in (
                    "momentum_days",
                    "trend_days",
                    "volatility_days",
                    "pre_registered_selected",
                    "selection_performed",
                    "pre_holdout_observation_count",
                    "pre_holdout_total_return",
                    "pre_holdout_sharpe",
                    "pre_holdout_max_drawdown",
                )
            ]
            for row in robustness_rows
        ],
    )
    run_by_name = {run.name: run for run in runs}
    chart_paths = _write_charts(output_dir, runs=run_by_name, robustness_rows=robustness_rows)
    primary_metrics = _study_metrics(primary.equity_curve, primary.trades)
    holdout_boundary = boundaries[2]
    holdout_trades = tuple(
        trade
        for trade in primary.trades
        if holdout_boundary.first_day <= trade.execution_time.date() <= holdout_boundary.last_day
    )
    holdout_metrics = _study_metrics(
        _curve_slice(primary.equity_curve, holdout_offset, holdout_size),
        holdout_trades,
        include_cost_attribution=False,
    )
    expected_decisions = len(evaluation_bars)
    missing_references = sum(
        item.outcome.value == "no_fill_reference_unavailable" for item in primary.decisions
    )
    if len(primary.decisions) != expected_decisions:
        raise HistoricalStudyError("engine result does not align with the evaluation-day boundary")

    summary: dict[str, object] = {
        "schema_version": STUDY_SCHEMA,
        "study_id": loaded.document["study_id"],
        "evidence_scope": "historical_price_only",
        "engineering_status": (
            "ENGINEERING_VALIDATED" if research_validated else "RESEARCH_INVALIDATED"
        ),
        "profitability_status": "PROFITABILITY_NOT_ESTABLISHED",
        "evidence_statement": (
            "ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED"
            if research_validated
            else "RESEARCH_INVALIDATED"
        ),
        "live_trading_authorized": False,
        "repository": asdict(git_state),
        "code_identity": {
            "matches_head": code_identity_validated,
            "expected_commit": expected_commit,
            "engine_override_used": engine_override_used,
            "files": code_file_hashes,
        },
        "hashes": {
            "preregistration_sha256": loaded.sha256,
            "input_data_sha256": input_hash,
            "selected_clean_data_sha256": clean_hash,
            "normalized_manifest_sha256": normalized_manifest_sha256,
            "uv_lock_sha256": uv_lock_sha256,
        },
        "runtime": runtime,
        "dataset": {
            "selected_first_day": selected[0].day.isoformat(),
            "selected_last_day": selected[-1].day.isoformat(),
            "selected_day_count": len(selected),
            "discarded_day_count": len(input_bars) - len(selected),
            "warmup_day_count": WARMUP_DAYS,
            "evaluation_day_count": len(evaluation_bars),
            "missing_execution_reference_count": missing_references,
            "instrument_rules_applied": True,
        },
        "chronological_splits": [
            {
                "name": boundary.name,
                "first_day": boundary.first_day.isoformat(),
                "last_day": boundary.last_day.isoformat(),
                "observation_count": boundary.observation_count,
            }
            for boundary in boundaries
        ],
        "selected_policy": {
            "momentum_days": SELECTED_POLICY[0],
            "trend_days": SELECTED_POLICY[1],
            "volatility_days": SELECTED_POLICY[2],
            "selected_before_results": True,
        },
        "comparators": list(COMPARATOR_ORDER),
        "fee_aware_buy_and_hold": buy_hold_status,
        "holdout_evaluation_contract": {
            "selected_v1_primary_cost_case": primary_case.name,
            "prespecified_comparators": list(COMPARATOR_ORDER[:-1]),
            "neighboring_grid_access": False,
            "non_primary_cost_sensitivity_access": False,
            "statistical_significance_claimed": False,
        },
        "metrics": metrics_rows,
        "robustness": {
            "grid_size": len(robustness_rows),
            "optimization_performed": False,
            "selected_point_unchanged": True,
            "evidence_scope": "development_plus_validation_only",
            "frozen_holdout_accessed_by_neighboring_grid": False,
        },
        "cost_sensitivities": {
            "primary_case_scope": "full_evaluation_primary_frozen",
            "non_primary_case_scope": "development_plus_validation_only",
            "frozen_holdout_accessed_by_non_primary_cases": False,
        },
        "cost_accounting": {
            "full_curve_gross_pnl_includes_terminal_liquidation_costs": True,
            "full_curve_turnover_includes_terminal_liquidation_notional": True,
            "actual_fill_count_excludes_terminal_mark_to_liquidation": True,
            "split_gross_cost_attribution": "null_due_to_boundary_reserves",
        },
    }
    summary_path = output_dir / "summary.json"
    _write_json(summary_path, summary)
    report_path = output_dir / "report.md"
    _write_report(
        report_path,
        study_id=cast(str, loaded.document["study_id"]),
        git_state=git_state,
        config_sha256=loaded.sha256,
        input_hash=input_hash,
        clean_hash=clean_hash,
        selected=selected,
        discarded_count=len(input_bars) - len(selected),
        boundaries=boundaries,
        primary_metrics=primary_metrics,
        holdout_metrics=holdout_metrics,
        missing_references=missing_references,
        buy_hold_status=buy_hold_status,
        research_validated=research_validated,
        engine_override_used=engine_override_used,
    )

    artifact_paths = sorted(
        (
            output_dir / "summary.json",
            output_dir / "calendar_returns.csv",
            output_dir / "metrics.csv",
            output_dir / "daily_equity.csv",
            output_dir / "decisions.csv",
            output_dir / "fills.csv",
            output_dir / "buy_and_hold_entries.csv",
            output_dir / "risk_events.csv",
            output_dir / "robustness.csv",
            report_path,
            *chart_paths,
        ),
        key=lambda item: item.relative_to(output_dir).as_posix(),
    )
    artifact_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path) for path in artifact_paths
    }
    checksums_path = output_dir / "checksums.sha256"
    checksums_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in artifact_hashes.items()),
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schema_version": STUDY_SCHEMA,
        "study_id": loaded.document["study_id"],
        "repository": asdict(git_state),
        "code_identity": {
            "matches_head": code_identity_validated,
            "expected_commit": expected_commit,
            "engine_override_used": engine_override_used,
            "files": code_file_hashes,
        },
        "runtime": runtime,
        "dependency_lock": {
            "filename": "uv.lock" if uv_lock_sha256 is not None else None,
            "sha256": uv_lock_sha256,
        },
        "preregistration": {
            "filename": preregistration_path.name,
            "sha256": loaded.sha256,
        },
        "data": {
            "input_sha256": input_hash,
            "selected_clean_sha256": clean_hash,
            "normalized_manifest": (
                {
                    "filename": normalized_manifest_path.name,
                    "sha256": normalized_manifest_sha256,
                }
                if normalized_manifest_path is not None
                else None
            ),
            "clean_history_rule": "longest_contiguous_then_earliest_on_tie",
        },
        "artifacts": artifact_hashes,
        "checksums_sha256": _sha256_file(checksums_path),
        "determinism": {
            "generated_at_omitted": True,
            "randomness_used": False,
            "parameter_selection_from_results": False,
        },
    }
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    return StudyResult(
        output_dir=output_dir,
        summary_path=summary_path,
        manifest_path=manifest_path,
        checksums_path=checksums_path,
        config_sha256=loaded.sha256,
        input_data_sha256=input_hash,
        clean_data_sha256=clean_hash,
        git_state=git_state,
        selected_first_day=selected[0].day,
        selected_last_day=selected[-1].day,
        selected_day_count=len(selected),
        discarded_day_count=len(input_bars) - len(selected),
    )


def run_historical_study_from_csv(
    data_path: Path,
    *,
    preregistration_path: Path,
    output_dir: Path,
    repository_root: Path,
    allow_dirty: bool = False,
    normalized_manifest_path: Path,
    expected_commit: str,
    expected_preregistration_sha256: str,
) -> StudyResult:
    """Read a normalized file and retain both source-file and clean-slice hashes."""

    return run_historical_study(
        load_daily_bars(data_path),
        preregistration_path=preregistration_path,
        output_dir=output_dir,
        repository_root=repository_root,
        normalized_manifest_path=normalized_manifest_path,
        source_data_filename=data_path.name,
        expected_commit=expected_commit,
        expected_preregistration_sha256=expected_preregistration_sha256,
        allow_dirty=allow_dirty,
        source_data_sha256=_sha256_file(data_path),
    )
