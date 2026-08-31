# Architecture

## 1. Design goals

Kraken Knight is designed around five invariants:

1. **One causal decision path.** Backtest, paper, shadow, and live modes use the
   same signal, sizing, and risk code.
2. **One account writer.** At most one process can create or modify Kraken
   orders, and the old bot cannot run concurrently.
3. **Reconcile before action.** Local state is never assumed to be exchange
   truth after a timeout, restart, or deployment.
4. **Append facts; derive views.** Signals, intents, attempts, executions, fees,
   balances, and incidents are immutable facts from which current state is
   derived.
5. **Research cannot bypass production policy.** Blockchair and challenger-model
   failures have no path to V1 live targets or orders.

The system is intentionally small enough to audit on a 1 GB droplet. It uses a
typed Python application, SQLite in write-ahead-log mode for transactional local
state, Parquet/JSON artifacts for immutable research inputs, and systemd for
process supervision and scheduling. Blockchair research streams block partitions
instead of loading history into memory; transaction-scale historical dumps are
outside this host boundary. A container orchestrator is not required.

## 2. Context and trust boundaries

```text
 Kraken public APIs ── market/instrument data ─┐
 Kraken private APIs ─ account/fee/order data ─┼─> Kraken adapter
                                               │         │
                                               │         v
 UTC scheduler ─> orchestrator ─> decision engine ─> risk engine
                       │                │              │
                       │                └──────────────┤
                       │                               v
                       ├────────> ledger <──── execution state machine
                       │             │                 │
                       │             ├──── alerts      └──> Kraken orders
                       │             └──── audit artifacts
                       │
 Blockchair API/dumps ─┴─> research store ─> challenger (shadow only)
```

External responses are untrusted inputs. Kraken is authoritative for exchange
and account state, but responses still require schema, timestamp, and invariant
validation. Blockchair is authoritative only for its raw observed payload; the
application assigns causal availability and never treats a derived label as an
exchange fact.

### Checkpoint 2 boundary

The implemented exchange boundary is authenticated but read-only. A closed
Kraken endpoint allowlist supplies server time, BTC/CAD instrument rules, the
authenticated fee tier, balances and holds, open and closed orders, trades,
ledger entries, and sanitized API-key metadata. The adapter owns request
signing, serialized monotonic nonces, response-size/time bounds, strict parsing,
error classification, and a conservative per-workflow request-cost budget.
History collection is deliberately single-page rather than paginated: it reads
one account-lifetime page and one fenced-tail page per endpoint, capped at 50
`ClosedOrders`, 50 `Ledgers`, and 100 `TradesHistory` records per page. A reported
count beyond any page capacity fails completeness, so the workflow cannot call a
larger account history `CLEAN`.

The reconciliation core is exchange-independent. It canonicalizes observed
facts and legacy hints, applies deterministic completeness and safety gates, and
emits `CLEAN`, `UNRESOLVED`, or `DISARMED` with a source hash and
`exchange_writes: false`. A `CLEAN` cutover requires a wallet ID discovered and
pinned through `KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID`, five unique Kraken
order IDs whose reviewed digest equals
`KRAKEN_KNIGHT_EXPECTED_LEGACY_MANIFEST_HASH`, an exact reviewed non-trade
ledger digest equal to `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH`, and the
operator's `KRAKEN_KNIGHT_CUTOVER_QUIESCED` attestation. The first
funding-manifest run is intentionally `UNRESOLVED`: it persists the observed
digest for entry-by-entry review and a pinned rerun. Its persistence boundary
accepts only sanitized, content-addressed reports. The API key returned in
Kraken key metadata, private signatures, nonces, and raw credential-bearing
responses are never persisted.

No Checkpoint 2 object can submit, edit, or cancel an order. `live` mode remains
rejected at configuration validation, and no broker write port is wired into the
CLI. The order path in the context diagram is the reviewed target architecture,
not current capability.

## 3. Package boundaries

The implementation should preserve these responsibilities even if exact module
names evolve:

