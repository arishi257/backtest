from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backtest.config import normalize_underlying
from backtest_sample_hf.__main__ import DEFAULT_DATA_DIR, parse_date_key


NODE_EXE = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)
NODE_MODULES = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run 1-second sample diagnostics and leave one workbook file."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--underlying",
        required=True,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dynamic-gamma-threshold-ratio", type=float, default=0.40)
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    run_dir = args.run_dir or default_run_dir(args.underlying, trade_date)
    run_dir.mkdir(parents=True, exist_ok=True)

    export_command = [
        sys.executable,
        str(ROOT / "scripts" / "export_sample_hf_diagnostics.py"),
        "--underlying",
        args.underlying,
        "--date",
        trade_date.strftime("%d%m%Y"),
        "--data-dir",
        str(args.data_dir),
        "--dynamic-gamma-threshold-ratio",
        str(args.dynamic_gamma_threshold_ratio),
        "--run-dir",
        str(run_dir),
    ]
    subprocess.run(export_command, cwd=ROOT, check=True)

    node_modules_link = ROOT / "node_modules"
    created_link = ensure_node_modules_link(node_modules_link)
    try:
        env = os.environ.copy()
        env["RUN_DIR"] = str(run_dir)
        env["UNDERLYING"] = args.underlying
        env["TRADE_DATE"] = trade_date.isoformat()
        env["CLEAN_INPUTS"] = "1"
        subprocess.run(
            [
                str(NODE_EXE),
                str(ROOT / "scripts" / "build_sample_hf_diagnostics_workbook.mjs"),
            ],
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
    print(f"Workbook: {workbooks[-1]}")


def default_run_dir(underlying: str, trade_date) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs_1s" / f"{underlying.lower()}_{trade_date:%Y%m%d}_{timestamp}"


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


if __name__ == "__main__":
    main()
