# Operator Runbook

## 1. Scope and operating rule

This runbook is for the sole human operator of the dedicated BTC/CAD account.
When local state and Kraken disagree, Kraken is authoritative for orders,
executions, and balances, while the append-only local ledger remains the audit
record. The safe default is to **disarm, reconcile, and preserve evidence**.

Stopping a process does not cancel an exchange order or sell BTC. Canceling an
order does not sell BTC. The Kraken dead-man switch can affect every account
order. Never assume one action implies another.

The concrete CLI is not implemented at the documentation checkpoint. Before the
live canary, it must provide authenticated, audited equivalents of:

- `status --json` — mode, arm state, release, risk gates, lease, last jobs;
- `reconcile --read-only` — compare Kraken with the ledger without writes;
- `orders` — list intents/attempts and linked Kraken state;
- `disarm --reason ...` — persist disarm and block new exposure;
- `cancel-bot-orders --intent ...` — cancel only proven bot-owned orders;
- `target-cash --reason ...` — invoke bounded risk-exit behavior;
- `run-daily --mode shadow` — idempotent scheduled/manual replay;
- `verify-alerts` — exercise redacted Telegram delivery; and
- `rearm --review ...` — require explicit reviewed evidence.

Examples in this document name operations, not currently runnable commands.

## 2. Severity and response targets

| Severity | Examples | Initial action |
| --- | --- | --- |
| SEV-1 | Unknown live order, unexplained balance, credential exposure, concurrent writer, drawdown disarm, unauthorized transfer | Disarm immediately, preserve evidence, reconcile; revoke credentials when compromise is possible |
| SEV-2 | Kraken outage with resting order, partial fill not converging, database integrity failure, disk full, missing daily heartbeat in live mode | Block new exposure, inspect/reconcile, notify operator promptly |
| SEV-3 | Stale candle causing no action, Telegram delivery failure with local health intact, Blockchair collection/schema failure | Keep or reduce risk according to policy, repair without inventing data |
| INFO | Normal no-trade, validated cancellation, release/shadow heartbeat | Record and review in the daily heartbeat |

Response targets are operational priorities, not promises of resolution. A SEV-1
is never downgraded merely because market exposure is small.

## 3. Expected daily heartbeat

The job runs at 00:15 UTC. By 00:30 UTC, the operator should receive one daily
heartbeat containing:

- strategy date, UTC calculation time, release/config/data hashes;
- completed-candle end time and validation status;
- close, 90-day momentum, SMA200, and 30-day annualized volatility;
- eligibility, pre-risk target, post-risk target, and reason code;
- account equity, BTC/CAD balances, current/target exposure, and CAD reserve;
- rolling-24-hour return, high-water mark, drawdown, active gates, and arm state;
- intended/submitted/final order state, fills, fees, and slippage if applicable;
- exchange-versus-ledger reconciliation result; and
- source, service, database, clock, and alert health.

`NO_REBALANCE`, `NO_ACTION_*`, and `BELOW_EXCHANGE_MINIMUM` are valid outcomes
only when their reasons are present. No message is not a no-trade signal.

## 4. Daily operator check

1. Confirm exactly one heartbeat exists for the strategy date.
2. Confirm its release, mode, arm state, and cap match the approved deployment.
3. Confirm the candle is complete and the calculation values are finite.
4. Confirm equity, BTC, CAD, open orders, fills, and fees reconcile with Kraken.
5. Confirm no manual/unknown order or asset appears in the dedicated account.
6. Confirm no active risk gate is omitted from the decision explanation.
7. If an order occurred, match the deterministic client ID and Kraken ID through
   intent, attempts, fills, fees, final balances, and alert.
8. Record review outcome. Escalate any unexplained difference; do not wait for the
   next daily job to resolve itself.

## 5. No heartbeat by 00:30 UTC

1. Inspect systemd timer/service status, current release, and local health logs.
2. Check host UTC time synchronization, disk space, memory pressure, database
   access, network/DNS/TLS, and Telegram delivery state.
3. Query the read-only status and Kraken reconciliation paths.
4. If the job might have reached order submission, treat it as an unknown-order
   incident before rerunning anything.
5. If no exchange intent exists and inputs are valid, an idempotent manual daily
   replay may be run under the same strategy date and decision identity.
6. If causality or prior execution cannot be proved, leave the day as no action,
   remain disarmed or block exposure increases, and document the missed run.

Never change the system date, decision key, or input snapshot to force a second
opportunity for the same day.

## 6. Kraken data or API outage

### Public market data unavailable or stale

- Block exposure increases.
- Continue read-only account health checks if the private API is available.
- Do not substitute another exchange's BTC price for a live Kraken order.
- Do not use an incomplete candle or later close to manufacture the signal.
- Alert with last known good observation and freshness; retry with bounded
  backoff that respects rate limits.

