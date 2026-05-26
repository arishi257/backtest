from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from backtest.config import PROJECT_ROOT


DEFAULT_PROCESSED_DIR = PROJECT_ROOT / "processed_data"
START_TIME = "09:20"
END_TIME = "15:20"
INTERVAL_MINUTES = 10
PNL_METRICS = {
    "total": (("portfolio_total_pnl",), "Portfolio Total PnL"),
    "frozen-iv": (("frozen_iv_total_pnl",), "Frozen IV Total PnL"),
    "gamma-diff": (("portfolio_gamma_diff_total",), "Gamma Diff PnL"),
    "frozen-iv-gamma-diff": (
        ("frozen_iv_total_pnl", "portfolio_gamma_diff_total"),
        "Frozen IV PnL + Gamma Diff",
    ),
}


@dataclass(frozen=True)
class BucketRow:
    date_key: str
    underlying: str
    values: list[float | None]
    total: float


@dataclass(frozen=True)
class SummaryRow:
    date_key: str
    underlying: str
    frozen_iv_pnl: float | None
    total_pnl: float | None
    gamma_diff_pnl: float | None
    full_spot_vol: float | None
    full_um_mid_vol: float | None
    frozen_closest_strike_avg_vol: float | None


def main() -> None:
    parser = argparse.ArgumentParser(description="Show processed PnL buckets in a GUI table.")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR)
    parser.add_argument("--year", default="2026", help="Year or comma-separated years, e.g. 2025,2026.")
    parser.add_argument("--start-date", help="Inclusive start date, e.g. 01012026.")
    parser.add_argument("--end-date", help="Inclusive end date, e.g. 31032026.")
    parser.add_argument("--underlying", choices=("NIFTY", "SENSEX"), default=None)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show date-level summary rows instead of 10-minute interval buckets.",
    )
    parser.add_argument(
        "--closest-strikes",
        type=int,
        default=4,
        help="Number of strikes closest to universal mid to average for summary IV.",
    )
    parser.add_argument(
        "--metric",
        choices=tuple(PNL_METRICS),
        default="total",
        help="PnL series to bucket. Use frozen-iv-gamma-diff for Frozen IV PnL + Gamma Diff.",
    )
    args = parser.parse_args()

    intervals = interval_labels()
    date_filter = parse_date_filter(args.start_date, args.end_date)
    years = years_for_filter(args.year, date_filter)
    if args.summary:
        rows = load_summary_rows(
            args.processed_dir,
            years,
            args.underlying,
            date_filter,
            args.closest_strikes,
        )
        show_summary_table(rows, years, args.underlying, date_filter)
        return

    metric_fields, metric_label = PNL_METRICS[args.metric]
    rows = load_bucket_rows(
        args.processed_dir,
        years,
        args.underlying,
        intervals,
        metric_fields,
        date_filter,
    )
    show_table(rows, intervals, years, args.underlying, metric_label, date_filter)


def interval_points() -> list[str]:
    current = datetime.strptime(START_TIME, "%H:%M")
    end = datetime.strptime(END_TIME, "%H:%M")
    points = []
    while current <= end:
        points.append(current.strftime("%H:%M"))
        current += timedelta(minutes=INTERVAL_MINUTES)
    return points


def interval_labels() -> list[str]:
    points = interval_points()
    return [f"{points[index]}-{points[index + 1]}" for index in range(len(points) - 1)]


