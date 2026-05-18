from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from vol_dashboard import compat  # noqa: F401
from fit_sensex.config import ApiConfig, build_app_config
from fit_sensex.services.expiry_calendar import load_full_days_for_expiry, load_model_params


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKBOOK = PROJECT_ROOT / "hols.xlsx"


def build_api_config_from_env() -> ApiConfig:
    return ApiConfig(
        api_key=os.getenv("KITE_API_KEY", ""),
        access_token=os.getenv("KITE_ACCESS_TOKEN", ""),
    )


def build_expiry_config(underlying: str, expiry, workbook_path: Path):
    previous = os.environ.get("HOLIDAYS_FILE")
    os.environ["HOLIDAYS_FILE"] = str(workbook_path)
    try:
        config = build_app_config(underlying)
    finally:
        if previous is None:
            os.environ.pop("HOLIDAYS_FILE", None)
        else:
            os.environ["HOLIDAYS_FILE"] = previous

    full_days = load_full_days_for_expiry(
        workbook_path,
        expiry,
        config.market.underlying,
    )
    model_params = load_model_params(workbook_path, config.market.underlying)

    return replace(
        config,
        api=build_api_config_from_env(),
        market=replace(config.market, full_days=full_days, holidays_file=workbook_path),
        model=replace(
            config.model,
            initial_a=model_params["a"],
            initial_bl=model_params["bL"],
            initial_br=model_params["bR"],
            initial_capl=model_params["capL"],
            initial_floorr=model_params["floorR"],
        ),
    )

