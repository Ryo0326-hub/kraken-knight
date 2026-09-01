# Production V3 daily shadow decision

`kraken-knight daily --json` is the first bounded production workflow for the
no-drawdown-gate strategy. It is a decision recorder, not a trading command.

At 00:15 UTC it:

1. calls only Kraken's public BTC/CAD daily OHLC endpoint;
2. quarantines Kraken's mutable final row;
3. requires a fresh, contiguous history of at least 200 completed UTC candles;
4. evaluates the frozen 90-day momentum, 200-day trend, and 30-day volatility
   sizing policy;
5. records exactly one immutable decision for the latest completed strategy
   date; and
6. reports `exchange_writes=false` and `order_intent_created=false`.

The command accepts `paper`, `shadow`, and `validate` modes. It rejects
`backtest`; `Settings` continues to reject `live`. No account balances, private
Kraken endpoint, broker adapter, or order-intent builder is present in this
workflow, so its target is a strategy weight only—not an executable order.

## Release binding and retry behavior

Install `deploy/systemd/kraken-knight-daily.env.example` as the separate
`/etc/kraken-knight/daily.env` file and set `KRAKEN_KNIGHT_RELEASE_ID` to the
full lowercase Git commit SHA. The checked-out release must resolve to
`/opt/kraken-knight/releases/<that-same-sha>`; the command rejects a missing,
malformed, or mismatched identity. The configuration hash binds that identity
to the strategy ID, mode, pair, frozen risk fingerprint, strategy-policy hash,
and daily-job code schema.

The daily environment is an exact allowlist of state directory, account, pair,
strategy, release, and optional non-systemd mode fields. The job rejects every
other `KRAKEN_KNIGHT_*` variable—including current or future private, research,
alert, and live-arming fields—before parsing settings, initializing state, or
making its public request. The primary systemd unit also pins `shadow`, loads
only `daily.env`, and makes the reconciliation `config.env` inaccessible inside
its mount namespace. The separate reconciliation unit remains the only unit
that loads `config.env`.

The input hash includes only completed-candle evidence. It deliberately excludes
the mutable OHLC tail and observation timestamp, so a same-release retry with the
same completed data returns the existing decision ID. Changed completed data,
mode, release, or policy for the same account/strategy/date conflicts with the
immutable ledger row and fails closed.

## Operator check

Run manually before enabling the timer:

```console
kraken-knight daily --json
kraken-knight status --json
```

Confirm the result includes:

- `drawdown_policy_mode: "disabled"`;
- `exchange_writes: false`;
- `order_intent_created: false`;
- the expected release and strategy IDs; and
- one added decision with no added order intent.

The systemd service runs this same finite command. Promotion to authenticated
account sizing or live execution requires a separate implementation and review;
this workflow does not provide either capability.