| Area | Responsibility | Must not do |
| --- | --- | --- |
| `config` | Typed environment/file configuration, mode, immutable config hash | Read secrets into logs or choose strategy parameters dynamically |
| `domain` | Money, quantity, candle, signal, target, intent, order, fill, and reason types | Perform network or database I/O |
| `kraken_read` / `market` | Allowlisted account reads, OHLC history, instrument rules, server time, validation | Accept an arbitrary private method or write to Kraken |
| `reconciliation` | Normalize exchange facts, match legacy hints, classify safety | Treat a hint or missing page as exchange truth |
| `data.blockchair` | Raw archival, metadata, confirmation and availability rules | Feed V1 live decisions |
| `strategy` | Frozen indicators and raw target calculation | Submit orders or relax risk gates |
| `risk` | Capital constraints, circuit breakers, target reduction, armed state | Increase a strategy target |
| `execution` | Intent planning, bounded order attempts, reconciliation | Invent missing fills or blindly retry unknown submissions |
| `storage` | Atomic ledger, projections, locks, snapshots, migrations | Mutate an immutable historical fact in place |
| `backtest` | Causal event replay, simulated fills/costs, metrics | Maintain a separate strategy implementation |
| `research` | Blockchair features, challenger, ablation, statistics | Promote itself to live automatically |
| `alerts` | Redacted human-readable and structured notifications | Include secrets or claim success before reconciliation |
| `jobs` / `cli` | Orchestration and operator entry points | Contain trading formulas |

Dependency direction should point from orchestration and adapters toward domain
interfaces. Domain, strategy, and risk code should have no dependency on Kraken
SDK objects, systemd, Telegram, or SQLite.

## 4. Runtime processes

### 4.1 Daily decision job

The primary timer fires at 00:15 UTC. One invocation performs a finite workflow
and exits; it is not an always-running strategy loop.

1. Acquire the global writer lease for a live mode, or a read-only job lease for
   shadow mode.
2. Load configuration and persist its redacted hash.
3. Check armed state, server time, and the last completed strategy date.
4. Reconcile balances, open orders, recent executions, and unresolved intents.
5. Fetch and validate the completed daily candle dataset.
6. Calculate indicators and the price-only target.
7. Mark equity, update risk observations, and apply risk precedence.
8. Persist the complete decision before exchange submission.
9. In write-enabled mode, execute the bounded intent state machine.
10. Reconcile the final exchange state and compute ledger projections.
11. Emit the daily heartbeat and release the lease.

An exception after step 8 leaves a recoverable persisted intent, not an excuse
to recompute and submit a new one.

### 4.2 Reconciliation/health job

A lightweight periodic job observes live balances, orders, executions, account
ledger history, fee tier, key permissions, clock skew, and database health. At
Checkpoint 2 it can only classify and persist a reconciliation; `DISARMED` is a
report outcome, not an exchange action. It cannot cancel, submit, edit, create a
strategy target, or arm trading. Its conservative 30-minute timer template is
provided for later operator installation but is not enabled or deployed by this
checkpoint.

`CUTOVER_QUIESCED` is not process control. The operator may assert it only after
the legacy writer and all restart paths are disabled and manual trading is
stopped for the supervised run. Systemd does not set it automatically, and the
inactive timer must not turn a maintenance-window attestation into a standing
claim.

### 4.3 Research collection and build jobs

Blockchair ingestion runs with separate credentials, tables, and failure state.
It archives raw observations before feature derivation. Research builds are
offline, resource-bounded processes and must not contend with the production
writer or cause the daily job to miss its window. Historical ingestion streams
daily Bitcoin block-dump partitions one at a time. It must not perform a full
transaction, input, output, or address dump backfill on the 1 GB host, and it
must not substitute beta server-side aggregation as the canonical dataset.
Bounded API calls are reserved for compatibility, tip/finality, reorg, and
contemporaneous snapshot checks under the research request-point budget. There
is no unauthenticated Blockchair fallback.

## 5. Ports and adapters

Core workflows depend on interfaces rather than mode checks distributed through
the codebase:

