from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from backtest.config import (
    DEFAULT_PROCESSED_OUTPUT_DIR,
    DEFAULT_UNDERLYING,
    DEFAULT_WORKBOOK,
    normalize_file_date,
    normalize_underlying,
    resolve_sample_csv,
)
from backtest.data import load_option_dataset
from backtest.data import parse_expiry_text, ticker_pattern
from backtest.headless_portfolio import (
    GammaDiffTracker,
    HeadlessFrozenIvState,
    HeadlessPortfolioState,
)
from backtest.processed_data import ProcessedDataWriter
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest.spot_data import load_spot_series


@dataclass
class BatchResult:
    date_key: str
    underlying: str
    status: str
    detail: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run headless backtests for many dates and save processed_data CSVs."
    )
    parser.add_argument("--year", help="Year to process, e.g. 2026.")
    parser.add_argument("--start-date", help="Start date, e.g. 01012026.")
    parser.add_argument("--end-date", help="End date, e.g. 31012026.")
    parser.add_argument("--dates", help="Comma-separated dates, e.g. 01012026,06012026.")
    parser.add_argument(
        "--underlying",
        default=DEFAULT_UNDERLYING,
        help="Underlying or comma-separated underlyings, e.g. NIFTY,SENSEX.",
    )
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument(
        "--processed-output-dir",
        type=Path,
        default=DEFAULT_PROCESSED_OUTPUT_DIR,
    )
    parser.add_argument(
        "--hedge-threshold",
        type=float,
        default=1.3,
        help="BS delta lots threshold for re-hedging.",
    )
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="Run every requested date instead of only 0DTE expiry dates.",
    )
    args = parser.parse_args()

    date_keys = requested_dates(args)
    underlyings = parse_underlyings(args.underlying)
    results = []
    for date_key in date_keys:
        for underlying in underlyings:
            print(f"Running {underlying} {date_key}...")
            result = run_one(
                date_key,
                underlying,
                args.workbook,
                args.processed_output_dir,
                args.hedge_threshold,
                expiry_only=not args.all_dates,
            )
            results.append(result)
            print(f"  {result.status}: {result.detail}")

    ok = sum(1 for result in results if result.status == "OK")
    skipped = len(results) - ok
    print(f"Batch complete. OK: {ok}. Skipped/failed: {skipped}.")


def run_one(
    date_key: str,
    underlying: str,
    workbook: Path,
    processed_output_dir: Path,
    hedge_threshold: float,
    expiry_only: bool = True,
) -> BatchResult:
    try:
        csv_path = resolve_sample_csv(date_key, underlying=underlying)
        if expiry_only:
            expiry_status = raw_csv_0dte_status(csv_path, date_key, underlying)
            if expiry_status is not None:
                return BatchResult(date_key, underlying, "SKIP", expiry_status)
        dataset = load_option_dataset(csv_path, underlying)
    except Exception as exc:
        return BatchResult(date_key, underlying, "SKIP", str(exc))

    spot_points = []
    try:
        spot_points = load_spot_series(dataset.trade_date, dataset.underlying).points
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"  Spot data unavailable: {exc}")

    current_time = lambda: replay.now()
    try:
        sessions = build_backtest_sessions(dataset, workbook, current_time)
    except Exception as exc:
        return BatchResult(date_key, underlying, "SKIP", str(exc))

    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)
    processed_writer = ProcessedDataWriter(processed_output_dir, sessions)
    universal_mid_points = {session.spec.tab_name: [] for session in sessions}
    portfolio_states = {
        session.spec.tab_name: HeadlessPortfolioState(session=session)
        for session in sessions
    }
    frozen_states = {
        session.spec.tab_name: HeadlessFrozenIvState(session)
        for session in sessions
    }
    gamma_trackers = {session.spec.tab_name: GammaDiffTracker() for session in sessions}

    cycles = 0
    analytics = 0
    try:
        while replay.advance():
            cycles += 1
            for session in sessions:
                result = session.analytics.calculate(session.store.snapshot())
                if result is None:
                    continue
                analytics += 1
                tab_name = session.spec.tab_name
                timestamp = replay.now()
                universal_mid_points[tab_name].append((timestamp, result.universal_mid))
                portfolio_metrics = portfolio_states[tab_name].update(
                    result,
                    timestamp,
                    session.config.market.funding_rate,
                    session.config.market.brokerage_rate,
                    hedge_threshold,
                )
                frozen_metrics = frozen_states[tab_name].update(
                    result,
                    timestamp,
                    session.config.market.funding_rate,
                    session.config.market.brokerage_rate,
                    hedge_threshold,
                )
                gamma_diff_total = gamma_trackers[tab_name].update(
                    timestamp,
                    result.universal_mid,
                    portfolio_metrics.gamma_l,
                )
                processed_writer.write(
                    timestamp,
                    session,
                    result,
                    spot_points,
                    universal_mid_points[tab_name],
                    portfolio_metrics.total_pnl,
                    gamma_diff_total,
                    frozen_metrics.total_pnl,
                )
    except Exception as exc:
        return BatchResult(date_key, underlying, "FAIL", str(exc))

    return BatchResult(
        date_key,
        underlying,
        "OK",
        f"{cycles} cycles, {analytics} analytics rows",
    )


def requested_dates(args: argparse.Namespace) -> list[str]:
    if args.dates:
        return [normalize_file_date(value) for value in args.dates.split(",") if value.strip()]
    if args.year:
        start = date(int(args.year), 1, 1)
        end = date(int(args.year), 12, 31)
        return weekday_date_keys(start, end)
    if args.start_date and args.end_date:
        start = datetime.strptime(normalize_file_date(args.start_date), "%d%m%Y").date()
        end = datetime.strptime(normalize_file_date(args.end_date), "%d%m%Y").date()
        return weekday_date_keys(start, end)
    raise SystemExit("Use --year, --dates, or --start-date with --end-date.")


def weekday_date_keys(start: date, end: date) -> list[str]:
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date.")
    current = start
    values = []
    while current <= end:
        if current.weekday() < 5:
            values.append(current.strftime("%d%m%Y"))
        current += timedelta(days=1)
    return values


def parse_underlyings(value: str) -> list[str]:
    return [normalize_underlying(part) for part in value.split(",") if part.strip()]


def raw_csv_0dte_status(csv_path: Path, date_key: str, underlying: str) -> str | None:
    trade_date = datetime.strptime(date_key, "%d%m%Y").date()
    raw = pd.read_csv(csv_path, usecols=["Ticker"])
    parsed = raw["Ticker"].astype(str).str.extract(ticker_pattern(underlying))
    expiry_text = parsed.loc[parsed["underlying"].eq(underlying), "expiry_text"].dropna()
    if expiry_text.empty:
        return "no matching option tickers"
    expiries = sorted(parse_expiry_text(expiry_text, underlying).dt.date.unique())
    if not expiries:
        return "no expiries found"
    nearest_expiry = expiries[0]
    if nearest_expiry != trade_date:
        return f"not 0DTE; nearest expiry is {nearest_expiry.isoformat()}"
    return None


if __name__ == "__main__":
    main()
