# Causal Backtest and Blockchair Research Protocol

## 1. Research questions

This project answers two separate questions:

1. **V1 behavior:** How would the frozen BTC/CAD daily momentum/trend strategy
   have behaved under causal execution, current-fee assumptions, slippage, and
   the production risk constraints?
2. **Incremental on-chain value:** Do lagged, auditable Blockchair Bitcoin
   variables improve out-of-sample forecasts or net trading behavior beyond
   price-derived information?

Neither question is “Can parameters be found that make the chart attractive?”
A negative result, including no measurable Blockchair contribution, is a valid
outcome. Engineering readiness and evidence of profitability are reported in
separate sections of every report.

## 2. Pre-registration and change control

Before the final holdout is evaluated, the research run MUST freeze and hash:

- this protocol and the strategy/risk specifications;
- all feature definitions and availability lags;
- dataset manifests and exclusions;
- chronological split boundaries;
- cost/fill scenarios;
- model family, prediction timestamp, label price references, primary label cost,
  refit schedule, and hyperparameter selection rule;
- metric definitions, practical effect-size floors, bootstrap estimand and test
  construction, and the exact members of each multiple-testing family;
- Blockchair compatibility baseline, request-point budgets, and failure policy;
  and
- code, dependency lock, configuration, and random seeds.

Any choice changed after seeing final-holdout results creates a clearly labeled
exploratory follow-up and a new untouched holdout period. It MUST NOT replace the
pre-registered result.

## 3. Data provenance

### 3.1 Kraken price and execution data

Kraken is the source for BTC/CAD market history. The REST OHLC endpoint alone is
not a historical database: it has a limited rolling window and includes a
current incomplete candle. The canonical price dataset therefore begins with
Kraken's downloadable OHLCVT/trade archives and is extended with archived API
observations.

Daily signal candles are independently aggregated from lower-frequency source
data where possible. Aggregation uses UTC boundaries and records source row
counts. The API's incomplete last candle is always removed.

For every snapshot, preserve:

- source URL/type and retrieval time in UTC;
- covered interval, pair identifier, and interval;
- raw byte hash and normalized table hash;
- expected/observed rows, gaps, duplicates, and repairs;
- schema/parser version; and
- whether the payload was historical backfill or contemporaneous observation.

Historical price rows must pass OHLC invariants and monotonic, unique UTC time
checks. Missing signal history rejects that decision date; it is not forward
filled. A corrected dataset gets a new manifest and cannot silently overwrite a
published experiment.

### 3.2 Execution timestamp

Day `t` becomes observable when its UTC daily candle closes. The strategy
decision is timestamped 00:15 UTC on day `t+1`. It may not trade at day `t`'s
close.

The preferred historical execution reference is the VWAP of the first complete
one-minute bar, or the trade-derived VWAP for that minute, in
`[00:15, 00:20] UTC` on `t+1`. A minute occurring before the decision time is
forbidden. The interval is selected by its minute-open timestamp, while its
VWAP is timestamped as available only at the minute close; for example, the
`00:15` interval may execute at `00:16`, never at `00:15`. The `00:19`
interval becomes available at the `00:20` window boundary. A simulated fill
may consume at most 10% of the selected minute's observed BTC volume; any
remainder stays unfilled, and an exchange-minimum remainder records no fill. A
zero-volume minute is unavailable. If the exact window is
unavailable, the primary replay records no fill for that date; it does not
substitute a favorable later close. A separately labeled daily-open approximation
MAY be reported as a sensitivity analysis, never as exact execution evidence.

Daily OHLC cannot prove that a post-only live limit would fill. The research
therefore reports multiple explicit fill/cost scenarios rather than inventing
queue position.

### 3.3 Blockchair observations

Blockchair is a research-only source. Prefer block-level native Bitcoin fields
that can be recomputed from archived rows:

- transaction count;
- total block weight and utilization proxy;
- total native BTC fees and fee pressure;
- coin days destroyed;
- difficulty and difficulty change; and
- block count and block-production interval proxy.

Do not use Blockchair USD-denominated fields as on-chain predictors, because
they embed an external price series. Do not label address counts as unique users
or holders. Do not claim exchange inflow, outflow, or whale behavior without an
independently validated address-label dataset.