### Private account API unavailable

- Disarm new intents because balances and orders cannot be reconciled.
- Check whether a bot-owned resting order may exist from the last known state.
- Preserve request/client IDs and timestamps; do not retry a timed-out submit.
- Restore private connectivity and reconcile open/closed orders plus executions.
- If a live resting order cannot be observed, treat the incident as SEV-1.

### Kraken maintenance or pair not tradeable

- Record pair status and reject submissions.
- Do not route to another pair or exchange.
- Risk-reduction intent remains pending/disarmed and alerts until it can be
  executed within policy.

## 7. Unknown order outcome

An HTTP/WebSocket timeout, disconnect, or ambiguous error after submission is
`UNKNOWN`, not `FAILED`.

1. Disarm further submissions for the intent and retain the writer lease if safe.
2. Preserve request hash, deterministic client ID, nonce/timestamps after secret
   redaction, last response/error, and local balance observation.
3. Query Kraken open orders by client/exchange identifier where possible.
4. Query closed/canceled orders and executions over an interval beginning before
   the attempted submission.
5. Reconcile balances and held funds.
6. If found, attach the Kraken order/fills to the original attempt and continue
   its state machine.
7. If absence is proven by the implementation's tested recovery rule, mark the
   attempt failed/canceled before considering another bounded attempt.
8. If ambiguity remains, stay disarmed and escalate. Never create a fresh
   strategy intent or manually repeat the order.

## 8. Partial fill or stuck order

1. Identify the original intent and confirm order ownership.
2. Read current cumulative fill, remaining quantity, fees, status, and balances
   from Kraken.
3. Update the ledger only with immutable exchange facts.
4. For an entry, use the bounded attempt/expiry policy; do not chase after the
   attempt window.
5. For risk reduction, cancel/replace only within the configured three-attempt
   and 50-basis-point collar rules.
6. Recalculate any remainder from confirmed current exposure, not the original
   requested quantity.
7. If the remainder is below exchange minimum, record dust and finish with an
   explicit residual-exposure alert.
8. Reconcile balances before reporting a terminal result.

## 9. Balance or ledger mismatch

1. Disarm and block all exposure increases.
2. Capture Kraken balances, held amounts, open orders, executions, fees, and
   deposits/withdrawals with timestamps and hashes.
3. Check unprocessed partial fills, fee currency, rounding, opening inventory,
   external cash flows, and manual activity.
4. Replay ledger projections from immutable facts into a temporary view; do not
   edit past fills or balances to make the total match.
5. Record an adjustment only when supported by an identified external fact and
   retain the prior projection.
6. A mismatch above tested rounding tolerance is SEV-1 until explained. Rearm
   requires a clean signed reconciliation.

## 10. Rolling-loss and drawdown gates

### 8% rolling-24-hour gate

- Confirm both cash-flow-adjusted equity observations and current book freshness.
- Verify bot-owned exposure-increasing orders were canceled.
- Exposure reduction and reconciliation continue; entries remain blocked through
  the stored gate-expiry time.
- Do not manually clear or shorten the 24-hour block.
- Expiry does not override another disarm or drawdown event.

### 20% high-water drawdown

- Treat the alert as SEV-1.
- Confirm disarmed state was persisted before exit attempts.
- Confirm all proven bot-owned orders were canceled.
- Observe bounded target-cash attempts, partial fills, and residual dust.
- If the market moves beyond the exit collar or Kraken is unavailable, preserve
  the disarmed target-cash state and escalate; do not authorize an unbounded
  market order from Telegram.
- Complete incident review before any rearm. The high-water mark is not reset by
  restarting, redeploying, depositing funds, or clicking rearm.

## 11. Emergency disarm and target cash

Emergency disarm is appropriate for unknown account state, a suspected bug,
credential incident, concurrent writer, broken risk control, or operator request.

1. Persist the reason and disarm event if local storage is safe.
2. Prevent new intents and cancel only verified bot-owned resting orders.
3. Reconcile Kraken.
4. Decide explicitly whether the incident requires target cash. Disarming alone
   leaves existing BTC untouched.
5. If target cash is required, use the same bounded risk-exit state machine and
   monitor through final reconciliation.
6. Stop services only after order state is known, unless continuing the process
   itself is causing unauthorized submissions; in that case stop first, then
   inspect Kraken directly.

Do not use Kraken cancel-all unless the operator has verified that the dedicated
account contains no unrelated order and the incident policy specifically calls
for it.

## 12. Process or host restart

1. A restart must come up read-only/disarmed until startup reconciliation passes.
2. Verify the active release/config hashes, database integrity, stored risk
   epoch/high-water mark, cap, mode, and arm record.
3. Check Kraken open orders, closed orders, executions, and balances before the
   execution worker resumes.
