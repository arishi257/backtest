from __future__ import annotations

import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET


EXCEL_EPOCH_1900 = date(1899, 12, 30)
MAIN_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def load_variables(workbook_path: Path, underlying: str | None = None) -> dict[str, str]:
    if not workbook_path.exists():
        return {}
    if workbook_path.suffix.lower() != ".xlsx":
        raise ValueError("Variables workbook must be an .xlsx file.")

    rows = read_sheet_rows_with_fallback(workbook_path, "variables", underlying)
    values: dict[str, str] = {}
    for row in rows[1:]:
        name = str(row.get("A", "")).strip()
        value = str(row.get("B", "")).strip().replace("\xa0", " ")
        if name and value:
            values[name] = value

    return values


def load_full_days_for_expiry(
    workbook_path: Path,
    expiry: date,
    underlying: str | None = None,
) -> float:
    if not workbook_path.exists():
        raise FileNotFoundError(f"Holiday workbook not found: {workbook_path}")
    if workbook_path.suffix.lower() != ".xlsx":
        raise ValueError("Holiday workbook must be an .xlsx file.")

    target_serial = date_to_excel_serial(expiry)
    rows = read_sheet_rows_with_fallback(workbook_path, "hols", underlying)

    for row in rows:
        expiry_cell = row.get("A")
        full_days_cell = row.get("C")
        if full_days_cell in (None, ""):
            continue
        try:
            full_days_value = float(full_days_cell)
        except (TypeError, ValueError):
            continue

        if cell_matches_expiry(expiry_cell, expiry, target_serial):
            return full_days_value

    raise ValueError(
        f"Expiry {expiry} was not found in column A of {workbook_path.name}."
    )


def load_model_params(
    workbook_path: Path,
    underlying: str | None = None,
) -> dict[str, float]:
    if not workbook_path.exists():
        raise FileNotFoundError(f"Holiday workbook not found: {workbook_path}")
    if workbook_path.suffix.lower() != ".xlsx":
        raise ValueError("Holiday workbook must be an .xlsx file.")

    rows = read_sheet_rows_with_fallback(workbook_path, "params", underlying)
    values = {
        str(row.get("A", "")).strip(): float(row["B"])
        for row in rows
        if row.get("A") and row.get("B") not in (None, "")
    }
    required = {"a", "bL", "bR", "capL", "floorR"}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(
            f"Missing model params in params tab column A: {', '.join(missing)}"
        )

    return {name: values[name] for name in required}


def date_to_excel_serial(value: date) -> int:
    return (value - EXCEL_EPOCH_1900).days


def cell_matches_expiry(cell_value: str | None, expiry: date, serial: int) -> bool:
    if cell_value is None:
        return False

    text = str(cell_value).strip()
    if not text:
        return False

    try:
        return int(float(text)) == serial
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d, %Y", "%B %d,%Y"):
        try:
            return datetime.strptime(text, fmt).date() == expiry
        except ValueError:
            continue

    return False


def read_sheet_rows(workbook_path: Path, sheet_name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet_path = sheet_path_by_name(archive, sheet_name)
        sheet_xml = ET.fromstring(archive.read(sheet_path))

    rows: list[dict[str, str]] = []
    for row in sheet_xml.findall(".//m:sheetData/m:row", MAIN_NS):
        values: dict[str, str] = {}
        for cell in row.findall("m:c", MAIN_NS):
            ref = cell.attrib.get("r", "")
            match = re.match(r"[A-Z]+", ref)
            if match is None:
                continue

            column = match.group(0)
            values[column] = read_cell_value(cell, shared_strings)
        rows.append(values)

    return rows


def read_sheet_rows_with_fallback(
    workbook_path: Path,
    base_sheet_name: str,
    underlying: str | None = None,
) -> list[dict[str, str]]:
    sheet_names = sheet_name_candidates(base_sheet_name, underlying)
    last_error: Exception | None = None
    for sheet_name in sheet_names:
        try:
            return read_sheet_rows(workbook_path, sheet_name)
        except ValueError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise ValueError(f"Workbook has no '{base_sheet_name}' sheet.")


def sheet_name_candidates(base_sheet_name: str, underlying: str | None) -> list[str]:
    if not underlying:
        return [base_sheet_name]

    normalized = underlying.strip().lower()
    return [f"{base_sheet_name}_{normalized}", base_sheet_name]


def workbook_has_sheet(workbook_path: Path, sheet_name: str) -> bool:
    if not workbook_path.exists():
        return False

    with zipfile.ZipFile(workbook_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))

    normalized_name = sheet_name.strip().lower()
    for sheet in workbook.findall("m:sheets/m:sheet", MAIN_NS):
        if sheet.attrib.get("name", "").strip().lower() == normalized_name:
            return True
    return False


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(text.text or "" for text in item.findall(".//m:t", MAIN_NS))
        for item in root.findall("m:si", MAIN_NS)
    ]


def sheet_path_by_name(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    target_sheet = None
    normalized_name = sheet_name.strip().lower()
    for sheet in workbook.findall("m:sheets/m:sheet", MAIN_NS):
        if sheet.attrib.get("name", "").strip().lower() == normalized_name:
            target_sheet = sheet
            break

    if target_sheet is None:
        raise ValueError(f"Workbook has no '{sheet_name}' sheet.")

    rel_id = target_sheet.attrib[f"{{{office_rel_ns}}}id"]
    for relationship in rels.findall("r:Relationship", rel_ns):
        if relationship.attrib.get("Id") == rel_id:
            target = relationship.attrib["Target"]
            return "xl/" + target.lstrip("/")

    raise ValueError(f"Could not resolve the '{sheet_name}' worksheet in workbook.")


def read_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//m:t", MAIN_NS))

    value = cell.find("m:v", MAIN_NS)
    if value is None or value.text is None:
        return ""

    raw_value = value.text
    if cell_type == "s":
        return shared_strings[int(raw_value)]

    return raw_value
