from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPOSITORY_ROOT / "deploy" / "systemd"


def test_streamlit_is_localhost_only_and_cannot_reach_trading_state() -> None:
    unit = (SYSTEMD_DIR / "kraken-knight-dashboard.service").read_text(encoding="utf-8")

    assert "ConditionFileNotEmpty=/var/lib/kraken-knight-dashboard/telemetry.json" in unit
    assert "ConditionPathIsRegular=" not in unit
    assert "--server.address=127.0.0.1" in unit
    assert "--server.port=8501" in unit
    assert "--client.toolbarMode=viewer" in unit
    assert "--client.showErrorDetails=none" in unit
    assert "KRAKEN_KNIGHT_DASHBOARD_SNAPSHOT=" in unit
    assert (
        "InaccessiblePaths=/etc/kraken-knight /var/lib/kraken-knight "
        "/var/backups/kraken-knight" in unit
    )
    assert "EnvironmentFile=" not in unit
    assert "config.env" not in unit
    assert "kraken-knight.sqlite3" not in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "SocketBindAllow=ipv4:tcp:8501" in unit


def test_exporter_is_networkless_read_only_and_writes_only_dashboard_state() -> None:
    unit = (SYSTEMD_DIR / "kraken-knight-dashboard-export.service").read_text(encoding="utf-8")

    assert "ConditionPathExists=/var/lib/kraken-knight/kraken-knight.sqlite3" in unit
    assert "ConditionPathIsRegular=" not in unit
    assert "PrivateNetwork=yes" in unit
    assert "ReadOnlyPaths=/var/lib/kraken-knight" in unit
    assert "ReadWritePaths=/var/lib/kraken-knight-dashboard" in unit
    assert "InaccessiblePaths=/etc/kraken-knight /var/backups/kraken-knight" in unit
    assert "kraken-knight-dashboard-export --ledger" in unit
    assert "Group=kraken-knight-dashboard" in unit
    assert "EnvironmentFile=" not in unit
    assert "config.env" not in unit


def test_streamlit_source_has_no_database_or_exchange_adapter() -> None:
    source = (REPOSITORY_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")

    assert "sqlite3" not in source
    assert "KrakenPublicClient" not in source
    assert "KrakenRead" not in source
    assert "add_order" not in source.lower()
    assert "cancel_order" not in source.lower()