def load_bucket_rows(
    processed_dir: Path,
    years: list[str],
    underlying_filter: str | None,
    intervals: list[str],
    metric_fields: tuple[str, ...],
    date_filter: tuple[datetime, datetime] | None = None,
) -> list[BucketRow]:
    rows_by_key: dict[tuple[str, str], BucketRow] = {}
    points = interval_points()
    paths = sorted(
        {
            path
            for year in years
            for path in processed_dir.glob(f"{year}*.csv")
        },
        reverse=True,
    )
    for path in paths:
        if not path_in_date_filter(path, date_filter):
            continue
        with path.open(newline="") as file:
            data = list(csv.DictReader(file))
        if not data:
            continue

        underlying = (data[0].get("underlying") or "").upper()
        if underlying_filter is not None and underlying != underlying_filter:
            continue

        pnl_by_time = {}
        for record in data:
            timestamp = record.get("timestamp", "")
            value = metric_value(record, metric_fields)
            if len(timestamp) >= 16 and value is not None:
                pnl_by_time[timestamp[11:16]] = value
        available_times = sorted(pnl_by_time)

        bucket_values = []
        for start, end in zip(points, points[1:]):
            start_value = next_value_at_or_after(start, pnl_by_time, available_times)
            end_value = next_value_at_or_after(end, pnl_by_time, available_times)
            bucket_values.append(
                None if start_value is None or end_value is None else end_value - start_value
            )
        total = sum(value for value in bucket_values if value is not None)
        row = BucketRow(path.stem, underlying, bucket_values, total)
        key = (date_key_for_sort(path.stem), underlying)
        existing = rows_by_key.get(key)
        if existing is None or ("_" in path.stem and "_" not in existing.date_key):
            rows_by_key[key] = row
    return sorted(rows_by_key.values(), key=lambda row: row.date_key, reverse=True)


def processed_paths(
    processed_dir: Path,
    years: list[str],
    date_filter: tuple[datetime, datetime] | None = None,
) -> list[Path]:
    return sorted(
        {
            path
            for year in years
            for path in processed_dir.glob(f"{year}*.csv")
            if path_in_date_filter(path, date_filter)
        },
        reverse=True,
    )


def load_summary_rows(
    processed_dir: Path,
    years: list[str],
    underlying_filter: str | None,
    date_filter: tuple[datetime, datetime] | None,
    closest_strikes: int,
) -> list[SummaryRow]:
    rows_by_key: dict[tuple[str, str], SummaryRow] = {}
    for path in processed_paths(processed_dir, years, date_filter):
        with path.open(newline="") as file:
            data = list(csv.DictReader(file))
        if not data:
            continue

        underlying = (data[0].get("underlying") or "").upper()
        if underlying_filter is not None and underlying != underlying_filter:
            continue

        row = SummaryRow(
            date_key=path.stem,
            underlying=underlying,
            frozen_iv_pnl=last_float(data, "frozen_iv_total_pnl"),
            total_pnl=last_float(data, "portfolio_total_pnl"),
            gamma_diff_pnl=last_float(data, "portfolio_gamma_diff_total", default=0.0),
            full_spot_vol=last_float(data, "vol_spot_full_day"),
            full_um_mid_vol=last_float(data, "vol_universal_mid_full_day"),
            frozen_closest_strike_avg_vol=closest_strike_average_iv(
                data,
                closest_strikes,
            ),
        )
        key = (date_key_for_sort(path.stem), underlying)
        existing = rows_by_key.get(key)
        if existing is None or ("_" in path.stem and "_" not in existing.date_key):
            rows_by_key[key] = row
    return sorted(rows_by_key.values(), key=lambda row: row.date_key, reverse=True)


def show_table(
    rows: list[BucketRow],
    intervals: list[str],
    years: list[str],
    underlying: str | None,
    metric_label: str,
    date_filter: tuple[datetime, datetime] | None,
) -> None:
    root = tk.Tk()
    suffix = f" - {underlying}" if underlying else ""
    year_label = date_filter_label(years, date_filter)
    root.title(f"{metric_label} {INTERVAL_MINUTES}-Min Buckets {year_label}{suffix}")
    root.geometry("1500x780")

    title = tk.Label(
        root,
        text=f"{metric_label} Change by {INTERVAL_MINUTES}-Minute Interval ({year_label}){suffix}",
        anchor="w",
        padx=10,
        pady=8,
        font=("Arial", 12, "bold"),
    )
    title.pack(fill="x")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    table_tab = tk.Frame(notebook)
    chart_tab = tk.Frame(notebook)
    cumulative_tab = tk.Frame(notebook)
    notebook.add(table_tab, text="Table")
    notebook.add(chart_tab, text="Interval Totals Chart")
    notebook.add(cumulative_tab, text="Cumulative Avg PnL")

    totals = column_totals(rows, intervals)
    averages = column_averages(rows, intervals)
    medians = column_medians(rows, intervals)
    cumulative_totals = cumulative_values(totals)

    build_table_tab(table_tab, rows, intervals, totals, averages, medians)
    build_chart_tab(chart_tab, intervals, totals, medians, metric_label)
    build_cumulative_chart_tab(cumulative_tab, intervals, cumulative_totals, metric_label)

    root.mainloop()


