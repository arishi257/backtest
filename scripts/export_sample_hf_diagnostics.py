from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest.headless_portfolio import GammaDiffTracker, ParkGammaTracker, is_top_move_time
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest_1m.__main__ import (
    DynamicGammaThresholdFrozenIvState,
    DynamicGammaThresholdPortfolioState,
    dynamic_gamma_threshold,
)
from backtest_sample_hf.__main__ import (
    DEFAULT_DATA_DIR,
    load_sample_hf_option_dataset,
    parse_date_key,
)
from fit_sensex.ui.app import total_row_numeric_values


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export 1-second sample-data portfolio diagnostics to CSV."
    )
    parser.add_argument("--date", default="26052026")
    parser.add_argument(
        "--underlying",
        default="NIFTY",
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dynamic-gamma-threshold-ratio", type=float, default=0.40)
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Output folder for this run. Defaults to runs_1s/<underlying>_<date>_<timestamp>.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--portfolio-output",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=None,
    )
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    run_dir = resolve_run_dir(args.run_dir, args.underlying, trade_date)
    diagnostics_output = args.output or run_dir / "diagnostics.csv"
    portfolio_output = args.portfolio_output or run_dir / "portfolio_0920.csv"
    summary_output = args.summary_output or run_dir / "summary_metrics.csv"
    dataset = load_sample_hf_option_dataset(args.data_dir, trade_date, args.underlying)

    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, args.workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    states = {
        session.spec.tab_name: DynamicGammaThresholdPortfolioState(session=session)
        for session in sessions
    }
    frozen_states = {
        session.spec.tab_name: DynamicGammaThresholdFrozenIvState(session)
        for session in sessions
    }
    rows: list[dict[str, object]] = []
    portfolio_rows: list[dict[str, object]] = []
    universal_mid_points = {session.spec.tab_name: [] for session in sessions}
    gamma_l_points = {session.spec.tab_name: [] for session in sessions}
    park_gamma_trackers = {session.spec.tab_name: ParkGammaTracker() for session in sessions}
    previous_spot_close: float | None = None
    previous_universal_mid: float | None = None
    previous_hedge_universal_mid: float | None = None
    portfolio_snapshot_captured = False
    c2c_spot_variance = 0.0
    c2c_synth_variance = 0.0
    park_variance = 0.0
    gk_variance = 0.0
    hedge_variance = 0.0
    calendar_days: float | None = None
    intraday_var: float | None = None
    final_summary_metrics: dict[str, float | None] = {}

    while replay.advance():
        timestamp = replay.now()
        for session in sessions:
            result = session.analytics.calculate(session.store.snapshot())
            if result is None:
                continue
            state = states[session.spec.tab_name]
            frozen_state = frozen_states[session.spec.tab_name]
            prior_trade_count = len(state.hedge_trades)
            prior_frozen_trade_count = len(frozen_state.hedge_trades)
            calendar_days = session.config.market.calendar_days
            intraday_var = result.intraday_var
            metrics = state.update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.dynamic_gamma_threshold_ratio,
            )
            frozen_metrics = frozen_state.update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                args.dynamic_gamma_threshold_ratio,
            )
            if state.portfolio is None or metrics.total_pnl is None:
                continue
            if not portfolio_snapshot_captured:
                portfolio_rows = portfolio_snapshot_rows(
                    timestamp,
                    result.universal_mid,
                    state.portfolio.positions,
                )
                portfolio_snapshot_captured = True

            risk_rows = state.portfolio.calculate(result)
            totals = total_row_numeric_values(risk_rows, result)
            options_delta_lots = numeric_or_none(totals[19] if len(totals) > 19 else None)
            gamma_lots = numeric_or_none(totals[21] if len(totals) > 21 else None)
            hedge_delta_lots = state.cumulative_hedge_lots
            net_delta_lots = (
                options_delta_lots + hedge_delta_lots
                if options_delta_lots is not None
                else None
            )
            traded_delta_lots = sum(
                float(trade["lots_change"])
                for trade in state.hedge_trades[prior_trade_count:]
            )
            traded_universal_mid = result.universal_mid if abs(traded_delta_lots) > 1e-9 else None
            spot_bar = dataset.future_series.bar_at(timestamp) if dataset.future_series else None
            if is_top_move_time(timestamp):
                if previous_spot_close is not None and spot_bar is not None:
                    c2c_spot_variance += close_to_close_variance(
                        previous_spot_close,
                        spot_bar.close,
                    )
                if previous_universal_mid is not None:
                    c2c_synth_variance += close_to_close_variance(
                        previous_universal_mid,
                        result.universal_mid,
                    )
                if spot_bar is not None:
                    previous_spot_close = spot_bar.close
                    park_variance += parkinson_variance(spot_bar.high, spot_bar.low)
                    gk_variance += garman_klass_variance(
                        spot_bar.open,
                        spot_bar.high,
                        spot_bar.low,
                        spot_bar.close,
                    )
                previous_universal_mid = result.universal_mid
                if traded_universal_mid is not None:
                    if previous_hedge_universal_mid is not None:
                        hedge_variance += close_to_close_variance(
                            previous_hedge_universal_mid,
                            traded_universal_mid,
                        )
                    previous_hedge_universal_mid = traded_universal_mid
            threshold_lots = dynamic_gamma_threshold(
                gamma_lots,
                args.dynamic_gamma_threshold_ratio,
            )
            if isinstance(result.universal_mid, (int, float)) and math.isfinite(result.universal_mid):
                universal_mid_points[session.spec.tab_name].append(
                    (timestamp, result.universal_mid)
                )
            if isinstance(gamma_lots, (int, float)) and math.isfinite(gamma_lots):
                gamma_l_points[session.spec.tab_name].append((timestamp, gamma_lots))
            park_gamma_metrics = park_gamma_trackers[session.spec.tab_name].update(
                spot_bar,
                gamma_lots,
            )
            frozen_delta_lots = None
            frozen_gamma_lots = None
            frozen_threshold_lots = None
            frozen_hedge_delta_lots = frozen_state.cumulative_hedge_lots
            frozen_net_delta_lots = None
            frozen_traded_delta_lots = sum(
                float(trade["lots_change"])
                for trade in frozen_state.hedge_trades[prior_frozen_trade_count:]
            )
            frozen_traded_universal_mid = (
                result.universal_mid if abs(frozen_traded_delta_lots) > 1e-9 else None
            )
            if frozen_state.portfolio is not None and frozen_metrics.total_pnl is not None:
                frozen_risk_rows = frozen_state._frozen_risk_rows(
                    result,
                    session.config.market.funding_rate,
                )
                frozen_totals = total_row_numeric_values(frozen_risk_rows, result)
                frozen_delta_lots = numeric_or_none(
                    frozen_totals[19] if len(frozen_totals) > 19 else None
                )
                frozen_gamma_lots = numeric_or_none(
                    frozen_totals[21] if len(frozen_totals) > 21 else None
                )
                frozen_threshold_lots = dynamic_gamma_threshold(
                    frozen_gamma_lots,
                    args.dynamic_gamma_threshold_ratio,
                )
                frozen_net_delta_lots = (
                    frozen_delta_lots + frozen_hedge_delta_lots
                    if frozen_delta_lots is not None
                    else None
                )

            rows.append(
                {
                    "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "universal_mid": round_or_none(result.universal_mid),
                    "running_total_pnl": round_or_none(metrics.total_pnl),
                    "gamma_lots": round_or_none(gamma_lots),
                    "threshold_lots": round_or_none(threshold_lots),
                    "net_delta_lots_options_plus_hedge": round_or_none(net_delta_lots),
                    "hedge_delta_lots": round_or_none(hedge_delta_lots),
                    "traded_delta_lots": round_or_none(traded_delta_lots),
                    "traded_universal_mid": round_or_none(traded_universal_mid),
                    "frozen_iv_running_total_pnl": round_or_none(frozen_metrics.total_pnl),
                    "frozen_iv_gamma_lots": round_or_none(frozen_gamma_lots),
                    "frozen_iv_threshold_lots": round_or_none(frozen_threshold_lots),
                    "frozen_iv_net_delta_lots_options_plus_hedge": round_or_none(
                        frozen_net_delta_lots
                    ),
                    "frozen_iv_hedge_delta_lots": round_or_none(frozen_hedge_delta_lots),
                    "frozen_iv_traded_delta_lots": round_or_none(frozen_traded_delta_lots),
                    "frozen_iv_traded_universal_mid": round_or_none(
                        frozen_traded_universal_mid
                    ),
                }
            )
            final_summary_metrics = {
                "portfolio_total_pnl": metrics.total_pnl,
                "portfolio_gamma_l": gamma_lots,
                "portfolio_gamma_diff_total": None,
                "park_gamma_pnl_diff_total": park_gamma_metrics.park_gamma_pnl_diff_total,
                "gk_gamma_pnl_diff_total": park_gamma_metrics.gk_gamma_pnl_diff_total,
                "frozen_iv_total_pnl": frozen_metrics.total_pnl,
            }

    if not rows:
        raise SystemExit("No diagnostics rows were produced.")

    for tab_name in universal_mid_points:
        if universal_mid_points[tab_name] and gamma_l_points[tab_name]:
            tracker = GammaDiffTracker()
            tracker.universal_mid_points = universal_mid_points[tab_name]
            tracker.gamma_l_points = gamma_l_points[tab_name]
            final_summary_metrics["portfolio_gamma_diff_total"] = tracker.total()

    diagnostics_output.parent.mkdir(parents=True, exist_ok=True)
    with diagnostics_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with portfolio_output.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "timestamp",
            "universal_mid",
            "underlying",
            "maturity",
            "strike",
            "option_type",
            "lots",
            "qty",
            "mult",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(portfolio_rows)
    with summary_output.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["metric", "value"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            [
                {"metric": "portfolio_total_pnl", "value": round_or_none(final_summary_metrics.get("portfolio_total_pnl"))},
                {"metric": "portfolio_gamma_l", "value": round_or_none(final_summary_metrics.get("portfolio_gamma_l"))},
                {"metric": "portfolio_gamma_diff_total", "value": round_or_none(final_summary_metrics.get("portfolio_gamma_diff_total"))},
                {"metric": "park_gamma_pnl_diff_total", "value": round_or_none(final_summary_metrics.get("park_gamma_pnl_diff_total"))},
                {"metric": "gk_gamma_pnl_diff_total", "value": round_or_none(final_summary_metrics.get("gk_gamma_pnl_diff_total"))},
                {"metric": "frozen_iv_total_pnl", "value": round_or_none(final_summary_metrics.get("frozen_iv_total_pnl"))},
                {"metric": "c2c_spot_vol", "value": round_or_none(scaled_volatility_or_none(c2c_spot_variance, calendar_days, intraday_var))},
                {"metric": "park_vol", "value": round_or_none(scaled_volatility_or_none(park_variance, calendar_days, intraday_var))},
                {"metric": "gk_vol", "value": round_or_none(scaled_volatility_or_none(gk_variance, calendar_days, intraday_var))},
                {"metric": "c2c_synth_vol", "value": round_or_none(scaled_volatility_or_none(c2c_synth_variance, calendar_days, intraday_var))},
                {"metric": "hedge_vol", "value": round_or_none(scaled_volatility_or_none(hedge_variance, calendar_days, intraday_var))},
            ]
        )
    print(diagnostics_output)
    print(f"Run dir: {run_dir}")


def numeric_or_none(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def round_or_none(value) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


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


def scaled_volatility_or_none(
    variance: float,
    calendar_days: float | None,
    intraday_var: float | None,
) -> float | None:
    if calendar_days is None or intraday_var is None or intraday_var <= 0:
        return None
    return math.sqrt(variance / intraday_var * calendar_days) * 100


def resolve_run_dir(run_dir: Path | None, underlying: str, trade_date) -> Path:
    if run_dir is not None:
        return run_dir
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs_1s" / f"{underlying.lower()}_{trade_date:%Y%m%d}_{timestamp}"


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


if __name__ == "__main__":
    main()
