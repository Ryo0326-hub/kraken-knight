"""Sanitized, read-only telemetry snapshots for the Streamlit dashboard.

The exporter is the only dashboard component allowed to read the trading
ledger.  It opens SQLite in read-only/query-only mode, selects a deliberately
small field allowlist, and writes an atomic JSON snapshot.  The Streamlit
process consumes only that snapshot; it never receives a ledger path, Kraken
credential, or production configuration file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Self

from kraken_knight.ledger import SCHEMA_VERSION

DASHBOARD_SNAPSHOT_SCHEMA = "kraken-knight-dashboard-v1"
DEFAULT_SIGNAL_LIMIT = 366
MAX_SIGNAL_LIMIT = 1_000
MAX_SNAPSHOT_BYTES = 5_000_000
_RUN_MODES = frozenset({"backtest", "paper", "shadow", "validate", "live"})
_OUTCOMES = frozenset({"TARGET_BTC", "TARGET_CASH"})
_STATES = frozenset({"btc", "cash"})
_RECONCILIATION_STATUSES = frozenset({"CLEAN", "DISARMED", "UNRESOLVED"})
_FORBIDDEN_KEYS = frozenset(
    {
        "account_binding_hash",
        "account_id",
        "api_key",
        "api_secret",
        "client_order_id",
        "configuration_hash",
        "credential",
        "decision_id",
        "input_data_hash",
        "order_id",
        "password",
        "private_key",
        "secret",
        "snapshot_id",
        "source_data_hash",
        "token",
        "trade_id",
    }
)
_FORBIDDEN_VALUE_FRAGMENTS = (
    "-----begin private key-----",
    "&api_key=",
    "&key=",
    "&secret=",
    "&token=",
    "?api_key=",
    "?key=",
    "?secret=",
    "?token=",
)


class DashboardSnapshotError(RuntimeError):
    """Raised when dashboard telemetry cannot be exported or trusted."""


@dataclass(frozen=True, slots=True)
class SignalTelemetry:
    """One secret-free strategy decision prepared for presentation."""

    strategy_date: date
    recorded_at_utc: datetime
    run_mode: str
    pair: str
    strategy_id: str
    outcome: str
    state: str
    reason: str
    target_weight: Decimal
    close_cad: Decimal | None
    sma_cad: Decimal | None
    momentum: Decimal | None
    annualized_volatility: Decimal | None
    exchange_writes: bool

    def to_payload(self) -> dict[str, object]:
        """Return the canonical JSON-ready representation."""

        return {
            "annualized_volatility": _decimal_text(self.annualized_volatility),
            "close_cad": _decimal_text(self.close_cad),
            "exchange_writes": self.exchange_writes,
            "momentum": _decimal_text(self.momentum),
            "outcome": self.outcome,
            "pair": self.pair,
            "reason": self.reason,
            "recorded_at_utc": _utc_text(self.recorded_at_utc),
            "run_mode": self.run_mode,
            "sma_cad": _decimal_text(self.sma_cad),
            "state": self.state,
            "strategy_date": self.strategy_date.isoformat(),
            "strategy_id": self.strategy_id,
            "target_weight": _decimal_text(self.target_weight),
        }

    @classmethod
    def from_payload(cls, value: object) -> Self:
        """Validate and decode one signal object from an untrusted snapshot."""

        row = _object(value, field="signal")
        _require_exact_keys(
            row,
            {
                "annualized_volatility",
                "close_cad",
                "exchange_writes",
                "momentum",
                "outcome",
                "pair",
                "reason",
                "recorded_at_utc",
                "run_mode",
                "sma_cad",
                "state",
                "strategy_date",
                "strategy_id",
                "target_weight",
            },
            field="signal",
        )
        run_mode = _nonempty_string(row["run_mode"], field="signal.run_mode")
        if run_mode not in _RUN_MODES:
            raise DashboardSnapshotError("signal.run_mode is unsupported")
        outcome = _nonempty_string(row["outcome"], field="signal.outcome")
        if outcome not in _OUTCOMES:
            raise DashboardSnapshotError("signal.outcome is unsupported")
        state = _nonempty_string(row["state"], field="signal.state")
        if state not in _STATES:
            raise DashboardSnapshotError("signal.state is unsupported")
        if (outcome == "TARGET_BTC") != (state == "btc"):
            raise DashboardSnapshotError("signal outcome and state disagree")
        target_weight = _decimal(row["target_weight"], field="signal.target_weight")
        if target_weight is None or not Decimal("0") <= target_weight <= Decimal("1"):
            raise DashboardSnapshotError("signal.target_weight must be within [0, 1]")
        exchange_writes = row["exchange_writes"]
        if not isinstance(exchange_writes, bool):
            raise DashboardSnapshotError("signal.exchange_writes must be a bool")
        return cls(
            strategy_date=_plain_date(row["strategy_date"], field="signal.strategy_date"),
            recorded_at_utc=_utc_datetime(row["recorded_at_utc"], field="signal.recorded_at_utc"),
            run_mode=run_mode,
            pair=_nonempty_string(row["pair"], field="signal.pair"),
            strategy_id=_nonempty_string(row["strategy_id"], field="signal.strategy_id"),
            outcome=outcome,
            state=state,
            reason=_nonempty_string(row["reason"], field="signal.reason"),
            target_weight=target_weight,
            close_cad=_nonnegative_decimal(row["close_cad"], field="signal.close_cad"),
            sma_cad=_nonnegative_decimal(row["sma_cad"], field="signal.sma_cad"),
            momentum=_decimal(row["momentum"], field="signal.momentum"),
            annualized_volatility=_nonnegative_decimal(
                row["annualized_volatility"], field="signal.annualized_volatility"
            ),
            exchange_writes=exchange_writes,
        )


@dataclass(frozen=True, slots=True)
class DashboardHealth:
    """Operational facts that do not identify the Kraken account."""

    ledger_integrity: str
    ledger_schema_version: int
    decision_count: int
    order_intent_count: int
    reconciliation_count: int
    latest_reconciliation_status: str | None
    latest_reconciliation_observed_at_utc: datetime | None
    latest_reconciliation_exchange_writes: bool | None

    def to_payload(self) -> dict[str, object]:
        return {
            "decision_count": self.decision_count,
            "latest_reconciliation_exchange_writes": (self.latest_reconciliation_exchange_writes),
            "latest_reconciliation_observed_at_utc": (
                None
                if self.latest_reconciliation_observed_at_utc is None
                else _utc_text(self.latest_reconciliation_observed_at_utc)
            ),
            "latest_reconciliation_status": self.latest_reconciliation_status,
            "ledger_integrity": self.ledger_integrity,
            "ledger_schema_version": self.ledger_schema_version,
            "order_intent_count": self.order_intent_count,
            "reconciliation_count": self.reconciliation_count,
        }

    @classmethod
    def from_payload(cls, value: object) -> Self:
        row = _object(value, field="health")
        _require_exact_keys(
            row,
            {
                "decision_count",
                "latest_reconciliation_exchange_writes",
                "latest_reconciliation_observed_at_utc",
                "latest_reconciliation_status",
                "ledger_integrity",
                "ledger_schema_version",
                "order_intent_count",
                "reconciliation_count",
            },
            field="health",
        )
        integrity = _nonempty_string(row["ledger_integrity"], field="health.ledger_integrity")
        if integrity != "ok":
            raise DashboardSnapshotError("health.ledger_integrity must be ok")
        schema_version = _nonnegative_int(
            row["ledger_schema_version"], field="health.ledger_schema_version"
        )
        if schema_version != SCHEMA_VERSION:
            raise DashboardSnapshotError("dashboard snapshot ledger schema is unsupported")
        status_value = row["latest_reconciliation_status"]
        status = (
            None
            if status_value is None
            else _nonempty_string(status_value, field="health.latest_reconciliation_status")
        )
        if status is not None and status not in _RECONCILIATION_STATUSES:
            raise DashboardSnapshotError("latest reconciliation status is unsupported")
        observed_value = row["latest_reconciliation_observed_at_utc"]
        observed_at = (
            None
            if observed_value is None
            else _utc_datetime(
                observed_value,
                field="health.latest_reconciliation_observed_at_utc",
            )
        )
        writes = row["latest_reconciliation_exchange_writes"]
        if writes is not None and not isinstance(writes, bool):
            raise DashboardSnapshotError(
                "health.latest_reconciliation_exchange_writes must be a bool or null"
            )
        if (status is None) != (observed_at is None) or (status is None) != (writes is None):
            raise DashboardSnapshotError("latest reconciliation health fields must be all-or-none")
        return cls(
            ledger_integrity=integrity,
            ledger_schema_version=schema_version,
            decision_count=_nonnegative_int(row["decision_count"], field="health.decision_count"),
            order_intent_count=_nonnegative_int(
                row["order_intent_count"], field="health.order_intent_count"
            ),
            reconciliation_count=_nonnegative_int(
                row["reconciliation_count"], field="health.reconciliation_count"
            ),
            latest_reconciliation_status=status,
            latest_reconciliation_observed_at_utc=observed_at,
            latest_reconciliation_exchange_writes=writes,
        )


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Validated telemetry contract consumed by the dashboard UI."""

    generated_at_utc: datetime
    signals: tuple[SignalTelemetry, ...]
    health: DashboardHealth

    @property
    def latest_signal(self) -> SignalTelemetry | None:
        return self.signals[-1] if self.signals else None

    def to_payload(self) -> dict[str, object]:
        latest = self.latest_signal
        payload: dict[str, object] = {
            "generated_at_utc": _utc_text(self.generated_at_utc),
            "health": self.health.to_payload(),
            "latest_signal": None if latest is None else latest.to_payload(),
            "performance": {
                "basis": "verified_account_equity",
                "message": (
                    "Verified live account-equity observations are not yet available; "
                    "signal prices are not portfolio P&L."
                ),
                "status": "unavailable",
            },
            "schema": DASHBOARD_SNAPSHOT_SCHEMA,
            "signal_history": [signal.to_payload() for signal in self.signals],
        }
        _assert_secret_free(payload)
        return payload

    @classmethod
    def from_payload(cls, value: object) -> Self:
        payload = _object(value, field="snapshot")
        _require_exact_keys(
            payload,
            {
                "generated_at_utc",
                "health",
                "latest_signal",
                "performance",
                "schema",
                "signal_history",
            },
            field="snapshot",
        )
        if payload["schema"] != DASHBOARD_SNAPSHOT_SCHEMA:
            raise DashboardSnapshotError("dashboard snapshot schema is unsupported")
        history_value = payload["signal_history"]
        if not isinstance(history_value, list):
            raise DashboardSnapshotError("snapshot.signal_history must be a list")
        if len(history_value) > MAX_SIGNAL_LIMIT:
            raise DashboardSnapshotError("snapshot.signal_history is too large")
        signals = tuple(SignalTelemetry.from_payload(row) for row in history_value)
        if any(
            previous.strategy_date >= current.strategy_date
            for previous, current in pairwise(signals)
        ):
            raise DashboardSnapshotError("signal history must be strictly chronological")
        latest_value = payload["latest_signal"]
        if latest_value is None:
            if signals:
                raise DashboardSnapshotError("latest signal is missing")
        else:
            latest = SignalTelemetry.from_payload(latest_value)
            if not signals or latest != signals[-1]:
                raise DashboardSnapshotError("latest signal does not match signal history")
        if signals:
            expected_scope = (signals[-1].pair, signals[-1].strategy_id)
            if any((signal.pair, signal.strategy_id) != expected_scope for signal in signals):
                raise DashboardSnapshotError("signal history mixes pair or strategy scopes")
        performance = _object(payload["performance"], field="performance")
        _require_exact_keys(performance, {"basis", "message", "status"}, field="performance")
        if performance["status"] != "unavailable":
            raise DashboardSnapshotError("unsupported performance telemetry status")
        if performance["basis"] != "verified_account_equity":
            raise DashboardSnapshotError("unsupported performance telemetry basis")
        _nonempty_string(performance["message"], field="performance.message")
        _assert_secret_free(payload)
        return cls(
            generated_at_utc=_utc_datetime(
                payload["generated_at_utc"], field="snapshot.generated_at_utc"
            ),
            signals=signals,
            health=DashboardHealth.from_payload(payload["health"]),
        )


