from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from backtest.config import DEFAULT_UNDERLYING, MARKET_OPEN, MAX_EXPIRIES, REPLAY_END, normalize_underlying


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

    timestamp_text = data["Date"].astype(str) + " " + data["Time"].astype(str)
    data["timestamp"] = parse_timestamps(timestamp_text, normalized_underlying)
    data = data[
        ["ticker", "timestamp", "expiry", "strike", "option_type", "close"]
    ].sort_values(["timestamp", "ticker"])

    start = pd.Timestamp(f"{trade_date} {MARKET_OPEN}", tz="Asia/Kolkata")
    end = pd.Timestamp(f"{trade_date} {REPLAY_END}", tz="Asia/Kolkata")
    timestamps = pd.date_range(start, end, freq="min")
    return OptionDataset(data, trade_date, timestamps, normalized_underlying)


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
