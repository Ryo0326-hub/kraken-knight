import base64
import csv
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

import kraken_knight.historical_data as historical_data
from kraken_knight.historical_data import (
    KRAKEN_TRADES_URL,
    RAW_ARCHIVE_FILENAME,
    STATE_FILENAME,
    HistoricalDataError,
    KrakenTradesApiError,
    RetryableHistoricalDataError,
    TradeArchive,
    download_btc_cad_trades,
    iter_daily_bars,
    parse_trades_bytes,
    parse_trades_payload,
    write_normalized_dataset,
)

CUTOFF = datetime(2023, 1, 4, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 9, 1, 1, tzinfo=UTC)


def _trade(
    price: str,
    volume: str,
    timestamp: int | float | Decimal,
    trade_id: int,
    *,
    side: str = "b",
    order_type: str = "l",
    misc: str = "",
) -> list[object]:
    return [price, volume, timestamp, side, order_type, misc, trade_id]


def _payload(
    rows: list[list[object]],
    cursor: str,
    *,
    pair: str = "XXBTZCAD",
) -> dict[str, object]:
    return {"error": [], "result": {pair: rows, "last": cursor}}


def _raw(rows: list[list[object]], cursor: str, *, pair: str = "XXBTZCAD") -> bytes:
    return json.dumps(_payload(rows, cursor, pair=pair), separators=(",", ":")).encode()


def _pages() -> tuple[bytes, bytes]:
    first = _raw(
        [
            _trade("100.00", "1.0", 1672531200.0, 1),
            _trade("110.00", "2.0", 1672532102.0, 2),
            _trade("130.00", "1.0", 1672532130.0, 3),
        ],
        "1672532130000000000",
    )
    second = _raw(
        [
            _trade("130.00", "1.0", 1672532130.0, 3),
            _trade("90.00", "3.0", 1672705080.0, 4),
            _trade("95.00", "1.0", 1672790400.0, 5),
        ],
        "1672790400000000100",
    )
    return first, second


def _download(tmp_path: Path) -> tuple[TradeArchive, list[str], list[float]]:
    pages = iter(_pages())
    urls: list[str] = []
    sleeps: list[float] = []

    def transport(url: str, timeout: float) -> bytes:
        assert timeout == 7
        urls.append(url)
        return next(pages)

    archive = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        pace_seconds=1.25,
        timeout_seconds=7,
        transport=transport,
        sleeper=sleeps.append,
        clock=lambda: RETRIEVED_AT,
    )
    return archive, urls, sleeps


def test_parse_trades_payload_preserves_decimal_evidence() -> None:
    page = parse_trades_bytes(
        _raw(
            [_trade("315.00000", "0.25000000", 1435548461.3168616, 1)],
            "1435548461316861697",
        ),
        request_cursor="0",
        page_size=1,
    )

    assert page.pair == "XXBTZCAD"
    assert page.next_cursor == "1435548461316861697"
    assert page.trades[0].price == Decimal("315.00000")
    assert page.trades[0].volume == Decimal("0.25000000")
    assert page.trades[0].opened_at.date().isoformat() == "2015-06-29"


@pytest.mark.parametrize(
    ("payload", "cursor", "message"),
    [
        ({"error": ["EGeneral:Invalid arguments"]}, "0", "Kraken API error"),
        ({"error": [1], "result": {}}, "0", "entries must be strings"),
        ({"error": [], "result": {"XETHZCAD": [], "last": "1"}}, "0", "not BTC/CAD"),
        (
            {"error": [], "result": {"XXBTZCAD": [], "XBTCAD": [], "last": "1"}},
            "0",
            "exactly one pair",
        ),
        (_payload([], "1") | {"extra": True}, "0", "exactly error and result"),
        (_payload([], "1"), "1", "did not advance"),
        (_payload([], "01"), "0", "canonical decimal"),
        (
            _payload(
                [
                    _trade("1", "1", Decimal("2"), 1),
                    _trade("1", "1", Decimal("1"), 2),
                ],
                "3",
            ),
            "0",
            "not timestamp ordered",
        ),
        (
            _payload(
                [
                    _trade("1", "1", Decimal("1"), 1),
                    _trade("1", "1", Decimal("2"), 1),
                ],
                "3",
            ),
            "0",
            "duplicate trade ids",
        ),
        (
            _payload([_trade("1", "1", Decimal("1"), 1)], "2000000000"),
            "3000000000",
            "did not advance",
        ),
        (
            _payload([_trade("1", "1", Decimal("4"), 1)], "3000000000"),
            "0",
            "precedes its final trade",
        ),
    ],
)
def test_parse_trades_payload_rejects_schema_and_progress_errors(
    payload: object,
    cursor: str,
    message: str,
) -> None:
    with pytest.raises(HistoricalDataError, match=message):
        parse_trades_payload(payload, request_cursor=cursor, page_size=3)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["1", "1", 1.0, "b", "l", ""], "seven fields"),
        (_trade("0", "1", 1.0, 1), "finite and positive"),
        (_trade("1", "0", 1.0, 1), "finite and positive"),
        (_trade("1", "1", 1.0, 1, side="x"), "side"),
        (_trade("1", "1", 1.0, 1, order_type="x"), "order type"),
        (["1", "1", 1.0, "b", "l", 1, 1], "misc field"),
        (_trade("1", "1", 1.0, 0), "positive integer"),
        (["1", "1", "1", "b", "l", "", 1], "JSON number"),
    ],
)
def test_parse_trade_rejects_bad_fields(row: list[object], message: str) -> None:
    with pytest.raises(HistoricalDataError, match=message):
        parse_trades_payload(_payload([row], "2000000000"), request_cursor="0")