def show_summary_table(
    rows: list[SummaryRow],
    years: list[str],
    underlying: str | None,
    date_filter: tuple[datetime, datetime] | None,
) -> None:
    root = tk.Tk()
    suffix = f" - {underlying}" if underlying else ""
    label = date_filter_label(years, date_filter)
    root.title(f"PnL Summary {label}{suffix}")
    root.geometry("1250x760")

    title = tk.Label(
        root,
        text=f"PnL Summary ({label}){suffix}",
        anchor="w",
        padx=10,
        pady=8,
        font=("Arial", 12, "bold"),
    )
    title.pack(fill="x")

    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    canvas = tk.Canvas(frame, highlightthickness=0)
    table = tk.Frame(canvas)
    y_scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
    canvas.create_window((0, 0), window=table, anchor="nw")
    table.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.grid(row=0, column=0, sticky="nsew")
    y_scroll.grid(row=0, column=1, sticky="ns")
    x_scroll.grid(row=1, column=0, sticky="ew")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)

    headers = [
        "Date",
        "Underlying",
        "Frozen IV PnL",
        "Total PnL",
        "Gamma Diff PnL",
        "Full Spot Vol",
        "Full UM Mid Vol",
        "Frozen Closest Strike Avg IV",
    ]
    for column_index, header in enumerate(headers):
        add_cell(
            table,
            0,
            column_index,
            header,
            bg="#e5e7eb",
            bold=True,
            anchor="center",
        )

    for row_index, row in enumerate(rows, start=1):
        values = [
            format_date(row.date_key),
            row.underlying,
            format_summary_number(row.frozen_iv_pnl),
            format_summary_number(row.total_pnl),
            format_summary_number(row.gamma_diff_pnl),
            format_percent_value(row.full_spot_vol),
            format_percent_value(row.full_um_mid_vol),
            format_plain_percent_value(row.frozen_closest_strike_avg_vol),
        ]
        for column_index, value in enumerate(values):
            add_cell(
                table,
                row_index,
                column_index,
                value,
                fg=summary_fg(row, column_index),
                anchor="center" if column_index < 2 else "e",
            )

    total_row = len(rows) + 1
    add_cell(table, total_row, 0, "Total", bg="#dbeafe", bold=True, anchor="center")
    add_cell(table, total_row, 1, "", bg="#dbeafe", bold=True, anchor="center")
    add_cell(table, total_row, 2, format_summary_number(sum_optional(row.frozen_iv_pnl for row in rows)), bg="#dbeafe", bold=True)
    add_cell(table, total_row, 3, format_summary_number(sum_optional(row.total_pnl for row in rows)), bg="#dbeafe", bold=True)
    add_cell(table, total_row, 4, format_summary_number(sum_optional(row.gamma_diff_pnl for row in rows)), bg="#dbeafe", bold=True)
    add_cell(table, total_row, 5, format_percent_value(avg_optional(row.full_spot_vol for row in rows)), bg="#dbeafe", bold=True)
    add_cell(table, total_row, 6, format_percent_value(avg_optional(row.full_um_mid_vol for row in rows)), bg="#dbeafe", bold=True)
    add_cell(table, total_row, 7, format_plain_percent_value(avg_optional(row.frozen_closest_strike_avg_vol for row in rows)), bg="#dbeafe", bold=True)

    canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))
    root.mainloop()


