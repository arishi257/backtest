from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from backtest.config import DEFAULT_UNDERLYING, MARKET_OPEN, MAX_EXPIRIES, REPLAY_END, normalize_underlying
from backtest.config import EXTRACTED_DATA_DIR


NFO_TICKER_PATTERN = (
    r"^(?P<underlying>{underlying})(?P<expiry_text>\d{{2}}[A-Z]{{3}}\d{{2}})"
    r"(?P<strike>\d+)(?P<option_type>CE|PE)\.NFO$"
)
BFO_TICKER_PATTERN = (
    r"^(?P<underlying>{underlying})(?P<expiry_text>\d{{6}})"
    r"(?P<strike>\d+)(?P<option_type>CE|PE)$"
)


@dataclass(frozen=True)
class OptionDataset:
    frame: pd.DataFrame
    trade_date: date
    timestamps: pd.DatetimeIndex
    underlying: str
    future_series: FutureSeries | None = None

    @property
    def expiries(self) -> list[date]:
        return sorted(self.frame["expiry"].unique())

    def option_surface(self, expiry: date) -> pd.DataFrame:
        expiry_frame = self.frame[self.frame["expiry"] == expiry]
        surface = expiry_frame.pivot_table(
            index="timestamp",
            columns="ticker",
            values="close",
            aggfunc="last",
        )
        return surface.reindex(self.timestamps).ffill()


@dataclass(frozen=True)
class FutureBar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class FutureSeries:
    ticker: str
    bars: dict[datetime, FutureBar]

    def bar_at(self, timestamp: datetime) -> FutureBar | None:
        return self.bars.get(timestamp)


def load_option_dataset(
    csv_path: Path,
    underlying: str = DEFAULT_UNDERLYING,
) -> OptionDataset:
    normalized_underlying = normalize_underlying(underlying)
    columns = ["Ticker", "Date", "Time", "Close"]
    raw = pd.read_csv(csv_path, usecols=columns)
    parsed = raw["Ticker"].astype(str).str.extract(ticker_pattern(normalized_underlying))
    data = raw[parsed["underlying"].eq(normalized_underlying)].copy()
    parsed = parsed.loc[data.index]
    if data.empty:
        raise ValueError(f"No {normalized_underlying} option rows were found in {csv_path}.")

    data["ticker"] = data["Ticker"].astype(str)
    data["expiry"] = parse_expiry_text(parsed["expiry_text"], normalized_underlying).dt.date
    data["strike"] = parsed["strike"].astype(int)
    data["option_type"] = parsed["option_type"]
    data["close"] = pd.to_numeric(data["Close"], errors="coerce")
    data = data.dropna(subset=["close"])
    nearest_expiries = sorted(data["expiry"].unique())[:MAX_EXPIRIES]
    data = data[data["expiry"].isin(nearest_expiries)]

    trade_dates = parse_trade_dates(data["Date"], normalized_underlying).dt.date
    trade_date = trade_dates.iloc[0]
    if trade_dates.nunique() != 1:
        raise ValueError("Expected a single trading date in the sample CSV.")
    future_series = load_underlying_ohlc_series(csv_path, normalized_underlying, trade_date)

    timestamp_text = data["Date"].astype(str) + " " + data["Time"].astype(str)
    data["timestamp"] = parse_timestamps(timestamp_text, normalized_underlying)
    data = data[
        ["ticker", "timestamp", "expiry", "strike", "option_type", "close"]
    ].sort_values(["timestamp", "ticker"])

    start = pd.Timestamp(f"{trade_date} {MARKET_OPEN}", tz="Asia/Kolkata")
    end = pd.Timestamp(f"{trade_date} {REPLAY_END}", tz="Asia/Kolkata")
    timestamps = pd.date_range(start, end, freq="min")
    return OptionDataset(data, trade_date, timestamps, normalized_underlying, future_series)


def load_underlying_ohlc_series(
    csv_path: Path,
    underlying: str,
    trade_date: date,
) -> FutureSeries | None:
    if underlying == "SENSEX":
        return load_sensex_underlying_series(trade_date)
    return load_cached_nearest_future_series(csv_path, underlying)


