# Historical BTC/CAD Backtest

This checkpoint asks a narrow question: how would the frozen Kraken Knight V1
strategy have behaved on historical BTC/CAD data under observable timing,
current fee assumptions, adverse slippage, and the C$1,000 risk policy?

It does not search for the most profitable parameters, use Blockchair as a
price source, or authorize live trading. Blockchair belongs to the later
on-chain challenger study, where it must prove incremental value beyond this
price-only baseline.

## The causal clock

For UTC day `D`:

1. Kraken trades during `D` form the daily OHLCV candle.
2. That candle becomes complete at `00:00` on `D+1`.
3. The strategy makes its decision at `00:15` on `D+1` using no later candle.
4. The first positive-volume one-minute interval whose start is in
   `[00:15, 00:20)` supplies the execution VWAP.
5. A minute VWAP is timestamped only when that interval closes. For example,
   the `00:15` minute is available at `00:16`; it is never backdated.
6. A simulated order may use at most 10% of that minute's observed BTC volume.
7. If no qualifying minute exists, the replay records `NO_FILL_REFERENCE_UNAVAILABLE`
   and keeps the existing holdings. It does not substitute the daily open or a
   favorable later price.

This timeline is the main distinction between a causal replay and a chart that
quietly trades at a price known only after the decision.

## Data and provenance

The frozen study uses Kraken's official downloadable Time-and-Sales archive,
which contains one headerless `timestamp,price,volume` CSV per pair. The exact
`XBTCAD.csv` member is pinned by SHA-256, ZIP CRC, compressed size, and
uncompressed size. No Kraken key or Blockchair key is accepted. The importer:

- verifies the extracted member before reading any row;
- validates its three-field schema, positive values, chronology, and cutoff;
- stops at a fixed exclusive UTC-midnight cutoff;
- preserves missing UTC days instead of filling them; and
- hashes the source CSV, normalized CSV, and manifest.

Download Kraken's complete official archive from the documentation page and
extract only `TimeAndSales_Combined/XBTCAD.csv` to the path below. Then run the
credential-free normalization:

```bash
PYTHONPATH=src .venv/bin/python scripts/import_kraken_trade_archive.py \
  --source-csv data/raw/kraken_time_and_sales_2025/XBTCAD.csv \
  --cutoff 2026-01-01 \
  --normalized-csv data/interim/kraken_xbtcad_daily_archive_2026-01-01.csv \
  --manifest data/interim/kraken_xbtcad_daily_archive_2026-01-01.manifest.json \
  --archive-url https://drive.google.com/file/d/10zh3tDpqANYvVtYVgczwVz3UZFRUb1el/view \
  --archive-file-id 10zh3tDpqANYvVtYVgczwVz3UZFRUb1el \
  --entry-name TimeAndSales_Combined/XBTCAD.csv \
  --expected-source-sha256 7210cbce6a23ec7e27f3a8c3fefd45fb9b8e6855702fcfca2c28b8a7af45217c \
  --zip-member-crc32 0x2b123610 \
  --zip-compressed-size-bytes 36888748 \
  --zip-uncompressed-size-bytes 139210779
```

This snapshot contains 4,630,185 BTC/CAD trades through December 31, 2025. The
exclusive 2026-01-01 cutoff is deliberately immutable; adding later quarterly
files would define a new experiment rather than revising this holdout.

The repository also retains a rate-limited, resumable public-Trades API
backfiller for future rolling studies. It is not the source used by this frozen
result.

## Frozen evaluation

Before the final run, [`research/btc_cad_v1_backtest.json`](../research/btc_cad_v1_backtest.json)
freezes the data cutoff, 250-day common warm-up, 60/20/20 chronological split,
90/200/30 strategy, C$1,000 portfolio constraints, current observed Kraken
instrument rules, seven cost cases, and 27 neighboring parameter combinations.

