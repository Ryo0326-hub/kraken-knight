from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.cli import main
from kraken_knight.config import ConfigError, FrozenRiskSettings, RunMode, Settings
from kraken_knight.daily_job import DailyJobError, execute_daily_decision, release_id_from_env
from kraken_knight.domain import Candle
from kraken_knight.ledger import Ledger, LedgerConflict
from kraken_knight.market_data import KrakenOhlcBatch, MarketDataError
from kraken_knight.risk import DrawdownPolicyMode

NOW = datetime(2026, 9, 1, 0, 15, tzinfo=UTC)
RELEASE_ID = "a" * 40


class StaticPublicClient:
    def __init__(self, batch: KrakenOhlcBatch) -> None:
        self.batch = batch
        self.requested_pairs: list[str] = []

    def fetch_daily_ohlc(self, *, pair: str = "XBTCAD") -> KrakenOhlcBatch:
        self.requested_pairs.append(pair)
        return self.batch


class BrokenPublicClient:
    def fetch_daily_ohlc(self, *, pair: str = "XBTCAD") -> KrakenOhlcBatch:
        raise MarketDataError("malformed public response")


def _candle(open_time: datetime, close: Decimal, *, complete: bool = True) -> Candle:
    return Candle(
        open_time=open_time,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("10"),
        complete=complete,
    )


def _batch(
    *,
    direction: str = "up",
    count: int = 200,
    observed_at: datetime = NOW,
) -> KrakenOhlcBatch:
    first_open = NOW.replace(hour=0, minute=0) - timedelta(days=count)
    if direction == "up":
        closes = [Decimal(100 + index) for index in range(count)]
    elif direction == "down":
        closes = [Decimal(500 - index) for index in range(count)]
    else:
        raise ValueError("unsupported fixture direction")
    completed = tuple(
        _candle(first_open + timedelta(days=index), close) for index, close in enumerate(closes)
    )
    tail_open = first_open + timedelta(days=count)
    return KrakenOhlcBatch(
        completed=completed,
        mutable_tail=_candle(tail_open, closes[-1] + Decimal("2"), complete=False),
        raw_pair_key="XXBTZCAD",
        last_cursor=int(tail_open.timestamp()),
        observed_at=observed_at,
        raw_sha256="b" * 64,
        requested_pair="XBTCAD",
    )


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "state" / "kraken-knight.sqlite3")
    ledger.initialize()
    return ledger


def _run(
    tmp_path: Path,
    *,
    batch: KrakenOhlcBatch | None = None,
    settings: Settings | None = None,
    ledger: Ledger | None = None,
    release_id: str = RELEASE_ID,
    now: datetime = NOW,
) -> tuple[dict[str, object], Ledger, StaticPublicClient]:
    selected_ledger = ledger or _ledger(tmp_path)
    client = StaticPublicClient(batch or _batch())
    releases_root = tmp_path / "releases"
    release_path = releases_root / release_id
    release_path.mkdir(parents=True, exist_ok=True)
    payload = execute_daily_decision(
        settings=settings or Settings(mode=RunMode.SHADOW),
        ledger=selected_ledger,
        release_id=release_id,
        release_path=release_path,
        releases_root=releases_root,
        public_client=client,
        clock=lambda: now,
    )
    return payload, selected_ledger, client


def test_daily_shadow_records_long_target_without_an_order_intent(tmp_path: Path) -> None:
    payload, ledger, client = _run(tmp_path)

    assert client.requested_pairs == ["XBTCAD"]
    assert payload["outcome"] == "TARGET_BTC"
    assert payload["state"] == "btc"
    assert Decimal(str(payload["target_weight"])) > 0
    assert payload["exchange_writes"] is False
    assert payload["order_intent_created"] is False
    assert payload["drawdown_policy_mode"] == "disabled"
    assert payload["market_data"] == {
        "completed_candle_count": 200,
        "latest_completed_open_time": "2026-08-31T00:00:00+00:00",
        "mutable_tail_quarantined": True,
        "observed_at": "2026-09-01T00:15:00+00:00",
        "pair": "BTC/CAD",
    }
    assert ledger.status()["decision_count"] == 1
    assert ledger.status()["intent_count"] == 0

    with sqlite3.connect(ledger.path) as connection:
        details = json.loads(
            str(connection.execute("SELECT details_json FROM daily_decisions").fetchone()[0])
        )
    assert details["reason"] == "long_signal"
    assert details["drawdown_policy_mode"] == "disabled"
    assert details["exchange_writes"] is False
    assert details["order_intent_created"] is False
    assert Decimal(details["momentum"]) > 0
    assert Decimal(details["close"]) > Decimal(details["sma"])


