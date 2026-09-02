from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from kraken_knight.config import RunMode
from kraken_knight.dashboard_snapshot import (
    DASHBOARD_SNAPSHOT_SCHEMA,
    DashboardSnapshot,
    DashboardSnapshotError,
    export_dashboard_snapshot,
    load_dashboard_snapshot,
    main,
    write_dashboard_snapshot,
)
from kraken_knight.ledger import Ledger

NOW = datetime(2026, 9, 2, 0, 20, tzinfo=UTC)
DIGEST = "a" * 64
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _ledger(tmp_path: Path, *, details: dict[str, object] | None = None) -> Ledger:
    ledger = Ledger(tmp_path / "state" / "kraken-knight.sqlite3")
    ledger.initialize()
    ledger.append_daily_decision(
        account_id="private-wallet-identifier",
        strategy_id="btc_cad_daily_momentum_v3_no_drawdown",
        strategy_date=date(2026, 9, 1),
        configuration_hash=DIGEST,
        input_data_hash="b" * 64,
        run_mode=RunMode.SHADOW,
        pair="BTC/CAD",
        outcome="TARGET_BTC",
        code_version="daily-shadow-v3+release",
        details=(
            {
                "annualized_volatility": "0.39226",
                "close": "108812",
                "exchange_writes": False,
                "momentum": "0.17819",
                "reason": "long_signal",
                "sma": "96209.9095",
                "state": "btc",
                "target_weight": "0.637328",
            }
            if details is None
            else details
        ),
        recorded_at=NOW,
    )
    return ledger


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_selects_only_sanitized_signal_and_health_fields(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    database_digest = _sha256(ledger.path)

    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)
    payload = snapshot.to_payload()
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert _sha256(ledger.path) == database_digest
    assert payload["schema"] == DASHBOARD_SNAPSHOT_SCHEMA
    assert payload["performance"] == {
        "basis": "verified_account_equity",
        "message": (
            "Verified live account-equity observations are not yet available; "
            "signal prices are not portfolio P&L."
        ),
        "status": "unavailable",
    }
    assert snapshot.latest_signal is not None
    assert snapshot.latest_signal.run_mode == "shadow"
    assert snapshot.latest_signal.target_weight == Decimal("0.637328")
    assert snapshot.health.decision_count == 1
    assert snapshot.health.order_intent_count == 0
    assert "private-wallet-identifier" not in encoded
    assert DIGEST not in encoded
    for prohibited in (
        "account_id",
        "api_key",
        "api_secret",
        "configuration_hash",
        "decision_id",
        "input_data_hash",
        "source_data_hash",
    ):
        assert prohibited not in encoded


def test_empty_initialized_ledger_exports_honest_unavailable_state(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state" / "kraken-knight.sqlite3")
    ledger.initialize()

    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)

    assert snapshot.signals == ()
    assert snapshot.latest_signal is None
    assert snapshot.health.decision_count == 0


def test_snapshot_round_trip_is_atomic_and_mode_0640(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)
    output = tmp_path / "dashboard" / "telemetry.json"
    output.parent.mkdir()

    write_dashboard_snapshot(snapshot, output_path=output, ledger_path=ledger.path)
    loaded = load_dashboard_snapshot(output)

    assert loaded == snapshot
    assert os.stat(output).st_mode & 0o777 == 0o640
    assert list(output.parent.glob(".telemetry.json.*")) == []


def test_exporter_console_command_never_prints_paths_or_private_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = _ledger(tmp_path)
    output = tmp_path / "dashboard" / "telemetry.json"
    output.parent.mkdir()

    assert main(["--ledger", str(ledger.path), "--output", str(output)]) == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["exchange_writes"] is False
    assert summary["performance_status"] == "unavailable"
    assert "private-wallet-identifier" not in captured.out
    assert str(ledger.path) not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not json", "malformed or unreadable"),
        (json.dumps({"schema": "wrong"}), "fields do not match"),
    ],
)
def test_loader_rejects_malformed_snapshots(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    path = tmp_path / "telemetry.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(DashboardSnapshotError, match=message):
        load_dashboard_snapshot(path)


def test_loader_rejects_missing_snapshot(tmp_path: Path) -> None:
    with pytest.raises(DashboardSnapshotError, match="not available"):
        load_dashboard_snapshot(tmp_path / "missing.json")


def test_loader_rejects_symlinked_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "telemetry.json"
    link.symlink_to(target)

    with pytest.raises(DashboardSnapshotError, match="malformed or unreadable"):
        load_dashboard_snapshot(link)


def test_loader_rejects_secret_shaped_value(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)
    payload = snapshot.to_payload()
    performance = payload["performance"]
    assert isinstance(performance, dict)
    performance["message"] = "https://example.test/?api_key=must-not-leak"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DashboardSnapshotError, match="sensitive value"):
        load_dashboard_snapshot(path)


def test_export_rejects_malformed_or_incomplete_stored_details(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, details={})

    with pytest.raises(DashboardSnapshotError, match="stored signal state"):
        export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)


def test_output_cannot_replace_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)

    with pytest.raises(DashboardSnapshotError, match="must not replace"):
        write_dashboard_snapshot(snapshot, output_path=ledger.path, ledger_path=ledger.path)


def test_payload_rejects_latest_signal_that_disagrees_with_history(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)
    payload = snapshot.to_payload()
    latest = payload["latest_signal"]
    assert isinstance(latest, dict)
    latest["target_weight"] = "0"

    with pytest.raises(DashboardSnapshotError, match="does not match"):
        DashboardSnapshot.from_payload(payload)


def test_streamlit_app_renders_shadow_boundary_without_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(ledger_path=ledger.path, generated_at=NOW)
    output = tmp_path / "dashboard" / "telemetry.json"
    output.parent.mkdir()
    write_dashboard_snapshot(snapshot, output_path=output, ledger_path=ledger.path)
    monkeypatch.setenv("KRAKEN_KNIGHT_DASHBOARD_SNAPSHOT", str(output))

    app = AppTest.from_file(REPOSITORY_ROOT / "dashboard" / "app.py", default_timeout=30)
    app.run()

    assert list(app.exception) == []
    markdown = "\n".join(str(element.value) for element in app.markdown)
    assert "SHADOW" in markdown
    assert "NO LIVE ORDERS" in markdown
    assert "Read-only monitoring surface" in markdown
    assert any("P&L telemetry is not available" in str(item.value) for item in app.warning)
    assert list(app.button) == []
    assert list(app.download_button) == []


def test_streamlit_app_does_not_mask_a_stale_decision_with_a_fresh_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    snapshot = export_dashboard_snapshot(
        ledger_path=ledger.path,
        generated_at=datetime.now(UTC),
    )
    assert snapshot.latest_signal is not None
    stale_signal = replace(
        snapshot.latest_signal,
        recorded_at_utc=datetime.now(UTC) - timedelta(days=3),
    )
    stale_snapshot = replace(snapshot, signals=(stale_signal,))
    output = tmp_path / "dashboard" / "telemetry.json"
    output.parent.mkdir()
    write_dashboard_snapshot(stale_snapshot, output_path=output, ledger_path=ledger.path)
    monkeypatch.setenv("KRAKEN_KNIGHT_DASHBOARD_SNAPSHOT", str(output))

    app = AppTest.from_file(REPOSITORY_ROOT / "dashboard" / "app.py", default_timeout=30)
    app.run()

    assert list(app.exception) == []
    assert any("Latest strategy decision is stale" in str(item.value) for item in app.error)