def test_parse_bytes_rejects_invalid_and_ambiguous_json() -> None:
    with pytest.raises(HistoricalDataError, match="invalid JSON"):
        parse_trades_bytes(b"not-json", request_cursor="0")
    with pytest.raises(HistoricalDataError, match="duplicate key"):
        parse_trades_bytes(
            b'{"error":[],"error":[],"result":{}}',
            request_cursor="0",
        )
    with pytest.raises(HistoricalDataError, match="numeric constant"):
        parse_trades_bytes(
            b'{"error":[],"result":{"XXBTZCAD":[["1","1",NaN,"b","l","",1]],"last":"1"}}',
            request_cursor="0",
        )


def test_download_paginates_from_zero_at_safe_cadence_and_archives_raw_pages(
    tmp_path: Path,
) -> None:
    archive, urls, sleeps = _download(tmp_path)

    assert archive.complete is True
    assert archive.page_count == 2
    assert archive.included_trade_count == 4
    assert archive.final_cursor == "1672790400000000100"
    assert archive.first_retrieved_at == RETRIEVED_AT
    assert archive.last_retrieved_at == RETRIEVED_AT
    assert sleeps == [1.25]
    assert urls == [
        f"{KRAKEN_TRADES_URL}?pair=XBTCAD&since=0&count=3",
        f"{KRAKEN_TRADES_URL}?pair=XBTCAD&since=1672532130000000000&count=3",
    ]
    raw_lines = (tmp_path / RAW_ARCHIVE_FILENAME).read_text().splitlines()
    assert len(raw_lines) == 2
    first_record = json.loads(raw_lines[0])
    assert base64.b64decode(first_record["raw_base64"]) == _pages()[0]
    assert first_record["raw_sha256"] == hashlib.sha256(_pages()[0]).hexdigest()
    state = json.loads((tmp_path / STATE_FILENAME).read_text())
    assert state["complete"] is True
    assert state["archive_prefix_sha256"] == archive.raw_sha256


def test_download_hot_path_is_linear_and_supports_bounded_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page, second_page = _pages()
    counted_inspect = Mock(wraps=historical_data._inspect_archive)
    monkeypatch.setattr(historical_data, "_inspect_archive", counted_inspect)
    progress: list[tuple[int, bool]] = []
    partial = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        max_pages=1,
        transport=lambda _url, _timeout: first_page,
        clock=lambda: RETRIEVED_AT,
        progress=lambda archive: progress.append((archive.page_count, archive.complete)),
    )

    assert partial.complete is False
    assert partial.page_count == 1
    assert counted_inspect.call_count == 1
    assert progress == [(1, False)]

    completed = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        transport=lambda _url, _timeout: second_page,
        clock=lambda: RETRIEVED_AT,
        progress=lambda archive: progress.append((archive.page_count, archive.complete)),
    )
    assert completed.complete is True
    assert counted_inspect.call_count == 2
    assert progress[-1] == (2, True)


def test_download_rejects_nonidentical_inclusive_boundary_trade(tmp_path: Path) -> None:
    first_page, _second_page = _pages()
    download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        max_pages=1,
        transport=lambda _url, _timeout: first_page,
        clock=lambda: RETRIEVED_AT,
    )
    conflicting_page = _raw(
        [
            _trade("131.00", "1.0", 1672532130.0, 3),
            _trade("90.00", "3.0", 1672705080.0, 4),
        ],
        "1672705080000000100",
    )

    with pytest.raises(HistoricalDataError, match="conflicting boundary trade"):
        download_btc_cad_trades(
            directory=tmp_path,
            cutoff=CUTOFF,
            page_size=3,
            transport=lambda _url, _timeout: conflicting_page,
            clock=lambda: RETRIEVED_AT,
        )

    assert len((tmp_path / RAW_ARCHIVE_FILENAME).read_text().splitlines()) == 1


