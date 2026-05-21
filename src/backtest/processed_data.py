from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

from fit_sensex.models import AnalyticsResult
from vol_dashboard.models import ExpirySession


PROCESSED_COLUMNS = [
    "timestamp",
    "trade_date",
    "underlying",
    "expiry",
    "user_value",
    "best_bid",
    "best_ask",
    "universal_mid",
    "universal_spot",
    "basis",
    "atm_vol",
    "vol_spot_10",
    "vol_universal_mid_10",
    "vol_spot_full_day",
    "vol_universal_mid_full_day",
    "time",
    "full_days",
    "fraction_days",
    "intraday",
    "intraday_var",
    "calendar_days",
    "risk_free_rate",
    "funding_rate",
    "brokerage_rate",
    "fit_error",
    "param_a",
    "param_bL",
    "param_bR",
    "param_capL",
    "param_floorR",
    "portfolio_total_pnl",
    "portfolio_gamma_diff_total",
    "frozen_iv_total_pnl",
]

PER_STRIKE_FIELDS = [
    "ce_bid",
    "ce_ask",
    "pe_bid",
    "pe_ask",
    "iv_bid",
    "iv_ask",
    "iv_mid",
    "normalized_strike",
    "slope",
    "model_iv",
]


class ProcessedDataWriter:
    def __init__(self, output_dir: Path, sessions: list[ExpirySession]) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.columns = processed_columns(sessions)
        self.initialized_paths: set[Path] = set()
        self.last_written_minute: set[tuple[Path, str]] = set()

    def write(
        self,
        timestamp: datetime,
        session: ExpirySession,
        result: AnalyticsResult | None,
        spot_points: list[tuple[datetime, float]],
        universal_mid_points: list[tuple[datetime, float]],
        portfolio_total_pnl: float | None = None,
        portfolio_gamma_diff_total: float | None = None,
        frozen_iv_total_pnl: float | None = None,
    ) -> None:
        if result is None:
            return

        path = self.path_for(timestamp)
        minute_key = timestamp.strftime("%Y-%m-%d %H:%M")
        write_key = (path, minute_key)
        if write_key in self.last_written_minute:
            return

        if path not in self.initialized_paths:
            if path.exists():
                path.unlink()
            self.initialized_paths.add(path)

        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=self.columns, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(
                processed_row(
                    timestamp,
                    session,
                    result,
                    spot_points,
                    universal_mid_points,
                    portfolio_total_pnl,
                    portfolio_gamma_diff_total,
                    frozen_iv_total_pnl,
                )
            )
        self.last_written_minute.add(write_key)

    def path_for(self, timestamp: datetime) -> Path:
        return self.output_dir / f"{timestamp:%Y%m%d}.csv"


def processed_columns(sessions: list[ExpirySession]) -> list[str]:
    strikes = sorted({strike for session in sessions for strike in session.chain})
    strike_columns = [
        f"strike_{strike}_{field}" for strike in strikes for field in PER_STRIKE_FIELDS
    ]
    return PROCESSED_COLUMNS + strike_columns


def processed_row(
    timestamp: datetime,
    session: ExpirySession,
    result: AnalyticsResult,
    spot_points: list[tuple[datetime, float]],
    universal_mid_points: list[tuple[datetime, float]],
    portfolio_total_pnl: float | None,
    portfolio_gamma_diff_total: float | None,
    frozen_iv_total_pnl: float | None,
) -> dict[str, object]:
    a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params
    elapsed_spot = elapsed_points(spot_points, timestamp)
    current_spot = elapsed_spot[-1][1] if elapsed_spot else None
    market = session.config.market
    row: dict[str, object] = {
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": timestamp.strftime("%Y-%m-%d"),
        "underlying": session.spec.underlying,
        "expiry": session.spec.expiry.isoformat(),
        "user_value": result.user_value,
        "best_bid": result.best_bid,
        "best_ask": result.best_ask,
        "universal_mid": result.universal_mid,
        "universal_spot": result.universal_spot,
        "basis": "" if current_spot is None else result.universal_mid - current_spot,
        "atm_vol": result.atm_vol,
        "vol_spot_10": annualized_vol(elapsed_spot, market.calendar_days, result.intraday_var, 10),
        "vol_universal_mid_10": annualized_vol(
            universal_mid_points,
            market.calendar_days,
            result.intraday_var,
            10,
        ),
        "vol_spot_full_day": annualized_vol(
            elapsed_spot,
            market.calendar_days,
            result.intraday_var,
            None,
        ),
        "vol_universal_mid_full_day": annualized_vol(
            universal_mid_points,
            market.calendar_days,
            result.intraday_var,
            None,
        ),
        "time": result.time,
        "full_days": result.full_days,
        "fraction_days": result.fraction_days,
        "intraday": result.intraday,
        "intraday_var": result.intraday_var,
        "calendar_days": market.calendar_days,
        "risk_free_rate": market.risk_free_rate,
        "funding_rate": market.funding_rate,
        "brokerage_rate": market.brokerage_rate,
        "fit_error": result.fit_error,
        "param_a": a_fit,
        "param_bL": bl_fit,
        "param_bR": br_fit,
        "param_capL": capl_fit,
        "param_floorR": floorr_fit,
        "portfolio_total_pnl": blank_if_none(portfolio_total_pnl),
        "portfolio_gamma_diff_total": blank_if_none(portfolio_gamma_diff_total),
        "frozen_iv_total_pnl": blank_if_none(frozen_iv_total_pnl),
    }
    for result_row in result.rows:
        prefix = f"strike_{result_row.strike}_"
        for field in PER_STRIKE_FIELDS:
            row[f"{prefix}{field}"] = blank_if_none(getattr(result_row, field))
    return row


def elapsed_points(
    points: list[tuple[datetime, float]],
    timestamp: datetime,
) -> list[tuple[datetime, float]]:
    return [(point_time, value) for point_time, value in points if point_time <= timestamp]


def annualized_vol(
    points: list[tuple[datetime, float]],
    calendar_days: float,
    intraday_var: float,
    window: int | None,
) -> float | str:
    if intraday_var == 0:
        return ""

    variances = one_minute_variances(points)
    if window is not None:
        if len(variances) < window:
            return ""
        variances = variances[-window:]
    if not variances:
        return ""

    average_variance = sum(variances) / len(variances)
    return math.sqrt(average_variance * 375 * calendar_days / intraday_var)


def one_minute_variances(points: list[tuple[datetime, float]]) -> list[float]:
    variances = []
    for (previous_time, previous_value), (timestamp, value) in zip(points, points[1:]):
        if (timestamp - previous_time).total_seconds() != 60:
            continue
        if previous_value <= 0 or value <= 0:
            continue
        log_return = math.log(value / previous_value)
        variances.append(log_return * log_return)
    return variances


def load_processed_series(path: Path, variable: str) -> list[tuple[str, float | str]]:
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    return [(row["timestamp"], parse_series_value(row.get(variable, ""))) for row in rows]


def parse_series_value(value: str) -> float | str:
    if value == "":
        return ""
    try:
        return float(value)
    except ValueError:
        return value


def blank_if_none(value) -> object:
    return "" if value is None else value