The canonical historical backfill MUST stream the daily Bitcoin `blocks` dump
partitions one at a time. It archives each compressed partition by content hash,
validates the header and rows, derives the required daily fields, and releases
the partition before processing the next one. Full-history transaction, input,
output, and address dump ingestion is prohibited on the 1 GB production host.
Server-side Blockchair aggregation, which the public v2 documentation marks
beta, is not a canonical research input. The API is limited to bounded current
tip/hash checks, compatibility probes, and contemporaneous research snapshots;
it is not used to replay every historical block.

Each raw response or dump partition records retrieval time, source URL and
partition name, request parameters after secret redaction, available HTTP and
cache/context metadata, maximum block height/hash, row/page boundaries, observed
column schema and schema hash, response/content hash, and request cost when
supplied. A dump that exposes no version records `source_version: null`; an API
response records the returned version without assuming it is complete or
current.

The initial compatibility baseline is Blockchair v2 as observed on 2026-08-31:
the detailed public document identifies itself as v2.0.80, while the live Bitcoin
stats response identified itself as v2.0.95-ie and advertised a separate v3
service. The experiment manifest MUST therefore pin the exact verified endpoint
URLs, validation timestamp, required fields and types, allowed API versions, dump
header hash, and representative fixture hashes. `next_major_update` is advisory,
not a compatibility guarantee. An unknown version, field removal/type change, or
dump-header change quarantines the input until reviewed and a new baseline is
registered.

The initial allowlist is the Bitcoin block-dump index and dated partitions under
`https://gz.blockchair.com/bitcoin/blocks/`, the stable v2
`/bitcoin/stats`, `/bitcoin/blocks` or `/bitcoin/dashboards/block/{id}` surfaces
for bounded tip/hash checks, and `/premium/stats` for key status. Query strings
and concrete fixture requests belong in the redacted manifest. Any other
Blockchair surface requires a reviewed compatibility-baseline revision before use.

Blockchair usage is governed by request points, not merely HTTP request count.
At key activation, archive a redacted entitlement/`premium/stats` observation and
record its expiry and quota semantics. Until a stricter entitlement is observed,
ordinary collection has a hard limit of 20 request points per UTC day and total
locally recorded consumption may not exceed 95% of the 100,000-point student
allowance. Warnings fire at 70% and 85%; nonessential requests stop at 85% so the
remaining allowance is reserved for finality and incident checks. Every API
response must contribute `context.request_cost` to an append-only local counter;
a missing or malformed cost is an ingestion failure, not zero. Requests are
serialized at no more than one per second with bounded jittered retries. Quota,
block, or high-load errors stop collection for the applicable window. The system
MUST NOT fall back to an unauthenticated/keyless request path.

The API key may appear in a query string. A separately constructed sanitized URL
must be used for all telemetry, and HTTP-client wire/debug logging must be off.
The key MUST be redacted before logs, exception representations, manifests,
filenames, durable raw metadata, or alerts are written.

Public repository artifacts must respect the source terms. Raw third-party data
is not redistributed unless its license/terms permit it; reproducible download
and hash manifests are preferred.

## 4. Causal Blockchair availability

Keep provenance time separate from causal availability. `retrieved_at_D` is the
real wall-clock time at which this run downloaded a partition. The nominal
historical availability convention is `A_D = 00:15 UTC on D+2`.

For a retrospective block-dump partition, define reconstructed tip `S_D` as the
highest canonical-chain height whose Blockchair block-header `time` is no later
than `A_D`. The manifest labels its availability basis
`RECONSTRUCTED_HEADER_TIME_D_PLUS_2`; it does not pretend that today’s retrieval
occurred historically. For a contemporaneous snapshot, `O_D` is the actual
successful observation time, `S_D` is the tip height recorded in that observation,
and availability is `max(A_D, O_D)` with basis `OBSERVED`. An observation that
finishes after a scheduled decision is eligible only at a later scheduled run.

