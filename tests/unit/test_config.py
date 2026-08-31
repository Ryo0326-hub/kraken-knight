from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

import kraken_knight.cli as cli_module
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


def test_checkpoint_two_rejects_live_even_after_environment_arms() -> None:
    environment = {
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "private-material",
        "KRAKEN_KNIGHT_LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION,
        "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "true",
        "KRAKEN_KNIGHT_MODE": "live",
    }

    with pytest.raises(ConfigError, match="unavailable in Checkpoint 2"):
        Settings.from_env(environment)


def test_live_arms_are_rejected_outside_live_mode() -> None:
    with pytest.raises(ConfigError, match="may only be set"):
        Settings.from_env(
            {
                "KRAKEN_KNIGHT_LIVE_TRADING_ENABLED": "true",
                "KRAKEN_KNIGHT_MODE": "shadow",
            }
        )


def test_account_alias_cannot_bypass_the_checkpoint_two_identity_boundary() -> None:
    with pytest.raises(ConfigError, match="freezes account_id"):
        Settings.from_env({"KRAKEN_KNIGHT_ACCOUNT_ID": "same-account-new-label"})


@pytest.mark.parametrize(
    "environment",
    [
        {"KRAKEN_KNIGHT_KRAKEN_API_KEY": "identifier-only"},
        {"KRAKEN_KNIGHT_KRAKEN_API_SECRET": "secret-only"},
    ],
)
def test_authenticated_read_credentials_must_be_configured_as_a_pair(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigError, match="require both"):
        Settings.from_env(environment)


def test_read_only_account_binding_is_normalized_and_not_disclosed() -> None:
    settings = Settings.from_env(
        {
            "KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP": "2001:0db8::1",
            "KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME": "  kraken-knight-read  ",
            "KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID": "wx6v-jukw-kkpb-qe36",
            "KRAKEN_KNIGHT_EXPECTED_LEGACY_MANIFEST_HASH": "a" * 64,
            "KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH": "b" * 64,
            "KRAKEN_KNIGHT_CUTOVER_QUIESCED": "true",
            "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
            "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "private-material",
            "KRAKEN_KNIGHT_LEGACY_HINTS_PATH": "/restricted/legacy-hints.json",
        }
    )

    assert settings.expected_kraken_ip == "2001:db8::1"
    assert settings.expected_kraken_key_name == "kraken-knight-read"
    assert settings.expected_kraken_account_id == "WX6V-JUKW-KKPB-QE36"
    assert settings.cutover_quiesced is True
    summary = settings.safe_summary()
    assert summary["kraken_read_binding_configured"] is True
    assert summary["legacy_hints_configured"] is True
    assert summary["legacy_manifest_pinned"] is True
    assert summary["funding_manifest_pinned"] is True
    assert "2001:db8::1" not in json.dumps(summary)
    assert "kraken-knight-read" not in json.dumps(summary)
    assert "/restricted/legacy-hints.json" not in json.dumps(summary)
    assert "WX6V-JUKW-KKPB-QE36" not in json.dumps(summary)


def test_invalid_or_unbound_read_only_account_binding_is_rejected() -> None:
    with pytest.raises(ConfigError, match="IPv4 or IPv6"):
        Settings.from_env(
            {
                "KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP": "not-an-ip",
                "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
                "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "private-material",
            }
        )
    with pytest.raises(ConfigError, match="require authenticated credentials"):
        Settings.from_env({"KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME": "kraken-knight-read"})
    with pytest.raises(ConfigError, match="EXPECTED_FUNDING_MANIFEST_HASH"):
        Settings.from_env({"KRAKEN_KNIGHT_EXPECTED_FUNDING_MANIFEST_HASH": "not-a-digest"})


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
    assert initialized_payload["ledger"]["schema_version"] == 3

    assert main(["status", "--json"], environ=environment) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["operation"] == "status"
    assert status_payload["ledger"]["initialized"] is True
    assert status_payload["ledger"]["reconciliation_count"] == 0


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


def test_cli_reconcile_requires_authenticated_read_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {"KRAKEN_KNIGHT_STATE_DIR": str(tmp_path / "state")}

    assert main(["reconcile", "--json"], environ=environment) == 2

    captured = capsys.readouterr()
    assert "credentials are not configured" in captured.err
    assert captured.out == ""


def test_cli_builds_normalized_legacy_manifest_without_exchange_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hints_path = tmp_path / "legacy-hints.json"
    hints_path.write_text(
        json.dumps(
            [
                {
                    "hint_id": f"legacy-{index}",
                    "pair": "BTC/CAD",
                    "side": "buy",
                    "quantity_btc": "0.00100000",
                    "limit_price_cad": "100000.0",
                    "window_start": f"2026-08-{index + 1:02d}T00:00:00Z",
                    "window_end": f"2026-08-{index + 1:02d}T00:01:00Z",
                    "order_id": f"ORDER-{index}",
                    "client_order_id": f"legacy-client-{index}",
                }
                for index in range(5)
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            ["legacy-manifest", "--json", "--legacy-hints", str(hints_path)],
            environ={},
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "legacy-manifest"
    assert payload["exchange_writes"] is False
    assert payload["legacy_hint_count"] == 5
    assert len(payload["legacy_manifest_hash"]) == 64


def test_cli_account_id_bootstrap_is_explicit_and_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP": "203.0.113.10",
        "KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME": "kraken-knight-read",
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "c2VjcmV0",
        "KRAKEN_KNIGHT_STATE_DIR": str(tmp_path / "state"),
    }

    def fake_discovery(**_: object) -> dict[str, object]:
        return {
            "exchange_writes": False,
            "observed_at": "2026-08-31T00:00:00Z",
            "private_request_cost_spent": 2,
            "read_only_profile_verified": True,
            "wallet_account_id": "WX6V-JUKW-KKPB-QE36",
        }

    monkeypatch.setattr(cli_module, "discover_read_only_account_id", fake_discovery)

    assert main(["account-id", "--json"], environ=environment) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "account-id"
    assert payload["exchange_writes"] is False
    assert payload["read_only_profile_verified"] is True
    assert payload["wallet_account_id"] == "WX6V-JUKW-KKPB-QE36"


def test_cli_reconcile_returns_a_distinct_nonclean_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "KRAKEN_KNIGHT_EXPECTED_KRAKEN_IP": "203.0.113.10",
        "KRAKEN_KNIGHT_EXPECTED_KRAKEN_KEY_NAME": "kraken-knight-read",
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": "public-identifier",
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": "c2VjcmV0",
        "KRAKEN_KNIGHT_STATE_DIR": str(tmp_path / "state"),
    }

    def fake_reconcile(**_: object) -> dict[str, object]:
        return {
            "account_binding_hash": "a" * 64,
            "exchange_writes": False,
            "ledger_snapshot_id": "reconciliation_" + ("b" * 64),
            "legacy_hint_count": 0,
            "observed_at": "2026-08-31T00:00:00+00:00",
            "private_request_cost_spent": 1,
            "source_data_hash": "c" * 64,
            "status": "UNRESOLVED",
        }

    monkeypatch.setattr(cli_module, "execute_read_only_reconciliation", fake_reconcile)

    assert main(["reconcile", "--json"], environ=environment) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNRESOLVED"
    assert payload["exchange_writes"] is False
