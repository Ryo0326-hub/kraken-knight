"""Dependency-free SVG charts for immutable historical research artifacts."""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChartPoint:
    observed_at: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class ChartSeries:
    name: str
    color: str
    points: tuple[ChartPoint, ...]


@dataclass(frozen=True, slots=True)
class RobustnessCell:
    momentum_days: int
    trend_days: int
    volatility_days: int
    value: Decimal
    selected: bool = False


_WIDTH = 1200
_HEIGHT = 650
_PLOT_LEFT = 96
_PLOT_RIGHT = 1156
_PLOT_TOP = 94
_PLOT_BOTTOM = 556
_COLORS = {
    "background": "#08111f",
    "panel": "#0d1b2e",
    "grid": "#29415f",
    "text": "#e7eef8",
    "muted": "#9fb0c6",
    "positive": "#2dd4bf",
    "negative": "#fb7185",
    "selected": "#fbbf24",
}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _finite_decimal(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _validate_series(series: Sequence[ChartSeries]) -> tuple[ChartSeries, ...]:
    if not series:
        raise ValueError("at least one chart series is required")
    validated = tuple(series)
    if any(not item.name.strip() for item in validated):
        raise ValueError("series names cannot be blank")
    for item in validated:
        if not item.points:
            raise ValueError("each chart series requires at least one point")
        previous: datetime | None = None
        for point in item.points:
            if point.observed_at.tzinfo is None or point.observed_at.utcoffset() != timedelta(0):
                raise ValueError("chart timestamps must be timezone-aware UTC")
            if previous is not None and point.observed_at <= previous:
                raise ValueError("chart timestamps must be strictly increasing within a series")
            _finite_decimal(point.value, field="point value")
            previous = point.observed_at
    return validated


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("SVG coordinates must be finite")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _base_svg(*, title: str, subtitle: str) -> list[str]:
    safe_title = _escape(title)
    safe_subtitle = _escape(subtitle)
    return [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
            f'viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-labelledby="title description">'
        ),
        f'<title id="title">{safe_title}</title>',
        f'<desc id="description">{safe_subtitle}</desc>',
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="{_COLORS["background"]}"/>',
        (
            f'<rect x="32" y="28" width="1136" height="594" rx="18" '
            f'fill="{_COLORS["panel"]}" stroke="{_COLORS["grid"]}"/>'
        ),
        (
            f'<text x="64" y="65" fill="{_COLORS["text"]}" font-size="25" '
            f'font-family="ui-sans-serif,system-ui" font-weight="700">{safe_title}</text>'
        ),
        (
            f'<text x="64" y="88" fill="{_COLORS["muted"]}" font-size="13" '
            f'font-family="ui-sans-serif,system-ui">{safe_subtitle}</text>'
        ),
    ]


