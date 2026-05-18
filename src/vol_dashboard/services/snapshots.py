from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from vol_dashboard import compat  # noqa: F401
from fit_sensex.models import AnalyticsResult
from vol_dashboard.models import ExpirySession


SNAPSHOT_COLUMNS = [
    "timestamp",
    "underlying",
    "expiry",
    "universal_mid",
    "atm_vol",
    "time",
    "risk_free_rate",
    "user_locked_skew",
    "a",
    "bL",
    "bR",
    "capL",
    "floorR",
]


class SnapshotWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.last_written_minute: dict[str, str] = {}

    def maybe_write(
        self,
        timestamp: datetime,
        session: ExpirySession,
        result: AnalyticsResult | None,
        user_locked_skew: float | None = None,
    ) -> None:
        if result is None:
            return

        minute_key = timestamp.strftime("%Y-%m-%d %H:%M")
        session_key = session.spec.tab_name
        if self.last_written_minute.get(session_key) == minute_key:
            return

        self._append_row(timestamp, session, result, user_locked_skew)
        self.last_written_minute[session_key] = minute_key

    def _append_row(
        self,
        timestamp: datetime,
        session: ExpirySession,
        result: AnalyticsResult,
        user_locked_skew: float | None,
    ) -> None:
        path = self.path_for(session)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=SNAPSHOT_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(snapshot_row(timestamp, session, result, user_locked_skew))

    def path_for(self, session: ExpirySession) -> Path:
        expiry_text = session.spec.expiry.strftime("%Y%m%d")
        filename = f"{session.spec.underlying.lower()}_{expiry_text}.csv"
        return self.output_dir / filename


def snapshot_row(
    timestamp: datetime,
    session: ExpirySession,
    result: AnalyticsResult,
    user_locked_skew: float | None = None,
) -> dict[str, str | float]:
    a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params
    return {
        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "underlying": session.spec.underlying,
        "expiry": session.spec.expiry.isoformat(),
        "universal_mid": result.universal_mid,
        "atm_vol": result.atm_vol,
        "time": result.time,
        "risk_free_rate": session.config.market.risk_free_rate,
        "user_locked_skew": "" if user_locked_skew is None else user_locked_skew,
        "a": a_fit,
        "bL": bl_fit,
        "bR": br_fit,
        "capL": capl_fit,
        "floorR": floorr_fit,
    }


def load_snapshot_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def load_latest_snapshot_row(path: Path) -> dict[str, str] | None:
    rows = load_snapshot_rows(path)
    return rows[-1] if rows else None
