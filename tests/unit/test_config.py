from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.cli import main
from kraken_knight.config import (
    LIVE_CONFIRMATION,
    ConfigError,
    FrozenRiskSettings,
    ResearchSettings,
    RunMode,
    Settings,
)


def test_default_mode_is_shadow_and_uses_local_state() -> None:
    settings = Settings.from_env({})

    assert settings.mode is RunMode.SHADOW
    assert settings.live_armed is False
    assert settings.ledger_path == Path("var/kraken-knight.sqlite3")
    assert settings.pair == "BTC/CAD"


@pytest.mark.parametrize("mode", ["backtest", "paper", "shadow", "validate"])
def test_non_live_modes_are_supported_without_credentials(mode: str) -> None:
    settings = Settings.from_env({"KRAKEN_KNIGHT_MODE": mode})

    assert settings.mode.value == mode
    assert settings.live_armed is False


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported mode"):
        Settings.from_env({"KRAKEN_KNIGHT_MODE": "production"})


@pytest.mark.parametrize(
    ("enabled", "confirmation"),
    [
        ("false", ""),
        ("true", ""),
        ("false", LIVE_CONFIRMATION),
        ("true", "close-enough"),
    ],
)
def test_live_mode_requires_both_exact_arms(enabled: str, confirmation: str) -> None:
    environment = {
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "private-material",
        "KRAKEN_KNIGHT_LIVE_TRADING_CONFIRMATION": confirmation,
        "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": enabled,
        "KRAKEN_KNIGHT_MODE": "live",
    }

    with pytest.raises(ConfigError, match="requires both"):
        Settings.from_env(environment)


def test_live_mode_requires_credentials_after_double_arm() -> None:
    environment = {
        "KRAKEN_KNIGHT_LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION,
        "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "true",
        "KRAKEN_KNIGHT_MODE": "live",
    }

    with pytest.raises(ConfigError, match="requires Kraken trading credentials"):
        Settings.from_env(environment)


def test_checkpoint_one_rejects_live_even_after_environment_arms() -> None:
    environment = {
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "private-material",
        "KRAKEN_KNIGHT_LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION,
        "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "true",
        "KRAKEN_KNIGHT_MODE": "live",
    }

    with pytest.raises(ConfigError, match="unavailable in Checkpoint 1"):
        Settings.from_env(environment)


def test_live_arms_are_rejected_outside_live_mode() -> None:
    with pytest.raises(ConfigError, match="may only be set"):
        Settings.from_env(
            {
                "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "true",
                "KRAKEN_KNIGHT_MODE": "shadow",
            }
        )


def test_account_alias_cannot_bypass_the_checkpoint_one_identity_boundary() -> None:
    with pytest.raises(ConfigError, match="freezes account_id"):
        Settings.from_env({"KRAKEN_KNIGHT_ACCOUNT_ID": "same-account-new-label"})


def test_secret_values_are_redacted_from_repr_and_safe_summary() -> None:
    secret = "must-never-be-printed"
    settings = Settings.from_env(
        {
            "KRAKEN_KNIGHT_KRAKEN_API_KEY": secret,
            "KRAKEN_KNIGHT_KRAKEN_API_SECRET": secret,
        }
    )

    assert secret not in repr(settings)
    assert secret not in repr(settings.kraken_api_secret)
    assert secret not in json.dumps(settings.safe_summary())
    assert settings.kraken_api_secret is not None
    assert settings.kraken_api_secret.reveal() == secret


def test_research_secret_is_redacted_and_isolated_from_runtime_settings() -> None:
    secret = "blockchair-secret-must-never-be-printed"
    environment = {"KRAKEN_KNIGHT_BLOCKCHAIR_API_KEY": secret}

    runtime = Settings.from_env(environment)
    research = ResearchSettings.from_env(environment)

    assert "blockchair" not in json.dumps(runtime.safe_summary()).lower()
    assert secret not in repr(research)
    assert secret not in json.dumps(research.safe_summary())
    assert research.blockchair_api_key is not None
    assert research.blockchair_api_key.reveal() == secret


def test_frozen_risk_settings_are_immutable_and_have_a_stable_hash() -> None:
    risk = FrozenRiskSettings()
    second = FrozenRiskSettings()

    assert risk.fingerprint == second.fingerprint
    assert len(risk.fingerprint) == 64
    with pytest.raises(FrozenInstanceError):
        risk.max_exposure_fraction = Decimal("0.75")  # type: ignore[misc]


def test_frozen_risk_override_requires_a_new_strategy_version() -> None:
    with pytest.raises(ConfigError, match="frozen"):
        FrozenRiskSettings(max_exposure_fraction=Decimal("0.75"))


def test_boolean_arming_parser_is_strict() -> None:
    with pytest.raises(ConfigError, match="must be true or false"):
        Settings.from_env({"KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "maybe"})


def test_cli_initializes_and_reports_local_only_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {
        "KRAKEN_KNIGHT_MODE": "shadow",
        "KRAKEN_KNIGHT_STATE_DIR": str(tmp_path / "state"),
    }

    assert main(["init", "--json"], environ=environment) == 0
    initialized_payload = json.loads(capsys.readouterr().out)
    assert initialized_payload["exchange_writes"] is False
    assert initialized_payload["ledger"]["journal_mode"] == "wal"

    assert main(["status", "--json"], environ=environment) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["operation"] == "status"
    assert status_payload["ledger"]["initialized"] is True


def test_cli_never_emits_configured_secret_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "do-not-log-this-value"
    environment = {
        "KRAKEN_KNIGHT_BLOCKCHAIR_API_KEY": secret,
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": secret,
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": secret,
        "KRAKEN_KNIGHT_STATE_DIR": str(tmp_path / "state"),
    }

    assert main(["init"], environ=environment) == 0

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
