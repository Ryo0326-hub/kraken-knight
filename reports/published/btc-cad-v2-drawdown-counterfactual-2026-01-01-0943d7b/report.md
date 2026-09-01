# BTC/CAD drawdown-policy counterfactual

## Interpretation first

This is an **exploratory historical counterfactual after the V1 holdout was opened**. It is not
fresh out-of-sample evidence, does not establish future profitability, and authorizes neither
paper nor live trading. Development, validation, and `opened_v1_holdout` labels are descriptive
time partitions only.

## Engineering status

- Status: **ENGINEERING_VALIDATED**
- Production persistent-mode replay exactly matched the sealed V1 daily equity, decisions,
  fills, and risk-event records before either counterfactual was evaluated.
- Git commit: `0943d7bf84321886a9956b105bcb8cfff81dd61b`
- V2 pre-registration SHA-256: `2c10774480b2c2fcf056a5227b2df1d1875cc621b05afd18408bd1f9d92204f9`
- Base V1 protocol SHA-256: `95856c4fd857a6789fbf1a224865e7cf72210ba011bb76d88999875234a60749`
- Input-data SHA-256: `eab9022203ad161559e03a1bbd9e1519198408b5a0687cd70032cf9a8a9b3ec3`
- Selected clean-sequence SHA-256: `193c8dbee45ba0dc86ab26cf9402ae80cdc0073580a0524d88bbe09b16c33724`
- Parameter optimization, robustness search, and cost sweeping performed: no

## Dataset and descriptive partitions

- Selected history: 2018-01-13 through 2025-12-31
- Common information-only warm-up: 250 days

- development: 2018-09-20 through 2023-02-01 (1596 days)
- validation: 2023-02-02 through 2024-07-17 (532 days)
- opened_v1_holdout: 2024-07-18 through 2025-12-31 (532 days)

## Full-period historical outputs

- cad_cash: final equity C$1000; return 0; maximum drawdown 0; trades 0
- fee_aware_btc_buy_and_hold: final equity C$14286.863984417056; return 13.286863984417056; maximum drawdown 0.7466956382129823807136516573; trades 3
- sealed_frozen_v1_persistent_disarm: final equity C$1225.27204548952; return 0.22527204548952; maximum drawdown 0.2083148627765723741034545679; trades 56
- no_drawdown_gate: final equity C$3028.900851916576; return 2.028900851916576; maximum drawdown 0.236474580272131925639065434; trades 358
- mechanical_90d_trend_rearm: final equity C$2792.128623894256; return 1.792128623894256; maximum drawdown 0.2811374180307440141354761957; trades 348

## Frozen changes

- `no_drawdown_gate`: the 20% drawdown gate, disarm, and forced liquidation are disabled.
- `mechanical_90d_trend_rearm`: the same 20% disarm and liquidation remain; after 90 calendar
  days, a new causal long signal mechanically rearms the strategy and begins a new high-water
  risk epoch.
- Signal, sizing, BTC/CAD data, causal execution reference, exchange minimums, participation
  cap, fee, slippage, rolling-loss gate, and all other portfolio assumptions remain V1-identical.

## Audit artifacts

`pairwise_deltas.csv` compares each counterfactual directly with sealed V1. Every graph has a
machine-readable CSV source. `checksums.sha256` binds the report, CSVs, summary, and SVGs;
`manifest.json` records the code, parent publication, configuration, and data identities.
