from __future__ import annotations

from dataclasses import dataclass

from fit_sensex.config import MarketConfig
from fit_sensex.models import AnalyticsResult, AnalyticsRow
from fit_sensex.services.risk import PortfolioPosition, RiskEngine, RiskRow
from vol_dashboard.models import ExpirySession


SHORT_LOTS = -20
LONG_LOTS = 60
HEDGE_DISTANCE = 300
SENSEX_SHORT_LOTS = -12
SENSEX_SHORT_COUNT = 5
SENSEX_LONG_LOTS = 60
SENSEX_WING_DISTANCE = 900


@dataclass
class SamplePortfolioRisk:
    session: ExpirySession
    positions: list[PortfolioPosition]
    risk_engine: "StaticPortfolioRiskEngine"

    def calculate(self, result: AnalyticsResult) -> list[RiskRow]:
        return self.risk_engine.calculate_positions(self.positions, result)


class StaticPortfolioRiskEngine(RiskEngine):
    def __init__(self, market_config: MarketConfig) -> None:
        super().__init__(market_config)

    def calculate_positions(
        self,
        positions: list[PortfolioPosition],
        result: AnalyticsResult,
    ) -> list[RiskRow]:
        rows_by_strike = {row.strike: row for row in result.rows}
        risk_rows: list[RiskRow] = []
        for position in positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None or market_row.model_iv is None:
                risk_rows.append(self._blank_row(position))
                continue
            risk_rows.append(self._calculate_position(position, market_row, result))
        return risk_rows


def build_sample_portfolio(
    session: ExpirySession,
    result: AnalyticsResult,
) -> SamplePortfolioRisk | None:
    if session.spec.underlying == "SENSEX":
        return build_sensex_portfolio(session, result)
    return build_nifty_portfolio(session, result)


def build_nifty_portfolio(
    session: ExpirySession,
    result: AnalyticsResult,
) -> SamplePortfolioRisk | None:
    strikes = sorted(row.strike for row in result.rows if row_has_complete_risk(row))
    put_shorts = nearest_downside_strikes(strikes, result.universal_mid, 3)
    call_shorts = nearest_upside_strikes(strikes, result.universal_mid, 3)
    if len(put_shorts) < 3 or len(call_shorts) < 3:
        return None

    long_put = nearest_strike(strikes, min(put_shorts) - HEDGE_DISTANCE)
    long_call = nearest_strike(strikes, max(call_shorts) + HEDGE_DISTANCE)
    maturity = session.spec.expiry.strftime("%d-%b-%y")
    positions = [
        build_position(session, maturity, strike, "PE", SHORT_LOTS)
        for strike in put_shorts
    ]
    positions.append(build_position(session, maturity, long_put, "PE", LONG_LOTS))
    positions.extend(
        build_position(session, maturity, strike, "CE", SHORT_LOTS)
        for strike in call_shorts
    )
    positions.append(build_position(session, maturity, long_call, "CE", LONG_LOTS))
    positions.sort(key=lambda position: (position.strike, position.option_type))

    return SamplePortfolioRisk(
        session=session,
        positions=positions,
        risk_engine=StaticPortfolioRiskEngine(session.config.market),
    )


def build_sensex_portfolio(
    session: ExpirySession,
    result: AnalyticsResult,
) -> SamplePortfolioRisk | None:
    strikes = sorted(row.strike for row in result.rows if row_has_complete_risk(row))
    put_otm = nearest_downside_strikes(
        strikes,
        result.universal_mid,
        SENSEX_SHORT_COUNT,
    )
    call_otm = nearest_upside_strikes(
        strikes,
        result.universal_mid,
        SENSEX_SHORT_COUNT,
    )
    if len(put_otm) < SENSEX_SHORT_COUNT or len(call_otm) < SENSEX_SHORT_COUNT:
        return None

    long_put = nearest_strike(strikes, min(put_otm) - SENSEX_WING_DISTANCE)
    long_call = nearest_strike(strikes, max(call_otm) + SENSEX_WING_DISTANCE)
    maturity = session.spec.expiry.strftime("%d-%b-%y")
    positions = [
        build_position(session, maturity, strike, "PE", SENSEX_SHORT_LOTS)
        for strike in put_otm
    ]
    positions.extend(
        build_position(session, maturity, strike, "CE", SENSEX_SHORT_LOTS)
        for strike in call_otm
    )
    positions.append(
        build_position(session, maturity, long_put, "PE", SENSEX_LONG_LOTS)
    )
    positions.append(
        build_position(session, maturity, long_call, "CE", SENSEX_LONG_LOTS)
    )
    positions.sort(key=lambda position: (position.strike, position.option_type))

    return SamplePortfolioRisk(
        session=session,
        positions=positions,
        risk_engine=StaticPortfolioRiskEngine(session.config.market),
    )


def row_has_complete_risk(row: AnalyticsRow) -> bool:
    return row.model_iv is not None and row.iv_mid != ""


def nearest_downside_strikes(strikes: list[int], value: float, count: int) -> list[int]:
    return sorted([strike for strike in strikes if strike < value], reverse=True)[:count]


def nearest_upside_strikes(strikes: list[int], value: float, count: int) -> list[int]:
    return sorted(strike for strike in strikes if strike > value)[:count]


def nearest_strike(strikes: list[int], target: int) -> int:
    return min(strikes, key=lambda strike: (abs(strike - target), strike))


def build_position(
    session: ExpirySession,
    maturity: str,
    strike: int,
    option_type: str,
    lots: int,
) -> PortfolioPosition:
    multiplier = multiplier_for(session.spec.underlying)
    return PortfolioPosition(
        book="options",
        lots=lots,
        underlying=session.spec.underlying,
        maturity=maturity,
        strike=strike,
        option_type=option_type,
        qty=lots * multiplier,
        mult=multiplier,
    )


def multiplier_for(underlying: str) -> int:
    return 20 if underlying.strip().upper() == "SENSEX" else 65
