from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from kraken_knight.backtest import run_backtest
from kraken_knight.historical_data import (
    DATASET_SCHEMA,
    DailyTradeBar,
)
from kraken_knight.historical_study import (
    HistoricalStudyError,
    bars_to_causal_inputs,
    load_daily_bars,
    run_historical_study,
    select_longest_contiguous_sequence,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PREREGISTRATION = REPOSITORY_ROOT / "research" / "btc_cad_v1_backtest.json"


def _bar(day: date, index: int, *, execution: bool = True) -> DailyTradeBar:
    close = Decimal("10000") + Decimal(index * 11 + index % 7)
    minute = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=16)
    return DailyTradeBar(
        day=day,
        open=close - Decimal("2"),
        high=close + Decimal("8"),
        low=close - Decimal("8"),
        close=close,
        volume=Decimal("4.5"),
        trade_count=12,
        execution_minute=minute if execution else None,
        execution_vwap=close + Decimal("0.5") if execution else None,
        execution_volume=Decimal("0.75") if execution else None,
        execution_trade_count=3 if execution else None,
    )


def _bars(count: int = 258) -> tuple[DailyTradeBar, ...]:
    first = date(2020, 1, 1)
    return tuple(_bar(first + timedelta(days=index), index) for index in range(count))


def _git_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=path, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=path, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=path, check=True)
    tracked = path / "frozen.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    (path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (path / "pyproject.toml").write_text('[project]\nname = "frozen-test"\n', encoding="utf-8")
    config = path / "research" / "btc_cad_v1_backtest.json"
    config.parent.mkdir()
    config.write_bytes(PREREGISTRATION.read_bytes())
    identity = path / "src" / "frozen_runner.py"
    identity.parent.mkdir()
    identity.write_text("# frozen test identity\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=path, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "frozen"), cwd=path, check=True)
    return path


def _commit(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _config(repository: Path) -> Path:
    return repository / "research" / "btc_cad_v1_backtest.json"


def _config_hash(repository: Path) -> str:
    return hashlib.sha256(_config(repository).read_bytes()).hexdigest()


def _normalized_manifest(
    path: Path,
    *,
    bars: tuple[DailyTradeBar, ...] | list[DailyTradeBar],
    data_sha256: str,
    filename: str = "daily.csv",
) -> Path:
    frozen_dataset = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))["dataset"]
    payload = {
        "schema_version": DATASET_SCHEMA,
        "source": {
            "provider": frozen_dataset["provider"],
            "method": frozen_dataset["source_method"],
            "documentation_url": frozen_dataset["documentation_url"],
            "archive_url": frozen_dataset["archive_url"],
            "archive_file_id": frozen_dataset["archive_file_id"],
            "archive_entry_name": frozen_dataset["archive_entry_name"],
            "request_pair": frozen_dataset["request_pair"],
            "cutoff_exclusive": frozen_dataset["cutoff_exclusive_utc"],
        },
        "raw_archive": {
            "filename": "XBTCAD.csv",
            "entry_name": frozen_dataset["archive_entry_name"],
            "sha256": frozen_dataset["raw_csv_sha256"],
            "crc32": frozen_dataset["raw_csv_crc32"],
            "size_bytes": frozen_dataset["raw_csv_size_bytes"],
            "zip_compressed_size_bytes": frozen_dataset["zip_compressed_size_bytes"],
            "zip_uncompressed_size_bytes": frozen_dataset["zip_uncompressed_size_bytes"],
            "row_count": frozen_dataset["raw_csv_row_count"],
            "included_trade_count": frozen_dataset["raw_csv_row_count"],
            "cutoff_exclusive": frozen_dataset["cutoff_exclusive_utc"],
            "complete": True,
            "completeness_basis": (
                "all_rows_strictly_before_cutoff_and_last_observed_utc_day_is_cutoff_minus_one"
            ),
        },
        "normalized_csv": {
            "filename": filename,
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


def _run_arguments(
    repository: Path,
    manifest: Path,
    *,
    data_sha256: str,
) -> dict[str, Any]:
    return {
        "preregistration_path": _config(repository),
        "repository_root": repository,
        "normalized_manifest_path": manifest,
        "source_data_filename": "daily.csv",
        "source_data_sha256": data_sha256,
        "expected_commit": _commit(repository),
        "expected_preregistration_sha256": _config_hash(repository),
        "_code_identity_paths": (repository / "src" / "frozen_runner.py",),
    }


def _artifact_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_longest_contiguous_sequence_uses_earliest_tie_and_rejects_duplicates() -> None:
    first = date(2024, 1, 1)
    bars = (
        _bar(first, 0),
        _bar(first + timedelta(days=1), 1),
        _bar(first + timedelta(days=4), 4),
        _bar(first + timedelta(days=5), 5),
    )

    selected = select_longest_contiguous_sequence(bars)

    assert tuple(bar.day for bar in selected) == (first, first + timedelta(days=1))
    with pytest.raises(HistoricalStudyError, match="strictly date ordered"):
        select_longest_contiguous_sequence((bars[0], bars[0]))


def test_causal_input_uses_next_days_trade_minute_and_preserves_missing_reference() -> None:
    first = date(2024, 1, 1)
    bars = (
        _bar(first, 0),
        _bar(first + timedelta(days=1), 1, execution=False),
        _bar(first + timedelta(days=2), 2),
    )

    candles, references = bars_to_causal_inputs(bars)

    assert len(candles) == 3
    assert len(references) == 1
    assert references[0].decision_time == datetime(2024, 1, 3, 0, 15, tzinfo=UTC)
    assert references[0].execution_time == datetime(2024, 1, 3, 0, 17, tzinfo=UTC)
    assert references[0].reference_price == bars[2].execution_vwap

    last_minute_bar = replace(
        bars[2],
        execution_minute=datetime(2024, 1, 3, 0, 19, tzinfo=UTC),
    )
    _, last_minute_references = bars_to_causal_inputs((*bars[:2], last_minute_bar))
    assert last_minute_references[0].execution_time == datetime(2024, 1, 3, 0, 20, tzinfo=UTC)


def test_load_daily_bars_requires_complete_execution_evidence(tmp_path: Path) -> None:
    path = tmp_path / "daily.csv"
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(
            (
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume_btc",
                "trade_count",
                "execution_minute_utc",
                "execution_vwap_cad",
                "execution_volume_btc",
                "execution_trade_count",
            )
        )
        writer.writerow(
            ("2024-01-01", "10", "12", "9", "11", "1", "2", "2024-01-01T00:16:00Z", "", "1", "1")
        )

    with pytest.raises(HistoricalStudyError, match="invalid row at line 2"):
        load_daily_bars(path)


def test_final_study_refuses_dirty_repository_without_explicit_override(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repo")
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    bars = _bars(256)
    data_sha256 = "a" * 64
    manifest = _normalized_manifest(
        tmp_path / "normalized.manifest.json", bars=bars, data_sha256=data_sha256
    )

    with pytest.raises(HistoricalStudyError, match="dirty worktree"):
        run_historical_study(
            bars,
            output_dir=tmp_path / "result",
            **_run_arguments(repository, manifest, data_sha256=data_sha256),
        )


def test_engine_override_requires_development_mode_and_invalidates_result(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repo")
    bars = _bars(256)
    data_sha256 = "d" * 64
    manifest = _normalized_manifest(
        tmp_path / "normalized.manifest.json",
        bars=bars,
        data_sha256=data_sha256,
    )
    arguments = _run_arguments(repository, manifest, data_sha256=data_sha256)
    delegated_engine = partial(run_backtest)

    with pytest.raises(HistoricalStudyError, match="production run_backtest engine"):
        run_historical_study(
            bars,
            output_dir=tmp_path / "rejected",
            engine=delegated_engine,
            **arguments,
        )

    output = tmp_path / "development"
    run_historical_study(
        bars,
        output_dir=output,
        allow_dirty=True,
        engine=delegated_engine,
        **arguments,
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["evidence_statement"] == "RESEARCH_INVALIDATED"
    assert summary["code_identity"]["engine_override_used"] is True
    assert "NON-PRODUCTION ENGINE OVERRIDE" in (output / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    (
        ("source", "archive_file_id", "different-official-file"),
        ("raw_archive", "sha256", "0" * 64),
        ("normalized_csv.execution_reference", "available_at", "minute_open"),
    ),
)
def test_study_rejects_manifest_that_differs_from_frozen_archive_identity(
    tmp_path: Path,
    section: str,
    field: str,
    replacement: str,
) -> None:
    repository = _git_repository(tmp_path / "repo")
    bars = _bars(256)
    data_sha256 = "c" * 64
    manifest = _normalized_manifest(
        tmp_path / "normalized.manifest.json",
        bars=bars,
        data_sha256=data_sha256,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    target = payload
    for part in section.split("."):
        target = target[part]
    target[field] = replacement
    manifest.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(HistoricalStudyError, match="normalized provenance"):
        run_historical_study(
            bars,
            output_dir=tmp_path / "result",
            **_run_arguments(repository, manifest, data_sha256=data_sha256),
        )


def test_study_writes_deterministic_causal_audit_bundle(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repo")
    bars = list(_bars())
    # A missing trade minute is evidence of a no-fill, not permission to use a close.
    missing_index = 252
    bars[missing_index] = _bar(bars[missing_index].day, missing_index, execution=False)
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"
    source_data_sha256 = "a" * 64
    normalized_manifest = _normalized_manifest(
        tmp_path / "normalized.manifest.json",
        bars=bars,
        data_sha256=source_data_sha256,
    )

    first = run_historical_study(
        bars,
        output_dir=first_output,
        **_run_arguments(repository, normalized_manifest, data_sha256=source_data_sha256),
    )
    second = run_historical_study(
        bars,
        output_dir=second_output,
        **_run_arguments(repository, normalized_manifest, data_sha256=source_data_sha256),
    )

    assert _artifact_bytes(first_output) == _artifact_bytes(second_output)
    assert first.git_state.dirty is False
    assert first.clean_data_sha256 == second.clean_data_sha256
    summary = json.loads(first.summary_path.read_text(encoding="utf-8"))
    assert summary["live_trading_authorized"] is False
    assert summary["evidence_statement"] == ("ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED")
    assert summary["dataset"]["warmup_day_count"] == 250
    assert summary["dataset"]["evaluation_day_count"] == 8
    assert summary["dataset"]["missing_execution_reference_count"] == 1
    assert [item["observation_count"] for item in summary["chronological_splits"]] == [
        4,
        1,
        3,
    ]
    assert summary["robustness"] == {
        "evidence_scope": "development_plus_validation_only",
        "frozen_holdout_accessed_by_neighboring_grid": False,
        "grid_size": 27,
        "optimization_performed": False,
        "selected_point_unchanged": True,
    }
    buy_hold_status = summary["fee_aware_buy_and_hold"]
    assert buy_hold_status["comparator_definition"] == (
        "causal_accumulation_under_execution_volume_cap"
    )
    assert buy_hold_status["status"] == ("accumulated_until_remaining_cash_below_exchange_minimum")
    assert buy_hold_status["participation_capped"] is True
    assert buy_hold_status["entry_attempt_count"] == 2
    assert buy_hold_status["entry_fill_count"] == 2
    assert buy_hold_status["entry_volume_capped_fill_count"] == 1
    assert Decimal(buy_hold_status["entry_quantity_btc"]) > Decimal("0.075")
    assert Decimal(buy_hold_status["remaining_cash_cad"]) < Decimal("1")
    assert summary["holdout_evaluation_contract"]["non_primary_cost_sensitivity_access"] is False
    report = (first_output / "report.md").read_text(encoding="utf-8")
    assert "historical price evidence, not evidence of future profitability" in report
    assert "Clean committed worktree" in report
    assert "ENGINEERING_VALIDATED, PROFITABILITY_NOT_ESTABLISHED" in report

    with (first_output / "daily_equity.csv").open(encoding="utf-8", newline="") as source:
        equity_rows = list(csv.DictReader(source))
    frozen = [row for row in equity_rows if row["strategy"] == "frozen_v1"]
    assert frozen[0]["split"] == "shared_initial_boundary"
    assert frozen[0]["equity_cad"] == "1000"
    assert {row["split"] for row in frozen[1:]} == {
        "development",
        "validation",
        "frozen_holdout",
    }

    with (first_output / "robustness.csv").open(encoding="utf-8", newline="") as source:
        robustness = list(csv.DictReader(source))
    assert len(robustness) == 27
    assert {row["selection_performed"] for row in robustness} == {"False"}
    assert sum(row["pre_registered_selected"] == "True" for row in robustness) == 1
    selected = next(row for row in robustness if row["pre_registered_selected"] == "True")
    assert (selected["momentum_days"], selected["trend_days"], selected["volatility_days"]) == (
        "90",
        "200",
        "30",
    )
    assert set(selected) == {
        "momentum_days",
        "trend_days",
        "volatility_days",
        "pre_registered_selected",
        "selection_performed",
        "pre_holdout_observation_count",
        "pre_holdout_total_return",
        "pre_holdout_sharpe",
        "pre_holdout_max_drawdown",
    }

    with (first_output / "metrics.csv").open(encoding="utf-8", newline="") as source:
        metrics = list(csv.DictReader(source))
    cash = next(
        row
        for row in metrics
        if row["category"] == "comparator"
        and row["name"] == "cad_cash"
        and row["scope"] == "full_evaluation"
    )
    assert cash["sharpe"] == ""
    assert cash["sortino"] == ""
    assert cash["calmar"] == ""
    buy_hold = next(
        row
        for row in metrics
        if row["category"] == "comparator"
        and row["name"] == "fee_aware_btc_buy_and_hold"
        and row["scope"] == "full_evaluation"
    )
    assert buy_hold["trade_count"] == str(buy_hold_status["entry_fill_count"] + 1)
    assert Decimal(buy_hold["total_fees_cad"]) > 0
    v1 = next(
        row
        for row in metrics
        if row["category"] == "comparator"
        and row["name"] == "frozen_v1"
        and row["scope"] == "full_evaluation"
    )
    assert Decimal(v1["gross_profit_cad"]) == (
        Decimal(v1["net_profit_cad"])
        + Decimal(v1["total_fees_cad"])
        + Decimal(v1["total_slippage_cad"])
    )
    non_primary_costs = [
        row
        for row in metrics
        if row["category"] == "cost_sensitivity" and row["name"] != "taker_taker_plus_10bps"
    ]
    assert {row["scope"] for row in non_primary_costs} == {"pre_holdout"}
    holdout_v1 = next(
        row
        for row in metrics
        if row["category"] == "comparator"
        and row["name"] == "frozen_v1"
        and row["scope"] == "frozen_holdout"
    )
    assert holdout_v1["gross_profit_cad"] == ""
    assert holdout_v1["total_fees_cad"] == ""

    decisions = (first_output / "decisions.csv").read_text(encoding="utf-8")
    assert decisions.count("no_fill_reference_unavailable") == 1
    with (first_output / "decisions.csv").open(encoding="utf-8", newline="") as source:
        decision_rows = list(csv.DictReader(source))
    assert decision_rows
    assert "remaining_btc_after_attempt" in decision_rows[0]
    with (first_output / "fills.csv").open(encoding="utf-8", newline="") as source:
        fills = list(csv.DictReader(source))
    assert fills
    assert all(Decimal(row["execution_price_cad"]) % Decimal("0.1") == 0 for row in fills)
    assert all(Decimal(row["quantity_btc"]) % Decimal("0.00000001") == 0 for row in fills)
    assert all(row["execution_volume_btc"] for row in fills)
    assert all(Decimal(row["volume_participation_fraction"]) <= Decimal("0.10") for row in fills)
    assert {row["volume_cap_applied"] for row in fills} <= {"True", "False"}

    with (first_output / "buy_and_hold_entries.csv").open(encoding="utf-8", newline="") as source:
        buy_hold_entries = list(csv.DictReader(source))
    assert len(buy_hold_entries) == buy_hold_status["entry_attempt_count"]
    assert (
        sum(row["outcome"].startswith("filled") for row in buy_hold_entries)
        == (buy_hold_status["entry_fill_count"])
    )
    assert all(
        Decimal(row["executed_quantity_btc"]) <= Decimal(row["maximum_participating_quantity_btc"])
        for row in buy_hold_entries
    )
    assert any(row["volume_cap_applied"] == "True" for row in buy_hold_entries)
    for chart in ("equity.svg", "drawdown.svg", "robustness.svg"):
        ElementTree.parse(first_output / "charts" / chart)

    checksum_lines = (first_output / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    assert checksum_lines == sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1])
    for line in checksum_lines:
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((first_output / relative).read_bytes()).hexdigest() == expected
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["repository"]["commit"] == first.git_state.commit
    assert (
        manifest["dependency_lock"]["sha256"]
        == hashlib.sha256((repository / "uv.lock").read_bytes()).hexdigest()
    )
    assert (
        manifest["data"]["normalized_manifest"]["sha256"]
        == hashlib.sha256(normalized_manifest.read_bytes()).hexdigest()
    )
    assert manifest["determinism"]["parameter_selection_from_results"] is False


def test_dirty_override_is_prominent_and_output_folder_must_be_empty(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repo")
    (repository / "dirty.txt").write_text("development\n", encoding="utf-8")
    output = tmp_path / "result"
    bars = _bars(256)
    data_sha256 = "b" * 64
    manifest = _normalized_manifest(
        tmp_path / "normalized.manifest.json", bars=bars, data_sha256=data_sha256
    )
    arguments = _run_arguments(repository, manifest, data_sha256=data_sha256)

    result = run_historical_study(
        bars,
        output_dir=output,
        allow_dirty=True,
        **arguments,
    )

    assert result.git_state.dirty_override_used is True
    assert "DIRTY DEVELOPMENT OVERRIDE" in (output / "report.md").read_text(encoding="utf-8")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["evidence_statement"] == "RESEARCH_INVALIDATED"
    with pytest.raises(HistoricalStudyError, match="absent or empty"):
        run_historical_study(
            bars,
            output_dir=output,
            allow_dirty=True,
            **arguments,
        )