Under either basis, a block at height `h` is depth-eligible only when
`h <= S_D - 6`: it has at least six successor blocks. This is seven confirmations
under the convention that counts the containing block itself. The protocol uses
the unambiguous term **six-successor depth**, not “six confirmations.” A later
observation rechecks the retained terminal height/hash for a reorg.

For a completed UTC day `D`:

- the candidate partition is the validated archived dump rows whose Blockchair
  block-header `time`, interpreted in UTC, has calendar date `D`; `median_time`
  does not assign the day, and header time is a miner-provided proxy rather than
  first-observed time;
- every candidate row must be unique, schema-valid, present in the frozen
  partition, and depth-eligible at `S_D`; the retained terminal height/hash must
  match the observed canonical chain;
- if the dump is missing, duplicate, truncated, schema-incompatible, or any
  candidate row fails the six-successor rule, the entire day is
  `MISSING_INCOMPLETE_DAY`; a partial numeric aggregate is forbidden;
- retrospective availability is exactly `A_D` only after the reconstructed
  complete-day/depth checks pass; contemporaneous availability is the actual
  `max(A_D, O_D)` described above;
- pages/heights included in the aggregation are frozen in its manifest;
- missing values remain missing, with a reason code; and
- no feature is backward filled, forward filled, or revised using later
  knowledge inside an already published replay.

A later-arriving or changed remote partition creates a new source revision and
incident record; it does not alter the already published as-of feature. Thus D+2
is a conservative publication convention, not a claim that Bitcoin header dates
or Blockchair's remote dump partitions are intrinsically immutable. Historical
Blockchair data does not contain first-observed block times, so the reconstructed
basis cannot prove exact intraday availability. Every result discloses this
limitation, and the required longer-lag sensitivity tests how much it matters.

Rolling-24-hour endpoint statistics are permitted only when snapshots were
actually observed at comparable query times. A present-day rolling endpoint
must never be treated as if it had been observed historically.

This conservative `D+2` rule plus six-successor depth is chosen to prevent
publication/finality look-ahead. A sensitivity analysis may use longer lags or
greater depth, but never a shorter primary lag or shallower primary depth.

## 5. Feature construction

All transforms are computed as-of each decision timestamp. For the frozen daily
partition `P_D`, the native base series and units are:

- `blocks_1d = count(P_D)`;
- `user_transactions_1d = sum(max(transaction_count - 1, 0))`, explicitly
  removing the one coinbase transaction in each valid block;
- `weight_wu_1d = sum(weight)`;
- `utilization_1d = weight_wu_1d / (4_000_000 * blocks_1d)`;
- `fees_sat_1d = sum(fee_total)`;
- `fee_pressure_sat_per_kwu_1d = 1_000 * fees_sat_1d / weight_wu_1d`, a
  weight-weighted rate rather than the mean of block rates;
- `cdd_1d = sum(cdd_total)`;
- `difficulty_close_1d = difficulty` of the highest-height row in `P_D`;
- `difficulty_change_1d = close_D / close_D_minus_1 - 1`, where `close` means
  `difficulty_close_1d`; it is missing when the immediately preceding calendar
  day is unavailable; and
- `block_interval_proxy_seconds_1d = 86_400 / blocks_1d`, explicitly a fixed-day
  count proxy rather than a claim about propagation or miner clock accuracy.

Division by zero makes the affected feature missing and rejects the complete
feature vector for that date. Native integer values remain in satoshi/weight
units until deterministic numeric conversion; no Blockchair USD field enters a
predictor.

For extensive base series (`blocks`, `user_transactions`, `weight`, `fees`, and
`cdd`), the 7-day transform is the sum of `D-6..D`. For rate/state series
(`utilization`, `fee_pressure`, `difficulty_close`, `difficulty_change`, and the
interval proxy), it is the arithmetic mean over `D-6..D`. The log-ratio transform
is `log(mean(D-6..D) / mean(D-29..D))` only when both means are strictly positive.
The robust standardized transform uses the median and
`1.4826 * median_absolute_deviation` of the prior 90 calendar days `D-90..D-1`.
Every required calendar day must be present; windows do not skip missing dates.
A zero median absolute deviation produces standardized value zero plus an
explicit constant-feature flag. A difficulty-regime flag is one exactly when
`difficulty_close_1d != difficulty_close_1d[D-1]` and is otherwise zero; it is
missing when either day is missing.

