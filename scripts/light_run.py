from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest.data import OptionDataset
from backtest.headless_portfolio import GammaDiffTracker, ParkGammaTracker, is_top_move_time
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest_1m.__main__ import dynamic_gamma_threshold
from backtest_sample_hf.__main__ import (
    DEFAULT_DATA_DIR,
    cache_file_path,
    load_sample_hf_option_dataset,
    parse_date_key,
)
from export_sample_hf_diagnostics import (
    close_to_close_variance,
    garman_klass_variance,
    parkinson_variance,
    round_or_none,
    scaled_volatility_or_none,
)
from run_sample_hf_pnl_only import (
    DirectHedgeState,
    DirectPriceBook,
    build_light_sample_portfolio,
    capture_light_frozen_ivs,
)


NODE_EXE = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)
NODE_MODULES = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
)
PNL_START_TIME = "09:20"
IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run light 1-second diagnostics without fitting the full vol surface."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--underlying",
        required=True,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--breeze-parquet",
        type=Path,
        default=None,
        help="Optional Breeze parquet file to use instead of the sample-HF folder parser.",
    )
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dynamic-gamma-threshold-ratio", type=float, default=0.40)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument(
        "--start-time",
        default=PNL_START_TIME,
        help="Time from which to create the portfolio and begin PnL rows, e.g. 10:00.",
    )
    parser.add_argument(
        "--final-output-dir",
        type=Path,
        default=None,
        help="Optional flat output folder for the final diagnostics workbook.",
    )
    parser.add_argument(
        "--stop-on-positive-gamma",
        action="store_true",
        help="Stop the run and treat the portfolio as liquidated when live option gamma in lots turns positive.",
    )
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    run_dir = args.run_dir or default_run_dir(args.underlying, trade_date)
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_path = None if args.breeze_parquet else cache_file_path(args.data_dir, trade_date, args.underlying)
    cache_used = cache_path.exists() if cache_path is not None else False

    total_start = time.perf_counter()
    data_start = time.perf_counter()
    if args.breeze_parquet:
        dataset = load_breeze_parquet_option_dataset(
            args.breeze_parquet,
            trade_date,
            args.underlying,
            args.start_time,
        )
    else:
        dataset = load_sample_hf_option_dataset(args.data_dir, trade_date, args.underlying)
    data_seconds = time.perf_counter() - data_start

    sim_start = time.perf_counter()
    diagnostics, portfolio_rows, summary, cycles = run_light_diagnostics(
        dataset,
        args.workbook,
        args.dynamic_gamma_threshold_ratio,
        args.start_time,
        args.stop_on_positive_gamma,
    )
    simulation_seconds = time.perf_counter() - sim_start

    diagnostics_csv = run_dir / "diagnostics.csv"
    portfolio_csv = run_dir / "portfolio_0920.csv"
    summary_csv = run_dir / "summary_metrics.csv"
    write_csv(diagnostics_csv, diagnostics)
    write_csv(portfolio_csv, portfolio_rows)
    write_summary_csv(summary_csv, summary)

    workbook_start = time.perf_counter()
    workbook_path = build_diagnostics_workbook(run_dir, args.underlying, trade_date, args.start_time)
    if args.final_output_dir is not None:
        args.final_output_dir.mkdir(parents=True, exist_ok=True)
        final_path = unique_flat_output_path(args.final_output_dir, args.underlying, trade_date)
        shutil.move(str(workbook_path), final_path)
        workbook_path = final_path
    workbook_seconds = time.perf_counter() - workbook_start
    total_seconds = time.perf_counter() - total_start

    print(f"Completed {cycles} light 1-second cycles.")
    if cache_path is not None:
        print(f"Raw parsed-data cache: {cache_path}")
        print(f"Cache used at start: {'yes' if cache_used else 'no; created during this run'}")
    else:
        print(f"Breeze parquet source: {args.breeze_parquet}")
    print(f"Run dir: {run_dir}")
    print(f"Workbook: {workbook_path}")
    print("Final metrics:")
    for key in (
        "portfolio_total_pnl",
        "portfolio_gamma_l",
        "portfolio_gamma_diff_total",
        "park_gamma_pnl_diff_total",
        "gk_gamma_pnl_diff_total",
        "frozen_iv_total_pnl",
        "c2c_spot_vol",
        "park_vol",
        "gk_vol",
        "c2c_synth_vol",
        "hedge_vol",
        "liquidation_timestamp",
        "liquidation_gamma_l",
    ):
        print(f"  {key}: {format_number(summary.get(key))}")
    print("Timing:")
    print(f"  data_load_seconds: {data_seconds:.2f}")
    print(f"  simulation_seconds: {simulation_seconds:.2f}")
    print(f"  workbook_seconds: {workbook_seconds:.2f}")
    print(f"  total_seconds: {total_seconds:.2f}")


