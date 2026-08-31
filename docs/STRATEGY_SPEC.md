# BTC/CAD V1 Strategy Specification

## 1. Status and interpretation

This document is the normative contract for the first production strategy.
Terms such as **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used in their
ordinary requirements sense. Any implementation difference requires a new,
reviewed strategy version; it must not be introduced as an operational hotfix.

Strategy identifier: `btc_cad_daily_momentum_v1`

The strategy is deterministic, spot-only, long-or-cash, and evaluated once per
UTC day. Its purpose is to create a comprehensible and testable trading
experiment. It does not promise positive returns.

## 2. Market and account scope

- Instrument: Bitcoin against Canadian dollars on Kraken Spot.
- Direction: long BTC or CAD cash only.
- Account: one dedicated account/sleeve with no unrelated automated writer.
- Initial sleeve equity: C$1,000.
- Borrowing, margin, leverage, derivatives, and short sales: prohibited.
- AI/ML/LLM changes to signals, position targets, risk gates, or orders: prohibited.
- Manual holdings or orders inside the dedicated account: prohibited during
  autonomous operation except an explicitly documented recovery action.

The Kraken instrument identifier, minimum order quantity, minimum cost, quantity
precision, price tick, and current account fee tier MUST be discovered from
Kraken at runtime. They MUST NOT be hardcoded from this document.

## 3. Time and candle semantics

The scheduled decision time is **00:15:00 UTC** each calendar day.

Let candle `t` be the latest fully completed Kraken UTC daily candle covering
`[00:00:00 UTC, 00:00:00 UTC on the next day)`. At the decision time, the system
MUST use only candle `t` and candles that ended no later than it. A current,
still-forming daily candle MUST be discarded even if an API includes it.

The decision job MUST:

1. Normalize timestamps to UTC.
2. Require one unique candle for every expected day in the feature window.
3. Reject duplicate timestamps, nonpositive OHLC values, `high < low`, or a close
   outside `[low, high]`.
4. Confirm that candle `t` ended at least 15 minutes before evaluation.
5. Refuse new risk if the latest completed candle is missing or stale.
6. Preserve a content hash and source metadata for the exact input snapshot.

Late data MAY be repaired and replayed, but a replay for an existing strategy
date MUST reuse the same deterministic decision identity and MUST NOT create a
second economic action.

## 4. Inputs and formulas

Let `C_t` be the BTC/CAD close of completed day `t`.

### 4.1 Momentum

The 90-calendar-day simple return is:

\[
M_t = \frac{C_t}{C_{t-90}} - 1
\]

### 4.2 Trend

The 200-day simple moving average includes `C_t`:

\[
SMA_{200,t} = \frac{1}{200}\sum_{i=0}^{199} C_{t-i}
\]

### 4.3 Realized volatility

Daily log returns are:

\[
r_i = \log\left(\frac{C_i}{C_{i-1}}\right)
\]

The 30-day annualized realized volatility is the sample standard deviation of
the most recent 30 daily log returns, annualized with 365 days:

\[
\sigma_t = \operatorname{stdev}_{sample}(r_{t-29},\ldots,r_t)\sqrt{365}
\]

The implementation MUST use one consistent decimal/float policy across live and
backtest paths. Nonfinite, nonpositive, or otherwise invalid volatility causes a
fail-closed, no-increase decision; the implementation MUST NOT silently insert a
volatility floor.

### 4.4 Eligibility and raw target

BTC is eligible only when both comparisons are strictly true:

\[
eligible_t = (M_t > 0) \land (C_t > SMA_{200,t})
\]

There is no tolerance band in V1. Equality is ineligible.

If eligible, the unconstrained volatility target is:

\[
w^{vol}_t = \frac{0.25}{\sigma_t}
\]

If ineligible, the target weight is zero.

## 5. Account-aware target

Let:

- `E_t` be reconciled total account equity in CAD after estimated liquidation
  fees, using a fresh executable reference price;
- `B_t` be current BTC exposure in CAD;
- `R = C$200` be the required CAD reserve.

For positive equity, the cash-reserve weight ceiling is:

\[
w^{cash}_t = \max\left(0, \frac{E_t-R}{E_t}\right)
\]

