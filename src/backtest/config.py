from __future__ import annotations

from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = PROJECT_ROOT / "hols.xlsx"
SAMPLE_DATA_ROOT = Path.home() / "OneDrive" / "Desktop" / "sample data"
DEFAULT_CSV = (
    SAMPLE_DATA_ROOT
    / "GFDLNFO_BACKADJUSTED_09042026"
    / "GFDLNFO_BACKADJUSTED_09042026.csv"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backtest_snapshots"
UNDERLYING = "NIFTY"
MAX_EXPIRIES = 1
MARKET_OPEN = "09:15"
REPLAY_END = "15:24"


def resolve_sample_csv(file_date: str | None, explicit_csv: Path | None = None) -> Path:
    if explicit_csv is not None:
        return explicit_csv
    if file_date is None:
        return DEFAULT_CSV

    date_key = normalize_file_date(file_date)
    folder_name = f"GFDLNFO_BACKADJUSTED_{date_key}"
    return SAMPLE_DATA_ROOT / folder_name / f"{folder_name}.csv"


def normalize_file_date(value: str) -> str:
    text = value.strip()
    if text.isdigit() and len(text) == 8:
        datetime.strptime(text, "%d%m%Y")
        return text

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d%m%Y")
        except ValueError:
            continue
    raise ValueError(
        f"Could not parse date {value!r}. Use DDMMYYYY, DD-MM-YYYY, or YYYY-MM-DD."
    )
