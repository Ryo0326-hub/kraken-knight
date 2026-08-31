"""Causal, cost-aware replay engine for the deterministic strategy."""

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
from .strategy import MomentumTrendStrategy, StrategyDecision, StrategyPolicy


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExecutionOutcome(StrEnum):
    BUY = "buy"
    SELL = "sell"
    WITHIN_BAND = "within_band"
    ZERO_QUANTITY = "zero_quantity"
    NO_ACTION_DATA_INVALID = "no_action_data_invalid"
    NO_ACTION_STALE_SIGNAL = "no_action_stale_signal"
    NO_ACTION_RISK_GATE = "no_action_risk_gate"
    NO_ACTION_DISARMED = "no_action_disarmed"
    FORCED_LIQUIDATION = "forced_liquidation"


class RiskEventType(StrEnum):
    ROLLING_24H_LOSS_GATE = "rolling_24h_loss_gate"
    EXPOSURE_INCREASE_BLOCKED = "exposure_increase_blocked"
    DRAWDOWN_DISARMED = "drawdown_disarmed"
    FORCED_LIQUIDATION = "forced_liquidation"


@dataclass(frozen=True, slots=True)
class ExecutionCosts:
    """Explicit fee and slippage assumptions used by the simulator."""

    maker_fee_rate: Decimal = Decimal("0.004")
    taker_fee_rate: Decimal = Decimal("0.008")
    slippage_rate: Decimal = Decimal("0")
    buy_liquidity: Liquidity = Liquidity.MAKER
    sell_liquidity: Liquidity = Liquidity.TAKER

    def __post_init__(self) -> None:
        for name in ("maker_fee_rate", "taker_fee_rate", "slippage_rate"):
            value = to_decimal(getattr(self, name), field=name)
            object.__setattr__(self, name, value)
            if value < 0 or value >= 1:
                raise ValueError(f"{name} must be in [0, 1)")
        try:
            object.__setattr__(self, "buy_liquidity", Liquidity(self.buy_liquidity))
            object.__setattr__(self, "sell_liquidity", Liquidity(self.sell_liquidity))
        except ValueError as exc:
            raise ValueError("liquidity must be 'maker' or 'taker'") from exc

    def liquidity_for(self, side: TradeSide) -> Liquidity:
        return self.buy_liquidity if side is TradeSide.BUY else self.sell_liquidity

    def fee_rate_for(self, side: TradeSide) -> Decimal:
        liquidity = self.liquidity_for(side)
        return self.maker_fee_rate if liquidity is Liquidity.MAKER else self.taker_fee_rate


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal("1000")
    costs: ExecutionCosts = ExecutionCosts()
    cash_reserve_cad: Decimal = Decimal("200")
    absolute_btc_cap_cad: Decimal = Decimal("800")
    max_post_cost_exposure: Decimal = Decimal("0.80")
    rebalance_min_cad: Decimal = Decimal("50")
    rebalance_equity_fraction: Decimal = Decimal("0.05")
    rolling_24h_loss_threshold: Decimal = Decimal("0.08")
    max_drawdown_threshold: Decimal = Decimal("0.20")
    decision_delay_minutes: int = 15
    daily_execution_lag_days: int = 1

    def __post_init__(self) -> None:
        initial_cash = to_decimal(self.initial_cash, field="initial_cash")
        cash_reserve = to_decimal(self.cash_reserve_cad, field="cash_reserve_cad")
        absolute_cap = to_decimal(self.absolute_btc_cap_cad, field="absolute_btc_cap_cad")
        exposure_cap = to_decimal(self.max_post_cost_exposure, field="max_post_cost_exposure")
        minimum = to_decimal(self.rebalance_min_cad, field="rebalance_min_cad")
        fraction = to_decimal(self.rebalance_equity_fraction, field="rebalance_equity_fraction")
        rolling_loss = to_decimal(
            self.rolling_24h_loss_threshold,
            field="rolling_24h_loss_threshold",
        )
        max_drawdown = to_decimal(
            self.max_drawdown_threshold,
            field="max_drawdown_threshold",
        )
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "cash_reserve_cad", cash_reserve)
        object.__setattr__(self, "absolute_btc_cap_cad", absolute_cap)
        object.__setattr__(self, "max_post_cost_exposure", exposure_cap)
        object.__setattr__(self, "rebalance_min_cad", minimum)
        object.__setattr__(self, "rebalance_equity_fraction", fraction)
        object.__setattr__(self, "rolling_24h_loss_threshold", rolling_loss)
        object.__setattr__(self, "max_drawdown_threshold", max_drawdown)
        if initial_cash <= 0:
            raise ValueError("initial_cash must be greater than zero")
        if cash_reserve < 0:
            raise ValueError("cash_reserve_cad cannot be negative")
        if absolute_cap < 0:
            raise ValueError("absolute_btc_cap_cad cannot be negative")
        if exposure_cap < 0 or exposure_cap > 1:
            raise ValueError("max_post_cost_exposure must be in [0, 1]")
        if minimum < 0:
            raise ValueError("rebalance_min_cad cannot be negative")
        if fraction < 0 or fraction > 1:
            raise ValueError("rebalance_equity_fraction must be in [0, 1]")
        if rolling_loss <= 0 or rolling_loss >= 1:
            raise ValueError("rolling_24h_loss_threshold must be in (0, 1)")
        if max_drawdown <= 0 or max_drawdown >= 1:
            raise ValueError("max_drawdown_threshold must be in (0, 1)")
        if (
            isinstance(self.decision_delay_minutes, bool)
            or not isinstance(self.decision_delay_minutes, int)
            or self.decision_delay_minutes <= 0
        ):
            raise ValueError("decision_delay_minutes must be a positive integer")
        if (
            isinstance(self.daily_execution_lag_days, bool)
            or not isinstance(self.daily_execution_lag_days, int)
            or self.daily_execution_lag_days <= 0
        ):
            raise ValueError("daily_execution_lag_days must be a positive integer")
        if self.decision_delay_minutes >= self.daily_execution_lag_days * 24 * 60:
            raise ValueError("decision delay must precede the scheduled execution open")
        if not isinstance(self.costs, ExecutionCosts):
            raise TypeError("costs must be an ExecutionCosts instance")


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    intent_id: str
    strategy_decision_id: str
    decision_time: datetime
    execution_time: datetime
    side: TradeSide
    liquidity: Liquidity
    quantity_btc: Decimal
    reference_price: Decimal
    execution_price: Decimal
    gross_notional_cad: Decimal
    fee_cad: Decimal
    slippage_cad: Decimal
    pre_trade_equity: Decimal
    cash_after: Decimal
    btc_after: Decimal

    def __post_init__(self) -> None:
        if self.execution_time < self.decision_time:
            raise ValueError("execution_time cannot precede decision_time")


