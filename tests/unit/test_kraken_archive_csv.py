from __future__ import annotations

import hashlib
import json
import zlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.historical_data import DATASET_SCHEMA
from kraken_knight.kraken_archive_csv import (
    ARCHIVE_SOURCE_METHOD,
    KRAKEN_TIME_AND_SALES_DOCUMENTATION_URL,
    KrakenArchiveCsvError,
    KrakenArchiveSource,
    import_kraken_time_and_sales_csv,
    iter_archive_daily_bars,
)


def _timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


def _source(path: Path, content: bytes, **overrides: object) -> KrakenArchiveSource:
    values: dict[str, object] = {
        "archive_url": "https://drive.google.com/file/d/test-archive/view",
        "archive_file_id": "test-archive",
        "entry_name": "TimeAndSales_Combined/XBTCAD.csv",
        "expected_csv_sha256": hashlib.sha256(content).hexdigest(),
        "zip_member_crc32": zlib.crc32(content),
        "zip_compressed_size_bytes": 123,
        "zip_uncompressed_size_bytes": len(content),
    }
    values.update(overrides)
    return KrakenArchiveSource(**values)  # type: ignore[arg-type]


def _write_source(path: Path, rows: list[tuple[int, str, str]]) -> bytes:
    content = "".join(f"{timestamp},{price},{volume}\n" for timestamp, price, volume in rows)
    raw = content.encode("utf-8")
    path.write_bytes(raw)
    return raw


def _valid_rows() -> list[tuple[int, str, str]]:
    return [
        (_timestamp("2023-01-01T00:00:00"), "100.00", "1"),
        (_timestamp("2023-01-01T00:15:02"), "110", "2"),
        (_timestamp("2023-01-01T00:15:50"), "120", "1"),
        (_timestamp("2023-01-01T00:16:00"), "999", ".5"),
        (_timestamp("2023-01-01T23:59:59"), "105", "1"),
        (_timestamp("2023-01-03T00:00:00"), "200", ".5"),
        (_timestamp("2023-01-03T00:19:05"), "210", ".25"),
        (_timestamp("2023-01-03T23:59:59"), "205", ".75"),
    ]


def test_import_writes_deterministic_daily_csv_and_honest_manifest(tmp_path: Path) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    content = _write_source(source_csv, _valid_rows())
    normalized = tmp_path / "out" / "daily.csv"
    manifest_path = tmp_path / "out" / "manifest.json"
    source = _source(source_csv, content)
    cutoff = datetime(2023, 1, 4, tzinfo=UTC)

    first = import_kraken_time_and_sales_csv(
        source_csv,
        cutoff=cutoff,
        source=source,
        normalized_csv_path=normalized,
        manifest_path=manifest_path,
    )
    first_csv = normalized.read_bytes()
    first_manifest = manifest_path.read_bytes()
    second = import_kraken_time_and_sales_csv(
        source_csv,
        cutoff=cutoff,
        source=source,
        normalized_csv_path=normalized,
        manifest_path=manifest_path,
    )

    assert second == first
    assert normalized.read_bytes() == first_csv
    assert manifest_path.read_bytes() == first_manifest
    assert first.row_count == 2
    assert first.gap_dates == (datetime(2023, 1, 2).date(),)
    assert first.csv_sha256 == hashlib.sha256(first_csv).hexdigest()
    assert first.manifest_sha256 == hashlib.sha256(first_manifest).hexdigest()
    rows = first_csv.decode().splitlines()
    assert rows[0] == (
        "date,open,high,low,close,volume_btc,trade_count,execution_minute_utc,"
        "execution_vwap_cad,execution_volume_btc,execution_trade_count"
    )
    assert rows[1] == (
        "2023-01-01,100,999,100,105,5.5,5,2023-01-01T00:15:00Z,"
        "113.3333333333333333333333333333333333333,3,2"
    )
    assert rows[2] == "2023-01-03,200,210,200,205,1.5,3,2023-01-03T00:19:00Z,210,0.25,1"

    manifest = json.loads(first_manifest)
    assert manifest["schema_version"] == DATASET_SCHEMA
    assert manifest["source"] == {
        "archive_entry_name": "TimeAndSales_Combined/XBTCAD.csv",
        "archive_file_id": "test-archive",
        "archive_url": "https://drive.google.com/file/d/test-archive/view",
        "cutoff_exclusive": "2023-01-04T00:00:00Z",
        "documentation_url": KRAKEN_TIME_AND_SALES_DOCUMENTATION_URL,
        "method": ARCHIVE_SOURCE_METHOD,
        "provider": "Kraken",
        "request_pair": "XBTCAD",
    }
    assert manifest["raw_archive"] == {
        "complete": True,
        "completeness_basis": (
            "all_rows_strictly_before_cutoff_and_last_observed_utc_day_is_cutoff_minus_one"
        ),
        "crc32": f"{zlib.crc32(content):08x}",
        "cutoff_exclusive": "2023-01-04T00:00:00Z",
        "entry_name": "TimeAndSales_Combined/XBTCAD.csv",
        "filename": "XBTCAD.csv",
        "included_trade_count": 8,
        "row_count": 8,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "zip_compressed_size_bytes": 123,
        "zip_uncompressed_size_bytes": len(content),
    }
    assert manifest["normalized_csv"]["gap_dates"] == ["2023-01-02"]
    assert manifest["normalized_csv"]["execution_reference"]["available_at"] == "minute_close"


