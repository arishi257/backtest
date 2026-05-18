from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from backtest.config import MARKET_OPEN, MAX_EXPIRIES, REPLAY_END, UNDERLYING


TICKER_PATTERN = (
    rf"^(?P<underlying>{UNDERLYING})(?P<expiry_text>\d{{2}}[A-Z]{{3}}\d{{2}})"
    r"(?P<strike>\d+)(?P<option_type>CE|PE)\.NFO$"
)


@dataclass(frozen=True)
class OptionDataset:
    frame: pd.DataFrame
    trade_date: date
    timestamps: pd.DatetimeIndex

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


def load_option_dataset(csv_path: Path) -> OptionDataset:
    columns = ["Ticker", "Date", "Time", "Close"]
    raw = pd.read_csv(csv_path, usecols=columns)
    parsed = raw["Ticker"].astype(str).str.extract(TICKER_PATTERN)
    data = raw[parsed["underlying"].eq(UNDERLYING)].copy()
    parsed = parsed.loc[data.index]
    if data.empty:
        raise ValueError(f"No {UNDERLYING} option rows were found in {csv_path}.")

    data["ticker"] = data["Ticker"].astype(str)
    data["expiry"] = pd.to_datetime(
        parsed["expiry_text"],
        format="%d%b%y",
    ).dt.date
    data["strike"] = parsed["strike"].astype(int)
    data["option_type"] = parsed["option_type"]
    data["close"] = pd.to_numeric(data["Close"], errors="coerce")
    data = data.dropna(subset=["close"])
    nearest_expiries = sorted(data["expiry"].unique())[:MAX_EXPIRIES]
    data = data[data["expiry"].isin(nearest_expiries)]

    trade_dates = pd.to_datetime(data["Date"], dayfirst=True).dt.date
    trade_date = trade_dates.iloc[0]
    if trade_dates.nunique() != 1:
        raise ValueError("Expected a single trading date in the sample CSV.")

    timestamp_text = data["Date"].astype(str) + " " + data["Time"].astype(str)
    data["timestamp"] = (
        pd.to_datetime(timestamp_text, dayfirst=True)
        .dt.tz_localize("Asia/Kolkata")
        .dt.floor("min")
    )
    data = data[
        ["ticker", "timestamp", "expiry", "strike", "option_type", "close"]
    ].sort_values(["timestamp", "ticker"])

    start = pd.Timestamp(f"{trade_date} {MARKET_OPEN}", tz="Asia/Kolkata")
    end = pd.Timestamp(f"{trade_date} {REPLAY_END}", tz="Asia/Kolkata")
    timestamps = pd.date_range(start, end, freq="min")
    return OptionDataset(data, trade_date, timestamps)