def build_table_tab(
    parent: tk.Frame,
    rows: list[BucketRow],
    intervals: list[str],
    totals: list[float],
    averages: list[float],
    medians: list[float],
) -> None:
    frame = tk.Frame(parent)
    frame.pack(fill="both", expand=True)

    canvas = tk.Canvas(frame, highlightthickness=0)
    table = tk.Frame(canvas)
    y_scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
    window_id = canvas.create_window((0, 0), window=table, anchor="nw")

    def update_scroll_region(_event: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    table.bind("<Configure>", update_scroll_region)

    canvas.grid(row=0, column=0, sticky="nsew")
    y_scroll.grid(row=0, column=1, sticky="ns")
    x_scroll.grid(row=1, column=0, sticky="ew")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)

    headers = ["Date", "Underlying", *intervals, "Row Total"]
    for column_index, header in enumerate(headers):
        add_cell(
            table,
            0,
            column_index,
            header,
            bg="#e5e7eb",
            bold=True,
            anchor="center",
        )

    for row_index, row in enumerate(rows, start=1):
        add_cell(table, row_index, 0, format_date(row.date_key), anchor="center")
        add_cell(table, row_index, 1, row.underlying, anchor="center")
        for column_index, value in enumerate(row.values, start=2):
            add_value_cell(table, row_index, column_index, value)
        add_value_cell(table, row_index, len(headers) - 1, row.total, bold=True)

    total_row_index = len(rows) + 1
    add_cell(table, total_row_index, 0, "Total", bg="#dbeafe", bold=True, anchor="center")
    add_cell(table, total_row_index, 1, "", bg="#dbeafe", bold=True, anchor="center")
    for column_index, value in enumerate(totals, start=2):
        add_value_cell(table, total_row_index, column_index, value, bg="#dbeafe", bold=True)
    add_value_cell(table, total_row_index, len(headers) - 1, sum(totals), bg="#dbeafe", bold=True)

    average_row_index = total_row_index + 1
    add_cell(table, average_row_index, 0, "Average", bg="#f0fdf4", bold=True, anchor="center")
    add_cell(table, average_row_index, 1, "", bg="#f0fdf4", bold=True, anchor="center")
    for column_index, value in enumerate(averages, start=2):
        add_value_cell(table, average_row_index, column_index, value, bg="#f0fdf4", bold=True)
    add_value_cell(
        table,
        average_row_index,
        len(headers) - 1,
        average_value([row.total for row in rows]),
        bg="#f0fdf4",
        bold=True,
    )

    median_row_index = average_row_index + 1
    add_cell(table, median_row_index, 0, "Median", bg="#eff6ff", bold=True, anchor="center")
    add_cell(table, median_row_index, 1, "", bg="#eff6ff", bold=True, anchor="center")
    for column_index, value in enumerate(medians, start=2):
        add_value_cell(table, median_row_index, column_index, value, bg="#eff6ff", bold=True)
    add_value_cell(
        table,
        median_row_index,
        len(headers) - 1,
        median_value([row.total for row in rows]),
        bg="#eff6ff",
        bold=True,
    )

    canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))


def build_chart_tab(
    parent: tk.Frame,
    intervals: list[str],
    totals: list[float],
    medians: list[float],
    metric_label: str,
) -> None:
    canvas = tk.Canvas(parent, bg="white", highlightthickness=0)
    x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=canvas.xview)
    canvas.configure(xscrollcommand=x_scroll.set)
    canvas.pack(fill="both", expand=True)
    x_scroll.pack(fill="x")

    def redraw(_event: tk.Event | None = None) -> None:
        canvas.delete("all")
        draw_bar_chart(canvas, intervals, totals, medians, metric_label)

    canvas.bind("<Configure>", redraw)
    redraw()


