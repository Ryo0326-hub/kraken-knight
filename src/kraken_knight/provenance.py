"""Deterministic content hashing for datasets, decisions, and reports."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path


def _json_default(value: object) -> object:
    """Convert supported domain values without losing decimal precision."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetimes cannot be hashed canonically")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a value with stable key ordering and representation."""

    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    """Return the SHA-256 digest of a canonical JSON representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it fully into memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    """Compare a file digest using constant-time string comparison."""

    normalized = expected.strip().lower()
    invalid_character = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or invalid_character:
        raise ValueError("expected must be a 64-character hexadecimal SHA-256 digest")
    return hmac.compare_digest(sha256_file(path), normalized)
