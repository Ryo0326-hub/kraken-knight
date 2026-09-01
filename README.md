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

## System boundaries

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

## Checkpoint 2: authenticated read-only reconciliation

The current implementation adds an authenticated Kraken read path to the
Checkpoint 1 research core. It reads a closed allowlist of instrument, fee,
balance, order, trade, ledger, and API-key metadata endpoints; validates and
redacts their responses; and produces a deterministic account reconciliation.
Private requests use Kraken signing, serialized monotonic nonces, bounded
responses, and a conservative per-run request-cost budget. There is no arbitrary
private-method escape hatch.

Reconciliation classifies an observation as `CLEAN`, `UNRESOLVED`, or
`DISARMED`, records `exchange_writes: false`, and can append a content-addressed
snapshot to the immutable SQLite schema v3 ledger. Legacy hints are claims to
match against Kraken history, never a substitute for exchange facts. A `CLEAN`
cutover requires exactly five reviewed hints with five unique Kraken order IDs
and an operator-pinned manifest digest.

With Python 3.12 and `uv` installed, use a non-editable install so the same
packaged console entry point is exercised locally and in release verification:

```bash
uv sync --locked --extra dev --no-editable --reinstall-package kraken-knight
.venv/bin/pytest
.venv/bin/kraken-knight init --json
.venv/bin/kraken-knight status --json
.venv/bin/python scripts/run_public_smoke.py
```

The CLI defaults to shadow mode and reports `exchange_writes: false`. After the
operator creates and installs a new read-only Kraken key, bootstrap and perform
the authenticated checkpoint in this order:

```bash
.venv/bin/kraken-knight account-id --json
.venv/bin/kraken-knight legacy-manifest --json \
  --legacy-hints /restricted/path/hints.json
# Only after pinning both outputs and completing the quiescence steps below:
.venv/bin/kraken-knight reconcile --json \
  --legacy-hints /restricted/path/hints.json
```

The key must be unique to Kraken Knight, restricted to the production host IP,
and have exactly `Query Funds`, `Query Open Orders & Trades`, `Query Closed
Orders & Trades`, and `Query Ledger Entries`. Do not enable trading, cancellation,
funding, withdrawal, transfer, staking, or Earn permissions, and never copy or
reuse the legacy bot key. Credentials remain outside Git and command arguments.
The protected environment must set `KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME` and
`KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP`. Review the `account-id` output and pin its
`wallet_account_id` as `KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID`. Review the
five-hint file, run `legacy-manifest`, and pin its `legacy_manifest_hash` as
`KRAKEN_KNIGHT_EXPECTED_LEGACY_MANIFEST_HASH`. Configure
`KRAKEN_KNIGHT_LEGACY_HINTS_PATH` or supply the same restricted file explicitly.
The workflow compares these bindings without echoing protected values in status.

Before the first supervised `reconcile`, preserve the legacy evidence, stop and
disable the legacy writer and every process, timer, cron job, supervisor, or
container that could restart it, and stop manual Kraken trading. Only then set
`KRAKEN_KNIGHT_CUTOVER_QUIESCED=true`; it is an operator attestation, not a
process-control mechanism. Set it back to `false` whenever those conditions no
longer hold. The supplied reconciliation timer remains disabled.

Leave `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH` blank for the first
supervised reconciliation. Exit code 3 and `UNRESOLVED` are expected. Review the
persisted `evidence.account_lifetime_ledgers.entries`,
`evidence.tail_ledgers.entries`, and `evidence.funding_manifest_hash`; pin that
hash only if every non-trade entry is a recognized positive inbound CAD deposit
and the collection was quiet. Then rerun with
`KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH` set. Any unknown asset, direction,
or ledger type remains a hard stop; the hash does not turn an unexplained entry
into a deposit.

This checkpoint deliberately does not paginate history. It reads one
account-lifetime page and one fenced-tail page from each endpoint, with a maximum
of 50 `ClosedOrders`, 50 `Ledgers`, and 100 `TradesHistory` records per page. If
Kraken reports more records than a page can hold, completeness fails and the run
cannot be `CLEAN`. This one-page ceiling is an explicit Checkpoint 2 limitation,
not a claim that all account history was fetched.