def build_cumulative_chart_tab(
    parent: tk.Frame,
    intervals: list[str],
    values: list[float],
    metric_label: str,
) -> None:
    canvas = tk.Canvas(parent, bg="white", highlightthickness=0)
    x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=canvas.xview)
    canvas.configure(xscrollcommand=x_scroll.set)
    canvas.pack(fill="both", expand=True)
    x_scroll.pack(fill="x")

    def redraw(_event: tk.Event | None = None) -> None:
        canvas.delete("all")
        draw_line_chart(
            canvas,
            intervals,
            values,
            f"Cumulative {metric_label} Using {INTERVAL_MINUTES}-Minute Buckets",
        )

    canvas.bind("<Configure>", redraw)
    redraw()


def draw_bar_chart(
    canvas: tk.Canvas,
    intervals: list[str],
    totals: list[float],
    medians: list[float],
    metric_label: str,
) -> None:
    if not intervals:
        return

    width = max(canvas.winfo_width(), len(intervals) * 78 + 100)
    height = max(canvas.winfo_height(), 500)
    margin_left = 80
    margin_right = 30
    margin_top = 35
    margin_bottom = 95
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    zero_y = margin_top + plot_height / 2
    max_abs = max([abs(value) for value in [*totals, *medians]] or [1])
    max_abs = max(max_abs, 1)
    slot_width = plot_width / len(intervals)
    bar_width = max(12, min(34, slot_width * 0.45))

    canvas.configure(scrollregion=(0, 0, width, height))
    canvas.create_text(
        margin_left,
        16,
        text=f"{metric_label} by {INTERVAL_MINUTES}-Minute Interval    Blue marker = median",
        anchor="w",
        font=("Arial", 12, "bold"),
        fill="#111827",
    )
    canvas.create_line(margin_left, zero_y, width - margin_right, zero_y, fill="#6b7280", width=1)
    canvas.create_text(
        margin_left - 8,
        zero_y,
        text="0",
        anchor="e",
        font=("Arial", 9),
        fill="#374151",
    )

    scale = (plot_height / 2) / max_abs
    for index, (interval, value) in enumerate(zip(intervals, totals)):
        x_center = margin_left + (index + 0.5) * (plot_width / len(intervals))
        x0 = x_center - bar_width / 2
        x1 = x_center + bar_width / 2
        bar_y = zero_y - value * scale
        y0 = min(zero_y, bar_y)
        y1 = max(zero_y, bar_y)
        color = "#16a34a" if value >= 0 else "#b91c1c"
        canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline=color)
        label_y = y0 - 10 if value >= 0 else y1 + 10
        canvas.create_text(
            x_center,
            label_y,
            text=format_value(value),
            anchor="center",
            font=("Arial", 8),
            fill=color,
        )
        canvas.create_text(
            x_center,
            height - margin_bottom + 35,
            text=interval,
            angle=60,
            anchor="e",
            font=("Arial", 8),
            fill="#374151",
        )
        median = medians[index]
        median_y = zero_y - median * scale
        canvas.create_line(
            x_center - bar_width * 0.62,
            median_y,
            x_center + bar_width * 0.62,
            median_y,
            fill="#2563eb",
            width=2,
        )
        canvas.create_oval(
            x_center - 3,
            median_y - 3,
            x_center + 3,
            median_y + 3,
            fill="#2563eb",
            outline="#2563eb",
        )
        canvas.create_text(
            x_center,
            median_y - 10 if median >= 0 else median_y + 10,
            text=format_value(median),
            anchor="center",
            font=("Arial", 8),
            fill="#2563eb",
        )


