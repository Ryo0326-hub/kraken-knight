from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

import pytest

from kraken_knight.backtest import (
    BacktestConfig,
    DrawdownPolicyMode,
    ExecutionCosts,
    ExecutionOutcome,
    ExecutionReference,
    InstrumentRules,
    Liquidity,
    RiskEventType,
    TradeSide,
    run_backtest,
)
from kraken_knight.domain import Candle
from kraken_knight.strategy import MomentumTrendStrategy, StrategyPolicy

START = datetime(2025, 1, 1, tzinfo=UTC)


def candle(
    day: int,
    close: Decimal,
    *,
    open_price: Decimal | None = None,
    complete: bool = True,
    date_offset: int = 0,
) -> Candle:
    opening = open_price if open_price is not None else close
    return Candle(
        open_time=START + timedelta(days=day + date_offset),
        open=opening,
        high=max(opening, close) + Decimal("2"),
        low=min(opening, close) - Decimal("2"),
        close=close,
        volume=Decimal("5"),
        complete=complete,
    )


def rising(
    count: int,
    *,
    execution_open: Decimal | None = None,
    execution_day: int = 201,
) -> list[Candle]:
    result: list[Candle] = []
    for day in range(count):
        close = (
            Decimal("100")
            + Decimal(day) * Decimal("0.2")
            + (Decimal("0.03") if day % 3 == 0 else Decimal("0"))
        )
        opening = execution_open if day == execution_day and execution_open is not None else close
        result.append(candle(day, close, open_price=opening))
    return result


def zero_band_config(*, costs: ExecutionCosts | None = None) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=Decimal("1000"),
        costs=costs or ExecutionCosts(),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )


def short_window_strategy() -> MomentumTrendStrategy:
    return MomentumTrendStrategy(
        StrategyPolicy(
            momentum_days=1,
            trend_days=2,
            volatility_days=2,
        )
    )


def cooldown_rearm_candles() -> list[Candle]:
    candles = [
        candle(0, Decimal("100")),
        candle(1, Decimal("105")),
        candle(2, Decimal("110")),
        candle(3, Decimal("115")),
        candle(4, Decimal("200"), open_price=Decimal("115")),
        candle(5, Decimal("100"), open_price=Decimal("100")),
    ]
    candles.extend(
        candle(day, Decimal("100") if day % 2 else Decimal("99")) for day in range(6, 93)
    )
    candles.extend(
        (
            candle(93, Decimal("105")),
            candle(94, Decimal("110")),
            candle(95, Decimal("115"), open_price=Decimal("110")),
            candle(96, Decimal("115"), open_price=Decimal("115")),
        )
    )
    return candles


def precise_reference(
    candles: list[Candle],
    execution_index: int,
    price: Decimal,
    *,
    minute_offset: int = 0,
    volume_btc: Decimal = Decimal("1.25"),
) -> ExecutionReference:
    decision_time = candles[execution_index - 1].close_time + timedelta(minutes=15)
    return ExecutionReference(
        decision_time=decision_time,
        execution_time=decision_time + timedelta(minutes=minute_offset),
        reference_price=price,
        volume_btc=volume_btc,
        trade_count=7,
    )


