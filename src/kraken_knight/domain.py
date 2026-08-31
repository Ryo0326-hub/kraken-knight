"""Core, exchange-independent market data types.

The strategy deliberately operates on explicitly completed UTC daily candles.
This keeps Kraken's mutable final OHLC row from being mistaken for historical
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

type Number = Decimal | int | str


def to_decimal(value: Number, *, field: str) -> Decimal:
    """Return a finite ``Decimal`` without silently accepting binary floats."""

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be supplied as Decimal, int, or str")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class Candle:
    """A validated UTC daily OHLCV candle.

    ``open_time`` is the inclusive beginning of the UTC day.  Callers must
    explicitly mark a still-forming Kraken candle with ``complete=False``.
    Price and volume fields are normalized to :class:`~decimal.Decimal`.
    """

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.open_time, datetime):
            raise TypeError("open_time must be a datetime")
        if self.open_time.tzinfo is None or self.open_time.utcoffset() != timedelta(0):
            raise ValueError("open_time must be timezone-aware UTC")
        if any(
            (
                self.open_time.hour,
                self.open_time.minute,
                self.open_time.second,
                self.open_time.microsecond,
            )
        ):
            raise ValueError("a daily candle must begin at 00:00:00 UTC")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")

        for field_name in ("open", "high", "low", "close", "volume"):
            value = to_decimal(getattr(self, field_name), field=field_name)
            object.__setattr__(self, field_name, value)

        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must all be greater than zero")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within [low, high]")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within [low, high]")

    @property
    def close_time(self) -> datetime:
        """Exclusive end of this daily interval."""

        return self.open_time + timedelta(days=1)
