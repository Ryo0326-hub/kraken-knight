from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kraken_knight.provenance import canonical_json_bytes, sha256_file, sha256_json, verify_sha256


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    price: Decimal


def test_canonical_hash_is_independent_of_mapping_order() -> None:
    first = {"price": Decimal("100.00"), "pair": "BTC/CAD"}
    second = {"pair": "BTC/CAD", "price": Decimal("100.00")}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_json(first) == sha256_json(second)


def test_canonical_hash_supports_domain_dataclasses() -> None:
    value = Sample(datetime(2026, 8, 31, tzinfo=UTC), Decimal("109200.10"))

    assert canonical_json_bytes(value) == (
        b'{"price":"109200.10","timestamp":"2026-08-31T00:00:00+00:00"}'
    )


def test_canonical_hash_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        sha256_json({"timestamp": datetime(2026, 8, 31)})


def test_file_hash_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    path.write_bytes(b"timestamp,close\n1,2\n")

    digest = sha256_file(path)

    assert verify_sha256(path, digest)
    assert not verify_sha256(path, "0" * 64)


def test_verify_hash_rejects_invalid_digest(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    path.write_bytes(b"data")

    with pytest.raises(ValueError, match="64-character"):
        verify_sha256(path, "not-a-digest")
