"""Deterministic economic metrics for historical portfolio research.

The module intentionally keeps data validation and metric semantics independent
from the backtest engine.  Equity is assumed to be net of fees and slippage and
free of external cash flows.  Returns are simple close-to-close returns.  An
observation timestamp is the exclusive end of the valuation interval, so a
daily close at January 1 00:00 UTC belongs to the December 31 trading day.  For
irregularly spaced observations, volatility ratios use the observed average
sampling frequency and exposure is time weighted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise

from .domain import to_decimal

CALENDAR_DAYS_PER_YEAR = Decimal("365")
SECONDS_PER_DAY = Decimal("86400")
_MATH_PRECISION = 50


@dataclass(frozen=True, slots=True)
class EquityObservation:
    """One UTC portfolio valuation and optional gross exposure fraction.

    ``observed_at`` is the exclusive end of the valuation interval.  At UTC
    midnight, the observation therefore closes the preceding trading day.

    ``exposure_fraction`` is the fraction of portfolio equity exposed to the
    traded asset at ``observed_at``.  It must be in ``[0, 1]`` when supplied.
    The aggregate exposure metric treats each value as applying until the next
    observation.
    """

    observed_at: datetime
    equity: Decimal
    exposure_fraction: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observed_at, datetime):
            raise TypeError("observed_at must be a datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be timezone-aware UTC")

        equity = to_decimal(self.equity, field="equity")
        object.__setattr__(self, "equity", equity)
        if equity <= 0:
            raise ValueError("equity must be greater than zero")

        if self.exposure_fraction is not None:
            exposure = to_decimal(self.exposure_fraction, field="exposure_fraction")
            object.__setattr__(self, "exposure_fraction", exposure)
            if exposure < 0 or exposure > 1:
                raise ValueError("exposure_fraction must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class TradeCostAggregate:
    """Execution totals associated with a net-of-cost equity curve.

    ``traded_notional`` is the sum of absolute traded notionals. ``fees`` and
    ``slippage`` are non-negative costs in the same currency as portfolio
    equity.  Gross P&L is recovered as net P&L plus these explicit costs.
    """

    traded_notional: Decimal
    fees: Decimal
    slippage: Decimal
    trade_count: int

    def __post_init__(self) -> None:
        for field_name in ("traded_notional", "fees", "slippage"):
            value = to_decimal(getattr(self, field_name), field=field_name)
            object.__setattr__(self, field_name, value)
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")

        if isinstance(self.trade_count, bool) or not isinstance(self.trade_count, int):
            raise TypeError("trade_count must be an integer")
        if self.trade_count < 0:
            raise ValueError("trade_count cannot be negative")
        if self.trade_count == 0 and self.traded_notional != 0:
            raise ValueError("traded_notional must be zero when trade_count is zero")
        if self.trade_count > 0 and self.traded_notional == 0:
            raise ValueError("traded_notional must be positive when trade_count is positive")


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    """A chain-linked return for one UTC calendar period."""

    period: str
    return_fraction: Decimal


@dataclass(frozen=True, slots=True)
class ResearchMetrics:
    """Economic summary of one historical, net-of-cost equity curve."""

    start_at: datetime
    end_at: datetime
    observation_count: int
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    cagr: Decimal | None
    annualized_volatility: Decimal | None
    sharpe: Decimal | None
    downside_deviation: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    max_drawdown_duration_days: Decimal
    calmar: Decimal | None
    exposure_fraction: Decimal | None
    turnover: Decimal | None
    gross_pnl: Decimal | None
    fees: Decimal | None
    slippage: Decimal | None
    net_pnl: Decimal
    trade_count: int | None
    calendar_year_returns: tuple[PeriodReturn, ...]
    calendar_month_returns: tuple[PeriodReturn, ...]


def _duration_seconds(delta: timedelta) -> Decimal:
    """Convert a positive ``timedelta`` to seconds without binary floats."""

    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(
        "1000000"
    )


def _duration_days(start: datetime, end: datetime) -> Decimal:
    return _duration_seconds(end - start) / SECONDS_PER_DAY


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=Decimal("0")) / Decimal(len(values))


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _MATH_PRECISION
        return +value.sqrt()


def _positive_power(base: Decimal, exponent: Decimal) -> Decimal:
    """Return ``base ** exponent`` using deterministic Decimal operations."""

    with localcontext() as context:
        context.prec = _MATH_PRECISION
        return +(exponent * base.ln()).exp()


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), start=Decimal("0")) / Decimal(
        len(values) - 1
    )
    return _sqrt(variance)


def _validate_observations(
    observations: Sequence[EquityObservation],
) -> tuple[EquityObservation, ...]:
    if not observations:
        raise ValueError("at least one equity observation is required")
    if any(not isinstance(observation, EquityObservation) for observation in observations):
        raise TypeError("all observations must be EquityObservation instances")

    frozen = tuple(observations)
    if any(right.observed_at <= left.observed_at for left, right in pairwise(frozen)):
        raise ValueError("observation timestamps must be strictly increasing")
    return frozen


def _calendar_returns(
    observations: Sequence[EquityObservation],
    *,
    monthly: bool,
) -> tuple[PeriodReturn, ...]:
    """Chain link returns by the UTC period immediately before each exclusive end.

    Python datetimes have microsecond resolution, so subtracting one microsecond
    selects the final representable instant in the half-open valuation interval.
    It changes the period only at an exact month or year boundary.
    """

    growth_by_period: dict[str, Decimal] = {}
    for left, right in pairwise(observations):
        interval_last_instant = right.observed_at - timedelta(microseconds=1)
        period = interval_last_instant.strftime("%Y-%m" if monthly else "%Y")
        growth = right.equity / left.equity
        growth_by_period[period] = growth_by_period.get(period, Decimal("1")) * growth
    return tuple(
        PeriodReturn(period=period, return_fraction=growth - Decimal("1"))
        for period, growth in sorted(growth_by_period.items())
    )


def _drawdown_metrics(
    observations: Sequence[EquityObservation],
) -> tuple[Decimal, Decimal]:
    """Return maximum depth and longest continuous underwater duration."""

    peak_equity = observations[0].equity
    peak_at = observations[0].observed_at
    underwater_since: datetime | None = None
    maximum_drawdown = Decimal("0")
    maximum_duration = Decimal("0")

    for observation in observations[1:]:
        if observation.equity >= peak_equity:
            if underwater_since is not None:
                maximum_duration = max(
                    maximum_duration,
                    _duration_days(underwater_since, observation.observed_at),
                )
            peak_equity = observation.equity
            peak_at = observation.observed_at
            underwater_since = None
            continue

        if underwater_since is None:
            underwater_since = peak_at
        maximum_drawdown = max(
            maximum_drawdown,
            Decimal("1") - observation.equity / peak_equity,
        )

    if underwater_since is not None:
        maximum_duration = max(
            maximum_duration,
            _duration_days(underwater_since, observations[-1].observed_at),
        )
    return maximum_drawdown, maximum_duration


def _time_weighted_exposure(
    observations: Sequence[EquityObservation],
) -> Decimal | None:
    """Weight each interval by the exposure observed at its exclusive end.

    Historical-study trades occur just after the interval's opening boundary
    and are reflected in the next close observation. Using the right endpoint
    therefore describes the holdings during that interval instead of lagging
    every trade by one full day.
    """

    if len(observations) < 2 or any(
        observation.exposure_fraction is None for observation in observations
    ):
        return None

    total_seconds = _duration_seconds(observations[-1].observed_at - observations[0].observed_at)
    weighted = Decimal("0")
    for left, right in pairwise(observations):
        # The all-present check above narrows this for readers, but not mypy.
        assert right.exposure_fraction is not None
        interval_seconds = _duration_seconds(right.observed_at - left.observed_at)
        weighted += right.exposure_fraction * interval_seconds
    return weighted / total_seconds


def _time_weighted_average_equity(observations: Sequence[EquityObservation]) -> Decimal:
    """Use trapezoidal time weighting for the turnover denominator."""

    if len(observations) == 1:
        return observations[0].equity

    total_seconds = _duration_seconds(observations[-1].observed_at - observations[0].observed_at)
    weighted = Decimal("0")
    for left, right in pairwise(observations):
        interval_seconds = _duration_seconds(right.observed_at - left.observed_at)
        weighted += (left.equity + right.equity) / Decimal("2") * interval_seconds
    return weighted / total_seconds


def calculate_research_metrics(
    observations: Sequence[EquityObservation],
    *,
    trade_costs: TradeCostAggregate | None = None,
) -> ResearchMetrics:
    """Calculate deterministic portfolio metrics.

    Ratios whose denominator or sample is undefined are returned as ``None``.
    Calendar returns are chain linked by the UTC period immediately before each
    interval's exclusive right endpoint, so extending the curve cannot revise
    an already completed period.
    """

    curve = _validate_observations(observations)
    if trade_costs is not None and not isinstance(trade_costs, TradeCostAggregate):
        raise TypeError("trade_costs must be a TradeCostAggregate instance")

    initial_equity = curve[0].equity
    final_equity = curve[-1].equity
    total_return = final_equity / initial_equity - Decimal("1")
    net_pnl = final_equity - initial_equity
    returns = tuple(right.equity / left.equity - Decimal("1") for left, right in pairwise(curve))

    elapsed_days = _duration_days(curve[0].observed_at, curve[-1].observed_at)
    if elapsed_days > 0:
        cagr = _positive_power(
            final_equity / initial_equity,
            CALENDAR_DAYS_PER_YEAR / elapsed_days,
        ) - Decimal("1")
        annualization_factor = Decimal(len(returns)) * CALENDAR_DAYS_PER_YEAR / elapsed_days
    else:
        cagr = None
        annualization_factor = None

    sample_deviation = _sample_standard_deviation(returns)
    if sample_deviation is not None and annualization_factor is not None:
        annualization_root = _sqrt(annualization_factor)
        annualized_volatility = sample_deviation * annualization_root
        sharpe = (
            _mean(returns) / sample_deviation * annualization_root if sample_deviation > 0 else None
        )
    else:
        annualized_volatility = None
        sharpe = None

    if returns and annualization_factor is not None:
        downside_variance = _mean(tuple(min(value, Decimal("0")) ** 2 for value in returns))
        downside_deviation = _sqrt(downside_variance) * _sqrt(annualization_factor)
        sortino = (
            _mean(returns) / _sqrt(downside_variance) * _sqrt(annualization_factor)
            if downside_variance > 0
            else None
        )
    else:
        downside_deviation = None
        sortino = None

    max_drawdown, max_drawdown_duration = _drawdown_metrics(curve)
    calmar = cagr / max_drawdown if cagr is not None and max_drawdown > 0 else None

    if trade_costs is None:
        turnover = None
        gross_pnl = None
        fees = None
        slippage = None
        trade_count = None
    else:
        turnover = trade_costs.traded_notional / _time_weighted_average_equity(curve)
        fees = trade_costs.fees
        slippage = trade_costs.slippage
        gross_pnl = net_pnl + fees + slippage
        trade_count = trade_costs.trade_count

    return ResearchMetrics(
        start_at=curve[0].observed_at,
        end_at=curve[-1].observed_at,
        observation_count=len(curve),
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        downside_deviation=downside_deviation,
        sortino=sortino,
        max_drawdown=max_drawdown,
        max_drawdown_duration_days=max_drawdown_duration,
        calmar=calmar,
        exposure_fraction=_time_weighted_exposure(curve),
        turnover=turnover,
        gross_pnl=gross_pnl,
        fees=fees,
        slippage=slippage,
        net_pnl=net_pnl,
        trade_count=trade_count,
        calendar_year_returns=_calendar_returns(curve, monthly=False),
        calendar_month_returns=_calendar_returns(curve, monthly=True),
    )


def metrics_to_jsonable(metrics: ResearchMetrics) -> dict[str, object]:
    """Return an exact, JSON-serializable representation of ``metrics``.

    Decimal values are encoded as strings to avoid silently reintroducing
    binary floating-point rounding in research artifacts.
    """

    if not isinstance(metrics, ResearchMetrics):
        raise TypeError("metrics must be a ResearchMetrics instance")

    def decimal_or_none(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    def period_returns(values: Sequence[PeriodReturn]) -> list[dict[str, str]]:
        return [
            {"period": value.period, "return_fraction": str(value.return_fraction)}
            for value in values
        ]

    return {
        "start_at": metrics.start_at.isoformat(),
        "end_at": metrics.end_at.isoformat(),
        "observation_count": metrics.observation_count,
        "initial_equity": str(metrics.initial_equity),
        "final_equity": str(metrics.final_equity),
        "total_return": str(metrics.total_return),
        "cagr": decimal_or_none(metrics.cagr),
        "annualized_volatility": decimal_or_none(metrics.annualized_volatility),
        "sharpe": decimal_or_none(metrics.sharpe),
        "downside_deviation": decimal_or_none(metrics.downside_deviation),
        "sortino": decimal_or_none(metrics.sortino),
        "max_drawdown": str(metrics.max_drawdown),
        "max_drawdown_duration_days": str(metrics.max_drawdown_duration_days),
        "calmar": decimal_or_none(metrics.calmar),
        "exposure_fraction": decimal_or_none(metrics.exposure_fraction),
        "turnover": decimal_or_none(metrics.turnover),
        "gross_pnl": decimal_or_none(metrics.gross_pnl),
        "fees": decimal_or_none(metrics.fees),
        "slippage": decimal_or_none(metrics.slippage),
        "net_pnl": str(metrics.net_pnl),
        "trade_count": metrics.trade_count,
        "calendar_year_returns": period_returns(metrics.calendar_year_returns),
        "calendar_month_returns": period_returns(metrics.calendar_month_returns),
    }
