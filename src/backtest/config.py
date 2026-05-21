from __future__ import annotations

from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = PROJECT_ROOT / "hols.xlsx"
SAMPLE_DATA_ROOT = Path(os.environ.get("BACKTEST_OPTIONS_DATA_ROOT", r"C:\options data"))
EXTRACTED_DATA_DIR = PROJECT_ROOT / ".backtest_data_cache"
DEFAULT_FILE_DATE = "09042026"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backtest_snapshots"
UNDERLYING = "NIFTY"
MAX_EXPIRIES = 1
MARKET_OPEN = "09:15"
REPLAY_END = "15:24"


def resolve_sample_csv(file_date: str | None, explicit_csv: Path | None = None) -> Path:
    if explicit_csv is not None:
        return explicit_csv

    date_key = normalize_file_date(file_date or DEFAULT_FILE_DATE)
    csv_path = find_sample_csv(date_key)
    if csv_path is None:
        raise FileNotFoundError(
            f"Could not find GFDLNFO_BACKADJUSTED data ending with {date_key} "
            f"under {SAMPLE_DATA_ROOT}."
        )
    return csv_path


def find_sample_csv(date_key: str) -> Path | None:
    csv_name = f"GFDLNFO_BACKADJUSTED_{date_key}.csv"

    loose_matches = sorted(
        path
        for path in SAMPLE_DATA_ROOT.rglob(f"*{date_key}.csv")
        if path.name == csv_name or path.stem.endswith(date_key)
    )
    if loose_matches:
        return loose_matches[0]

    for monthly_zip in candidate_archive_paths(date_key):
        if not monthly_zip.exists():
            continue
        # Newer monthly zips contain nested daily zips. Older ones, such as 1225,
        # contain daily CSVs directly under a month folder.
        extracted = extract_nested_daily_csv(monthly_zip, date_key, csv_name)
        if extracted is not None:
            return extracted
        extracted = extract_daily_csv(monthly_zip, csv_name)
        if extracted is not None:
            return extracted

    for zip_path in sorted(SAMPLE_DATA_ROOT.rglob("*.zip")):
        if zip_path in candidate_archive_paths(date_key):
            continue
        extracted = extract_nested_daily_csv(zip_path, date_key, csv_name)
        if extracted is not None:
            return extracted
        extracted = extract_daily_csv(zip_path, csv_name)
        if extracted is not None:
            return extracted
    return None


def candidate_archive_paths(date_key: str) -> list[Path]:
    year = date_key[4:8]
    year_month = f"{year}{date_key[2:4]}"
    month_year = f"{date_key[2:4]}{date_key[6:8]}"
    names = [
        f"{month_year}.zip",
        f"GDFL_FNO_{year_month}.zip",
        f"{year}.zip",
    ]
    roots = [SAMPLE_DATA_ROOT, SAMPLE_DATA_ROOT / year]
    return [root / name for root in roots for name in names]


def extract_nested_daily_csv(monthly_zip: Path, date_key: str, csv_name: str) -> Path | None:
    with ZipFile(monthly_zip) as outer:
        nested_zip_name = next(
            (
                name
                for name in outer.namelist()
                if Path(name).suffix.lower() == ".zip"
                and Path(name).stem.endswith(date_key)
            ),
            None,
        )
        if nested_zip_name is None:
            return None
        daily_zip_bytes = outer.read(nested_zip_name)

    with ZipFile(BytesIO(daily_zip_bytes)) as daily_zip:
        return extract_csv_member(daily_zip, csv_name)


def extract_daily_csv(zip_path: Path, csv_name: str) -> Path | None:
    with ZipFile(zip_path) as archive:
        if csv_name in (Path(name).name for name in archive.namelist()):
            return extract_csv_member(archive, csv_name)
    return None


def extract_csv_member(archive: ZipFile, csv_name: str) -> Path | None:
    member_name = next(
        (name for name in archive.namelist() if Path(name).name == csv_name),
        None,
    )
    if member_name is None:
        return None

    EXTRACTED_DATA_DIR.mkdir(exist_ok=True)
    extracted_path = EXTRACTED_DATA_DIR / csv_name
    if not extracted_path.exists():
        extracted_path.write_bytes(archive.read(member_name))
    return extracted_path


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
