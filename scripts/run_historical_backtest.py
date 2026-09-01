#!/usr/bin/env python3
"""Run the frozen, offline BTC/CAD historical study from normalized Kraken data."""

from __future__ import annotations

import argparse
from pathlib import Path

from kraken_knight.historical_study import run_historical_study_from_csv


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a deterministic causal BTC/CAD backtest bundle. This command does not "
            "accept Kraken credentials and cannot place orders."
        )
    )
    parser.add_argument("--data", required=True, type=Path, help="normalized daily-trade CSV")
    parser.add_argument(
        "--data-manifest",
        required=True,
        type=Path,
        help="normalized dataset provenance manifest to bind into the result",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("research/btc_cad_v1_backtest.json"),
        help="frozen pre-registration JSON",
    )
    parser.add_argument("--output", required=True, type=Path, help="new or empty artifact folder")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Git repository whose commit is bound into the result",
    )
    parser.add_argument(
        "--expected-commit",
        required=True,
        help="full 40-character Git commit frozen before revealing results",
    )
    parser.add_argument(
        "--expected-prereg-sha256",
        required=True,
        help="SHA-256 of the frozen pre-registration JSON",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="development only: permit, and prominently record, a dirty worktree",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    result = run_historical_study_from_csv(
        arguments.data,
        preregistration_path=arguments.config,
        output_dir=arguments.output,
        repository_root=arguments.repo,
        allow_dirty=arguments.allow_dirty,
        normalized_manifest_path=arguments.data_manifest,
        expected_commit=arguments.expected_commit,
        expected_preregistration_sha256=arguments.expected_prereg_sha256,
    )
    print(f"Historical study artifacts: {result.output_dir}")
    print(f"Git commit: {result.git_state.commit}")
    print(f"Clean data SHA-256: {result.clean_data_sha256}")
    if result.git_state.dirty_override_used:
        print("WARNING: dirty development override used; this is not a final holdout run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
