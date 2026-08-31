"""Runtime configuration with fail-closed live-trading controls.

Configuration is read from the process environment.  systemd is responsible
for loading the production environment file; this module deliberately does not
parse ``.env`` files or log secret-bearing values.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path

from kraken_knight.provenance import sha256_json

ENV_PREFIX = "KRAKEN_KNIGHT_"
LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS"


class ConfigError(ValueError):
    """Raised when runtime configuration violates a safety invariant."""


class RunMode(StrEnum):
    """Recognized broker/runtime modes; Checkpoint 2 still rejects live."""

    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    VALIDATE = "validate"
    LIVE = "live"


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """A small redacting wrapper for credentials.

    Calling :meth:`reveal` must be an explicit boundary action by an adapter.
    Neither ``str`` nor ``repr`` exposes the wrapped value.
    """

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value:
            raise ConfigError("secret values cannot be empty")

    def __repr__(self) -> str:
        return "SecretValue(**redacted**)"

    def __str__(self) -> str:
        return "**redacted**"

    def reveal(self) -> str:
        """Return the credential for a narrowly scoped API adapter."""

        return self._value


@dataclass(frozen=True, slots=True)
class FrozenRiskSettings:
    """Normative BTC/CAD V1 risk values.

    These are intentionally code-frozen instead of environment-tunable.  Any
    change requires a new strategy version and its own research/release review.
    ``__post_init__`` also protects callers that try to override a dataclass
    constructor argument directly.
    """

    target_annual_volatility: Decimal = Decimal("0.25")
    max_exposure_fraction: Decimal = Decimal("0.80")
    cash_reserve_cad: Decimal = Decimal("200")
    min_rebalance_notional_cad: Decimal = Decimal("50")
    min_rebalance_equity_fraction: Decimal = Decimal("0.05")
    rolling_24h_loss_gate_fraction: Decimal = Decimal("0.08")
    high_water_drawdown_fraction: Decimal = Decimal("0.20")
    max_entry_spread_bps: Decimal = Decimal("20")
    risk_exit_price_collar_bps: Decimal = Decimal("50")
    max_price_attempts: int = 3
    allow_margin: bool = False
    allow_short: bool = False

    def __post_init__(self) -> None:
        expected: dict[str, Decimal | int | bool] = {
            "target_annual_volatility": Decimal("0.25"),
            "max_exposure_fraction": Decimal("0.80"),
            "cash_reserve_cad": Decimal("200"),
            "min_rebalance_notional_cad": Decimal("50"),
            "min_rebalance_equity_fraction": Decimal("0.05"),
            "rolling_24h_loss_gate_fraction": Decimal("0.08"),
            "high_water_drawdown_fraction": Decimal("0.20"),
            "max_entry_spread_bps": Decimal("20"),
            "risk_exit_price_collar_bps": Decimal("50"),
            "max_price_attempts": 3,
            "allow_margin": False,
            "allow_short": False,
        }
        for name, required_value in expected.items():
            if getattr(self, name) != required_value:
                raise ConfigError(
                    f"{name} is frozen for btc_cad_daily_momentum_v1; "
                    "create and validate a new strategy version to change it"
                )

    @property
    def fingerprint(self) -> str:
        """Return a deterministic, secret-free risk-policy digest."""

        return sha256_json(
            {
                "allow_margin": self.allow_margin,
                "allow_short": self.allow_short,
                "cash_reserve_cad": self.cash_reserve_cad,
                "high_water_drawdown_fraction": self.high_water_drawdown_fraction,
                "max_entry_spread_bps": self.max_entry_spread_bps,
                "max_exposure_fraction": self.max_exposure_fraction,
                "max_price_attempts": self.max_price_attempts,
                "min_rebalance_equity_fraction": self.min_rebalance_equity_fraction,
                "min_rebalance_notional_cad": self.min_rebalance_notional_cad,
                "risk_exit_price_collar_bps": self.risk_exit_price_collar_bps,
                "rolling_24h_loss_gate_fraction": self.rolling_24h_loss_gate_fraction,
                "target_annual_volatility": self.target_annual_volatility,
            }
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated application settings.

    ``shadow`` is the default even when no environment is present. Checkpoint 2
    parses prospective live fields only so every attempted live configuration
    fails with a specific reason; it rejects live even when they are populated.
    A later durable arm must not treat two values in one environment file as
    independent authority. This class never performs an exchange operation.
    """

    mode: RunMode = RunMode.SHADOW
    state_dir: Path = Path("var")
    account_id: str = "dedicated-btc-cad"
    pair: str = "BTC/CAD"
    strategy_id: str = "btc_cad_daily_momentum_v1"
    risk: FrozenRiskSettings = field(default_factory=FrozenRiskSettings)
    live_trading_enabled: bool = False
    live_trading_confirmation: str = field(default="", repr=False)
    kraken_api_key: SecretValue | None = field(default=None, repr=False)
    kraken_api_secret: SecretValue | None = field(default=None, repr=False)
    expected_kraken_key_name: str | None = field(default=None, repr=False)
    expected_kraken_ip: str | None = field(default=None, repr=False)
    expected_kraken_account_id: str | None = field(default=None, repr=False)
    expected_legacy_manifest_hash: str | None = field(default=None, repr=False)
    expected_funding_manifest_hash: str | None = field(default=None, repr=False)
    cutover_quiesced: bool = False
    legacy_hints_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RunMode):
            raise ConfigError("mode must be a RunMode")
        if not isinstance(self.state_dir, Path):
            raise ConfigError("state_dir must be a pathlib.Path")
        if not self.account_id.strip():
            raise ConfigError("account_id cannot be empty")
        if self.account_id != "dedicated-btc-cad":
            raise ConfigError(
                "Checkpoint 2 freezes account_id; changing the account binding requires review"
            )
        if self.pair != "BTC/CAD":
            raise ConfigError("btc_cad_daily_momentum_v1 is frozen to BTC/CAD")
        if self.strategy_id != "btc_cad_daily_momentum_v1":
            raise ConfigError("unsupported strategy_id")

        confirmation_matches = hmac.compare_digest(
            self.live_trading_confirmation,
            LIVE_CONFIRMATION,
        )
        if (self.kraken_api_key is None) != (self.kraken_api_secret is None):
            raise ConfigError(
                "Kraken authenticated reads require both KRAKEN_API_KEY and KRAKEN_API_SECRET"
            )
        if self.expected_kraken_key_name is not None:
            normalized_name = self.expected_kraken_key_name.strip()
            if not normalized_name:
                raise ConfigError("EXPECTED_KRAKEN_KEY_NAME cannot be blank")
            object.__setattr__(self, "expected_kraken_key_name", normalized_name)
        if self.expected_kraken_ip is not None:
            try:
                normalized_ip = str(ip_address(self.expected_kraken_ip.strip()))
            except ValueError:
                raise ConfigError("EXPECTED_KRAKEN_IP must be one IPv4 or IPv6 address") from None
            object.__setattr__(self, "expected_kraken_ip", normalized_ip)
        if self.expected_kraken_account_id is not None:
            normalized_account_id = self.expected_kraken_account_id.strip().upper()
            if re.fullmatch(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}", normalized_account_id) is None:
                raise ConfigError(
                    "EXPECTED_KRAKEN_ACCOUNT_ID must use Kraken's public wallet-account format"
                )
            object.__setattr__(self, "expected_kraken_account_id", normalized_account_id)
        if self.expected_legacy_manifest_hash is not None:
            normalized_manifest_hash = self.expected_legacy_manifest_hash.strip().lower()
            if len(normalized_manifest_hash) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_manifest_hash
            ):
                raise ConfigError("EXPECTED_LEGACY_MANIFEST_HASH must be a SHA-256 digest")
            object.__setattr__(
                self,
                "expected_legacy_manifest_hash",
                normalized_manifest_hash,
            )
        if self.expected_funding_manifest_hash is not None:
            normalized_funding_hash = self.expected_funding_manifest_hash.strip().lower()
            if len(normalized_funding_hash) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_funding_hash
            ):
                raise ConfigError("EXPECTED_FUNDING_MANIFEST_HASH must be a SHA-256 digest")
            object.__setattr__(
                self,
                "expected_funding_manifest_hash",
                normalized_funding_hash,
            )
        if not isinstance(self.cutover_quiesced, bool):
            raise ConfigError("cutover_quiesced must be a bool")
        if self.legacy_hints_path is not None and not isinstance(self.legacy_hints_path, Path):
            raise ConfigError("legacy_hints_path must be a pathlib.Path")
        if self.kraken_api_key is None and (
            self.expected_kraken_key_name is not None
            or self.expected_kraken_ip is not None
            or self.expected_kraken_account_id is not None
        ):
            raise ConfigError("Kraken account-binding fields require authenticated credentials")
        if self.mode is RunMode.LIVE:
            if not self.live_trading_enabled or not confirmation_matches:
                raise ConfigError(
                    "live mode requires both LIVE_TRADING_ENABLED=true and the exact "
                    "LIVE_TRADING_CONFIRMATION acknowledgement"
                )
            if self.kraken_api_key is None or self.kraken_api_secret is None:
                raise ConfigError("live mode requires Kraken trading credentials")
            raise ConfigError(
                "live mode is unavailable in Checkpoint 2; authenticated access is read-only "
                "and no exchange-write adapter is implemented"
            )
        elif self.live_trading_enabled or self.live_trading_confirmation:
            raise ConfigError("live arming fields may only be set when MODE=live")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from an environment mapping without mutating it."""

        values = os.environ if environ is None else environ
        raw_mode = values.get(f"{ENV_PREFIX}MODE", RunMode.SHADOW.value).strip().lower()
        try:
            mode = RunMode(raw_mode)
        except ValueError as exc:
            supported = ", ".join(item.value for item in RunMode)
            raise ConfigError(f"unsupported mode; expected one of: {supported}") from exc

        raw_state_dir = values.get(f"{ENV_PREFIX}STATE_DIR", "var").strip()
        if not raw_state_dir:
            raise ConfigError("STATE_DIR cannot be empty")

        return cls(
            mode=mode,
            state_dir=Path(raw_state_dir).expanduser(),
            account_id=values.get(f"{ENV_PREFIX}ACCOUNT_ID", "dedicated-btc-cad").strip(),
            pair=values.get(f"{ENV_PREFIX}PAIR", "BTC/CAD").strip().upper(),
            strategy_id=values.get(
                f"{ENV_PREFIX}STRATEGY_ID",
                "btc_cad_daily_momentum_v1",
            ).strip(),
            live_trading_enabled=_parse_bool(
                values.get(f"{ENV_PREFIX}LIVE_TRADING_ENABLED", "false"),
                field_name="LIVE_TRADING_ENABLED",
            ),
            live_trading_confirmation=values.get(
                f"{ENV_PREFIX}LIVE_TRADING_CONFIRMATION",
                "",
            ),
            kraken_api_key=_optional_secret(values.get(f"{ENV_PREFIX}KRAKEN_API_KEY")),
            kraken_api_secret=_optional_secret(values.get(f"{ENV_PREFIX}KRAKEN_API_SECRET")),
            expected_kraken_key_name=_optional_text(
                values.get(f"{ENV_PREFIX}EXPECTED_KRAKEN_KEY_NAME")
            ),
            expected_kraken_ip=_optional_text(values.get(f"{ENV_PREFIX}EXPECTED_KRAKEN_IP")),
            expected_kraken_account_id=_optional_text(
                values.get(f"{ENV_PREFIX}EXPECTED_KRAKEN_ACCOUNT_ID")
            ),
            expected_legacy_manifest_hash=_optional_text(
                values.get(f"{ENV_PREFIX}EXPECTED_LEGACY_MANIFEST_HASH")
            ),
            expected_funding_manifest_hash=_optional_text(
                values.get(f"{ENV_PREFIX}EXPECTED_FUNDING_MANIFEST_HASH")
            ),
            cutover_quiesced=_parse_bool(
                values.get(f"{ENV_PREFIX}CUTOVER_QUIESCED", "false"),
                field_name="CUTOVER_QUIESCED",
            ),
            legacy_hints_path=_optional_path(values.get(f"{ENV_PREFIX}LEGACY_HINTS_PATH")),
        )

    @property
    def ledger_path(self) -> Path:
        """Return the sole mutable decision-ledger path."""

        return self.state_dir / "kraken-knight.sqlite3"

    @property
    def live_armed(self) -> bool:
        """Report validated arm state without exposing acknowledgement text."""

        return self.mode is RunMode.LIVE and self.live_trading_enabled

    def safe_summary(self) -> dict[str, object]:
        """Return operator-visible settings with no credential material."""

        return {
            "account_id": self.account_id,
            "kraken_credentials_configured": (
                self.kraken_api_key is not None and self.kraken_api_secret is not None
            ),
            "kraken_read_binding_configured": (
                self.kraken_api_key is not None
                and self.kraken_api_secret is not None
                and self.expected_kraken_key_name is not None
                and self.expected_kraken_ip is not None
                and self.expected_kraken_account_id is not None
            ),
            "cutover_quiesced": self.cutover_quiesced,
            "legacy_manifest_pinned": self.expected_legacy_manifest_hash is not None,
            "funding_manifest_pinned": self.expected_funding_manifest_hash is not None,
            "ledger_path": str(self.ledger_path),
            "live_armed": self.live_armed,
            "legacy_hints_configured": self.legacy_hints_path is not None,
            "mode": self.mode.value,
            "pair": self.pair,
            "risk_fingerprint": self.risk.fingerprint,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Credentials and storage isolated from the production decision service."""

    state_dir: Path = Path("data")
    blockchair_api_key: SecretValue | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state_dir, Path):
            raise ConfigError("research state_dir must be a pathlib.Path")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ResearchSettings:
        values = os.environ if environ is None else environ
        raw_state_dir = values.get(f"{ENV_PREFIX}RESEARCH_STATE_DIR", "data").strip()
        if not raw_state_dir:
            raise ConfigError("RESEARCH_STATE_DIR cannot be empty")
        return cls(
            state_dir=Path(raw_state_dir).expanduser(),
            blockchair_api_key=_optional_secret(values.get(f"{ENV_PREFIX}BLOCKCHAIR_API_KEY")),
        )

    def safe_summary(self) -> dict[str, object]:
        return {
            "blockchair_credential_configured": self.blockchair_api_key is not None,
            "state_dir": str(self.state_dir),
        }


def _parse_bool(raw_value: str, *, field_name: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{field_name} must be true or false")


def _optional_secret(raw_value: str | None) -> SecretValue | None:
    if raw_value is None or not raw_value.strip():
        return None
    return SecretValue(raw_value.strip())


def _optional_text(raw_value: str | None) -> str | None:
    if raw_value is None or not raw_value.strip():
        return None
    return raw_value.strip()


def _optional_path(raw_value: str | None) -> Path | None:
    if raw_value is None or not raw_value.strip():
        return None
    return Path(raw_value.strip()).expanduser()
