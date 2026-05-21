from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk

from backtest.config import (
    DEFAULT_PROCESSED_OUTPUT_DIR,
    DEFAULT_UNDERLYING,
    DEFAULT_WORKBOOK,
    resolve_sample_csv,
    normalize_underlying,
)
from backtest.data import load_option_dataset
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest.spot_data import load_spot_series
from vol_dashboard.market.spot import SpotStore
from vol_dashboard.ui.app import VolDashboardApp


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--refresh-ms must be greater than zero.")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay option CSV data into Vol Dashboard.")
    parser.add_argument("--date", help="Sample file date, e.g. 09042026 or 09-04-2026")
    parser.add_argument(
        "--underlying",
        default=DEFAULT_UNDERLYING,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument(
        "--processed-output-dir",
        type=Path,
        default=DEFAULT_PROCESSED_OUTPUT_DIR,
    )
    parser.add_argument(
        "--refresh-ms",
        type=positive_int,
        default=None,
        help="Milliseconds between replay slices, e.g. 100 for faster playback.",
    )
    args = parser.parse_args()

    csv_path = resolve_sample_csv(args.date, args.csv, args.underlying)
    dataset = load_option_dataset(csv_path, args.underlying)
    spot_points = []
    spot_source = None
    try:
        spot_series = load_spot_series(dataset.trade_date, dataset.underlying)
        spot_points = spot_series.points
        spot_source = str(spot_series.source_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Spot data unavailable: {exc}")

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
        f"Loaded {len(dataset.frame):,} {dataset.underlying} option rows, "
        f"{len(sessions)} expiries, {len(dataset.timestamps)} replay minutes "
        f"from {csv_path}."
    )
    for session in sessions:
        print(f"  {session.spec.tab_name}: {len(session.chain)} strikes")
    print(
        f"Replay refresh: "
        f"{min(session.config.market.refresh_ms for session in sessions)} ms"
    )

    root = tk.Tk()
    root.title("Backtest Vol Dashboard")
    app = VolDashboardApp(
        root,
        sessions,
        spot_store=SpotStore(),
        spot_points=spot_points,
        spot_source=spot_source,
        processed_output_dir=args.processed_output_dir,
        before_refresh=replay.advance,
        clock=replay.now,
    )
    app.start()


if __name__ == "__main__":
    main()