def test_signal_executes_only_at_next_candle_open() -> None:
    candles = rising(202, execution_open=Decimal("500"))

    result = run_backtest(candles, config=zero_band_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.execution_time == candles[201].open_time
    assert trade.reference_price == Decimal("500")
    assert trade.execution_time == result.decisions[-1].execution_time
    assert result.decisions[-1].signal_time == candles[199].close_time
    assert result.decisions[-1].decision_time == candles[199].close_time + timedelta(minutes=15)
    assert result.decisions[-1].execution_time >= result.decisions[-1].decision_time
    assert trade.decision_time == result.decisions[-1].decision_time
    assert result.strategy_decisions[-1].signal_close_time == candles[199].close_time
    assert result.strategy_decisions[-1].momentum is not None
    assert result.strategy_decisions[-1].sma is not None
    assert result.strategy_decisions[-1].annualized_volatility is not None


def test_next_candle_close_cannot_change_pending_trade() -> None:
    base = rising(202, execution_open=Decimal("500"))
    changed = list(base)
    changed[201] = candle(
        201,
        Decimal("900"),
        open_price=Decimal("500"),
    )

    original_result = run_backtest(base, config=zero_band_config())
    changed_result = run_backtest(changed, config=zero_band_config())

    assert original_result.trades == changed_result.trades
    assert original_result.decisions == changed_result.decisions
    assert original_result.metrics.final_equity != changed_result.metrics.final_equity


def test_fee_and_slippage_costs_are_accounted_exactly() -> None:
    costs = ExecutionCosts(
        maker_fee_rate=Decimal("0.01"),
        taker_fee_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.02"),
        buy_liquidity=Liquidity.MAKER,
        sell_liquidity=Liquidity.TAKER,
    )
    candles = rising(202, execution_open=Decimal("500"))

    result = run_backtest(candles, config=zero_band_config(costs=costs))
    trade = result.trades[0]
    expected_execution_price = Decimal("500") * Decimal("1.02")
    expected_quantity = Decimal("800") / (expected_execution_price * Decimal("1.01"))
    expected_notional = expected_quantity * expected_execution_price
    expected_fee = expected_notional * Decimal("0.01")

    assert trade.side is TradeSide.BUY
    assert trade.liquidity is Liquidity.MAKER
    assert trade.quantity_btc == expected_quantity
    assert trade.execution_price == expected_execution_price
    assert trade.gross_notional_cad == expected_notional
    assert trade.slippage_cad == expected_quantity * Decimal("10")
    assert trade.fee_cad == expected_fee
    assert trade.cash_after == Decimal("1000") - expected_notional - expected_fee
    assert trade.cash_after == Decimal("200")
    assert result.metrics.total_fees == expected_fee
    assert result.metrics.fee_drag == expected_fee / Decimal("1000")
    assert result.metrics.trade_count == 1


def test_rebalance_band_is_maximum_of_cad_floor_and_equity_fraction() -> None:
    candles = rising(202)
    config = BacktestConfig(
        rebalance_min_cad=Decimal("900"),
        rebalance_equity_fraction=Decimal("0.05"),
    )

    result = run_backtest(candles, config=config)

    assert result.trades == ()
    final_decision = result.decisions[-1]
    assert Decimal("0") < final_decision.requested_delta_cad < Decimal("800")
    assert final_decision.rebalance_band_cad == Decimal("900")
    assert final_decision.outcome is ExecutionOutcome.WITHIN_BAND


def test_exit_uses_configured_taker_fee() -> None:
    candles = rising(203, execution_open=Decimal("500"))
    # Candle 200 is unknown to the entry decision. Its crash becomes a risk-off
    # signal at 00:15 after candle 201 opens, so it executes at candle 202.
    candles[200] = candle(200, Decimal("50"), open_price=Decimal("500"))
    candles[201] = candle(201, Decimal("50"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("52"), open_price=Decimal("50"))
    costs = ExecutionCosts(
        maker_fee_rate=Decimal("0.004"),
        taker_fee_rate=Decimal("0.008"),
        slippage_rate=Decimal("0"),
        buy_liquidity=Liquidity.MAKER,
        sell_liquidity=Liquidity.TAKER,
    )

    config = BacktestConfig(
        costs=costs,
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
        max_drawdown_threshold=Decimal("0.99"),
    )
    result = run_backtest(candles, config=config)

    assert [trade.side for trade in result.trades] == [TradeSide.BUY, TradeSide.SELL]
    sale = result.trades[1]
    assert sale.liquidity is Liquidity.TAKER
    assert sale.fee_cad == sale.gross_notional_cad * Decimal("0.008")
    assert result.metrics.total_fees == sum(
        (trade.fee_cad for trade in result.trades), start=Decimal("0")
    )


def test_risk_off_transition_bypasses_normal_rebalance_band() -> None:
    candles = rising(203, execution_open=Decimal("500"))
    candles[200] = candle(200, Decimal("10"), open_price=Decimal("500"))
    candles[201] = candle(201, Decimal("10"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("11"), open_price=Decimal("10"))

    result = run_backtest(
        candles,
        config=BacktestConfig(max_drawdown_threshold=Decimal("0.99")),
    )

    assert [trade.side for trade in result.trades] == [TradeSide.BUY, TradeSide.SELL]
    sale = result.trades[-1]
    assert sale.gross_notional_cad < Decimal("50")
    assert sale.quantity_btc == result.trades[0].quantity_btc


def test_signal_does_not_execute_after_a_gap() -> None:
    candles = rising(202, execution_open=Decimal("500"))
    final = candles[-1]
    candles[-1] = Candle(
        open_time=final.open_time + timedelta(days=1),
        open=final.open,
        high=final.high,
        low=final.low,
        close=final.close,
        volume=final.volume,
    )

    result = run_backtest(candles, config=zero_band_config())

    assert result.trades == ()
    assert result.decisions[-1].signal_time == candles[-3].close_time
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_STALE_SIGNAL


def test_invalid_data_does_not_liquidate_an_existing_position() -> None:
    candles = rising(206)
    for index in range(203, 206):
        candles[index] = candle(index, candles[index].close, date_offset=1)

    result = run_backtest(candles)

    assert result.trades[0].side is TradeSide.BUY
    assert all(trade.side is not TradeSide.SELL for trade in result.trades)
    assert result.equity_curve[-1].btc > 0
    assert result.decisions[-2].outcome is ExecutionOutcome.NO_ACTION_STALE_SIGNAL
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_DATA_INVALID


def test_backtest_rejects_incomplete_duplicate_and_out_of_order_candles() -> None:
    incomplete = rising(2)
    incomplete[1] = candle(1, Decimal("101"), complete=False)
    with pytest.raises(ValueError, match="incomplete"):
        run_backtest(incomplete)

    duplicate = [candle(0, Decimal("100")), candle(0, Decimal("101"))]
    with pytest.raises(ValueError, match="strictly increasing"):
        run_backtest(duplicate)

    with pytest.raises(ValueError, match="strictly increasing"):
        run_backtest(list(reversed(rising(2))))


def test_gap_inside_signal_history_fails_closed_without_trading() -> None:
    candles = rising(203)
    del candles[150]

    result = run_backtest(candles, config=zero_band_config())

    assert result.trades == ()
    assert result.decisions[-1].strategy_reason == "invalid_sequence"


def test_results_and_audit_ids_are_deterministic() -> None:
    candles = rising(203)
    config = zero_band_config()

    first = run_backtest(candles, config=config)
    second = run_backtest(tuple(candles), config=config)

    assert first == second
    assert first.trades[0].trade_id.startswith("trade_")
    assert first.decisions[-1].intent_id.startswith("intent_")


def test_benchmark_and_strategy_metrics_capture_drawdown_and_trade_statistics() -> None:
    candles = [
        candle(0, Decimal("100")),
        candle(1, Decimal("200")),
        candle(2, Decimal("100")),
    ]

    result = run_backtest(candles)

    assert result.metrics.initial_equity == Decimal("1000")
    assert result.metrics.final_equity == Decimal("1000")
    assert result.metrics.total_return == 0
    assert result.metrics.max_drawdown == 0
    assert result.metrics.trade_count == 0
    assert result.benchmark_metrics.initial_equity == Decimal("1000")
    assert result.benchmark_metrics.final_equity == Decimal("1000")
    assert result.benchmark_metrics.max_drawdown == Decimal("0.5")
    assert result.benchmark_metrics.turnover == 0
    assert result.benchmark_metrics.total_fees == 0


def test_equity_is_fee_aware_and_uses_taker_liquidation_cost() -> None:
    costs = ExecutionCosts(
        maker_fee_rate=Decimal("0.01"),
        taker_fee_rate=Decimal("0.02"),
        slippage_rate=Decimal("0"),
    )
    candles = rising(202, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))

    result = run_backtest(candles, config=zero_band_config(costs=costs))

    final = result.equity_curve[-1]
    expected_mark = final.btc * Decimal("500")
    expected_liquidation_fee = expected_mark * Decimal("0.02")
    assert final.btc_mark_value_cad == expected_mark
    assert final.estimated_liquidation_fee_cad == expected_liquidation_fee
    assert final.equity == final.cash + expected_mark - expected_liquidation_fee
    assert result.trades[0].pre_trade_equity == Decimal("1000")


def test_absolute_cap_post_cost_fraction_and_reserve_all_hold_after_entry() -> None:
    candles = rising(202, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    config = BacktestConfig(
        initial_cash=Decimal("2000"),
        cash_reserve_cad=Decimal("200"),
        absolute_btc_cap_cad=Decimal("800"),
        max_post_cost_exposure=Decimal("0.80"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(candles, config=config)

    trade = result.trades[0]
    point = result.equity_curve[-1]
    post_cost_exposure = point.btc_mark_value_cad - point.estimated_liquidation_fee_cad
    assert trade.btc_after * trade.reference_price <= Decimal("800")
    assert post_cost_exposure / point.equity <= Decimal("0.80")
    assert trade.cash_after >= Decimal("200")


def test_absolute_cap_reduction_bypasses_the_normal_rebalance_band() -> None:
    candles = rising(203, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("515"), open_price=Decimal("515"))

    result = run_backtest(candles)

    assert [trade.side for trade in result.trades] == [TradeSide.BUY, TradeSide.SELL]
    assert abs(result.decisions[-1].requested_delta_cad) < Decimal("50")
    final = result.equity_curve[-1]
    assert final.btc_mark_value_cad <= Decimal("800")


def test_post_cost_fraction_cap_is_enforced_when_it_is_the_binding_limit() -> None:
    candles = rising(202, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    config = BacktestConfig(
        cash_reserve_cad=Decimal("0"),
        absolute_btc_cap_cad=Decimal("5000"),
        max_post_cost_exposure=Decimal("0.50"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(candles, config=config)

    point = result.equity_curve[-1]
    post_cost_exposure = point.btc_mark_value_cad - point.estimated_liquidation_fee_cad
    assert post_cost_exposure / point.equity <= Decimal("0.50")
    assert post_cost_exposure / point.equity == pytest.approx(Decimal("0.50"))


def test_rolling_24h_loss_gate_blocks_only_an_exposure_increase() -> None:
    candles = rising(203, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("425"), open_price=Decimal("425"))

    config = BacktestConfig(
        cash_reserve_cad=Decimal("0"),
        absolute_btc_cap_cad=Decimal("5000"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )
    result = run_backtest(candles, config=config)

    assert [trade.side for trade in result.trades] == [TradeSide.BUY]
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_RISK_GATE
    event_types = [event.event_type for event in result.risk_events]
    assert RiskEventType.ROLLING_24H_LOSS_GATE in event_types
    assert RiskEventType.EXPOSURE_INCREASE_BLOCKED in event_types
    assert not result.final_risk_state.drawdown_disarmed


def test_drawdown_disarm_forces_liquidation_and_persists() -> None:
    candles = rising(204, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("350"), open_price=Decimal("350"))
    candles[203] = candle(203, Decimal("360"), open_price=Decimal("360"))

    result = run_backtest(candles, config=zero_band_config())

    assert [trade.side for trade in result.trades] == [TradeSide.BUY, TradeSide.SELL]
    assert result.decisions[-2].outcome is ExecutionOutcome.FORCED_LIQUIDATION
    assert result.decisions[-2].remaining_btc == 0
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_DISARMED
    assert result.equity_curve[-1].btc == 0
    assert result.final_risk_state.drawdown_disarmed
    event_types = [event.event_type for event in result.risk_events]
    assert RiskEventType.DRAWDOWN_DISARMED in event_types
    assert RiskEventType.FORCED_LIQUIDATION in event_types
    forced_event = next(
        event
        for event in result.risk_events
        if event.event_type is RiskEventType.FORCED_LIQUIDATION
    )
    assert forced_event.action == "completed_forced_liquidation;remaining_btc=0"
    assert result.config.drawdown_policy_mode is DrawdownPolicyMode.PERSISTENT
    assert result.final_risk_state.drawdown_policy_mode is DrawdownPolicyMode.PERSISTENT
    assert result.final_risk_state.risk_epoch == 1
    assert result.final_risk_state.drawdown_disarmed_at == candles[202].open_time
    assert result.final_risk_state.drawdown_rearm_eligible_at is None
    assert result.final_risk_state.last_drawdown_rearmed_at is None


def test_disabled_drawdown_policy_never_disarms_or_forces_liquidation() -> None:
    candles = cooldown_rearm_candles()[:6]
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.DISABLED,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
    )

    assert [trade.side for trade in result.trades] == [TradeSide.BUY]
    assert result.equity_curve[-1].btc > 0
    assert not result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.drawdown_policy_mode is DrawdownPolicyMode.DISABLED
    assert result.final_risk_state.risk_epoch == 1
    assert all(
        event.event_type not in {RiskEventType.DRAWDOWN_DISARMED, RiskEventType.DRAWDOWN_REARMED}
        for event in result.risk_events
    )


def test_disabled_drawdown_policy_keeps_the_independent_rolling_loss_gate() -> None:
    candles = rising(203, execution_open=Decimal("500"))
    candles[201] = candle(201, Decimal("500"), open_price=Decimal("500"))
    candles[202] = candle(202, Decimal("425"), open_price=Decimal("425"))
    result = run_backtest(
        candles,
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.DISABLED,
            cash_reserve_cad=Decimal("0"),
            absolute_btc_cap_cad=Decimal("5000"),
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
    )

    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_RISK_GATE
    assert RiskEventType.ROLLING_24H_LOSS_GATE in {event.event_type for event in result.risk_events}
    assert RiskEventType.EXPOSURE_INCREASE_BLOCKED in {
        event.event_type for event in result.risk_events
    }
    assert RiskEventType.DRAWDOWN_DISARMED not in {event.event_type for event in result.risk_events}


def test_cooldown_policy_rearms_after_90_days_on_current_causal_long_signal() -> None:
    candles = cooldown_rearm_candles()
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
    )

    disarm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_DISARMED
    )
    rearm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_REARMED
    )
    rearm_eligible_at = disarm_event.observed_at + timedelta(days=90)
    expected_rearm_time = candles[94].close_time + timedelta(minutes=15)

    assert rearm_event.observed_at == expected_rearm_time
    assert rearm_event.observed_at >= rearm_eligible_at
    assert result.decisions[-2].outcome is ExecutionOutcome.NO_ACTION_DISARMED
    assert result.decisions[-1].outcome is ExecutionOutcome.BUY
    assert result.trades[-1].side is TradeSide.BUY
    assert rearm_event.strategy_decision_id == result.strategy_decisions[-1].decision_id
    assert rearm_event.reference_equity > rearm_event.equity
    assert result.final_risk_state.high_water_equity == rearm_event.equity
    assert not result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.risk_epoch == 2
    assert result.final_risk_state.drawdown_disarmed_at is None
    assert result.final_risk_state.drawdown_rearm_eligible_at is None
    assert result.final_risk_state.last_drawdown_rearmed_at == expected_rearm_time
    assert disarm_event.risk_epoch == 1
    assert rearm_event.risk_epoch == 2
    assert rearm_event.action == (
        "rearm_after_cooldown_on_causal_long_signal;"
        f"risk_epoch=2;high_water_equity={rearm_event.equity}"
    )


def test_cooldown_policy_stays_disarmed_without_an_eligible_long_signal() -> None:
    candles = cooldown_rearm_candles()
    candles[94] = candle(94, Decimal("98"))
    candles[95] = candle(95, Decimal("97"))
    candles[96] = candle(96, Decimal("96"))
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
    )

    assert result.final_risk_state.drawdown_rearm_eligible_at is not None
    assert result.strategy_decisions[-1].target_weight == 0
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_ACTION_DISARMED
    assert all(
        event.event_type is not RiskEventType.DRAWDOWN_REARMED for event in result.risk_events
    )


def test_cooldown_rearm_does_not_require_same_day_execution_reference() -> None:
    candles = cooldown_rearm_candles()
    entry = precise_reference(candles, 4, Decimal("115"), volume_btc=Decimal("100"))
    drawdown_exit = precise_reference(candles, 5, Decimal("100"), volume_btc=Decimal("100"))
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(entry, drawdown_exit),
        evaluation_start=entry.decision_time,
    )

    rearm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_REARMED
    )
    disarm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_DISARMED
    )

    assert rearm_event.observed_at == disarm_event.observed_at + timedelta(days=90)
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_FILL_REFERENCE_UNAVAILABLE
    assert [trade.side for trade in result.trades] == [TradeSide.BUY, TradeSide.SELL]
    assert not result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.risk_epoch == 2
    assert result.equity_curve[-1].btc == 0


def test_rearm_uses_decision_time_not_a_later_execution_reference_boundary() -> None:
    candles = cooldown_rearm_candles()
    entry = precise_reference(candles, 4, Decimal("115"), volume_btc=Decimal("100"))
    drawdown_exit = precise_reference(
        candles,
        5,
        Decimal("100"),
        minute_offset=1,
        volume_btc=Decimal("100"),
    )
    boundary_reference = precise_reference(
        candles,
        95,
        Decimal("110"),
        minute_offset=1,
        volume_btc=Decimal("100"),
    )
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(entry, drawdown_exit, boundary_reference),
        evaluation_start=entry.decision_time,
    )

    disarm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_DISARMED
    )
    rearm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_REARMED
    )
    eligible_at = disarm_event.observed_at + timedelta(days=90)

    assert boundary_reference.decision_time < eligible_at
    assert boundary_reference.execution_time == eligible_at
    assert result.decisions[-2].outcome is ExecutionOutcome.NO_ACTION_DISARMED
    assert rearm_event.observed_at == result.decisions[-1].decision_time
    assert rearm_event.observed_at > eligible_at
    assert result.decisions[-1].outcome is ExecutionOutcome.NO_FILL_REFERENCE_UNAVAILABLE
    assert not result.final_risk_state.drawdown_disarmed


