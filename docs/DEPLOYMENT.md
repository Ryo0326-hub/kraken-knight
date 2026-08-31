# Deployment and Cutover Plan

## 1. Scope

This document defines the production deployment contract for the designated
Ubuntu droplet and the replacement of the existing ML bot. It is not permission
to deploy from an unreviewed working tree or to send a live trade before the
release gates pass.

Checkpoint 2 stops before deployment. It provides authenticated read-only
reconciliation code and inactive systemd templates, but no credential has been
created or installed, no real Kraken reconciliation has been recorded, and no
host service or legacy process has been changed. The procedures below remain
operator-controlled release gates, not a report that cutover occurred.

Host-specific audit facts, paths, service names, and process evidence belong in
the restricted cutover record and MUST be rechecked at cutover.

The public repository MUST NOT contain the droplet address, SSH details,
credentials, production database, private logs, or operator chat identifiers.

## 2. Production layout

Use a direct Python/systemd deployment rather than adding a container runtime to
the small host.

Recommended filesystem separation:

```text
/opt/kraken-knight/releases/<git-commit>/   immutable application release
/opt/kraken-knight/current                 atomic symlink to active release
/etc/kraken-knight/config.env              root/service-readable secrets, mode
/var/lib/kraken-knight/                    SQLite ledger, locks, snapshots
/var/lib/kraken-knight/artifacts/          redacted run manifests and reports
/var/backups/kraken-knight/                restricted encrypted/checksummed backup
```

Run as a dedicated unprivileged `kraken-knight` service account. Code and
configuration are not writable by that account; only the state/artifact
directories are. Secret files are mode `0600` or an equivalent systemd
credential mechanism. journald receives structured, redacted logs.

Systemd unit templates:

- `kraken-knight.service` — finite daily decision and bounded execution;
- `kraken-knight.timer` — schedules 00:15 UTC with persistence disabled
  for missed live decisions unless replay safety explicitly handles the date;
- `kraken-knight-reconcile.service` / `.timer` — read/reconcile health and
  unresolved orders every 30 minutes with no exchange-write capability; and
- an optional research collection unit with lower priority and no trading
  credential access.

Systemd should set a restrictive umask, read-only system paths, private temporary
storage, no-new-privileges, bounded memory/CPU, restart limits, and explicit
writable paths. The reconciliation process and daily process still obey the
application-level database lease; systemd alone is not the single-writer proof.
The Checkpoint 2 reconcile unit and timer are tracked for review but MUST NOT be
installed, enabled, or started during account bootstrap or the supervised
cutover reconciliation. They remain inactive until the legacy writer is disabled,
manual trading is frozen, the pinned rerun is `CLEAN`, and a later deployment
checkpoint explicitly authorizes scheduling.

Host time synchronization, disk capacity, SQLite integrity, DNS/TLS, memory
headroom, and log rotation are preflight checks. Resource-intensive backtests run
off the production path.

## 3. Secrets and permissions

Configuration is typed and fail-closed. The planned secret/config surface
includes, at minimum:

- Kraken API key and private secret;
- Blockchair API key, available only to the research collector;
- Telegram bot token and destination identifier;
- deployment mode and separate armed-state authority;
- account/sleeve and release-cap settings; and
- database/state paths.

Production values never enter the Git working tree, shell history, command
arguments, unit file, CI logs, or alerts. Checkpoint 2's protected configuration
binds the exact key name and allowlisted host IP with
`KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME` and
`KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP`; the public wallet ID with
`KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID`; the reviewed five-claim digest with
`KRAKEN_KNIGHT_EXPECTED_LEGACY_MANIFEST_HASH`; and the reviewed non-trade ledger
digest with `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH`.
`KRAKEN_KNIGHT_LEGACY_HINTS_PATH` points to the restricted evidence file.
`KRAKEN_KNIGHT_CUTOVER_QUIESCED` is false by default and may be true only after
the old writer, its restart paths, and manual trading are stopped. Status output
exposes only safe configured/attested booleans for these bindings.