def export_dashboard_snapshot(
    *,
    ledger_path: Path,
    generated_at: datetime | None = None,
    signal_limit: int = DEFAULT_SIGNAL_LIMIT,
) -> DashboardSnapshot:
    """Build a sanitized snapshot from an initialized ledger without writing it."""

    if not isinstance(ledger_path, Path):
        raise TypeError("ledger_path must be a pathlib.Path")
    if isinstance(signal_limit, bool) or not 1 <= signal_limit <= MAX_SIGNAL_LIMIT:
        raise ValueError(f"signal_limit must be within [1, {MAX_SIGNAL_LIMIT}]")
    timestamp = datetime.now(UTC) if generated_at is None else generated_at
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("generated_at must be timezone-aware UTC")
    if not ledger_path.is_file():
        raise DashboardSnapshotError("telemetry ledger is missing")

    uri = f"{ledger_path.resolve(strict=True).as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != SCHEMA_VERSION:
                raise DashboardSnapshotError("telemetry ledger schema is unsupported")
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower()
            if integrity != "ok":
                raise DashboardSnapshotError("telemetry ledger integrity check failed")
            decision_count = _count(connection, "daily_decisions")
            intent_count = _count(connection, "order_intents")
            reconciliation_count = _count(connection, "reconciliation_snapshots")
            latest_scope = connection.execute(
                """
                SELECT account_id, strategy_id, pair
                  FROM daily_decisions
                 ORDER BY strategy_date DESC, recorded_at_utc DESC
                 LIMIT 1
                """
            ).fetchone()
            if latest_scope is None:
                decision_rows: list[sqlite3.Row] = []
            else:
                decision_rows = connection.execute(
                    """
                    SELECT strategy_id, strategy_date, run_mode, pair, outcome,
                           details_json, recorded_at_utc
                      FROM daily_decisions
                     WHERE account_id = ? AND strategy_id = ? AND pair = ?
                     ORDER BY strategy_date DESC, recorded_at_utc DESC
                     LIMIT ?
                    """,
                    (
                        latest_scope["account_id"],
                        latest_scope["strategy_id"],
                        latest_scope["pair"],
                        signal_limit,
                    ),
                ).fetchall()
            if latest_scope is None:
                reconciliation_row = None
            else:
                reconciliation_row = connection.execute(
                    """
                    SELECT status, observed_at_utc, exchange_writes
                      FROM reconciliation_snapshots
                     WHERE account_id = ? AND pair = ?
                     ORDER BY observed_at_utc DESC, recorded_at_utc DESC
                     LIMIT 1
                    """,
                    (latest_scope["account_id"], latest_scope["pair"]),
                ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise DashboardSnapshotError("telemetry ledger could not be read safely") from exc

    signals = tuple(_signal_from_row(row) for row in reversed(decision_rows))
    status: str | None = None
    observed_at: datetime | None = None
    reconciliation_writes: bool | None = None
    if reconciliation_row is not None:
        status = _nonempty_string(reconciliation_row["status"], field="reconciliation.status")
        if status not in _RECONCILIATION_STATUSES:
            raise DashboardSnapshotError("stored reconciliation status is unsupported")
        observed_at = _utc_datetime(
            reconciliation_row["observed_at_utc"], field="reconciliation.observed_at_utc"
        )
        raw_writes = reconciliation_row["exchange_writes"]
        if raw_writes not in (0, 1):
            raise DashboardSnapshotError("stored reconciliation write flag is invalid")
        reconciliation_writes = bool(raw_writes)

    snapshot = DashboardSnapshot(
        generated_at_utc=timestamp.astimezone(UTC),
        signals=signals,
        health=DashboardHealth(
            ledger_integrity=integrity,
            ledger_schema_version=schema_version,
            decision_count=decision_count,
            order_intent_count=intent_count,
            reconciliation_count=reconciliation_count,
            latest_reconciliation_status=status,
            latest_reconciliation_observed_at_utc=observed_at,
            latest_reconciliation_exchange_writes=reconciliation_writes,
        ),
    )
    # Round-trip through the same validator used by the UI before publishing.
    return DashboardSnapshot.from_payload(snapshot.to_payload())


def write_dashboard_snapshot(
    snapshot: DashboardSnapshot,
    *,
    output_path: Path,
    ledger_path: Path | None = None,
) -> None:
    """Atomically publish a mode-0640 telemetry file outside the trading state."""

    if not isinstance(snapshot, DashboardSnapshot):
        raise TypeError("snapshot must be a DashboardSnapshot")
    if not isinstance(output_path, Path):
        raise TypeError("output_path must be a pathlib.Path")
    if ledger_path is not None and output_path.resolve() == ledger_path.resolve():
        raise DashboardSnapshotError("dashboard output must not replace the trading ledger")
    if not output_path.parent.is_dir():
        raise DashboardSnapshotError("dashboard output directory must be pre-created")
    payload = snapshot.to_payload()
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise DashboardSnapshotError("dashboard snapshot exceeds the size limit")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o640)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
        temporary_path = None
        directory_descriptor = os.open(
            output_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_dashboard_snapshot(path: Path) -> DashboardSnapshot:
    """Load and strictly validate a sanitized dashboard snapshot."""

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            file_status = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_status.st_mode):
                raise DashboardSnapshotError("dashboard telemetry must be a regular file")
            if file_status.st_size > MAX_SNAPSHOT_BYTES:
                raise DashboardSnapshotError("dashboard telemetry exceeds the size limit")
            encoded = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise DashboardSnapshotError("dashboard telemetry exceeds the size limit")
        value = json.loads(encoded.decode("utf-8"))
    except DashboardSnapshotError:
        raise
    except FileNotFoundError as exc:
        raise DashboardSnapshotError("dashboard telemetry is not available yet") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardSnapshotError("dashboard telemetry is malformed or unreadable") from exc
    return DashboardSnapshot.from_payload(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kraken-knight-dashboard-export",
        description="Export a sanitized, read-only Kraken Knight dashboard snapshot.",
    )
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signal-limit", type=int, default=DEFAULT_SIGNAL_LIMIT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the isolated telemetry exporter."""

    arguments = build_parser().parse_args(argv)
    try:
        snapshot = export_dashboard_snapshot(
            ledger_path=arguments.ledger,
            signal_limit=arguments.signal_limit,
        )
        write_dashboard_snapshot(
            snapshot,
            output_path=arguments.output,
            ledger_path=arguments.ledger,
        )
    except (DashboardSnapshotError, OSError, TypeError, ValueError) as exc:
        print(f"kraken-knight-dashboard-export: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "decision_count": snapshot.health.decision_count,
                "exchange_writes": False,
                "generated_at_utc": _utc_text(snapshot.generated_at_utc),
                "operation": "dashboard-export",
                "performance_status": "unavailable",
                "signal_count": len(snapshot.signals),
            },
            sort_keys=True,
        )
    )
    return 0


