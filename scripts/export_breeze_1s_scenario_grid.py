from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest_sample_hf.__main__ import parse_date_key
from export_1s_scenario_grid import write_workbook
from export_1s_batch_restrike_workbooks import run_path
from light_run import load_breeze_parquet_option_dataset


DEFAULT_OUTPUT_DIR = Path(r"C:\Users\rishi\OneDrive\Desktop\Summary")
START_TIMES = ("09:20", "10:00")
THRESHOLDS = (0.1, 0.4, 0.9)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--underlying", required=True, type=normalize_underlying, choices=("NIFTY", "SENSEX"))
    parser.add_argument("--breeze-parquet", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--end-time", default="15:15")
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{args.underlying.lower()}_{trade_date:%d%b%y}_breeze_1s_light_scenario_grid.xlsx"

    total_start = time.perf_counter()
    load_start = time.perf_counter()
    dataset = load_breeze_parquet_option_dataset(
        args.breeze_parquet,
        trade_date,
        args.underlying,
        min(START_TIMES),
    )
    load_seconds = time.perf_counter() - load_start

    results = []
    for start_time in START_TIMES:
        for threshold in THRESHOLDS:
            for restrike in (False, True):
                scenario_start = time.perf_counter()
                result = run_path(
                    dataset,
                    DEFAULT_WORKBOOK,
                    threshold,
                    start_time,
                    args.end_time,
                    with_restrike=restrike,
                )
                results.append(
                    {
                        "start_time": start_time,
                        "threshold": threshold,
                        "restrike": "Yes" if restrike else "No",
                        "final_pnl": result["summary"]["final_pnl"],
                        "seconds": time.perf_counter() - scenario_start,
                    }
                )

    total_seconds = time.perf_counter() - total_start
    write_workbook(output, args.underlying, trade_date, args.end_time, load_seconds, total_seconds, results)

    print(f"OUTPUT,{output}")
    print(f"TOTAL_SECONDS,{total_seconds:.2f}")
    print("START,THRESHOLD,NO_RESTRIKE,RESTRIKE")
    for start_time in START_TIMES:
        for threshold in THRESHOLDS:
            no_restrike = next(
                row["final_pnl"]
                for row in results
                if row["start_time"] == start_time and row["threshold"] == threshold and row["restrike"] == "No"
            )
            restrike = next(
                row["final_pnl"]
                for row in results
                if row["start_time"] == start_time and row["threshold"] == threshold and row["restrike"] == "Yes"
            )
            print(f"{start_time},{threshold:.1f},{no_restrike:.2f},{restrike:.2f}")


if __name__ == "__main__":
    main()