def load_breeze_parquet_option_dataset(
    path: Path,
    trade_date: date,
    underlying: str,
    start_time: str,
) -> OptionDataset:
    columns = [
        "close",
        "datetime",
        "expiry_date",
        "product_type",
        "right",
        "strike_price",
        "trade_date",
        "underlying",
    ]
    raw = pd.read_parquet(path, columns=columns)
    raw = raw[
        raw["underlying"].astype(str).str.upper().eq(underlying)
        & raw["product_type"].astype(str).str.lower().eq("options")
    ].copy()
    if raw.empty:
        raise ValueError(f"No {underlying} option rows found in {path}.")

    raw["timestamp"] = pd.to_datetime(raw["datetime"], errors="coerce")
    if raw["timestamp"].dt.tz is None:
        raw["timestamp"] = raw["timestamp"].dt.tz_localize(IST)
    else:
        raw["timestamp"] = raw["timestamp"].dt.tz_convert(IST)
    raw["expiry"] = pd.to_datetime(raw["expiry_date"], errors="coerce").dt.date
    raw["trade_date_value"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.date
    raw["strike"] = pd.to_numeric(raw["strike_price"], errors="coerce").astype("Int64")
    raw["option_type"] = raw["right"].astype(str).str.lower().map({"call": "CE", "put": "PE"})
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw = raw.dropna(subset=["timestamp", "expiry", "trade_date_value", "strike", "option_type", "close"])
    raw = raw[raw["trade_date_value"].eq(trade_date) & raw["expiry"].eq(trade_date)]
    if raw.empty:
        raise ValueError(f"No {underlying} 0DTE option rows found for {trade_date} in {path}.")

    start = pd.Timestamp(f"{trade_date} {start_time}:00", tz=IST)
    end = pd.Timestamp(f"{trade_date} 15:40:00", tz=IST)
    raw = raw[(raw["timestamp"] >= start) & (raw["timestamp"] <= end)]
    if raw.empty:
        raise ValueError(f"No Breeze parquet timestamps found between {start} and {end}.")

    raw["strike"] = raw["strike"].astype(int)
    raw["ticker"] = raw.apply(
        lambda row: f"{underlying}{row['expiry']:%d%b%y}{int(row['strike'])}{row['option_type']}.NFO".upper(),
        axis=1,
    )
    frame = raw[["ticker", "timestamp", "expiry", "strike", "option_type", "close"]].sort_values(
        ["timestamp", "ticker"]
    )
    available = pd.DatetimeIndex(frame["timestamp"].drop_duplicates().sort_values())
    timestamps = pd.date_range(available.min(), available.max(), freq="s")
    return OptionDataset(frame.reset_index(drop=True), trade_date, timestamps, underlying, future_series=None)


def run_light_diagnostics(
    dataset,
    workbook: Path,
    threshold_ratio: float,
    start_time: str = PNL_START_TIME,
    stop_on_positive_gamma: bool = False,
):
    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)
    if len(sessions) != 1:
        raise ValueError("light_run expects one expiry session.")
    session = sessions[0]
    surface = dataset.option_surface(session.spec.expiry)
    price_book = DirectPriceBook(dataset, surface, session)

    market_state = None
    frozen_state = None
    portfolio_rows = []
    diagnostics = []
    universal_mid_points = []
    gamma_l_points = []
    park_gamma_tracker = ParkGammaTracker()
    previous_spot_close = None
    previous_universal_mid = None
    previous_hedge_universal_mid = None
    c2c_spot_variance = 0.0
    c2c_synth_variance = 0.0
    park_variance = 0.0
    gk_variance = 0.0
    hedge_variance = 0.0
    calendar_days = session.config.market.calendar_days
    intraday_var = session.config.market.intraday_var
    user_value = session.config.market.user_value
    summary = {}

    cycles = 0
    while replay.advance():
        timestamp = replay.now()
        if timestamp.strftime("%H:%M") < start_time:
            continue
        cycles += 1
        timestamp_value = dataset.timestamps[replay.position]
        row = surface.loc[timestamp_value]
        snapshot = price_book.market_snapshot(row, timestamp, user_value)
        if snapshot is None:
            continue
        user_value = snapshot.user_value
        if market_state is None or frozen_state is None:
            strikes = complete_strikes(price_book, row)
            portfolio = build_light_sample_portfolio(session, snapshot, strikes)
            if portfolio is None:
                continue
            frozen_ivs = capture_light_frozen_ivs(
                portfolio.positions,
                price_book,
                row,
                snapshot,
            )
            if len(frozen_ivs) != len(portfolio.positions):
                continue
            hedge_strike = min(strikes, key=lambda strike: (abs(strike - snapshot.universal_mid), strike))
            market_state = DirectHedgeState(portfolio=portfolio, options_pv_snapshot=None, hedge_strike=hedge_strike)
            frozen_state = DirectHedgeState(
                portfolio=portfolio,
                options_pv_snapshot=None,
                hedge_strike=hedge_strike,
                frozen_ivs=frozen_ivs,
            )
            portfolio_rows = portfolio_snapshot_rows(timestamp, snapshot.universal_mid, portfolio.positions)

        prior_trade_count = len(market_state.hedge_trades)
        prior_frozen_trade_count = len(frozen_state.hedge_trades)
        market_pnl = market_state.update(price_book, row, snapshot, timestamp, threshold_ratio)
        frozen_pnl = frozen_state.update(price_book, row, snapshot, timestamp, threshold_ratio)
        if market_pnl is None:
            continue

        gamma_lots = market_state.last_gamma_lots
        threshold_lots = dynamic_gamma_threshold(gamma_lots, threshold_ratio)
        net_delta_lots = (
            market_state.last_delta_lots + market_state.cumulative_hedge_lots
            if market_state.last_delta_lots is not None
            else None
        )
        traded_delta_lots = sum(
            float(trade["lots_change"])
            for trade in market_state.hedge_trades[prior_trade_count:]
        )
        traded_universal_mid = snapshot.universal_mid if abs(traded_delta_lots) > 1e-9 else None

        frozen_threshold_lots = dynamic_gamma_threshold(
            frozen_state.last_gamma_lots,
            threshold_ratio,
        )
        frozen_net_delta_lots = (
            frozen_state.last_delta_lots + frozen_state.cumulative_hedge_lots
            if frozen_state.last_delta_lots is not None
            else None
        )
        frozen_traded_delta_lots = sum(
            float(trade["lots_change"])
            for trade in frozen_state.hedge_trades[prior_frozen_trade_count:]
        )
        frozen_traded_universal_mid = (
            snapshot.universal_mid if abs(frozen_traded_delta_lots) > 1e-9 else None
        )

        spot_bar = dataset.future_series.bar_at(timestamp) if dataset.future_series else None
        if is_top_move_time(timestamp):
            if previous_spot_close is not None and spot_bar is not None:
                c2c_spot_variance += close_to_close_variance(previous_spot_close, spot_bar.close)
            if previous_universal_mid is not None:
                c2c_synth_variance += close_to_close_variance(previous_universal_mid, snapshot.universal_mid)
            if spot_bar is not None:
                previous_spot_close = spot_bar.close
                park_variance += parkinson_variance(spot_bar.high, spot_bar.low)
                gk_variance += garman_klass_variance(spot_bar.open, spot_bar.high, spot_bar.low, spot_bar.close)
            previous_universal_mid = snapshot.universal_mid
            if traded_universal_mid is not None:
                if previous_hedge_universal_mid is not None:
                    hedge_variance += close_to_close_variance(previous_hedge_universal_mid, traded_universal_mid)
                previous_hedge_universal_mid = traded_universal_mid

        if gamma_lots is not None:
            gamma_l_points.append((timestamp, gamma_lots))
        universal_mid_points.append((timestamp, snapshot.universal_mid))
        park_gamma_metrics = park_gamma_tracker.update(spot_bar, gamma_lots)

        diagnostics.append(
            {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "universal_mid": round_or_none(snapshot.universal_mid),
                "running_total_pnl": round_or_none(market_pnl),
                "gamma_lots": round_or_none(gamma_lots),
                "threshold_lots": round_or_none(threshold_lots),
                "net_delta_lots_options_plus_hedge": round_or_none(net_delta_lots),
                "hedge_delta_lots": round_or_none(market_state.cumulative_hedge_lots),
                "traded_delta_lots": round_or_none(traded_delta_lots),
                "traded_universal_mid": round_or_none(traded_universal_mid),
                "frozen_iv_running_total_pnl": round_or_none(frozen_pnl),
                "frozen_iv_gamma_lots": round_or_none(frozen_state.last_gamma_lots),
                "frozen_iv_threshold_lots": round_or_none(frozen_threshold_lots),
                "frozen_iv_net_delta_lots_options_plus_hedge": round_or_none(frozen_net_delta_lots),
                "frozen_iv_hedge_delta_lots": round_or_none(frozen_state.cumulative_hedge_lots),
                "frozen_iv_traded_delta_lots": round_or_none(frozen_traded_delta_lots),
                "frozen_iv_traded_universal_mid": round_or_none(frozen_traded_universal_mid),
            }
        )
        summary = {
            "portfolio_total_pnl": market_pnl,
            "portfolio_gamma_l": gamma_lots,
            "portfolio_gamma_diff_total": None,
            "park_gamma_pnl_diff_total": park_gamma_metrics.park_gamma_pnl_diff_total,
            "gk_gamma_pnl_diff_total": park_gamma_metrics.gk_gamma_pnl_diff_total,
            "frozen_iv_total_pnl": frozen_pnl,
        }
        if stop_on_positive_gamma and gamma_lots is not None and gamma_lots > 0:
            summary["liquidation_timestamp"] = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            summary["liquidation_gamma_l"] = gamma_lots
            break

    if not diagnostics:
        raise ValueError("No light diagnostics rows were produced.")

    if universal_mid_points and gamma_l_points:
        tracker = GammaDiffTracker()
        tracker.universal_mid_points = universal_mid_points
        tracker.gamma_l_points = gamma_l_points
        summary["portfolio_gamma_diff_total"] = tracker.total()
    summary["c2c_spot_vol"] = scaled_volatility_or_none(c2c_spot_variance, calendar_days, intraday_var)
    summary["park_vol"] = scaled_volatility_or_none(park_variance, calendar_days, intraday_var)
    summary["gk_vol"] = scaled_volatility_or_none(gk_variance, calendar_days, intraday_var)
    summary["c2c_synth_vol"] = scaled_volatility_or_none(c2c_synth_variance, calendar_days, intraday_var)
    summary["hedge_vol"] = scaled_volatility_or_none(hedge_variance, calendar_days, intraday_var)
    return diagnostics, portfolio_rows, summary, cycles


