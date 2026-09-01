# BTC/CAD causal historical study

## Interpretation

This is historical price evidence, not evidence of future profitability or statistical
significance. The frozen holdout reports only the pre-specified selected V1 primary-cost replay
and the pre-specified comparator evaluations. Neighboring parameters and non-primary cost cases
see development plus validation only; they diagnose fragility and do not select an optimum.

## Engineering status

- Evidence statement: **ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED.**
- Deterministic causal replay completed.
- Clean committed worktree using the production backtest engine.
- Git commit: `e22985ec83f3104736f31d5818aa5f1acda39b51`
- Pre-registration SHA-256: `95856c4fd857a6789fbf1a224865e7cf72210ba011bb76d88999875234a60749`
- Full input-data SHA-256: `eab9022203ad161559e03a1bbd9e1519198408b5a0687cd70032cf9a8a9b3ec3`
- Selected clean-sequence SHA-256: `193c8dbee45ba0dc86ab26cf9402ae80cdc0073580a0524d88bbe09b16c33724`
- Missing post-decision execution references: 355 (recorded as no-fill)

## Dataset boundary

- Study: `btc_cad_price_only_v1_preregistered`
- Selected history: 2018-01-13 through 2025-12-31
- Selected contiguous days: 2910
- Information-only warm-up: 250 days
- Discarded days outside the selected contiguous sequence: 874

- development: 2018-09-20 through 2023-02-01 (1596 days)
- validation: 2023-02-02 through 2024-07-17 (532 days)
- frozen_holdout: 2024-07-18 through 2025-12-31 (532 days)

## Frozen V1 result

- Full-period final equity: C$1225.27204548952
- Full-period net P&L: C$225.27204548952
- Full-period return: 0.22527204548952
- Full-period Sharpe: 0.3901626005703091029948867558
- Full-period maximum drawdown: 0.2083148627765723741034545679
- Frozen-holdout return: 0
- Frozen-holdout Sharpe: undefined
- Fee-aware buy-and-hold accumulation status: accumulated_until_remaining_cash_below_exchange_minimum
- First buy-and-hold fill time: 2018-09-20T00:16:00Z
- Last buy-and-hold fill time: 2018-09-22T00:16:00Z
- Buy-and-hold entry attempts / fills: 2 / 2
- Buy-and-hold volume-capped fills: 1

## Profitability status

No profitability claim is made automatically. Read `metrics.csv` together with the split labels,
cost sensitivities, drawdown chart, risk events, and robustness grid. A profitable historical row
does not authorize paper or live trading.

## Reproduction artifacts

The machine-readable source of every graph is included in this directory. `checksums.sha256`
binds the report, CSVs, JSON summary, and SVG charts; `manifest.json` records the code, config,
and input-data identities used for this run.