This base list, window list, units, and missingness behavior are frozen before
model comparison. Any added feature or changed transform is a new exploratory
experiment. Model standardization or other fitted missingness handling is fit
inside the training window only; model input never receives an implicit forward
fill.

Price-model candidates may use only features available from completed candles,
including the frozen 90-day momentum, close/SMA200 distance, and 30-day realized
volatility. The response variable never appears in a rolling transform.

## 6. V1 economic backtest

The primary V1 backtest uses the exact strategy and risk implementation loaded
by production through a simulated broker and explicit replay clock.

It compares:

- CAD cash;
- BTC/CAD buy and hold;
- close above SMA200;
- positive 90-day momentum;
- combined momentum and SMA200 without volatility sizing; and
- the frozen combined rule with 25% volatility targeting and all V1 caps.

The primary parameter values are 90-day momentum, SMA200, and 30-day volatility.
The neighboring grid below is a robustness surface, not an optimizer:

- momentum: 60, 90, 120 days;
- moving average: 150, 200, 250 days; and
- realized volatility: 20, 30, 60 days.

The selected V1 result remains the 90/200/30 point even if a neighbor performs
better. The report shows the full grid and identifies fragility rather than
silently selecting its maximum.

## 7. Fill, fee, and slippage scenarios

Each result names the exact fee and fill model. Fees are not hardcoded as an
unstated “Kraken fee”; the run captures the observed published/authenticated
tier and the assumptions applied historically.

At minimum report:

1. maker/maker fees with a stated optimistic fill assumption;
2. maker/taker fees;
3. taker/taker fees as the conservative primary cost case;
4. each scenario plus 10 basis points of adverse slippage per side; and
5. doubled fees and slippage as a stress case.

The simulator includes quantity/price rounding, minimum cost/quantity, the C$200
reserve, non-fill behavior, and the rebalance threshold. Fees reduce cash and
equity on the correct event date. Turnover and cost drag are reconciled from
individual simulated fills, not estimated only from aggregate returns.

## 8. Chronological evaluation

For each research family, use the maximum clean causal history available. The V1
price-only backtest uses all qualifying Kraken price history. The Blockchair
comparison uses the common intersection of clean price and confirmed on-chain
features and reports the resulting sample loss explicitly.

The common sample is divided chronologically:

- first 60%: development/training;
- next 20%: validation and frozen hyperparameter selection; and
- final 20%: confirmatory `FROZEN_HOLDOUT` dates.

No random train/test split is permitted. Thirty-day forward labels overlap, so
training/validation boundaries are purged by at least 30 days plus the five-minute
exit-reference window. Model training at an as-of date includes only labels whose
entry and exit references have fully matured.

For the primary `FROZEN_HOLDOUT`, the model is fit once after validation using
only pre-holdout observations whose labels have matured; preprocessing is refit
on that same training set. Parameters do not update during the final 20%. No
holdout feature, label, loss, or chart informs a model or research choice.

A separately labeled secondary `PREQUENTIAL_HOLDOUT` replays the same final dates
in time order and MAY refit monthly using only earlier observations whose labels
have matured before that refit. Features, hyperparameters, thresholds, refit
calendar, and evaluation rules remain frozen. This estimates the pre-registered
online-update policy; it is not called untouched, is not an independent holdout,
and does not supply the primary confirmatory p-values.

## 9. Blockchair challenger models

The challenger is deliberately interpretable ridge logistic regression. Index
observations by scheduled decision timestamp `T`, not by a Blockchair source date
or a preceding candle date. Every feature in a row must have
`availability_timestamp <= T`, and the prediction is persisted before its entry
price reference is observed.

Let `P_in,T` be the VWAP of the first complete one-minute bar, or trade-derived
VWAP for that minute, in `[T, T+5 minutes]`. Let `P_out,T` be the equivalent first
complete reference in `[T+30 days, T+30 days+5 minutes]`. A zero-volume minute or
a reference before its window is forbidden; if either exact window is
unavailable, the label is missing.