@dataclass(frozen=True, slots=True)
class RebalanceDecision:
    """Audit record joining a close-time signal to next-candle execution."""

    intent_id: str
    strategy_decision_id: str
    signal_time: datetime | None
    decision_time: datetime
    execution_time: datetime
    strategy_reason: str
    target_weight: Decimal
    pre_trade_equity: Decimal
    current_btc_value: Decimal
    estimated_liquidation_fee_cad: Decimal
    requested_delta_cad: Decimal
    rebalance_band_cad: Decimal
    outcome: ExecutionOutcome
    trade_id: str | None

    def __post_init__(self) -> None:
        if self.execution_time < self.decision_time:
            raise ValueError("execution_time cannot precede decision_time")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    close_time: datetime
    close: Decimal
    cash: Decimal
    btc: Decimal
    equity: Decimal
    btc_mark_value_cad: Decimal
    estimated_liquidation_fee_cad: Decimal
    cumulative_fees: Decimal


@dataclass(frozen=True, slots=True)
class RiskEvent:
    event_id: str
    event_type: RiskEventType
    observed_at: datetime
    strategy_decision_id: str | None
    equity: Decimal
    reference_equity: Decimal
    observed_fraction: Decimal
    threshold: Decimal
    action: str


@dataclass(frozen=True, slots=True)
class RiskState:
    high_water_equity: Decimal
    drawdown_disarmed: bool
    rolling_loss_blocked_until: datetime | None


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    cagr: Decimal
    annualized_volatility: Decimal
    sharpe: Decimal
    max_drawdown: Decimal
    turnover: Decimal
    total_fees: Decimal
    fee_drag: Decimal
    trade_count: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    policy: StrategyPolicy
    equity_curve: tuple[EquityPoint, ...]
    benchmark_curve: tuple[EquityPoint, ...]
    strategy_decisions: tuple[StrategyDecision, ...]
    decisions: tuple[RebalanceDecision, ...]
    trades: tuple[Trade, ...]
    risk_events: tuple[RiskEvent, ...]
    final_risk_state: RiskState
    metrics: PerformanceMetrics
    benchmark_metrics: PerformanceMetrics


