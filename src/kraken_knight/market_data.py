"""Strict parsing and retrieval of Kraken public daily OHLC data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kraken_knight.domain import Candle
from kraken_knight.provenance import sha256_json

KRAKEN_REST_URL = "https://api.kraken.com/0/public/OHLC"
DAILY_INTERVAL_MINUTES = 1440
BTC_CAD_REQUEST_ALIASES = frozenset({"XBTCAD", "XXBTZCAD", "BTC/CAD"})
BTC_CAD_RESPONSE_KEYS = frozenset({"XBTCAD", "XXBTZCAD"})
MINIMUM_COMPLETION_DELAY = timedelta(minutes=15)
MAXIMUM_OBSERVATION_AGE = timedelta(minutes=5)


class MarketDataError(RuntimeError):
    """Raised when market data is unavailable, malformed, or unsafe to use."""


@dataclass(frozen=True, slots=True)
class KrakenOhlcBatch:
    """A parsed response with the mutable tail isolated from completed candles."""

    completed: tuple[Candle, ...]
    mutable_tail: Candle
    raw_pair_key: str
    last_cursor: int
    observed_at: datetime
    raw_sha256: str
    requested_pair: str | None = None


Transport = Callable[[str, float], bytes]


def _default_transport(url: str, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "kraken-knight/0.2"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError) as exc:
        raise MarketDataError("Kraken public OHLC request failed") from exc


class KrakenPublicClient:
    """Minimal public client used only for low-frequency market data retrieval."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._transport = transport or _default_transport
        self._timeout_seconds = timeout_seconds

    def fetch_daily_ohlc(
        self,
        *,
        pair: str = "XBTCAD",
        since: int | None = None,
        observed_at: datetime | None = None,
    ) -> KrakenOhlcBatch:
        """Fetch daily candles while keeping Kraken's final mutable row separate."""

        if not pair or not pair.isascii():
            raise ValueError("pair must be a non-empty ASCII Kraken pair")
        if pair.upper() not in BTC_CAD_REQUEST_ALIASES:
            raise ValueError("Checkpoint 1 public data is frozen to BTC/CAD")
        query: dict[str, str | int] = {"pair": pair, "interval": DAILY_INTERVAL_MINUTES}
        if since is not None:
            if since < 0:
                raise ValueError("since must be nonnegative")
            query["since"] = since
        url = f"{KRAKEN_REST_URL}?{urlencode(query)}"
        raw = self._transport(url, self._timeout_seconds)
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketDataError("Kraken returned invalid JSON") from exc
        return parse_ohlc_payload(
            decoded,
            observed_at=observed_at or datetime.now(UTC),
            expected_pair=pair,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )


def _as_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketDataError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _as_sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MarketDataError(f"{field} must be an array")
    return value


def _parse_decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"{field} is not decimal-compatible") from exc
    if not parsed.is_finite():
        raise MarketDataError(f"{field} must be finite")
    return parsed


def _parse_row(row: object, *, complete: bool) -> Candle:
    values = _as_sequence(row, field="OHLC row")
    if len(values) < 8:
        raise MarketDataError("OHLC row must contain at least eight fields")
    try:
        timestamp = int(values[0])
    except (TypeError, ValueError) as exc:
        raise MarketDataError("OHLC timestamp must be an integer") from exc
    opened_at = datetime.fromtimestamp(timestamp, tz=UTC)
    if opened_at.time() != datetime.min.time():
        raise MarketDataError("daily OHLC timestamp must be UTC midnight")
    try:
        return Candle(
            open_time=opened_at,
            open=_parse_decimal(values[1], field="open"),
            high=_parse_decimal(values[2], field="high"),
            low=_parse_decimal(values[3], field="low"),
            close=_parse_decimal(values[4], field="close"),
            volume=_parse_decimal(values[6], field="volume"),
            complete=complete,
        )
    except ValueError as exc:
        raise MarketDataError("OHLC row violates candle invariants") from exc