The primary label uses the taker fee rate `f` captured and frozen in the
pre-registration manifest and adverse slippage `s = 0.001` on each side. The same
`f` is used on entry and exit so no future fee-tier observation enters the label.
The net forward return and target are:

\[
R^{net}_T =
\frac{P_{out,T}(1-f-s)}{P_{in,T}(1+f+s)} - 1,
\qquad
y_T = 1\left[R^{net}_T > 0\right].
\]

Other maker/taker, fee, and slippage cases remain economic sensitivity analyses;
they do not redefine labels after model results are seen. This target never uses
`C_D` for an on-chain day `D` that was unavailable until `D+2`.

Three matched models use identical eligible dates, refit schedule, label, solver,
and regularization-selection procedure:

1. `PRICE_ONLY`: causal price features;
2. `ONCHAIN_ONLY`: causal Blockchair features; and
3. `PRICE_PLUS_ONCHAIN`: both feature groups.

Regularization strength is selected only within the development/validation
period using expanding-window, purged time-series folds and primary mean Brier
loss. Standardization is fit inside each training fold. Random seeds and solver
tolerances are fixed.

For economic comparison, probability greater than 0.5 is eligible and uses the
same V1 volatility sizing, reserve, rebalance, and risk logic. This threshold is
frozen before holdout and is not optimized for Sharpe. Model coefficients,
intercepts, feature availability, and calibration plots are published for each
refit.

The challenger remains shadow-only. It cannot modify V1 production targets,
orders, or risk state.

## 10. Metrics

### 10.1 Forecast metrics

Primary model metrics are Brier score and log loss. Also report calibration
intercept/slope, reliability bins with sample counts, ROC AUC as a secondary
ranking metric, class balance, and confidence intervals. Probabilities are
clipped to `[1e-6, 1-1e-6]` only for numerical evaluation of log loss; unclipped
probabilities are retained in the artifact.

The primary incremental comparisons are paired:

- `PRICE_PLUS_ONCHAIN - PRICE_ONLY`; and
- `ONCHAIN_ONLY - PRICE_ONLY` as a diagnostic, not proof of incrementality.

Calibration intercept and slope use the standard logistic recalibration of the
binary label on the prediction logit over matched `FROZEN_HOLDOUT` rows. If both
classes are not present, calibration is not estimable and the research cannot
pass the promotion gate. “Not materially degraded” means both
`abs(intercept_combined) <= abs(intercept_price) + 0.05` and
`abs(slope_combined - 1) <= abs(slope_price - 1) + 0.10`.

### 10.2 Economic metrics

For every baseline, V1, and challenger portfolio report:

- total and annualized return (CAGR);
- annualized volatility and Sharpe ratio using 365 daily observations and a
  disclosed risk-free-rate assumption;
- downside deviation and Sortino ratio;
- maximum drawdown and its duration;
- Calmar ratio;
- market exposure and CAD reserve breaches;
- turnover, decision count, order/fill count, and average holding period;
- gross P&L, fees, slippage, and net P&L; and
- calendar-year/month attribution.

Show both CAD equity and return series. Include buy-and-hold drawdown beside the
strategy; do not discuss CAGR without risk and exposure.

## 11. Statistical uncertainty and multiple testing

The sole confirmatory family contains two paired `FROZEN_HOLDOUT` hypotheses for
`PRICE_PLUS_ONCHAIN` versus `PRICE_ONLY`:

1. `H0_B: mean(d_Brier) >= 0` versus `H1_B: mean(d_Brier) < 0`; and
2. `H0_L: mean(d_LogLoss) >= 0` versus `H1_L: mean(d_LogLoss) < 0`,

where each daily difference is `combined loss - price-only loss` on the same
eligible decision timestamp. Negative values favor the combined model.

Use 10,000 paired stationary-block bootstrap replications of the aligned vector
`(d_Brier, d_LogLoss)`, with expected block length 30 days and the frozen random
seed. For metric `j`, let `m_j` be the observed mean, let `m*_{j,b}` be the mean
of resample `b`, and form the centered-null statistic
`z*_{j,b} = m*_{j,b} - m_j`. The one-sided p-value is
`(1 + count(z*_{j,b} <= m_j)) / 10_001`. Report the 2.5th and 97.5th percentiles
of the uncentered `m*_{j,b}` values as a 95% interval and the fraction whose sign
differs from `m_j`.

