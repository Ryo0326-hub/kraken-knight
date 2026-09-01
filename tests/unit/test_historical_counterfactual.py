from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.backtest import RiskEventType, run_backtest
from kraken_knight.historical_counterfactual import (
    COUNTERFACTUAL_STUDY_SCHEMA,
    HistoricalCounterfactualError,
    run_historical_counterfactual,
)
from kraken_knight.historical_data import DATASET_SCHEMA, DailyTradeBar
from kraken_knight.historical_study import (
    SELECTED_POLICY,
    STUDY_SCHEMA,
    WARMUP_DAYS,
    CurveRun,
    _backtest_config,
    _bars_hash,
    _buy_and_hold_curve,
    _cost_cases,
    _decimal_text,
    _load_config,
    _selected_strategy,
    _split_boundaries,
    _write_buy_and_hold_entries,
    _write_decisions,
    _write_equity,
    _write_fills,
    _write_metrics,
    _write_risk,
    bars_to_causal_inputs,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPOSITORY_ROOT / "research" / "btc_cad_v1_backtest.json"
V2_CONFIG = REPOSITORY_ROOT / "research" / "btc_cad_v2_drawdown_counterfactual.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bars(count: int = 380) -> tuple[DailyTradeBar, ...]:
    first = date(2020, 1, 1)
    result: list[DailyTradeBar] = []
    for index in range(count):
        if index < 255:
            close = Decimal("10000") + Decimal(index * 20)
        elif index < 275:
            close = Decimal("9000") + Decimal((index - 255) * 5)
        else:
            close = Decimal("9100") + Decimal((index - 275) * 25)
        day = first + timedelta(days=index)
        minute = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=16)
        result.append(
            DailyTradeBar(
                day=day,
                open=close,
                high=close + Decimal("10"),
                low=close - Decimal("10"),
                close=close,
                volume=Decimal("100"),
                trade_count=20,
                execution_minute=minute,
                execution_vwap=close + Decimal("0.5"),
                execution_volume=Decimal("100"),
                execution_trade_count=10,
            )
        )
    return tuple(result)