4. Resolve any `SUBMITTING`, `UNKNOWN`, `OPEN`, `PARTIALLY_FILLED`, or
   `CANCEL_PENDING` intent.
5. Prove the previous writer lease is expired and only one current writer exists.
6. Verify Telegram delivery.
7. Rearm only if the incident category permits automatic continuity and every
   release gate passes; risk/credential/account-integrity incidents always need
   manual review.

## 13. Database, disk, and backup incidents

### Disk pressure

- Block new risk before the filesystem is full.
- Preserve the current database and newest logs; remove nothing required for
  order recovery.
- Rotate/compress only according to retention policy. Never delete an unknown
  active database/WAL file.

### SQLite integrity failure

- Stop the writer and disarm through the safest available control.
- Reconcile Kraken independently and preserve the damaged database, WAL, and SHM
  files as evidence.
- Restore the last verified backup into a separate path.
- Import all later Kraken orders/executions and reconstruct projections; do not
  overwrite the damaged evidence.
- Resume only after integrity, uniqueness, balance, and recovery replay tests.

Backups are not considered valid until a test restore and integrity check have
been recorded.

## 14. Credential compromise or authentication anomaly

1. Disarm/stop the writer and inspect Kraken directly.
2. Revoke the suspected Kraken key immediately when secrecy is at risk.
3. Inspect open orders, executions, balances, API-key activity where available,
   and account security settings.
4. Cancel unauthorized orders and address unauthorized exposure through the
   bounded incident plan; contact Kraken support when required.
5. Rotate Blockchair or Telegram secrets independently if exposed.
6. Search sanitized code, Git history, logs, shell history, CI artifacts, and
   alerts for the leak path without redisplaying the secret.
7. Create a new least-privilege, IP-restricted key. Never restore the compromised
   key or copy the old bot's credentials.
8. Require shadow and validation gates again before live rearm.

## 15. Legacy bot or second writer detected

If `alpha-seek.service`, another private-API process, or unexplained nonce stream
appears:

1. Treat it as SEV-1 and disarm Kraken Knight.
2. Stop and disable the unauthorized/legacy writer if ownership is proven.
3. Revoke any shared or legacy key.
4. Reconcile every order and execution from before the overlap began.
5. Preserve service/process/log evidence and update the incident timeline.
6. Do not rearm until single-writer proof and a clean opening/current ledger are
   restored.

## 16. Blockchair or challenger failure

- Pause the affected ingestion/model build and retain the last immutable raw
  snapshot.
- Record API version/schema/cache metadata and the failed contract validation.
- Never fill the missing feature from a future response or reuse yesterday's
  feature as though current.
- The V1 price-only decision remains independent. Confirm that no production
  strategy hash or target changed.
- A Blockchair failure can delay challenger shadow qualification indefinitely;
  it cannot promote a fallback feature or bypass the 30-day shadow requirement.

## 17. Telegram alert failure

1. Persist the intended redacted alert and delivery error locally.
2. Check token validity, destination, network, rate limits, and Telegram status
   without logging the token.
3. Use the approved secondary local health channel to notify the operator.
4. Do not promote rollout stages while alert delivery is broken.
5. A necessary risk reduction may continue; include its complete alert history
   after delivery recovers.

## 18. Manual rearm checklist

Rearm after a drawdown, credential incident, account mismatch, second writer,
database integrity failure, or unauthorized trade requires all of:

- incident cause and timeline documented;
- Kraken balances/orders/executions reconciled with immutable local facts;
- no unknown intents or unexplained holdings/liabilities;
- legacy/unauthorized writers disabled and credentials revoked as applicable;
- active release, tests, configuration hash, cap, and strategy version reviewed;
- high-water/risk epoch treatment explicitly acknowledged without erasing
  history;
- fresh candle, instrument, fee, book, clock, database, disk, and alert checks;
- shadow/validation evidence appropriate to the change; and
- operator identity, UTC timestamp, review reference, reason, and remaining
  exposure saved in the rearm record.

Rearm authorizes only the configured rollout stage. It does not authorize a
manual trade or a higher capital cap.

## 19. Incident record and postmortem

Every SEV-1/SEV-2 record includes:

- UTC start/detection/resolution times;
- release/config/schema/strategy versions;
- account exposure and market context;
- source hashes and redacted logs;
- decisions, intents, attempts, Kraken order/trade IDs, fills, fees, and balances;
- risk and alert state before/after;
- operator actions and their evidence;
- root cause, contributing controls, and what detected the event;
- financial effect separated into strategy, probe, fees, and incident cost; and
- corrective tests, documentation changes, rollout regression, and owner.

“The bot is running again” is not closure. Closure requires reconciled exchange
state, an explained failure mode, and a regression control proportional to the
incident.
