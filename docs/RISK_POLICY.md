# Risk Policy

## 1. Purpose

This policy defines controls that are independent of the strategy's expected
return. The strategy proposes a BTC target; the risk engine may keep or reduce
that target and may disarm the system. It must never enlarge the target.

These controls limit normal operation, but they do not guarantee a maximum loss.
Price gaps, exchange outages, stale markets, order rejection, partial fills, and
operational mistakes can cause realized loss to exceed a configured threshold.

## 2. Capital boundary

The initial experiment is one dedicated C$1,000 Kraken account sleeve.

| Control | V1 value |
| --- | ---: |
| Target annualized volatility | 25% |
| Maximum BTC exposure | 80% of reconciled equity |
| Minimum CAD reserve | C$200 |
| Normal rebalance threshold | Greater of C$50 or 5% of equity |
| Rolling 24-hour loss gate | 8% |
| High-water drawdown gate | 20% |
| Canary strategy cap | C$250 |
| Controlled-sleeve BTC cap | C$800 and all percentage/reserve constraints |

Capital outside this dedicated account is out of scope and MUST NOT be queried,
transferred, pledged, or used for recovery. Deposits and withdrawals are manual
operator actions outside the bot.

## 3. Non-negotiable prohibitions

The bot MUST NOT:

- use margin, leverage, credit, futures, options, perpetuals, or short sales;
- borrow CAD or BTC;
- pyramid to recover a loss, use martingale sizing, or average down outside the
  deterministic target calculation;
- transfer, deposit, withdraw, stake, or interact with Kraken Earn;
- let an LLM, ML model, remote prompt, or alert response alter a live target;
- submit an unbounded market order;
- treat an API timeout as proof that an order failed;
- operate while another bot can write to the same account; or
- increase exposure while data, account state, instrument rules, fee tier, or
  order state is uncertain.

## 4. Equity, P&L, and price definitions

All risk metrics use CAD as the reporting currency.

`equity` is reconciled available CAD plus held CAD plus BTC valued at a fresh,
executable conservative price, less estimated liquidation fees and any known
liability. Although liabilities should be zero in a spot-only account, any
nonzero or unrecognized liability is an invariant failure and disarms trading.

The risk price MUST come from a fresh BTC/CAD book. It SHOULD use the executable
bid for BTC liquidation rather than a last-trade mark. A missing or crossed book
does not produce an optimistic substitute; it blocks new exposure.

P&L records MUST distinguish:

- external cash flows;
- strategy trading P&L;
- operational-probe P&L;
- Kraken fees;
- spread/slippage; and
- unrealized mark changes.

External deposits or withdrawals reset comparison baselines only through an
explicitly recorded cash-flow event. They MUST NOT appear as strategy profit or
loss.

## 5. Rolling 24-hour loss gate

At every reconciliation, compare current equity `E_now` with cash-flow-adjusted
equity at or immediately before `now - 24 hours`, `E_24h`:

\[
L_{24h} = \frac{E_{now}}{E_{24h}} - 1
\]

When `L_24h <= -0.08`:

1. Persist a `ROLLING_LOSS_GATE` event with both equity observations.
2. Cancel bot-owned exposure-increasing orders.
3. Block every new exposure increase until at least 24 hours after the trigger.
4. Continue reconciliation, monitoring, and risk-reducing trades.
5. Alert the operator immediately and in each daily heartbeat while active.

The gate does not force liquidation by itself. Repeated triggers extend the
blocked-until timestamp from the newest trigger. Expiry removes only this gate;
it does not rearm a drawdown-disarmed system or override another failure.

If a causal 24-hour comparison cannot be constructed, exposure increases are
blocked until it can be constructed or the operator resolves the state.

## 6. High-water drawdown gate

The high-water mark `H` is the greatest cash-flow-adjusted, fee-aware reconciled
equity observed since the current risk epoch began. Drawdown is:

\[
D = 1 - \frac{E_{now}}{H}
\]

When `D >= 0.20`, the system MUST:

1. atomically persist `DRAWDOWN_DISARMED` before sending new orders;
2. cancel all verified bot-owned open orders;
3. target cash using the bounded risk-exit policy;
4. preserve and repeatedly reconcile any partial fill or exchange-minimum dust;
5. alert the operator with high-water mark, current equity, positions, open
   orders, and attempted actions; and
6. remain disarmed until a manual review and explicit rearm.

The drawdown trigger bypasses the normal rebalance threshold but not exchange
minimums or the bounded execution collar. If an exit cannot be completed safely,
the system remains disarmed and escalates; it does not send success messaging.

A process restart MUST preserve `DRAWDOWN_DISARMED`. The high-water mark MUST NOT
be reset automatically by a restart, deployment, daily boundary, deposit, or
rearm. A new risk epoch and baseline require an operator-signed reason after the
incident review has been saved.

## 7. Data and market gates

An exposure increase is prohibited when any of the following is true:

