"""Bounded daily BTC/CAD strategy evaluation with no exchange-write path.

The daily job is intentionally account-blind.  It reads only Kraken's public
OHLC endpoint, evaluates the frozen production strategy, and appends one
immutable shadow decision.  It cannot calculate an account order and never
creates an order intent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from kraken_knight.config import ConfigError, RunMode, Settings
from kraken_knight.domain import Candle
from kraken_knight.ledger import Ledger
from kraken_knight.market_data import (
    BTC_CAD_RESPONSE_KEYS,
    KrakenOhlcBatch,
    KrakenPublicClient,
    MarketDataError,
    validate_batch_freshness,
    validate_daily_sequence,
)
from kraken_knight.provenance import sha256_json
from kraken_knight.risk import DrawdownPolicyMode
from kraken_knight.strategy import MomentumTrendStrategy, PositionState, StrategyPolicy

DAILY_JOB_CODE_VERSION = "daily-shadow-v3-no-drawdown-v1"
KRAKEN_REQUEST_PAIR = "XBTCAD"
PRODUCTION_RELEASES_ROOT = Path("/opt/kraken-knight/releases")
_ALLOWED_MODES = frozenset({RunMode.PAPER, RunMode.SHADOW, RunMode.VALIDATE})
_RELEASE_ID_PATTERN = re.compile(r"[0-9a-f]{40}")
_ALLOWED_DAILY_ENVIRONMENT_KEYS = frozenset(
    {
        "KRAKEN_KNIGHT_ACCOUNT_ID",
        "KRAKEN_KNIGHT_MODE",
        "KRAKEN_KNIGHT_PAIR",
        "KRAKEN_KNIGHT_RELEASE_ID",
        "KRAKEN_KNIGHT_STATE_DIR",
        "KRAKEN_KNIGHT_STRATEGY_ID",
    }
)


class DailyJobError(RuntimeError):
    """Raised when the bounded daily decision cannot run safely."""


class DailyOhlcSource(Protocol):
    """Public market-data port used by the daily job."""

    def fetch_daily_ohlc(self, *, pair: str = KRAKEN_REQUEST_PAIR) -> KrakenOhlcBatch:
        """Return a BTC/CAD batch whose mutable tail is isolated."""


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return the current aware UTC time through an injectable clock boundary."""

    return datetime.now(UTC)


def release_id_from_env(environ: Mapping[str, str] | None = None) -> str:
    """Read a reviewed release identity without including it in ``Settings``.

    The daily command fails closed when the deploy has not bound the process to
    an immutable release.  Other inspection and reconciliation commands remain
    usable without this variable.
    """

    values = os.environ if environ is None else environ
    release_id = values.get("KRAKEN_KNIGHT_RELEASE_ID", "").strip()
    if not release_id:
        raise DailyJobError("KRAKEN_KNIGHT_RELEASE_ID is required for the daily decision command")
    if _RELEASE_ID_PATTERN.fullmatch(release_id) is None:
        raise DailyJobError("KRAKEN_KNIGHT_RELEASE_ID must be a full lowercase Git commit SHA")
    return release_id


def validate_daily_environment(environ: Mapping[str, str] | None = None) -> None:
    """Reject every unrecognized application variable before parsing settings.

    This exact process-boundary allowlist prevents today's private or research
    credentials—and future ``KRAKEN_KNIGHT_*`` fields—from silently entering
    the public-data service environment.
    """

    values = os.environ if environ is None else environ
    unexpected = sorted(
        key
        for key in values
        if key.startswith("KRAKEN_KNIGHT_") and key not in _ALLOWED_DAILY_ENVIRONMENT_KEYS
    )
    if unexpected:
        raise DailyJobError(
            "daily command rejects unsupported KRAKEN_KNIGHT_* environment fields: "
            + ", ".join(unexpected)
        )


