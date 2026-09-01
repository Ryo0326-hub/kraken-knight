"""Deterministic post-holdout drawdown-policy counterfactual study.

This module is deliberately separate from both live trading and the sealed V1
historical study.  It replays the sealed V1 result first, verifies that its
economic records are identical to the checksummed publication, and only then
evaluates the two pre-specified drawdown-policy counterfactuals.  The former V1
holdout has already been observed, so every result produced here is explicitly
exploratory rather than fresh out-of-sample evidence.
"""

from __future__ import annotations

import csv
import json
import platform
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, cast

from .backtest import (
    BacktestConfig,
    BacktestResult,
    DrawdownPolicyMode,
    EquityPoint,
    RiskEvent,
    RiskEventType,
    Trade,
    run_backtest,
)
from .historical_data import DailyTradeBar
from .historical_study import (
    METRIC_COLUMNS,
    SELECTED_POLICY,
    STUDY_SCHEMA,
    WARMUP_DAYS,
    BuyAndHoldEntryAttempt,
    CurveRun,
    GitState,
    HistoricalStudyError,
    SplitBoundary,
    _backtest_config,
    _bars_hash,
    _buy_and_hold_curve,
    _canonical_json_bytes,
    _cash_curve,
    _code_identity,
    _cost_cases,
    _decimal,
    _decimal_text,
    _git_state,
    _iso_z,
    _load_config,
    _mapping,
    _metric_dict,
    _normalized_manifest_hash,
    _selected_strategy,
    _sha256_bytes,
    _sha256_file,
    _split_boundaries,
    _split_for_day,
    _study_metrics,
    _validate_commit,
    _validate_sha256,
    _write_json,
    bars_to_causal_inputs,
    load_daily_bars,
    select_longest_contiguous_sequence,
)
from .historical_study import (
    _write_buy_and_hold_entries as _write_v1_buy_and_hold_entries,
)
from .historical_study import (
    _write_decisions as _write_v1_decisions,
)
from .historical_study import (
    _write_equity as _write_v1_equity,
)
from .historical_study import (
    _write_fills as _write_v1_fills,
)
from .historical_study import (
    _write_risk as _write_v1_risk,
)
from .research_charts import ChartPoint, ChartSeries, write_line_chart
from .research_metrics import ResearchMetrics

COUNTERFACTUAL_STUDY_SCHEMA = "kraken-knight-counterfactual-study-v2"
COUNTERFACTUAL_PREREGISTRATION_SCHEMA = "kraken-knight-counterfactual-preregistration-v1"
COUNTERFACTUAL_STUDY_ID = "btc_cad_v2_drawdown_counterfactual_preregistered"
OPENED_HOLDOUT_SCOPE = "opened_v1_holdout"
EVIDENCE_SCOPE = "opened_v1_holdout_exploratory_counterfactual"
FROZEN_DATA_IDENTITY: Mapping[str, object] = {
    "normalized_csv_filename": "kraken_xbtcad_daily_archive_2026-01-01.csv",
    "normalized_csv_sha256": "eab9022203ad161559e03a1bbd9e1519198408b5a0687cd70032cf9a8a9b3ec3",
    "normalized_manifest_filename": "kraken_xbtcad_daily_archive_2026-01-01.manifest.json",
    "normalized_manifest_sha256": (
        "3f861b4687b57d1770ba16c13afe726d63366cf101f08dee60ae9f288530c079"
    ),
    "selected_clean_sequence_sha256": (
        "193c8dbee45ba0dc86ab26cf9402ae80cdc0073580a0524d88bbe09b16c33724"
    ),
}
VARIANT_ORDER = (
    "cad_cash",
    "fee_aware_btc_buy_and_hold",
    "sealed_frozen_v1_persistent_disarm",
    "no_drawdown_gate",
    "mechanical_90d_trend_rearm",
)
ENGINE_VARIANTS = (
    "sealed_frozen_v1_persistent_disarm",
    "no_drawdown_gate",
    "mechanical_90d_trend_rearm",
)
COUNTERFACTUAL_VARIANTS = (
    "no_drawdown_gate",
    "mechanical_90d_trend_rearm",
)


