#!/usr/bin/env python3
"""Download and normalize credential-free Kraken BTC/CAD trade history."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from kraken_knight.historical_data import (
    TradeArchive,
    download_btc_cad_trades,
    write_normalized_dataset,
)


def _utc_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("cutoff must be YYYY-MM-DD") from exc
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resumably archive public Kraken BTC/CAD trades from since=0, then derive "
            "causal daily and post-00:15 execution data. No API key is accepted."
        )
    )
    parser.add_argument("--cutoff", required=True, type=_utc_cutoff)
    parser.add_argument("--raw-dir", required=True, type=Path)
    parser.add_argument("--normalized-csv", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--max-pages",
        type=int,
        help="optional bounded batch; rerun the identical command to resume",
    )
    parser.add_argument("--pace-seconds", type=float, default=1.05)
    parser.add_argument("--progress-every", type=int, default=25)
    return parser.parse_args()


def _progress(every: int):
    if every <= 0:
        raise ValueError("progress-every must be positive")

    def report(archive: TradeArchive) -> None:
        if archive.page_count == 1 or archive.page_count % every == 0 or archive.complete:
            last = archive.last_trade.opened_at.isoformat() if archive.last_trade else "none"
            print(
                f"pages={archive.page_count} unique_trades={archive.included_trade_count} "
                f"last_trade={last} complete={str(archive.complete).lower()}",
                flush=True,
            )

    return report


def main() -> int:
    arguments = _arguments()
    archive = download_btc_cad_trades(
        directory=arguments.raw_dir,
        cutoff=arguments.cutoff,
        page_size=1000,
        pace_seconds=arguments.pace_seconds,
        max_pages=arguments.max_pages,
        progress=_progress(arguments.progress_every),
    )
    if not archive.complete:
        print("Backfill batch saved. Rerun the same command to resume.")
        return 0

    dataset = write_normalized_dataset(
        archive,
        csv_path=arguments.normalized_csv,
        manifest_path=arguments.manifest,
    )
    print(f"Normalized CSV: {dataset.csv_path}")
    print(f"Normalized SHA-256: {dataset.csv_sha256}")
    print(f"Provenance manifest: {dataset.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
