from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from fit_sensex.services.expiry_calendar import load_variables, workbook_has_sheet


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CREDENTIALS_FILE = PROJECT_ROOT / "credentials.ini"
DEFAULT_HOLIDAYS_FILE = PROJECT_ROOT / "hols.xlsx"
SUPPORTED_UNDERLYINGS = ("SENSEX", "NIFTY")

UNDERLYING_DEFAULTS = {
    "SENSEX": {
        "exchange": "BFO",
        "symbol_name": "SENSEX",
        "low_strike": "65000",
        "high_strike": "85000",
        "strike_gap": "100",
        "user_value": "76300",
        "strike_round_base": "100",
        "synthetic_search_width": "300",
    },
    "NIFTY": {
        "exchange": "NFO",
        "symbol_name": "NIFTY",
        "low_strike": "20000",
        "high_strike": "28000",
        "strike_gap": "50",
        "user_value": "24000",
        "strike_round_base": "50",
        "synthetic_search_width": "150",
    },
}


def normalize_underlying(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in SUPPORTED_UNDERLYINGS:
        raise ValueError(
            f"Unsupported underlying {value!r}. Use one of: {', '.join(SUPPORTED_UNDERLYINGS)}."
        )
    return normalized


def _holidays_file() -> Path:
    return Path(os.getenv("HOLIDAYS_FILE", str(DEFAULT_HOLIDAYS_FILE)))


def _read_credentials_file() -> configparser.SectionProxy | dict[str, str]:
    path = Path(os.getenv("KITE_CREDENTIALS_FILE", str(DEFAULT_CREDENTIALS_FILE)))
    if not path.exists():
        return {}

    parser = configparser.ConfigParser()
    parser.read(path)
    return parser["kite"] if parser.has_section("kite") else {}


def _re_match_number(value: str) -> str | None:
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(value))
    return match.group(0) if match else None


def _number_value(value: str, name: str) -> float:
    match = _re_match_number(value)
    if match is None:
        raise ValueError(f"Could not parse numeric variable '{name}' from value {value!r}")
    return float(match)


def _variables_for(underlying: str, workbook_path: Path) -> tuple[dict[str, str], bool]:
    specific_sheet_name = f"variables_{underlying.lower()}"
    has_specific_sheet = workbook_has_sheet(workbook_path, specific_sheet_name)
    if has_specific_sheet:
        return load_variables(workbook_path, underlying), True
    return load_variables(workbook_path, None), False


def _default_for(underlying: str, key: str, fallback: str) -> str:
    return UNDERLYING_DEFAULTS.get(underlying, {}).get(key, fallback)


def _variable(
    variables: dict[str, str],
    name: str,
    env_name: str,
    default: str,
) -> str:
    return os.getenv(env_name) or variables.get(name, default)


def _number_variable(
    variables: dict[str, str],
    name: str,
    env_name: str,
    default: str,
) -> float:
    return _number_value(_variable(variables, name, env_name, default), name)


@dataclass(frozen=True)
class ApiConfig:
    api_key: str
    access_token: str

    def validate(self) -> None:
        if not self.api_key or not self.access_token:
            raise ValueError(
                "Set KITE_API_KEY and KITE_ACCESS_TOKEN before starting the live app."
            )


@dataclass(frozen=True)
class StrikeConfig:
    low: int
    high: int
    gap: int


@dataclass(frozen=True)
class MarketConfig:
    underlying: str
    user_value: int
    calendar_days: float
    full_days: float
    intraday_var: float
    funding_rate: float
    brokerage_rate: float
    risk_free_rate: float
    atm_vol: float
    refresh_ms: int
    exchange: str
    symbol_name: str
    holidays_file: Path
    portfolio_file: Path
    strike_round_base: int
    synthetic_search_width: int
    delta_hedge_threshold_ratio: float


@dataclass(frozen=True)
class ModelConfig:
    initial_a: float
    initial_bl: float
    initial_br: float
    initial_capl: float
    initial_floorr: float

    @property
    def initial_params(self) -> list[float]:
        return [
            self.initial_a,
            self.initial_bl,
            self.initial_br,
            self.initial_capl,
            self.initial_floorr,
        ]


@dataclass(frozen=True)
class AppConfig:
    api: ApiConfig
    market: MarketConfig
    model: ModelConfig
    strikes: StrikeConfig
    workbook_variables: dict[str, str] = field(default_factory=dict)


def build_api_config() -> ApiConfig:
    credentials = _read_credentials_file()
    return ApiConfig(
        api_key=os.getenv("KITE_API_KEY") or credentials.get("api_key", ""),
        access_token=os.getenv("KITE_ACCESS_TOKEN") or credentials.get("access_token", ""),
    )