def execute_daily_decision(
    *,
    settings: Settings,
    ledger: Ledger,
    release_id: str,
    release_path: Path | None = None,
    releases_root: Path = PRODUCTION_RELEASES_ROOT,
    public_client: DailyOhlcSource | None = None,
    clock: Clock = utc_now,
) -> dict[str, object]:
    """Evaluate and record one idempotent, exchange-write-free daily decision."""

    validate_daily_settings(settings)
    if _RELEASE_ID_PATTERN.fullmatch(release_id) is None:
        raise DailyJobError("release_id must be a full lowercase Git commit SHA")
    verify_release_binding(
        release_id=release_id,
        release_path=release_path or Path.cwd(),
        releases_root=releases_root,
    )

    client = public_client if public_client is not None else KrakenPublicClient()
    batch = client.fetch_daily_ohlc(pair=KRAKEN_REQUEST_PAIR)
    evaluated_at = clock()
    if evaluated_at.tzinfo is None:
        raise DailyJobError("daily clock must return a timezone-aware time")
    evaluated_at = evaluated_at.astimezone(UTC)
    _validate_decision_batch(batch, evaluated_at=evaluated_at)

    policy = StrategyPolicy(
        volatility_target=settings.risk.target_annual_volatility,
        max_weight=settings.risk.max_exposure_fraction,
    )
    strategy = MomentumTrendStrategy(policy)
    decision = strategy.evaluate(batch.completed)
    if not decision.usable_data:
        raise DailyJobError(f"strategy rejected completed market data: {decision.reason.value}")
    if decision.signal_open_time is None or decision.signal_close_time is None:
        raise DailyJobError("strategy did not identify a completed signal candle")

    configuration_hash = _configuration_hash(
        settings=settings,
        strategy_policy_hash=decision.policy_hash,
        release_id=release_id,
    )
    input_data_hash = _daily_input_hash(batch=batch, strategy_input_hash=decision.input_data_hash)
    outcome = "TARGET_BTC" if decision.state is PositionState.BTC else "TARGET_CASH"
    code_version = f"{DAILY_JOB_CODE_VERSION}+{release_id}"
    details = {
        "annualized_volatility": _optional_decimal(decision.annualized_volatility),
        "close": _optional_decimal(decision.close),
        "drawdown_policy_mode": settings.risk.drawdown_policy_mode.value,
        "exchange_writes": False,
        "momentum": _optional_decimal(decision.momentum),
        "order_intent_created": False,
        "policy_hash": decision.policy_hash,
        "reason": decision.reason.value,
        "signal_close_time": decision.signal_close_time,
        "signal_open_time": decision.signal_open_time,
        "sma": _optional_decimal(decision.sma),
        "state": decision.state.value,
        "target_weight": format(decision.target_weight, "f"),
        "usable_data": decision.usable_data,
    }
    decision_id = ledger.append_daily_decision(
        account_id=settings.account_id,
        strategy_id=settings.strategy_id,
        strategy_date=decision.signal_open_time.date(),
        configuration_hash=configuration_hash,
        input_data_hash=input_data_hash,
        run_mode=settings.mode,
        pair=settings.pair,
        outcome=outcome,
        code_version=code_version,
        details=details,
        recorded_at=evaluated_at,
    )

    return {
        "configuration_hash": configuration_hash,
        "decision_id": decision_id,
        "drawdown_policy_mode": settings.risk.drawdown_policy_mode.value,
        "evaluated_at": evaluated_at.isoformat(),
        "exchange_writes": False,
        "input_data_hash": input_data_hash,
        "market_data": {
            "completed_candle_count": len(batch.completed),
            "latest_completed_open_time": decision.signal_open_time.isoformat(),
            "mutable_tail_quarantined": True,
            "observed_at": batch.observed_at.isoformat(),
            "pair": settings.pair,
        },
        "operation": "daily",
        "order_intent_created": False,
        "outcome": outcome,
        "reason": decision.reason.value,
        "release_id": release_id,
        "state": decision.state.value,
        "strategy_date": decision.signal_open_time.date().isoformat(),
        "strategy_id": settings.strategy_id,
        "target_weight": format(decision.target_weight, "f"),
    }


