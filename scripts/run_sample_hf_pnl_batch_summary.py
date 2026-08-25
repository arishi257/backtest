from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest_sample_hf.__main__ import (
    DEFAULT_DATA_DIR,
    cache_file_path,
    load_sample_hf_option_dataset,
    parse_date_key,
)
from run_sample_hf_pnl_only import run_direct_pnl_only


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run fast 1-second PnL-only summaries for multiple dates."
    )
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument(
        "--underlying",
        required=True,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dynamic-gamma-threshold-ratio", type=float, default=0.40)
    args = parser.parse_args()

    print("date,portfolio_total_pnl,frozen_iv_total_pnl,total_seconds,cache_used")
    for raw_date in args.dates:
        trade_date = parse_date_key(raw_date)
        cache_path = cache_file_path(args.data_dir, trade_date, args.underlying)
        cache_used = cache_path.exists()
        start = time.perf_counter()
        try:
            dataset = load_sample_hf_option_dataset(
                args.data_dir,
                trade_date,
                args.underlying,
            )
            _rows, metrics, _cycles, _analytics = run_direct_pnl_only(
                dataset,
                args.workbook,
                args.dynamic_gamma_threshold_ratio,
            )
            elapsed = time.perf_counter() - start
            print(
                ",".join(
                    [
                        trade_date.strftime("%d-%b-%Y"),
                        format_number(metrics.get("portfolio_total_pnl")),
                        format_number(metrics.get("frozen_iv_total_pnl")),
                        f"{elapsed:.2f}",
                        "yes" if cache_used else "no",
                    ]
                ),
                flush=True,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start
            print(
                f"{trade_date:%d-%b-%Y},ERROR,{type(exc).__name__}: {exc},{elapsed:.2f},{'yes' if cache_used else 'no'}",
                flush=True,
            )


def format_number(value) -> str:
    return "--" if value is None else f"{float(value):.2f}"


if __name__ == "__main__":
    main()
