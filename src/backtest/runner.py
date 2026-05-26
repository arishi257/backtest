from __future__ import annotations

import argparse
from pathlib import Path

from backtest.config import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROCESSED_OUTPUT_DIR,
    DEFAULT_UNDERLYING,
    DEFAULT_WORKBOOK,
    normalize_underlying,
    resolve_sample_csv,
)
from backtest.data import load_option_dataset
from backtest.headless_portfolio import (
    GammaDiffTracker,
    HeadlessFrozenIvState,
    HeadlessPortfolioState,
)
from backtest.processed_data import ProcessedDataWriter
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest.spot_data import load_spot_series
from vol_dashboard.services.snapshots import SnapshotWriter


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--refresh-ms must be greater than zero.")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the option replay headlessly.")
    parser.add_argument("--date", help="Sample file date, e.g. 09042026 or 09-04-2026")
    parser.add_argument(
        "--underlying",
        default=DEFAULT_UNDERLYING,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
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
    parser.add_argument(
        "--hedge-threshold",
        type=float,
        default=1.3,
        help="BS delta lots threshold for re-hedging.",
    )
    args = parser.parse_args()

    csv_path = resolve_sample_csv(args.date, args.csv, args.underlying)
    dataset = load_option_dataset(csv_path, args.underlying)
    spot_points = []
    try:
        spot_points = load_spot_series(dataset.trade_date, dataset.underlying).points
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
    writer = SnapshotWriter(args.output_dir)
    processed_writer = ProcessedDataWriter(args.processed_output_dir, sessions)
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
    while replay.advance():
        cycles += 1
        for session in sessions:
            result = session.analytics.calculate(session.store.snapshot())
            if result is None:
                continue
            analytics += 1
            writer.maybe_write(replay.now(), session, result)
            tab_name = session.spec.tab_name
            universal_mid_points[tab_name].append((replay.now(), result.universal_mid))
            portfolio_metrics = portfolio_states[tab_name].update(
                result,
                replay.now(),
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.hedge_threshold,
            )
            frozen_metrics = frozen_states[tab_name].update(
                result,
                replay.now(),
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.hedge_threshold,
            )
            gamma_diff_total = gamma_trackers[tab_name].update(
                replay.now(),
                result.universal_mid,
                portfolio_metrics.gamma_l,
            )
            processed_writer.write(
                replay.now(),
                session,
                result,
                spot_points,
                universal_mid_points[tab_name],
                portfolio_metrics.total_pnl,
                gamma_diff_total,
                frozen_metrics.total_pnl,
            )

    print(
        f"Completed {cycles} replay cycles across {len(sessions)} expiries. "
        f"Wrote {analytics} analytics snapshots to {args.output_dir}. "
        f"Processed data: {args.processed_output_dir}. "
        f"Replay refresh: {min(session.config.market.refresh_ms for session in sessions)} ms. "
        f"Source CSV: {csv_path}"
    )


if __name__ == "__main__":
    main()
