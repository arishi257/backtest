from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


SPOT_DATA_ROOT = Path.home() / "OneDrive" / "spot data"
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SpotSeries:
    underlying: str
    trade_date: date
    source_path: Path
    points: list[tuple[datetime, float]]


def load_spot_series(
    trade_date: date,
    underlying: str = "NIFTY",
    data_root: Path = SPOT_DATA_ROOT,
) -> SpotSeries:
    source_path = spot_file_path(trade_date, underlying, data_root)
    if not source_path.exists():
        raise FileNotFoundError(
            f"Could not find spot file {source_path}. Expected a monthly file named "
            f"like '{trade_date:%Y %b} {underlying}.txt'."
        )

    frame = pd.read_csv(
        source_path,
        header=None,
        names=[
            "underlying",
            "date",
            "time",
            "open",
            "high",
            "low",
            "close",
            "unused_1",
            "unused_2",
        ],
    )
    date_key = int(trade_date.strftime("%Y%m%d"))
    frame = frame[
        (frame["underlying"].astype(str).str.upper() == underlying.upper())
        & (frame["date"].astype(int) == date_key)
    ].copy()
    if frame.empty:
        raise ValueError(f"No {underlying} spot rows found for {date_key} in {source_path}.")

    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"])
    timestamp_text = frame["date"].astype(str) + " " + frame["time"].astype(str)
    frame["timestamp"] = (
        pd.to_datetime(timestamp_text, format="%Y%m%d %H:%M")
        .dt.tz_localize(IST)
        .dt.floor("min")
    )
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    points = [
        (row.timestamp.to_pydatetime(), float(row.close))
        for row in frame[["timestamp", "close"]].itertuples(index=False)
    ]
    return SpotSeries(underlying.upper(), trade_date, source_path, points)


def spot_file_path(trade_date: date, underlying: str, data_root: Path) -> Path:
    month_name = trade_date.strftime("%b").upper()
    return data_root / f"{trade_date:%Y} {month_name} {underlying.upper()}.txt"
