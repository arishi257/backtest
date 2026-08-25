from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import io
import json
import math
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.config import (
    DEFAULT_PROCESSED_OUTPUT_DIR,
    DEFAULT_WORKBOOK,
    MARKET_OPEN,
    normalize_underlying,
)
from backtest.data import FutureBar, FutureSeries, OptionDataset
from backtest.headless_portfolio import (
    GammaDiffTracker,
    HeadlessFrozenIvState,
    HeadlessPortfolioMetrics,
    HeadlessPortfolioState,
    ParkGammaTracker,
    is_hedge_start_time,
    is_top_move_time,
    nearest_result_strike,
)
from backtest.portfolio import build_sample_portfolio
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest.spot_data import load_spot_series
from fit_sensex.ui.app import total_row_numeric_values, weighted_price_total
from vol_dashboard.market.spot import SpotStore
from vol_dashboard.ui.app import VolDashboardApp


FYERS_ROOT = Path(r"C:\options data\Fyers")
DEFAULT_OPTIONS_ZIP = FYERS_ROOT / "fyers_option-20260528T035403Z-3-001.zip"
DEFAULT_INDEX_ZIP = FYERS_ROOT / "fyers_index-20260527T143533Z-3-001.zip"
FYERS_CACHE_DIR = Path(__file__).resolve().parents[2] / ".backtest_data_cache" / "fyers_1m"
MIN_HEDGE_THRESHOLD_LOTS = 1.2
IST = ZoneInfo("Asia/Kolkata")
OPTION_RE_TEMPLATE = (
    r"^1m/{underlying}(?P<expiry>\d{{6}})(?P<strike>\d+)(?P<option_type>CE|PE)_"
    r"(?P<trade_date>\d{{4}}_\d{{2}}_\d{{2}})\.json$"
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--refresh-ms must be greater than zero.")
    return parsed


@dataclass
class VolMetrics:
    close_to_close_vol: float | None = None
    park_vol: float | None = None
    gk_vol: float | None = None


class OhlcVolTracker:
    def __init__(self) -> None:
        self.previous_bar: FutureBar | None = None
        self.close_to_close_variance = 0.0
        self.park_variance = 0.0
        self.gk_variance = 0.0
        self.calendar_days: float | None = None
        self.intraday_var: float | None = None
        self.has_values = False

    def update(
        self,
        timestamp: datetime,
        bar: FutureBar | None,
        calendar_days: float,
        intraday_var: float,
    ) -> None:
        self.calendar_days = calendar_days
        self.intraday_var = intraday_var
        if bar is None:
            return
        if self.previous_bar is None:
            self.previous_bar = bar
            return
        if is_top_move_time(timestamp):
            self.close_to_close_variance += close_to_close_variance(
                self.previous_bar.close,
                bar.close,
            )
            self.park_variance += parkinson_variance(bar.high, bar.low)
            self.gk_variance += garman_klass_variance(
                bar.open,
                bar.high,
                bar.low,
                bar.close,
            )
            self.has_values = True
        self.previous_bar = bar

    def metrics(self) -> VolMetrics:
        if (
            not self.has_values
            or self.calendar_days is None
            or self.intraday_var is None
            or self.intraday_var <= 0
        ):
            return VolMetrics()
        return VolMetrics(
            close_to_close_vol=scaled_volatility(
                self.close_to_close_variance,
                self.calendar_days,
                self.intraday_var,
            ),
            park_vol=scaled_volatility(
                self.park_variance,
                self.calendar_days,
                self.intraday_var,
            ),
            gk_vol=scaled_volatility(
                self.gk_variance,
                self.calendar_days,
                self.intraday_var,
            ),
        )


class DynamicGammaThresholdPortfolioState(HeadlessPortfolioState):
    def update(
        self,
        result,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> HeadlessPortfolioMetrics:
        if self.portfolio is None and is_hedge_start_time(timestamp):
            if self.session is not None:
                self.portfolio = build_sample_portfolio(self.session, result)
        if self.portfolio is None:
            return HeadlessPortfolioMetrics()

        risk_rows = self.portfolio.calculate(result)
        totals = total_row_numeric_values(risk_rows, result)
        options_bs_delta_lots = totals[19] if len(totals) > 19 else None
        gamma_l = totals[21] if len(totals) > 21 else None
        options_pv = weighted_price_total(risk_rows, "mid_mkt")
        dynamic_threshold = dynamic_gamma_threshold(gamma_l, hedge_threshold)
        total_pnl = self._update_pnl_with_threshold(
            result,
            timestamp,
            options_pv,
            options_bs_delta_lots,
            funding_rate,
            brokerage_rate,
            dynamic_threshold,
        )
        return HeadlessPortfolioMetrics(total_pnl=total_pnl, gamma_l=gamma_l)

    def _update_pnl_with_threshold(
        self,
        result,
        timestamp: datetime,
        options_pv: float,
        options_bs_delta_lots,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> float | None:
        if not isinstance(options_bs_delta_lots, (int, float)):
            return None

        rows_by_strike = {row.strike: row for row in result.rows}
        if self.options_pv_snapshot is None and is_hedge_start_time(timestamp):
            self.options_pv_snapshot = options_pv
            self.hedge_strike = nearest_result_strike(result, result.universal_mid)

        if self.options_pv_snapshot is not None and self.hedge_strike is not None:
            combined_delta = options_bs_delta_lots + self.cumulative_hedge_lots
            if abs(combined_delta) > hedge_threshold:
                self._add_hedge_trade(
                    round(-combined_delta),
                    result,
                    rows_by_strike,
                    timestamp,
                    funding_rate,
                    brokerage_rate,
                )

        options_pnl = (
            options_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        hedge_pnl = self._hedge_pnl(result, rows_by_strike, funding_rate, brokerage_rate)
        return (options_pnl or 0.0) + (hedge_pnl or 0.0)


class DynamicGammaThresholdFrozenIvState(HeadlessFrozenIvState):
    def update(
        self,
        result,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> HeadlessPortfolioMetrics:
        if not self.frozen_ivs:
            if not is_hedge_start_time(timestamp):
                return HeadlessPortfolioMetrics()
            portfolio = build_sample_portfolio(self.session, result)
            if portfolio is None:
                return HeadlessPortfolioMetrics()
            frozen_ivs = self._capture_position_ivs(
                portfolio.positions,
                result,
                funding_rate,
            )
            if len(frozen_ivs) != len(portfolio.positions):
                return HeadlessPortfolioMetrics()
            self.portfolio = portfolio
            self.frozen_ivs = frozen_ivs

        risk_rows = self._frozen_risk_rows(result, funding_rate)
        totals = total_row_numeric_values(risk_rows, result)
        options_bs_delta_lots = totals[19] if len(totals) > 19 else None
        gamma_l = totals[21] if len(totals) > 21 else None
        options_pv = weighted_price_total(risk_rows, "mid_mkt")
        dynamic_threshold = dynamic_gamma_threshold(gamma_l, hedge_threshold)
        total_pnl = self._update_pnl_with_threshold(
            result,
            timestamp,
            options_pv,
            options_bs_delta_lots,
            funding_rate,
            brokerage_rate,
            dynamic_threshold,
        )
        return HeadlessPortfolioMetrics(total_pnl=total_pnl, gamma_l=gamma_l)

    def _update_pnl_with_threshold(
        self,
        result,
        timestamp: datetime,
        options_pv: float,
        options_bs_delta_lots,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> float | None:
        if not isinstance(options_bs_delta_lots, (int, float)):
            return None

        rows_by_strike = {row.strike: row for row in result.rows}
        if self.options_pv_snapshot is None and is_hedge_start_time(timestamp):
            self.options_pv_snapshot = options_pv
            self.hedge_strike = nearest_result_strike(result, result.universal_mid)

        if self.options_pv_snapshot is not None and self.hedge_strike is not None:
            combined_delta = options_bs_delta_lots + self.cumulative_hedge_lots
            if abs(combined_delta) > hedge_threshold:
                self._add_hedge_trade(
                    round(-combined_delta),
                    result,
                    rows_by_strike,
                    timestamp,
                    funding_rate,
                    brokerage_rate,
                )

        options_pnl = (
            options_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        hedge_pnl = self._hedge_pnl(result, rows_by_strike, funding_rate, brokerage_rate)
        return (options_pnl or 0.0) + (hedge_pnl or 0.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a separate 1-minute FYERS headless backtest."
    )
    parser.add_argument("--date", default="27052026", help="Trading date, e.g. 27052026.")
    parser.add_argument("--start-date", help="Batch start date, e.g. 06012026.")
    parser.add_argument("--end-date", help="Batch end date, e.g. 30032026.")
    parser.add_argument(
        "--underlying",
        default="SENSEX",
        type=normalize_underlying,
        choices=("SENSEX", "NIFTY"),
    )
    parser.add_argument("--options-zip", type=Path, default=None)
    parser.add_argument("--index-zip", type=Path, default=DEFAULT_INDEX_ZIP)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument(
        "--processed-output-dir",
        type=Path,
        default=DEFAULT_PROCESSED_OUTPUT_DIR,
    )
    parser.add_argument(
        "--dynamic-gamma-threshold-ratio",
        type=float,
        default=0.40,
        help="Re-hedge threshold as a ratio of current absolute gamma lots.",
    )
    parser.add_argument(
        "--plot-output",
        type=Path,
        default=None,
        help="Optional PNG path for a running total PnL plot.",
    )
    parser.add_argument(
        "--refresh-ms",
        type=positive_int,
        default=None,
        help="Milliseconds between GUI replay ticks, e.g. 200 for faster playback.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the ticking dashboard GUI for a single-date 1-minute replay.",
    )
    parser.add_argument(
        "--batch-workers",
        type=int,
        default=None,
        help="Parallel worker count for batch mode. Defaults to a conservative CPU-based value.",
    )
    args = parser.parse_args()

    if args.start_date or args.end_date:
        run_batch(args)
        return
    if args.gui:
        run_ticking_gui(args)
        return

    trade_date = parse_date_key(args.date)
    options_zip = args.options_zip or default_options_zip(args.underlying, trade_date)
    dataset = load_fyers_dataset(
        options_zip,
        args.index_zip,
        trade_date,
        args.underlying,
    )
    future_series = dataset.future_series
    if future_series is None:
        raise SystemExit(f"{args.underlying} index OHLC was not loaded.")

    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, args.workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    portfolio_states = {
        session.spec.tab_name: DynamicGammaThresholdPortfolioState(session=session)
        for session in sessions
    }
    frozen_states = {
        session.spec.tab_name: DynamicGammaThresholdFrozenIvState(session)
        for session in sessions
    }
    gamma_trackers = {session.spec.tab_name: GammaDiffTracker() for session in sessions}
    park_gamma_trackers = {session.spec.tab_name: ParkGammaTracker() for session in sessions}
    vol_trackers = {session.spec.tab_name: OhlcVolTracker() for session in sessions}
    final_metrics: dict[str, dict[str, float | None]] = {}
    pnl_points: dict[str, list[tuple[datetime, float | None, float | None]]] = {
        session.spec.tab_name: [] for session in sessions
    }

    cycles = 0
    analytics = 0
    while replay.advance():
        cycles += 1
        timestamp = replay.now()
        for session in sessions:
            result = session.analytics.calculate(session.store.snapshot())
            if result is None:
                continue
            analytics += 1
            tab_name = session.spec.tab_name
            portfolio_metrics = portfolio_states[tab_name].update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.dynamic_gamma_threshold_ratio,
            )
            frozen_metrics = frozen_states[tab_name].update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.dynamic_gamma_threshold_ratio,
            )
            gamma_diff_total = gamma_trackers[tab_name].update(
                timestamp,
                result.universal_mid,
                portfolio_metrics.gamma_l,
            )
            bar = future_series.bar_at(timestamp)
            park_gamma_metrics = park_gamma_trackers[tab_name].update(
                bar,
                portfolio_metrics.gamma_l,
            )
            vol_trackers[tab_name].update(
                timestamp,
                bar,
                session.config.market.calendar_days,
                result.intraday_var,
            )
            vol_metrics = vol_trackers[tab_name].metrics()
            final_metrics[tab_name] = {
                "portfolio_total_pnl": portfolio_metrics.total_pnl,
                "portfolio_gamma_l": portfolio_metrics.gamma_l,
                "portfolio_gamma_diff_total": gamma_diff_total,
                "park_gamma_pnl_diff_total": park_gamma_metrics.park_gamma_pnl_diff_total,
                "gk_gamma_pnl_diff_total": park_gamma_metrics.gk_gamma_pnl_diff_total,
                "frozen_iv_total_pnl": frozen_metrics.total_pnl,
                "close_to_close_vol": vol_metrics.close_to_close_vol,
                "park_vol": vol_metrics.park_vol,
                "gk_vol": vol_metrics.gk_vol,
            }
            pnl_points[tab_name].append(
                (timestamp, portfolio_metrics.total_pnl, frozen_metrics.total_pnl)
            )

    print(
        f"Completed {cycles} 1-minute replay cycles across {len(sessions)} expiries. "
        f"Analytics rows: {analytics}. "
        f"Options source: {options_zip}. Index source: {args.index_zip}."
    )
    if final_metrics:
        print("Final running PnL and volatility metrics:")
        for tab_name, metrics in final_metrics.items():
            print(f"  {tab_name}:")
            for name, value in metrics.items():
                print(f"    {name}: {format_final_metric(name, value)}")
    if args.plot_output is not None:
        write_pnl_plot(pnl_points, args.plot_output)
        print(f"Running PnL plot: {args.plot_output}")


def load_fyers_dataset(
    options_zip: Path,
    index_zip: Path,
    trade_date: date,
    underlying: str = "SENSEX",
) -> OptionDataset:
    normalized_underlying = normalize_underlying(underlying)
    option_frame = load_option_frame(options_zip, trade_date, normalized_underlying)
    future_series = load_index_series(index_zip, trade_date, normalized_underlying)
    timestamps = pd.date_range(
        pd.Timestamp(f"{trade_date} {MARKET_OPEN}:00", tz=IST),
        pd.Timestamp(f"{trade_date} 15:24:00", tz=IST),
        freq="min",
    )
    return OptionDataset(
        frame=option_frame,
        trade_date=trade_date,
        timestamps=timestamps,
        underlying=normalized_underlying,
        future_series=future_series,
    )


def run_ticking_gui(args: argparse.Namespace) -> None:
    if args.plot_output is not None:
        raise SystemExit("--plot-output is only supported for headless single-date runs.")

    trade_date = parse_date_key(args.date)
    options_zip = args.options_zip or default_options_zip(args.underlying, trade_date)
    dataset = load_fyers_dataset(
        options_zip,
        args.index_zip,
        trade_date,
        args.underlying,
    )
    future_series = dataset.future_series
    if future_series is None:
        print(f"{args.underlying} index OHLC was not loaded.")

    spot_points = []
    spot_source = None
    try:
        spot_series = load_spot_series(dataset.trade_date, dataset.underlying)
        spot_points = spot_series.points
        spot_source = str(spot_series.source_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Spot data unavailable: {exc}")

    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(
        dataset,
        args.workbook,
        current_time,
        refresh_ms=args.refresh_ms,
    )
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    print(
        f"Loaded {len(dataset.frame):,} {dataset.underlying} 1-minute option rows, "
        f"{len(sessions)} expiries, {len(dataset.timestamps)} replay minutes. "
        f"Options source: {options_zip}. Index source: {args.index_zip}."
    )
    for session in sessions:
        print(f"  {session.spec.tab_name}: {len(session.chain)} strikes")
    print(
        f"Replay refresh: "
        f"{min(session.config.market.refresh_ms for session in sessions)} ms"
    )

    root = tk_root()
    root.title(f"{dataset.underlying} 1m Backtest Vol Dashboard")
    app = VolDashboardApp(
        root,
        sessions,
        spot_store=SpotStore(),
        spot_points=spot_points,
        spot_source=spot_source,
        future_series=future_series,
        processed_output_dir=args.processed_output_dir,
        before_refresh=replay.advance,
        clock=replay.now,
    )
    app.start()


def tk_root():
    import tkinter as tk

    return tk.Tk()


def run_batch(args: argparse.Namespace) -> None:
    if not args.start_date or not args.end_date:
        raise SystemExit("Use both --start-date and --end-date for batch mode.")
    start = parse_date_key(args.start_date)
    end = parse_date_key(args.end_date)
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date.")
    if args.plot_output is not None:
        raise SystemExit("--plot-output is only supported for single-date runs.")

    trade_dates = weekday_dates(start, end)
    workers = resolve_batch_workers(args.batch_workers, len(trade_dates))
    print(f"Batch dates: {len(trade_dates)} weekday(s). Workers: {workers}.")

    rows_by_date: dict[date, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                run_single_backtest_subprocess,
                build_single_date_command(args, trade_date),
            ): trade_date
            for trade_date in trade_dates
        }
        for future in as_completed(futures):
            trade_date = futures[future]
            try:
                metrics = future.result()
            except Exception as exc:
                rows_by_date[trade_date] = {
                    "date": trade_date.strftime("%d%m%Y"),
                    "status": "SKIP",
                    "detail": str(exc),
                }
                print(f"{args.underlying} {trade_date:%d%m%Y}: SKIP - {exc}")
                continue
            rows_by_date[trade_date] = {
                "date": trade_date.strftime("%d%m%Y"),
                "status": "OK",
                "detail": f"{metrics['cycles']:.0f} cycles",
                **metrics,
            }
            print(f"{args.underlying} {trade_date:%d%m%Y}: OK")

    rows = [
        rows_by_date[trade_date]
        for trade_date in trade_dates
        if rows_by_date.get(trade_date, {}).get("status") == "OK"
    ]
    if not rows:
        print("No dates with available data were found in the requested range.")
        return
    show_batch_results_gui(rows, args)


def build_single_date_command(args: argparse.Namespace, trade_date: date) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "backtest_1m",
        "--underlying",
        args.underlying,
        "--date",
        trade_date.strftime("%d%m%Y"),
        "--index-zip",
        str(args.index_zip),
        "--workbook",
        str(args.workbook),
        "--dynamic-gamma-threshold-ratio",
        str(args.dynamic_gamma_threshold_ratio),
    ]
    if args.options_zip is not None:
        command.extend(["--options-zip", str(args.options_zip)])
    return command


def run_single_backtest_subprocess(command: list[str]) -> dict[str, float | None]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(last_non_empty_line(detail) or f"exit code {completed.returncode}")
    return parse_single_run_output(completed.stdout)


def parse_single_run_output(output: str) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for line in output.splitlines():
        completed_match = re.search(r"Completed\s+(\d+)\s+1-minute replay cycles", line)
        if completed_match:
            metrics["cycles"] = float(completed_match.group(1))
            continue
        metric_match = re.match(r"\s{4}([A-Za-z0-9_]+):\s+(.+?)\s*$", line)
        if metric_match:
            metrics[metric_match.group(1)] = parse_metric_value(metric_match.group(2))
    if "portfolio_total_pnl" not in metrics:
        raise RuntimeError(last_non_empty_line(output) or "No final metrics were printed.")
    return metrics


def parse_metric_value(value: str) -> float | None:
    if value == "--":
        return None
    if value.endswith("%"):
        return float(value[:-1]) / 100.0
    return float(value)


def last_non_empty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def resolve_batch_workers(requested_workers: int | None, date_count: int) -> int:
    if date_count <= 1:
        return 1
    if requested_workers is not None:
        if requested_workers < 1:
            raise SystemExit("--batch-workers must be at least 1.")
        return min(requested_workers, date_count)
    cpu_count = os.cpu_count() or 2
    return min(max(cpu_count - 1, 1), date_count, 4)


def weekday_dates(start: date, end: date) -> list[date]:
    current = start
    values = []
    while current <= end:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def batch_result_columns() -> list[str]:
    return [
        "date",
        "portfolio_total_pnl",
        "portfolio_gamma_l",
        "portfolio_gamma_diff_total",
        "park_gamma_pnl_diff_total",
        "gk_gamma_pnl_diff_total",
        "frozen_iv_total_pnl",
        "close_to_close_vol",
        "park_vol",
        "gk_vol",
        "detail",
    ]


def show_batch_results_gui(rows: list[dict[str, object]], args: argparse.Namespace) -> None:
    import tkinter as tk
    from tkinter import ttk

    columns = batch_result_columns()
    root = tk.Tk()
    root.title(f"{args.underlying} 1m Batch Results")
    root.geometry("1280x560")

    header = ttk.Frame(root, padding=(10, 10, 10, 6))
    header.pack(fill=tk.X)
    title = ttk.Label(
        header,
        text=(
            f"{args.underlying} 1m batch results | "
            f"{args.start_date} to {args.end_date} | "
            f"threshold ratio {args.dynamic_gamma_threshold_ratio:.2f}"
        ),
        font=("Segoe UI", 11, "bold"),
    )
    title.pack(side=tk.LEFT)
    count = ttk.Label(header, text=f"{len(rows)} available date(s)")
    count.pack(side=tk.RIGHT)

    table_frame = ttk.Frame(root, padding=(10, 0, 10, 10))
    table_frame.pack(fill=tk.BOTH, expand=True)

    tree = ttk.Treeview(table_frame, columns=columns, show="headings")
    vertical_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
    horizontal_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.configure(yscrollcommand=vertical_scroll.set, xscrollcommand=horizontal_scroll.set)

    column_widths = {
        "date": 90,
        "portfolio_total_pnl": 135,
        "portfolio_gamma_l": 120,
        "portfolio_gamma_diff_total": 165,
        "park_gamma_pnl_diff_total": 185,
        "gk_gamma_pnl_diff_total": 170,
        "frozen_iv_total_pnl": 145,
        "close_to_close_vol": 140,
        "park_vol": 95,
        "gk_vol": 90,
        "detail": 105,
    }
    for column in columns:
        tree.heading(column, text=column)
        tree.column(
            column,
            width=column_widths.get(column, 120),
            minwidth=80,
            anchor=tk.E if column not in ("date", "detail") else tk.W,
            stretch=column == "detail",
        )

    for row in rows:
        tree.insert(
            "",
            tk.END,
            values=[format_batch_value(column, row.get(column)) for column in columns],
        )

    tree.grid(row=0, column=0, sticky="nsew")
    vertical_scroll.grid(row=0, column=1, sticky="ns")
    horizontal_scroll.grid(row=1, column=0, sticky="ew")
    table_frame.rowconfigure(0, weight=1)
    table_frame.columnconfigure(0, weight=1)

    root.mainloop()


def format_batch_value(column: str, value: object) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        if column.endswith("_vol"):
            return f"{value:.2%}"
        return f"{value:.0f}"
    return str(value)


def load_fyers_sensex_dataset(
    options_zip: Path,
    index_zip: Path,
    trade_date: date,
) -> OptionDataset:
    return load_fyers_dataset(options_zip, index_zip, trade_date, "SENSEX")


def load_option_frame(
    options_zip: Path,
    trade_date: date,
    underlying: str = "SENSEX",
) -> pd.DataFrame:
    if not options_zip.exists():
        raise FileNotFoundError(options_zip)
    normalized_underlying = normalize_underlying(underlying)
    cache_path = option_cache_path(options_zip, trade_date, normalized_underlying)
    if cache_path.exists():
        return read_option_frame_cache(cache_path)
    legacy_cache_path = legacy_option_cache_path(options_zip, trade_date, normalized_underlying)
    if legacy_cache_path.exists():
        frame = read_option_frame_cache(legacy_cache_path)
        write_option_frame_cache(frame, cache_path)
        return frame

    trade_date_text = trade_date.strftime("%Y_%m_%d")
    rows = []
    option_re = option_regex(normalized_underlying)
    with open_option_archive(options_zip, normalized_underlying, trade_date) as archive:
        for member in archive.namelist():
            match = option_re.match(member)
            if match is None or match.group("trade_date") != trade_date_text:
                continue
            ticker = (
                f"{normalized_underlying}{match.group('expiry')}{match.group('strike')}"
                f"{match.group('option_type')}"
            )
            expiry = datetime.strptime(match.group("expiry"), "%y%m%d").date()
            strike = int(match.group("strike"))
            option_type = match.group("option_type")
            candles = json.loads(archive.read(member)).get("candles", [])
            for candle in candles:
                rows.append(
                    {
                        "ticker": ticker,
                        "timestamp": candle_timestamp(candle[0]),
                        "expiry": expiry,
                        "strike": strike,
                        "option_type": option_type,
                        "close": float(candle[4]),
                    }
                )
    if not rows:
        raise ValueError(
            f"No 1m {normalized_underlying} option rows found for {trade_date} in {options_zip}."
        )
    frame = pd.DataFrame(rows)
    frame = frame.sort_values(["timestamp", "ticker"])
    write_option_frame_cache(frame, cache_path)
    return frame


def load_index_series(
    index_zip: Path,
    trade_date: date,
    underlying: str = "SENSEX",
) -> FutureSeries:
    if not index_zip.exists():
        raise FileNotFoundError(index_zip)
    normalized_underlying = normalize_underlying(underlying)
    cache_path = index_cache_path(index_zip, trade_date, normalized_underlying)
    if cache_path.exists():
        return read_index_series_cache(cache_path, normalized_underlying)
    legacy_cache_path = legacy_index_cache_path(index_zip, trade_date, normalized_underlying)
    if legacy_cache_path.exists():
        series = read_index_series_cache(legacy_cache_path, normalized_underlying)
        write_index_series_cache(series, cache_path)
        return series

    member_name = f"1m/{normalized_underlying}_{trade_date:%Y_%m_%d}.json"
    with zipfile.ZipFile(index_zip) as outer:
        nested_name = find_index_nested_zip(outer, trade_date, member_name)
        nested_bytes = outer.read(nested_name)
    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
        if member_name not in nested.namelist():
            raise FileNotFoundError(f"{member_name} not found inside {nested_name}.")
        candles = json.loads(nested.read(member_name)).get("candles", [])
    bars = {
        candle_timestamp(candle[0]).to_pydatetime(): FutureBar(
            open=float(candle[1]),
            high=float(candle[2]),
            low=float(candle[3]),
            close=float(candle[4]),
        )
        for candle in candles
    }
    if not bars:
        raise ValueError(f"No {normalized_underlying} index candles found for {trade_date}.")
    series = FutureSeries(ticker=normalized_underlying, bars=bars)
    write_index_series_cache(series, cache_path)
    return series


def default_options_zip(underlying: str, trade_date: date) -> Path:
    normalized_underlying = normalize_underlying(underlying)
    expiry_text = trade_date.strftime("%y%m%d")
    if DEFAULT_OPTIONS_ZIP.exists():
        return DEFAULT_OPTIONS_ZIP
    return FYERS_ROOT / f"{normalized_underlying}{expiry_text}.zip"


@contextmanager
def open_option_archive(
    options_zip: Path,
    underlying: str,
    trade_date: date,
):
    with zipfile.ZipFile(options_zip) as archive:
        direct_members = [
            name
            for name in archive.namelist()
            if name.startswith("1m/") and name.endswith(".json")
        ]
        if direct_members:
            yield archive
            return

        nested_name = find_option_nested_zip(archive, underlying, trade_date)
        nested_bytes = archive.read(nested_name)

    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
        yield nested


def find_option_nested_zip(
    outer: zipfile.ZipFile,
    underlying: str,
    trade_date: date,
) -> str:
    expected = f"fyers_option/{underlying}{trade_date:%y%m%d}.zip"
    if expected in outer.namelist():
        return expected
    suffix = f"/{underlying}{trade_date:%y%m%d}.zip"
    candidates = [
        name
        for name in outer.namelist()
        if name.endswith(suffix)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No nested FYERS option zip found for {underlying} {trade_date:%Y-%m-%d} "
            f"inside {outer.filename}."
        )
    return sorted(candidates)[-1]


def option_regex(underlying: str) -> re.Pattern[str]:
    return re.compile(OPTION_RE_TEMPLATE.format(underlying=underlying))


def option_cache_path(options_zip: Path, trade_date: date, underlying: str) -> Path:
    return (
        FYERS_CACHE_DIR
        / underlying
        / f"{options_zip.stem}_{trade_date:%Y%m%d}_options_1m.pkl"
    )


def index_cache_path(index_zip: Path, trade_date: date, underlying: str) -> Path:
    return (
        FYERS_CACHE_DIR
        / underlying
        / f"{index_zip.stem}_{trade_date:%Y%m%d}_index_1m.pkl"
    )


def legacy_option_cache_path(options_zip: Path, trade_date: date, underlying: str) -> Path:
    return (
        FYERS_CACHE_DIR
        / underlying
        / f"{options_zip.stem}_{trade_date:%Y%m%d}_options_1m.csv"
    )


def legacy_index_cache_path(index_zip: Path, trade_date: date, underlying: str) -> Path:
    return (
        FYERS_CACHE_DIR
        / underlying
        / f"{index_zip.stem}_{trade_date:%Y%m%d}_index_1m.csv"
    )


def read_option_frame_cache(cache_path: Path) -> pd.DataFrame:
    frame = pd.read_pickle(cache_path) if cache_path.suffix == ".pkl" else pd.read_csv(cache_path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_convert(IST)
    frame["expiry"] = pd.to_datetime(frame["expiry"]).dt.date
    frame["strike"] = frame["strike"].astype(int)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"])
    return frame.sort_values(["timestamp", "ticker"])


def write_option_frame_cache(frame: pd.DataFrame, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    output = frame[["ticker", "timestamp", "expiry", "strike", "option_type", "close"]].copy()
    output.to_pickle(cache_path)


def read_index_series_cache(cache_path: Path, underlying: str = "SENSEX") -> FutureSeries:
    frame = pd.read_pickle(cache_path) if cache_path.suffix == ".pkl" else pd.read_csv(cache_path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_convert(IST)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    bars = {
        row.timestamp.to_pydatetime(): FutureBar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in frame[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)
    }
    if not bars:
        raise ValueError(f"No {underlying} index candles found in cache {cache_path}.")
    return FutureSeries(ticker=underlying, bars=bars)


def write_index_series_cache(series: FutureSeries, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": timestamp,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
        }
        for timestamp, bar in sorted(series.bars.items())
    ]
    pd.DataFrame(rows).to_pickle(cache_path)


def find_index_nested_zip(
    outer: zipfile.ZipFile,
    trade_date: date,
    member_name: str,
) -> str:
    suffix = f"{trade_date:%Y_%m}.zip"
    candidates = [
        name
        for name in outer.namelist()
        if name.endswith(suffix) and name.startswith("fyers_index/")
    ]
    if not candidates:
        candidates = [
            name
            for name in outer.namelist()
            if name.endswith(".zip") and name.startswith("fyers_index/")
        ]
    for candidate in sorted(candidates, reverse=True):
        nested_bytes = outer.read(candidate)
        with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
            if member_name in nested.namelist():
                return candidate
    raise FileNotFoundError(
        f"{member_name} not found in any FYERS index nested zip for {trade_date:%Y-%m}."
    )


def candle_timestamp(epoch_seconds: int | float) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromtimestamp(float(epoch_seconds), IST)).floor("s")


def dynamic_gamma_threshold(gamma_l: float | None, fallback: float) -> float:
    if isinstance(gamma_l, (int, float)) and math.isfinite(gamma_l):
        return max(abs(gamma_l) * fallback, MIN_HEDGE_THRESHOLD_LOTS)
    return MIN_HEDGE_THRESHOLD_LOTS


def parse_date_key(value: str) -> date:
    text = value.strip()
    for fmt in ("%d%m%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Could not parse date {value!r}.")


def close_to_close_variance(previous_close: float, close: float) -> float:
    if previous_close <= 0 or close <= 0:
        return 0.0
    return math.log(close / previous_close) ** 2


def parkinson_variance(high: float, low: float) -> float:
    if high <= 0 or low <= 0:
        return 0.0
    return math.log(high / low) ** 2 / (4 * math.log(2))


def garman_klass_variance(
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> float:
    if open_price <= 0 or high <= 0 or low <= 0 or close <= 0:
        return 0.0
    range_term = 0.5 * math.log(high / low) ** 2
    close_open_term = (2 * math.log(2) - 1) * math.log(close / open_price) ** 2
    return max(range_term - close_open_term, 0.0)


def scaled_volatility(
    variance: float,
    calendar_days: float,
    intraday_var: float,
) -> float:
    return math.sqrt(variance / intraday_var * calendar_days)


def format_final_metric(name: str, value: float | None) -> str:
    if value is None:
        return "--"
    if name.endswith("_vol"):
        return f"{value:.2%}"
    return f"{value:.0f}"


def write_pnl_plot(
    pnl_points: dict[str, list[tuple[datetime, float | None, float | None]]],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6), dpi=140)
    for tab_name, rows in pnl_points.items():
        timestamps = [timestamp for timestamp, _, _ in rows]
        live_values = [live for _, live, _ in rows]
        frozen_values = [frozen for _, _, frozen in rows]
        ax.plot(timestamps, live_values, label=f"{tab_name} live-IV total PnL", linewidth=1.4)
        ax.plot(
            timestamps,
            frozen_values,
            label=f"{tab_name} frozen-IV total PnL",
            linewidth=1.2,
            linestyle="--",
        )
    ax.axhline(0, color="#6b7280", linewidth=0.8)
    ax.set_title("Running Total PnL")
    ax.set_xlabel("Time")
    ax.set_ylabel("PnL")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    fig.autofmt_xdate()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


if __name__ == "__main__":
    main()