def _content_id(prefix: str, fields: dict[str, object]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _validate_backtest_candles(candles: Sequence[Candle]) -> None:
    if not candles:
        raise ValueError("at least one candle is required")
    if any(not isinstance(candle, Candle) for candle in candles):
        raise TypeError("all backtest observations must be Candle instances")
    if any(not candle.complete for candle in candles):
        raise ValueError("backtests cannot use incomplete candles")
    if any(right.open_time <= left.open_time for left, right in pairwise(candles)):
        raise ValueError("candle timestamps must be strictly increasing")


def _intent_id(
    strategy_decision: StrategyDecision,
    decision_time: datetime,
    execution_time: datetime,
    target_weight: Decimal,
    pre_trade_equity: Decimal,
    requested_delta: Decimal,
) -> str:
    return _content_id(
        "intent",
        {
            "strategy_decision_id": strategy_decision.decision_id,
            "decision_time": decision_time.isoformat(),
            "execution_time": execution_time.isoformat(),
            "target_weight": str(target_weight),
            "pre_trade_equity": str(pre_trade_equity),
            "requested_delta": str(requested_delta),
        },
    )


@dataclass(frozen=True, slots=True)
class _PortfolioSnapshot:
    equity: Decimal
    btc_mark_value: Decimal
    btc_liquidation_value: Decimal
    estimated_liquidation_fee: Decimal


def _portfolio_snapshot(
    *,
    cash: Decimal,
    btc: Decimal,
    reference_price: Decimal,
    costs: ExecutionCosts,
) -> _PortfolioSnapshot:
    btc_mark_value = btc * reference_price
    estimated_liquidation_fee = btc_mark_value * costs.taker_fee_rate
    btc_liquidation_value = btc_mark_value - estimated_liquidation_fee
    return _PortfolioSnapshot(
        equity=cash + btc_liquidation_value,
        btc_mark_value=btc_mark_value,
        btc_liquidation_value=btc_liquidation_value,
        estimated_liquidation_fee=estimated_liquidation_fee,
    )


def _execute_rebalance(
    *,
    signal: StrategyDecision,
    candle: Candle,
    decision_time: datetime,
    cash: Decimal,
    btc: Decimal,
    config: BacktestConfig,
    exposure_increase_blocked: bool,
    drawdown_disarmed: bool,
) -> tuple[Decimal, Decimal, RebalanceDecision, Trade | None]:
    reference_price = candle.open
    snapshot = _portfolio_snapshot(
        cash=cash,
        btc=btc,
        reference_price=reference_price,
        costs=config.costs,
    )
    pre_trade_equity = snapshot.equity
    if pre_trade_equity <= 0:
        raise RuntimeError("portfolio equity must remain positive")
    if candle.open_time < decision_time:
        raise RuntimeError("execution cannot precede the recorded decision time")
    if signal.signal_close_time is not None:
        expected_decision_time = signal.signal_close_time + timedelta(
            minutes=config.decision_delay_minutes
        )
        if decision_time != expected_decision_time:
            raise RuntimeError("decision_time does not match the configured schedule")

    current_btc_value = snapshot.btc_liquidation_value
    band = max(
        config.rebalance_min_cad,
        config.rebalance_equity_fraction * pre_trade_equity,
    )

    # Missing, malformed, or stale evidence blocks new risk but is not a sell
    # signal. This distinction prevents an API/data incident from causing a
    # blind liquidation. The raw strategy decision remains in BacktestResult.
    no_action_outcome: ExecutionOutcome | None = None
    if not signal.usable_data:
        no_action_outcome = ExecutionOutcome.NO_ACTION_DATA_INVALID
    elif (
        signal.signal_close_time is None
        or signal.signal_close_time + timedelta(days=config.daily_execution_lag_days)
        != candle.open_time
    ):
        no_action_outcome = ExecutionOutcome.NO_ACTION_STALE_SIGNAL
    if no_action_outcome is not None and not drawdown_disarmed:
        current_weight = current_btc_value / pre_trade_equity
        intent_id = _intent_id(
            signal,
            decision_time,
            candle.open_time,
            current_weight,
            pre_trade_equity,
            Decimal("0"),
        )
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=current_weight,
            pre_trade_equity=pre_trade_equity,
            current_btc_value=current_btc_value,
            estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
            requested_delta_cad=Decimal("0"),
            rebalance_band_cad=band,
            outcome=no_action_outcome,
            trade_id=None,
        )
        return cash, btc, decision, None

    post_cost_price = reference_price * (Decimal("1") - config.costs.taker_fee_rate)
    if post_cost_price <= 0:
        raise RuntimeError("liquidation value must remain positive")

    target_btc_value = signal.target_weight * pre_trade_equity
    desired_btc = min(
        target_btc_value / post_cost_price,
        config.absolute_btc_cap_cad / reference_price,
    )

    side_for_cap = TradeSide.BUY if desired_btc > btc else TradeSide.SELL
    cap_fee_rate = config.costs.fee_rate_for(side_for_cap)
    cap_execution_price = reference_price * (
        Decimal("1") + config.costs.slippage_rate
        if side_for_cap is TradeSide.BUY
        else Decimal("1") - config.costs.slippage_rate
    )
    cash_per_btc = cap_execution_price * (
        Decimal("1") + cap_fee_rate
        if side_for_cap is TradeSide.BUY
        else Decimal("1") - cap_fee_rate
    )

    exposure_cap = config.max_post_cost_exposure
    exposure_denominator = (
        Decimal("1") - exposure_cap
    ) * post_cost_price + exposure_cap * cash_per_btc
    if exposure_denominator > 0:
        maximum_for_fraction = exposure_cap * (cash + btc * cash_per_btc) / exposure_denominator
        desired_btc = min(desired_btc, maximum_for_fraction)
    else:
        desired_btc = Decimal("0")

    if cash < config.cash_reserve_cad and btc > 0:
        sell_fee_rate = config.costs.fee_rate_for(TradeSide.SELL)
        net_sell_cash_per_btc = (
            reference_price
            * (Decimal("1") - config.costs.slippage_rate)
            * (Decimal("1") - sell_fee_rate)
        )
        reserve_shortfall = config.cash_reserve_cad - cash
        maximum_remaining_btc = max(
            Decimal("0"),
            btc - reserve_shortfall / net_sell_cash_per_btc,
        )
        desired_btc = min(desired_btc, maximum_remaining_btc)
    elif desired_btc > btc:
        spendable_cash = max(Decimal("0"), cash - config.cash_reserve_cad)
        desired_btc = min(desired_btc, btc + spendable_cash / cash_per_btc)

    risk_gate_blocks_increase = exposure_increase_blocked and desired_btc > btc
    if risk_gate_blocks_increase:
        desired_btc = btc
    if drawdown_disarmed:
        desired_btc = Decimal("0")

    target_btc_value = desired_btc * post_cost_price
    effective_target_weight = target_btc_value / pre_trade_equity
    requested_delta = target_btc_value - current_btc_value
    intent_id = _intent_id(
        signal,
        decision_time,
        candle.open_time,
        effective_target_weight,
        pre_trade_equity,
        requested_delta,
    )

    if risk_gate_blocks_increase:
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=effective_target_weight,
            pre_trade_equity=pre_trade_equity,
            current_btc_value=current_btc_value,
            estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
            requested_delta_cad=Decimal("0"),
            rebalance_band_cad=band,
            outcome=ExecutionOutcome.NO_ACTION_RISK_GATE,
            trade_id=None,
        )
        return cash, btc, decision, None

    if drawdown_disarmed and btc == 0:
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=Decimal("0"),
            pre_trade_equity=pre_trade_equity,
            current_btc_value=Decimal("0"),
            estimated_liquidation_fee_cad=Decimal("0"),
            requested_delta_cad=Decimal("0"),
            rebalance_band_cad=band,
            outcome=ExecutionOutcome.NO_ACTION_DISARMED,
            trade_id=None,
        )
        return cash, btc, decision, None

    # A transition to cash is risk reduction.  It bypasses the normal drift
    # band so a small residual is not deliberately retained; a live adapter may
    # still classify an exchange-minimum remainder as dust.
    current_post_cost_fraction = current_btc_value / pre_trade_equity
    cap_reduction = desired_btc < btc and (
        snapshot.btc_mark_value > config.absolute_btc_cap_cad
        or current_post_cost_fraction > config.max_post_cost_exposure
        or cash < config.cash_reserve_cad
    )
    risk_off_reduction = (signal.target_weight == 0 or drawdown_disarmed) and btc > 0
    if abs(requested_delta) < band and not risk_off_reduction and not cap_reduction:
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=effective_target_weight,
            pre_trade_equity=pre_trade_equity,
            current_btc_value=current_btc_value,
            estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
            requested_delta_cad=requested_delta,
            rebalance_band_cad=band,
            outcome=ExecutionOutcome.WITHIN_BAND,
            trade_id=None,
        )
        return cash, btc, decision, None

    signed_quantity = desired_btc - btc
    if signed_quantity == 0:
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=effective_target_weight,
            pre_trade_equity=pre_trade_equity,
            current_btc_value=current_btc_value,
            estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
            requested_delta_cad=requested_delta,
            rebalance_band_cad=band,
            outcome=ExecutionOutcome.ZERO_QUANTITY,
            trade_id=None,
        )
        return cash, btc, decision, None

    side = TradeSide.BUY if signed_quantity > 0 else TradeSide.SELL
    liquidity = config.costs.liquidity_for(side)
    fee_rate = config.costs.fee_rate_for(side)
    slippage_multiplier = (
        Decimal("1") + config.costs.slippage_rate
        if side is TradeSide.BUY
        else Decimal("1") - config.costs.slippage_rate
    )
    execution_price = reference_price * slippage_multiplier
    quantity = abs(signed_quantity)

    if side is TradeSide.BUY:
        spendable_cash = max(Decimal("0"), cash - config.cash_reserve_cad)
        maximum_affordable = spendable_cash / (execution_price * (Decimal("1") + fee_rate))
        quantity = min(quantity, maximum_affordable)
    else:
        quantity = min(quantity, btc)

    if quantity <= 0:
        decision = RebalanceDecision(
            intent_id=intent_id,
            strategy_decision_id=signal.decision_id,
            signal_time=signal.signal_close_time,
            decision_time=decision_time,
            execution_time=candle.open_time,
            strategy_reason=signal.reason.value,
            target_weight=effective_target_weight,
            pre_trade_equity=pre_trade_equity,
            current_btc_value=current_btc_value,
            estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
            requested_delta_cad=requested_delta,
            rebalance_band_cad=band,
            outcome=ExecutionOutcome.ZERO_QUANTITY,
            trade_id=None,
        )
        return cash, btc, decision, None

    gross_notional = quantity * execution_price
    fee = gross_notional * fee_rate
    slippage = quantity * abs(execution_price - reference_price)
    if side is TradeSide.BUY:
        new_cash = cash - gross_notional - fee
        new_btc = btc + quantity
    else:
        new_cash = cash + gross_notional - fee
        new_btc = btc - quantity

    # Decimal arithmetic can leave a signed zero or an insignificant negative
    # after an affordability cap.  Holdings are never allowed below zero.
    if new_cash < 0 and abs(new_cash) < Decimal("1e-20"):
        new_cash = Decimal("0")
    if new_btc < 0 and abs(new_btc) < Decimal("1e-20"):
        new_btc = Decimal("0")
    if new_cash < 0 or new_btc < 0:
        raise RuntimeError("execution produced a negative portfolio balance")

    post_trade_snapshot = _portfolio_snapshot(
        cash=new_cash,
        btc=new_btc,
        reference_price=reference_price,
        costs=config.costs,
    )
    tolerance = Decimal("1e-18")
    if post_trade_snapshot.btc_mark_value > config.absolute_btc_cap_cad + tolerance:
        raise RuntimeError("execution exceeded the absolute BTC exposure cap")
    if (
        post_trade_snapshot.btc_liquidation_value
        > config.max_post_cost_exposure * post_trade_snapshot.equity + tolerance
    ):
        raise RuntimeError("execution exceeded the post-cost exposure cap")
    if new_btc > tolerance and new_cash + tolerance < config.cash_reserve_cad:
        raise RuntimeError("execution spent the required CAD reserve")

    trade_id = _content_id(
        "trade",
        {
            "intent_id": intent_id,
            "side": side.value,
            "quantity_btc": str(quantity),
            "execution_price": str(execution_price),
            "gross_notional_cad": str(gross_notional),
            "fee_cad": str(fee),
        },
    )
    trade = Trade(
        trade_id=trade_id,
        intent_id=intent_id,
        strategy_decision_id=signal.decision_id,
        decision_time=decision_time,
        execution_time=candle.open_time,
        side=side,
        liquidity=liquidity,
        quantity_btc=quantity,
        reference_price=reference_price,
        execution_price=execution_price,
        gross_notional_cad=gross_notional,
        fee_cad=fee,
        slippage_cad=slippage,
        pre_trade_equity=pre_trade_equity,
        cash_after=new_cash,
        btc_after=new_btc,
    )
    outcome = (
        ExecutionOutcome.FORCED_LIQUIDATION
        if drawdown_disarmed
        else (ExecutionOutcome.BUY if side is TradeSide.BUY else ExecutionOutcome.SELL)
    )
    decision = RebalanceDecision(
        intent_id=intent_id,
        strategy_decision_id=signal.decision_id,
        signal_time=signal.signal_close_time,
        decision_time=decision_time,
        execution_time=candle.open_time,
        strategy_reason=signal.reason.value,
        target_weight=effective_target_weight,
        pre_trade_equity=pre_trade_equity,
        current_btc_value=current_btc_value,
        estimated_liquidation_fee_cad=snapshot.estimated_liquidation_fee,
        requested_delta_cad=requested_delta,
        rebalance_band_cad=band,
        outcome=outcome,
        trade_id=trade_id,
    )
    return new_cash, new_btc, decision, trade