def _normalized_manifest(
    path: Path,
    *,
    bars: tuple[DailyTradeBar, ...],
    data_sha256: str,
) -> Path:
    base = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    dataset = base["dataset"]
    payload = {
        "schema_version": DATASET_SCHEMA,
        "source": {
            "provider": dataset["provider"],
            "method": dataset["source_method"],
            "documentation_url": dataset["documentation_url"],
            "archive_url": dataset["archive_url"],
            "archive_file_id": dataset["archive_file_id"],
            "archive_entry_name": dataset["archive_entry_name"],
            "request_pair": dataset["request_pair"],
            "cutoff_exclusive": dataset["cutoff_exclusive_utc"],
        },
        "raw_archive": {
            "filename": "XBTCAD.csv",
            "entry_name": dataset["archive_entry_name"],
            "sha256": dataset["raw_csv_sha256"],
            "crc32": dataset["raw_csv_crc32"],
            "size_bytes": dataset["raw_csv_size_bytes"],
            "zip_compressed_size_bytes": dataset["zip_compressed_size_bytes"],
            "zip_uncompressed_size_bytes": dataset["zip_uncompressed_size_bytes"],
            "row_count": dataset["raw_csv_row_count"],
            "included_trade_count": dataset["raw_csv_row_count"],
            "cutoff_exclusive": dataset["cutoff_exclusive_utc"],
            "complete": True,
            "completeness_basis": (
                "all_rows_strictly_before_cutoff_and_last_observed_utc_day_is_cutoff_minus_one"
            ),
        },
        "normalized_csv": {
            "filename": "synthetic_daily.csv",
            "sha256": data_sha256,
            "row_count": len(bars),
            "first_date": bars[0].day.isoformat(),
            "last_date": bars[-1].day.isoformat(),
            "execution_reference": {
                "window": "[00:15,00:20) UTC",
                "selection": "first_positive_volume_minute",
                "price": "trade_volume_weighted_vwap",
                "interval_timestamp": "minute_open",
                "available_at": "minute_close",
                "missing_policy": "blank",
            },
        },
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _git_init(repository: Path) -> str:
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "counterfactual@example.com"),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Counterfactual Test"),
        cwd=repository,
        check=True,
    )
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "synthetic frozen study"), cwd=repository, check=True
    )
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _prepare_synthetic_study(
    tmp_path: Path,
) -> tuple[Path, Path, tuple[DailyTradeBar, ...], dict[str, object]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "research").mkdir()
    (repository / "reports" / "published" / "synthetic-v1").mkdir(parents=True)
    (repository / "src").mkdir()
    (repository / "src" / "frozen_runner.py").write_text(
        "# synthetic frozen identity\n", encoding="utf-8"
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "synthetic-counterfactual"\nversion = "0"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    base_path = repository / "research" / BASE_CONFIG.name
    base_path.write_bytes(BASE_CONFIG.read_bytes())

    bars = _bars()
    data_sha = _bars_hash(bars)
    normalized_manifest = _normalized_manifest(
        tmp_path / "synthetic.manifest.json", bars=bars, data_sha256=data_sha
    )
    normalized_manifest_sha = _sha256(normalized_manifest)
    base_document = _load_config(base_path).document
    primary_case = next(
        case for case in _cost_cases(base_document) if case.name == "taker_taker_plus_10bps"
    )
    config = _backtest_config(base_document, primary_case)
    strategy = _selected_strategy(base_document, windows=SELECTED_POLICY)
    candles, references = bars_to_causal_inputs(bars)
    evaluation_start = candles[WARMUP_DAYS - 1].close_time + timedelta(minutes=15)
    result = run_backtest(
        candles,
        strategy,
        config,
        execution_references=references,
        evaluation_start=evaluation_start,
    )
    boundaries = _split_boundaries(bars[WARMUP_DAYS:])
    parent = repository / "reports" / "published" / "synthetic-v1"
    buy_hold_curve, buy_hold_costs, _, buy_hold_attempts = _buy_and_hold_curve(
        template=result.equity_curve,
        evaluation_start=evaluation_start,
        references=references,
        config=config,
    )
    _write_equity(
        parent / "daily_equity.csv",
        runs=(
            CurveRun("frozen_v1", result.equity_curve, result.trades, result),
            CurveRun(
                "fee_aware_btc_buy_and_hold",
                buy_hold_curve,
                (),
                None,
                buy_hold_costs,
            ),
        ),
        boundaries=boundaries,
    )
    _write_decisions(parent / "decisions.csv", result, boundaries)
    _write_fills(parent / "fills.csv", result.trades, boundaries)
    _write_risk(parent / "risk_events.csv", result.risk_events, boundaries)
    _write_buy_and_hold_entries(parent / "buy_and_hold_entries.csv", buy_hold_attempts, boundaries)
    _write_metrics(
        parent / "metrics.csv",
        runs=(CurveRun("frozen_v1", result.equity_curve, result.trades, result),),
        cost_results=(),
        boundaries=boundaries,
    )

    disarms = [
        event for event in result.risk_events if event.event_type is RiskEventType.DRAWDOWN_DISARMED
    ]
    assert disarms
    fake_v1_commit = "e" * 40
    summary = {
        "schema_version": STUDY_SCHEMA,
        "study_id": "btc_cad_price_only_v1_preregistered",
        "repository": {"commit": fake_v1_commit},
        "hashes": {
            "input_data_sha256": data_sha,
            "selected_clean_data_sha256": _bars_hash(bars),
            "normalized_manifest_sha256": normalized_manifest_sha,
        },
    }
    (parent / "summary.json").write_text(
        json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
    )
    artifact_names = (
        "buy_and_hold_entries.csv",
        "daily_equity.csv",
        "decisions.csv",
        "fills.csv",
        "metrics.csv",
        "risk_events.csv",
        "summary.json",
    )
    artifacts = {name: _sha256(parent / name) for name in artifact_names}
    checksums = parent / "checksums.sha256"
    checksums.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(artifacts.items())),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": STUDY_SCHEMA,
        "study_id": "btc_cad_price_only_v1_preregistered",
        "repository": {"commit": fake_v1_commit},
        "artifacts": artifacts,
        "checksums_sha256": _sha256(checksums),
        "data": {
            "input_sha256": data_sha,
            "selected_clean_sha256": _bars_hash(bars),
            "normalized_manifest": {
                "filename": normalized_manifest.name,
                "sha256": normalized_manifest_sha,
            },
        },
    }
    (parent / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )

    v2 = json.loads(V2_CONFIG.read_text(encoding="utf-8"))
    v2["base_protocol"]["path"] = f"research/{BASE_CONFIG.name}"
    v2["base_protocol"]["sha256"] = _sha256(base_path)
    v2["sealed_v1_reference"].update(
        {
            "path": "reports/published/synthetic-v1",
            "code_commit": fake_v1_commit,
            "manifest_sha256": _sha256(parent / "manifest.json"),
            "checksums_file_sha256": _sha256(checksums),
            "daily_equity_sha256": artifacts["daily_equity.csv"],
            "metrics_sha256": artifacts["metrics.csv"],
            "risk_events_sha256": artifacts["risk_events.csv"],
            "expected_primary_final_equity_cad": _decimal_text(result.equity_curve[-1].equity),
            "expected_primary_trade_count": len(result.trades),
            "expected_disarm_date_utc": disarms[0].observed_at.date().isoformat(),
        }
    )
    frozen_identity: dict[str, object] = {
        "normalized_csv_filename": "synthetic_daily.csv",
        "normalized_csv_sha256": data_sha,
        "normalized_manifest_filename": normalized_manifest.name,
        "normalized_manifest_sha256": normalized_manifest_sha,
        "selected_clean_sequence_sha256": _bars_hash(bars),
    }
    v2["frozen_data_identity"] = frozen_identity
    config_path = repository / "research" / V2_CONFIG.name
    config_path.write_text(json.dumps(v2, indent=2) + "\n", encoding="utf-8")
    commit = _git_init(repository)
    arguments: dict[str, object] = {
        "preregistration_path": config_path,
        "repository_root": repository,
        "normalized_manifest_path": normalized_manifest,
        "source_data_filename": "synthetic_daily.csv",
        "source_data_sha256": data_sha,
        "expected_commit": commit,
        "expected_preregistration_sha256": _sha256(config_path),
        "allow_dirty": True,
        "_code_identity_paths": (repository / "src" / "frozen_runner.py",),
        "_expected_frozen_data_identity": frozen_identity,
    }
    return repository, normalized_manifest, bars, arguments


