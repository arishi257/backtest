from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.config import UNDERLYING
from backtest.data import OptionDataset
from fit_sensex.config import ApiConfig, build_app_config
from fit_sensex.market.instruments import build_option_chain
from fit_sensex.market.kite_stream import MarketDataStore
from fit_sensex.services.analytics import AnalyticsEngine
from fit_sensex.services.expiry_calendar import load_full_days_for_expiry, load_model_params
from vol_dashboard.models import ExpirySession, ExpirySpec


def build_backtest_sessions(
    dataset: OptionDataset,
    workbook_path: Path,
    current_time: Callable[[], datetime],
) -> list[ExpirySession]:
    sessions = []
    for expiry in dataset.expiries:
        config = build_backtest_config(workbook_path, expiry, dataset.trade_date)
        instruments = instruments_for_expiry(dataset, expiry)
        chain, token_to_strike, tokens = build_option_chain(instruments, config.strikes)
        if not tokens:
            continue
        store = MarketDataStore(chain, token_to_strike)
        sessions.append(
            ExpirySession(
                spec=ExpirySpec(underlying=UNDERLYING, expiry=expiry),
                config=config,
                chain=chain,
                store=store,
                analytics=AnalyticsEngine(config.market, config.model, clock=current_time),
                tokens=tokens,
            )
        )
    if not sessions:
        raise ValueError("No NIFTY option sessions could be built from the CSV.")
    return sessions


def build_backtest_config(workbook_path: Path, expiry: date, trade_date: date):
    previous = os.environ.get("HOLIDAYS_FILE")
    os.environ["HOLIDAYS_FILE"] = str(workbook_path)
    try:
        config = build_app_config(UNDERLYING)
    finally:
        if previous is None:
            os.environ.pop("HOLIDAYS_FILE", None)
        else:
            os.environ["HOLIDAYS_FILE"] = previous

    try:
        full_days = load_full_days_for_expiry(workbook_path, expiry, UNDERLYING)
    except (FileNotFoundError, ValueError):
        full_days = business_days_until_expiry(trade_date, expiry)

    try:
        model_params = load_model_params(workbook_path, UNDERLYING)
    except (FileNotFoundError, ValueError):
        model_params = {
            "a": config.model.initial_a,
            "bL": config.model.initial_bl,
            "bR": config.model.initial_br,
            "capL": config.model.initial_capl,
            "floorR": config.model.initial_floorr,
        }

    return replace(
        config,
        api=ApiConfig(api_key="", access_token=""),
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


def instruments_for_expiry(dataset: OptionDataset, expiry: date) -> pd.DataFrame:
    rows = dataset.frame[dataset.frame["expiry"] == expiry][
        ["ticker", "expiry", "strike", "option_type"]
    ].drop_duplicates()
    rows = rows.sort_values(["strike", "option_type", "ticker"]).reset_index(drop=True)
    rows["instrument_token"] = rows.index + token_base(expiry)
    rows["name"] = UNDERLYING
    rows["tradingsymbol"] = rows["ticker"].str.removesuffix(".NFO")
    rows["instrument_type"] = rows["option_type"]
    return rows[
        [
            "instrument_token",
            "tradingsymbol",
            "name",
            "expiry",
            "strike",
            "instrument_type",
        ]
    ]


def token_base(expiry: date) -> int:
    return int(expiry.strftime("%y%m%d")) * 100000


def business_days_until_expiry(trade_date: date, expiry: date) -> int:
    if expiry <= trade_date:
        return 0

    days = 0
    current = trade_date + timedelta(days=1)
    while current <= expiry:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def localized_datetime(value: pd.Timestamp) -> datetime:
    if value.tzinfo is None:
        return value.to_pydatetime().replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return value.to_pydatetime()
