"""Read-only Streamlit dashboard for a sanitized Kraken Knight snapshot."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import streamlit as st

from kraken_knight.dashboard_snapshot import (
    DashboardSnapshot,
    DashboardSnapshotError,
    SignalTelemetry,
    load_dashboard_snapshot,
)

SNAPSHOT_ENV = "KRAKEN_KNIGHT_DASHBOARD_SNAPSHOT"
DEFAULT_SNAPSHOT = Path("/var/lib/kraken-knight-dashboard/telemetry.json")
STALE_AFTER_SECONDS = 36 * 60 * 60
SIGNAL_STALE_AFTER_SECONDS = 36 * 60 * 60

st.set_page_config(
    page_title="Kraken Knight",
    page_icon="♞",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      :root { color-scheme: dark; }
      .stApp {
        background:
          radial-gradient(circle at 90% -10%, rgba(91, 71, 255, .18), transparent 32rem),
          radial-gradient(circle at 2% 18%, rgba(0, 213, 190, .10), transparent 28rem),
          #0a0d12;
      }
      .block-container { max-width: 1240px; padding-top: 2.2rem; }
      [data-testid="stMetric"] {
        background: rgba(18, 24, 34, .82);
        border: 1px solid rgba(255,255,255,.08);
        border-radius: 14px;
        padding: .85rem 1rem;
      }
      .mode-banner {
        border-radius: 12px;
        font-weight: 750;
        letter-spacing: .045em;
        margin: .55rem 0 1.35rem;
        padding: .75rem 1rem;
      }
      .mode-shadow { background: rgba(245, 166, 35, .14); border: 1px solid #f5a623; }
      .mode-live { background: rgba(255, 75, 75, .17); border: 1px solid #ff4b4b; }
      .mode-safe { background: rgba(0, 213, 190, .13); border: 1px solid #00d5be; }
      .mode-unknown { background: rgba(160, 174, 192, .12); border: 1px solid #718096; }
      .subtle { color: #aeb8c7; }
      .footnote {
        color: #8f9bad; font-size: .78rem; margin-top: 2rem;
        border-top: 1px solid rgba(255,255,255,.07); padding-top: 1rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=30, show_spinner=False)
def _load_snapshot(path_text: str, modified_ns: int) -> DashboardSnapshot:
    del modified_ns  # The value is part of the cache key.
    return load_dashboard_snapshot(Path(path_text))


def _snapshot_path() -> Path:
    configured = os.environ.get(SNAPSHOT_ENV, "").strip()
    return DEFAULT_SNAPSHOT if not configured else Path(configured)


def _read_snapshot() -> DashboardSnapshot:
    path = _snapshot_path()
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise DashboardSnapshotError("dashboard telemetry is not available yet") from exc
    return _load_snapshot(str(path), modified_ns)


def _mode_banner(mode: str | None) -> tuple[str, str]:
    if mode == "live":
        return "mode-live", "LIVE — REAL CAPITAL IS AT RISK"
    if mode == "shadow":
        return "mode-shadow", "SHADOW — SIGNAL OBSERVATION ONLY; NO LIVE ORDERS"
    if mode == "paper":
        return "mode-safe", "PAPER — SIMULATED EXECUTION ONLY"
    if mode == "validate":
        return "mode-safe", "VALIDATE — EXCHANGE VALIDATION ONLY"
    if mode == "backtest":
        return "mode-safe", "BACKTEST — HISTORICAL RESEARCH ONLY"
    return "mode-unknown", "NO VERIFIED RUNTIME TELEMETRY"


def _percent(value: Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    number = value * Decimal("100")
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.2f}%"


def _cad(value: Decimal | None) -> str:
    return "—" if value is None else f"C${value:,.2f}"


def _utc_label(value: datetime | None) -> str:
    if value is None:
        return "Not available"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _signal_chart(signals: tuple[SignalTelemetry, ...]) -> None:
    values = [
        {
            "date": signal.strategy_date.isoformat(),
            "BTC/CAD close": None if signal.close_cad is None else float(signal.close_cad),
            "200-day SMA": None if signal.sma_cad is None else float(signal.sma_cad),
        }
        for signal in signals
    ]
    st.vega_lite_chart(
        {
            "data": {"values": values},
            "mark": {"type": "line", "strokeWidth": 2},
            "encoding": {
                "x": {"field": "date", "type": "temporal", "title": None},
                "y": {
                    "field": "value",
                    "type": "quantitative",
                    "title": "CAD reference price",
                    "scale": {"zero": False},
                },
                "color": {
                    "field": "series",
                    "type": "nominal",
                    "scale": {"range": ["#00d5be", "#8676ff"]},
                    "title": None,
                },
            },
            "transform": [
                {
                    "fold": ["BTC/CAD close", "200-day SMA"],
                    "as": ["series", "value"],
                }
            ],
            "height": 310,
        },
        width="stretch",
    )


def _allocation_chart(signals: tuple[SignalTelemetry, ...]) -> None:
    values = [
        {
            "date": signal.strategy_date.isoformat(),
            "target": float(signal.target_weight * Decimal("100")),
        }
        for signal in signals
    ]
    st.vega_lite_chart(
        {
            "data": {"values": values},
            "mark": {
                "type": "area",
                "line": {"color": "#f5a623"},
                "color": {
                    "x1": 1,
                    "y1": 1,
                    "x2": 1,
                    "y2": 0,
                    "gradient": "linear",
                    "stops": [
                        {"offset": 0, "color": "rgba(245,166,35,.03)"},
                        {"offset": 1, "color": "rgba(245,166,35,.45)"},
                    ],
                },
            },
            "encoding": {
                "x": {"field": "date", "type": "temporal", "title": None},
                "y": {
                    "field": "target",
                    "type": "quantitative",
                    "title": "Target BTC allocation (%)",
                    "scale": {"domain": [0, 100]},
                },
                "tooltip": [
                    {"field": "date", "type": "temporal", "title": "Strategy date"},
                    {"field": "target", "type": "quantitative", "format": ".2f"},
                ],
            },
            "height": 250,
        },
        width="stretch",
    )


def _recent_signal_rows(signals: tuple[SignalTelemetry, ...]) -> list[dict[str, str]]:
    return [
        {
            "Strategy date": signal.strategy_date.isoformat(),
            "Mode": signal.run_mode.upper(),
            "Target": "BTC" if signal.state == "btc" else "Cash",
            "BTC target": _percent(signal.target_weight),
            "Reference close": _cad(signal.close_cad),
            "Momentum": _percent(signal.momentum, signed=True),
            "Reason": signal.reason.replace("_", " "),
        }
        for signal in reversed(signals[-14:])
    ]


st.title("♞ Kraken Knight")
st.caption("BTC/CAD systematic trading telemetry")

try:
    snapshot = _read_snapshot()
except DashboardSnapshotError as exc:
    css_class, banner = _mode_banner(None)
    st.markdown(f'<div class="mode-banner {css_class}">{banner}</div>', unsafe_allow_html=True)
    st.error(str(exc))
    st.info(
        "The dashboard is read-only and will remain unavailable until the sanitized telemetry "
        "exporter publishes its first valid snapshot."
    )
    st.stop()

latest = snapshot.latest_signal
mode = None if latest is None else latest.run_mode
css_class, banner = _mode_banner(mode)
st.markdown(f'<div class="mode-banner {css_class}">{banner}</div>', unsafe_allow_html=True)

age_seconds = (datetime.now(UTC) - snapshot.generated_at_utc).total_seconds()
if age_seconds < -60:
    st.error("Telemetry timestamp is in the future. Treat this dashboard as unhealthy.")
elif age_seconds > STALE_AFTER_SECONDS:
    st.error(
        f"Telemetry is stale: last sanitized export was {_utc_label(snapshot.generated_at_utc)}."
    )

if latest is not None:
    signal_age_seconds = (datetime.now(UTC) - latest.recorded_at_utc).total_seconds()
    if signal_age_seconds < -60:
        st.error("Latest decision timestamp is in the future. Treat the signal as unhealthy.")
    elif signal_age_seconds > SIGNAL_STALE_AFTER_SECONDS:
        st.error(
            "Latest strategy decision is stale: "
            f"recorded {_utc_label(latest.recorded_at_utc)}. "
            "A fresh dashboard export does not prove that the daily strategy job succeeded."
        )

with st.sidebar:
    st.subheader("Telemetry boundary")
    st.write("Sanitized snapshot only")
    st.caption("No Kraken keys · no production config · no database access · no trade controls")
    st.divider()
    st.write("Snapshot generated")
    st.code(_utc_label(snapshot.generated_at_utc), language=None)
    st.write("Ledger integrity")
    st.code(snapshot.health.ledger_integrity.upper(), language=None)
    st.caption("Refresh the page after the daily exporter runs.")

if latest is None:
    st.info("The ledger is healthy, but no strategy decision has been exported yet.")
else:
    metric_columns = st.columns(4)
    metric_columns[0].metric("Target BTC allocation", _percent(latest.target_weight))
    metric_columns[1].metric("BTC/CAD reference close", _cad(latest.close_cad))
    metric_columns[2].metric("90-day momentum", _percent(latest.momentum, signed=True))
    metric_columns[3].metric("Strategy date", latest.strategy_date.isoformat())

    overview_tab, performance_tab, health_tab = st.tabs(
        ["Signal overview", "Performance", "System health"]
    )

    with overview_tab:
        st.subheader("Signal history")
        st.caption(
            "Reference prices and model targets only. These charts are not an account equity "
            "curve and do not include execution, fees, slippage, or cash flows."
        )
        if len(snapshot.signals) < 2:
            st.info("A second daily decision is needed before a time-series line can form.")
        _signal_chart(snapshot.signals)
        st.subheader("Target allocation")
        _allocation_chart(snapshot.signals)
        st.subheader("Recent decisions")
        st.dataframe(
            _recent_signal_rows(snapshot.signals),
            hide_index=True,
            width="stretch",
        )

    with performance_tab:
        st.subheader("Verified account performance")
        st.warning("P&L telemetry is not available yet")
        st.write(
            "The current production ledger records causal strategy decisions, not a verified "
            "account-equity series. Showing BTC price movement or backtest equity here as live "
            "profit would be misleading. This panel will activate only after the live execution "
            "checkpoint emits reconciled CAD equity, fees, fills, and external cash-flow data."
        )
        st.caption(
            "No profitability claim is implied by the current long or cash signal, historical "
            "backtests, or this dashboard."
        )

    with health_tab:
        st.subheader("Operational health")
        health_columns = st.columns(4)
        health_columns[0].metric("Decisions", snapshot.health.decision_count)
        health_columns[1].metric("Order intents", snapshot.health.order_intent_count)
        health_columns[2].metric("Reconciliations", snapshot.health.reconciliation_count)
        health_columns[3].metric("Ledger schema", snapshot.health.ledger_schema_version)
        st.write("Latest reconciliation status")
        if snapshot.health.latest_reconciliation_status is None:
            st.info("No reconciliation snapshot has been exported.")
        else:
            status = snapshot.health.latest_reconciliation_status
            reconciliation_time = _utc_label(snapshot.health.latest_reconciliation_observed_at_utc)
            if status == "CLEAN":
                st.success(f"{status} · {reconciliation_time}")
            else:
                st.error(f"{status} · {reconciliation_time}")
        st.caption(
            f"Latest decision recorded {_utc_label(latest.recorded_at_utc)} · "
            f"strategy `{latest.strategy_id}` · pair `{latest.pair}`"
        )
        st.subheader("Live readiness")
        readiness = [
            ("Sanitized signal monitoring", True),
            ("Verified account-equity and P&L telemetry", False),
            ("Account-aware shadow order planner", False),
            ("Validation-only Kraken order request", False),
            ("Durable order, fill, fee, and recovery lifecycle", False),
        ]
        for label, ready in readiness:
            st.write(f"{'✅' if ready else '⬜'} {label}")
        st.caption(
            "These are engineering promotion gates, not predictions of profitability. "
            "The dashboard cannot waive or operate them."
        )

st.markdown(
    '<div class="footnote">Read-only monitoring surface. It cannot submit, edit, or cancel '
    "Kraken orders. Trading Bitcoin can result in substantial loss.</div>",
    unsafe_allow_html=True,
)