def complete_strikes(price_book: DirectPriceBook, row) -> list[int]:
    return [
        strike
        for strike in price_book.strikes
        if price_book.option_price(row, strike, "CE") is not None
        and price_book.option_price(row, strike, "PE") is not None
    ]


def portfolio_snapshot_rows(timestamp, universal_mid, positions) -> list[dict[str, object]]:
    return [
        {
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "universal_mid": round_or_none(universal_mid),
            "underlying": position.underlying,
            "maturity": position.maturity,
            "strike": position.strike,
            "option_type": position.option_type,
            "lots": position.lots,
            "qty": position.qty,
            "mult": position.mult,
        }
        for position in positions
    ]


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_summary_csv(path: Path, summary: dict[str, object]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
        writer.writeheader()
        for key in (
            "portfolio_total_pnl",
            "portfolio_gamma_l",
            "portfolio_gamma_diff_total",
            "park_gamma_pnl_diff_total",
            "gk_gamma_pnl_diff_total",
            "frozen_iv_total_pnl",
            "c2c_spot_vol",
            "park_vol",
            "gk_vol",
            "c2c_synth_vol",
            "hedge_vol",
            "liquidation_timestamp",
            "liquidation_gamma_l",
        ):
            writer.writerow({"metric": key, "value": round_or_none(summary.get(key))})


def build_diagnostics_workbook(run_dir: Path, underlying: str, trade_date, start_time: str = PNL_START_TIME) -> Path:
    node_modules_link = ROOT / "node_modules"
    created_link = ensure_node_modules_link(node_modules_link)
    try:
        env = os.environ.copy()
        env["RUN_DIR"] = str(run_dir)
        env["UNDERLYING"] = underlying
        env["TRADE_DATE"] = trade_date.isoformat()
        env["CLEAN_INPUTS"] = "1"
        env["START_TIME_LABEL"] = start_time
        subprocess.run(
            [str(NODE_EXE), str(ROOT / "scripts" / "build_sample_hf_diagnostics_workbook.mjs")],
            cwd=ROOT,
            env=env,
            check=True,
        )
    finally:
        if created_link:
            remove_junction(node_modules_link)
    workbooks = sorted(run_dir.glob("*.xlsx"))
    if not workbooks:
        raise SystemExit(f"No workbook was created in {run_dir}.")
    return workbooks[-1]


def unique_flat_output_path(output_dir: Path, underlying: str, trade_date) -> Path:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    date_name = f"{trade_date.day:02d}{months[trade_date.month - 1]}{trade_date.year % 100:02d}"
    base = f"{underlying.lower()}_{date_name}_light_diagnostics"
    path = output_dir / f"{base}.xlsx"
    suffix = 2
    while path.exists():
        path = output_dir / f"{base}_{suffix}.xlsx"
        suffix += 1
    return path


def ensure_node_modules_link(link_path: Path) -> bool:
    if link_path.exists():
        return False
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link_path), str(NODE_MODULES)],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    else:
        link_path.symlink_to(NODE_MODULES, target_is_directory=True)
    return True


def remove_junction(link_path: Path) -> None:
    if not link_path.exists():
        return
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/c", "rmdir", str(link_path)], cwd=ROOT, check=False)
    else:
        if link_path.is_symlink():
            link_path.unlink()
        else:
            shutil.rmtree(link_path)


def default_run_dir(underlying: str, trade_date) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs_1s" / f"{underlying.lower()}_{trade_date:%Y%m%d}_light_{timestamp}"


def format_number(value) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


if __name__ == "__main__":
    main()
