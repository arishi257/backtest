from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = PROJECT_ROOT / "hols.xlsx"
SAMPLE_DATA_ROOT = Path.home() / "OneDrive" / "Options data"
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
    monthly_zip_name = f"{date_key[2:4]}{date_key[6:8]}.zip"

    loose_matches = sorted(SAMPLE_DATA_ROOT.rglob(csv_name))
    if loose_matches:
        return loose_matches[0]

    monthly_zip = SAMPLE_DATA_ROOT / monthly_zip_name
    if monthly_zip.exists():
        # Newer monthly zips contain nested daily zips. Older ones, such as 1225,
        # contain daily CSVs directly under a month folder.
        nested_zip_name = f"{monthly_zip.stem}/GFDLNFO_BACKADJUSTED_{date_key}.zip"
        extracted = extract_nested_daily_csv(monthly_zip, nested_zip_name, csv_name)
        if extracted is not None:
            return extracted
        extracted = extract_daily_csv(monthly_zip, csv_name)
        if extracted is not None:
            return extracted

    for zip_path in sorted(SAMPLE_DATA_ROOT.rglob("*.zip")):
        if zip_path == monthly_zip:
            continue
        extracted = extract_daily_csv(zip_path, csv_name)
        if extracted is not None:
            return extracted
    return None


def extract_nested_daily_csv(monthly_zip: Path, nested_zip_name: str, csv_name: str) -> Path | None:
    with ZipFile(monthly_zip) as outer:
        if nested_zip_name not in outer.namelist():
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
