from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(r"C:\options data\NIFTY")
DEFAULT_CACHE = PROJECT_ROOT / ".backtest_data_cache" / "nifty_1m_0dte_dates_2019_2026.json"
DATE_RE = re.compile(r"(?P<date>\d{8})")


@dataclass(frozen=True)
class Source:
    path: Path
    member: str | None
    nested_member: str | None
    trade_date: date


def main() -> None:
    parser = argparse.ArgumentParser(description="Mine NIFTY 1-minute 0DTE dates from data files.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--start-year", type=int, default=2026)
    parser.add_argument("--end-year", type=int, default=2019)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    years = list(range(args.start_year, args.end_year - 1, -1))
    signature = data_signature(args.data_dir, years)
    if args.cache.exists() and not args.force:
        cached = json.loads(args.cache.read_text(encoding="utf-8"))
        if cached.get("signature") == signature:
            print(args.cache)
            return

    results: dict[str, list[str]] = {str(year): [] for year in years}
    for year in years:
        print(f"Scanning {year}...")
        seen: set[date] = set()
        for source in iter_sources(args.data_dir / str(year)):
            if source.trade_date.year != year:
                continue
            if source.trade_date in seen:
                continue
            if source_has_0dte_nifty(source):
                seen.add(source.trade_date)
                print(f"  {source.trade_date:%d-%b-%Y}")
        results[str(year)] = [day.isoformat() for day in sorted(seen)]

    payload = {
        "data_dir": str(args.data_dir),
        "years": years,
        "signature": signature,
        "results": results,
    }
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    args.cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(args.cache)


def data_signature(data_dir: Path, years: list[int]) -> dict[str, object]:
    rows = []
    for year in years:
        year_dir = data_dir / str(year)
        if not year_dir.exists():
            rows.append([str(year_dir), None, None])
            continue
        for path in sorted(year_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".csv", ".zip"}:
                stat = path.stat()
                rows.append([str(path), stat.st_size, int(stat.st_mtime)])
    return {"file_count": len(rows), "files": rows}


def iter_sources(year_dir: Path):
    if not year_dir.exists():
        return
    files = sorted(year_dir.rglob("*"), key=lambda path: str(path).lower())
    # Loose CSVs are usually daily files. Zips may be direct monthly archives or
    # monthly archives containing nested daily zips.
    for path in files:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".csv":
            trade_date = date_from_text(path.name)
            if trade_date is not None:
                yield Source(path=path, member=None, nested_member=None, trade_date=trade_date)
        elif suffix == ".zip":
            try:
                with ZipFile(path) as zipped:
                    for member in sorted(zipped.namelist()):
                        member_path = Path(member)
                        if member_path.suffix.lower() == ".csv":
                            trade_date = date_from_text(member_path.name)
                            if trade_date is not None:
                                yield Source(path=path, member=member, nested_member=None, trade_date=trade_date)
                        elif member_path.suffix.lower() == ".zip":
                            trade_date = date_from_text(member_path.name)
                            if trade_date is not None:
                                yield Source(path=path, member=member, nested_member="*", trade_date=trade_date)
            except BadZipFile:
                continue


def source_has_0dte_nifty(source: Source) -> bool:
    target = f"NIFTY{source.trade_date:%d%b%y}".upper().encode("ascii")
    if source.member is None:
        return stream_contains(source.path.open("rb"), target)
    try:
        with ZipFile(source.path) as outer:
            if source.nested_member is None:
                with outer.open(source.member) as handle:
                    return stream_contains(handle, target)
            nested_bytes = outer.read(source.member)
            from io import BytesIO

            with ZipFile(BytesIO(nested_bytes)) as inner:
                for nested in inner.namelist():
                    if Path(nested).suffix.lower() != ".csv":
                        continue
                    with inner.open(nested) as handle:
                        if stream_contains(handle, target):
                            return True
    except (BadZipFile, KeyError):
        return False
    return False


def stream_contains(handle, target: bytes, chunk_size: int = 1024 * 1024) -> bool:
    overlap = b""
    with handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return False
            data = overlap + chunk.upper()
            if target in data:
                return True
            overlap = data[-len(target) :]


def date_from_text(text: str) -> date | None:
    match = DATE_RE.search(text)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("date"), "%d%m%Y").date()
    except ValueError:
        return None


if __name__ == "__main__":
    main()
