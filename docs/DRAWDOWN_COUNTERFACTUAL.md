# V2 drawdown-policy counterfactual

This study answers the narrow question exposed by the sealed V1 result: how
would the unchanged 90/200/30 BTC/CAD signal have behaved if its drawdown policy
had not left it permanently in cash after May 2020?

It does **not** replace or revise V1. The original result and its checksums stay
immutable. Because that result already revealed the old holdout and motivated
these variants, every V2 number is post-holdout exploratory evidence. It cannot
be described as a fresh out-of-sample test or used by itself to authorize live
trading.

## Frozen variants

Both variants inherit V1's data cutoff, causal decision and execution clocks,
90/200/30 signal, volatility sizing, C$1,000 portfolio limits, 8% rolling-loss
gate, Kraken instrument rules, 10% execution-minute volume cap, taker fees, and
10-basis-point adverse slippage per side. Only the named high-water drawdown
behavior changes.

### No drawdown gate

`no_drawdown_gate` never disarms or liquidates solely because of a high-water
drawdown. Natural strategy exits, exposure caps, the rolling-loss gate, missing
execution evidence, fees, slippage, and every other V1 rule still apply.

This measures the signal and sizing logic without the persistent latch. It is
not a recommendation to remove drawdown protection from the live system.

### Mechanical 90-day trend rearm

`mechanical_90d_trend_rearm` preserves the 20% drawdown trigger and forced
liquidation. After each trigger it:

1. remains disarmed for at least 90 elapsed calendar days from the recorded
   `DRAWDOWN_DISARMED` observation;
2. completes any volume-capped liquidation and requires the BTC balance to be
   exactly zero;
3. waits for a causally aligned V1 `LONG_SIGNAL`, meaning positive 90-day
   momentum, close strictly above the 200-day SMA, and valid 30-day volatility;
4. tests cooldown eligibility at that signal's 00:15 UTC decision time;
5. records `DRAWDOWN_REARMED`, resets the new risk epoch's high-water mark to
   current fee-aware liquidation equity, and lets that same decision follow the
   normal execution path; and
6. permits later 20% drawdowns to disarm and repeat the same process.

An execution reference is not required to change the risk state. If the rearm
day has no qualifying `[00:15, 00:20)` minute, the normal no-fill outcome is
recorded; no price is invented, and the now-armed strategy can act on a later
causal decision.

The exact machine-readable contract is
[`research/btc_cad_v2_drawdown_counterfactual.json`](../research/btc_cad_v2_drawdown_counterfactual.json).

## Why the clock detail matters

Suppose a disarm occurs at `00:16` after the execution-minute VWAP becomes
observable. Ninety days later, a `00:15` signal is still one minute too early.
Its later execution reference cannot retroactively make that signal eligible.
The runner therefore compares the signal's decision time with the cooldown
boundary, then records any qualifying rearm at the decision time.

## Audit contract

Before producing a counterfactual result, the runner must:

- start from a clean, explicitly named Git commit;
- match the committed V2 specification hash;
- verify the frozen V1 configuration, published artifact checksums, normalized
  Kraken dataset, and provenance manifest;
- replay the persistent V1 control and require exact equality with its
  published equity, decisions, fills, and risk events;
- use the production causal engine with no injectable result or randomness;
- require a new or empty output folder; and
- hash every generated artifact.

Any mismatch fails before a V2 bundle can be called engineering-valid.

The bundle includes combined metrics, calendar returns, daily equity,
decisions, fills, and risk events with a `variant` column; pairwise deltas; the
verified fee-aware buy-and-hold entry log; a plain-English report; and four SVG
charts covering equity, drawdown, change versus V1, and risk state.

The old 60/20/20 boundaries are retained only for descriptive attribution. The
former final segment is named `opened_v1_holdout`, not `frozen_holdout`.

## Clean one-time run

Commit the implementation, tests, and pre-registration before resolving the
two identity values below. Then run:

```bash
PYTHONPATH=src .venv/bin/python scripts/run_historical_counterfactual.py \
  --data data/interim/kraken_xbtcad_daily_archive_2026-01-01.csv \
  --data-manifest data/interim/kraken_xbtcad_daily_archive_2026-01-01.manifest.json \
  --config research/btc_cad_v2_drawdown_counterfactual.json \
  --expected-commit "$(git rev-parse HEAD)" \
  --expected-prereg-sha256 "$(shasum -a 256 research/btc_cad_v2_drawdown_counterfactual.json | awk '{print $1}')" \
  --output reports/generated/btc-cad-v2-drawdown-counterfactual-2026-01-01
```

Do not tune either rule after looking at the output. A future decision to adopt
one must rely on new data after the 2026-01-01 cutoff or forward paper or
micro-live observation, with the engineering release gates evaluated
separately from historical return.