def validate_daily_settings(settings: Settings) -> None:
    """Fail before state initialization when the daily boundary is unsafe."""

    if settings.mode not in _ALLOWED_MODES:
        supported = ", ".join(mode.value for mode in sorted(_ALLOWED_MODES))
        raise DailyJobError(f"daily command requires one of these modes: {supported}")
    if settings.risk.drawdown_policy_mode is not DrawdownPolicyMode.DISABLED:
        raise ConfigError("production V3 daily decisions require drawdown_policy_mode=disabled")
    if settings.kraken_api_key is not None or settings.kraken_api_secret is not None:
        raise DailyJobError("daily command refuses authenticated Kraken credentials")


def _validate_decision_batch(batch: KrakenOhlcBatch, *, evaluated_at: datetime) -> None:
    if not isinstance(batch, KrakenOhlcBatch):
        raise MarketDataError("public OHLC client returned a malformed batch")
    if not batch.completed:
        raise MarketDataError("no completed candle is available")
    if not isinstance(batch.requested_pair, str) or batch.requested_pair != KRAKEN_REQUEST_PAIR:
        raise MarketDataError("public OHLC batch is not bound to the requested BTC/CAD pair")
    if (
        not isinstance(batch.raw_pair_key, str)
        or batch.raw_pair_key.upper() not in BTC_CAD_RESPONSE_KEYS
    ):
        raise MarketDataError("public OHLC response pair is not BTC/CAD")
    if any(not isinstance(candle, Candle) for candle in batch.completed):
        raise MarketDataError("completed OHLC history is malformed")
    if not isinstance(batch.mutable_tail, Candle):
        raise MarketDataError("mutable OHLC tail is malformed")
    if not isinstance(batch.observed_at, datetime) or batch.observed_at.tzinfo is None:
        raise MarketDataError("OHLC observation timestamp must be timezone-aware")
    if any(not candle.complete for candle in batch.completed):
        raise MarketDataError("completed OHLC history contains an incomplete candle")
    if batch.mutable_tail.complete:
        raise MarketDataError("mutable OHLC tail was not quarantined")
    validate_daily_sequence((*batch.completed, batch.mutable_tail), require_contiguous=True)
    validate_batch_freshness(batch, evaluated_at=evaluated_at)
    minimum_history = StrategyPolicy().minimum_history
    if len(batch.completed) < minimum_history:
        raise MarketDataError(f"at least {minimum_history} completed daily candles are required")


def _configuration_hash(
    *,
    settings: Settings,
    strategy_policy_hash: str,
    release_id: str,
) -> str:
    return sha256_json(
        {
            "account_id": settings.account_id,
            "code_version": DAILY_JOB_CODE_VERSION,
            "mode": settings.mode,
            "pair": settings.pair,
            "release_id": release_id,
            "risk_fingerprint": settings.risk.fingerprint,
            "schema": "kraken-knight-daily-configuration-v1",
            "strategy_id": settings.strategy_id,
            "strategy_policy_hash": strategy_policy_hash,
        }
    )


def verify_release_binding(
    *,
    release_id: str,
    release_path: Path,
    releases_root: Path = PRODUCTION_RELEASES_ROOT,
) -> None:
    """Bind the asserted SHA to the resolved immutable deployment directory."""

    if not isinstance(release_path, Path) or not isinstance(releases_root, Path):
        raise TypeError("release paths must be pathlib.Path values")
    try:
        resolved = release_path.resolve(strict=True)
        resolved_root = releases_root.resolve(strict=True)
    except OSError as exc:
        raise DailyJobError("daily release path cannot be resolved") from exc
    if resolved.name != release_id or resolved.parent != resolved_root:
        raise DailyJobError("release identity does not match the resolved release directory")


def _daily_input_hash(*, batch: KrakenOhlcBatch, strategy_input_hash: str) -> str:
    """Hash only immutable completed evidence so mutable-tail retries are stable."""

    return sha256_json(
        {
            "canonical_pair": "BTC/CAD",
            "completed_candle_count": len(batch.completed),
            "schema": "kraken-knight-daily-input-v1",
            "strategy_input_hash": strategy_input_hash,
        }
    )


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