class HistoricalCounterfactualError(HistoricalStudyError):
    """Raised when the counterfactual research contract cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class CounterfactualStudyResult:
    output_dir: Path
    summary_path: Path
    manifest_path: Path
    checksums_path: Path
    config_sha256: str
    base_config_sha256: str
    input_data_sha256: str
    clean_data_sha256: str
    git_state: GitState


@dataclass(frozen=True, slots=True)
class _LoadedCounterfactualConfig:
    document: Mapping[str, Any]
    sha256: str
    base_document: Mapping[str, Any]
    base_sha256: str
    base_path: Path
    parent_path: Path


@dataclass(frozen=True, slots=True)
class _VerifiedParent:
    path: Path
    checksums_sha256: str
    artifacts: Mapping[str, str]
    summary: Mapping[str, Any]
    manifest: Mapping[str, Any]


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "none"
        extra = ",".join(sorted(actual - expected)) or "none"
        raise HistoricalCounterfactualError(
            f"{field} keys differ from the frozen contract; missing={missing}; extra={extra}"
        )


def _relative_repository_path(repository_root: Path, value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise HistoricalCounterfactualError(f"{field} must be a non-empty relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
        raise HistoricalCounterfactualError(f"{field} must be a normalized repository path")
    resolved = (repository_root / Path(*pure.parts)).resolve()
    if not resolved.is_relative_to(repository_root.resolve()):
        raise HistoricalCounterfactualError(f"{field} escapes repository_root")
    return resolved


def _load_counterfactual_config(
    path: Path,
    *,
    repository_root: Path,
    expected_data_identity: Mapping[str, object] = FROZEN_DATA_IDENTITY,
) -> _LoadedCounterfactualConfig:
    try:
        raw = path.read_bytes()
        root = _mapping(
            json.loads(raw.decode("utf-8"), parse_float=Decimal),
            field="counterfactual config",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalCounterfactualError(
            "counterfactual pre-registration must be readable JSON"
        ) from exc

    _require_exact_keys(
        root,
        {
            "schema_version",
            "study_id",
            "analysis_type",
            "pair",
            "base_protocol",
            "prior_information",
            "sealed_v1_reference",
            "frozen_data_identity",
            "counterfactual_variants",
            "comparators",
            "evaluation_contract",
            "interpretation",
        },
        field="counterfactual config",
    )
    if root.get("schema_version") != COUNTERFACTUAL_PREREGISTRATION_SCHEMA:
        raise HistoricalCounterfactualError("unsupported counterfactual config schema")
    if root.get("study_id") != COUNTERFACTUAL_STUDY_ID:
        raise HistoricalCounterfactualError("unexpected counterfactual study_id")
    if root.get("analysis_type") != "exploratory_counterfactual_after_v1_holdout_opened":
        raise HistoricalCounterfactualError("analysis_type must disclose the opened V1 holdout")
    if root.get("pair") != "BTC/CAD":
        raise HistoricalCounterfactualError("counterfactual runner accepts only BTC/CAD")

    base = _mapping(root.get("base_protocol"), field="base_protocol")
    expected_base = {
        "path",
        "sha256",
        "inheritance_rule",
        "data_cutoff_exclusive_utc",
    }
    _require_exact_keys(base, expected_base, field="base_protocol")
    if base.get("inheritance_rule") != (
        "all_v1_data_signal_execution_cost_volume_instrument_and_portfolio_rules_unchanged_"
        "except_the_named_drawdown_mode"
    ):
        raise HistoricalCounterfactualError("base-protocol inheritance rule is not frozen")
    if base.get("data_cutoff_exclusive_utc") != "2026-01-01T00:00:00Z":
        raise HistoricalCounterfactualError("counterfactual data cutoff is not frozen")
    base_sha = base.get("sha256")
    if not isinstance(base_sha, str):
        raise HistoricalCounterfactualError("base_protocol.sha256 must be a string")
    _validate_sha256(base_sha, field="base_protocol.sha256")
    base_path = _relative_repository_path(
        repository_root, base.get("path"), field="base_protocol.path"
    )
    if _sha256_file(base_path) != base_sha:
        raise HistoricalCounterfactualError("base V1 config SHA-256 does not match")
    loaded_base = _load_config(base_path)
    if loaded_base.sha256 != base_sha:
        raise HistoricalCounterfactualError("base V1 config identity changed during loading")
    base_dataset = _mapping(loaded_base.document.get("dataset"), field="base dataset")
    if base_dataset.get("cutoff_exclusive_utc") != base.get("data_cutoff_exclusive_utc"):
        raise HistoricalCounterfactualError("base V1 and V2 cutoff identities differ")

    prior = _mapping(root.get("prior_information"), field="prior_information")
    expected_prior = {
        "v1_result_seen_before_this_specification": True,
        "v1_holdout_opened": True,
        "v1_observation": "persistent_drawdown_disarm_removed_all_post_2020_market_exposure",
        "fresh_out_of_sample_claim_permitted": False,
        "legacy_split_labels_are_descriptive_only": True,
    }
    if dict(prior) != expected_prior:
        raise HistoricalCounterfactualError("prior-information disclosure is not frozen")

    sealed = _mapping(root.get("sealed_v1_reference"), field="sealed_v1_reference")
    _require_exact_keys(
        sealed,
        {
            "path",
            "code_commit",
            "manifest_sha256",
            "checksums_file_sha256",
            "daily_equity_sha256",
            "metrics_sha256",
            "risk_events_sha256",
            "expected_primary_final_equity_cad",
            "expected_primary_trade_count",
            "expected_disarm_date_utc",
        },
        field="sealed_v1_reference",
    )
    for field in (
        "manifest_sha256",
        "checksums_file_sha256",
        "daily_equity_sha256",
        "metrics_sha256",
        "risk_events_sha256",
    ):
        value = sealed.get(field)
        if not isinstance(value, str):
            raise HistoricalCounterfactualError(f"sealed_v1_reference.{field} must be a string")
        _validate_sha256(value, field=f"sealed_v1_reference.{field}")
    code_commit = sealed.get("code_commit")
    if not isinstance(code_commit, str):
        raise HistoricalCounterfactualError("sealed V1 code commit must be a string")
    _validate_commit(code_commit)
    _decimal(
        sealed.get("expected_primary_final_equity_cad"),
        field="expected_primary_final_equity_cad",
    )
    trade_count = sealed.get("expected_primary_trade_count")
    if isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0:
        raise HistoricalCounterfactualError("expected_primary_trade_count must be non-negative")
    try:
        date.fromisoformat(cast(str, sealed.get("expected_disarm_date_utc")))
    except (TypeError, ValueError) as exc:
        raise HistoricalCounterfactualError("expected_disarm_date_utc must be an ISO date") from exc
    parent_path = _relative_repository_path(
        repository_root, sealed.get("path"), field="sealed_v1_reference.path"
    )

    frozen_data = _mapping(root.get("frozen_data_identity"), field="frozen_data_identity")
    _require_exact_keys(
        frozen_data,
        {
            "normalized_csv_filename",
            "normalized_csv_sha256",
            "normalized_manifest_filename",
            "normalized_manifest_sha256",
            "selected_clean_sequence_sha256",
        },
        field="frozen_data_identity",
    )
    for field in (
        "normalized_csv_sha256",
        "normalized_manifest_sha256",
        "selected_clean_sequence_sha256",
    ):
        value = frozen_data.get(field)
        if not isinstance(value, str):
            raise HistoricalCounterfactualError(f"frozen_data_identity.{field} must be a string")
        _validate_sha256(value, field=f"frozen_data_identity.{field}")
    for field in ("normalized_csv_filename", "normalized_manifest_filename"):
        value = frozen_data.get(field)
        if not isinstance(value, str) or not value or Path(value).name != value:
            raise HistoricalCounterfactualError(
                f"frozen_data_identity.{field} must be one plain filename"
            )
    if dict(frozen_data) != dict(expected_data_identity):
        raise HistoricalCounterfactualError(
            "frozen data identity differs from the expected sealed V1 identity"
        )

    variants = root.get("counterfactual_variants")
    if not isinstance(variants, list):
        raise HistoricalCounterfactualError("counterfactual_variants must be an array")
    expected_variants: list[dict[str, object]] = [
        {
            "name": "no_drawdown_gate",
            "drawdown_mode": "disabled",
            "maximum_drawdown_threshold_applied": False,
            "drawdown_forced_liquidation": False,
            "drawdown_disarm": False,
            "purpose": "measure_the_frozen_v1_signal_without_a_high_water_drawdown_latch",
        },
        {
            "name": "mechanical_90d_trend_rearm",
            "drawdown_mode": "cooldown_rearm",
            "maximum_drawdown_threshold_applied": True,
            "maximum_drawdown_threshold_inherited": "0.20",
            "forced_liquidation_inherited": True,
            "cooldown_calendar_days": 90,
            "cooldown_clock_starts_at": "drawdown_disarmed_observed_at_utc",
            "cooldown_eligibility": (
                "causal_decision_time_greater_than_or_equal_to_disarm_observed_at_plus_90_"
                "calendar_days"
            ),
            "liquidation_completion_required": True,
            "rearm_signal_reason": "long_signal",
            "rearm_signal_definition": (
                "positive_90_day_momentum_and_close_strictly_above_sma200_with_valid_30_day_"
                "volatility"
            ),
            "rearm_signal_clock": "same_causal_daily_decision_available_at_00_15_utc",
            "rearm_event_time": "causal_decision_time_utc",
            "execution_reference_required_for_state_rearm": False,
            "rearm_high_water_reset": "current_fee_aware_liquidation_equity",
            "rearm_decision_may_rebalance": True,
            "other_risk_and_execution_gates_still_apply": True,
            "future_disarms_and_rearms_permitted": True,
            "purpose": "measure_a_fully_mechanical_recovery_policy_without_operator_discretion",
        },
    ]
    if variants != expected_variants:
        raise HistoricalCounterfactualError("counterfactual variants differ from the frozen pair")
    if tuple(root.get("comparators", ())) != VARIANT_ORDER:
        raise HistoricalCounterfactualError("counterfactual comparator order differs")

    evaluation = _mapping(root.get("evaluation_contract"), field="evaluation_contract")
    expected_evaluation = {
        "run_each_counterfactual_exactly_once_after_code_tests_and_this_file_are_committed": True,
        "parameter_optimization_permitted": False,
        "variant_selection_from_results_permitted": False,
        "same_normalized_dataset_and_manifest_required": True,
        "same_causal_clock_required": True,
        "same_fee_slippage_and_volume_assumptions_required": True,
        "same_strategy_and_position_sizing_required": True,
        "same_portfolio_constraints_except_drawdown_mode_required": True,
        "randomness_permitted": False,
    }
    if dict(evaluation) != expected_evaluation:
        raise HistoricalCounterfactualError("counterfactual evaluation contract is not frozen")
    interpretation = _mapping(root.get("interpretation"), field="interpretation")
    expected_interpretation = {
        "evidence_scope": "exploratory_historical_counterfactual",
        "engineering_validation_and_profitability_are_separate": True,
        "live_trading_authorized": False,
        "paper_trading_authorized": False,
        "future_profitability_established": False,
        "next_independent_evidence": (
            "new_data_after_2026_01_01_or_forward_paper_or_micro_live_observation"
        ),
    }
    if dict(interpretation) != expected_interpretation:
        raise HistoricalCounterfactualError("counterfactual interpretation is not frozen")

    return _LoadedCounterfactualConfig(
        document=root,
        sha256=_sha256_bytes(raw),
        base_document=loaded_base.document,
        base_sha256=loaded_base.sha256,
        base_path=base_path,
        parent_path=parent_path,
    )


def _parse_checksums(path: Path, *, root: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoricalCounterfactualError("sealed V1 checksums file is not readable") from exc
    if not lines:
        raise HistoricalCounterfactualError("sealed V1 checksums file is empty")
    artifacts: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if len(line) < 67 or line[64:66] != "  ":
            raise HistoricalCounterfactualError(
                f"sealed V1 checksum line {line_number} is malformed"
            )
        digest, name = line[:64], line[66:]
        _validate_sha256(digest, field=f"sealed checksum line {line_number}")
        pure = PurePosixPath(name)
        if (
            not name
            or pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != name
            or name in artifacts
        ):
            raise HistoricalCounterfactualError(
                f"sealed V1 checksum path at line {line_number} is unsafe or duplicated"
            )
        artifact = root / Path(*pure.parts)
        if not artifact.is_file() or _sha256_file(artifact) != digest:
            raise HistoricalCounterfactualError(
                f"sealed V1 artifact does not match checksums.sha256: {name}"
            )
        artifacts[name] = digest
    return artifacts


def _read_json_mapping(path: Path, *, field: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), field=field)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalCounterfactualError(f"{field} is not readable JSON") from exc


def _verify_parent_bundle(loaded: _LoadedCounterfactualConfig) -> _VerifiedParent:
    parent = loaded.parent_path
    if not parent.is_dir():
        raise HistoricalCounterfactualError("sealed V1 publication directory is missing")
    sealed = _mapping(loaded.document.get("sealed_v1_reference"), field="sealed_v1_reference")
    checksums_path = parent / "checksums.sha256"
    checksums_sha = _sha256_file(checksums_path)
    if checksums_sha != sealed.get("checksums_file_sha256"):
        raise HistoricalCounterfactualError("sealed V1 checksums file SHA-256 differs")
    artifacts = _parse_checksums(checksums_path, root=parent)
    required = {
        "summary.json",
        "metrics.csv",
        "daily_equity.csv",
        "decisions.csv",
        "fills.csv",
        "risk_events.csv",
        "buy_and_hold_entries.csv",
    }
    if not required.issubset(artifacts):
        missing = ", ".join(sorted(required - set(artifacts)))
        raise HistoricalCounterfactualError(f"sealed V1 publication is incomplete: {missing}")
    named_hashes = {
        "daily_equity.csv": sealed.get("daily_equity_sha256"),
        "metrics.csv": sealed.get("metrics_sha256"),
        "risk_events.csv": sealed.get("risk_events_sha256"),
    }
    if any(artifacts[name] != expected for name, expected in named_hashes.items()):
        raise HistoricalCounterfactualError("sealed V1 named artifact hash differs")

    summary = _read_json_mapping(parent / "summary.json", field="sealed V1 summary")
    manifest = _read_json_mapping(parent / "manifest.json", field="sealed V1 manifest")
    if _sha256_file(parent / "manifest.json") != sealed.get("manifest_sha256"):
        raise HistoricalCounterfactualError("sealed V1 manifest SHA-256 differs")
    if (
        summary.get("schema_version") != STUDY_SCHEMA
        or manifest.get("schema_version") != STUDY_SCHEMA
    ):
        raise HistoricalCounterfactualError("sealed V1 schema is not the price-study V1 schema")
    if (
        summary.get("study_id") != "btc_cad_price_only_v1_preregistered"
        or manifest.get("study_id") != "btc_cad_price_only_v1_preregistered"
    ):
        raise HistoricalCounterfactualError("sealed V1 study identity differs")
    manifest_artifacts = _mapping(manifest.get("artifacts"), field="sealed manifest artifacts")
    if dict(manifest_artifacts) != artifacts:
        raise HistoricalCounterfactualError("sealed manifest and checksums artifact maps differ")
    if manifest.get("checksums_sha256") != checksums_sha:
        raise HistoricalCounterfactualError("sealed manifest does not bind checksums.sha256")
    code_commit = sealed.get("code_commit")
    summary_repository = _mapping(summary.get("repository"), field="sealed summary repository")
    manifest_repository = _mapping(manifest.get("repository"), field="sealed manifest repository")
    if (
        summary_repository.get("commit") != code_commit
        or manifest_repository.get("commit") != code_commit
    ):
        raise HistoricalCounterfactualError("sealed V1 code commit identity differs")

    with (parent / "metrics.csv").open(encoding="utf-8", newline="") as source:
        matches = [
            row
            for row in csv.DictReader(source)
            if row.get("category") == "comparator"
            and row.get("name") == "frozen_v1"
            and row.get("scope") == "full_evaluation"
        ]
    if len(matches) != 1:
        raise HistoricalCounterfactualError(
            "sealed V1 primary metrics row is missing or duplicated"
        )
    expected_equity = _decimal(
        sealed.get("expected_primary_final_equity_cad"),
        field="expected_primary_final_equity_cad",
    )
    if _decimal(matches[0].get("final_equity_cad"), field="sealed final equity") != expected_equity:
        raise HistoricalCounterfactualError("sealed V1 final equity differs from pre-registration")
    expected_trades = sealed.get("expected_primary_trade_count")
    try:
        actual_trades = int(cast(str, matches[0].get("trade_count")))
    except (TypeError, ValueError) as exc:
        raise HistoricalCounterfactualError("sealed V1 trade count is invalid") from exc
    if actual_trades != expected_trades:
        raise HistoricalCounterfactualError("sealed V1 trade count differs from pre-registration")

    with (parent / "risk_events.csv").open(encoding="utf-8", newline="") as source:
        disarms = [
            row
            for row in csv.DictReader(source)
            if row.get("event_type") == RiskEventType.DRAWDOWN_DISARMED.value
        ]
    expected_disarm_date = sealed.get("expected_disarm_date_utc")
    if not disarms or disarms[0]["observed_at_utc"][:10] != expected_disarm_date:
        raise HistoricalCounterfactualError("sealed V1 disarm date differs from pre-registration")
    return _VerifiedParent(
        path=parent,
        checksums_sha256=checksums_sha,
        artifacts=artifacts,
        summary=summary,
        manifest=manifest,
    )


def _default_code_identity_paths(repository_root: Path) -> tuple[Path, ...]:
    package = Path(__file__).resolve().parent
    return (
        package / "backtest.py",
        package / "domain.py",
        package / "historical_data.py",
        package / "historical_study.py",
        package / "historical_counterfactual.py",
        package / "research_charts.py",
        package / "research_metrics.py",
        package / "strategy.py",
        repository_root / "scripts" / "run_historical_counterfactual.py",
    )


def _scope_label(name: str) -> str:
    return OPENED_HOLDOUT_SCOPE if name == "frozen_holdout" else name


def _split_label(day: date, boundaries: Sequence[SplitBoundary]) -> str:
    return _scope_label(_split_for_day(day, boundaries))


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _boundary_slices(
    boundaries: Sequence[SplitBoundary],
) -> tuple[tuple[str, int, int], ...]:
    result: list[tuple[str, int, int]] = []
    start = 0
    for boundary in boundaries:
        result.append((_scope_label(boundary.name), start, boundary.observation_count))
        start += boundary.observation_count
    return tuple(result)


def _curve_slice(curve: Sequence[EquityPoint], start: int, size: int) -> tuple[EquityPoint, ...]:
    selected = tuple(curve[start : start + size + 1])
    if len(selected) != size + 1:
        raise HistoricalCounterfactualError("curve does not align with chronological split")
    return selected


def _trades_for_scope(
    trades: Sequence[Trade], scope: str, boundaries: Sequence[SplitBoundary]
) -> tuple[Trade, ...]:
    if scope == "full_evaluation":
        return tuple(trades)
    original = "frozen_holdout" if scope == OPENED_HOLDOUT_SCOPE else scope
    boundary = next(item for item in boundaries if item.name == original)
    return tuple(
        trade
        for trade in trades
        if boundary.first_day <= trade.execution_time.date() <= boundary.last_day
    )


def _write_metrics(
    path: Path, *, runs: Sequence[CurveRun], boundaries: Sequence[SplitBoundary]
) -> tuple[list[dict[str, object]], dict[tuple[str, str], ResearchMetrics]]:
    rows: list[list[object]] = []
    summary_rows: list[dict[str, object]] = []
    by_variant_scope: dict[tuple[str, str], ResearchMetrics] = {}
    roles = {
        "cad_cash": "benchmark",
        "fee_aware_btc_buy_and_hold": "benchmark",
        "sealed_frozen_v1_persistent_disarm": "sealed_reference",
        "no_drawdown_gate": "counterfactual",
        "mechanical_90d_trend_rearm": "counterfactual",
    }
    slices = _boundary_slices(boundaries)
    for run in runs:
        scopes: list[tuple[str, tuple[EquityPoint, ...]]] = [("full_evaluation", run.curve)]
        scopes.extend(
            (scope, _curve_slice(run.curve, start, size)) for scope, start, size in slices
        )
        for scope, curve in scopes:
            metrics = _study_metrics(
                curve,
                _trades_for_scope(run.trades, scope, boundaries),
                synthetic_trade_costs=(
                    run.synthetic_trade_costs if scope == "full_evaluation" else None
                ),
                include_cost_attribution=scope == "full_evaluation",
            )
            by_variant_scope[(run.name, scope)] = metrics
            values = _metric_dict(metrics)
            row = {
                "variant": run.name,
                "role": roles[run.name],
                "scope": scope,
                **values,
            }
            summary_rows.append(row)
            rows.append(
                [run.name, roles[run.name], scope, *(values[column] for column in METRIC_COLUMNS)]
            )
    _write_csv(path, ("variant", "role", "scope", *METRIC_COLUMNS), rows)
    return summary_rows, by_variant_scope


def _optional_delta(left: Decimal | None, right: Decimal | None) -> str:
    return "" if left is None or right is None else _decimal_text(left - right)


def _write_pairwise_deltas(
    path: Path,
    *,
    metrics: Mapping[tuple[str, str], ResearchMetrics],
) -> list[dict[str, object]]:
    rows: list[list[object]] = []
    summary: list[dict[str, object]] = []
    reference_name = "sealed_frozen_v1_persistent_disarm"
    scopes = ("full_evaluation", "development", "validation", OPENED_HOLDOUT_SCOPE)
    for variant in COUNTERFACTUAL_VARIANTS:
        for scope in scopes:
            candidate = metrics[(variant, scope)]
            reference = metrics[(reference_name, scope)]
            values: dict[str, object] = {
                "variant": variant,
                "reference_variant": reference_name,
                "scope": scope,
                "final_equity_delta_cad": _decimal_text(
                    candidate.final_equity - reference.final_equity
                ),
                "net_profit_delta_cad": _decimal_text(candidate.net_pnl - reference.net_pnl),
                "total_return_delta": _decimal_text(
                    candidate.total_return - reference.total_return
                ),
                "cagr_delta": _optional_delta(candidate.cagr, reference.cagr),
                "sharpe_delta": _optional_delta(candidate.sharpe, reference.sharpe),
                "max_drawdown_delta": _decimal_text(
                    candidate.max_drawdown - reference.max_drawdown
                ),
                "average_exposure_delta": _optional_delta(
                    candidate.exposure_fraction, reference.exposure_fraction
                ),
                "trade_count_delta": (
                    ""
                    if candidate.trade_count is None or reference.trade_count is None
                    else candidate.trade_count - reference.trade_count
                ),
            }
            summary.append(values)
            columns = tuple(values)
            rows.append([values[column] for column in columns])
    header = tuple(summary[0])
    _write_csv(path, header, rows)
    return summary


def _write_calendar_returns(path: Path, runs: Sequence[CurveRun]) -> None:
    rows: list[list[object]] = []
    for run in runs:
        metrics = _study_metrics(
            run.curve,
            run.trades,
            synthetic_trade_costs=run.synthetic_trade_costs,
        )
        for frequency, values in (
            ("year", metrics.calendar_year_returns),
            ("month", metrics.calendar_month_returns),
        ):
            rows.extend(
                [run.name, frequency, item.period, _decimal_text(item.return_fraction)]
                for item in values
            )
    _write_csv(
        path,
        ("variant", "frequency", "period_utc", "return_fraction"),
        rows,
    )


def _write_equity(
    path: Path, *, runs: Sequence[CurveRun], boundaries: Sequence[SplitBoundary]
) -> None:
    rows: list[list[object]] = []
    for run in runs:
        for index, point in enumerate(run.curve):
            split = (
                "shared_initial_boundary"
                if index == 0
                else _split_label((point.close_time - timedelta(microseconds=1)).date(), boundaries)
            )
            rows.append(
                [
                    run.name,
                    index,
                    _iso_z(point.close_time),
                    split,
                    _decimal_text(point.close),
                    _decimal_text(point.cash),
                    _decimal_text(point.btc),
                    _decimal_text(point.equity),
                    _decimal_text(point.btc_mark_value_cad),
                    _decimal_text(point.estimated_liquidation_fee_cad),
                    _decimal_text(point.estimated_liquidation_slippage_cad),
                    _decimal_text(point.cumulative_fees),
                ]
            )
    _write_csv(
        path,
        (
            "variant",
            "observation_index",
            "observed_at_utc",
            "split",
            "reference_price_cad",
            "cash_cad",
            "btc",
            "equity_cad",
            "btc_mark_value_cad",
            "estimated_liquidation_fee_cad",
            "estimated_liquidation_slippage_cad",
            "cumulative_fees_cad",
        ),
        rows,
    )


def _risk_epoch_at(result: BacktestResult, observed_at: datetime) -> int:
    epoch = 1
    for event in result.risk_events:
        if event.observed_at > observed_at:
            break
        if event.event_type is RiskEventType.DRAWDOWN_REARMED:
            epoch = event.risk_epoch
    return epoch


def _audit_record_id(
    *, variant: str, record_type: str, engine_record_id: str, risk_epoch: int
) -> str:
    digest = _sha256_bytes(
        _canonical_json_bytes(
            {
                "variant": variant,
                "record_type": record_type,
                "engine_record_id": engine_record_id,
                "risk_epoch": risk_epoch,
            }
        )
    )
    return f"audit_{digest[:24]}"


def _write_decisions(
    path: Path,
    *,
    results: Mapping[str, BacktestResult],
    boundaries: Sequence[SplitBoundary],
) -> None:
    rows: list[list[object]] = []
    for variant in ENGINE_VARIANTS:
        for item in results[variant].decisions:
            risk_epoch = _risk_epoch_at(results[variant], item.decision_time)
            rows.append(
                [
                    variant,
                    risk_epoch,
                    _audit_record_id(
                        variant=variant,
                        record_type="decision",
                        engine_record_id=item.intent_id,
                        risk_epoch=risk_epoch,
                    ),
                    item.intent_id,
                    item.strategy_decision_id,
                    _iso_z(item.signal_time) if item.signal_time else "",
                    _iso_z(item.decision_time),
                    _iso_z(item.execution_time),
                    _split_label(item.decision_time.date(), boundaries),
                    item.strategy_reason,
                    _decimal_text(item.target_weight),
                    _decimal_text(item.pre_trade_equity),
                    _decimal_text(item.requested_delta_cad),
                    _decimal_text(item.rebalance_band_cad),
                    item.outcome.value,
                    item.trade_id or "",
                    _decimal_text(item.remaining_btc) if item.remaining_btc is not None else "",
                ]
            )
    _write_csv(
        path,
        (
            "variant",
            "risk_epoch",
            "audit_record_id",
            "intent_id",
            "strategy_decision_id",
            "signal_close_utc",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "strategy_reason",
            "target_weight",
            "pre_trade_equity_cad",
            "requested_delta_cad",
            "rebalance_band_cad",
            "outcome",
            "trade_id",
            "remaining_btc_after_attempt",
        ),
        rows,
    )


def _write_fills(
    path: Path,
    *,
    results: Mapping[str, BacktestResult],
    boundaries: Sequence[SplitBoundary],
) -> None:
    rows: list[list[object]] = []
    for variant in ENGINE_VARIANTS:
        for trade in results[variant].trades:
            risk_epoch = _risk_epoch_at(results[variant], trade.decision_time)
            rows.append(
                [
                    variant,
                    risk_epoch,
                    _audit_record_id(
                        variant=variant,
                        record_type="fill",
                        engine_record_id=trade.trade_id,
                        risk_epoch=risk_epoch,
                    ),
                    trade.trade_id,
                    trade.intent_id,
                    trade.strategy_decision_id,
                    _iso_z(trade.decision_time),
                    _iso_z(trade.execution_time),
                    _split_label(trade.decision_time.date(), boundaries),
                    trade.side.value,
                    trade.liquidity.value,
                    _decimal_text(trade.quantity_btc),
                    (
                        _decimal_text(trade.execution_volume_btc)
                        if trade.execution_volume_btc is not None
                        else ""
                    ),
                    (
                        _decimal_text(trade.volume_participation_fraction)
                        if trade.volume_participation_fraction is not None
                        else ""
                    ),
                    trade.volume_cap_applied,
                    _decimal_text(trade.reference_price),
                    _decimal_text(trade.execution_price),
                    _decimal_text(trade.gross_notional_cad),
                    _decimal_text(trade.fee_cad),
                    _decimal_text(trade.slippage_cad),
                    _decimal_text(trade.cash_after),
                    _decimal_text(trade.btc_after),
                ]
            )
    _write_csv(
        path,
        (
            "variant",
            "risk_epoch",
            "audit_record_id",
            "trade_id",
            "intent_id",
            "strategy_decision_id",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "side",
            "liquidity",
            "quantity_btc",
            "execution_volume_btc",
            "volume_participation_fraction",
            "volume_cap_applied",
            "reference_price_cad",
            "execution_price_cad",
            "gross_notional_cad",
            "fee_cad",
            "slippage_cad",
            "cash_after_cad",
            "btc_after",
        ),
        rows,
    )


def _write_risk(
    path: Path,
    *,
    results: Mapping[str, BacktestResult],
    boundaries: Sequence[SplitBoundary],
) -> None:
    rows: list[list[object]] = []
    for variant in ENGINE_VARIANTS:
        for event in results[variant].risk_events:
            rows.append(
                [
                    variant,
                    event.risk_epoch,
                    _audit_record_id(
                        variant=variant,
                        record_type="risk_event",
                        engine_record_id=event.event_id,
                        risk_epoch=event.risk_epoch,
                    ),
                    event.event_id,
                    event.event_type.value,
                    _iso_z(event.observed_at),
                    _split_label(event.observed_at.date(), boundaries),
                    event.strategy_decision_id or "",
                    _decimal_text(event.equity),
                    _decimal_text(event.reference_equity),
                    _decimal_text(event.observed_fraction),
                    _decimal_text(event.threshold),
                    event.action,
                ]
            )
    _write_csv(
        path,
        (
            "variant",
            "risk_epoch",
            "audit_record_id",
            "event_id",
            "event_type",
            "observed_at_utc",
            "split",
            "strategy_decision_id",
            "equity_cad",
            "reference_equity_cad",
            "observed_fraction",
            "threshold",
            "action",
        ),
        rows,
    )


def _write_buy_and_hold_entries(
    path: Path,
    *,
    attempts: Sequence[BuyAndHoldEntryAttempt],
    boundaries: Sequence[SplitBoundary],
) -> None:
    rows = [
        [
            "fee_aware_btc_buy_and_hold",
            index,
            _iso_z(attempt.decision_time),
            _iso_z(attempt.execution_time),
            _split_label(attempt.decision_time.date(), boundaries),
            _decimal_text(attempt.reference_price_cad),
            _decimal_text(attempt.execution_price_cad),
            _decimal_text(attempt.execution_volume_btc),
            _decimal_text(attempt.maximum_participation_fraction),
            _decimal_text(attempt.maximum_participating_quantity_btc),
            _decimal_text(attempt.affordable_quantity_btc),
            _decimal_text(attempt.executed_quantity_btc),
            _decimal_text(attempt.executed_quantity_btc / attempt.execution_volume_btc),
            attempt.volume_cap_applied,
            _decimal_text(attempt.gross_notional_cad),
            _decimal_text(attempt.fee_cad),
            _decimal_text(attempt.slippage_cad),
            _decimal_text(attempt.cash_after_cad),
            _decimal_text(attempt.btc_after),
            attempt.outcome,
        ]
        for index, attempt in enumerate(attempts, start=1)
    ]
    _write_csv(
        path,
        (
            "variant",
            "attempt_index",
            "decision_time_utc",
            "execution_time_utc",
            "split",
            "reference_price_cad",
            "execution_price_cad",
            "execution_volume_btc",
            "maximum_participation_fraction",
            "maximum_participating_quantity_btc",
            "affordable_quantity_btc",
            "executed_quantity_btc",
            "volume_participation_fraction",
            "volume_cap_applied",
            "gross_notional_cad",
            "fee_cad",
            "slippage_cad",
            "cash_after_cad",
            "btc_after",
            "outcome",
        ),
        rows,
    )


def _drawdown_points(curve: Sequence[EquityPoint]) -> tuple[ChartPoint, ...]:
    peak = curve[0].equity
    points: list[ChartPoint] = []
    for point in curve:
        peak = max(peak, point.equity)
        points.append(ChartPoint(point.close_time, point.equity / peak - Decimal("1")))
    return tuple(points)


def _risk_state_points(
    curve: Sequence[EquityPoint], events: Sequence[RiskEvent]
) -> tuple[ChartPoint, ...]:
    ordered = tuple(sorted(events, key=lambda item: item.observed_at))
    event_index = 0
    armed = Decimal("1")
    points: list[ChartPoint] = []
    for point in curve:
        while event_index < len(ordered) and ordered[event_index].observed_at <= point.close_time:
            event = ordered[event_index]
            if event.event_type is RiskEventType.DRAWDOWN_DISARMED:
                armed = Decimal("0")
            elif event.event_type is RiskEventType.DRAWDOWN_REARMED:
                armed = Decimal("1")
            event_index += 1
        points.append(ChartPoint(point.close_time, armed))
    return tuple(points)


def _write_charts(
    output_dir: Path,
    *,
    runs: Mapping[str, CurveRun],
    results: Mapping[str, BacktestResult],
) -> tuple[Path, ...]:
    colors = {
        "cad_cash": "#9fb0c6",
        "fee_aware_btc_buy_and_hold": "#60a5fa",
        "sealed_frozen_v1_persistent_disarm": "#2dd4bf",
        "no_drawdown_gate": "#fbbf24",
        "mechanical_90d_trend_rearm": "#c084fc",
    }
    labels = {
        "cad_cash": "CAD cash",
        "fee_aware_btc_buy_and_hold": "BTC buy & hold",
        "sealed_frozen_v1_persistent_disarm": "Sealed V1",
        "no_drawdown_gate": "No drawdown gate",
        "mechanical_90d_trend_rearm": "90d mechanical rearm",
    }
    charts = output_dir / "charts"
    equity = write_line_chart(
        charts / "equity.svg",
        title="BTC/CAD drawdown-policy counterfactual equity",
        subtitle="Opened V1 holdout; strategy variants only, with identical causal assumptions",
        series=tuple(
            ChartSeries(
                labels[name],
                colors[name],
                tuple(ChartPoint(point.close_time, point.equity) for point in runs[name].curve),
            )
            for name in ENGINE_VARIANTS
        ),
        y_label="Liquidation equity (CAD)",
    )
    benchmark_equity = write_line_chart(
        charts / "benchmark_equity.svg",
        title="BTC/CAD strategy and passive benchmarks",
        subtitle="Shared C$1,000 boundary; fee-aware BTC accumulation shown separately",
        series=tuple(
            ChartSeries(
                labels[name],
                colors[name],
                tuple(ChartPoint(point.close_time, point.equity) for point in runs[name].curve),
            )
            for name in (
                "sealed_frozen_v1_persistent_disarm",
                "fee_aware_btc_buy_and_hold",
                "cad_cash",
            )
        ),
        y_label="Liquidation equity (CAD)",
    )
    drawdown = write_line_chart(
        charts / "drawdown.svg",
        title="Counterfactual historical drawdown",
        subtitle="Peak-to-current decline; exploratory after the V1 holdout was opened",
        series=tuple(
            ChartSeries(labels[name], colors[name], _drawdown_points(runs[name].curve))
            for name in VARIANT_ORDER[1:]
        ),
        y_label="Drawdown",
        percent_axis=True,
    )
    reference = runs["sealed_frozen_v1_persistent_disarm"].curve
    delta_series: list[ChartSeries] = []
    for name in COUNTERFACTUAL_VARIANTS:
        candidate = runs[name].curve
        if tuple(point.close_time for point in candidate) != tuple(
            point.close_time for point in reference
        ):
            raise HistoricalCounterfactualError("counterfactual curve clocks do not align")
        delta_series.append(
            ChartSeries(
                labels[name],
                colors[name],
                tuple(
                    ChartPoint(point.close_time, point.equity - baseline.equity)
                    for point, baseline in zip(candidate, reference, strict=True)
                ),
            )
        )
    delta = write_line_chart(
        charts / "delta_vs_frozen_v1.svg",
        title="Equity delta versus sealed V1",
        subtitle="Positive values mean more liquidation equity than persistent disarm",
        series=tuple(delta_series),
        y_label="Equity delta (CAD)",
    )
    risk_state = write_line_chart(
        charts / "risk_state.svg",
        title="Drawdown risk-state timeline",
        subtitle="1 = armed; 0 = drawdown-disarmed; disabled variant remains armed",
        series=tuple(
            ChartSeries(
                labels[name],
                colors[name],
                _risk_state_points(runs[name].curve, results[name].risk_events),
            )
            for name in ENGINE_VARIANTS
        ),
        y_label="Risk state",
    )
    return equity, benchmark_equity, drawdown, delta, risk_state


def _read_csv_rows(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    try:
        with path.open(encoding="utf-8", newline="") as source:
            rows = tuple(tuple(row) for row in csv.reader(source))
    except OSError as exc:
        raise HistoricalCounterfactualError(
            f"cannot read CSV for V1 replay check: {path.name}"
        ) from exc
    if not rows:
        raise HistoricalCounterfactualError(f"V1 replay CSV is empty: {path.name}")
    return rows[0], rows[1:]


def _verify_persistent_replay(
    result: BacktestResult,
    *,
    parent: _VerifiedParent,
    boundaries: Sequence[SplitBoundary],
) -> None:
    """Fail unless the production replay exactly reproduces V1 economic CSV rows."""

    with TemporaryDirectory(prefix="kraken-knight-v1-replay-") as temporary:
        root = Path(temporary)
        _write_v1_decisions(root / "decisions.csv", result, boundaries)
        _write_v1_fills(root / "fills.csv", result.trades, boundaries)
        _write_v1_risk(root / "risk_events.csv", result.risk_events, boundaries)
        _write_v1_equity(
            root / "daily_equity.csv",
            runs=(CurveRun("frozen_v1", result.equity_curve, result.trades, result),),
            boundaries=boundaries,
        )
        for name in ("decisions.csv", "fills.csv", "risk_events.csv"):
            if (root / name).read_bytes() != (parent.path / name).read_bytes():
                raise HistoricalCounterfactualError(
                    f"production persistent replay does not exactly reproduce sealed V1 {name}"
                )
        actual_header, actual_rows = _read_csv_rows(root / "daily_equity.csv")
        parent_header, parent_rows = _read_csv_rows(parent.path / "daily_equity.csv")
        selected_parent_rows = tuple(row for row in parent_rows if row and row[0] == "frozen_v1")
        if actual_header != parent_header or actual_rows != selected_parent_rows:
            raise HistoricalCounterfactualError(
                "production persistent replay does not exactly reproduce sealed V1 daily equity"
            )


def _verify_buy_and_hold_reconstruction(
    curve: Sequence[EquityPoint],
    attempts: Sequence[BuyAndHoldEntryAttempt],
    *,
    parent: _VerifiedParent,
    boundaries: Sequence[SplitBoundary],
) -> None:
    with TemporaryDirectory(prefix="kraken-knight-buy-hold-replay-") as temporary:
        root = Path(temporary)
        _write_v1_buy_and_hold_entries(root / "buy_and_hold_entries.csv", attempts, boundaries)
        if (root / "buy_and_hold_entries.csv").read_bytes() != (
            parent.path / "buy_and_hold_entries.csv"
        ).read_bytes():
            raise HistoricalCounterfactualError(
                "fee-aware buy-and-hold entries do not exactly reproduce sealed V1"
            )
        _write_v1_equity(
            root / "daily_equity.csv",
            runs=(CurveRun("fee_aware_btc_buy_and_hold", tuple(curve), (), None),),
            boundaries=boundaries,
        )
        actual_header, actual_rows = _read_csv_rows(root / "daily_equity.csv")
        parent_header, parent_rows = _read_csv_rows(parent.path / "daily_equity.csv")
        selected_parent_rows = tuple(
            row for row in parent_rows if row and row[0] == "fee_aware_btc_buy_and_hold"
        )
        if actual_header != parent_header or actual_rows != selected_parent_rows:
            raise HistoricalCounterfactualError(
                "fee-aware buy-and-hold equity does not exactly reproduce sealed V1"
            )


def _report_text(
    *,
    loaded: _LoadedCounterfactualConfig,
    git_state: GitState,
    research_validated: bool,
    input_hash: str,
    clean_hash: str,
    selected: Sequence[DailyTradeBar],
    boundaries: Sequence[SplitBoundary],
    metrics: Mapping[tuple[str, str], ResearchMetrics],
) -> str:
    lines = "\n".join(
        f"- {_scope_label(boundary.name)}: {boundary.first_day.isoformat()} through "
        f"{boundary.last_day.isoformat()} ({boundary.observation_count} days)"
        for boundary in boundaries
    )
    status = "ENGINEERING_VALIDATED" if research_validated else "RESEARCH_INVALIDATED"
    result_lines: list[str] = []
    for name in VARIANT_ORDER:
        item = metrics[(name, "full_evaluation")]
        result_lines.append(
            f"- {name}: final equity C${_decimal_text(item.final_equity)}; "
            f"return {_decimal_text(item.total_return)}; maximum drawdown "
            f"{_decimal_text(item.max_drawdown)}; trades {item.trade_count}"
        )
    results = "\n".join(result_lines)
    return f"""# BTC/CAD drawdown-policy counterfactual