Apply Holm's step-down procedure at family-wise alpha `0.05` across exactly these
two p-values. Both adjusted p-values must pass. In addition, the practical
effect-size floor requires at least a 1% relative reduction in each mean loss:
`mean(Brier_combined) <= 0.99 * mean(Brier_price)` and
`mean(LogLoss_combined) <= 0.99 * mean(LogLoss_price)`. A smaller improvement is
reported but cannot pass the promotion gate.

Repeat intervals, sign stability, and effect-size checks at expected block
lengths 14 and 60 days as sensitivity analyses; their p-values do not enter the
confirmatory family. `ONCHAIN_ONLY`, the V1 robustness grid, feature-family
ablations, permutation results, economic metrics, subperiods, and
`PREQUENTIAL_HOLDOUT` are diagnostic or exploratory and cannot supply a missing
confirmatory result. If a later protocol assigns p-values to any such analysis,
it must enumerate a separate family before viewing results and apply Holm within
that family.

This bootstrap estimates the mean paired loss advantage conditional on the
frozen fitted predictions and observed dependent market path. It does not include
model-selection uncertainty or imply that future regimes resemble the sample.
No claim should describe it as uncertainty for the full research process.

Financial returns are nonstationary and the sample is small; bootstrap intervals
describe uncertainty under the stated resampling scheme, not a guarantee that
future regimes resemble history.

## 12. Ablation and robustness requirements

The Blockchair report MUST include:

- price-only versus combined matched-date performance;
- leave-one-feature-family-out ablations;
- feature permutation performed within time blocks, never across all dates;
- the primary D+2/six-successor result versus D+3 and D+7 at the same depth, and
  D+2 at twelve-successor depth;
- fee/slippage stress;
- subperiod/regime results without selecting a favorable subperiod; and
- missing-data and API-schema-change sensitivity.

A contribution is not attributed to “on-chain data” when it disappears after
removing a price-derived or USD-denominated field.

## 13. Interpretation and promotion gate

Every report begins with one of these evidence statements:

- `ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED`
- `ENGINEERING_VALIDATED, NEGATIVE_OUT_OF_SAMPLE_RESULT`
- `RESEARCH_INVALIDATED` with reason; or
- `CANDIDATE_INCREMENTAL_SIGNAL` pending shadow evidence.

The combined model can be proposed for a future V2 review only when:

- both `FROZEN_HOLDOUT` Brier score and log loss improve over `PRICE_ONLY` by at
  least the pre-registered 1% relative effect-size floor;
- both confirmatory paired p-values survive Holm correction at family-wise
  alpha `0.05`;
- calibration is not materially degraded;
- improvement persists under cost, block-length, lag, and ablation checks;
- the separately labeled `PREQUENTIAL_HOLDOUT` does not reverse the direction of
  both primary loss improvements;
- the economic version does not rely on one isolated trade or period; and
- 30 consecutive days of shadow operation produce complete, on-time,
  reproducible features and decisions.

Passing this gate does not change V1. Promotion requires a new strategy version,
risk review, implementation tests, and the full canary ladder.

## 14. Reproducibility artifacts

Each published experiment produces an immutable run directory containing:

- a machine-readable manifest and experiment ID;
- Git commit and dirty-state assertion;
- Python/dependency lock and platform details;
- redacted configuration plus configuration hash;
- raw and normalized data hashes and retrieval metadata;
- exclusion/gap report;
- train/validation/holdout boundaries;
- features, predictions, decisions, simulated fills, and daily equity;
- metrics, bootstrap samples or their deterministic seeds, and adjusted tests;
- a human-readable report with assumptions and limitations; and
- a single command contract for exact rebuild from permitted inputs.

CI runs a small fixed-fixture replay and verifies artifact hashes. Full historical
runs execute off the production trading path and publish a checksum manifest.
A notebook may present results, but tested modules—not notebook cell order—are
the source of truth.