def build_app_config(underlying: str) -> AppConfig:
    normalized_underlying = normalize_underlying(underlying)
    holidays_file = _holidays_file()
    variables, has_specific_variables = _variables_for(normalized_underlying, holidays_file)
    if not has_specific_variables and normalized_underlying != "SENSEX":
        variables = variables.copy()
        for name in (
            "Low Strike",
            "High Strike",
            "Strike Gap",
            "Initial User Value",
            "Kite Exchange",
            "Symbol Name",
            "Strike Round Base",
            "Synthetic Search Width",
        ):
            variables.pop(name, None)

    def scoped_default(name: str, key: str, fallback: str) -> str:
        if has_specific_variables or normalized_underlying == "SENSEX":
            return variables.get(name, _default_for(normalized_underlying, key, fallback))
        return _default_for(normalized_underlying, key, fallback)

    strike_config = StrikeConfig(
        low=int(
            _number_variable(
                variables,
                "Low Strike",
                "LOW_STRIKE",
                scoped_default("Low Strike", "low_strike", "65000"),
            )
        ),
        high=int(
            _number_variable(
                variables,
                "High Strike",
                "HIGH_STRIKE",
                scoped_default("High Strike", "high_strike", "85000"),
            )
        ),
        gap=int(
            _number_variable(
                variables,
                "Strike Gap",
                "STRIKE_GAP",
                scoped_default("Strike Gap", "strike_gap", "100"),
            )
        ),
    )

    market_config = MarketConfig(
        underlying=normalized_underlying,
        user_value=int(
            _number_variable(
                variables,
                "Initial User Value",
                "USER_VALUE",
                scoped_default("Initial User Value", "user_value", "76300"),
            )
        ),
        calendar_days=_number_variable(
            variables,
            "Calendar Days",
            "CALENDAR_DAYS",
            "255.5",
        ),
        full_days=float(os.getenv("FULL_DAYS", "3")),
        intraday_var=_number_variable(
            variables,
            "Intraday Var",
            "INTRADAY_VAR",
            "0.4",
        ),
        funding_rate=_number_variable(
            variables,
            "Funding Rate",
            "FUNDING_RATE",
            "0.055",
        ),
        brokerage_rate=_number_variable(
            variables,
            "Brokerage Rate",
            "BROKERAGE_RATE",
            "0.0024",
        ),
        risk_free_rate=_number_variable(
            variables,
            "Risk Free Rate",
            "RISK_FREE_RATE",
            "0.055",
        ),
        atm_vol=_number_variable(
            variables,
            "Initial ATM Vol",
            "ATM_VOL",
            "0.176",
        ),
        refresh_ms=int(
            _number_variable(
                variables,
                "GUI Refresh",
                "REFRESH_MS",
                "1000",
            )
        ),
        exchange=_variable(
            variables,
            "Kite Exchange",
            "KITE_EXCHANGE",
            scoped_default("Kite Exchange", "exchange", "BFO"),
        ),
        symbol_name=_variable(
            variables,
            "Symbol Name",
            "SYMBOL_NAME",
            scoped_default("Symbol Name", "symbol_name", normalized_underlying),
        ),
        holidays_file=holidays_file,
        portfolio_file=Path(
            os.getenv(
                "PORTFOLIO_FILE",
                str(PROJECT_ROOT / f"portfolio_{normalized_underlying.lower()}.csv"),
            )
        ),
        strike_round_base=int(
            _number_variable(
                variables,
                "Strike Round Base",
                "STRIKE_ROUND_BASE",
                scoped_default("Strike Round Base", "strike_round_base", "100"),
            )
        ),
        synthetic_search_width=int(
            _number_variable(
                variables,
                "Synthetic Search Width",
                "SYNTHETIC_SEARCH_WIDTH",
                scoped_default("Synthetic Search Width", "synthetic_search_width", "300"),
            )
        ),
        delta_hedge_threshold_ratio=_number_variable(
            variables,
            "Delta Hedge Threshold Ratio",
            "DELTA_HEDGE_THRESHOLD_RATIO",
            "0.7",
        ),
    )

    model_config = ModelConfig(
        initial_a=float(os.getenv("INITIAL_A", "0.1527")),
        initial_bl=float(os.getenv("INITIAL_BL", "-0.063")),
        initial_br=float(os.getenv("INITIAL_BR", "-0.157")),
        initial_capl=float(os.getenv("INITIAL_CAPL", "0.485")),
        initial_floorr=float(os.getenv("INITIAL_FLOORR", "-0.50")),
    )

    return AppConfig(
        api=build_api_config(),
        market=market_config,
        model=model_config,
        strikes=strike_config,
        workbook_variables=variables,
    )
