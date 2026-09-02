# Read-only Streamlit dashboard

The dashboard is a local monitoring surface for Kraken Knight. It is not a
trading interface: there are no order buttons, Kraken clients, credentials, or
production configuration in the Streamlit process.

## Security boundary

The trading ledger and the UI are separated by a small exporter:

```text
production SQLite ledger
  -> read-only/query-only exporter (no network)
  -> /var/lib/kraken-knight-dashboard/telemetry.json
  -> Streamlit (localhost:8501 only)
  -> authenticated SSH tunnel
```

The JSON contract excludes account IDs, account-binding hashes, decision and
order IDs, configuration/input/source hashes, raw reconciliation reports, and
all credential-shaped fields. The Streamlit service cannot access
`/etc/kraken-knight` or `/var/lib/kraken-knight`; it can read only the sanitized
snapshot. The exporter cannot use the network and cannot write to the trading
state directory.

The dashboard prominently labels `SHADOW`, `PAPER`, `VALIDATE`, `BACKTEST`, or
`LIVE`. It shows strategy signals, reference BTC/CAD prices, target allocation,
ledger counts, freshness, and reconciliation health. Reference prices are not
an account-equity curve. Until live execution records reconciled equity, fees,
fills, and external cash flows, the P&L panel deliberately says unavailable.

## Local run

Install the optional UI dependency, pre-create an output directory, and export
a snapshot from a development ledger:

```console
uv sync --extra dashboard --extra dev
mkdir -p ./var/dashboard
uv run kraken-knight-dashboard-export \
  --ledger ./var/state/kraken-knight.sqlite3 \
  --output ./var/dashboard/telemetry.json
KRAKEN_KNIGHT_DASHBOARD_SNAPSHOT=./var/dashboard/telemetry.json \
  uv run streamlit run dashboard/app.py --server.address=127.0.0.1
```

Open `http://127.0.0.1:8501`. The app handles missing or malformed telemetry by
showing an unavailable state; it never falls back to the production database.

## Droplet installation

The release environment must include the dashboard extra:

```console
uv sync --frozen --extra dashboard
systemd-sysusers deploy/systemd/kraken-knight-dashboard.sysusers.conf
systemd-tmpfiles --create deploy/systemd/kraken-knight-dashboard.tmpfiles.conf
```

Install these units under `/etc/systemd/system/`:

- `kraken-knight-dashboard-export.service`
- `kraken-knight-dashboard-export.timer`
- `kraken-knight-dashboard.service`

Then reload systemd, generate the first snapshot, and enable the UI and daily
00:20 UTC refresh:

```console
systemctl daemon-reload
systemctl start kraken-knight-dashboard-export.service
systemctl enable --now kraken-knight-dashboard-export.timer
systemctl enable --now kraken-knight-dashboard.service
```

Keep port `8501` closed in the cloud firewall. Access it through SSH:

```console
ssh -N -L 8501:127.0.0.1:8501 root@143.110.213.240
```

Then browse to `http://127.0.0.1:8501`. SSH supplies authentication and
encryption without adding another paid hosting service or exposing Streamlit
directly to the Internet.

## Operator checks

```console
systemctl status kraken-knight-dashboard.service
systemctl status kraken-knight-dashboard-export.timer
journalctl -u kraken-knight-dashboard-export.service -n 20 --no-pager
curl --fail --silent http://127.0.0.1:8501/_stcore/health
```

Expected exporter output includes `exchange_writes: false`. A stale, missing,
future-dated, schema-invalid, or secret-shaped snapshot fails visibly instead
of silently presenting old data.
