from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from kraken_knight.domain import Candle
from kraken_knight.strategy import (
    DecisionReason,
    MomentumTrendStrategy,
    PositionState,
    StrategyPolicy,
    evaluate_signal,
)

START = datetime(2025, 1, 1, tzinfo=UTC)


def candle(day: int, close: Decimal, *, complete: bool = True) -> Candle:
    return Candle(
        open_time=START + timedelta(days=day),
        open=close,
        high=close + Decimal("2"),
        low=close - Decimal("2"),
        close=close,
        volume=Decimal("10"),
        complete=complete,
    )


def gently_rising(count: int = 200) -> list[Candle]:
    prices = [
        Decimal("100")
        + Decimal(day) * Decimal("0.2")
        + (Decimal("0.03") if day % 3 == 0 else Decimal("0"))
        for day in range(count)
    ]
    return [candle(day, price) for day, price in enumerate(prices)]


def test_candle_preserves_decimals_and_exposes_close_time() -> None:
    item = Candle(
        open_time=START,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("95"),
        close=Decimal("102"),
        volume=Decimal("1.25"),
    )

    assert item.close == Decimal("102")
    assert item.volume == Decimal("1.25")
    assert item.close_time == START + timedelta(days=1)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"open_time": datetime(2025, 1, 1)}, "timezone-aware UTC"),
        ({"open_time": START + timedelta(hours=1)}, "00:00:00 UTC"),
        ({"close": Decimal("0")}, "greater than zero"),
        ({"low": Decimal("106")}, "low cannot exceed high"),
        ({"close": Decimal("106")}, "within"),
        ({"volume": Decimal("-1")}, "cannot be negative"),
    ],
)
def test_candle_rejects_invalid_daily_market_data(changes: dict[str, object], error: str) -> None:
    values: dict[str, object] = {
        "open_time": START,
        "open": Decimal("100"),
        "high": Decimal("105"),
        "low": Decimal("95"),
        "close": Decimal("102"),
        "volume": Decimal("1"),
    }
    values.update(changes)

    with pytest.raises(ValueError, match=error):
        Candle(**values)  # type: ignore[arg-type]


def test_candle_rejects_binary_float_prices() -> None:
    with pytest.raises(TypeError, match="Decimal, int, or str"):
        Candle(
            open_time=START,
            open=100.0,  # type: ignore[arg-type]
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
        )


def test_default_policy_is_the_frozen_v1_specification() -> None:
    policy = StrategyPolicy()

    assert policy.momentum_days == 90
    assert policy.trend_days == 200
    assert policy.volatility_days == 30
    assert policy.annualization_days == 365
    assert policy.volatility_target == Decimal("0.25")
    assert policy.max_weight == Decimal("0.80")
    assert policy.minimum_history == 200


def test_long_signal_uses_exact_momentum_sma_and_sample_volatility() -> None:
    candles = gently_rising()
    decision = evaluate_signal(candles)
    closes = [item.close for item in candles]
    expected_momentum = closes[-1] / closes[-91] - Decimal("1")
    expected_sma = sum(closes[-200:], start=Decimal("0")) / Decimal("200")
    expected_returns = [math.log(float(right / left)) for left, right in pairwise(closes[-31:])]
    expected_volatility = statistics.stdev(expected_returns) * math.sqrt(365)

    assert decision.state is PositionState.BTC
    assert decision.reason is DecisionReason.LONG_SIGNAL
    assert decision.usable_data
    assert decision.momentum == expected_momentum
    assert decision.sma == expected_sma
    assert float(decision.annualized_volatility or 0) == pytest.approx(expected_volatility)
    assert decision.target_weight == Decimal("0.80")


def test_high_volatility_reduces_weight_below_cap() -> None:
    price = Decimal("100")
    candles: list[Candle] = []
    for day in range(200):
        if day:
            price *= Decimal("1.04") if day % 2 else Decimal("0.98")
        candles.append(candle(day, price))

    decision = evaluate_signal(candles)

    assert decision.state is PositionState.BTC
    assert Decimal("0") < decision.target_weight < Decimal("0.80")
    assert decision.annualized_volatility is not None
    assert decision.target_weight == Decimal("0.25") / decision.annualized_volatility