The Checkpoint 2 Kraken key is newly created for Kraken Knight, restricted to the
production host IP, and has exactly these permissions:

- `Query Funds`;
- `Query Open Orders & Trades`;
- `Query Closed Orders & Trades`; and
- `Query Ledger Entries`.

Do not enable order creation, order modification, cancellation, funding,
withdrawal, deposit, transfer, staking, Earn, or any other permission. Never copy
or reuse the legacy bot key. Private requests are serialized as needed to
preserve nonce order. Kraken API-key metadata is validated in memory; its API-key
identifier is not stored in a report, ledger row, log, or alert.

Blockchair URLs are sanitized before logging because the API key may be supplied
as a query parameter. The production V1 service should not receive the
Blockchair secret at all.

The tracked examples enforce that separation: `.env.example` is the trading
service surface and `.env.research.example` is collector-only. They are
documentation templates, not production secret stores.

## 4. Build and release artifact

Only an identified Git commit that passed CI may be deployed. A release records:

- commit SHA and assertion that the source tree was clean;
- locked dependency hash and Python version;
- test/CI run identifiers;
- database schema version (Checkpoint 2 is schema v3);
- redacted configuration hash;
- built artifact checksum; and
- operator, deployment time, previous release, and rollback path.

The host installs dependencies into a release-local virtual environment. It does
not run from a mutable clone with local source edits. Activation uses an atomic
`current` symlink change followed by a read-only startup preflight. Database
migrations are forward-tested on a restored copy and backed up before production
application.

No deployment command may force-push, rewrite remote history, discard an
uncommitted production change, or remove the prior release/state backup.

## 5. CI and pre-deployment gates

The release must pass:

- format, lint, type, unit, and secret scans;
- deterministic fixed-fixture strategy and risk calculations;
- causal data validation and incomplete-candle rejection;
- cost, rounding, reserve, and P&L hand-check fixtures;
- idempotent rerun and duplicate-order prevention;
- simulated restart at every execution-state transition;
- timeout/unknown-submission and partial-fill recovery;
- drawdown/disarmed-state persistence across restart;
- Blockchair/V1 separation and schema-contract tests; and
- redaction tests for secrets, signed requests, and query-string keys.

Checkpoint 2 additionally requires fixed-fixture proof of Kraken request
signing, strictly increasing serialized nonces, the closed read-endpoint
allowlist, strict response parsing, single-page completeness/request-cost
ceilings, API-key permission and IP-allowlist gates, deterministic legacy-hint
matching, immutable schema-v3 reconciliation snapshots, and
`exchange_writes: false` through every success and failure path.

The full backtest report must exist before a live operational probe. Profit may
be negative, but the run cannot contain causal leakage, accounting mismatch,
pathological defect-driven turnover, risk violations, or unresolved errors.

The first credentialed bootstrap is manual and supervised. From an immutable
reviewed release, with the protected environment loaded, the operator runs:

```bash
kraken-knight init --json
kraken-knight account-id --json
kraken-knight legacy-manifest --json \
  --legacy-hints /restricted/path/hints.json
```

The operator independently reviews and pins the returned `wallet_account_id` as
`KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID`, and pins the reviewed
`legacy_manifest_hash` as `KRAKEN_KNIGHT_EXPECTED_LEGACY_MANIFEST_HASH`. The
restricted file must contain all five uncertain submissions with five unique
Kraken order IDs. Leave `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH` blank and
`KRAKEN_KNIGHT_CUTOVER_QUIESCED=false` at this stage. Do not run the supervised
reconciliation until the writer and manual-trading freeze in section 6.2 is
complete.

## 6. Legacy bot evidence and cutover

The restricted initial audit found five legacy submissions whose local state did
not prove either fills or non-fills. Host paths and service names are deliberately
excluded from this public repository; Kraken account history is authoritative.

Cutover is a maintenance event with a single-writer invariant:

### 6.1 Preserve evidence

1. Record current time, service status, process tree, unit definition, code Git
   status, configuration names (never secret values), state files, and log range.
2. Create a restricted, checksummed archive of the legacy code, dirty changes,
   state, and necessary redacted logs. Store it outside the public repository.
3. Record archive checksum, owner, permissions, and a successful test listing or
   restore. Do not delete or clean the original during initial preservation.

### 6.2 Quiesce the old writer and manual account activity

1. Stop and disable the legacy writer identified in the restricted cutover record.
2. Confirm the service is inactive and no legacy process, cron job, timer,
   supervisor, shell, or container can restart it.
3. Declare a manual-trading freeze. Do not place an order through Kraken Pro,
   another client, another API key, or an automation during reconciliation.
4. Revoke the legacy Kraken key after the writer has stopped. It is never shared
   with or reused by Kraken Knight.
5. Only now set `KRAKEN_KNIGHT_CUTOVER_QUIESCED=true`. This setting attests to
   verified external conditions; it does not stop a process or prevent a manual
   order. Set it back to `false` if the maintenance freeze ends.

### 6.3 Reconcile the exchange and pin funding evidence

1. Leave `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH` blank and run
   `kraken-knight reconcile --json --legacy-hints /restricted/path/hints.json`
   with the fresh read-only key. Barring a stronger failed gate, this first run
   intentionally returns `UNRESOLVED`, exits 3, and persists its evidence.
2. Inspect balances, held amounts, open orders, the five queried legacy order
   IDs, and the bounded closed-order, trade, ledger, liability, and fee evidence.
   The implementation does not paginate: each account-lifetime page and each
   fenced-tail page is capped at 50 `ClosedOrders`, 50 `Ledgers`, and 100
   `TradesHistory` records. If Kraken reports a higher count, the run cannot be
   `CLEAN`; do not describe the evidence as complete account history.
3. Match each of the five uncertain submissions and every other legacy record to
   Kraken identifiers, time, side, price, quantity, and fill status. The pinned
   hint file supplies claims to check and never proves them by itself.
4. Review every item in `evidence.account_lifetime_ledgers.entries` and
   `evidence.tail_ledgers.entries`, require a quiet collection, and review the
   returned `evidence.funding_manifest_hash`. Pin that hash as
   `KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH` only when every non-trade
   ledger item is a recognized positive inbound CAD deposit with a nonnegative
   fee. A withdrawal, transfer, non-CAD asset, nonpositive amount, or unknown
   type remains unexplained and must not be blessed by copying the hash.
5. Rerun the same reconciliation with the reviewed funding digest configured and
   the account still quiescent. Cancel only open orders proven to belong to the
   legacy bot, using a separately authorized operator path; Checkpoint 2 has no
   cancel command. If ownership is uncertain, stop instead of using cancel-all.
   After any cancellation, rerun from a quiet account snapshot.
6. Stop if the final status is `UNRESOLVED` or `DISARMED`, or if any order, fill,
   balance, liability, or cash flow remains unexplained. A successful process exit
   and `CLEAN` are necessary evidence, not authorization to trade or deploy.

### 6.4 Establish the opening ledger

Create an immutable Kraken Knight opening-balance event with CAD, BTC, reference
price, estimated liquidation fees, open orders (normally none), and source
account-history hashes. Existing BTC is opening inventory, not Kraken Knight
profit. The operator explicitly acknowledges whether V1 may rebalance that
inventory after arming.

Only after these steps may the new read-only/shadow services start. There is no
interval in which both bots hold live write authority.

Checkpoint 2 has not performed any of these host or account actions.

## 7. Staged rollout

### Stage 0 — local backtest and paper broker

Run the full causal report and deterministic/failure suite. Prove repeatable
manifests and exact ledger-to-performance reconciliation. Exchange writes are
impossible in this mode.

### Stage 1 — Kraken validation

