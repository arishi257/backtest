from __future__ import annotations

import argparse
import math
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.config import DEFAULT_WORKBOOK, MARKET_OPEN, normalize_underlying
from backtest.data import FutureBar, FutureSeries, OptionDataset
from backtest.headless_portfolio import GammaDiffTracker, ParkGammaTracker
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest_1m.__main__ import (
    DynamicGammaThresholdFrozenIvState,
    DynamicGammaThresholdPortfolioState,
    OhlcVolTracker,
    format_final_metric,
)


DEFAULT_DATA_DIR = Path(r"C:\options data\sample data hf")
CACHE_DIR = Path(__file__).resolve().parents[2] / ".backtest_data_cache" / "sample_hf_1s"
IST = ZoneInfo("Asia/Kolkata")
OPTION_FILE_RE = re.compile(
    r"^(?P<underlying>[A-Z]+)_(?P<expiry>\d{2}[A-Z]{3}\d{2})_"
    r"(?P<strike>\d+)_(?P<option_type>CE|PE)\.csv$",
    re.IGNORECASE,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a headless 1-second sample-data backtest using spot OHLC."
    )
    parser.add_argument("--date", default="26052026", help="Trading date, e.g. 26052026.")
    parser.add_argument(
        "--underlying",
        default="NIFTY",
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument(
        "--dynamic-gamma-threshold-ratio",
        type=float,
        default=0.40,
        help="Re-hedge threshold as a ratio of current absolute gamma lots.",
    )
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    dataset = load_sample_hf_option_dataset(args.data_dir, trade_date, args.underlying)
    final_metrics, cycles, analytics = run_headless(
        dataset,
        args.workbook,
        args.dynamic_gamma_threshold_ratio,
    )

    print(
        f"Completed {cycles} 1-second replay cycles across {len(final_metrics)} expiries. "
        f"Analytics rows: {analytics}. "
        f"Source: {args.data_dir / trade_date.isoformat()}."
    )
    if final_metrics:
        print("Final running PnL and volatility metrics:")
        for tab_name, metrics in final_metrics.items():
            print(f"  {tab_name}:")
            for name, value in metrics.items():
                print(f"    {name}: {format_final_metric(name, value)}")


def run_headless(
    dataset: OptionDataset,
    workbook: Path,
    threshold_ratio: float,
) -> tuple[dict[str, dict[str, float | None]], int, int]:
    future_series = dataset.future_series
    if future_series is None:
        raise ValueError("Spot OHLC was not loaded.")

    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    portfolio_states = {
        session.spec.tab_name: DynamicGammaThresholdPortfolioState(session=session)
        for session in sessions
    }
    frozen_states = {
        session.spec.tab_name: DynamicGammaThresholdFrozenIvState(session)
        for session in sessions
    }
    gamma_trackers = {session.spec.tab_name: GammaDiffTracker() for session in sessions}
    park_gamma_trackers = {session.spec.tab_name: ParkGammaTracker() for session in sessions}
    vol_trackers = {session.spec.tab_name: OhlcVolTracker() for session in sessions}

    cycles = 0
    analytics = 0
    final_metrics: dict[str, dict[str, float | None]] = {}
    while replay.advance():
        cycles += 1
        timestamp = replay.now()
        for session in sessions:
            result = session.analytics.calculate(session.store.snapshot())
            if result is None:
                continue
            analytics += 1
            tab_name = session.spec.tab_name
            portfolio_metrics = portfolio_states[tab_name].update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                threshold_ratio,
            )
            frozen_metrics = frozen_states[tab_name].update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                threshold_ratio,
            )
            gamma_diff_total = gamma_trackers[tab_name].update(
                timestamp,
                result.universal_mid,
                portfolio_metrics.gamma_l,
            )
            bar = future_series.bar_at(timestamp)
            park_gamma_metrics = park_gamma_trackers[tab_name].update(
                bar,
                portfolio_metrics.gamma_l,
            )
            vol_trackers[tab_name].update(
                timestamp,
                bar,
                session.config.market.calendar_days,
                result.intraday_var,
            )
            vol_metrics = vol_trackers[tab_name].metrics()
            final_metrics[tab_name] = {
                "portfolio_total_pnl": portfolio_metrics.total_pnl,
                "portfolio_gamma_l": portfolio_metrics.gamma_l,
                "portfolio_gamma_diff_total": gamma_diff_total,
                "park_gamma_pnl_diff_total": park_gamma_metrics.park_gamma_pnl_diff_total,
                "gk_gamma_pnl_diff_total": park_gamma_metrics.gk_gamma_pnl_diff_total,
                "frozen_iv_total_pnl": frozen_metrics.total_pnl,
                "close_to_close_vol": vol_metrics.close_to_close_vol,
                "park_vol": vol_metrics.park_vol,
                "gk_vol": vol_metrics.gk_vol,
            }

    return final_metrics, cycles, analytics


def load_sample_hf_option_dataset(
    data_dir: Path,
    trade_date: date,
    underlying: str,
) -> OptionDataset:
    normalized_underlying = normalize_underlying(underlying)
    cache_path = cache_file_path(data_dir, trade_date, normalized_underlying)
    if cache_path.exists():
        try:
            cached = pd.read_pickle(cache_path)
        except AttributeError:
            cached = parse_sample_hf_dataset(data_dir, trade_date, normalized_underlying)
            write_dataset_cache(cached, cache_path)
    else:
        cached = parse_sample_hf_dataset(data_dir, trade_date, normalized_underlying)
        write_dataset_cache(cached, cache_path)

    return OptionDataset(
        frame=cached["option_frame"],
        trade_date=trade_date,
        timestamps=cached["timestamps"],
        underlying=normalized_underlying,
        future_series=cached["spot_series"],
    )