def test_eligible_precise_rearm_executes_capped_buy_after_decision_event() -> None:
    candles = cooldown_rearm_candles()
    entry = precise_reference(candles, 4, Decimal("115"), volume_btc=Decimal("100"))
    drawdown_exit = precise_reference(candles, 5, Decimal("100"), volume_btc=Decimal("100"))
    capped_reentry = precise_reference(
        candles,
        95,
        Decimal("110"),
        minute_offset=2,
        volume_btc=Decimal("0.5"),
    )
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            max_execution_volume_fraction=Decimal("0.10"),
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(entry, drawdown_exit, capped_reentry),
        evaluation_start=entry.decision_time,
    )

    rearm_event = next(
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_REARMED
    )
    reentry = next(
        trade
        for trade in result.trades
        if trade.strategy_decision_id == rearm_event.strategy_decision_id
    )

    assert rearm_event.observed_at == capped_reentry.decision_time
    assert reentry.side is TradeSide.BUY
    assert reentry.decision_time == rearm_event.observed_at
    assert reentry.execution_time == capped_reentry.execution_time
    assert reentry.execution_time > rearm_event.observed_at
    assert reentry.volume_cap_applied
    assert reentry.execution_volume_btc == capped_reentry.volume_btc
    assert reentry.quantity_btc == Decimal("0.05")
    assert reentry.volume_participation_fraction == Decimal("0.10")
    assert rearm_event.risk_epoch == 2


