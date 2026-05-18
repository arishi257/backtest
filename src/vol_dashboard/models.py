from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from vol_dashboard import compat  # noqa: F401
from fit_sensex.config import AppConfig
from fit_sensex.market.instruments import OptionChain
from fit_sensex.market.kite_stream import MarketDataStore
from fit_sensex.services.analytics import AnalyticsEngine


@dataclass(frozen=True)
class ExpirySpec:
    underlying: str
    expiry: date

    @property
    def tab_name(self) -> str:
        return f"{self.underlying.title()} {self.expiry.strftime('%d-%b-%y')}"


@dataclass
class ExpirySession:
    spec: ExpirySpec
    config: AppConfig
    chain: OptionChain
    store: MarketDataStore
    analytics: AnalyticsEngine
    tokens: list[int]