## Interpretation first

This is an **exploratory historical counterfactual after the V1 holdout was opened**. It is not
fresh out-of-sample evidence, does not establish future profitability, and authorizes neither
paper nor live trading. Development, validation, and `opened_v1_holdout` labels are descriptive
time partitions only.

## Engineering status

- Status: **{status}**
- Production persistent-mode replay exactly matched the sealed V1 daily equity, decisions,
  fills, and risk-event records before either counterfactual was evaluated.
- Git commit: `{git_state.commit}`
- V2 pre-registration SHA-256: `{loaded.sha256}`
- Base V1 protocol SHA-256: `{loaded.base_sha256}`
- Input-data SHA-256: `{input_hash}`
- Selected clean-sequence SHA-256: `{clean_hash}`
- Parameter optimization, robustness search, and cost sweeping performed: no

## Dataset and descriptive partitions

- Selected history: {selected[0].day.isoformat()} through {selected[-1].day.isoformat()}
- Common information-only warm-up: {WARMUP_DAYS} days

{lines}

## Full-period historical outputs

{results}

## Frozen changes

- `no_drawdown_gate`: the 20% drawdown gate, disarm, and forced liquidation are disabled.
- `mechanical_90d_trend_rearm`: the same 20% disarm and liquidation remain; after 90 calendar
  days, a new causal long signal mechanically rearms the strategy and begins a new high-water
  risk epoch.
