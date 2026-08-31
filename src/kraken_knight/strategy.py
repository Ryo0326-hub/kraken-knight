"""Deterministic medium-term BTC momentum/trend strategy."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from .domain import Candle, to_decimal


class PositionState(StrEnum):
    CASH = "cash"
    BTC = "btc"


class DecisionReason(StrEnum):
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPLETE_CANDLE = "incomplete_candle"
    INVALID_SEQUENCE = "invalid_sequence"
    NON_POSITIVE_MOMENTUM = "non_positive_momentum"
    BELOW_TREND = "below_trend"
    ZERO_VOLATILITY = "zero_volatility"
    LONG_SIGNAL = "long_signal"


@dataclass(frozen=True, slots=True)
class StrategyPolicy:
    """Frozen strategy parameters.

    A 30-day volatility estimate needs 31 closes.  ``trend_days=200`` is the
    largest default window, so 200 complete candles are sufficient overall.
    """

    momentum_days: int = 90
    trend_days: int = 200
    volatility_days: int = 30
    annualization_days: int = 365
    volatility_target: Decimal = Decimal("0.25")
    max_weight: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        for name in (
            "momentum_days",
            "trend_days",
            "volatility_days",
            "annualization_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        target = to_decimal(self.volatility_target, field="volatility_target")
        maximum = to_decimal(self.max_weight, field="max_weight")
        object.__setattr__(self, "volatility_target", target)
        object.__setattr__(self, "max_weight", maximum)
        if target <= 0:
            raise ValueError("volatility_target must be greater than zero")
        if maximum <= 0 or maximum > 1:
            raise ValueError("max_weight must be in (0, 1]")

    @property
    def minimum_history(self) -> int:
        return max(
            self.trend_days,
            self.momentum_days + 1,
            self.volatility_days + 1,
        )

    @property
    def fingerprint(self) -> str:
        fields = {
            "annualization_days": self.annualization_days,
            "max_weight": str(self.max_weight),
            "momentum_days": self.momentum_days,
            "trend_days": self.trend_days,
            "volatility_days": self.volatility_days,
            "volatility_target": str(self.volatility_target),
        }
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """An immutable and content-addressed strategy decision."""

    decision_id: str
    signal_open_time: datetime | None
    signal_close_time: datetime | None
    state: PositionState
    target_weight: Decimal
    reason: DecisionReason
    usable_data: bool
    policy_hash: str
    input_data_hash: str
    close: Decimal | None = None
    momentum: Decimal | None = None
    sma: Decimal | None = None
    annualized_volatility: Decimal | None = None


def _decision(
    *,
    signal: Candle | None,
    state: PositionState,
    target_weight: Decimal,
    reason: DecisionReason,
    usable_data: bool,
    policy_hash: str,
    input_data_hash: str,
    close: Decimal | None = None,
    momentum: Decimal | None = None,
    sma: Decimal | None = None,
    annualized_volatility: Decimal | None = None,
) -> StrategyDecision:
    fields = {
        "signal_open_time": signal.open_time.isoformat() if signal else None,
        "signal_close_time": signal.close_time.isoformat() if signal else None,
        "state": state.value,
        "target_weight": str(target_weight),
        "reason": reason.value,
        "usable_data": usable_data,
        "policy_hash": policy_hash,
        "input_data_hash": input_data_hash,
        "close": str(close) if close is not None else None,
        "momentum": str(momentum) if momentum is not None else None,
        "sma": str(sma) if sma is not None else None,
        "annualized_volatility": (
            str(annualized_volatility) if annualized_volatility is not None else None
        ),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    decision_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return StrategyDecision(
        decision_id=decision_id,
        signal_open_time=signal.open_time if signal else None,
        signal_close_time=signal.close_time if signal else None,
        state=state,
        target_weight=target_weight,
        reason=reason,
        usable_data=usable_data,
        policy_hash=policy_hash,
        input_data_hash=input_data_hash,
        close=close,
        momentum=momentum,
        sma=sma,
        annualized_volatility=annualized_volatility,
    )


class MomentumTrendStrategy:
    """90-day momentum + 200-day trend with 30-day volatility sizing."""

    def __init__(self, policy: StrategyPolicy | None = None) -> None:
        self.policy = policy or StrategyPolicy()

    def evaluate(self, candles: Sequence[Candle]) -> StrategyDecision:
        """Evaluate only the final supplied candle and its causal history.

        There is no implicit fallback to the most recent complete candle.  If
        the caller leaves Kraken's mutable final row in the sequence, the
        strategy explicitly returns cash.
        """

        policy_hash = self.policy.fingerprint
        input_data_hash = _input_data_hash(candles)
        signal = candles[-1] if candles and isinstance(candles[-1], Candle) else None
        if signal is not None and not signal.complete:
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.INCOMPLETE_CANDLE,
                usable_data=False,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=signal.close,
            )

        needed = self.policy.minimum_history
        if len(candles) < needed:
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.INSUFFICIENT_HISTORY,
                usable_data=False,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=signal.close if signal else None,
            )

        history = candles[-needed:]
        if any(not isinstance(candle, Candle) or not candle.complete for candle in history):
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.INVALID_SEQUENCE,
                usable_data=False,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=signal.close if signal else None,
            )

        one_day = timedelta(days=1)
        if any(right.open_time - left.open_time != one_day for left, right in pairwise(history)):
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.INVALID_SEQUENCE,
                usable_data=False,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=signal.close if signal else None,
            )

        closes = [candle.close for candle in history]
        close = closes[-1]
        momentum_reference = closes[-(self.policy.momentum_days + 1)]
        momentum = close / momentum_reference - Decimal("1")
        trend_closes = closes[-self.policy.trend_days :]
        sma = sum(trend_closes, start=Decimal("0")) / Decimal(self.policy.trend_days)

        volatility_closes = closes[-(self.policy.volatility_days + 1) :]
        try:
            log_returns = [
                math.log(float(right / left)) for left, right in pairwise(volatility_closes)
            ]
            daily_volatility = statistics.stdev(log_returns)
            annualized_float = daily_volatility * math.sqrt(self.policy.annualization_days)
        except (ValueError, OverflowError, statistics.StatisticsError):
            annualized_float = 0.0

        if not math.isfinite(annualized_float) or annualized_float <= 0:
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.ZERO_VOLATILITY,
                usable_data=False,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=close,
                momentum=momentum,
                sma=sma,
                annualized_volatility=Decimal("0"),
            )

        annualized_volatility = Decimal(str(annualized_float))
        if momentum <= 0:
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.NON_POSITIVE_MOMENTUM,
                usable_data=True,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=close,
                momentum=momentum,
                sma=sma,
                annualized_volatility=annualized_volatility,
            )
        if close <= sma:
            return _decision(
                signal=signal,
                state=PositionState.CASH,
                target_weight=Decimal("0"),
                reason=DecisionReason.BELOW_TREND,
                usable_data=True,
                policy_hash=policy_hash,
                input_data_hash=input_data_hash,
                close=close,
                momentum=momentum,
                sma=sma,
                annualized_volatility=annualized_volatility,
            )

        uncapped_weight = self.policy.volatility_target / annualized_volatility
        target_weight = min(self.policy.max_weight, uncapped_weight)
        return _decision(
            signal=signal,
            state=PositionState.BTC,
            target_weight=target_weight,
            reason=DecisionReason.LONG_SIGNAL,
            usable_data=True,
            policy_hash=policy_hash,
            input_data_hash=input_data_hash,
            close=close,
            momentum=momentum,
            sma=sma,
            annualized_volatility=annualized_volatility,
        )


def _input_data_hash(candles: Sequence[Candle]) -> str:
    rows: list[dict[str, object]] = []
    for candle in candles:
        rows.append(
            {
                "close": str(candle.close),
                "complete": candle.complete,
                "high": str(candle.high),
                "low": str(candle.low),
                "open": str(candle.open),
                "open_time": candle.open_time.isoformat(),
                "volume": str(candle.volume),
            }
        )
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_signal(
    candles: Sequence[Candle], policy: StrategyPolicy | None = None
) -> StrategyDecision:
    """Functional convenience wrapper for deterministic signal evaluation."""

    return MomentumTrendStrategy(policy).evaluate(candles)