def test_download_retries_transient_transport_and_throttle_errors(tmp_path: Path) -> None:
    first_page, second_page = _pages()
    responses: list[object] = [
        RetryableHistoricalDataError("temporary"),
        json.dumps({"error": ["EService:Throttled: 1"]}).encode(),
        first_page,
        second_page,
    ]
    sleeps: list[float] = []

    def transport(_url: str, _timeout: float) -> bytes:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, bytes)
        return response

    archive = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        pace_seconds=1.0,
        max_attempts=3,
        transport=transport,
        sleeper=sleeps.append,
        clock=lambda: RETRIEVED_AT,
    )

    assert archive.complete
    assert sleeps == [1.0, 1.0, 1.0]


def test_download_resumes_from_durable_cursor_and_complete_resume_is_noop(
    tmp_path: Path,
) -> None:
    first_page, second_page = _pages()
    calls = 0

    def interrupted(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_page
        raise RetryableHistoricalDataError("offline")

    with pytest.raises(RetryableHistoricalDataError, match="offline"):
        download_btc_cad_trades(
            directory=tmp_path,
            cutoff=CUTOFF,
            page_size=3,
            max_attempts=1,
            transport=interrupted,
            sleeper=lambda _seconds: None,
            clock=lambda: RETRIEVED_AT,
        )
    state = json.loads((tmp_path / STATE_FILENAME).read_text())
    assert state["page_count"] == 1
    resumed_urls: list[str] = []

    def resumed_transport(url: str, _timeout: float) -> bytes:
        resumed_urls.append(url)
        return second_page

    archive = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        transport=resumed_transport,
        sleeper=lambda _seconds: None,
        clock=lambda: RETRIEVED_AT,
    )
    assert resumed_urls == [f"{KRAKEN_TRADES_URL}?pair=XBTCAD&since=1672532130000000000&count=3"]

    def forbidden(_url: str, _timeout: float) -> bytes:
        raise AssertionError("completed archive must not make another request")

    again = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        transport=forbidden,
    )
    assert again == archive


def test_download_rejects_tampered_state(tmp_path: Path) -> None:
    archive, _urls, _sleeps = _download(tmp_path)
    state = json.loads(archive.state_path.read_text())
    state["next_cursor"] = "1"
    archive.state_path.write_text(json.dumps(state))
    with pytest.raises(HistoricalDataError, match="state does not match"):
        download_btc_cad_trades(
            directory=tmp_path,
            cutoff=CUTOFF,
            page_size=3,
            transport=lambda _url, _timeout: b"",
        )


def test_download_rejects_truncated_archive(tmp_path: Path) -> None:
    first_page, _second_page = _pages()
    archive = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=CUTOFF,
        page_size=3,
        max_pages=1,
        transport=lambda _url, _timeout: first_page,
        clock=lambda: RETRIEVED_AT,
    )
    archive.state_path.unlink()
    with archive.archive_path.open("ab") as target:
        target.write(b"partial")
    with pytest.raises(HistoricalDataError, match="truncated"):
        download_btc_cad_trades(
            directory=tmp_path,
            cutoff=CUTOFF,
            page_size=3,
            transport=lambda _url, _timeout: b"",
        )


def test_daily_aggregation_uses_first_window_minute_and_preserves_gap(tmp_path: Path) -> None:
    archive, _urls, _sleeps = _download(tmp_path)

    bars = list(iter_daily_bars(archive))

    assert [bar.day.isoformat() for bar in bars] == ["2023-01-01", "2023-01-03"]
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("100.00"),
        Decimal("130.00"),
        Decimal("100.00"),
        Decimal("130.00"),
    )
    assert first.volume == Decimal("4.0")
    assert first.trade_count == 3
    assert first.execution_minute == datetime(2023, 1, 1, 0, 15, tzinfo=UTC)
    assert first.execution_volume == Decimal("3.0")
    assert first.execution_trade_count == 2
    assert first.execution_vwap == Decimal("116.6666666666666666666666666666666666667")
    assert bars[1].execution_vwap == Decimal("90.00")


def test_daily_aggregation_does_not_borrow_later_hour_minute_15(tmp_path: Path) -> None:
    cutoff = datetime(2023, 1, 2, tzinfo=UTC)
    response = _raw(
        [
            _trade("100.00", "1.0", 1672531200, 1),
            _trade("999.00", "1.0", 1672575300, 2),  # 12:15 UTC, not 00:15 UTC.
            _trade("101.00", "1.0", 1672617600, 3),  # Cutoff sentinel.
        ],
        "1672617600000000000",
    )
    archive = download_btc_cad_trades(
        directory=tmp_path,
        cutoff=cutoff,
        page_size=3,
        transport=lambda _url, _timeout: response,
        clock=lambda: RETRIEVED_AT,
    )

    bars = list(iter_daily_bars(archive))

    assert len(bars) == 1
    assert bars[0].execution_minute is None
    assert bars[0].execution_vwap is None
    assert bars[0].execution_volume is None


