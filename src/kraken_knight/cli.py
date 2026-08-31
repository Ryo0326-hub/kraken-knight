"""Safe operational CLI for local state and authenticated reconciliation.

Checkpoint 2 adds a closed authenticated read path.  There is still no command
that constructs, submits, edits, or cancels a Kraken order.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from kraken_knight.config import ConfigError, Settings
from kraken_knight.kraken_read import KrakenReadError
from kraken_knight.ledger import Ledger, LedgerError
from kraken_knight.reconcile_job import (
    ReconciliationJobError,
    discover_read_only_account_id,
    execute_read_only_reconciliation,
    legacy_manifest_hash,
    load_legacy_hints,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(
        prog="kraken-knight",
        description="Initialize or inspect Kraken Knight local runtime state.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="create/verify the append-only SQLite ledger",
    )
    init_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = subparsers.add_parser(
        "status",
        help="show configuration and ledger status without exchange writes",
    )
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    account_id_parser = subparsers.add_parser(
        "account-id",
        help="reveal the public Kraken wallet ID after read-only access gates pass",
    )
    account_id_parser.add_argument("--json", action="store_true", dest="as_json")

    manifest_parser = subparsers.add_parser(
        "legacy-manifest",
        help="validate legacy hints and print their normalized SHA-256 digest",
    )
    manifest_parser.add_argument("--json", action="store_true", dest="as_json")
    manifest_parser.add_argument(
        "--legacy-hints",
        type=Path,
        help="restricted JSON file containing the five identified legacy submissions",
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="read and reconcile Kraken account state without exchange writes",
    )
    reconcile_parser.add_argument("--json", action="store_true", dest="as_json")
    reconcile_parser.add_argument(
        "--legacy-hints",
        type=Path,
        help="restricted JSON file containing non-authoritative legacy submission hints",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the local-only CLI and return a process exit code."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    exit_code = 0
    try:
        settings = Settings.from_env(environ)
        ledger = Ledger(settings.ledger_path)
        command = str(arguments.command)
        if command == "init":
            previous_umask = os.umask(0o077)
            try:
                ledger.initialize()
            finally:
                os.umask(previous_umask)
            payload = _status_payload(settings, ledger)
            payload["operation"] = "initialized"
        elif command == "status":
            payload = _status_payload(settings, ledger)
            payload["operation"] = "status"
        elif command == "account-id":
            previous_umask = os.umask(0o077)
            try:
                ledger.initialize()
            finally:
                os.umask(previous_umask)
            payload = discover_read_only_account_id(settings=settings, ledger=ledger)
            payload["operation"] = "account-id"
        elif command == "legacy-manifest":
            legacy_path = arguments.legacy_hints or settings.legacy_hints_path
            if legacy_path is None:
                raise ReconciliationJobError("a restricted legacy-hints path is required")
            hints = load_legacy_hints(legacy_path)
            payload = {
                "exchange_writes": False,
                "legacy_hint_count": len(hints),
                "legacy_manifest_hash": legacy_manifest_hash(hints),
                "operation": "legacy-manifest",
            }
        elif command == "reconcile":
            previous_umask = os.umask(0o077)
            try:
                ledger.initialize()
            finally:
                os.umask(previous_umask)
            legacy_path = arguments.legacy_hints or settings.legacy_hints_path
            hints = () if legacy_path is None else load_legacy_hints(legacy_path)
            payload = execute_read_only_reconciliation(
                settings=settings,
                ledger=ledger,
                legacy_hints=hints,
            )
            payload["operation"] = "reconcile"
            if payload["status"] != "CLEAN":
                exit_code = 3
        else:  # argparse enforces the choices; this protects future edits.
            parser.error("unknown command")
    except (
        ConfigError,
        KrakenReadError,
        LedgerError,
        OSError,
        ReconciliationJobError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        print(f"kraken-knight: {exc}", file=sys.stderr)
        return 2

    _emit(payload, as_json=bool(arguments.as_json))
    return exit_code


def _status_payload(settings: Settings, ledger: Ledger) -> dict[str, object]:
    return {
        "configuration": settings.safe_summary(),
        "exchange_writes": False,
        "ledger": ledger.status(),
    }


def _emit(payload: Mapping[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"operation={payload['operation']}")
    print("exchange_writes=false")
    if payload["operation"] == "account-id":
        for key in (
            "wallet_account_id",
            "read_only_profile_verified",
            "observed_at",
            "private_request_cost_spent",
        ):
            print(f"account_identity.{key}={payload[key]}")
        return
    if payload["operation"] == "legacy-manifest":
        print(f"legacy_manifest.hint_count={payload['legacy_hint_count']}")
        print(f"legacy_manifest.sha256={payload['legacy_manifest_hash']}")
        return
    if payload["operation"] == "reconcile":
        for key in (
            "status",
            "observed_at",
            "source_data_hash",
            "account_binding_hash",
            "legacy_hint_count",
            "private_request_cost_spent",
            "ledger_snapshot_id",
        ):
            print(f"reconciliation.{key}={payload[key]}")
        return
    configuration = payload["configuration"]
    ledger = payload["ledger"]
    if not isinstance(configuration, Mapping) or not isinstance(ledger, Mapping):
        raise TypeError("status payload is malformed")
    for key in (
        "mode",
        "pair",
        "strategy_id",
        "account_id",
        "ledger_path",
        "live_armed",
        "kraken_credentials_configured",
        "kraken_read_binding_configured",
        "legacy_hints_configured",
        "risk_fingerprint",
    ):
        print(f"configuration.{key}={configuration[key]}")
    for key in (
        "initialized",
        "schema_version",
        "journal_mode",
        "integrity",
        "decision_count",
        "intent_count",
        "reconciliation_count",
    ):
        print(f"ledger.{key}={ledger[key]}")
    if ledger["latest_decision"] is not None:
        print(
            "ledger.latest_decision="
            + json.dumps(ledger["latest_decision"], sort_keys=True, separators=(",", ":"))
        )
    if ledger["latest_reconciliation"] is not None:
        print(
            "ledger.latest_reconciliation="
            + json.dumps(
                ledger["latest_reconciliation"],
                sort_keys=True,
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