def test_daily_shadow_records_causal_cash_target(tmp_path: Path) -> None:
    payload, ledger, _ = _run(tmp_path, batch=_batch(direction="down"))

    assert payload["outcome"] == "TARGET_CASH"
    assert payload["state"] == "cash"
    assert payload["target_weight"] == "0"
    assert payload["reason"] == "non_positive_momentum"
    assert ledger.status()["decision_count"] == 1
    assert ledger.status()["intent_count"] == 0


@pytest.mark.parametrize("mode", [RunMode.PAPER, RunMode.SHADOW, RunMode.VALIDATE])
def test_daily_decision_accepts_only_bounded_operational_modes(
    tmp_path: Path,
    mode: RunMode,
) -> None:
    payload, ledger, _ = _run(tmp_path, settings=Settings(mode=mode))

    assert payload["exchange_writes"] is False
    assert payload["order_intent_created"] is False
    assert ledger.status()["decision_count"] == 1
    assert ledger.status()["intent_count"] == 0


@pytest.mark.parametrize(
    ("batch", "now", "message"),
    [
        (_batch(observed_at=NOW - timedelta(minutes=6)), NOW, "observation is stale"),
        (_batch(observed_at=NOW + timedelta(seconds=1)), NOW, "future"),
        (_batch(), NOW - timedelta(minutes=1), "future"),
    ],
)
def test_daily_shadow_rejects_stale_or_future_data_without_recording(
    tmp_path: Path,
    batch: KrakenOhlcBatch,
    now: datetime,
    message: str,
) -> None:
    ledger = _ledger(tmp_path)

    with pytest.raises(MarketDataError, match=message):
        _run(tmp_path, batch=batch, ledger=ledger, now=now)

    assert ledger.status()["decision_count"] == 0


def test_daily_shadow_rejects_incomplete_completed_history(tmp_path: Path) -> None:
    source = _batch()
    incomplete = replace(source.completed[-2], complete=False)
    batch = replace(source, completed=(*source.completed[:-2], incomplete, source.completed[-1]))
    ledger = _ledger(tmp_path)

    with pytest.raises(MarketDataError, match="contains an incomplete"):
        _run(tmp_path, batch=batch, ledger=ledger)

    assert ledger.status()["decision_count"] == 0


def test_daily_shadow_rejects_unquarantined_mutable_tail(tmp_path: Path) -> None:
    source = _batch()
    batch = replace(source, mutable_tail=replace(source.mutable_tail, complete=True))

    with pytest.raises(MarketDataError, match="not quarantined"):
        _run(tmp_path, batch=batch)


@pytest.mark.parametrize(
    "batch",
    [
        replace(_batch(), requested_pair="XETHCAD"),
        replace(_batch(), raw_pair_key="XETHZCAD"),
    ],
)
def test_daily_shadow_rejects_market_pair_metadata_mismatch(
    tmp_path: Path,
    batch: KrakenOhlcBatch,
) -> None:
    with pytest.raises(MarketDataError, match="pair"):
        _run(tmp_path, batch=batch)


def test_daily_shadow_rejects_gap_and_insufficient_history(tmp_path: Path) -> None:
    source = _batch()
    gapped = replace(
        source,
        completed=(source.completed[0], *source.completed[2:]),
    )
    with pytest.raises(MarketDataError, match="gap"):
        _run(tmp_path / "gap", batch=gapped)

    with pytest.raises(MarketDataError, match="at least 200"):
        _run(tmp_path / "short", batch=_batch(count=199))


