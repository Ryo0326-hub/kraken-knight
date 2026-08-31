import hashlib
import json
from datetime import UTC, datetime

import pytest

from kraken_knight.market_data import (
    KrakenPublicClient,
    MarketDataError,
    parse_ohlc_payload,
    validate_batch_freshness,
    validate_daily_sequence,
)


def _payload() -> dict[str, object]:
    return {
        "error": [],
        "result": {
            "XXBTZCAD": [
                [1788134400, "100", "112", "99", "110", "106", "2.5", 42],
                [1788220800, "110", "115", "108", "114", "112", "1.5", 30],
            ],
            "last": 1788220800,
        },
    }


def test_parse_ohlc_quarantines_mutable_tail() -> None:
    observed_at = datetime(2026, 9, 1, 0, 15, tzinfo=UTC)

    batch = parse_ohlc_payload(_payload(), observed_at=observed_at)

    assert batch.raw_pair_key == "XXBTZCAD"
    assert len(batch.completed) == 1
    assert batch.completed[0].complete is True
    assert batch.mutable_tail.complete is False
    assert batch.mutable_tail.close_time == datetime(2026, 9, 2, tzinfo=UTC)
    assert len(batch.raw_sha256) == 64


def test_batch_freshness_accepts_the_scheduled_current_snapshot() -> None:
    observed_at = datetime(2026, 9, 1, 0, 15, tzinfo=UTC)
    batch = parse_ohlc_payload(
        _payload(),
        observed_at=observed_at,
        expected_pair="XBTCAD",
    )

    validate_batch_freshness(batch, evaluated_at=observed_at)


@pytest.mark.parametrize(
    ("observed_at", "evaluated_at", "message"),
    [
        (
            datetime(2026, 9, 1, 0, 15, tzinfo=UTC),
            datetime(2026, 9, 1, 0, 14, tzinfo=UTC),
            "future",
        ),
        (
            datetime(2026, 9, 1, 0, 10, tzinfo=UTC),
            datetime(2026, 9, 1, 0, 15, 1, tzinfo=UTC),
            "observation is stale",
        ),
        (
            datetime(2026, 9, 1, 0, 14, tzinfo=UTC),
            datetime(2026, 9, 1, 0, 14, tzinfo=UTC),
            "completion delay",
        ),
        (
            datetime(2026, 9, 2, 0, 15, tzinfo=UTC),
            datetime(2026, 9, 2, 0, 15, tzinfo=UTC),
            "stale for this strategy date",
        ),
    ],
)
def test_batch_freshness_rejects_unsafe_timing(
    observed_at: datetime,
    evaluated_at: datetime,
    message: str,
) -> None:
    batch = parse_ohlc_payload(_payload(), observed_at=observed_at)

    with pytest.raises(MarketDataError, match=message):
        validate_batch_freshness(batch, evaluated_at=evaluated_at)


def test_parser_binds_the_response_to_requested_btc_cad_pair() -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    result["XETHZCAD"] = result.pop("XXBTZCAD")

    with pytest.raises(MarketDataError, match="does not match"):
        parse_ohlc_payload(
            payload,
            observed_at=datetime(2026, 9, 1, 0, 15, tzinfo=UTC),
            expected_pair="XBTCAD",
        )


def test_parse_ohlc_rejects_api_error() -> None:
    with pytest.raises(MarketDataError, match="Kraken API error"):
        parse_ohlc_payload(
            {"error": ["EGeneral:Invalid arguments"], "result": {}},
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_parse_ohlc_requires_aware_observation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_ohlc_payload(_payload(), observed_at=datetime(2026, 9, 1))


def test_validate_daily_sequence_can_reject_gap() -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    rows = result["XXBTZCAD"]
    assert isinstance(rows, list)
    second_row = rows[1]
    assert isinstance(second_row, list)
    second_row[0] = 1788307200
    batch = parse_ohlc_payload(payload, observed_at=datetime(2026, 9, 1, 0, 15, tzinfo=UTC))

    with pytest.raises(MarketDataError, match="gap"):
        validate_daily_sequence(
            (batch.completed[0], batch.mutable_tail),
            require_contiguous=True,
        )


def test_client_builds_safe_query_and_parses_response() -> None:
    calls: list[tuple[str, float]] = []

    def transport(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        return json.dumps(_payload()).encode()

    client = KrakenPublicClient(transport=transport, timeout_seconds=5)
    batch = client.fetch_daily_ohlc(
        pair="XBTCAD",
        since=123,
        observed_at=datetime(2026, 9, 1, 0, 15, tzinfo=UTC),
    )

    assert len(batch.completed) == 1
    assert batch.requested_pair == "XBTCAD"
    assert batch.raw_sha256 == hashlib.sha256(json.dumps(_payload()).encode()).hexdigest()
    assert calls == [
        (
            "https://api.kraken.com/0/public/OHLC?pair=XBTCAD&interval=1440&since=123",
            5,
        )
    ]


def test_client_rejects_invalid_json() -> None:
    client = KrakenPublicClient(transport=lambda _url, _timeout: b"not-json")

    with pytest.raises(MarketDataError, match="invalid JSON"):
        client.fetch_daily_ohlc(observed_at=datetime(2026, 9, 1, tzinfo=UTC))


def test_client_rejects_non_btc_cad_pair() -> None:
    client = KrakenPublicClient(transport=lambda _url, _timeout: b"{}")

    with pytest.raises(ValueError, match="frozen to BTC/CAD"):
        client.fetch_daily_ohlc(pair="XETHCAD")
