"""Append-only SQLite ledger for daily decisions and order intents.

The ledger records economic intent only.  It has no Kraken client and cannot
submit an order.  Stable identities plus one-decision/one-intent-scope unique
constraints make retries idempotent and make conflicting replays fail closed.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from kraken_knight.config import RunMode
from kraken_knight.domain import Number, to_decimal
from kraken_knight.provenance import canonical_json_bytes, sha256_json

SCHEMA_VERSION = 2
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_SECRET_VALUE_FRAGMENTS = (
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


class LedgerError(RuntimeError):
    """Base class for persistence and schema failures."""


class LedgerConflict(LedgerError):
    """Raised when a retry conflicts with an immutable stored record."""


class Ledger:
    """SQLite-backed append-only decision and order-intent ledger."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        self.path = path

    def initialize(self) -> None:
        """Create or verify the database schema and WAL configuration."""

        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)

        with self._connect(write=True) as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version not in {0, SCHEMA_VERSION}:
                raise LedgerError(
                    f"unsupported ledger schema version {current_version}; "
                    f"expected {SCHEMA_VERSION}"
                )
            journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            if journal_mode.lower() != "wal":
                raise LedgerError("SQLite did not enable WAL journal mode")

            try:
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS daily_decisions (
                    decision_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_date TEXT NOT NULL,
                    configuration_hash TEXT NOT NULL,
                    input_data_hash TEXT NOT NULL,
                    run_mode TEXT NOT NULL CHECK (
                        run_mode IN ('backtest', 'paper', 'shadow', 'validate', 'live')
                    ),
                    pair TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE (account_id, strategy_id, strategy_date)
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL REFERENCES daily_decisions(decision_id),
                    intent_index INTEGER NOT NULL CHECK (intent_index = 0),
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    order_type TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    limit_price TEXT,
                    time_in_force TEXT NOT NULL,
                    post_only INTEGER NOT NULL CHECK (post_only IN (0, 1)),
                    details_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE (decision_id)
                );

                CREATE INDEX IF NOT EXISTS daily_decisions_by_date
                    ON daily_decisions(strategy_date, strategy_id, account_id);
                CREATE INDEX IF NOT EXISTS order_intents_by_decision
                    ON order_intents(decision_id, intent_index);

                CREATE TRIGGER IF NOT EXISTS daily_decisions_no_update
                BEFORE UPDATE ON daily_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'daily_decisions is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_decisions_no_delete
                BEFORE DELETE ON daily_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'daily_decisions is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS order_intents_no_update
                BEFORE UPDATE ON order_intents
                BEGIN
                    SELECT RAISE(ABORT, 'order_intents is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS order_intents_no_delete
                BEFORE DELETE ON order_intents
                BEGIN
                    SELECT RAISE(ABORT, 'order_intents is immutable');
                END;
                    """
                )
            except sqlite3.DatabaseError as exc:
                raise LedgerError("ledger schema is incompatible with version 2") from exc
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _verify_schema(connection)
            connection.commit()

        os.chmod(self.path, 0o600)

    def append_daily_decision(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_date: date,
        configuration_hash: str,
        input_data_hash: str,
        run_mode: RunMode,
        pair: str,
        outcome: str,
        code_version: str,
        details: Mapping[str, object] | None = None,
        recorded_at: datetime | None = None,
    ) -> str:
        """Append one daily decision or return its existing deterministic ID.

        A retry is idempotent only when every immutable field matches.  A second
        outcome for the same account/strategy/date raises
        :class:`LedgerConflict`, even if its input hash changed.
        """

        self._require_initialized()
        _require_nonempty(account_id, field="account_id")
        _require_nonempty(strategy_id, field="strategy_id")
        _require_plain_date(strategy_date)
        _require_sha256(configuration_hash, field="configuration_hash")
        _require_sha256(input_data_hash, field="input_data_hash")
        if not isinstance(run_mode, RunMode):
            raise TypeError("run_mode must be a RunMode")
        _require_nonempty(pair, field="pair")
        _require_nonempty(outcome, field="outcome")
        _require_nonempty(code_version, field="code_version")

        safe_details = {} if details is None else dict(details)
        _assert_safe_details(safe_details)
        details_json = canonical_json_bytes(safe_details).decode("utf-8")
        identity = {
            "account_id": account_id,
            "configuration_hash": configuration_hash.lower(),
            "input_data_hash": input_data_hash.lower(),
            "pair": pair,
            "run_mode": run_mode.value,
            "strategy_date": strategy_date,
            "strategy_id": strategy_id,
        }
        decision_id = f"decision_{sha256_json(identity)}"
        values: tuple[object, ...] = (
            decision_id,
            account_id,
            strategy_id,
            strategy_date.isoformat(),
            configuration_hash.lower(),
            input_data_hash.lower(),
            run_mode.value,
            pair,
            outcome,
            code_version,
            details_json,
            _utc_timestamp(recorded_at),
        )

        with self._connect(write=True) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO daily_decisions (
                    decision_id, account_id, strategy_id, strategy_date,
                    configuration_hash, input_data_hash, run_mode, pair,
                    outcome, code_version, details_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            if cursor.rowcount == 1:
                return decision_id

            existing = connection.execute(
                """
                SELECT decision_id, configuration_hash, input_data_hash, outcome,
                       code_version, details_json
                  FROM daily_decisions
                 WHERE account_id = ? AND strategy_id = ? AND strategy_date = ?
                """,
                (
                    account_id,
                    strategy_id,
                    strategy_date.isoformat(),
                ),
            ).fetchone()
            if existing is None:
                raise LedgerConflict("deterministic decision identity collision")

            existing_values = (
                str(existing["decision_id"]),
                str(existing["configuration_hash"]),
                str(existing["input_data_hash"]),
                str(existing["outcome"]),
                str(existing["code_version"]),
                str(existing["details_json"]),
            )
            expected_values = (
                decision_id,
                configuration_hash.lower(),
                input_data_hash.lower(),
                outcome,
                code_version,
                details_json,
            )
            if existing_values != expected_values:
                raise LedgerConflict(
                    "a different immutable decision already exists for this daily scope"
                )
            return decision_id

    def append_order_intent(
        self,
        *,
        decision_id: str,
        intent_index: int,
        side: str,
        order_type: str,
        quantity: Number,
        limit_price: Number | None,
        time_in_force: str = "GTC",
        post_only: bool = True,
        details: Mapping[str, object] | None = None,
        recorded_at: datetime | None = None,
    ) -> str:
        """Append an immutable order intent without contacting an exchange."""

        self._require_initialized()
        _require_nonempty(decision_id, field="decision_id")
        if isinstance(intent_index, bool) or not isinstance(intent_index, int):
            raise TypeError("intent_index must be an int")
        if intent_index != 0:
            raise ValueError("V1 permits exactly one economic intent at index 0")
        normalized_side = side.strip().lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        _require_nonempty(order_type, field="order_type")
        normalized_order_type = order_type.strip().lower()
        if normalized_order_type != "limit":
            raise ValueError("V1 order intents must use bounded limit orders")
        normalized_quantity = to_decimal(quantity, field="quantity")
        if normalized_quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        if limit_price is None:
            raise ValueError("limit_price is required for a bounded limit order")
        normalized_limit_price = to_decimal(limit_price, field="limit_price")
        if normalized_limit_price <= 0:
            raise ValueError("limit_price must be greater than zero")
        _require_nonempty(time_in_force, field="time_in_force")
        normalized_time_in_force = time_in_force.strip().upper()
        if normalized_time_in_force not in {"GTC", "IOC"}:
            raise ValueError("time_in_force must be GTC or IOC")
        if not isinstance(post_only, bool):
            raise TypeError("post_only must be a bool")
        if post_only and normalized_time_in_force != "GTC":
            raise ValueError("post-only order intents must use GTC")

        safe_details = {} if details is None else dict(details)
        _assert_safe_details(safe_details)
        details_json = canonical_json_bytes(safe_details).decode("utf-8")
        identity = {
            "decision_id": decision_id,
            "intent_index": intent_index,
            "limit_price": normalized_limit_price,
            "order_type": normalized_order_type,
            "post_only": post_only,
            "quantity": normalized_quantity,
            "side": normalized_side,
            "time_in_force": normalized_time_in_force,
        }
        digest = sha256_json(identity)
        intent_id = f"intent_{digest}"
        # Kraken WebSocket v2 accepts at most 18 ASCII characters for the
        # free-text ``cl_ord_id`` form.  The full digest remains in intent_id;
        # this deterministic, namespaced projection is only the exchange key.
        client_order_id = f"kk{digest[:16]}"
        values: tuple[object, ...] = (
            intent_id,
            client_order_id,
            decision_id,
            intent_index,
            normalized_side,
            normalized_order_type,
            format(normalized_quantity, "f"),
            None if normalized_limit_price is None else format(normalized_limit_price, "f"),
            normalized_time_in_force,
            int(post_only),
            details_json,
            _utc_timestamp(recorded_at),
        )

        with self._connect(write=True) as connection, connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO order_intents (
                        intent_id, client_order_id, decision_id, intent_index,
                        side, order_type, quantity, limit_price, time_in_force,
                        post_only, details_json, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("order intent references an unknown decision") from exc
            if cursor.rowcount == 1:
                return intent_id

            existing = connection.execute(
                """
                SELECT intent_id, client_order_id, side, order_type, quantity,
                       limit_price, time_in_force, post_only, details_json
                  FROM order_intents
                 WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            if existing is None:
                raise LedgerConflict("deterministic order-intent identity collision")

            existing_values = (
                str(existing["intent_id"]),
                str(existing["client_order_id"]),
                str(existing["side"]),
                str(existing["order_type"]),
                str(existing["quantity"]),
                None if existing["limit_price"] is None else str(existing["limit_price"]),
                str(existing["time_in_force"]),
                int(existing["post_only"]),
                str(existing["details_json"]),
            )
            expected_values = (
                intent_id,
                client_order_id,
                normalized_side,
                normalized_order_type,
                format(normalized_quantity, "f"),
                None if normalized_limit_price is None else format(normalized_limit_price, "f"),
                normalized_time_in_force,
                int(post_only),
                details_json,
            )
            if existing_values != expected_values:
                raise LedgerConflict(
                    "a different immutable order intent already exists for this decision"
                )
            return intent_id

    def status(self) -> dict[str, object]:
        """Return a secret-free, read-only operational summary."""

        if not self.path.is_file():
            return {
                "decision_count": 0,
                "initialized": False,
                "integrity": "not_initialized",
                "intent_count": 0,
                "journal_mode": None,
                "latest_decision": None,
                "schema_version": None,
            }

        with self._connect(write=False) as connection:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            decision_count = int(
                connection.execute("SELECT COUNT(*) FROM daily_decisions").fetchone()[0]
            )
            intent_count = int(
                connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
            )
            latest = connection.execute(
                """
                SELECT decision_id, strategy_date, outcome, run_mode, pair
                  FROM daily_decisions
                 ORDER BY strategy_date DESC, recorded_at_utc DESC
                 LIMIT 1
                """
            ).fetchone()

        latest_decision: dict[str, object] | None = None
        if latest is not None:
            latest_decision = {
                "decision_id": str(latest["decision_id"]),
                "outcome": str(latest["outcome"]),
                "pair": str(latest["pair"]),
                "run_mode": str(latest["run_mode"]),
                "strategy_date": str(latest["strategy_date"]),
            }
        return {
            "decision_count": decision_count,
            "initialized": True,
            "integrity": integrity,
            "intent_count": intent_count,
            "journal_mode": journal_mode,
            "latest_decision": latest_decision,
            "schema_version": schema_version,
        }

    def _require_initialized(self) -> None:
        if not self.path.is_file():
            raise LedgerError("ledger is not initialized")

    @contextmanager
    def _connect(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if write:
            connection = sqlite3.connect(self.path, timeout=5.0)
        else:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            else:
                connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()


def _require_nonempty(value: str, *, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a str")
    if not value.strip():
        raise ValueError(f"{field} cannot be empty")


def _require_plain_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("strategy_date must be a date, not a datetime")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a str")
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field} must be a 64-character hexadecimal SHA-256 digest")


def _utc_timestamp(value: datetime | None) -> str:
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("recorded_at must be timezone-aware UTC")
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _assert_safe_details(value: object, *, path: str = "details") -> None:
    """Reject secret-shaped field names before data reaches durable storage."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            normalized_key = key.strip().lower().replace("-", "_")
            if any(fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS):
                raise ValueError(f"secret-bearing field names are prohibited in {path}")
            _assert_safe_details(child, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_safe_details(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        normalized_value = value.strip().lower()
        if any(fragment in normalized_value for fragment in _SECRET_VALUE_FRAGMENTS):
            raise ValueError(f"secret-bearing values are prohibited in {path}")


def _verify_schema(connection: sqlite3.Connection) -> None:
    """Reject databases whose declared version hides an incompatible schema."""

    required_objects = {
        "daily_decisions": "table",
        "daily_decisions_by_date": "index",
        "daily_decisions_no_delete": "trigger",
        "daily_decisions_no_update": "trigger",
        "order_intents": "table",
        "order_intents_by_decision": "index",
        "order_intents_no_delete": "trigger",
        "order_intents_no_update": "trigger",
    }
    rows = connection.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    objects = {str(row[0]): (str(row[1]), str(row[2] or "")) for row in rows}
    for name, expected_type in required_objects.items():
        actual = objects.get(name)
        if actual is None or actual[0] != expected_type:
            raise LedgerError(f"ledger schema is missing required {expected_type} {name}")

    normalized_daily_sql = " ".join(objects["daily_decisions"][1].split()).lower()
    normalized_intent_sql = " ".join(objects["order_intents"][1].split()).lower()
    required_daily_fragments = (
        "decision_id text primary key",
        "unique (account_id, strategy_id, strategy_date)",
    )
    required_intent_fragments = (
        "client_order_id text not null unique",
        "check (intent_index = 0)",
        "unique (decision_id)",
    )
    if any(fragment not in normalized_daily_sql for fragment in required_daily_fragments):
        raise LedgerError("daily_decisions schema is incompatible with version 2")
    if any(fragment not in normalized_intent_sql for fragment in required_intent_fragments):
        raise LedgerError("order_intents schema is incompatible with version 2")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise LedgerError("ledger schema has a foreign-key integrity failure")
