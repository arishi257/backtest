from __future__ import annotations

import threading
from collections.abc import Mapping

import pandas as pd

from vol_dashboard import compat  # noqa: F401
from fit_sensex.config import ApiConfig
from fit_sensex.market.instruments import fetch_instruments


SPOT_INSTRUMENTS = {
    "NIFTY": {
        "exchange": "NSE",
        "candidates": ("NIFTY 50", "NIFTY"),
    },
    "SENSEX": {
        "exchange": "BSE",
        "candidates": ("SENSEX",),
    },
}


class SpotStore:
    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._lock = threading.Lock()

    def update_tick(self, underlying: str, tick: Mapping) -> None:
        price = tick_last_price(tick)
        if price is None:
            return
        with self._lock:
            self._prices[underlying] = price

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)


def build_spot_tokens(api: ApiConfig, underlyings: list[str]) -> dict[str, int]:
    tokens: dict[str, int] = {}
    instruments_by_exchange: dict[str, pd.DataFrame] = {}
    for underlying in sorted(set(underlyings)):
        spec = SPOT_INSTRUMENTS.get(underlying)
        if spec is None:
            continue
        exchange = spec["exchange"]
        if exchange not in instruments_by_exchange:
            instruments_by_exchange[exchange] = fetch_instruments(api, exchange)
        tokens[underlying] = find_spot_token(
            instruments_by_exchange[exchange],
            spec["candidates"],
            exchange,
            underlying,
        )
    return tokens


def find_spot_token(
    df_instruments: pd.DataFrame,
    candidates: tuple[str, ...],
    exchange: str,
    underlying: str,
) -> int:
    candidate_values = {candidate.upper() for candidate in candidates}
    for column in ("tradingsymbol", "name"):
        if column not in df_instruments.columns:
            continue
        matches = df_instruments[
            df_instruments[column].astype(str).str.strip().str.upper().isin(candidate_values)
        ]
        if not matches.empty:
            return int(matches.iloc[0]["instrument_token"])

    raise ValueError(
        f"Could not find {underlying} spot instrument on {exchange}. "
        f"Tried: {', '.join(candidates)}."
    )


def tick_last_price(tick: Mapping) -> float | None:
    for field in ("last_price", "ltp"):
        value = tick.get(field)
        if isinstance(value, (int, float)):
            return float(value)
    return None

