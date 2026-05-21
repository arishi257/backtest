from __future__ import annotations

from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import subprocess
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = PROJECT_ROOT / "hols.xlsx"
SAMPLE_DATA_ROOT = Path(
    os.environ.get(
        "BACKTEST_OPTIONS_DATA_ROOT",
        str(Path.home() / "OneDrive" / "Options data"),
    )
)
EXTRACTED_DATA_DIR = PROJECT_ROOT / ".backtest_data_cache"
DEFAULT_FILE_DATE = "09042026"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backtest_snapshots"
DEFAULT_PROCESSED_OUTPUT_DIR = PROJECT_ROOT / "processed_data"
DEFAULT_UNDERLYING = "NIFTY"
SUPPORTED_UNDERLYINGS = ("NIFTY", "SENSEX")
MAX_EXPIRIES = 1
MARKET_OPEN = "09:15"
REPLAY_END = "15:24"


def normalize_underlying(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in SUPPORTED_UNDERLYINGS:
        raise ValueError(
            f"Unsupported underlying {value!r}. Use one of: {', '.join(SUPPORTED_UNDERLYINGS)}."
        )
    return normalized


def options_data_root(underlying: str) -> Path:
    return SAMPLE_DATA_ROOT / ("BFO" if normalize_underlying(underlying) == "SENSEX" else "NFO")


def resolve_sample_csv(
    file_date: str | None,
    explicit_csv: Path | None = None,
    underlying: str = DEFAULT_UNDERLYING,
) -> Path:
    if explicit_csv is not None:
        return explicit_csv

    date_key = normalize_file_date(file_date or DEFAULT_FILE_DATE)
    normalized_underlying = normalize_underlying(underlying)
    csv_path = find_sample_csv(date_key, normalized_underlying)
    if csv_path is None:
        raise FileNotFoundError(
            f"Could not find {normalized_underlying} option data ending with {date_key} "
            f"under {options_data_root(normalized_underlying)}."
        )
    return csv_path


def find_sample_csv(date_key: str, underlying: str = DEFAULT_UNDERLYING) -> Path | None:
    if normalize_underlying(underlying) == "SENSEX":
        return find_bfo_sample_csv(date_key)
    return find_nfo_sample_csv(date_key)


def find_nfo_sample_csv(date_key: str) -> Path | None:
    csv_name = f"GFDLNFO_BACKADJUSTED_{date_key}.csv"
    root = options_data_root("NIFTY")

    loose_matches = sorted(
        path
        for path in root.rglob(f"*{date_key}.csv")
        if path.name == csv_name or path.stem.endswith(date_key)
    )
    if loose_matches:
        return loose_matches[0]

    for monthly_zip in candidate_archive_paths(date_key, root):
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

    for zip_path in sorted(root.rglob("*.zip")):
        if zip_path in candidate_archive_paths(date_key, root):
            continue
        extracted = extract_nested_daily_csv(zip_path, date_key, csv_name)
        if extracted is not None:
            return extracted
        extracted = extract_daily_csv(zip_path, csv_name)
        if extracted is not None:
            return extracted
    return None


def candidate_archive_paths(date_key: str, root: Path) -> list[Path]:
    year = date_key[4:8]
    year_month = f"{year}{date_key[2:4]}"
    month_year = f"{date_key[2:4]}{date_key[6:8]}"
    names = [
        f"{month_year}.zip",
        f"GDFL_FNO_{year_month}.zip",
        f"{year}.zip",
    ]
    roots = [root, root / year]
    return [root / name for root in roots for name in names]


def find_bfo_sample_csv(date_key: str) -> Path | None:
    output_path = EXTRACTED_DATA_DIR / f"BFO_SENSEX_{date_key}.csv"
    if output_path.exists():
        return output_path

    rar_path = find_bfo_option_archive(date_key)
    if rar_path is None:
        return None

    rows = bfo_option_rows(rar_path, date_key)
    if not rows:
        return None

    EXTRACTED_DATA_DIR.mkdir(exist_ok=True)
    with output_path.open("w", newline="") as file:
        file.write("Ticker,Date,Time,Close\n")
        for ticker, date_text, time_text, close in rows:
            file.write(f"{ticker},{date_text},{time_text},{close}\n")
    return output_path


def find_bfo_option_archive(date_key: str) -> Path | None:
    trade_date = datetime.strptime(date_key, "%d%m%Y")
    root = options_data_root("SENSEX")
    month_dir = root / trade_date.strftime("%b%Y").upper()
    day_folder = month_dir / trade_date.strftime("%b%Y").upper() / date_key
    if day_folder.exists():
        matches = sorted(day_folder.glob("*Option*.rar"))
        if matches:
            return matches[0]

    month_code = trade_date.strftime("%m%y")
    candidates = [
        month_dir / f"BSE_Option_{month_code}.rar",
        month_dir / f"BSE Option {month_code}.rar",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    matches = sorted(month_dir.rglob("*Option*.rar")) if month_dir.exists() else []
    return matches[0] if matches else None


def bfo_option_rows(rar_path: Path, date_key: str) -> list[tuple[str, str, str, str]]:
    trade_date = datetime.strptime(date_key, "%d%m%Y")
    bfo_date_text = trade_date.strftime("%m/%d/%Y")
    normalized_date_text = trade_date.strftime("%Y-%m-%d")
    rows: list[tuple[str, str, str, str]] = []
    members = [
        member
        for member in rar_members(rar_path)
        if Path(member).name.startswith("SENSEX")
        and Path(member).suffix.lower() == ".csv"
        and Path(member).stem[-2:] in {"CE", "PE"}
    ]
    for member in members:
        content = rar_member_text(rar_path, member)
        for line in content.splitlines()[1:]:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 7 or parts[1] != bfo_date_text:
                continue
            rows.append((parts[0], normalized_date_text, parts[2], parts[6]))
    return rows


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