Checkpoint 2 still rejects live mode even when credential and environment-arm
fields are populated. The adapter has no submit, edit, or cancel method, the
provided reconciliation timer is not enabled, and neither the droplet nor the
legacy service has been changed by this repository checkpoint. A real account
reconciliation remains pending until the operator creates the new key and
performs the supervised procedure. Deployment, durable arming, execution,
Telegram alerts, and Blockchair ingestion remain later gated checkpoints.

The public smoke command prints an explicitly labeled engineering report; its
short rolling window is not the research-grade profitability backtest. A clean
reconciliation proves account-state observability, not positive expected P&L.

Because this checkpoint has daily OHLC rather than post-decision intraday trades,
its replay waits until the following UTC day's open. That is a conservative
timing/sensitivity approximation. It also assumes complete fills and does not
yet model Kraken increments, minimums, maker queue position, or dust, so its P&L
is not release evidence. The full research protocol requires observable
post-00:15 minute/trade data and explicit fill scenarios.

Blockchair research credentials belong only in the separately loaded
`.env.research.example` surface; the trading-service example does not include
them.

## Checkpoint 3: historical causal backtest

The current research checkpoint imports Kraken's official BTC/CAD trade archive,
reconstructs daily candles plus observable post-decision execution VWAPs, and
runs the frozen strategy from C$1,000. It includes Kraken fees, adverse
slippage, order increments and minimums, and a 10% execution-minute volume cap.
The output is an auditable bundle of metrics, decisions, fills, equity,
drawdown, and pre-holdout robustness charts.

The BTC buy-and-hold comparison is causal too: it accumulates across successive
available execution minutes under the same 10% cap, and logs every attempt,
instead of assuming the full C$1,000 filled instantly in one minute.

The final holdout run is bound to a clean Git commit, the frozen study JSON, and
hashed source data. Neighboring parameters and non-primary cost cases cannot
inspect the holdout. This makes the result reproducible; it does not make it a
promise of future profit or authorize live trading. See
[`docs/HISTORICAL_BACKTEST.md`](docs/HISTORICAL_BACKTEST.md) for the plain-English
method, exact commands, artifact guide, and suggested blog-post structure.

The first sealed result grew C$1,000 to C$1,225.27, then triggered the 20%
drawdown disarm in May 2020 and stayed in cash. Its holdout return was therefore
0% with no market exposure: an engineering-valid result, but not evidence of
out-of-sample profitability. See
[`docs/BACKTEST_RESULTS_2026-01-01.md`](docs/BACKTEST_RESULTS_2026-01-01.md) for
the graphs, metrics, and interpretation.

The follow-up V2 study keeps that result immutable and separately tests two
post-holdout counterfactuals: the signal without the drawdown gate, and the same
20% liquidation followed by a fixed 90-day cooldown plus causal trend rearm.
All data, timing, costs, and volume assumptions remain unchanged. See
[`docs/DRAWDOWN_COUNTERFACTUAL.md`](docs/DRAWDOWN_COUNTERFACTUAL.md) for the
frozen rules and reproducible run contract. These exploratory variants do not
alter the manual-rearm production policy.

## Documentation map

- [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md) — frozen signal, sizing, and
  decision semantics.
- [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md) — capital boundary, circuit
  breakers, security, and precedence.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data flow,
  persistence, idempotency, and observability.
- [`docs/RESEARCH_PROTOCOL.md`](docs/RESEARCH_PROTOCOL.md) — causal backtest and
  Blockchair challenger experiment.
- [`docs/HISTORICAL_BACKTEST.md`](docs/HISTORICAL_BACKTEST.md) — reproducible
  Kraken data download, frozen study run, outputs, and interpretation.
- [`docs/BACKTEST_RESULTS_2026-01-01.md`](docs/BACKTEST_RESULTS_2026-01-01.md) —
  sealed V1 result, visuals, limitations, and next research question.
- [`docs/DRAWDOWN_COUNTERFACTUAL.md`](docs/DRAWDOWN_COUNTERFACTUAL.md) — frozen
  post-holdout V2 variants, causal rearm rule, and audit contract.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — production topology, staged rollout,
  cutover, and rollback.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — routine operation and incident response.

## Repository and secret hygiene

No API key, private key, Telegram token, chat identifier, raw secret-bearing
response, production database, or `.env` file belongs in Git. The Checkpoint 2
Kraken key must be IP-allowlisted and limited to the four read permissions listed
above. The old and new bots must never share an API key or concurrently write to
the account.

Until the later checkpoints pass, this repository is not a deployable or live
trading system.

## License

Kraken Knight is released under the [MIT License](LICENSE).
