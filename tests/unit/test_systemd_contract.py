from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPOSITORY_ROOT / "deploy" / "systemd"


def test_daily_unit_is_shadow_only_and_cannot_receive_reconciliation_secrets() -> None:
    unit = (SYSTEMD_DIR / "kraken-knight.service").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/kraken-knight/daily.env" in unit
    assert "UnsetEnvironment=KRAKEN_KNIGHT_MODE" in unit
    assert "InaccessiblePaths=/etc/kraken-knight/config.env" in unit
    assert "ExecStart=/opt/kraken-knight/current/.venv/bin/kraken-knight daily --json" in unit
    assert "EnvironmentFile=/etc/kraken-knight/config.env" not in unit


def test_daily_environment_template_contains_no_private_credential_fields() -> None:
    template = (SYSTEMD_DIR / "kraken-knight-daily.env.example").read_text(encoding="utf-8")
    assignments = {
        line.split("=", 1)[0]
        for line in template.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert assignments == {
        "KRAKEN_KNIGHT_ACCOUNT_ID",
        "KRAKEN_KNIGHT_PAIR",
        "KRAKEN_KNIGHT_RELEASE_ID",
        "KRAKEN_KNIGHT_STATE_DIR",
        "KRAKEN_KNIGHT_STRATEGY_ID",
    }


def test_reconciliation_unit_retains_its_separate_authenticated_environment() -> None:
    unit = (SYSTEMD_DIR / "kraken-knight-reconcile.service").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/kraken-knight/config.env" in unit
    assert "EnvironmentFile=/etc/kraken-knight/daily.env" not in unit