The clean sample is the longest contiguous run of observed UTC daily candles;
an equal-length tie selects the earlier run. The first 250 days are information
only. Every portfolio begins with C$1,000 at the same evaluation boundary.

The primary cost case assumes taker fees on both sides plus 10 basis points of
adverse slippage per side. Simulated orders apply the frozen C$0.10 price tick,
eight-decimal BTC quantity increment, 0.00005 BTC order minimum, and C$1 cost
minimum. Applying today's observed rules to older dates is an explicit
historical approximation, not a claim that Kraken's rules never changed.

The selected 90/200/30 result is evaluated on the frozen holdout. Neighboring
parameters are displayed only on development plus validation data, as a
fragility diagnostic; they cannot replace the selected point.

The fee-aware BTC buy-and-hold comparator does not assume that the entire
C$1,000 can fill in one thin minute. Starting at the common evaluation
boundary, it buys at the first available post-decision minute and retries on
later available days, always using the same fees, adverse slippage, instrument
rules, and 10% volume-participation cap. Accumulation stops when the remaining
cash is below Kraken's frozen minimum cost or the sample ends. Every attempt,
including cap-limited and below-minimum no-fills, is recorded separately; this
keeps the benchmark causal without turning one low-volume minute into permanent
underinvestment.

After committing all code, configuration, and tests, run the final study from a
clean worktree:

```bash
PYTHONPATH=src .venv/bin/python scripts/run_historical_backtest.py \
  --data data/interim/kraken_xbtcad_daily_archive_2026-01-01.csv \
  --data-manifest data/interim/kraken_xbtcad_daily_archive_2026-01-01.manifest.json \
  --config research/btc_cad_v1_backtest.json \
  --expected-commit "$(git rev-parse HEAD)" \
  --expected-prereg-sha256 "$(shasum -a 256 research/btc_cad_v1_backtest.json | awk '{print $1}')" \
  --output reports/generated/btc-cad-v1-2026-01-01
```

Resolve and record both hashes before running the command. The runner requires
the full 40-character commit and the exact 64-character pre-registration hash,
checks that the data manifest describes a complete archive, and refuses a dirty
worktree unless `--allow-dirty` is supplied. That override is for synthetic
development only and is prominently marked inadmissible for a final holdout
result.

## Reading the output

The artifact bundle contains:

- `summary.json`, `manifest.json`, and `checksums.sha256` for identity and
  reproducibility;
- `metrics.csv` and `calendar_returns.csv` for full-period and split results;
- `daily_equity.csv`, `decisions.csv`, `fills.csv`, and `risk_events.csv` for
  reconciliation from each strategy decision to P&L; `fills.csv` includes the
  observed execution-minute volume, realized participation, and whether the
  volume cap caused a simulated partial fill;
- `buy_and_hold_entries.csv` for every causal benchmark accumulation attempt,
  its affordable and permitted quantities, fill outcome, costs, and remaining
  cash;
- `robustness.csv` for the pre-holdout parameter surface;
- `charts/equity.svg`, `charts/drawdown.svg`, and `charts/robustness.svg`; and
- `report.md` for the assumptions, limitations, and evidence statement.

Read return together with drawdown, exposure, turnover, costs, number of fills,
and subperiod attribution. A positive backtest is historical evidence under
these assumptions, not proof of future profit. A negative result does not mean
the engineering failed; it means the frozen strategy did not overcome its
historical path and simulated costs.

## Blog-post structure

A clear write-up can follow the same order:

1. the trading question and frozen rule;
2. why time-causal execution is harder than shifting a daily signal by one row;
3. raw-data provenance and byte-level archive identity;
4. fees, slippage, order minimums, and risk constraints;
5. the untouched selected-strategy holdout result;
6. equity, drawdown, calendar, and robustness visuals; and
7. limitations and the next paper/shadow or micro-live checkpoint.

Keep engineering confidence and profitability confidence as separate
conclusions. That separation is both more honest and more useful on a resume.