def _artifact_bytes(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def test_synthetic_counterfactual_bundle_is_deterministic_and_explicit(tmp_path: Path) -> None:
    _, _, bars, arguments = _prepare_synthetic_study(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    run_historical_counterfactual(bars, output_dir=first, **arguments)  # type: ignore[arg-type]
    run_historical_counterfactual(bars, output_dir=second, **arguments)  # type: ignore[arg-type]

    assert _artifact_bytes(first) == _artifact_bytes(second)
    expected = {
        "summary.json",
        "metrics.csv",
        "pairwise_deltas.csv",
        "calendar_returns.csv",
        "daily_equity.csv",
        "decisions.csv",
        "fills.csv",
        "risk_events.csv",
        "buy_and_hold_entries.csv",
        "report.md",
        "manifest.json",
        "checksums.sha256",
        "charts/equity.svg",
        "charts/benchmark_equity.svg",
        "charts/drawdown.svg",
        "charts/delta_vs_frozen_v1.svg",
        "charts/risk_state.svg",
    }
    assert set(_artifact_bytes(first)) == expected
    summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == COUNTERFACTUAL_STUDY_SCHEMA
    assert summary["analysis_class"] == "post_holdout_exploratory"
    assert summary["prior_v1_holdout_opened"] is True
    assert summary["confirmatory_evidence"] is False
    assert summary["live_trading_authorized"] is False
    assert summary["parent_v1"]["production_replay_exact_match"] is True
    assert summary["code_identity"]["synthetic_data_identity_override_used"] is True
    for filename in ("decisions.csv", "fills.csv", "risk_events.csv"):
        header = (first / filename).read_text(encoding="utf-8").splitlines()[0]
        assert header.startswith("variant,risk_epoch,audit_record_id,")
    assert "opened_v1_holdout" in (first / "metrics.csv").read_text(encoding="utf-8")


def test_counterfactual_fails_closed_when_parent_artifact_is_tampered(tmp_path: Path) -> None:
    repository, _, bars, arguments = _prepare_synthetic_study(tmp_path)
    decisions = repository / "reports" / "published" / "synthetic-v1" / "decisions.csv"
    decisions.write_text(decisions.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(HistoricalCounterfactualError, match="does not match checksums"):
        run_historical_counterfactual(
            bars,
            output_dir=tmp_path / "tampered-output",
            **arguments,  # type: ignore[arg-type]
        )