Before risk overlays, the strategy target is:

\[
w^{strategy}_t =
\begin{cases}
\min(0.80, w^{cash}_t, w^{vol}_t), & eligible_t \\
0, & otherwise
\end{cases}
\]

The requested BTC notional is `N_t = E_t * w_t`, where `w_t` is the strategy
target after the risk engine applies the controls in `RISK_POLICY.md`. The risk
engine may only maintain or reduce this target; it may never increase it.

If `E_t <= C$200`, the target is cash and the strategy is disarmed pending human
review.

## 6. Rebalance decision

The normal rebalance threshold is:

\[
T_t = \max(C\$50, 0.05E_t)
\]

Let `delta_t = N_t - B_t` after accounting for confirmed open bot orders and
expected fees.

- If `delta_t >= T_t`, request a buy subject to all risk and execution gates.
- If `delta_t <= -T_t`, request a sell subject to all risk and execution gates.
- Otherwise record `NO_REBALANCE` and submit no order.

A risk-off regime transition or circuit-breaker liquidation is risk reduction
and is not prevented by the normal threshold. It MUST sell the economically
tradable quantity. Exchange-minimum residual BTC is recorded as dust rather than
causing repeated invalid orders.

The resulting quantity MUST be rounded down to Kraken's current quantity
increment. A rounded order below Kraken's quantity or cost minimum becomes
`BELOW_EXCHANGE_MINIMUM`, not an API submission.

## 7. Decision precedence and reasons

The daily result is exactly one of:

- `TARGET_CASH_AND_DISARM`
- `RISK_REDUCTION`
- `BUY`
- `SELL`
- `NO_REBALANCE`
- `NO_ACTION_DISARMED`
- `NO_ACTION_RISK_GATE`
- `NO_ACTION_DATA_INVALID`
- `NO_ACTION_ACCOUNT_UNCERTAIN`
- `BELOW_EXCHANGE_MINIMUM`

Precedence is:

1. The 20% high-water drawdown response.
2. Existing disarmed state or an account/reconciliation invariant failure.
3. Mandatory risk reduction.
4. The 8% rolling-loss exposure-increase block.
5. Strategy target and normal rebalance threshold.

Every no-action result MUST be persisted and alerted with structured reason
codes. Silence is not a valid daily outcome.

## 8. Execution intent

The strategy produces a target and an economic intent; it does not directly
call Kraken. The execution adapter MUST honor these V1 constraints:

- reconcile balances, bot-owned open orders, and recent executions first;
- fetch current instrument rules and authenticated fees;
- require a fresh order book and block new entries when the spread exceeds
  20 basis points;
- use post-only limit execution first;
- allow no more than three controlled price attempts for one intent;
- abandon an unfilled entry until the next scheduled decision;
- permit a required risk exit to use a marketable immediate-or-cancel limit
  with a maximum 50-basis-point price collar;
- attach a deterministic client order identifier; and
- reconcile an unknown submission before any retry.

An unbounded market order is prohibited. A broker error may leave the account
temporarily above target; it must never cause compensating leverage or an
unverified duplicate.

## 9. Determinism and versioning

The decision identity MUST derive from at least account, strategy identifier,
strategy date, configuration hash, and input-data hash. Re-evaluation of the
same identity MUST return the stored outcome or prove why a recovery transition
is required.

Each record MUST preserve:

- calculation timestamp and strategy date;
- source candle range and content hash;
- all three indicator values;
- reconciled equity and exposure;
- pre-risk and post-risk targets;
- fee and instrument-rule observations;
- decision/reason code;
- linked order intent, exchange order IDs, fills, and fees; and
- code version and configuration hash.

Changing a window, comparator, annualization basis, rebalance threshold, target
volatility, or exposure cap creates a new strategy version and requires the full
research and rollout process.

## 10. Explicit exclusions

V1 does not use intraday bars, order-book prediction, news, sentiment, on-chain
features, discretionary overrides, parameter optimization, stop-loss orders,
take-profit orders, or profit targets. Blockchair features belong to a separate
shadow research challenger and cannot alter `btc_cad_daily_momentum_v1`.
