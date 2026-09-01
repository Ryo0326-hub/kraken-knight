from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from kraken_knight.research_charts import (
    ChartPoint,
    ChartSeries,
    RobustnessCell,
    write_line_chart,
    write_robustness_heatmap,
)


def point(day: int, value: str) -> ChartPoint:
    return ChartPoint(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day), Decimal(value))


def test_line_chart_is_deterministic_valid_svg(tmp_path: Path) -> None:
    series = (
        ChartSeries("Frozen V1", "#2dd4bf", (point(0, "1000"), point(1, "1010"))),
        ChartSeries("BTC buy & hold", "#60a5fa", (point(0, "1000"), point(1, "990"))),
    )
    first = write_line_chart(
        tmp_path / "first.svg",
        title="Equity & <risk>",
        subtitle="Causal replay",
        series=series,
        y_label="Equity (CAD)",
    )
    second = write_line_chart(
        tmp_path / "second.svg",
        title="Equity & <risk>",
        subtitle="Causal replay",
        series=series,
        y_label="Equity (CAD)",
    )

    assert first.read_bytes() == second.read_bytes()
    root = ElementTree.parse(first).getroot()
    assert root.tag.endswith("svg")
    assert "&lt;risk&gt;" in first.read_text(encoding="utf-8")


def test_line_chart_rejects_non_utc_and_non_monotonic_points(tmp_path: Path) -> None:
    naive = ChartSeries("bad", "#fff", (ChartPoint(datetime(2024, 1, 1), Decimal("1")),))
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        write_line_chart(
            tmp_path / "bad.svg",
            title="bad",
            subtitle="bad",
            series=(naive,),
            y_label="value",
        )

    repeated = ChartSeries("bad", "#fff", (point(0, "1"), point(0, "2")))
    with pytest.raises(ValueError, match="strictly increasing"):
        write_line_chart(
            tmp_path / "bad.svg",
            title="bad",
            subtitle="bad",
            series=(repeated,),
            y_label="value",
        )


def test_robustness_heatmap_marks_selected_cell_and_requires_rectangle(
    tmp_path: Path,
) -> None:
    cells = tuple(
        RobustnessCell(
            momentum,
            trend,
            volatility,
            Decimal(str(momentum - trend / 10 + volatility / 100)),
            (momentum, trend, volatility) == (90, 200, 30),
        )
        for volatility in (20, 30, 60)
        for momentum in (60, 90, 120)
        for trend in (150, 200, 250)
    )
    output = write_robustness_heatmap(
        tmp_path / "grid.svg",
        title="Robustness",
        subtitle="Neighboring policy grid",
        cells=cells,
        metric_label="holdout Sharpe",
    )
    payload = output.read_text(encoding="utf-8")

    ElementTree.parse(output)
    assert "pre-registered 90/200/30" in payload
    assert payload.count('stroke="#fbbf24"') == 2

    with pytest.raises(ValueError, match="rectangular"):
        write_robustness_heatmap(
            tmp_path / "bad.svg",
            title="bad",
            subtitle="bad",
            cells=cells[:-1],
            metric_label="metric",
        )