def load_sensex_underlying_series(trade_date: date) -> FutureSeries:
    from backtest.spot_data import load_sensex_index_ohlc_series

    series = load_sensex_index_ohlc_series(trade_date)
    bars = {
        row.timestamp.to_pydatetime(): FutureBar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in series.frame[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)
    }
    return FutureSeries(ticker="SENSEX", bars=bars)


def load_cached_nearest_future_series(
    csv_path: Path,
    underlying: str = DEFAULT_UNDERLYING,
) -> FutureSeries | None:
    normalized_underlying = normalize_underlying(underlying)
    ticker = nearest_future_ticker(normalized_underlying)
    if ticker is None:
        return None

    cache_path = future_cache_path(csv_path, normalized_underlying)
    if cache_path.exists():
        cached = pd.read_csv(cache_path)
        return future_series_from_raw(cached, normalized_underlying, ticker)

    try:
        series = load_nearest_future_series(csv_path, normalized_underlying)
    except ValueError:
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "Ticker": series.ticker,
            "Date": timestamp.strftime("%d/%m/%Y"),
            "Time": timestamp.strftime("%H:%M:%S"),
            "Open": bar.open,
            "High": bar.high,
            "Low": bar.low,
            "Close": bar.close,
        }
        for timestamp, bar in sorted(series.bars.items())
    ]
    pd.DataFrame(rows).to_csv(cache_path, index=False)
    return series


def future_cache_path(csv_path: Path, underlying: str) -> Path:
    date_key = csv_path.stem.rsplit("_", 1)[-1]
    return EXTRACTED_DATA_DIR / f"{underlying}_NEAREST_FUTURE_{date_key}.csv"


def load_nearest_future_series(
    csv_path: Path,
    underlying: str = DEFAULT_UNDERLYING,
) -> FutureSeries:
    normalized_underlying = normalize_underlying(underlying)
    ticker = nearest_future_ticker(normalized_underlying)
    if ticker is None:
        raise ValueError(f"Nearest futures ticker is not configured for {normalized_underlying}.")

    raw = pd.read_csv(csv_path, usecols=["Ticker", "Date", "Time", "Open", "High", "Low", "Close"])
    series = future_series_from_raw(raw, normalized_underlying, ticker)
    if series is None:
        raise ValueError(f"No {ticker} futures rows were found in {csv_path}.")
    return series


def future_series_from_raw(
    raw: pd.DataFrame,
    underlying: str,
    ticker: str,
) -> FutureSeries | None:
    data = raw[raw["Ticker"].astype(str).eq(ticker)].copy()
    if data.empty:
        return None

    timestamp_text = data["Date"].astype(str) + " " + data["Time"].astype(str)
    data["timestamp"] = parse_timestamps(timestamp_text, underlying)
    for column in ("Open", "High", "Low", "Close"):
        data[column.lower()] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close"])
    data = data.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    bars = {
        row.timestamp.to_pydatetime(): FutureBar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in data[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)
    }
    return FutureSeries(ticker=ticker, bars=bars)


def nearest_future_ticker(underlying: str) -> str | None:
    if underlying == "NIFTY":
        return "NIFTY-I.NFO"
    return None


def ticker_pattern(underlying: str) -> str:
    if underlying == "SENSEX":
        return BFO_TICKER_PATTERN.format(underlying=underlying)
    return NFO_TICKER_PATTERN.format(underlying=underlying)


def parse_expiry_text(expiry_text: pd.Series, underlying: str) -> pd.Series:
    if underlying == "SENSEX":
        return pd.to_datetime(expiry_text, format="%y%m%d")
    return pd.to_datetime(expiry_text, format="%d%b%y")


def parse_trade_dates(values: pd.Series, underlying: str) -> pd.Series:
    if underlying == "SENSEX":
        return pd.to_datetime(values, format="%Y-%m-%d")
    return pd.to_datetime(values, dayfirst=True)


def parse_timestamps(values: pd.Series, underlying: str) -> pd.Series:
    if underlying == "SENSEX":
        parsed = pd.to_datetime(values, format="%Y-%m-%d %H:%M:%S")
    else:
        parsed = pd.to_datetime(values, dayfirst=True)
    return parsed.dt.tz_localize("Asia/Kolkata").dt.floor("min")
