# BTC/CAD production V3 policy

## Status

Strategy identifier: `btc_cad_daily_momentum_v3_no_drawdown`

This is the configured production strategy after the separately labelled V2
drawdown-policy counterfactual. It is deterministic, spot-only, long-or-cash,
and evaluated once per UTC day. The current Checkpoint 2 application remains
read-only: selecting this strategy does not create an exchange-write path or
authorize deployment.

The sealed V1 specification, V1 result, V2 pre-registration, and V2 result
remain historical research records. They are not rewritten by this promotion.

## Frozen signal and sizing

V3 retains the V1 calculation without parameter changes:

- BTC/CAD on Kraken Spot;
- positive 90-day return and close strictly above the 200-day SMA;
- 30-day realized volatility annualized with 365 days;
- 25% annualized-volatility target, capped at 80% exposure;
- C$200 minimum CAD reserve and C$800 absolute BTC notional cap for the
  controlled C$1,000 sleeve;
- normal rebalance threshold equal to the greater of C$50 or 5% of equity;
- one causal decision at 00:15 UTC from completed daily candles only; and
- no leverage, margin, shorting, borrowing, derivatives, discretionary model
  override, or Blockchair input to production decisions.

The formulas, causal clock, validation rules, target calculation, order
rounding, and deterministic identity remain as specified in the sealed
[`STRATEGY_SPEC.md`](STRATEGY_SPEC.md), except for the drawdown-policy delta
below.

## Only policy change: no automatic drawdown gate

V3 uses `drawdown_policy_mode=disabled`. High-water equity and drawdown remain
observable telemetry, but crossing 20% does not by itself:

- change the momentum strategy's target;
- force BTC liquidation;
- cancel an otherwise valid strategy order;
- disarm the service; or
- require a drawdown-specific rearm.

There is no cooldown or automatic-rearm state in V3. This is deliberately the
same `disabled` variant evaluated in the sealed V2 counterfactual—not a newly
tuned rule after seeing the result.

## Controls that remain active

Removing the named drawdown gate does not weaken any other failure boundary.
The following still block exposure increases or disarm operation as applicable:

- the 8% rolling-24-hour loss gate and its full 24-hour block;
- incomplete, stale, duplicated, or inconsistent market data;
- stale/crossed books and entry spread above 20 basis points;
- unknown instrument rules, fees, balances, orders, fills, or liabilities;
- an unexplained account mismatch or manual/foreign account activity;
- an unknown submission outcome, concurrent writer, credential failure, clock
  failure, database-integrity failure, or operator disarm;
- the cash reserve, percentage exposure cap, absolute C$800 BTC cap, exchange
  increments/minimums, fee affordability, and rebalance threshold; and
- bounded post-only entry attempts and the 50-basis-point risk-exit collar.

Risk reduction from the momentum signal, an account-safety incident, or an
operator target-cash instruction continues normally. Only a drawdown level by
itself loses authority over the target and arm state.

## Version and replay boundary

Production settings freeze the V3 identifier and disabled mode together and
include the mode in the configuration fingerprint. The mode is not an
environment-tunable switch.

The generic replay engine intentionally keeps `persistent` as its omitted-value
default so the sealed V1 control cannot silently change. Any V3 engineering or
release replay must select `disabled` explicitly. The public smoke script does
so. V1/V2 research configurations and published artifacts remain unchanged.

## Release requirement

The V2 result is exploratory because the old holdout had already been opened.
It supports choosing a controlled experiment; it does not establish future
profitability. Before exchange writes, V3 still requires the documented
reconciliation, validation, shadow, operational-probe, canary, observability,
and rollback gates. The absence of a live adapter in Checkpoint 2 is a hard
engineering blocker, not a configuration problem.
