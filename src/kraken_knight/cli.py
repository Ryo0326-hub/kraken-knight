"""Safe operational CLI for configuration and ledger readiness.

Checkpoint 1 intentionally exposes only local initialization and read-only
status.  There is no command that constructs or submits a Kraken request.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence

from kraken_knight.config import ConfigError, Settings
from kraken_knight.ledger import Ledger, LedgerError


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
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the local-only CLI and return a process exit code."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
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
        else:  # argparse enforces the choices; this protects future edits.
            parser.error("unknown command")
    except (ConfigError, LedgerError, OSError, sqlite3.Error) as exc:
        print(f"kraken-knight: {exc}", file=sys.stderr)
        return 2

    _emit(payload, as_json=bool(arguments.as_json))
    return 0


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
    ):
        print(f"ledger.{key}={ledger[key]}")
    if ledger["latest_decision"] is not None:
        print(
            "ledger.latest_decision="
            + json.dumps(ledger["latest_decision"], sort_keys=True, separators=(",", ":"))
        )


if __name__ == "__main__":
    raise SystemExit(main())