def _signal_from_row(row: sqlite3.Row) -> SignalTelemetry:
    try:
        details_value = json.loads(str(row["details_json"]))
    except json.JSONDecodeError as exc:
        raise DashboardSnapshotError("stored signal details are malformed") from exc
    details = _object(details_value, field="stored signal details")
    state = _nonempty_string(details.get("state"), field="stored signal state")
    reason = _nonempty_string(details.get("reason"), field="stored signal reason")
    target_weight = _decimal(details.get("target_weight"), field="stored target weight")
    if target_weight is None:
        raise DashboardSnapshotError("stored target weight is missing")
    exchange_writes = details.get("exchange_writes")
    if not isinstance(exchange_writes, bool):
        raise DashboardSnapshotError("stored exchange-write flag is invalid")
    return SignalTelemetry(
        strategy_date=_plain_date(row["strategy_date"], field="stored strategy date"),
        recorded_at_utc=_utc_datetime(row["recorded_at_utc"], field="stored recorded time"),
        run_mode=_nonempty_string(row["run_mode"], field="stored run mode"),
        pair=_nonempty_string(row["pair"], field="stored pair"),
        strategy_id=_nonempty_string(row["strategy_id"], field="stored strategy ID"),
        outcome=_nonempty_string(row["outcome"], field="stored outcome"),
        state=state,
        reason=reason,
        target_weight=target_weight,
        close_cad=_nonnegative_decimal(details.get("close"), field="stored close"),
        sma_cad=_nonnegative_decimal(details.get("sma"), field="stored SMA"),
        momentum=_decimal(details.get("momentum"), field="stored momentum"),
        annualized_volatility=_nonnegative_decimal(
            details.get("annualized_volatility"), field="stored volatility"
        ),
        exchange_writes=exchange_writes,
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    # Table names are internal constants, never operator input.
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DashboardSnapshotError(f"{field} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise DashboardSnapshotError(f"{field} fields do not match the telemetry schema")


def _nonempty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DashboardSnapshotError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 200 or any(ord(character) < 32 for character in normalized):
        raise DashboardSnapshotError(f"{field} contains unsupported characters")
    return normalized


def _plain_date(value: object, *, field: str) -> date:
    text = _nonempty_string(value, field=field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise DashboardSnapshotError(f"{field} must be an ISO date") from None
    if parsed.isoformat() != text:
        raise DashboardSnapshotError(f"{field} must be a canonical ISO date")
    return parsed


def _utc_datetime(value: object, *, field: str) -> datetime:
    text = _nonempty_string(value, field=field)
    if not text.endswith("Z"):
        raise DashboardSnapshotError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        raise DashboardSnapshotError(f"{field} must be a canonical UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise DashboardSnapshotError(f"{field} must be a canonical UTC timestamp")
    return parsed


def _decimal(value: object, *, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DashboardSnapshotError(f"{field} must be a decimal string or null")
    if len(value) > 64:
        raise DashboardSnapshotError(f"{field} exceeds the decimal size limit")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise DashboardSnapshotError(f"{field} is not a valid decimal") from None
    if not parsed.is_finite():
        raise DashboardSnapshotError(f"{field} must be finite")
    if abs(parsed) > Decimal("1e15"):
        raise DashboardSnapshotError(f"{field} exceeds the decimal magnitude limit")
    return parsed


def _nonnegative_decimal(value: object, *, field: str) -> Decimal | None:
    parsed = _decimal(value, field=field)
    if parsed is not None and parsed < 0:
        raise DashboardSnapshotError(f"{field} cannot be negative")
    return parsed


def _nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DashboardSnapshotError(f"{field} must be a nonnegative int")
    return value


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _assert_secret_free(value: object, *, path: str = "snapshot") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = key.strip().lower().replace("-", "_")
            if normalized_key in _FORBIDDEN_KEYS:
                raise DashboardSnapshotError(f"sensitive field is prohibited in {path}")
            _assert_secret_free(child, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _assert_secret_free(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        normalized_value = value.strip().lower()
        if any(fragment in normalized_value for fragment in _FORBIDDEN_VALUE_FRAGMENTS):
            raise DashboardSnapshotError(f"sensitive value is prohibited in {path}")


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
