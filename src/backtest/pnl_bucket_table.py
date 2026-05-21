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


@dataclass(frozen=True)
class BucketRow:
    date_key: str
    underlying: str
    values: list[float | None]
    total: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Show processed PnL buckets in a GUI table.")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR)
    parser.add_argument("--year", default="2026")
    parser.add_argument("--underlying", choices=("NIFTY", "SENSEX"), default=None)
    args = parser.parse_args()

    intervals = interval_labels()
    rows = load_bucket_rows(args.processed_dir, args.year, args.underlying, intervals)
    show_table(rows, intervals, args.year, args.underlying)


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
    year: str,
    underlying_filter: str | None,
    intervals: list[str],
) -> list[BucketRow]:
    rows = []
    points = interval_points()
    for path in sorted(processed_dir.glob(f"{year}*.csv"), reverse=True):
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
            value = parse_float(record.get("portfolio_total_pnl"))
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
        rows.append(BucketRow(path.stem, underlying, bucket_values, total))
    return rows


def show_table(
    rows: list[BucketRow],
    intervals: list[str],
    year: str,
    underlying: str | None,
) -> None:
    root = tk.Tk()
    suffix = f" - {underlying}" if underlying else ""
    root.title(f"PnL {INTERVAL_MINUTES}-Min Buckets {year}{suffix}")
    root.geometry("1500x780")

    title = tk.Label(
        root,
        text=f"Portfolio Total PnL Change by {INTERVAL_MINUTES}-Minute Interval ({year}){suffix}",
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
    build_chart_tab(chart_tab, intervals, totals, medians)
    build_cumulative_chart_tab(cumulative_tab, intervals, cumulative_totals)

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
) -> None:
    canvas = tk.Canvas(parent, bg="white", highlightthickness=0)
    x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=canvas.xview)
    canvas.configure(xscrollcommand=x_scroll.set)
    canvas.pack(fill="both", expand=True)
    x_scroll.pack(fill="x")

    def redraw(_event: tk.Event | None = None) -> None:
        canvas.delete("all")
        draw_bar_chart(canvas, intervals, totals, medians)

    canvas.bind("<Configure>", redraw)
    redraw()


def build_cumulative_chart_tab(parent: tk.Frame, intervals: list[str], values: list[float]) -> None:
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
            f"Cumulative Total PnL Using {INTERVAL_MINUTES}-Minute Buckets",
        )

    canvas.bind("<Configure>", redraw)
    redraw()


def draw_bar_chart(
    canvas: tk.Canvas,
    intervals: list[str],
    totals: list[float],
    medians: list[float],
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
        text=f"Total PnL by {INTERVAL_MINUTES}-Minute Interval    Blue marker = median",
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


def format_value(value: float | None) -> str:
    return "" if value is None else f"{value:.0f}"


def format_date(date_key: str) -> str:
    try:
        return datetime.strptime(date_key, "%Y%m%d").strftime("%d%b%y")
    except ValueError:
        return date_key


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