def draw_line_chart(canvas: tk.Canvas, intervals: list[str], values: list[float], title: str) -> None:
    if not intervals:
        return

    width = max(canvas.winfo_width(), len(intervals) * 78 + 100)
    height = max(canvas.winfo_height(), 500)
    margin_left = 80
    margin_right = 30
    margin_top = 40
    margin_bottom = 95
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    min_value = min([0, *values])
    max_value = max([0, *values])
    span = max(max_value - min_value, 1)

    def y_for(value: float) -> float:
        return margin_top + (max_value - value) / span * plot_height

    canvas.configure(scrollregion=(0, 0, width, height))
    canvas.create_text(
        margin_left,
        16,
        text=title,
        anchor="w",
        font=("Arial", 12, "bold"),
        fill="#111827",
    )
    zero_y = y_for(0)
    canvas.create_line(margin_left, zero_y, width - margin_right, zero_y, fill="#6b7280", width=1)
    canvas.create_text(
        margin_left - 8,
        zero_y,
        text="0",
        anchor="e",
        font=("Arial", 9),
        fill="#374151",
    )

    points = []
    for index, value in enumerate(values):
        x = margin_left + (index + 0.5) * (plot_width / len(intervals))
        y = y_for(value)
        points.append((x, y, value))

    for start, end in zip(points, points[1:]):
        canvas.create_line(start[0], start[1], end[0], end[1], fill="#2563eb", width=2)

    for index, (x, y, value) in enumerate(points):
        color = "#b91c1c" if value < 0 else "#2563eb"
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=color)
        label_y = y - 14 if value >= 0 else y + 14
        canvas.create_text(
            x,
            label_y,
            text=format_value(value),
            anchor="center",
            font=("Arial", 8),
            fill=color,
        )
        canvas.create_text(
            x,
            height - margin_bottom + 35,
            text=intervals[index],
            angle=60,
            anchor="e",
            font=("Arial", 8),
            fill="#374151",
        )


def column_totals(rows: list[BucketRow], intervals: list[str]) -> list[float]:
    totals = []
    for index in range(len(intervals)):
        totals.append(
            sum(row.values[index] for row in rows if row.values[index] is not None)
        )
    return totals


def column_medians(rows: list[BucketRow], intervals: list[str]) -> list[float]:
    medians = []
    for index in range(len(intervals)):
        values = sorted(
            row.values[index]
            for row in rows
            if row.values[index] is not None
        )
        medians.append(median_value(values))
    return medians


def column_averages(rows: list[BucketRow], intervals: list[str]) -> list[float]:
    averages = []
    for index in range(len(intervals)):
        values = [
            row.values[index]
            for row in rows
            if row.values[index] is not None
        ]
        averages.append(average_value(values))
    return averages