def parse_sample_hf_dataset(
    data_dir: Path,
    trade_date: date,
    underlying: str,
) -> dict[str, object]:
    day_dir = resolve_day_dir(data_dir, trade_date, underlying)

    option_frame = load_option_frame(day_dir, trade_date, underlying)
    spot_series, spot_timestamps = load_spot_series(day_dir, underlying)
    start = resolve_start_timestamp(
        spot_timestamps,
        pd.Timestamp(f"{trade_date} {MARKET_OPEN}:00", tz=IST),
    )
    end = pd.Timestamp(f"{trade_date} 15:24:59", tz=IST)
    available = spot_timestamps[(spot_timestamps >= start) & (spot_timestamps <= end)]
    if available.empty:
        raise ValueError(f"No spot timestamps found between {start} and {end}.")
    timestamps = pd.date_range(available.min(), available.max(), freq="s")
    return {
        "option_frame": option_frame,
        "spot_series": spot_series,
        "timestamps": timestamps,
    }


def resolve_start_timestamp(spot_timestamps: pd.DatetimeIndex, target: pd.Timestamp) -> pd.Timestamp:
    if target in spot_timestamps:
        return target
    previous_ticks = spot_timestamps[spot_timestamps < target]
    if not previous_ticks.empty:
        return previous_ticks.max()
    next_ticks = spot_timestamps[spot_timestamps > target]
    if not next_ticks.empty:
        return next_ticks.min()
    raise ValueError(f"No spot timestamps available near {target}.")


def resolve_day_dir(data_dir: Path, trade_date: date, underlying: str) -> Path:
    candidates = [
        data_dir / underlying.lower() / trade_date.isoformat(),
        data_dir / underlying.upper() / trade_date.isoformat(),
        data_dir / trade_date.isoformat(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    checked = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"No data folder found for {underlying} {trade_date}; checked {checked}.")


def write_dataset_cache(dataset: dict[str, object], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(dataset, cache_path)


def load_option_frame(day_dir: Path, trade_date: date, underlying: str) -> pd.DataFrame:
    rows = []
    option_root = day_dir / "options"
    for option_type_dir in ("ce", "pe"):
        for path in sorted((option_root / option_type_dir).glob("*.csv")):
            match = OPTION_FILE_RE.match(path.name)
            if match is None:
                continue
            if match.group("underlying").upper() != underlying:
                continue
            option_type = match.group("option_type").upper()
            expiry = datetime.strptime(match.group("expiry").upper(), "%d%b%y").date()
            strike = int(match.group("strike"))
            ticker = f"{underlying}{expiry:%y%m%d}{strike}{option_type}"
            raw = pd.read_csv(path, usecols=["datetime", "close"])
            raw["timestamp"] = parse_timestamp_column(raw["datetime"])
            raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
            raw = raw.dropna(subset=["timestamp", "close"])
            raw["ticker"] = ticker
            raw["expiry"] = expiry
            raw["strike"] = strike
            raw["option_type"] = option_type
            rows.append(raw[["ticker", "timestamp", "expiry", "strike", "option_type", "close"]])

    if not rows:
        raise ValueError(f"No {underlying} option CSV files found in {option_root}.")
    frame = pd.concat(rows, ignore_index=True)
    frame = frame[frame["timestamp"].dt.date == trade_date]
    if frame.empty:
        raise ValueError(f"No {underlying} option rows found for {trade_date}.")
    return frame.sort_values(["timestamp", "ticker"]).reset_index(drop=True)


def load_spot_series(day_dir: Path, underlying: str) -> tuple[FutureSeries, pd.DatetimeIndex]:
    path = day_dir / "spot" / f"{underlying}_SPOT.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    raw = pd.read_csv(path, usecols=["datetime", "open", "high", "low", "close"])
    raw["timestamp"] = parse_timestamp_column(raw["datetime"])
    for column in ("open", "high", "low", "close"):
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw = raw.dropna(subset=["timestamp", "open", "high", "low", "close"])
    raw = raw.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    bars = {
        row.timestamp.to_pydatetime(): FutureBar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in raw[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)
    }
    if not bars:
        raise ValueError(f"No {underlying} spot candles found in {path}.")
    return FutureSeries(ticker=f"{underlying}_SPOT", bars=bars), pd.DatetimeIndex(raw["timestamp"])


def parse_timestamp_column(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce")
    if parsed.dt.tz is None:
        return parsed.dt.tz_localize(IST)
    return parsed.dt.tz_convert(IST)


def cache_file_path(data_dir: Path, trade_date: date, underlying: str) -> Path:
    source_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(data_dir.resolve()))
    return CACHE_DIR / underlying / f"{source_key}_{trade_date:%Y%m%d}_1s.pkl"


def parse_date_key(value: str) -> date:
    text = value.strip()
    for fmt in ("%d%m%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Could not parse date {value!r}.")


if __name__ == "__main__":
    main()