def test_non_positive_momentum_fails_to_cash() -> None:
    candles = [candle(day, Decimal("300") - Decimal(day) * Decimal("0.5")) for day in range(200)]

    decision = evaluate_signal(candles)

    assert decision.state is PositionState.CASH
    assert decision.target_weight == 0
    assert decision.reason is DecisionReason.NON_POSITIVE_MOMENTUM
    assert decision.usable_data


def test_close_below_sma_fails_to_cash_even_with_positive_90_day_momentum() -> None:
    prices: list[Decimal] = []
    for day in range(109):
        prices.append(Decimal("200") + Decimal(day % 2))
    prices.append(Decimal("100"))
    for offset in range(1, 91):
        prices.append(Decimal("100") + Decimal(offset) * Decimal("0.55"))
    candles = [candle(day, price) for day, price in enumerate(prices)]

    decision = evaluate_signal(candles)

    assert decision.momentum is not None and decision.momentum > 0
    assert decision.sma is not None and decision.close is not None
    assert decision.close < decision.sma
    assert decision.state is PositionState.CASH
    assert decision.reason is DecisionReason.BELOW_TREND


def test_zero_volatility_fails_closed() -> None:
    prices = [Decimal("100") + Decimal(day) for day in range(169)]
    prices.extend([Decimal("268")] * 31)

    decision = evaluate_signal([candle(day, price) for day, price in enumerate(prices)])

    assert decision.state is PositionState.CASH
    assert not decision.usable_data
    assert decision.reason is DecisionReason.ZERO_VOLATILITY


def test_missing_history_and_incomplete_tail_fail_closed() -> None:
    insufficient = evaluate_signal(gently_rising(199))
    full = gently_rising(200)
    incomplete = evaluate_signal([*full[:-1], candle(199, full[-1].close, complete=False)])

    assert insufficient.reason is DecisionReason.INSUFFICIENT_HISTORY
    assert insufficient.target_weight == 0
    assert incomplete.reason is DecisionReason.INCOMPLETE_CANDLE
    assert incomplete.target_weight == 0


def test_gap_in_required_history_fails_closed() -> None:
    candles = gently_rising(201)
    del candles[150]

    decision = evaluate_signal(candles)

    assert decision.reason is DecisionReason.INVALID_SEQUENCE
    assert decision.target_weight == 0
    assert not decision.usable_data


def test_decision_is_content_addressed_and_deterministic() -> None:
    candles = gently_rising()
    strategy = MomentumTrendStrategy()

    first = strategy.evaluate(candles)
    second = strategy.evaluate(tuple(candles))

    assert first == second
    assert len(first.decision_id) == 64
    assert len(first.policy_hash) == 64
    assert len(first.input_data_hash) == 64


def test_decision_identity_commits_to_policy_and_exact_input_snapshot() -> None:
    candles = gently_rising()
    baseline = MomentumTrendStrategy().evaluate(candles)
    changed_policy = MomentumTrendStrategy(
        StrategyPolicy(volatility_target=Decimal("0.20"))
    ).evaluate(candles)
    changed_input = list(candles)
    first = changed_input[0]
    changed_input[0] = Candle(
        open_time=first.open_time,
        open=first.open,
        high=first.high,
        low=first.low,
        close=first.close,
        volume=first.volume + Decimal("1"),
    )
    revised = MomentumTrendStrategy().evaluate(changed_input)

    assert baseline.policy_hash != changed_policy.policy_hash
    assert baseline.decision_id != changed_policy.decision_id
    assert baseline.input_data_hash != revised.input_data_hash
    assert baseline.decision_id != revised.decision_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("momentum_days", 0),
        ("trend_days", -1),
        ("volatility_days", 0),
        ("annualization_days", 0),
        ("volatility_target", Decimal("0")),
        ("max_weight", Decimal("1.01")),
    ],
)
def test_policy_rejects_invalid_parameters(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        StrategyPolicy(**{field: value})  # type: ignore[arg-type]