- Signal, sizing, BTC/CAD data, causal execution reference, exchange minimums, participation
  cap, fee, slippage, rolling-loss gate, and all other portfolio assumptions remain V1-identical.

## Audit artifacts

`pairwise_deltas.csv` compares each counterfactual directly with sealed V1. Every graph has a
machine-readable CSV source. `checksums.sha256` binds the report, CSVs, summary, and SVGs;
`manifest.json` records the code, parent publication, configuration, and data identities.
"""


def run_historical_counterfactual(
    bars: Sequence[DailyTradeBar],
    *,
    preregistration_path: Path,
    output_dir: Path,
    repository_root: Path,
    normalized_manifest_path: Path,
    source_data_filename: str,
    expected_commit: str,
    expected_preregistration_sha256: str,
    allow_dirty: bool = False,
    source_data_sha256: str | None = None,
    _code_identity_paths: Sequence[Path] | None = None,
    _expected_frozen_data_identity: Mapping[str, object] | None = None,
) -> CounterfactualStudyResult:
    """Run the frozen V2 counterfactual and write an immutable audit bundle."""

    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise HistoricalCounterfactualError("output_dir must be absent or empty")
    repository_root = repository_root.resolve()
    git_state = _git_state(repository_root, allow_dirty=allow_dirty)
    _validate_commit(expected_commit)
    if git_state.commit != expected_commit:
        raise HistoricalCounterfactualError("Git HEAD does not match expected_commit")
    if _expected_frozen_data_identity is not None and not allow_dirty:
        raise HistoricalCounterfactualError(
            "a synthetic frozen-data identity override requires allow_dirty=True"
        )
    data_identity_override_used = _expected_frozen_data_identity is not None
    loaded = _load_counterfactual_config(
        preregistration_path,
        repository_root=repository_root,
        expected_data_identity=_expected_frozen_data_identity or FROZEN_DATA_IDENTITY,
    )
    _validate_sha256(
        expected_preregistration_sha256,
        field="expected_preregistration_sha256",
    )
    if loaded.sha256 != expected_preregistration_sha256:
        raise HistoricalCounterfactualError(
            "counterfactual config does not match expected_preregistration_sha256"
        )
    code_identity_validated, code_file_hashes = _code_identity(
        repository_root,
        paths=(
            preregistration_path,
            loaded.base_path,
            repository_root / "pyproject.toml",
            repository_root / "uv.lock",
            *(_code_identity_paths or _default_code_identity_paths(repository_root)),
        ),
        allow_dirty=allow_dirty,
    )
    parent = _verify_parent_bundle(loaded)

    input_bars = tuple(bars)
    input_hash = source_data_sha256 or _bars_hash(input_bars)
    _validate_sha256(input_hash, field="source_data_sha256")
    if not source_data_filename or Path(source_data_filename).name != source_data_filename:
        raise HistoricalCounterfactualError("source_data_filename must be one plain filename")
    selected = select_longest_contiguous_sequence(input_bars)
    if len(selected) <= WARMUP_DAYS + 4:
        raise HistoricalCounterfactualError(
            "selected clean history needs 250 warm-up days plus split evidence"
        )
    clean_hash = _bars_hash(selected)
    base_dataset = _mapping(loaded.base_document.get("dataset"), field="base dataset")
    base_execution = _mapping(loaded.base_document.get("execution"), field="base execution")
    normalized_manifest_sha = _normalized_manifest_hash(
        normalized_manifest_path,
        expected_data_sha256=input_hash,
        expected_filename=source_data_filename,
        expected_row_count=len(input_bars),
        expected_first_date=input_bars[0].day,
        expected_last_date=input_bars[-1].day,
        expected_dataset=base_dataset,
        expected_execution=base_execution,
    )
    frozen_data = _mapping(
        loaded.document.get("frozen_data_identity"), field="frozen_data_identity"
    )
    if (
        source_data_filename != frozen_data.get("normalized_csv_filename")
        or input_hash != frozen_data.get("normalized_csv_sha256")
        or normalized_manifest_path.name != frozen_data.get("normalized_manifest_filename")
        or normalized_manifest_sha != frozen_data.get("normalized_manifest_sha256")
        or clean_hash != frozen_data.get("selected_clean_sequence_sha256")
    ):
        raise HistoricalCounterfactualError(
            "supplied normalized CSV, manifest, or clean sequence differs from frozen identity"
        )
    parent_hashes = _mapping(parent.summary.get("hashes"), field="sealed summary hashes")
    parent_manifest_data = _mapping(parent.manifest.get("data"), field="sealed manifest data")
    parent_normalized = _mapping(
        parent_manifest_data.get("normalized_manifest"),
        field="sealed normalized manifest identity",
    )
    if (
        parent_hashes.get("input_data_sha256") != input_hash
        or parent_hashes.get("selected_clean_data_sha256") != clean_hash
        or parent_hashes.get("normalized_manifest_sha256") != normalized_manifest_sha
        or parent_manifest_data.get("input_sha256") != input_hash
        or parent_manifest_data.get("selected_clean_sha256") != clean_hash
        or parent_normalized.get("filename") != normalized_manifest_path.name
        or parent_normalized.get("sha256") != normalized_manifest_sha
    ):
        raise HistoricalCounterfactualError(
            "supplied source data or normalized manifest is not identical to sealed V1"
        )

    candles, references = bars_to_causal_inputs(selected)
    evaluation_bars = selected[WARMUP_DAYS:]
    boundaries = _split_boundaries(evaluation_bars)
    evaluation_start = candles[WARMUP_DAYS - 1].close_time + timedelta(minutes=15)
    primary_case = next(
        case for case in _cost_cases(loaded.base_document) if case.name == "taker_taker_plus_10bps"
    )
    base_config = _backtest_config(loaded.base_document, primary_case)
    if base_config.drawdown_policy_mode is not DrawdownPolicyMode.PERSISTENT:
        raise HistoricalCounterfactualError("base V1 replay is not in persistent drawdown mode")
    if base_config.max_drawdown_threshold != Decimal("0.20"):
        raise HistoricalCounterfactualError("base V1 maximum drawdown threshold is not 20%")
    base_portfolio = _mapping(loaded.base_document.get("portfolio"), field="base portfolio")
    if base_portfolio.get("automatic_rearm") is not False:
        raise HistoricalCounterfactualError("base V1 automatic rearm must remain false")
    configs: dict[str, BacktestConfig] = {
        "sealed_frozen_v1_persistent_disarm": base_config,
        "no_drawdown_gate": replace(base_config, drawdown_policy_mode=DrawdownPolicyMode.DISABLED),
        "mechanical_90d_trend_rearm": replace(
            base_config,
            drawdown_policy_mode=DrawdownPolicyMode.COOLDOWN_REARM,
            drawdown_rearm_cooldown_days=90,
        ),
    }
    allowed_config_changes = {"drawdown_policy_mode", "drawdown_rearm_cooldown_days"}
    base_config_fields = asdict(base_config)
    for name, config in configs.items():
        differences = {
            field for field, value in asdict(config).items() if value != base_config_fields[field]
        }
        if not differences.issubset(allowed_config_changes):
            raise HistoricalCounterfactualError(
                f"{name} changes non-drawdown BacktestConfig fields: {sorted(differences)}"
            )
    strategy = _selected_strategy(loaded.base_document, windows=SELECTED_POLICY)
    last_decision = candles[-2].close_time + timedelta(minutes=15)
    selected_references = tuple(
        reference for reference in references if reference.decision_time <= last_decision
    )
    persistent = run_backtest(
        candles,
        strategy,
        base_config,
        execution_references=selected_references,
        evaluation_start=evaluation_start,
    )
    expected_decisions = len(evaluation_bars)
    if len(persistent.decisions) != expected_decisions:
        raise HistoricalCounterfactualError("persistent replay does not align with evaluation days")
    _verify_persistent_replay(
        persistent,
        parent=parent,
        boundaries=boundaries,
    )
    buy_hold_curve, buy_hold_costs, buy_hold_status, buy_hold_attempts = _buy_and_hold_curve(
        template=persistent.equity_curve,
        evaluation_start=evaluation_start,
        references=references,
        config=base_config,
    )
    _verify_buy_and_hold_reconstruction(
        buy_hold_curve,
        buy_hold_attempts,
        parent=parent,
        boundaries=boundaries,
    )
    results: dict[str, BacktestResult] = {"sealed_frozen_v1_persistent_disarm": persistent}
    for name in COUNTERFACTUAL_VARIANTS:
        results[name] = run_backtest(
            candles,
            strategy,
            configs[name],
            execution_references=selected_references,
            evaluation_start=evaluation_start,
        )
    if any(len(result.decisions) != expected_decisions for result in results.values()):
        raise HistoricalCounterfactualError("engine results do not align with evaluation days")
    shared_clock = tuple(point.close_time for point in persistent.equity_curve)
    if any(
        tuple(point.close_time for point in result.equity_curve) != shared_clock
        or result.equity_curve[0].equity != base_config.initial_cash
        for result in results.values()
    ):
        raise HistoricalCounterfactualError(
            "engine variants do not share the V1 clock and initial-capital boundary"
        )
    runs = (
        CurveRun("cad_cash", _cash_curve(persistent.equity_curve), (), None),
        CurveRun(
            "fee_aware_btc_buy_and_hold",
            buy_hold_curve,
            (),
            None,
            buy_hold_costs,
        ),
        *(
            CurveRun(name, results[name].equity_curve, results[name].trades, results[name])
            for name in ENGINE_VARIANTS
        ),
    )
    if tuple(run.name for run in runs) != VARIANT_ORDER:
        raise HistoricalCounterfactualError(
            "internal variant ordering differs from pre-registration"
        )
    if any(
        tuple(point.close_time for point in run.curve) != shared_clock
        or run.curve[0].equity != base_config.initial_cash
        for run in runs
    ):
        raise HistoricalCounterfactualError(
            "all strategy and benchmark curves must share the V1 clock and initial capital"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_rows, metrics_by_scope = _write_metrics(
        output_dir / "metrics.csv", runs=runs, boundaries=boundaries
    )
    delta_rows = _write_pairwise_deltas(
        output_dir / "pairwise_deltas.csv", metrics=metrics_by_scope
    )
    _write_calendar_returns(output_dir / "calendar_returns.csv", runs)
    _write_equity(output_dir / "daily_equity.csv", runs=runs, boundaries=boundaries)
    _write_decisions(output_dir / "decisions.csv", results=results, boundaries=boundaries)
    _write_fills(output_dir / "fills.csv", results=results, boundaries=boundaries)
    _write_risk(output_dir / "risk_events.csv", results=results, boundaries=boundaries)
    _write_buy_and_hold_entries(
        output_dir / "buy_and_hold_entries.csv",
        attempts=buy_hold_attempts,
        boundaries=boundaries,
    )
    run_map = {run.name: run for run in runs}
    chart_paths = _write_charts(output_dir, runs=run_map, results=results)
    research_validated = (
        not git_state.dirty and code_identity_validated and not data_identity_override_used
    )
    runtime = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
    summary: dict[str, object] = {
        "schema_version": COUNTERFACTUAL_STUDY_SCHEMA,
        "study_id": loaded.document["study_id"],
        "analysis_type": loaded.document["analysis_type"],
        "evidence_scope": EVIDENCE_SCOPE,
        "holdout_status": OPENED_HOLDOUT_SCOPE,
        "fresh_out_of_sample": False,
        "engineering_status": (
            "ENGINEERING_VALIDATED" if research_validated else "RESEARCH_INVALIDATED"
        ),
        "analysis_class": "post_holdout_exploratory",
        "prior_v1_holdout_opened": True,
        "confirmatory_evidence": False,
        "profitability_status": "PROFITABILITY_NOT_ESTABLISHED",
        "new_untouched_holdout_required_for_promotion": True,
        "live_trading_authorized": False,
        "paper_trading_authorized": False,
        "repository": asdict(git_state),
        "code_identity": {
            "matches_head": code_identity_validated,
            "expected_commit": expected_commit,
            "production_engine_only": True,
            "synthetic_data_identity_override_used": data_identity_override_used,
            "files": code_file_hashes,
        },
        "hashes": {
            "counterfactual_preregistration_sha256": loaded.sha256,
            "base_v1_preregistration_sha256": loaded.base_sha256,
            "input_data_sha256": input_hash,
            "selected_clean_data_sha256": clean_hash,
            "normalized_manifest_sha256": normalized_manifest_sha,
            "uv_lock_sha256": (
                _sha256_file(repository_root / "uv.lock")
                if (repository_root / "uv.lock").is_file()
                else None
            ),
        },
        "runtime": runtime,
        "parent_v1": {
            "publication_path": cast(
                str,
                _mapping(loaded.document.get("sealed_v1_reference"), field="sealed reference")[
                    "path"
                ],
            ),
            "manifest_sha256": _sha256_file(parent.path / "manifest.json"),
            "checksums_sha256": parent.checksums_sha256,
            "artifacts_verified": len(parent.artifacts),
            "production_replay_exact_match": True,
            "buy_and_hold_reconstruction_exact_match": True,
            "matched_economic_artifacts": [
                "daily_equity.csv:frozen_v1_rows",
                "decisions.csv",
                "fills.csv",
                "risk_events.csv",
                "daily_equity.csv:fee_aware_btc_buy_and_hold_rows",
                "buy_and_hold_entries.csv",
            ],
        },
        "dataset": {
            "selected_first_day": selected[0].day.isoformat(),
            "selected_last_day": selected[-1].day.isoformat(),
            "selected_day_count": len(selected),
            "discarded_day_count": len(input_bars) - len(selected),
            "warmup_day_count": WARMUP_DAYS,
            "evaluation_day_count": len(evaluation_bars),
            "same_as_sealed_v1": True,
            "synthetic_identity_override_used": data_identity_override_used,
        },
        "chronological_partitions": [
            {
                "name": _scope_label(boundary.name),
                "legacy_v1_name": boundary.name,
                "first_day": boundary.first_day.isoformat(),
                "last_day": boundary.last_day.isoformat(),
                "observation_count": boundary.observation_count,
                "fresh_holdout": False,
            }
            for boundary in boundaries
        ],
        "variant_order": list(VARIANT_ORDER),
        "counterfactual_variants": loaded.document["counterfactual_variants"],
        "invariants": {
            "same_data": True,
            "same_signal": True,
            "same_position_sizing": True,
            "same_causal_execution": True,
            "same_fees_slippage_volume_and_exchange_rules": True,
            "same_other_portfolio_and_risk_gates": True,
            "only_named_drawdown_mode_changed": True,
        },
        "optimization": {
            "performed": False,
            "robustness_grid_run": False,
            "cost_sweep_run": False,
            "variant_selection_performed": False,
        },
        "fee_aware_buy_and_hold": buy_hold_status,
        "metrics": metrics_rows,
        "pairwise_deltas_vs_sealed_v1": delta_rows,
    }
    summary_path = output_dir / "summary.json"
    _write_json(summary_path, summary)
    report_path = output_dir / "report.md"
    report_path.write_text(
        _report_text(
            loaded=loaded,
            git_state=git_state,
            research_validated=research_validated,
            input_hash=input_hash,
            clean_hash=clean_hash,
            selected=selected,
            boundaries=boundaries,
            metrics=metrics_by_scope,
        ),
        encoding="utf-8",
        newline="\n",
    )
    artifact_paths = sorted(
        (
            summary_path,
            output_dir / "metrics.csv",
            output_dir / "pairwise_deltas.csv",
            output_dir / "calendar_returns.csv",
            output_dir / "daily_equity.csv",
            output_dir / "decisions.csv",
            output_dir / "fills.csv",
            output_dir / "risk_events.csv",
            output_dir / "buy_and_hold_entries.csv",
            report_path,
            *chart_paths,
        ),
        key=lambda item: item.relative_to(output_dir).as_posix(),
    )
    artifact_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path) for path in artifact_paths
    }
    checksums_path = output_dir / "checksums.sha256"
    checksums_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in artifact_hashes.items()),
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schema_version": COUNTERFACTUAL_STUDY_SCHEMA,
        "study_id": loaded.document["study_id"],
        "evidence_scope": EVIDENCE_SCOPE,
        "repository": asdict(git_state),
        "code_identity": {
            "matches_head": code_identity_validated,
            "expected_commit": expected_commit,
            "synthetic_data_identity_override_used": data_identity_override_used,
            "files": code_file_hashes,
        },
        "runtime": runtime,
        "preregistration": {
            "filename": preregistration_path.name,
            "sha256": loaded.sha256,
        },
        "base_v1_protocol": {
            "filename": loaded.base_path.name,
            "sha256": loaded.base_sha256,
        },
        "parent_v1": {
            "checksums_filename": "checksums.sha256",
            "manifest_sha256": _sha256_file(parent.path / "manifest.json"),
            "checksums_sha256": parent.checksums_sha256,
            "production_replay_exact_match": True,
            "buy_and_hold_reconstruction_exact_match": True,
        },
        "data": {
            "input_filename": source_data_filename,
            "input_sha256": input_hash,
            "selected_clean_sha256": clean_hash,
            "normalized_manifest": {
                "filename": normalized_manifest_path.name,
                "sha256": normalized_manifest_sha,
            },
            "identical_to_sealed_v1": True,
        },
        "artifacts": artifact_hashes,
        "checksums_sha256": _sha256_file(checksums_path),
        "determinism": {
            "generated_at_omitted": True,
            "randomness_used": False,
            "parameter_optimization_performed": False,
            "counterfactuals_run_once_by_this_invocation": True,
        },
        "authorization": {"paper_trading": False, "live_trading": False},
    }
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    return CounterfactualStudyResult(
        output_dir=output_dir,
        summary_path=summary_path,
        manifest_path=manifest_path,
        checksums_path=checksums_path,
        config_sha256=loaded.sha256,
        base_config_sha256=loaded.base_sha256,
        input_data_sha256=input_hash,
        clean_data_sha256=clean_hash,
        git_state=git_state,
    )


def run_historical_counterfactual_from_csv(
    data_path: Path,
    *,
    preregistration_path: Path,
    output_dir: Path,
    repository_root: Path,
    normalized_manifest_path: Path,
    expected_commit: str,
    expected_preregistration_sha256: str,
    allow_dirty: bool = False,
) -> CounterfactualStudyResult:
    """Read the normalized CSV and bind its exact bytes into the V2 result."""

    return run_historical_counterfactual(
        load_daily_bars(data_path),
        preregistration_path=preregistration_path,
        output_dir=output_dir,
        repository_root=repository_root,
        normalized_manifest_path=normalized_manifest_path,
        source_data_filename=data_path.name,
        expected_commit=expected_commit,
        expected_preregistration_sha256=expected_preregistration_sha256,
        allow_dirty=allow_dirty,
        source_data_sha256=_sha256_file(data_path),
    )
