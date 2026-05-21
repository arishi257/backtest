from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import subprocess
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
    if underlying.upper() == "SENSEX":
        return load_sensex_index_series(trade_date)

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


def load_sensex_index_series(trade_date: date) -> SpotSeries:
    archive = sensex_index_archive(trade_date)
    if archive is None:
        raise FileNotFoundError(f"Could not find SENSEX index archive for {trade_date:%Y-%m-%d}.")

    member = next(
        (
            name
            for name in rar_members(archive)
            if Path(name).name.upper() == "SENSEX.CSV"
        ),
        None,
    )
    if member is None:
        raise FileNotFoundError(f"Could not find SENSEX.csv inside {archive}.")

    text = rar_member_text(archive, member)
    bfo_date_text = trade_date.strftime("%m/%d/%Y")
    points = []
    for line in text.splitlines()[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7 or parts[1] != bfo_date_text:
            continue
        timestamp = datetime.strptime(
            f"{parts[1]} {parts[2]}",
            "%m/%d/%Y %H:%M:%S",
        ).replace(tzinfo=IST)
        points.append((timestamp.replace(second=0, microsecond=0), float(parts[6])))
    if not points:
        raise ValueError(f"No SENSEX index rows found for {trade_date:%Y-%m-%d} in {archive}.")
    points = sorted(dict(points).items())
    return SpotSeries("SENSEX", trade_date, archive, points)


def sensex_index_archive(trade_date: date) -> Path | None:
    root = Path.home() / "OneDrive" / "Options data" / "BFO"
    month_dir = root / trade_date.strftime("%b%Y").upper()
    day_folder = month_dir / trade_date.strftime("%b%Y").upper() / trade_date.strftime("%d%m%Y")
    if day_folder.exists():
        matches = sorted(day_folder.glob("*Indic*.rar")) + sorted(day_folder.glob("*Indices*.rar"))
        if matches:
            return matches[0]
    month_code = trade_date.strftime("%m%y")
    for candidate in (
        month_dir / f"BSE_Indices_{month_code}.rar",
        month_dir / f"BSE Indices {month_code}.rar",
    ):
        if candidate.exists():
            return candidate
    matches = sorted(month_dir.rglob("*Indic*.rar")) + sorted(month_dir.rglob("*Indices*.rar")) if month_dir.exists() else []
    return matches[0] if matches else None


def rar_members(rar_path: Path) -> list[str]:
    result = subprocess.run(
        ["tar", "-tf", str(rar_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1) or not result.stdout:
        raise RuntimeError(f"Could not list archive {rar_path}: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def rar_member_text(rar_path: Path, member: str) -> str:
    result = subprocess.run(
        ["tar", "-xOf", str(rar_path), member],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
