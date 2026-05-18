from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk

from backtest.config import DEFAULT_WORKBOOK, resolve_sample_csv
from backtest.data import load_option_dataset
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from vol_dashboard.market.spot import SpotStore
from vol_dashboard.ui.app import VolDashboardApp


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay NIFTY CSV data into Vol Dashboard.")
    parser.add_argument("--date", help="Sample file date, e.g. 09042026 or 09-04-2026")
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    args = parser.parse_args()

    csv_path = resolve_sample_csv(args.date, args.csv)
    dataset = load_option_dataset(csv_path)
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, args.workbook, current_time)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    print(
        f"Loaded {len(dataset.frame):,} NIFTY option rows, "
        f"{len(sessions)} expiries, {len(dataset.timestamps)} replay minutes "
        f"from {csv_path}."
    )
    for session in sessions:
        print(f"  {session.spec.tab_name}: {len(session.chain)} strikes")

    root = tk.Tk()
    root.title("Backtest Vol Dashboard")
    app = VolDashboardApp(
        root,
        sessions,
        spot_store=SpotStore(),
        before_refresh=replay.advance,
        clock=replay.now,
    )
    app.start()


if __name__ == "__main__":
    main()