- the latest completed UTC daily candle cannot be uniquely established;
- the candle history is missing, duplicated, invalid, or stale;
- the live order book is stale, crossed, empty, or unavailable;
- entry spread exceeds 20 basis points;
- Kraken instrument constraints cannot be refreshed;
- the authenticated fee tier cannot be refreshed or safely bounded;
- server/client clock skew exceeds the implementation's tested tolerance;
- a Kraken maintenance or rate-limit state prevents reconciliation; or
- a source response fails its schema or integrity checks.

Historical cached values MAY support a no-action explanation. They MUST NOT be
used to justify new live risk after a freshness gate fails.

Blockchair availability has no authority over V1 production. Its outage pauses
the research challenger and creates an alert; it does not create a price-strategy
signal or prevent a required Kraken risk reduction.

## 8. Account and execution gates

Before each economic intent, the system MUST reconcile:

- CAD and BTC balances, including held amounts;
- bot-owned open orders;
- recent closed/canceled orders and executions;
- the last persisted intent and its exchange identifiers; and
- current observed exposure versus ledger-derived exposure.

The following conditions disarm or block writes until reconciled:

- an unknown open order;
- an unexplained balance difference above rounding/fee tolerance;
- a manual order or trade in the dedicated account;
- a submitted intent with unknown exchange outcome;
- multiple processes holding or bypassing the writer lease;
- a nonce or authentication failure suggesting shared credentials; or
- a liability, asset, or pair outside the approved BTC/CAD spot scope.

Unknown submission state is resolved through read-only order/client-ID queries
and execution history. Blind resubmission is prohibited.

## 9. Order limits

One strategy decision is permitted per UTC strategy date. It may create one
economic intent and at most three price attempts under the same intent. Cancel
and replace attempts do not create additional desired notional.

New entries use post-only limits and expire after the bounded attempt window.
They are not chased into the market. Required risk exits begin with a bounded
limit attempt and MAY use a marketable immediate-or-cancel limit no worse than
50 basis points from the fresh reference bid. If liquidity has moved beyond the
collar, the system pauses, refreshes state, and escalates rather than widening
without limit.

All prices and quantities are rounded conservatively to current Kraken rules.
Fees are included in affordability calculations so an order cannot consume the
C$200 reserve through omitted fee estimates.

The account-wide Kraken dead-man switch MAY run only while bot-owned resting
orders exist. Because it can cancel every account order, the dedicated account
must not contain unrelated manual protective orders.

## 10. State, authorization, and manual control

Trading modes are ordered by authority:

1. `BACKTEST`
2. `PAPER`
3. `VALIDATE`
4. `SHADOW`
5. `LIVE_CANARY`
6. `LIVE_CONTROLLED`

A deployment defaults to no exchange writes. Promotion to either live mode
requires an explicit local configuration change, passing release evidence, and a
fresh startup preflight. A mode string alone is insufficient; a separate arming
record is required.

Any process can disarm. Only the human operator can rearm after a drawdown,
credential incident, unexplained account state, concurrent-writer incident, or
integrity failure. Rearm requires a reason, timestamp, reviewed reconciliation
snapshot, code/config version, and acknowledged remaining exposure.

## 11. Credential and infrastructure controls

The Kraken key MUST:

- belong only to Kraken Knight;
- be restricted to the designated production IP where supported;
- allow only the minimum balance/order/history/WebSocket/trading permissions;
- deny withdrawal, deposit, transfer, staking, and Earn capabilities; and
- be revoked, not reused, if its secrecy or single-writer guarantee is uncertain.

Secrets MUST stay outside Git, process arguments, exception bodies, structured
logs, and alert payloads. Blockchair query parameters require explicit redaction
because the API key may appear in the URL. Secret files must be readable only by
the service account and administrators.

## 12. Alert obligations

Immediate operator alerts are required for:

- a live order submission, fill, partial fill, cancellation, or rejection;
- unknown order outcome or account mismatch;
- rolling-loss or drawdown-gate activation;
- live service start, stop, restart, or writer-lease conflict;
- authentication, clock, schema, or persistent data failures; and
- any mode or armed-state change.

The daily heartbeat MUST report strategy date, data freshness, signal inputs,
target/current exposure, risk gates, account equity, high-water drawdown, order
result, fees, and reconciliation status. Telegram delivery failure is itself an
alert through the secondary local log/health path and blocks promotion, though it
does not prevent a necessary risk reduction.

## 13. Release and rejection gates

Profitability is not required for the C$250 controlled experiment. The bot MUST
NOT trade live when testing reveals:

- look-ahead or survivorship leakage;
- nondeterministic decisions from identical inputs;
- duplicate-order behavior;
- unrecoverable unknown order state;
- incorrect balance, fee, or P&L accounting;
- risk-gate failure or restart state loss;
- pathological turnover or fee drag caused by an implementation defect; or
- simulated behavior capable of leverage, borrowing, or spending the reserve.

A poor but valid expected-return result is reported honestly and may proceed only
at the research capital limit. An engineering defect is fixed before any live
capital is exposed.