def _metrics(curve: Sequence[EquityPoint], trades: Sequence[Trade]) -> PerformanceMetrics:
    equities = [point.equity for point in curve]
    initial = equities[0]
    final = equities[-1]
    total_return = final / initial - Decimal("1")

    elapsed_days = (curve[-1].close_time - curve[0].close_time).total_seconds() / 86400
    if elapsed_days > 0 and final > 0:
        cagr_float = math.pow(float(final / initial), 365.0 / elapsed_days) - 1.0
        cagr = Decimal(str(cagr_float))
    else:
        cagr = Decimal("0")

    returns = [float(current / previous - Decimal("1")) for previous, current in pairwise(equities)]
    if len(returns) >= 2:
        daily_vol = statistics.stdev(returns)
        annualized_volatility = Decimal(str(daily_vol * math.sqrt(365)))
        sharpe_float = (
            statistics.mean(returns) / daily_vol * math.sqrt(365) if daily_vol > 0 else 0.0
        )
        sharpe = Decimal(str(sharpe_float))
    else:
        annualized_volatility = Decimal("0")
        sharpe = Decimal("0")

    peak = equities[0]
    max_drawdown = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)

    total_fees = sum((trade.fee_cad for trade in trades), start=Decimal("0"))
    turnover = sum(
        (
            trade.gross_notional_cad / trade.pre_trade_equity
            for trade in trades
            if trade.pre_trade_equity > 0
        ),
        start=Decimal("0"),
    )
    return PerformanceMetrics(
        initial_equity=initial,
        final_equity=final,
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        turnover=turnover,
        total_fees=total_fees,
        fee_drag=total_fees / initial,
        trade_count=len(trades),
    )


