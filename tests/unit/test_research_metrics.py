from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kraken_knight.research_metrics import (
    EquityObservation,
    PeriodReturn,
    TradeCostAggregate,
    calculate_research_metrics,
    metrics_to_jsonable,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


def observation(
    day: int,
    equity: str,
    *,
    exposure: str | None = None,
    start: datetime = START,
) -> EquityObservation:
    return EquityObservation(
        observed_at=start + timedelta(days=day),
        equity=Decimal(equity),
        exposure_fraction=None if exposure is None else Decimal(exposure),
    )


def test_single_observation_marks_sample_dependent_metrics_undefined() -> None:
    metrics = calculate_research_metrics([observation(0, "100", exposure="0.5")])

    assert metrics.total_return == Decimal("0")
    assert metrics.net_pnl == Decimal("0")
    assert metrics.cagr is None
    assert metrics.annualized_volatility is None
    assert metrics.sharpe is None
    assert metrics.downside_deviation is None
    assert metrics.sortino is None
    assert metrics.calmar is None
    assert metrics.exposure_fraction is None
    assert metrics.turnover is None
    assert metrics.gross_pnl is None
    assert metrics.fees is None
    assert metrics.slippage is None
    assert metrics.trade_count is None
    assert metrics.max_drawdown == Decimal("0")
    assert metrics.max_drawdown_duration_days == Decimal("0")
    assert metrics.calendar_year_returns == ()
    assert metrics.calendar_month_returns == ()


def test_single_observation_accepts_an_explicit_zero_trade_aggregate() -> None:
    metrics = calculate_research_metrics(
        [observation(0, "100")],
        trade_costs=TradeCostAggregate(
            traded_notional=Decimal("0"),
            fees=Decimal("0"),
            slippage=Decimal("0"),
            trade_count=0,
        ),
    )

    assert metrics.turnover == Decimal("0")
    assert metrics.gross_pnl == Decimal("0")
    assert metrics.fees == Decimal("0")
    assert metrics.slippage == Decimal("0")
    assert metrics.trade_count == 0


def test_total_return_cagr_and_net_pnl_use_actual_calendar_days() -> None:
    metrics = calculate_research_metrics(
        [
            observation(0, "100"),
            observation(365, "121"),
        ]
    )

    assert metrics.initial_equity == Decimal("100")
    assert metrics.final_equity == Decimal("121")
    assert metrics.total_return == Decimal("0.21")
    assert metrics.cagr == Decimal("0.21")
    assert metrics.net_pnl == Decimal("21")


def test_volatility_sharpe_downside_and_sortino_follow_documented_formulas() -> None:
    curve = [
        observation(0, "100"),
        observation(1, "110"),
        observation(2, "99"),
        observation(3, "118.8"),
    ]

    metrics = calculate_research_metrics(curve)

    returns = [Decimal("0.1"), Decimal("-0.1"), Decimal("0.2")]
    mean = sum(returns, start=Decimal("0")) / Decimal("3")
    sample_variance = sum(((value - mean) ** 2 for value in returns), start=Decimal("0")) / Decimal(
        "2"
    )
    expected_volatility = sample_variance.sqrt() * Decimal("365").sqrt()
    expected_sharpe = mean / sample_variance.sqrt() * Decimal("365").sqrt()
    downside_variance = Decimal("0.01") / Decimal("3")
    expected_downside = downside_variance.sqrt() * Decimal("365").sqrt()
    expected_sortino = mean / downside_variance.sqrt() * Decimal("365").sqrt()

    assert metrics.annualized_volatility is not None
    assert metrics.annualized_volatility == pytest.approx(expected_volatility)
    assert metrics.sharpe is not None
    assert metrics.sharpe == pytest.approx(expected_sharpe)
    assert metrics.downside_deviation is not None
    assert metrics.downside_deviation == pytest.approx(expected_downside)
    assert metrics.sortino is not None
    assert metrics.sortino == pytest.approx(expected_sortino)


def test_constant_curve_uses_zero_deviations_but_null_undefined_ratios() -> None:
    metrics = calculate_research_metrics(
        [observation(0, "100"), observation(1, "100"), observation(2, "100")]
    )

    assert metrics.annualized_volatility == Decimal("0")
    assert metrics.downside_deviation == Decimal("0")
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.max_drawdown == Decimal("0")
    assert metrics.calmar is None


def test_drawdown_depth_and_duration_use_time_not_observation_count() -> None:
    metrics = calculate_research_metrics(
        [
            observation(0, "100"),
            observation(2, "90"),
            observation(5, "80"),
            observation(10, "100"),
            observation(12, "120"),
            observation(20, "110"),
        ]
    )

    assert metrics.max_drawdown == Decimal("0.2")
    assert metrics.max_drawdown_duration_days == Decimal("10")
    assert metrics.cagr is not None
    assert metrics.calmar == metrics.cagr / Decimal("0.2")


def test_exposure_and_trade_cost_aggregates_are_economic_and_time_weighted() -> None:
    curve = [
        observation(0, "100", exposure="0.5"),
        observation(2, "110", exposure="1"),
        observation(5, "120", exposure="0"),
    ]
    costs = TradeCostAggregate(
        traded_notional=Decimal("222"),
        fees=Decimal("2"),
        slippage=Decimal("1"),
        trade_count=3,
    )

    metrics = calculate_research_metrics(curve, trade_costs=costs)

    # Exposure belongs to the half-open interval ending at each observation:
    # 2 days at 100%, followed by 3 days at 0%.
    assert metrics.exposure_fraction == Decimal("0.4")
    # Trapezoidal time-weighted average equity is exactly CAD 111.
    assert metrics.turnover == Decimal("2")
    assert metrics.net_pnl == Decimal("20")
    assert metrics.gross_pnl == Decimal("23")
    assert metrics.fees == Decimal("2")
    assert metrics.slippage == Decimal("1")
    assert metrics.trade_count == 3


def test_missing_any_exposure_observation_makes_aggregate_undefined() -> None:
    metrics = calculate_research_metrics(
        [
            observation(0, "100", exposure="1"),
            observation(1, "101"),
            observation(2, "102", exposure="0"),
        ]
    )

    assert metrics.exposure_fraction is None


def test_exposure_after_opening_fill_is_not_lagged_by_one_interval() -> None:
    metrics = calculate_research_metrics(
        [
            observation(0, "100", exposure="0"),
            observation(1, "101", exposure="0.8"),
            observation(2, "102", exposure="0.8"),
        ]
    )

    assert metrics.exposure_fraction == Decimal("0.8")


def test_calendar_returns_are_chain_linked_by_exclusive_interval_end() -> None:
    start = datetime(2024, 12, 31, tzinfo=UTC)
    curve = [
        observation(0, "100", start=start),
        observation(1, "110", start=start),
        observation(31, "121", start=start),
        observation(32, "108.9", start=start),
        observation(365, "119.79", start=start),
        observation(366, "131.769", start=start),
    ]

    metrics = calculate_research_metrics(curve)

    assert metrics.calendar_month_returns == (
        PeriodReturn("2024-12", Decimal("0.1")),
        PeriodReturn("2025-01", Decimal("-0.01")),
        PeriodReturn("2025-12", Decimal("0.21")),
    )
    assert metrics.calendar_year_returns == (
        PeriodReturn("2024", Decimal("0.1")),
        PeriodReturn("2025", Decimal("0.1979")),
    )


def test_january_first_midnight_close_belongs_to_december_31() -> None:
    curve = [
        EquityObservation(datetime(2024, 12, 30, tzinfo=UTC), Decimal("100")),
        EquityObservation(datetime(2024, 12, 31, tzinfo=UTC), Decimal("110")),
        EquityObservation(datetime(2025, 1, 1, tzinfo=UTC), Decimal("121")),
        EquityObservation(datetime(2025, 1, 2, tzinfo=UTC), Decimal("108.9")),
    ]

    metrics = calculate_research_metrics(curve)

    assert metrics.calendar_month_returns == (
        PeriodReturn("2024-12", Decimal("0.21")),
        PeriodReturn("2025-01", Decimal("-0.1")),
    )
    assert metrics.calendar_year_returns == (
        PeriodReturn("2024", Decimal("0.21")),
        PeriodReturn("2025", Decimal("-0.1")),
    )


def test_future_observation_does_not_revise_completed_calendar_period() -> None:
    january = [
        EquityObservation(datetime(2025, 1, 1, tzinfo=UTC), Decimal("100")),
        EquityObservation(datetime(2025, 1, 31, tzinfo=UTC), Decimal("110")),
    ]
    january_metrics = calculate_research_metrics(january)
    extended_metrics = calculate_research_metrics(
        [*january, EquityObservation(datetime(2025, 2, 28, tzinfo=UTC), Decimal("99"))]
    )

    assert january_metrics.calendar_month_returns == (PeriodReturn("2025-01", Decimal("0.1")),)
    assert extended_metrics.calendar_month_returns[0] == january_metrics.calendar_month_returns[0]


def test_serialization_is_exact_json_friendly_and_deterministic() -> None:
    curve = [observation(0, "100"), observation(365, "121")]
    first = calculate_research_metrics(curve)
    second = calculate_research_metrics(tuple(curve))

    assert first == second
    first_payload = metrics_to_jsonable(first)
    second_payload = metrics_to_jsonable(second)
    assert first_payload == second_payload
    assert first_payload["initial_equity"] == "100"
    assert first_payload["total_return"] == "0.21"
    assert first_payload["sharpe"] is None
    assert first_payload["calendar_year_returns"] == [{"period": "2024", "return_fraction": "0.21"}]
    assert json.loads(json.dumps(first_payload, sort_keys=True))["net_pnl"] == "21"


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        ({"observed_at": "2024-01-01", "equity": Decimal("100")}, TypeError, "datetime"),
        ({"observed_at": datetime(2024, 1, 1), "equity": Decimal("100")}, ValueError, "UTC"),
        (
            {
                "observed_at": datetime(
                    2024,
                    1,
                    1,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
                "equity": Decimal("100"),
            },
            ValueError,
            "UTC",
        ),
        ({"observed_at": START, "equity": Decimal("0")}, ValueError, "greater than zero"),
        ({"observed_at": START, "equity": Decimal("NaN")}, ValueError, "finite"),
        (
            {
                "observed_at": START,
                "equity": Decimal("100"),
                "exposure_fraction": Decimal("1.01"),
            },
            ValueError,
            r"in \[0, 1\]",
        ),
    ],
)
def test_equity_observation_validation(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        EquityObservation(**kwargs)  # type: ignore[arg-type]


def test_observations_must_be_nonempty_typed_and_strictly_increasing() -> None:
    with pytest.raises(ValueError, match="at least one"):
        calculate_research_metrics([])
    with pytest.raises(TypeError, match="EquityObservation"):
        calculate_research_metrics([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_research_metrics([observation(1, "100"), observation(0, "101")])
    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_research_metrics([observation(0, "100"), observation(0, "101")])


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        (
            {
                "traded_notional": Decimal("-1"),
                "fees": Decimal("0"),
                "slippage": Decimal("0"),
                "trade_count": 1,
            },
            ValueError,
            "traded_notional cannot be negative",
        ),
        (
            {
                "traded_notional": Decimal("1"),
                "fees": Decimal("-1"),
                "slippage": Decimal("0"),
                "trade_count": 1,
            },
            ValueError,
            "fees cannot be negative",
        ),
        (
            {
                "traded_notional": Decimal("1"),
                "fees": Decimal("0"),
                "slippage": Decimal("-1"),
                "trade_count": 1,
            },
            ValueError,
            "slippage cannot be negative",
        ),
        (
            {
                "traded_notional": Decimal("0"),
                "fees": Decimal("0"),
                "slippage": Decimal("0"),
                "trade_count": True,
            },
            TypeError,
            "integer",
        ),
        (
            {
                "traded_notional": Decimal("0"),
                "fees": Decimal("0"),
                "slippage": Decimal("0"),
                "trade_count": -1,
            },
            ValueError,
            "cannot be negative",
        ),
        (
            {
                "traded_notional": Decimal("1"),
                "fees": Decimal("0"),
                "slippage": Decimal("0"),
                "trade_count": 0,
            },
            ValueError,
            "must be zero",
        ),
        (
            {
                "traded_notional": Decimal("0"),
                "fees": Decimal("0"),
                "slippage": Decimal("0"),
                "trade_count": 1,
            },
            ValueError,
            "must be positive",
        ),
    ],
)
def test_trade_cost_aggregate_validation(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        TradeCostAggregate(**kwargs)  # type: ignore[arg-type]


def test_helper_and_trade_cost_argument_validate_types() -> None:
    metrics = calculate_research_metrics([observation(0, "100")])
    with pytest.raises(TypeError, match="TradeCostAggregate"):
        calculate_research_metrics([observation(0, "100")], trade_costs=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ResearchMetrics"):
        metrics_to_jsonable(object())  # type: ignore[arg-type]
    assert metrics_to_jsonable(metrics)["observation_count"] == 1