- `MarketDataPort`: completed candles, fresh book, and reference price.
- `InstrumentPort`: pair status, increments, minimums, and fee tier.
- `AccountPort`: balances, orders, executions, server time, and dead-man switch.
- `BrokerPort`: validate, submit, cancel, and query by deterministic client ID.
- `LedgerPort`: atomic facts, projections, leases, and armed state.
- `ClockPort`: explicit time for deterministic tests and replay.
- `AlertPort`: redacted delivery with persisted delivery outcome.

Live uses Kraken adapters; paper/backtest use deterministic simulators. Shadow
uses live read adapters and a broker that records what would have been submitted.
The decision and risk engines do not branch on the active adapter.

## 6. Persistence model

SQLite is the source of truth for operational state on one host. Write-ahead-log
mode, foreign keys, explicit transactions, integrity checks, and a busy timeout
are required. Only one process may hold the live writer lease.

The logical records include:

- `source_snapshots`: source, observation time, covered interval, raw hash,
  schema/contract version and hash, storage URI, request cost, quota state, and
  validation outcome;
- `reconciliation_snapshots`: immutable observation identity, account-binding
  and source hashes, status, sanitized canonical report, and an enforced
  zero-exchange-write flag;
- `decisions`: deterministic ID, strategy date/version, config/code/data hashes,
  features, equity, pre/post-risk targets, reason, and mode;
- `risk_events`: equity observations, cash flows, high-water state, gates,
  armed/disarmed transitions, actor, and reason;
- `intents`: side, desired notional/quantity, execution constraints, lifecycle,
  and linked decision;
- `order_attempts`: deterministic client ID, exchange ID, request hash,
  submission state, timestamps, price, quantity, and error classification;
- `executions`: immutable Kraken trade/fill ID, quantity, price, fee, currency,
  and observation source;
- `balance_observations`: immutable exchange balances and projection difference;
- `alerts`: event, redacted payload hash, channel, attempts, and delivery result;
- `cash_flows`: explicit deposits/withdrawals excluded from strategy P&L; and
- `leases` / `schema_migrations`: concurrency and database-version evidence.

Checkpoint 2 uses schema version 3. Initialization performs a verified v2-to-v3
migration before accepting reconciliation snapshots; unsupported or structurally
incompatible databases fail closed. A duplicate account/observation instant is
accepted only when its deterministic content is identical.

Exchange IDs and trade IDs require unique constraints. A decision has at most
one economic intent. A client order ID is unique across all attempts. Retried
observations use upsert-on-identical-content semantics; conflicting content is an
incident, not an overwrite.

Raw Kraken and Blockchair payloads should be content-addressed and compressed
outside SQLite, with their hashes and redacted metadata in the ledger. Raw files
that may contain secret-bearing URLs must be sanitized before durable storage.

## 7. Execution state machine

An economic intent moves monotonically through explicit states:

```text
PLANNED -> VALIDATED -> SUBMITTING -> OPEN -> PARTIALLY_FILLED -> FILLED
                           |           |             |
                           v           v             v
                        UNKNOWN    CANCEL_PENDING  CANCEL_PENDING
                           |           |             |
                           +------> RECONCILING <----+
                                           |
                              FILLED / CANCELED / FAILED
```

`UNKNOWN` is a first-class state. A network timeout after submission MUST query
Kraken by client ID and reconcile open/closed orders plus executions. It MUST NOT
create a new attempt until absence is established under a tested recovery rule.

Partial fills update remaining desired quantity from confirmed fills, current
balances, current rules, and fees. They never cause a new full-size order.
Terminal local state is accepted only after a final account reconciliation.

## 8. Idempotency and concurrency

The daily decision key is derived from account identity, strategy identifier,
strategy date, configuration hash, and input snapshot hash. The business key
for action is account + strategy identifier + strategy date; if a later input
hash conflicts for the same date, the system records a data revision incident
and requires review rather than trading twice.

Idempotency has three layers:

1. A database uniqueness constraint on the daily business key and intent.
2. A host-level/systemd exclusion plus transactional database writer lease.
3. A deterministic Kraken client order ID and exchange reconciliation.