Against production instrument/account state, construct representative orders
using Kraken's validation-only facility. Confirm current pair rules, precision,
minimums, authenticated fee tier, client order IDs, and error handling. Validation
success does not imply a live fill or strategy profitability.

### Stage 2 — shadow

Run the scheduled workflow with live reads and a recording-only broker. Verify
at least one complete daily decision, daily heartbeat, no duplicate on manual
rerun, and correct restart/reconciliation. The ledger must match Kraken balances
and known opening inventory.

### Stage 3 — C$25 operational probe

With separate explicit operator authorization, perform one C$25-equivalent
BTC/CAD buy and sell round trip using the production execution state machine.
Tag it `OPERATIONAL_PROBE`, exclude it from strategy returns, and report exact
orders, fills, fees, spread/slippage, balances, and alert delivery. A failed or
unknown leg disarms and is reconciled before further action.

### Stage 4 — C$250 live canary

Enable the frozen strategy with a hard C$250 BTC-notional cap. Require at least
three clean daily cycles, including no-action days, plus one controlled service
restart. Every account/ledger/alert check must pass. Lack of a natural buy signal
does not justify inventing one; the operational probe already tested execution.

### Stage 5 — controlled C$1,000 sleeve

After signed review, raise the absolute cap to C$800 while retaining the 80%
equity cap, C$200 reserve, volatility target, and all circuit breakers. The
effective cap is always the most conservative applicable constraint.

Promotion depends on engineering evidence, not positive P&L. A risk event,
unknown order, unexplained balance, missed heartbeat, failed alert, or integrity
failure blocks promotion.

## 8. Deployment procedure

For each release:

1. Announce/record the maintenance window and current mode.
2. Confirm the legacy bot remains disabled and its key revoked.
3. Reconcile Kraken and require no unresolved intent or unexplained order.
4. Back up and integrity-check the operational database and armed/risk state.
5. Verify the artifact checksum and CI provenance.
6. Install the immutable release and restore only approved production
   configuration outside it.
7. Apply tested migrations with a recorded pre-migration restore point.
8. Run offline configuration, schema, clock, permission, network, and redaction
   checks in a no-write mode.
9. Activate the release in `SHADOW` and reconcile again.
10. Verify the expected decision/heartbeat and Telegram delivery.
11. Arm only the previously approved live stage through the separate manual
    control, then observe the first complete job.
12. Record release evidence and retain the previous artifact and backup.

The deployment defaults back to shadow if a live mode or arm record is absent,
expired, inconsistent with the configured cap, or belongs to another release.

## 9. Rollback

Rollback never means restarting the legacy ML bot or re-enabling its revoked key.

1. Disarm the active release and prevent new intents.
2. Reconcile current Kraken orders, executions, balances, and exposure.
3. Cancel verified bot-owned resting orders. Do not assume process termination
   cancels them.
4. Decide whether to target cash using the risk-exit policy or preserve current
   spot inventory; record the operator decision. If exchange state is uncertain,
   preserve state and escalate rather than submitting speculative trades.
5. Stop the new services after order state is known.
6. Restore the previous Kraken Knight release and compatible database through
   the tested procedure, or remain disarmed in read-only recovery mode.
7. Reconcile before any rearm and record the incident/reason.

An application rollback must not roll back immutable order/fill facts. If a
database migration is not backward compatible, use the tested forward-recovery
release or restore only after reconciling and importing all exchange events that
occurred since the backup.

## 10. Completion evidence

Deployment is complete only when the release record contains:

- active commit/artifact/config hashes and schema version;
- systemd unit/timer state and next scheduled time in UTC;
- live mode, arm record, cap, and risk epoch;
- fresh Kraken balances/orders/executions and ledger reconciliation;
- last successful daily decision and health check;
- Telegram heartbeat delivery confirmation;
- legacy service inactive proof and legacy-key revocation confirmation; and
- backup/rollback location, checksum, and restoration status.

“Service is running” without exchange reconciliation and alert evidence is not a
successful deployment.