def test_cooldown_policy_supports_repeated_disarm_and_rearm_epochs() -> None:
    candles = cooldown_rearm_candles()
    candles[96] = candle(96, Decimal("230"), open_price=Decimal("115"))
    candles.append(candle(97, Decimal("100"), open_price=Decimal("100")))
    candles.extend(
        candle(day, Decimal("100") if day % 2 else Decimal("99")) for day in range(98, 186)
    )
    candles.extend(
        (
            candle(186, Decimal("105")),
            candle(187, Decimal("110")),
            candle(188, Decimal("110"), open_price=Decimal("110")),
        )
    )
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
    )

    disarms = [
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_DISARMED
    ]
    rearms = [
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_REARMED
    ]

    assert [event.risk_epoch for event in disarms] == [1, 2]
    assert [event.risk_epoch for event in rearms] == [2, 3]
    assert rearms[0].observed_at >= disarms[0].observed_at + timedelta(days=90)
    assert rearms[1].observed_at >= disarms[1].observed_at + timedelta(days=90)
    assert [trade.side for trade in result.trades] == [
        TradeSide.BUY,
        TradeSide.SELL,
        TradeSide.BUY,
        TradeSide.SELL,
        TradeSide.BUY,
    ]
    assert not result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.risk_epoch == 3
    assert result.final_risk_state.last_drawdown_rearmed_at == rearms[-1].observed_at


