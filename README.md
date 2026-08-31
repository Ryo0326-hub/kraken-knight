# Kraken Knight

Kraken Knight is a small, deterministic BTC/CAD trading system for a dedicated
Kraken Spot account. Its first objective is reliable autonomous operation: every
decision must be reproducible, every order must be attributable, and uncertain
exchange state must stop new risk. Profit is an experimental outcome, not a
software acceptance criterion.

The initial account sleeve is C$1,000. The production strategy is deliberately
simple: daily medium-term momentum and trend determine whether BTC is eligible,
and realized volatility controls position size. There is no leverage, shorting,
martingale sizing, intraday prediction, or AI-generated trading override.

> **Experimental software:** this project can lose money, including more than a
> configured drawdown threshold during gaps, outages, or failed execution. It is
> not investment advice and makes no claim of future profitability.

## Frozen V1 policy

At 00:15 UTC, using only the most recently completed UTC daily candle:

1. BTC is eligible when its 90-day return is positive **and** its close is above
   its 200-day simple moving average.
2. Eligible BTC exposure targets `25% / 30-day annualized realized volatility`,
   capped at 80% of account equity and constrained by a C$200 cash reserve.
3. Ineligible BTC exposure targets zero. Risk-reducing actions take precedence
   over normal rebalance thresholds.
4. A normal rebalance requires a target change of at least the greater of C$50
   or 5% of current equity.
5. An 8% rolling-24-hour equity loss blocks exposure increases for 24 hours. A
   20% high-water-mark drawdown targets cash, disarms trading, and requires a
   manual review and rearm.

The exact formulas, time conventions, and decision rules are normative in
[`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md). Risk precedence and failure
behavior are normative in [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md).

## Engineering success versus trading success

These are measured separately.

Engineering success means the system:

- rejects incomplete, stale, or inconsistent data;
- produces the same result when a decision is replayed;
- reconciles account state before taking risk;
- never blindly retries an order with an unknown outcome;
- survives restarts without duplicate orders;
- records signals, intents, exchange identifiers, fills, fees, balances, and P&L;
- sends a daily heartbeat even when it takes no action; and
- fails closed when account or exchange state is uncertain.

Trading success is an out-of-sample result after fees and slippage. A flat or
negative backtest does not become positive because the software works. Conversely,
a negative but correctly designed experiment can still be valuable research.
Deployment is permitted with a negative backtest only at the controlled research
capital limits and only when no catastrophic engineering or risk rejection gate
is present.

## Delivery stages

The release ladder is intentionally asymmetric: increasing capital requires
evidence, while disarming never does.

| Stage | Exchange writes | Maximum strategy BTC notional | Purpose |
| --- | --- | ---: | --- |
| Backtest/paper | None | C$0 | Verify causal decisions, accounting, and failure cases |
| Kraken validation | Validated but not submitted | C$0 | Verify request construction against production rules |
| Shadow | None | C$0 | Compare intended decisions with the live account and market |
| Operational probe | Yes, explicitly tagged | C$25 round trip | Verify the real order, fill, fee, alert, and ledger path |
| Canary | Yes | C$250 | Observe at least three clean daily cycles and a restart |
| Controlled sleeve | Yes | Up to C$800 | Operate the frozen strategy within the C$1,000 sleeve |

The operational probe is not a strategy signal and is excluded from performance
statistics. No profitability threshold unlocks a stage; the release gates are
correctness, reconciliation, observability, and risk behavior.

## Research design

The price-only V1 is the production baseline. Blockchair is an optional,
lagged on-chain data source used to test whether on-chain variables add genuine
out-of-sample information. It cannot block or change V1 trading.

The study compares price-only, on-chain-only, and combined models with
content-addressed raw snapshots, time-causal feature availability, a frozen
confirmatory holdout, a separately labeled prequential replay, cost stress,
feature ablation, and paired block-bootstrap uncertainty. A finding of no
incremental value is a valid result. See
[`docs/RESEARCH_PROTOCOL.md`](docs/RESEARCH_PROTOCOL.md).

## Planned system boundaries

- Kraken is authoritative for instruments, market data, fees, balances, orders,
  executions, and account history.
- Blockchair supplies research features only.
- A single decision engine and risk engine are shared by backtest, paper, shadow,
  and live modes; only the broker adapter changes.
- An append-only decision ledger and immutable raw-data snapshots make every run
  reproducible.
- The production host uses systemd and one write-capable process. Secrets and
  mutable state remain outside the repository.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for components and invariants,
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for release and old-bot cutover gates,
and [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for operator procedures.

## Checkpoint 1: local-only core

The current implementation cannot access authenticated Kraken endpoints or
submit an order. It includes a read-only public Kraken OHLC adapter that
quarantines the mutable final candle, the frozen strategy calculation, causal
market-data validation, an initial event backtester, provenance hashing, a
secret-redacting/fail-closed configuration layer, an append-only SQLite decision
and intent ledger, and unit tests.

With Python 3.12 and `uv` installed, use a non-editable install so the same
packaged console entry point is exercised locally and in release verification:

```bash
uv sync --locked --extra dev --no-editable
.venv/bin/pytest
.venv/bin/kraken-knight init --json
.venv/bin/kraken-knight status --json
.venv/bin/python scripts/run_public_smoke.py
```

The CLI defaults to shadow mode and reports `exchange_writes: false`.
Checkpoint 1 rejects live mode even when credential and environment-arm fields
are populated; a later checkpoint must implement durable release-bound arming,
account reconciliation, and the exchange adapter before that boundary can open.
The public smoke command prints an explicitly labeled engineering report; its
short rolling window is not the research-grade profitability backtest. Kraken
authentication, reconciliation, execution, Telegram alerts, production systemd
installation, and Blockchair ingestion remain later gated checkpoints.

Because this checkpoint has daily OHLC rather than post-decision intraday trades,
its replay waits until the following UTC day's open. That is a conservative
timing/sensitivity approximation. It also assumes complete fills and does not
yet model Kraken increments, minimums, maker queue position, or dust, so its P&L
is not release evidence. The full research protocol requires observable
post-00:15 minute/trade data and explicit fill scenarios.

Blockchair research credentials belong only in the separately loaded
`.env.research.example` surface; the trading-service example does not include
them.

## Documentation map

- [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md) — frozen signal, sizing, and
  decision semantics.
- [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md) — capital boundary, circuit
  breakers, security, and precedence.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data flow,
  persistence, idempotency, and observability.
- [`docs/RESEARCH_PROTOCOL.md`](docs/RESEARCH_PROTOCOL.md) — causal backtest and
  Blockchair challenger experiment.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — production topology, staged rollout,
  cutover, and rollback.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — routine operation and incident response.

## Repository and secret hygiene

No API key, private key, Telegram token, chat identifier, raw secret-bearing
response, production database, or `.env` file belongs in Git. Kraken credentials
must be IP-allowlisted and must not have withdrawal, deposit, transfer, or Earn
permissions. The old and new bots must never share an API key or concurrently
write to the account.

Until the later checkpoints pass, this repository is not a deployable or live
trading system.

## License

Kraken Knight is released under the [MIT License](LICENSE).