def _write_svg(path: Path, lines: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join((*lines, "</svg>", ""))
    path.write_text(payload, encoding="utf-8", newline="\n")
    return path


def write_line_chart(
    path: Path,
    *,
    title: str,
    subtitle: str,
    series: Sequence[ChartSeries],
    y_label: str,
    percent_axis: bool = False,
) -> Path:
    """Write a deterministic, accessible SVG line chart."""

    clean = _validate_series(series)
    all_points = tuple(point for item in clean for point in item.points)
    minimum_time = min(point.observed_at for point in all_points)
    maximum_time = max(point.observed_at for point in all_points)
    values = [float(point.value) for point in all_points]
    minimum_value = min(values)
    maximum_value = max(values)
    if minimum_value == maximum_value:
        padding = max(abs(minimum_value) * 0.05, 1.0)
    else:
        padding = (maximum_value - minimum_value) * 0.08
    y_min = minimum_value - padding
    y_max = maximum_value + padding
    elapsed = max((maximum_time - minimum_time).total_seconds(), 1.0)

    def x_position(observed_at: datetime) -> float:
        fraction = (observed_at - minimum_time).total_seconds() / elapsed
        return _PLOT_LEFT + fraction * (_PLOT_RIGHT - _PLOT_LEFT)

    def y_position(value: Decimal) -> float:
        fraction = (float(value) - y_min) / (y_max - y_min)
        return _PLOT_BOTTOM - fraction * (_PLOT_BOTTOM - _PLOT_TOP)

    lines = _base_svg(title=title, subtitle=subtitle)
    for tick in range(6):
        fraction = tick / 5
        y = _PLOT_BOTTOM - fraction * (_PLOT_BOTTOM - _PLOT_TOP)
        value = y_min + fraction * (y_max - y_min)
        label = f"{value * 100:.0f}%" if percent_axis else f"{value:,.0f}"
        lines.extend(
            (
                (
                    f'<line x1="{_PLOT_LEFT}" y1="{_number(y)}" x2="{_PLOT_RIGHT}" '
                    f'y2="{_number(y)}" stroke="{_COLORS["grid"]}" stroke-width="1"/>'
                ),
                (
                    f'<text x="84" y="{_number(y + 4)}" text-anchor="end" '
                    f'fill="{_COLORS["muted"]}" font-size="12" '
                    f'font-family="ui-monospace,monospace">{_escape(label)}</text>'
                ),
            )
        )

    for tick in range(5):
        fraction = tick / 4
        x = _PLOT_LEFT + fraction * (_PLOT_RIGHT - _PLOT_LEFT)
        observed_at = minimum_time + (maximum_time - minimum_time) * fraction
        lines.extend(
            (
                (
                    f'<line x1="{_number(x)}" y1="{_PLOT_TOP}" x2="{_number(x)}" '
                    f'y2="{_PLOT_BOTTOM}" stroke="{_COLORS["grid"]}" stroke-width="1"/>'
                ),
                (
                    f'<text x="{_number(x)}" y="580" text-anchor="middle" '
                    f'fill="{_COLORS["muted"]}" font-size="12" '
                    f'font-family="ui-monospace,monospace">{observed_at.date().isoformat()}</text>'
                ),
            )
        )

    legend_x = _PLOT_LEFT
    for item in clean:
        points = " ".join(
            f"{_number(x_position(point.observed_at))},{_number(y_position(point.value))}"
            for point in item.points
        )
        lines.append(
            f'<polyline points="{points}" fill="none" stroke="{_escape(item.color)}" '
            'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        lines.extend(
            (
                f'<line x1="{legend_x}" y1="608" x2="{legend_x + 24}" y2="608" '
                f'stroke="{_escape(item.color)}" stroke-width="3"/>',
                f'<text x="{legend_x + 31}" y="612" fill="{_COLORS["text"]}" '
                f'font-size="12" font-family="ui-sans-serif,system-ui">{_escape(item.name)}</text>',
            )
        )
        legend_x += max(132, 43 + len(item.name) * 7)

    lines.append(
        f'<text transform="translate(24 340) rotate(-90)" text-anchor="middle" '
        f'fill="{_COLORS["muted"]}" font-size="12" '
        f'font-family="ui-sans-serif,system-ui">{_escape(y_label)}</text>'
    )
    return _write_svg(path, lines)


def write_robustness_heatmap(
    path: Path,
    *,
    title: str,
    subtitle: str,
    cells: Sequence[RobustnessCell],
    metric_label: str,
) -> Path:
    """Write three momentum-by-trend panels, one per volatility window."""

    if not cells:
        raise ValueError("at least one robustness cell is required")
    clean = tuple(cells)
    keys = [(cell.momentum_days, cell.trend_days, cell.volatility_days) for cell in clean]
    if len(keys) != len(set(keys)):
        raise ValueError("robustness cells must have unique parameter triples")
    for cell in clean:
        if min(cell.momentum_days, cell.trend_days, cell.volatility_days) <= 0:
            raise ValueError("robustness windows must be positive")
        _finite_decimal(cell.value, field="robustness value")

    momentums = sorted({cell.momentum_days for cell in clean})
    trends = sorted({cell.trend_days for cell in clean})
    volatilities = sorted({cell.volatility_days for cell in clean})
    expected = len(momentums) * len(trends) * len(volatilities)
    if len(clean) != expected:
        raise ValueError("robustness grid must be rectangular")

    values = [float(cell.value) for cell in clean]
    scale = max(abs(min(values)), abs(max(values)), 1e-12)

    def color(value: float) -> str:
        intensity = min(abs(value) / scale, 1.0)
        if value >= 0:
            start = (20, 46, 64)
            end = (45, 212, 191)
        else:
            start = (48, 35, 57)
            end = (251, 113, 133)
        rgb = tuple(
            round(left + (right - left) * intensity) for left, right in zip(start, end, strict=True)
        )
        return "#" + "".join(f"{channel:02x}" for channel in rgb)

    by_key = {(cell.momentum_days, cell.trend_days, cell.volatility_days): cell for cell in clean}
    lines = _base_svg(title=title, subtitle=subtitle)
    panel_gap = 34
    panel_width = 320
    cell_width = panel_width / len(trends)
    cell_height = 102
    start_x = 106
    start_y = 166
    for panel_index, volatility in enumerate(volatilities):
        panel_x = start_x + panel_index * (panel_width + panel_gap)
        lines.append(
            f'<text x="{_number(panel_x + panel_width / 2)}" y="128" text-anchor="middle" '
            f'fill="{_COLORS["text"]}" font-size="15" font-family="ui-sans-serif,system-ui" '
            f'font-weight="700">Volatility {volatility}d</text>'
        )
        for column, trend in enumerate(trends):
            lines.append(
                f'<text x="{_number(panel_x + (column + 0.5) * cell_width)}" y="153" '
                f'text-anchor="middle" fill="{_COLORS["muted"]}" font-size="11" '
                f'font-family="ui-monospace,monospace">SMA {trend}</text>'
            )
        for row, momentum in enumerate(momentums):
            if panel_index == 0:
                lines.append(
                    f'<text x="94" y="{_number(start_y + (row + 0.5) * cell_height + 4)}" '
                    f'text-anchor="end" fill="{_COLORS["muted"]}" font-size="11" '
                    f'font-family="ui-monospace,monospace">MOM {momentum}</text>'
                )
            for column, trend in enumerate(trends):
                cell = by_key[(momentum, trend, volatility)]
                x = panel_x + column * cell_width
                y = start_y + row * cell_height
                stroke = _COLORS["selected"] if cell.selected else _COLORS["grid"]
                stroke_width = 4 if cell.selected else 1
                lines.extend(
                    (
                        f'<rect x="{_number(x)}" y="{_number(y)}" '
                        f'width="{_number(cell_width - 5)}" height="{cell_height - 5}" rx="8" '
                        f'fill="{color(float(cell.value))}" stroke="{stroke}" '
                        f'stroke-width="{stroke_width}"/>',
                        f'<text x="{_number(x + (cell_width - 5) / 2)}" '
                        f'y="{_number(y + cell_height / 2 + 5)}" text-anchor="middle" '
                        f'fill="#ffffff" font-size="15" font-family="ui-monospace,monospace" '
                        f'font-weight="700">{float(cell.value):+.2f}</text>',
                    )
                )

    lines.extend(
        (
            f'<circle cx="105" cy="526" r="7" fill="{_COLORS["negative"]}"/>',
            f'<text x="119" y="530" fill="{_COLORS["muted"]}" font-size="12" '
            f'font-family="ui-sans-serif,system-ui">negative</text>',
            f'<circle cx="206" cy="526" r="7" fill="{_COLORS["positive"]}"/>',
            f'<text x="220" y="530" fill="{_COLORS["muted"]}" font-size="12" '
            f'font-family="ui-sans-serif,system-ui">positive</text>',
            f'<rect x="311" y="519" width="14" height="14" rx="2" fill="none" '
            f'stroke="{_COLORS["selected"]}" stroke-width="3"/>',
            f'<text x="334" y="530" fill="{_COLORS["muted"]}" font-size="12" '
            f'font-family="ui-sans-serif,system-ui">pre-registered 90/200/30</text>',
            f'<text x="106" y="580" fill="{_COLORS["muted"]}" font-size="12" '
            f'font-family="ui-sans-serif,system-ui">Cell value: {_escape(metric_label)}. '
            "This grid diagnoses fragility; it does not select a winner.</text>",
        )
    )
    return _write_svg(path, lines)