def test_partial_liquidation_blocks_rearm_until_btc_is_zero() -> None:
    candles = cooldown_rearm_candles()
    entry = precise_reference(candles, 4, Decimal("115"), volume_btc=Decimal("100"))
    partial_exit = ExecutionReference(
        decision_time=candles[4].close_time + timedelta(minutes=15),
        execution_time=candles[4].close_time + timedelta(minutes=16),
        reference_price=Decimal("100"),
        volume_btc=Decimal("0.01"),
        trade_count=2,
    )
    eventual_exit = precise_reference(
        candles,
        96,
        Decimal("115"),
        volume_btc=Decimal("100"),
    )
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            max_execution_volume_fraction=Decimal("0.10"),
            max_drawdown_threshold=Decimal("0.01"),
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(entry, partial_exit, eventual_exit),
        evaluation_start=entry.decision_time,
    )

    assert result.decisions[1].outcome is ExecutionOutcome.FORCED_LIQUIDATION_PARTIAL_VOLUME_CAPPED
    assert result.decisions[-1].outcome is ExecutionOutcome.FORCED_LIQUIDATION
    assert result.decisions[-1].remaining_btc == 0
    assert all(
        event.event_type is not RiskEventType.DRAWDOWN_REARMED for event in result.risk_events
    )
    assert result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.risk_epoch == 1
    assert result.equity_curve[-1].btc == 0