def _risk_event(
    *,
    event_type: RiskEventType,
    observed_at: datetime,
    strategy_decision_id: str | None,
    equity: Decimal,
    reference_equity: Decimal,
    observed_fraction: Decimal,
    threshold: Decimal,
    action: str,
) -> RiskEvent:
    fields: dict[str, object] = {
        "event_type": event_type.value,
        "observed_at": observed_at.isoformat(),
        "strategy_decision_id": strategy_decision_id,
        "equity": str(equity),
        "reference_equity": str(reference_equity),
        "observed_fraction": str(observed_fraction),
        "threshold": str(threshold),
        "action": action,
    }
    return RiskEvent(
        event_id=_content_id("risk", fields),
        event_type=event_type,
        observed_at=observed_at,
        strategy_decision_id=strategy_decision_id,
        equity=equity,
        reference_equity=reference_equity,
        observed_fraction=observed_fraction,
        threshold=threshold,
        action=action,
    )


def run_backtest(
    candles: Sequence[Candle],
    strategy: MomentumTrendStrategy | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay one daily signal with an explicit, executable information clock.

    A candle closes at 00:00 UTC, its strategy decision is recorded at 00:15,
    and daily-only replay cannot fill at the already-passed 00:00 open.  It can
    first execute at 00:00 on the following day.  Thus execution candle ``e``
    is driven only by signal candle ``e - 2``.
    """

    _validate_backtest_candles(candles)
    selected_strategy = strategy or MomentumTrendStrategy()
    selected_config = config or BacktestConfig()

    cash = selected_config.initial_cash
    btc = Decimal("0")
    cumulative_fees = Decimal("0")
    initial_snapshot = _portfolio_snapshot(
        cash=cash,
        btc=btc,
        reference_price=candles[0].close,
        costs=selected_config.costs,
    )
    equity_curve: list[EquityPoint] = [
        EquityPoint(
            close_time=candles[0].close_time,
            close=candles[0].close,
            cash=cash,
            btc=btc,
            equity=initial_snapshot.equity,
            btc_mark_value_cad=initial_snapshot.btc_mark_value,
            estimated_liquidation_fee_cad=initial_snapshot.estimated_liquidation_fee,
            cumulative_fees=cumulative_fees,
        )
    ]
    decisions: list[RebalanceDecision] = []
    strategy_decisions: list[StrategyDecision] = []
    trades: list[Trade] = []
    risk_events: list[RiskEvent] = []
    high_water_equity = initial_snapshot.equity
    drawdown_disarmed = False
    rolling_loss_blocked_until: datetime | None = None
    risk_observations: list[tuple[datetime, Decimal]] = [
        (candles[0].open_time, selected_config.initial_cash),
        (candles[0].close_time, initial_snapshot.equity),
    ]

    for execution_index in range(1, len(candles)):
        execution_candle = candles[execution_index]
        execution_snapshot = _portfolio_snapshot(
            cash=cash,
            btc=btc,
            reference_price=execution_candle.open,
            costs=selected_config.costs,
        )
        high_water_equity = max(high_water_equity, execution_snapshot.equity)

        signal: StrategyDecision | None = None
        decision_time: datetime | None = None
        if execution_index >= 2:
            # Do not expose candle e-1: it closes only at the execution open.
            signal = selected_strategy.evaluate(candles[: execution_index - 1])
            strategy_decisions.append(signal)
            if signal.signal_close_time is None:
                raise RuntimeError("a daily strategy decision must identify its signal close")
            decision_time = signal.signal_close_time + timedelta(
                minutes=selected_config.decision_delay_minutes
            )

        observation_cutoff = execution_candle.open_time - timedelta(days=1)
        reference_24h = next(
            (
                equity
                for observed_at, equity in reversed(risk_observations)
                if observed_at <= observation_cutoff
            ),
            None,
        )
        rolling_loss = Decimal("0")
        if reference_24h is not None and reference_24h > 0:
            rolling_loss = max(
                Decimal("0"),
                Decimal("1") - execution_snapshot.equity / reference_24h,
            )
            if rolling_loss >= selected_config.rolling_24h_loss_threshold:
                rolling_loss_blocked_until = execution_candle.open_time + timedelta(days=1)
                risk_events.append(
                    _risk_event(
                        event_type=RiskEventType.ROLLING_24H_LOSS_GATE,
                        observed_at=execution_candle.open_time,
                        strategy_decision_id=signal.decision_id if signal else None,
                        equity=execution_snapshot.equity,
                        reference_equity=reference_24h,
                        observed_fraction=rolling_loss,
                        threshold=selected_config.rolling_24h_loss_threshold,
                        action="block_exposure_increases_for_24h",
                    )
                )
        if (
            rolling_loss_blocked_until is not None
            and execution_candle.open_time >= rolling_loss_blocked_until
        ):
            rolling_loss_blocked_until = None

        drawdown = Decimal("1") - execution_snapshot.equity / high_water_equity
        if not drawdown_disarmed and drawdown >= selected_config.max_drawdown_threshold:
            drawdown_disarmed = True
            risk_events.append(
                _risk_event(
                    event_type=RiskEventType.DRAWDOWN_DISARMED,
                    observed_at=execution_candle.open_time,
                    strategy_decision_id=signal.decision_id if signal else None,
                    equity=execution_snapshot.equity,
                    reference_equity=high_water_equity,
                    observed_fraction=drawdown,
                    threshold=selected_config.max_drawdown_threshold,
                    action="persist_disarm_and_force_liquidation",
                )
            )

        exposure_increase_blocked = (
            rolling_loss_blocked_until is not None
            and execution_candle.open_time < rolling_loss_blocked_until
        )
        if signal is not None and decision_time is not None:
            cash, btc, decision, trade = _execute_rebalance(
                signal=signal,
                candle=execution_candle,
                decision_time=decision_time,
                cash=cash,
                btc=btc,
                config=selected_config,
                exposure_increase_blocked=exposure_increase_blocked,
                drawdown_disarmed=drawdown_disarmed,
            )
            decisions.append(decision)
            if decision.outcome is ExecutionOutcome.NO_ACTION_RISK_GATE:
                risk_events.append(
                    _risk_event(
                        event_type=RiskEventType.EXPOSURE_INCREASE_BLOCKED,
                        observed_at=execution_candle.open_time,
                        strategy_decision_id=signal.decision_id,
                        equity=execution_snapshot.equity,
                        reference_equity=reference_24h or execution_snapshot.equity,
                        observed_fraction=rolling_loss,
                        threshold=selected_config.rolling_24h_loss_threshold,
                        action="hold_current_exposure",
                    )
                )
            if trade is not None:
                trades.append(trade)
                cumulative_fees += trade.fee_cad
                if decision.outcome is ExecutionOutcome.FORCED_LIQUIDATION:
                    risk_events.append(
                        _risk_event(
                            event_type=RiskEventType.FORCED_LIQUIDATION,
                            observed_at=execution_candle.open_time,
                            strategy_decision_id=signal.decision_id,
                            equity=execution_snapshot.equity,
                            reference_equity=high_water_equity,
                            observed_fraction=drawdown,
                            threshold=selected_config.max_drawdown_threshold,
                            action="sell_all_btc_with_configured_exit_costs",
                        )
                    )

        post_execution_snapshot = _portfolio_snapshot(
            cash=cash,
            btc=btc,
            reference_price=execution_candle.open,
            costs=selected_config.costs,
        )
        risk_observations.append((execution_candle.open_time, post_execution_snapshot.equity))
        close_snapshot = _portfolio_snapshot(
            cash=cash,
            btc=btc,
            reference_price=execution_candle.close,
            costs=selected_config.costs,
        )
        high_water_equity = max(high_water_equity, close_snapshot.equity)
        risk_observations.append((execution_candle.close_time, close_snapshot.equity))
        equity_curve.append(
            EquityPoint(
                close_time=execution_candle.close_time,
                close=execution_candle.close,
                cash=cash,
                btc=btc,
                equity=close_snapshot.equity,
                btc_mark_value_cad=close_snapshot.btc_mark_value,
                estimated_liquidation_fee_cad=(close_snapshot.estimated_liquidation_fee),
                cumulative_fees=cumulative_fees,
            )
        )

    benchmark_units = selected_config.initial_cash / candles[0].close
    benchmark_curve = tuple(
        EquityPoint(
            close_time=candle.close_time,
            close=candle.close,
            cash=Decimal("0"),
            btc=benchmark_units,
            equity=benchmark_units * candle.close,
            btc_mark_value_cad=benchmark_units * candle.close,
            estimated_liquidation_fee_cad=Decimal("0"),
            cumulative_fees=Decimal("0"),
        )
        for candle in candles
    )
    strategy_curve_tuple = tuple(equity_curve)
    trades_tuple = tuple(trades)
    return BacktestResult(
        config=selected_config,
        policy=selected_strategy.policy,
        equity_curve=strategy_curve_tuple,
        benchmark_curve=benchmark_curve,
        strategy_decisions=tuple(strategy_decisions),
        decisions=tuple(decisions),
        trades=trades_tuple,
        risk_events=tuple(risk_events),
        final_risk_state=RiskState(
            high_water_equity=high_water_equity,
            drawdown_disarmed=drawdown_disarmed,
            rolling_loss_blocked_until=rolling_loss_blocked_until,
        ),
        metrics=_metrics(strategy_curve_tuple, trades_tuple),
        benchmark_metrics=_metrics(benchmark_curve, ()),
    )