def test_daily_iterator_uses_first_positive_execution_minute_and_preserves_order(
    tmp_path: Path,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    _write_source(source_csv, _valid_rows())

    bars = tuple(iter_archive_daily_bars(source_csv, cutoff=datetime(2023, 1, 4, tzinfo=UTC)))

    assert len(bars) == 2
    assert bars[0].open == Decimal("100.00")
    assert bars[0].high == Decimal("999")
    assert bars[0].low == Decimal("100.00")
    assert bars[0].close == Decimal("105")
    assert bars[0].trade_count == 5
    assert bars[0].execution_minute == datetime(2023, 1, 1, 0, 15, tzinfo=UTC)
    assert bars[0].execution_volume == Decimal("3")
    assert bars[0].execution_trade_count == 2
    assert bars[0].execution_vwap == Decimal("113.3333333333333333333333333333333333333")


def test_daily_iterator_accepts_scientific_notation_present_in_official_archive(
    tmp_path: Path,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    source_csv.write_text("1,319.0,3.12e-05\n", encoding="utf-8")

    bars = tuple(iter_archive_daily_bars(source_csv, cutoff=datetime(1970, 1, 2, tzinfo=UTC)))

    assert bars[0].volume == Decimal("3.12e-05")


def test_daily_iterator_does_not_treat_later_hour_as_midnight_window(
    tmp_path: Path,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    _write_source(
        source_csv,
        [
            (_timestamp("2023-01-01T01:15:00"), "100", "1"),
            (_timestamp("2023-01-01T23:59:59"), "101", "1"),
        ],
    )

    bars = tuple(iter_archive_daily_bars(source_csv, cutoff=datetime(2023, 1, 2, tzinfo=UTC)))

    assert bars[0].execution_minute is None
    assert bars[0].execution_vwap is None
    assert bars[0].execution_volume is None
    assert bars[0].execution_trade_count is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"1,2\n", "exactly three fields"),
        (b"timestamp,2,3\n", "timestamp must be integer UTC seconds"),
        (b"1,NaN,3\n", "price is not a decimal"),
        (b"1,2,0\n", "volume must be finite and positive"),
        (b"1,+2,3\n", "price is not a decimal"),
        (b"1,2,3,4\n", "exactly three fields"),
    ],
)
def test_daily_iterator_rejects_malformed_rows(
    tmp_path: Path,
    content: bytes,
    message: str,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    source_csv.write_bytes(content)

    with pytest.raises(KrakenArchiveCsvError, match=message):
        tuple(iter_archive_daily_bars(source_csv, cutoff=datetime(2023, 1, 1, tzinfo=UTC)))


def test_daily_iterator_rejects_unsorted_and_out_of_cutoff_rows(tmp_path: Path) -> None:
    cutoff = datetime(2023, 1, 4, tzinfo=UTC)
    source_csv = tmp_path / "XBTCAD.csv"
    _write_source(
        source_csv,
        [
            (_timestamp("2023-01-02T00:00:01"), "1", "1"),
            (_timestamp("2023-01-01T00:00:01"), "1", "1"),
        ],
    )
    with pytest.raises(KrakenArchiveCsvError, match="moved backward"):
        tuple(iter_archive_daily_bars(source_csv, cutoff=cutoff))

    _write_source(
        source_csv,
        [
            (_timestamp("2023-01-03T23:59:59"), "1", "1"),
            (_timestamp("2023-01-04T00:00:00"), "1", "1"),
        ],
    )
    with pytest.raises(KrakenArchiveCsvError, match="at or after the exclusive cutoff"):
        tuple(iter_archive_daily_bars(source_csv, cutoff=cutoff))


def test_import_rejects_archive_that_does_not_reach_cutoff_minus_one_day(
    tmp_path: Path,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    content = _write_source(
        source_csv,
        [(_timestamp("2023-01-02T23:59:59"), "1", "1")],
    )

    with pytest.raises(KrakenArchiveCsvError, match="does not reach the final UTC day"):
        import_kraken_time_and_sales_csv(
            source_csv,
            cutoff=datetime(2023, 1, 4, tzinfo=UTC),
            source=_source(source_csv, content),
            normalized_csv_path=tmp_path / "daily.csv",
            manifest_path=tmp_path / "manifest.json",
        )
    assert not (tmp_path / "daily.csv").exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_csv_sha256": "0" * 64}, "SHA-256"),
        ({"zip_member_crc32": 123}, "CRC32"),
        ({"zip_uncompressed_size_bytes": 123}, "size"),
    ],
)
def test_import_rejects_source_identity_mismatch(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    content = _write_source(source_csv, _valid_rows())

    with pytest.raises(KrakenArchiveCsvError, match=message):
        import_kraken_time_and_sales_csv(
            source_csv,
            cutoff=datetime(2023, 1, 4, tzinfo=UTC),
            source=_source(source_csv, content, **overrides),
            normalized_csv_path=tmp_path / "daily.csv",
            manifest_path=tmp_path / "manifest.json",
        )


@pytest.mark.parametrize(
    "source",
    [
        KrakenArchiveSource("http://example.test/a", "id", "XBTCAD.csv", "0" * 64),
        KrakenArchiveSource("https://example.test/a", "", "XBTCAD.csv", "0" * 64),
        KrakenArchiveSource("https://example.test/a", "id", "../XBTCAD.csv", "0" * 64),
        KrakenArchiveSource("https://example.test/a", "id", "ETHCAD.csv", "0" * 64),
        KrakenArchiveSource("https://example.test/a", "id", "XBTCAD.csv", "bad"),
        KrakenArchiveSource(
            "https://example.test/a", "id", "XBTCAD.csv", "0" * 64, zip_member_crc32=-1
        ),
    ],
)
def test_import_rejects_invalid_source_metadata(
    tmp_path: Path,
    source: KrakenArchiveSource,
) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    source_csv.write_text("1,2,3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        import_kraken_time_and_sales_csv(
            source_csv,
            cutoff=datetime(1970, 1, 2, tzinfo=UTC),
            source=source,
            normalized_csv_path=tmp_path / "daily.csv",
            manifest_path=tmp_path / "manifest.json",
        )


def test_import_rejects_overlapping_paths_and_non_midnight_cutoff(tmp_path: Path) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    content = _write_source(source_csv, _valid_rows())
    source = _source(source_csv, content)
    with pytest.raises(ValueError, match="must not overlap"):
        import_kraken_time_and_sales_csv(
            source_csv,
            cutoff=datetime(2023, 1, 4, tzinfo=UTC),
            source=source,
            normalized_csv_path=source_csv,
            manifest_path=tmp_path / "manifest.json",
        )
    with pytest.raises(ValueError, match="UTC midnight"):
        tuple(
            iter_archive_daily_bars(
                source_csv,
                cutoff=datetime(2023, 1, 4, 0, 1, tzinfo=UTC),
            )
        )


def test_import_rejects_empty_or_non_utf8_source(tmp_path: Path) -> None:
    source_csv = tmp_path / "XBTCAD.csv"
    source_csv.write_bytes(b"")
    with pytest.raises(KrakenArchiveCsvError, match="contains no trades"):
        import_kraken_time_and_sales_csv(
            source_csv,
            cutoff=datetime(2023, 1, 4, tzinfo=UTC),
            source=_source(source_csv, b""),
            normalized_csv_path=tmp_path / "daily.csv",
            manifest_path=tmp_path / "manifest.json",
        )

    source_csv.write_bytes(b"1,2,\xff\n")
    with pytest.raises(KrakenArchiveCsvError, match="not valid RFC 4180"):
        tuple(iter_archive_daily_bars(source_csv, cutoff=datetime(2023, 1, 4, tzinfo=UTC)))
