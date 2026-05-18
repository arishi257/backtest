from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from vol_dashboard import compat  # noqa: F401
from fit_sensex.config import normalize_underlying
from fit_sensex.services.expiry_calendar import read_sheet_rows
from vol_dashboard.models import ExpirySpec


EXCEL_EPOCH_1900 = date(1899, 12, 30)


def load_expiry_specs(workbook_path: Path) -> list[ExpirySpec]:
    rows = read_sheet_rows(workbook_path, "Expiries")
    specs: list[ExpirySpec] = []
    for row in rows[1:]:
        underlying = str(row.get("A", "")).strip()
        expiry_value = row.get("B", "")
        if not underlying or expiry_value in (None, ""):
            continue
        specs.append(
            ExpirySpec(
                underlying=normalize_underlying(underlying),
                expiry=parse_excel_date(expiry_value),
            )
        )
    if not specs:
        raise ValueError("No expiries found in the Expiries tab.")
    return specs


def parse_excel_date(value) -> date:
    if isinstance(value, date):
        return value

    text = str(value).strip()
    try:
        serial = int(float(text))
        return EXCEL_EPOCH_1900 + timedelta(days=serial)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse expiry date from {value!r}.")

