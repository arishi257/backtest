from __future__ import annotations

from dataclasses import dataclass


OptionType = str


@dataclass
class OptionQuote:
    token: int
    bid: float | None = None
    ask: float | None = None


@dataclass
class ChainRow:
    strike: int
    ce: OptionQuote | None = None
    pe: OptionQuote | None = None


@dataclass(frozen=True)
class LatestQuote:
    ce_bid: float
    ce_ask: float
    pe_bid: float
    pe_ask: float


@dataclass
class AnalyticsRow:
    strike: int
    ce_bid: float
    ce_ask: float
    pe_bid: float
    pe_ask: float
    iv_bid: float | str = ""
    iv_ask: float | str = ""
    iv_mid: float | str = ""
    normalized_strike: float | None = None
    slope: float | str = ""
    model_iv: float | None = None


@dataclass
class AnalyticsResult:
    user_value: int
    time: float
    full_days: float
    fraction_days: float
    intraday: float
    intraday_var: float
    best_bid: float
    best_ask: float
    universal_mid: float
    universal_spot: float
    atm_vol: float
    fitted_params: list[float]
    fit_error: float
    rows: list[AnalyticsRow]
    market_ns: list[float]
    market_iv: list[float]
    market_vega: list[float]
    model_vols: list[float]
    model_errors: list[float]
    slope_x: list[float]
    slope_y: list[float]
