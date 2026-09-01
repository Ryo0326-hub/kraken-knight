#!/usr/bin/env python3
"""Normalize an extracted official Kraken XBTCAD Time-and-Sales CSV member."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from kraken_knight.kraken_archive_csv import (
    KrakenArchiveSource,
    import_kraken_time_and_sales_csv,
)


def _utc_cutoff(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("cutoff must be YYYY-MM-DD") from exc


def _crc32(value: str) -> int:
    base = 16 if value.lower().startswith("0x") else 10
    try:
        parsed = int(value, base)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("zip member CRC32 must be decimal or 0x-prefixed") from exc
    if not 0 <= parsed <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("zip member CRC32 must fit in 32 bits")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and normalize an already-extracted XBTCAD.csv member from Kraken's "
            "official downloadable Time-and-Sales archive. No API key is accepted."
        )
    )
    parser.add_argument("--source-csv", required=True, type=Path)
    parser.add_argument("--cutoff", required=True, type=_utc_cutoff)
    parser.add_argument("--normalized-csv", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive-url", required=True)
    parser.add_argument("--archive-file-id", required=True)
    parser.add_argument("--entry-name", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--zip-member-crc32", type=_crc32)
    parser.add_argument("--zip-compressed-size-bytes", type=int)
    parser.add_argument("--zip-uncompressed-size-bytes", type=int)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    source = KrakenArchiveSource(
        archive_url=arguments.archive_url,
        archive_file_id=arguments.archive_file_id,
        entry_name=arguments.entry_name,
        expected_csv_sha256=arguments.expected_source_sha256,
        zip_member_crc32=arguments.zip_member_crc32,
        zip_compressed_size_bytes=arguments.zip_compressed_size_bytes,
        zip_uncompressed_size_bytes=arguments.zip_uncompressed_size_bytes,
    )
    dataset = import_kraken_time_and_sales_csv(
        arguments.source_csv,
        cutoff=arguments.cutoff,
        source=source,
        normalized_csv_path=arguments.normalized_csv,
        manifest_path=arguments.manifest,
    )
    print(f"Normalized CSV: {dataset.csv_path}")
    print(f"Normalized SHA-256: {dataset.csv_sha256}")
    print(f"Daily rows: {dataset.row_count}")
    print(f"Coverage: {dataset.first_date.isoformat()} through {dataset.last_date.isoformat()}")
    print(f"Missing UTC days preserved: {len(dataset.gap_dates)}")
    print(f"Provenance manifest: {dataset.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
