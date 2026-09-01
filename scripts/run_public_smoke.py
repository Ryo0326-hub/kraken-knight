"""Run a non-trading smoke evaluation against Kraken's public OHLC endpoint."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from kraken_knight.backtest import BacktestConfig, run_backtest
from kraken_knight.config import FrozenRiskSettings
from kraken_knight.market_data import KrakenPublicClient, validate_batch_freshness
from kraken_knight.strategy import MomentumTrendStrategy


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def main() -> None:
    """Print a JSON smoke report without touching authenticated endpoints."""

    batch = KrakenPublicClient().fetch_daily_ohlc()
    evaluated_at = datetime.now(UTC)
    validate_batch_freshness(batch, evaluated_at=evaluated_at)
    if len(batch.completed) < MomentumTrendStrategy().policy.minimum_history:
        raise RuntimeError("Kraken public window is too short for the frozen strategy")

    production_risk = FrozenRiskSettings()
    decision = MomentumTrendStrategy().evaluate(batch.completed)
    replay = run_backtest(
        batch.completed,
        config=BacktestConfig(
            cash_reserve_cad=production_risk.cash_reserve_cad,
            absolute_btc_cap_cad=production_risk.absolute_btc_cap_cad,
            max_post_cost_exposure=production_risk.max_exposure_fraction,
            rebalance_min_cad=production_risk.min_rebalance_notional_cad,
            rebalance_equity_fraction=production_risk.min_rebalance_equity_fraction,
            rolling_24h_loss_threshold=production_risk.rolling_24h_loss_gate_fraction,
            max_drawdown_threshold=production_risk.high_water_drawdown_fraction,
            drawdown_policy_mode=production_risk.drawdown_policy_mode,
        ),
    )
    decision_time = decision.signal_close_time and decision.signal_close_time + timedelta(
        minutes=replay.config.decision_delay_minutes
    )
    report = {
        "report_type": "engineering_smoke_not_profitability_evidence",
        "generated_at": evaluated_at,
        "source": {
            "pair": batch.raw_pair_key,
            "completed_candles": len(batch.completed),
            "first_day": batch.completed[0].open_time,
            "last_completed_day": batch.completed[-1].open_time,
            "quarantined_mutable_day": batch.mutable_tail.open_time,
            "response_sha256": batch.raw_sha256,
        },
        "current_decision": asdict(decision),
        "current_decision_time": decision_time,
        "assumptions": {
            "strategy_policy": asdict(replay.policy),
            "backtest_config": asdict(replay.config),
            "timing": {
                "signal_input": "completed UTC daily candle",
                "decision_delay_minutes_after_close": (replay.config.decision_delay_minutes),
                "daily_execution_lag_days_after_signal_close": (
                    replay.config.daily_execution_lag_days
                ),
                "earliest_fill": "following UTC day's 00:00 open",
            },
            "equity": ("CAD cash plus BTC mark value less estimated taker liquidation fee"),
            "risk": {
                "absolute_btc_cap_cad": replay.config.absolute_btc_cap_cad,
                "max_post_cost_exposure": replay.config.max_post_cost_exposure,
                "cash_reserve_cad": replay.config.cash_reserve_cad,
                "rolling_24h_loss_threshold": (replay.config.rolling_24h_loss_threshold),
                "max_drawdown_threshold": replay.config.max_drawdown_threshold,
                "drawdown_policy_mode": replay.config.drawdown_policy_mode,
                "drawdown_action": "telemetry only; no drawdown-triggered target change",
            },
        },
        "replay": {
            "strategy": asdict(replay.metrics),
            "buy_and_hold_without_costs": asdict(replay.benchmark_metrics),
            "risk_events": [asdict(event) for event in replay.risk_events],
            "final_risk_state": asdict(replay.final_risk_state),
        },
        "limitations": [
            "Kraken public OHLC is a short rolling window, not the research dataset.",
            (
                "The daily-open replay deliberately delays execution to the next UTC open; "
                "it is a sensitivity approximation, not evidence of a fill at 00:15."
            ),
            (
                "The engineering simulator assumes complete fills and does not yet model "
                "maker queue position, exchange increments/minimums, or dust."
            ),
            "The benchmark currently excludes trading costs.",
            "The replay uses declared cost assumptions, not authenticated account fees.",
            "Fee-aware equity estimates liquidation fees but cannot guarantee a fill price.",
            "This command never authenticates or submits an order.",
        ],
    }
    print(json.dumps(report, default=_json_default, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