No nonce sequence or private API key is shared with the old bot or another
process. Startup refuses live mode when it cannot prove the lease, armed state,
and prior-intent reconciliation.

## 9. Backtest/live parity

The backtester advances an explicit clock one event at a time. At each decision
time, it exposes only data whose availability timestamp is not in the future.
The simulator implements the same instrument rounding, reserve, risk gates,
rebalance threshold, and order-intent types as live.

Simulation-specific fill and fee models enter through the broker adapter. They
must be visible in the result configuration and may not be embedded in strategy
formulas. Golden replay tests compare a fixed live-like input bundle with paper,
shadow, and backtest decision records field by field.

## 10. Observability

Logs are structured JSON in UTC with event name, correlation/decision/intent ID,
mode, strategy date, code/config version, and redacted error classification.
Secrets, private request signatures, nonces, and Blockchair-key URLs are omitted.
The same sanitization applies before exception rendering, manifests, filenames,
durable raw metadata, HTTP-client diagnostics, and human alerts.

Required health signals include:

- last successful daily decision and heartbeat;
- last completed reconciliation;
- current mode and armed state;
- writer lease owner and age;
- unresolved/unknown intents;
- exchange-versus-ledger balance difference;
- source freshness and schema status;
- current equity, high-water drawdown, and active risk gates;
- alert delivery state; and
- disk, database integrity, clock sync, memory, and service restart counts.

Human alerts contain enough context to act but link to locally protected detail
rather than embedding raw responses.

## 11. Security and failure boundaries

The service runs as an unprivileged user with a read-only code release, a
dedicated writable state directory, restrictive systemd protections, and
credentials provided outside the repository. Backups are encrypted or kept on a
restricted host and are restore-tested.

The Checkpoint 2 Kraken key is newly created for this service, IP-allowlisted,
and has exactly `Query Funds`, `Query Open Orders & Trades`, `Query Closed Orders
& Trades`, and `Query Ledger Entries`. It never reuses the legacy key and has no
trade, cancel, funding, withdrawal, transfer, staking, or Earn permission.

Network/API response parsing uses explicit schemas, bounds, timeouts, and
rate-limit awareness. Checkpoint 2 performs no automatic Kraken retry: a failed
read consumes its nonce and the workflow fails closed. A malformed response
cannot default to a buy. Kraken private calls are serialized to preserve nonce
ordering. Blockchair request points are recorded locally, requests are
serialized within the research budget, and quota or compatibility failure stops
research collection without affecting V1. The client never retries without its
configured key or logs the secret-bearing request URL.

Deployment, data migration, and rearm are privileged operator actions. The
trading service cannot modify its code, configuration, risk epoch, or secret
permissions.

## 12. Architectural acceptance tests

Before live canary, automated or recorded tests must prove:

- identical inputs produce an identical decision and data/config hash;
- a repeated job creates no duplicate intent or order;
- the authenticated adapter exposes only the reviewed read endpoint set and its
  request objects are always public GETs or signed read-only POSTs;
- broad key permissions, a missing IP allowlist, history exceeding a one-page
  endpoint bound, malformed Kraken data, or unresolved legacy claims cannot
  produce `CLEAN`;
- reconciliation snapshots are deterministic, immutable, secret-free, and
  enforce `exchange_writes: false` at application and schema boundaries;
- termination at every execution-state transition recovers correctly;
- a submission timeout is reconciled without blind retry;
- partial fill plus restart produces only the correct remainder;
- disarmed and drawdown state survives process and host restart;
- stale/incomplete candles, stale books, unknown balances, and fee/rule failures
  cannot increase exposure;
- simulated fees, cash reserve, rounding, and P&L match hand calculations;
- Blockchair failure cannot affect the V1 target;
- Blockchair six-successor depth, incomplete-day rejection, actual observation
  availability, schema quarantine, request-point exhaustion, and refusal of a
  keyless fallback match the research protocol; and
- secret-shaped values never appear in logs, exception text, HTTP diagnostics,
  manifests, filenames, durable raw metadata, or alerts.