def test_daily_shadow_propagates_malformed_public_response_without_recording(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    releases_root = tmp_path / "releases"
    release_path = releases_root / RELEASE_ID
    release_path.mkdir(parents=True)

    with pytest.raises(MarketDataError, match="malformed public response"):
        execute_daily_decision(
            settings=Settings(mode=RunMode.SHADOW),
            ledger=ledger,
            release_id=RELEASE_ID,
            release_path=release_path,
            releases_root=releases_root,
            public_client=BrokenPublicClient(),
            clock=lambda: NOW,
        )

    assert ledger.status()["decision_count"] == 0


def test_same_day_retry_ignores_mutable_tail_and_is_idempotent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first_batch = _batch()
    first, _, _ = _run(tmp_path, batch=first_batch, ledger=ledger)
    changed_tail = replace(
        first_batch,
        mutable_tail=_candle(
            first_batch.mutable_tail.open_time,
            first_batch.mutable_tail.close + Decimal("25"),
            complete=False,
        ),
        observed_at=NOW + timedelta(minutes=1),
        raw_sha256="c" * 64,
    )
    second, _, _ = _run(
        tmp_path,
        batch=changed_tail,
        ledger=ledger,
        now=NOW + timedelta(minutes=1),
    )

    assert first["decision_id"] == second["decision_id"]
    assert first["configuration_hash"] == second["configuration_hash"]
    assert first["input_data_hash"] == second["input_data_hash"]
    assert ledger.status()["decision_count"] == 1
    assert ledger.status()["intent_count"] == 0


def test_same_day_changed_completed_data_conflicts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first_batch = _batch()
    _run(tmp_path, batch=first_batch, ledger=ledger)
    revised_first = replace(
        first_batch.completed[0],
        high=first_batch.completed[0].high + Decimal("1"),
    )
    revised = replace(first_batch, completed=(revised_first, *first_batch.completed[1:]))

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        _run(tmp_path, batch=revised, ledger=ledger)

    assert ledger.status()["decision_count"] == 1


def test_same_day_changed_release_conflicts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _run(tmp_path, ledger=ledger)

    with pytest.raises(LedgerConflict, match="different immutable decision"):
        _run(tmp_path, ledger=ledger, release_id="b" * 40)

    assert ledger.status()["decision_count"] == 1


@pytest.mark.parametrize("mode", [RunMode.BACKTEST, RunMode.LIVE])
def test_daily_shadow_rejects_non_operational_modes(tmp_path: Path, mode: RunMode) -> None:
    settings = object.__new__(Settings)
    baseline = Settings(mode=RunMode.SHADOW)
    for field_name in baseline.__dataclass_fields__:
        object.__setattr__(settings, field_name, getattr(baseline, field_name))
    object.__setattr__(settings, "mode", mode)

    with pytest.raises(DailyJobError, match="requires one of"):
        _run(tmp_path, settings=settings)


def test_daily_shadow_defensively_requires_disabled_drawdown_policy(tmp_path: Path) -> None:
    risk = FrozenRiskSettings()
    object.__setattr__(risk, "drawdown_policy_mode", DrawdownPolicyMode.PERSISTENT)
    settings = Settings(mode=RunMode.SHADOW, risk=risk)

    with pytest.raises(ConfigError, match="drawdown_policy_mode=disabled"):
        _run(tmp_path, settings=settings)


def test_daily_json_is_redacted_and_records_no_intent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    environment = {
        "KRAKEN_KNIGHT_MODE": "shadow",
        "KRAKEN_KNIGHT_RELEASE_ID": RELEASE_ID,
        "KRAKEN_KNIGHT_STATE_DIR": str(state_dir),
    }
    releases_root = tmp_path / "releases"
    release_path = releases_root / RELEASE_ID
    release_path.mkdir(parents=True)

    assert (
        main(
            ["daily", "--json"],
            environ=environment,
            daily_client=StaticPublicClient(_batch()),
            daily_clock=lambda: NOW,
            daily_release_path=release_path,
            daily_releases_root=releases_root,
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["operation"] == "daily"
    assert payload["exchange_writes"] is False
    assert payload["order_intent_created"] is False


def test_daily_rejects_configured_credentials_without_fetching_or_storing_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "never-print-or-store-this"
    state_dir = tmp_path / "state"
    releases_root = tmp_path / "releases"
    release_path = releases_root / RELEASE_ID
    release_path.mkdir(parents=True)
    client = StaticPublicClient(_batch())
    environment = {
        "KRAKEN_KNIGHT_KRAKEN_API_KEY": secret,
        "KRAKEN_KNIGHT_KRAKEN_API_SECRET": secret,
        "KRAKEN_KNIGHT_MODE": "shadow",
        "KRAKEN_KNIGHT_RELEASE_ID": RELEASE_ID,
        "KRAKEN_KNIGHT_STATE_DIR": str(state_dir),
    }

    assert (
        main(
            ["daily", "--json"],
            environ=environment,
            daily_client=client,
            daily_clock=lambda: NOW,
            daily_release_path=release_path,
            daily_releases_root=releases_root,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rejects unsupported KRAKEN_KNIGHT_*" in captured.err
    assert "KRAKEN_KNIGHT_KRAKEN_API_KEY" in captured.err
    assert "KRAKEN_KNIGHT_KRAKEN_API_SECRET" in captured.err
    assert secret not in captured.err
    assert client.requested_pairs == []
    assert not state_dir.exists()


@pytest.mark.parametrize(
    "unexpected_name",
    [
        "KRAKEN_KNIGHT_BLOCKCHAIR_API_KEY",
        "KRAKEN_KNIGHT_EXPECTED_KRAKEN_ACCOUNT_ID",
        "KRAKEN_KNIGHT_LIVE_TRADING_CONFIRMATION",
        "KRAKEN_KNIGHT_TELEGRAM_BOT_TOKEN",
    ],
)
def test_daily_rejects_every_unexpected_application_variable_before_state_or_fetch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unexpected_name: str,
) -> None:
    state_dir = tmp_path / "state"
    client = StaticPublicClient(_batch())

    assert (
        main(
            ["daily", "--json"],
            environ={
                "KRAKEN_KNIGHT_RELEASE_ID": RELEASE_ID,
                "KRAKEN_KNIGHT_STATE_DIR": str(state_dir),
                unexpected_name: "must-not-enter-the-daily-process",
            },
            daily_client=client,
            daily_clock=lambda: NOW,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert "rejects unsupported KRAKEN_KNIGHT_*" in captured.err
    assert unexpected_name in captured.err
    assert "must-not-enter-the-daily-process" not in captured.err
    assert captured.out == ""
    assert client.requested_pairs == []
    assert not state_dir.exists()


def test_release_identity_must_match_resolved_release_directory(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    releases_root = tmp_path / "releases"
    wrong_path = releases_root / ("b" * 40)
    wrong_path.mkdir(parents=True)

    with pytest.raises(DailyJobError, match="does not match"):
        execute_daily_decision(
            settings=Settings(mode=RunMode.SHADOW),
            ledger=ledger,
            release_id=RELEASE_ID,
            release_path=wrong_path,
            releases_root=releases_root,
            public_client=StaticPublicClient(_batch()),
            clock=lambda: NOW,
        )

    assert ledger.status()["decision_count"] == 0


def test_cli_release_preflight_happens_before_ledger_initialization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    releases_root = tmp_path / "releases"
    wrong_path = releases_root / ("b" * 40)
    wrong_path.mkdir(parents=True)

    assert (
        main(
            ["daily", "--json"],
            environ={
                "KRAKEN_KNIGHT_RELEASE_ID": RELEASE_ID,
                "KRAKEN_KNIGHT_STATE_DIR": str(state_dir),
            },
            daily_client=StaticPublicClient(_batch()),
            daily_clock=lambda: NOW,
            daily_release_path=wrong_path,
            daily_releases_root=releases_root,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert "does not match" in captured.err
    assert captured.out == ""
    assert not state_dir.exists()


def test_release_identity_is_required_and_format_checked() -> None:
    with pytest.raises(DailyJobError, match="required"):
        release_id_from_env({})
    with pytest.raises(DailyJobError, match="full lowercase Git commit SHA"):
        release_id_from_env({"KRAKEN_KNIGHT_RELEASE_ID": "bad value with spaces"})
    with pytest.raises(DailyJobError, match="full lowercase Git commit SHA"):
        release_id_from_env({"KRAKEN_KNIGHT_RELEASE_ID": "A" * 40})
    assert release_id_from_env({"KRAKEN_KNIGHT_RELEASE_ID": RELEASE_ID}) == RELEASE_ID