def test_normalized_csv_and_manifest_are_deterministic_and_hashed(tmp_path: Path) -> None:
    archive, _urls, _sleeps = _download(tmp_path / "raw")
    csv_path = tmp_path / "out" / "daily.csv"
    manifest_path = tmp_path / "out" / "manifest.json"

    first = write_normalized_dataset(
        archive,
        csv_path=csv_path,
        manifest_path=manifest_path,
    )
    first_csv = csv_path.read_bytes()
    first_manifest = manifest_path.read_bytes()
    second = write_normalized_dataset(
        archive,
        csv_path=csv_path,
        manifest_path=manifest_path,
    )

    assert first == second
    assert csv_path.read_bytes() == first_csv
    assert manifest_path.read_bytes() == first_manifest
    assert first.csv_sha256 == hashlib.sha256(first_csv).hexdigest()
    assert first.manifest_sha256 == hashlib.sha256(first_manifest).hexdigest()
    assert first.row_count == 2
    assert [day.isoformat() for day in first.gap_dates] == ["2023-01-02"]
    rows = list(csv.DictReader(csv_path.open(newline="")))
    assert len(rows) == 2
    assert rows[0]["execution_volume_btc"] == "3"
    assert rows[0]["execution_trade_count"] == "2"
    assert rows[1]["date"] == "2023-01-03"
    manifest = json.loads(first_manifest)
    assert manifest["raw_archive"]["sha256"] == archive.raw_sha256
    assert manifest["raw_archive"]["complete"] is True
    assert manifest["normalized_csv"]["gap_dates"] == ["2023-01-02"]
    assert manifest["normalized_csv"]["gap_policy"] == (
        "preserve_missing_days_without_forward_fill"
    )
    assert manifest["normalized_csv"]["execution_reference"]["available_at"] == ("minute_close")


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"cutoff": datetime(2023, 1, 4)}, ValueError, "timezone-aware UTC"),
        ({"cutoff": datetime(2023, 1, 4, 1, tzinfo=UTC)}, ValueError, "UTC midnight"),
        ({"cutoff": CUTOFF, "page_size": 0}, ValueError, "between 1 and 1000"),
        ({"cutoff": CUTOFF, "pace_seconds": 0.99}, ValueError, "at least 1.0"),
        ({"cutoff": CUTOFF, "timeout_seconds": 0}, ValueError, "positive"),
        ({"cutoff": CUTOFF, "max_attempts": 0}, ValueError, "positive"),
    ],
)
def test_download_rejects_unsafe_configuration(
    tmp_path: Path,
    kwargs: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    call: Callable[..., TradeArchive] = download_btc_cad_trades
    with pytest.raises(error, match=message):
        call(directory=tmp_path, transport=lambda _url, _timeout: b"", **kwargs)


def test_normalizer_rejects_output_collision_and_empty_history(tmp_path: Path) -> None:
    archive, _urls, _sleeps = _download(tmp_path / "full")
    with pytest.raises(ValueError, match="must not overlap"):
        write_normalized_dataset(
            archive,
            csv_path=archive.archive_path,
            manifest_path=tmp_path / "manifest.json",
        )

    only_cutoff = _raw(
        [_trade("95", "1", 1672790400.0, 1)],
        "1672790400000000100",
    )
    empty = download_btc_cad_trades(
        directory=tmp_path / "empty",
        cutoff=CUTOFF,
        page_size=3,
        transport=lambda _url, _timeout: only_cutoff,
        clock=lambda: RETRIEVED_AT,
    )
    with pytest.raises(HistoricalDataError, match="no trades"):
        write_normalized_dataset(
            empty,
            csv_path=tmp_path / "empty.csv",
            manifest_path=tmp_path / "empty.json",
        )


def test_clock_must_be_aware_utc(tmp_path: Path) -> None:
    with pytest.raises(HistoricalDataError, match="clock"):
        download_btc_cad_trades(
            directory=tmp_path,
            cutoff=CUTOFF,
            page_size=3,
            transport=lambda _url, _timeout: _pages()[0],
            clock=lambda: datetime(2026, 9, 1),
        )


def test_api_error_retryable_classification() -> None:
    assert KrakenTradesApiError(["EAPI:Rate limit exceeded"]).retryable is True
    assert KrakenTradesApiError(["EGeneral:Invalid arguments"]).retryable is False