def average_value(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median_value(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def cumulative_values(values_to_sum: list[float]) -> list[float]:
    cumulative = 0.0
    values = []
    for value in values_to_sum:
        cumulative += value
        values.append(cumulative)
    return values


def next_value_at_or_after(
    target_time: str,
    values_by_time: dict[str, float],
    available_times: list[str],
) -> float | None:
    index = bisect_left(available_times, target_time)
    if index >= len(available_times):
        return None
    return values_by_time[available_times[index]]


def parse_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def metric_value(record: dict[str, str], fields: tuple[str, ...]) -> float | None:
    values = [parse_float(record.get(field)) for field in fields]
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def last_float(
    records: list[dict[str, str]],
    field: str,
    default: float | None = None,
) -> float | None:
    for record in reversed(records):
        value = parse_float(record.get(field))
        if value is not None:
            return value
    return default


def closest_strike_average_iv(
    records: list[dict[str, str]],
    closest_strikes: int,
) -> float | None:
    if closest_strikes <= 0:
        return None
    record = next_record_at_or_after(records, START_TIME)
    if record is None:
        return None
    universal_mid = parse_float(record.get("universal_mid"))
    if universal_mid is None:
        return None

    ivs_by_strike = []
    for field, value in record.items():
        strike = strike_from_model_iv_field(field)
        if strike is None:
            continue
        iv = parse_float(value)
        if iv is None:
            continue
        ivs_by_strike.append((abs(strike - universal_mid), strike, iv))
    if not ivs_by_strike:
        return None

    closest = sorted(ivs_by_strike)[:closest_strikes]
    return sum(iv for _, _, iv in closest) / len(closest)


def next_record_at_or_after(
    records: list[dict[str, str]],
    target_time: str,
) -> dict[str, str] | None:
    for record in records:
        timestamp = record.get("timestamp", "")
        if len(timestamp) >= 16 and timestamp[11:16] >= target_time:
            return record
    return None


def strike_from_model_iv_field(field: str) -> int | None:
    prefix = "strike_"
    suffix = "_model_iv"
    if not field.startswith(prefix) or not field.endswith(suffix):
        return None
    try:
        return int(field[len(prefix) : -len(suffix)])
    except ValueError:
        return None


def sum_optional(values) -> float | None:
    numeric_values = [value for value in values if value is not None]
    return sum(numeric_values) if numeric_values else None


def avg_optional(values) -> float | None:
    numeric_values = [value for value in values if value is not None]
    return sum(numeric_values) / len(numeric_values) if numeric_values else None


def parse_years(value: str) -> list[str]:
    years = [part.strip() for part in value.split(",") if part.strip()]
    return years or ["2026"]


def years_for_filter(
    year_value: str,
    date_filter: tuple[datetime, datetime] | None,
) -> list[str]:
    if date_filter is None:
        return parse_years(year_value)
    start, end = date_filter
    return [str(year) for year in range(start.year, end.year + 1)]


def parse_date_filter(
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime, datetime] | None:
    if not start_date and not end_date:
        return None
    if not start_date or not end_date:
        raise SystemExit("Use both --start-date and --end-date.")
    start = parse_date_key(start_date)
    end = parse_date_key(end_date)
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date.")
    return start, end


def parse_date_key(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%d%m%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise SystemExit(f"Could not parse date {value!r}. Use DDMMYYYY or YYYY-MM-DD.")


def path_in_date_filter(path: Path, date_filter: tuple[datetime, datetime] | None) -> bool:
    if date_filter is None:
        return True
    try:
        value = datetime.strptime(path.stem[:8], "%Y%m%d")
    except ValueError:
        return False
    start, end = date_filter
    return start <= value <= end


def date_filter_label(
    years: list[str],
    date_filter: tuple[datetime, datetime] | None,
) -> str:
    if date_filter is None:
        return ", ".join(years)
    start, end = date_filter
    return f"{start:%d%b%y}-{end:%d%b%y}"


def format_value(value: float | None) -> str:
    return "" if value is None else f"{value:.0f}"


def format_summary_number(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def format_percent_value(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.2f}%"


def format_plain_percent_value(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}%"


def summary_fg(row: SummaryRow, column_index: int) -> str:
    values = {
        2: row.frozen_iv_pnl,
        3: row.total_pnl,
        4: row.gamma_diff_pnl,
    }
    value = values.get(column_index)
    return "#b91c1c" if value is not None and value < 0 else "#111827"


def format_date(date_key: str) -> str:
    try:
        return datetime.strptime(date_key[:8], "%Y%m%d").strftime("%d%b%y")
    except ValueError:
        return date_key


def date_key_for_sort(value: str) -> str:
    return value[:8]


def add_value_cell(
    parent: tk.Frame,
    row: int,
    column: int,
    value: float | None,
    *,
    bg: str = "white",
    bold: bool = False,
) -> None:
    fg = "#b91c1c" if value is not None and value < 0 else "#111827"
    add_cell(parent, row, column, format_value(value), bg=bg, fg=fg, bold=bold, anchor="e")


def add_cell(
    parent: tk.Frame,
    row: int,
    column: int,
    text: str,
    *,
    bg: str = "white",
    fg: str = "#111827",
    bold: bool = False,
    anchor: str = "e",
) -> None:
    width = 11 if column >= 2 else 10
    font = ("Arial", 9, "bold" if bold else "normal")
    label = tk.Label(
        parent,
        text=text,
        width=width,
        anchor=anchor,
        bg=bg,
        fg=fg,
        font=font,
        padx=6,
        pady=4,
        relief="solid",
        borderwidth=1,
    )
    label.grid(row=row, column=column, sticky="nsew")


if __name__ == "__main__":
    main()