def parse_ohlc_payload(
    payload: object,
    *,
    observed_at: datetime,
    expected_pair: str | None = None,
    raw_sha256: str | None = None,
) -> KrakenOhlcBatch:
    """Parse a Kraken OHLC response and quarantine its documented mutable tail."""

    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)
    root = _as_mapping(payload, field="response")
    errors = _as_sequence(root.get("error"), field="error")
    if errors:
        safe_errors = ", ".join(str(error) for error in errors)
        raise MarketDataError(f"Kraken API error: {safe_errors}")
    result = _as_mapping(root.get("result"), field="result")
    pair_keys = [key for key in result if key != "last"]
    if len(pair_keys) != 1:
        raise MarketDataError("OHLC result must contain exactly one pair")
    pair_key = pair_keys[0]
    if expected_pair is not None:
        if expected_pair.upper() not in BTC_CAD_REQUEST_ALIASES:
            raise ValueError("expected_pair must identify BTC/CAD")
        if pair_key.upper() not in BTC_CAD_RESPONSE_KEYS:
            raise MarketDataError("OHLC response pair does not match requested BTC/CAD")
    rows = _as_sequence(result[pair_key], field=pair_key)
    if not rows:
        raise MarketDataError("OHLC result contains no candles")
    try:
        last_cursor = int(result["last"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError("OHLC result has no valid last cursor") from exc

    completed = tuple(_parse_row(row, complete=True) for row in rows[:-1])
    mutable_tail = _parse_row(rows[-1], complete=False)
    validate_daily_sequence((*completed, mutable_tail), require_contiguous=False)
    return KrakenOhlcBatch(
        completed=completed,
        mutable_tail=mutable_tail,
        raw_pair_key=pair_key,
        last_cursor=last_cursor,
        observed_at=observed_at,
        raw_sha256=raw_sha256 or sha256_json(payload),
        requested_pair=expected_pair,
    )


def validate_batch_freshness(
    batch: KrakenOhlcBatch,
    *,
    evaluated_at: datetime,
    minimum_completion_delay: timedelta = MINIMUM_COMPLETION_DELAY,
    maximum_observation_age: timedelta = MAXIMUM_OBSERVATION_AGE,
) -> None:
    """Prove that a public batch is current enough for a daily decision.

    Kraken's final OHLC row is always mutable.  A valid batch therefore has a
    completed row immediately before that tail, was observed recently, and is
    evaluated no earlier than 15 minutes after the completed row closed.  Once
    the following scheduled decision time arrives, the old completed row is
    stale and cannot authorize new risk.
    """

    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(UTC)
    if minimum_completion_delay < timedelta(0):
        raise ValueError("minimum_completion_delay cannot be negative")
    if maximum_observation_age < timedelta(0):
        raise ValueError("maximum_observation_age cannot be negative")
    if not batch.completed:
        raise MarketDataError("no completed candle is available")
    validate_daily_sequence(
        (*batch.completed, batch.mutable_tail),
        require_contiguous=True,
    )
    if batch.observed_at > evaluated_at:
        raise MarketDataError("OHLC observation timestamp is in the future")
    if evaluated_at - batch.observed_at > maximum_observation_age:
        raise MarketDataError("OHLC observation is stale")

    latest = batch.completed[-1]
    if batch.mutable_tail.open_time != latest.close_time:
        raise MarketDataError("mutable OHLC tail does not immediately follow completed data")
    earliest_decision = latest.close_time + minimum_completion_delay
    next_scheduled_decision = earliest_decision + timedelta(days=1)
    if evaluated_at < earliest_decision:
        raise MarketDataError("latest candle has not satisfied the completion delay")
    if evaluated_at >= next_scheduled_decision:
        raise MarketDataError("latest completed candle is stale for this strategy date")


def validate_daily_sequence(
    candles: Sequence[Candle],
    *,
    require_contiguous: bool,
) -> None:
    """Reject duplicate, out-of-order, and optionally missing daily candles."""

    for previous, current in pairwise(candles):
        difference = current.open_time - previous.open_time
        if difference <= timedelta(0):
            raise MarketDataError("candles must be unique and strictly increasing")
        if require_contiguous and difference != timedelta(days=1):
            raise MarketDataError("daily candle sequence contains a gap")