def test_exchange_minimum_dust_remains_disarmed_after_cooldown() -> None:
    candles = cooldown_rearm_candles()
    entry = precise_reference(candles, 4, Decimal("115"), volume_btc=Decimal("100"))
    dust_creating_exit = precise_reference(
        candles,
        5,
        Decimal("100"),
        volume_btc=Decimal("0.0005"),
    )
    eligible_attempt = precise_reference(
        candles,
        95,
        Decimal("110"),
        volume_btc=Decimal("100"),
    )
    result = run_backtest(
        candles,
        strategy=short_window_strategy(),
        config=BacktestConfig(
            initial_cash=Decimal("0.01"),
            cash_reserve_cad=Decimal("0"),
            absolute_btc_cap_cad=Decimal("0.0069"),
            max_post_cost_exposure=Decimal("1"),
            instrument_rules=InstrumentRules(
                price_tick_cad=Decimal("0.1"),
                quantity_increment_btc=Decimal("0.00001"),
                minimum_quantity_btc=Decimal("0.00005"),
                minimum_cost_cad=Decimal("0.001"),
            ),
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            max_execution_volume_fraction=Decimal("0.10"),
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(entry, dust_creating_exit, eligible_attempt),
        evaluation_start=entry.decision_time,
    )

    partial = next(
        decision
        for decision in result.decisions
        if decision.outcome is ExecutionOutcome.FORCED_LIQUIDATION_PARTIAL_VOLUME_CAPPED
    )
    dust_attempt = next(
        decision
        for decision in result.decisions
        if decision.decision_time == eligible_attempt.decision_time
    )

    assert partial.remaining_btc == Decimal("0.00001")
    assert dust_attempt.outcome is ExecutionOutcome.BELOW_EXCHANGE_MINIMUM
    assert result.equity_curve[-1].btc == Decimal("0.00001")
    assert result.final_risk_state.drawdown_disarmed
    assert result.final_risk_state.risk_epoch == 1
    assert all(
        event.event_type is not RiskEventType.DRAWDOWN_REARMED for event in result.risk_events
    )


def test_volume_capped_forced_liquidation_records_partial_attempt_and_remainder() -> None:
    candles = rising(202)
    entry = precise_reference(candles, 200, Decimal("500"))
    exit_decision = candles[200].close_time + timedelta(minutes=15)
    capped_exit = ExecutionReference(
        decision_time=exit_decision,
        execution_time=exit_decision + timedelta(minutes=1),
        reference_price=Decimal("50"),
        volume_btc=Decimal("0.01"),
        trade_count=2,
    )
    config = BacktestConfig(
        max_execution_volume_fraction=Decimal("0.10"),
        max_drawdown_threshold=Decimal("0.01"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(
        candles,
        config=config,
        execution_references=(entry, capped_exit),
        evaluation_start=entry.decision_time,
    )

    sale = result.trades[-1]
    decision = result.decisions[-1]
    forced_event = next(
        event
        for event in result.risk_events
        if event.event_type is RiskEventType.FORCED_LIQUIDATION
    )
    assert sale.side is TradeSide.SELL
    assert sale.volume_cap_applied
    assert sale.quantity_btc == Decimal("0.001")
    assert sale.btc_after > 0
    assert decision.outcome is ExecutionOutcome.FORCED_LIQUIDATION_PARTIAL_VOLUME_CAPPED
    assert decision.remaining_btc == sale.btc_after
    assert forced_event.action == (
        f"attempted_volume_capped_forced_liquidation;remaining_btc={sale.btc_after}"
    )


def test_result_retains_exact_configuration_and_strategy_policy() -> None:
    config = BacktestConfig(max_post_cost_exposure=Decimal("0.60"))

    result = run_backtest(rising(3), config=config)

    assert result.config is config
    assert result.policy.momentum_days == 90
    assert result.policy.trend_days == 200
    assert result.policy.volatility_days == 30


def test_drawdown_policy_configuration_is_explicit_and_frozen() -> None:
    default = BacktestConfig()
    disabled = BacktestConfig(drawdown_policy_mode="disabled")  # type: ignore[arg-type]

    assert default.drawdown_policy_mode is DrawdownPolicyMode.PERSISTENT
    assert default.drawdown_rearm_cooldown_days == 90
    assert disabled.drawdown_policy_mode is DrawdownPolicyMode.DISABLED

    with pytest.raises(ValueError, match="drawdown_policy_mode"):
        BacktestConfig(drawdown_policy_mode="automatic")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly 90"):
        BacktestConfig(drawdown_rearm_cooldown_days=89)
    with pytest.raises(ValueError, match="exactly 90"):
        BacktestConfig(drawdown_rearm_cooldown_days=True)


@pytest.mark.parametrize(
    "costs",
    [
        {"maker_fee_rate": Decimal("-0.1")},
        {"taker_fee_rate": Decimal("1")},
        {"slippage_rate": Decimal("1")},
        {"buy_liquidity": "unknown"},
    ],
)
def test_execution_costs_reject_invalid_assumptions(costs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ExecutionCosts(**costs)  # type: ignore[arg-type]


def test_backtest_config_rejects_invalid_risk_inputs() -> None:
    with pytest.raises(ValueError, match="initial_cash"):
        BacktestConfig(initial_cash=Decimal("0"))
    with pytest.raises(ValueError, match="cash_reserve"):
        BacktestConfig(cash_reserve_cad=Decimal("-1"))
    with pytest.raises(ValueError, match="rebalance_min"):
        BacktestConfig(rebalance_min_cad=Decimal("-1"))
    with pytest.raises(ValueError, match="fraction"):
        BacktestConfig(rebalance_equity_fraction=Decimal("1.1"))
    with pytest.raises(ValueError, match="absolute_btc_cap"):
        BacktestConfig(absolute_btc_cap_cad=Decimal("-1"))
    with pytest.raises(ValueError, match="max_post_cost_exposure"):
        BacktestConfig(max_post_cost_exposure=Decimal("1.1"))
    with pytest.raises(ValueError, match="rolling_24h"):
        BacktestConfig(rolling_24h_loss_threshold=Decimal("0"))
    with pytest.raises(ValueError, match="max_drawdown"):
        BacktestConfig(max_drawdown_threshold=Decimal("1"))
    with pytest.raises(ValueError, match="decision_delay"):
        BacktestConfig(decision_delay_minutes=0)
    with pytest.raises(ValueError, match="execution_lag"):
        BacktestConfig(daily_execution_lag_days=0)


def test_precise_execution_uses_previous_completed_candle_at_0015_boundary() -> None:
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500"))

    result = run_backtest(
        candles,
        config=zero_band_config(),
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.decision_time == reference.decision_time
    assert trade.execution_time == reference.decision_time
    assert trade.reference_price == Decimal("500")
    assert result.decisions[0].signal_time == candles[199].close_time
    assert result.strategy_decisions[0].signal_close_time == candles[199].close_time
    assert result.equity_curve[0].close_time == reference.execution_time
    assert trade.fee_cad == trade.gross_notional_cad * Decimal("0.004")
    assert result.metrics.total_fees == trade.fee_cad


@pytest.mark.parametrize("minute_offset", [-1, 6])
def test_precise_execution_rejects_reference_outside_completion_window(
    minute_offset: int,
) -> None:
    candles = rising(201)
    decision_time = candles[199].close_time + timedelta(minutes=15)

    with pytest.raises(ValueError, match="execution_time"):
        ExecutionReference(
            decision_time=decision_time,
            execution_time=decision_time + timedelta(minutes=minute_offset),
            reference_price=Decimal("500"),
            volume_btc=Decimal("1"),
            trade_count=1,
        )


def test_precise_execution_accepts_last_minute_when_vwap_completes_at_window_end() -> None:
    candles = rising(201)
    decision_time = candles[199].close_time + timedelta(minutes=15)

    reference = ExecutionReference(
        decision_time=decision_time,
        execution_time=decision_time + timedelta(minutes=5),
        reference_price=Decimal("500"),
        volume_btc=Decimal("1"),
        trade_count=1,
    )

    assert reference.execution_time == decision_time + timedelta(minutes=5)


def test_precise_execution_reference_requires_positive_observed_trade_evidence() -> None:
    candles = rising(201)
    decision_time = candles[199].close_time + timedelta(minutes=15)

    with pytest.raises(ValueError, match="reference_price"):
        ExecutionReference(
            decision_time=decision_time,
            execution_time=decision_time,
            reference_price=Decimal("0"),
            volume_btc=Decimal("1"),
            trade_count=1,
        )
    with pytest.raises(ValueError, match="volume_btc"):
        ExecutionReference(
            decision_time=decision_time,
            execution_time=decision_time,
            reference_price=Decimal("500"),
            volume_btc=Decimal("0"),
            trade_count=1,
        )
    with pytest.raises(ValueError, match="trade_count"):
        ExecutionReference(
            decision_time=decision_time,
            execution_time=decision_time,
            reference_price=Decimal("500"),
            volume_btc=Decimal("1"),
            trade_count=False,
        )


def test_precise_execution_rejects_unbound_and_duplicate_references() -> None:
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500"))
    unbound = ExecutionReference(
        decision_time=reference.decision_time + timedelta(days=1),
        execution_time=reference.execution_time + timedelta(days=1),
        reference_price=reference.reference_price,
        volume_btc=reference.volume_btc,
        trade_count=reference.trade_count,
    )

    with pytest.raises(ValueError, match="scheduled"):
        run_backtest(candles, execution_references=(unbound,))
    with pytest.raises(ValueError, match="unique"):
        run_backtest(candles, execution_references=(reference, reference))


def test_missing_precise_reference_records_no_fill_and_keeps_existing_position() -> None:
    candles = rising(202)
    entry_reference = precise_reference(candles, 200, Decimal("500"))

    result = run_backtest(
        candles,
        config=zero_band_config(),
        execution_references=(entry_reference,),
        evaluation_start=entry_reference.decision_time,
    )

    assert len(result.trades) == 1
    assert [decision.outcome for decision in result.decisions] == [
        ExecutionOutcome.BUY,
        ExecutionOutcome.NO_FILL_REFERENCE_UNAVAILABLE,
    ]
    missing = result.decisions[-1]
    assert missing.execution_time == missing.decision_time
    assert missing.trade_id is None
    assert missing.requested_delta_cad == 0
    assert result.equity_curve[-1].btc == result.trades[0].btc_after


def test_future_precise_candle_and_reference_do_not_change_prior_decisions() -> None:
    short_candles = rising(202)
    first = precise_reference(short_candles, 200, Decimal("500"))
    second = precise_reference(short_candles, 201, Decimal("510"), minute_offset=1)
    short_result = run_backtest(
        short_candles,
        config=zero_band_config(),
        execution_references=(first, second),
        evaluation_start=first.decision_time,
    )

    extended_candles = rising(203)
    extended_candles[202] = candle(202, Decimal("900"), open_price=Decimal("700"))
    future = precise_reference(extended_candles, 202, Decimal("800"), minute_offset=4)
    extended_result = run_backtest(
        extended_candles,
        config=zero_band_config(),
        execution_references=(first, second, future),
        evaluation_start=first.decision_time,
    )

    assert short_result.strategy_decisions == extended_result.strategy_decisions[:2]
    assert short_result.decisions == extended_result.decisions[:2]
    assert short_result.trades == tuple(
        trade for trade in extended_result.trades if trade.decision_time <= second.decision_time
    )


def test_precise_evaluation_start_keeps_warmup_information_only_and_aligns_benchmark() -> None:
    candles = rising(253)
    references = tuple(
        precise_reference(candles, execution_index, Decimal("500") + execution_index)
        for execution_index in range(250, 253)
    )
    evaluation_start = references[0].decision_time

    result = run_backtest(
        candles,
        config=zero_band_config(),
        execution_references=references,
        evaluation_start=evaluation_start,
    )

    assert len(result.decisions) == 3
    assert result.decisions[0].decision_time == evaluation_start
    assert result.trades[0].pre_trade_equity == Decimal("1000")
    assert result.metrics.initial_equity == Decimal("1000")
    assert result.benchmark_metrics.initial_equity == Decimal("1000")
    assert result.equity_curve[0].close_time == references[0].execution_time
    assert result.benchmark_curve[0].close_time == references[0].execution_time
    assert [point.close_time for point in result.equity_curve] == [
        point.close_time for point in result.benchmark_curve
    ]


def test_omitted_precise_inputs_preserve_daily_open_replay() -> None:
    candles = rising(203)

    assert run_backtest(candles) == run_backtest(
        candles,
        execution_references=None,
    )
    with pytest.raises(ValueError, match="requires precise"):
        run_backtest(candles, evaluation_start=candles[200].open_time)


def test_explicit_instrument_rules_round_price_and_quantity_conservatively() -> None:
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500.03"))
    rules = InstrumentRules(
        price_tick_cad=Decimal("0.1"),
        quantity_increment_btc=Decimal("0.00000001"),
        minimum_quantity_btc=Decimal("0.00005"),
        minimum_cost_cad=Decimal("1"),
    )
    config = BacktestConfig(
        costs=ExecutionCosts(
            maker_fee_rate=Decimal("0.008"),
            taker_fee_rate=Decimal("0.008"),
            slippage_rate=Decimal("0.001"),
            buy_liquidity=Liquidity.TAKER,
            sell_liquidity=Liquidity.TAKER,
        ),
        instrument_rules=rules,
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(
        candles,
        config=config,
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )
    trade = result.trades[0]

    assert trade.execution_price == Decimal("500.6")
    assert trade.quantity_btc % Decimal("0.00000001") == 0
    assert trade.quantity_btc >= rules.minimum_quantity_btc
    assert trade.gross_notional_cad >= rules.minimum_cost_cad
    assert trade.cash_after >= config.cash_reserve_cad


def test_explicit_instrument_rules_record_below_minimum_without_fill() -> None:
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500"))
    config = BacktestConfig(
        instrument_rules=InstrumentRules(
            price_tick_cad=Decimal("0.1"),
            quantity_increment_btc=Decimal("0.00000001"),
            minimum_quantity_btc=Decimal("2"),
            minimum_cost_cad=Decimal("1"),
        ),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(
        candles,
        config=config,
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )

    assert result.trades == ()
    assert result.decisions[0].outcome is ExecutionOutcome.BELOW_EXCHANGE_MINIMUM


def test_instrument_rules_reject_invalid_values_and_types() -> None:
    with pytest.raises(ValueError, match="price_tick_cad"):
        InstrumentRules(
            price_tick_cad=Decimal("0"),
            quantity_increment_btc=Decimal("0.00000001"),
            minimum_quantity_btc=Decimal("0.00005"),
            minimum_cost_cad=Decimal("1"),
        )
    with pytest.raises(ValueError, match="cannot be smaller"):
        InstrumentRules(
            price_tick_cad=Decimal("0.1"),
            quantity_increment_btc=Decimal("0.001"),
            minimum_quantity_btc=Decimal("0.00005"),
            minimum_cost_cad=Decimal("1"),
        )
    with pytest.raises(TypeError, match="instrument_rules"):
        BacktestConfig(instrument_rules="not-rules")  # type: ignore[arg-type]


def test_precise_fill_is_capped_by_observed_minute_volume_participation() -> None:
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500"))
    config = BacktestConfig(
        max_execution_volume_fraction=Decimal("0.10"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(
        candles,
        config=config,
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )
    trade = result.trades[0]

    assert trade.execution_volume_btc == Decimal("1.25")
    assert trade.quantity_btc == Decimal("0.125")
    assert trade.volume_participation_fraction == Decimal("0.10")

    with pytest.raises(ValueError, match="max_execution_volume_fraction"):
        BacktestConfig(max_execution_volume_fraction=Decimal("0"))
    with pytest.raises(ValueError, match="max_execution_volume_fraction"):
        BacktestConfig(max_execution_volume_fraction=Decimal("1.01"))


def test_liquidation_equity_uses_configured_sell_fee_and_exit_slippage() -> None:
    costs = ExecutionCosts(
        maker_fee_rate=Decimal("0.01"),
        taker_fee_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.03"),
        buy_liquidity=Liquidity.TAKER,
        sell_liquidity=Liquidity.MAKER,
    )
    candles = rising(201)
    reference = precise_reference(candles, 200, Decimal("500"))

    result = run_backtest(
        candles,
        config=zero_band_config(costs=costs),
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )
    final = result.equity_curve[-1]
    mark = final.btc * final.close
    liquidation_notional = mark * Decimal("0.97")

    assert final.estimated_liquidation_slippage_cad == mark - liquidation_notional
    assert final.estimated_liquidation_fee_cad == liquidation_notional * Decimal("0.01")
    assert final.equity == final.cash + liquidation_notional * Decimal("0.99")


def test_liquidation_equity_rounds_exit_price_down_to_exchange_tick() -> None:
    costs = ExecutionCosts(
        maker_fee_rate=Decimal("0.01"),
        taker_fee_rate=Decimal("0.02"),
        slippage_rate=Decimal("0.03"),
        buy_liquidity=Liquidity.TAKER,
        sell_liquidity=Liquidity.MAKER,
    )
    rules = InstrumentRules(
        price_tick_cad=Decimal("0.1"),
        quantity_increment_btc=Decimal("0.00000001"),
        minimum_quantity_btc=Decimal("0.00005"),
        minimum_cost_cad=Decimal("1"),
    )
    candles = rising(201)
    candles[200] = candle(200, Decimal("140.03"))
    reference = precise_reference(candles, 200, Decimal("500"))

    result = run_backtest(
        candles,
        config=BacktestConfig(
            costs=costs,
            instrument_rules=rules,
            rebalance_min_cad=Decimal("0"),
            rebalance_equity_fraction=Decimal("0"),
        ),
        execution_references=(reference,),
        evaluation_start=reference.decision_time,
    )
    final = result.equity_curve[-1]
    unrounded_sell_price = final.close * Decimal("0.97")
    rounded_sell_price = (unrounded_sell_price / rules.price_tick_cad).to_integral_value(
        rounding=ROUND_FLOOR
    ) * rules.price_tick_cad
    liquidation_notional = final.btc * rounded_sell_price

    assert rounded_sell_price < unrounded_sell_price
    assert final.estimated_liquidation_slippage_cad == (
        final.btc_mark_value_cad - liquidation_notional
    )
    assert final.estimated_liquidation_fee_cad == liquidation_notional * Decimal("0.01")
    assert final.equity == final.cash + liquidation_notional * Decimal("0.99")


def test_risk_reducing_sell_rounding_never_exceeds_minute_participation_cap() -> None:
    candles = rising(202)
    candles[200] = candle(200, Decimal("50"), open_price=Decimal("50"))
    entry = precise_reference(candles, 200, Decimal("500"))
    exit_decision = candles[200].close_time + timedelta(minutes=15)
    exit_reference = ExecutionReference(
        decision_time=exit_decision,
        execution_time=exit_decision + timedelta(minutes=1),
        reference_price=Decimal("50"),
        volume_btc=Decimal("0.00123456"),
        trade_count=2,
    )
    config = BacktestConfig(
        instrument_rules=InstrumentRules(
            price_tick_cad=Decimal("0.1"),
            quantity_increment_btc=Decimal("0.00000001"),
            minimum_quantity_btc=Decimal("0.00000001"),
            minimum_cost_cad=Decimal("0.00000001"),
        ),
        max_execution_volume_fraction=Decimal("0.10"),
        rebalance_min_cad=Decimal("0"),
        rebalance_equity_fraction=Decimal("0"),
    )

    result = run_backtest(
        candles,
        config=config,
        execution_references=(entry, exit_reference),
        evaluation_start=entry.decision_time,
    )
    sell = result.trades[-1]

    assert sell.side is TradeSide.SELL
    assert sell.quantity_btc <= exit_reference.volume_btc * Decimal("0.10")
    assert sell.volume_participation_fraction is not None
    assert sell.volume_participation_fraction <= Decimal("0.10")
