from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from backtest.data import FutureBar
from backtest.portfolio import SamplePortfolioRisk, build_sample_portfolio
from fit_sensex.models import AnalyticsResult
from fit_sensex.pricing.black_scholes import (
    black_scholes_delta,
    black_scholes_gamma,
    black_scholes_price,
    black_scholes_theta_price_change,
    black_scholes_vega,
    implied_volatility,
)
from fit_sensex.services.risk import (
    RiskRow,
    intrinsic_value_from_synthetic_mid,
    option_market_prices,
    synthetic_prices,
)
from fit_sensex.ui.app import total_row_numeric_values, weighted_price_total
from vol_dashboard.models import ExpirySession


GAMMA_MOVE_STEP = 0.001


@dataclass
class HeadlessPortfolioMetrics:
    total_pnl: float | None = None
    gamma_l: float | None = None


@dataclass
class ParkGammaMetrics:
    future_open: float | None = None
    future_high: float | None = None
    future_low: float | None = None
    future_close: float | None = None
    future_prev_close: float | None = None
    park_move: float | None = None
    park_gamma_pnl: float | None = None
    park_c2c_gamma_pnl: float | None = None
    park_gamma_pnl_diff: float | None = None
    park_gamma_pnl_diff_total: float | None = None
    gk_move: float | None = None
    gk_gamma_pnl: float | None = None
    gk_gamma_pnl_diff: float | None = None
    gk_gamma_pnl_diff_total: float | None = None


class HeadlessPortfolioState:
    def __init__(
        self,
        session: ExpirySession | None = None,
        portfolio: SamplePortfolioRisk | None = None,
    ) -> None:
        self.session = session
        self.portfolio = portfolio
        self.options_pv_snapshot: float | None = None
        self.hedge_strike: int | None = None
        self.hedge_trades: list[dict[str, float | datetime]] = []
        self.cumulative_hedge_lots = 0.0

    @property
    def positions(self) -> list:
        return [] if self.portfolio is None else self.portfolio.positions

    def update(
        self,
        result: AnalyticsResult,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> HeadlessPortfolioMetrics:
        if self.portfolio is None and is_hedge_start_time(timestamp):
            if self.session is not None:
                self.portfolio = build_sample_portfolio(self.session, result)
        if self.portfolio is None:
            return HeadlessPortfolioMetrics()

        risk_rows = self.portfolio.calculate(result)
        totals = total_row_numeric_values(risk_rows, result)
        options_bs_delta_lots = totals[19] if len(totals) > 19 else None
        gamma_l = totals[20] if len(totals) > 20 else None
        options_pv = weighted_price_total(risk_rows, "mid_mkt")
        total_pnl = self._update_pnl(
            result,
            timestamp,
            options_pv,
            options_bs_delta_lots,
            funding_rate,
            brokerage_rate,
            hedge_threshold,
        )
        return HeadlessPortfolioMetrics(total_pnl=total_pnl, gamma_l=gamma_l)

    def _update_pnl(
        self,
        result: AnalyticsResult,
        timestamp: datetime,
        options_pv: float,
        options_bs_delta_lots,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> float | None:
        if not isinstance(options_bs_delta_lots, (int, float)):
            return None

        rows_by_strike = {row.strike: row for row in result.rows}
        if self.options_pv_snapshot is None and is_hedge_start_time(timestamp):
            self.options_pv_snapshot = options_pv
            self.hedge_strike = nearest_result_strike(result, result.universal_mid)
            self._add_hedge_trade(
                round(-options_bs_delta_lots),
                result,
                rows_by_strike,
                timestamp,
                funding_rate,
                brokerage_rate,
            )
        elif self.options_pv_snapshot is not None and self.hedge_strike is not None:
            combined_delta = options_bs_delta_lots + self.cumulative_hedge_lots
            if abs(combined_delta) > hedge_threshold:
                self._add_hedge_trade(
                    round(-combined_delta),
                    result,
                    rows_by_strike,
                    timestamp,
                    funding_rate,
                    brokerage_rate,
                )

        options_pnl = (
            options_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        hedge_pnl = self._hedge_pnl(result, rows_by_strike, funding_rate, brokerage_rate)
        return (options_pnl or 0.0) + (hedge_pnl or 0.0)

    def _add_hedge_trade(
        self,
        lots_change: float,
        result: AnalyticsResult,
        rows_by_strike: dict,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
    ) -> None:
        if self.hedge_strike is None or abs(lots_change) < 1e-9:
            return
        prices = hedge_prices(
            self.hedge_strike,
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        if prices is None:
            return
        synth_bid, synth_ask, _ = prices
        trade_price = synth_ask if lots_change > 0 else synth_bid
        self.hedge_trades.append(
            {
                "timestamp": timestamp,
                "lots_change": lots_change,
                "trade_price": trade_price,
            }
        )
        self.cumulative_hedge_lots += lots_change

    def _hedge_pnl(
        self,
        result: AnalyticsResult,
        rows_by_strike: dict,
        funding_rate: float,
        brokerage_rate: float,
    ) -> float | None:
        if self.hedge_strike is None or not self.hedge_trades:
            return None
        prices = hedge_prices(
            self.hedge_strike,
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        if prices is None:
            return None
        _, _, synth_mid = prices
        multiplier = hedge_multiplier(self.positions)
        pnl = 0.0
        for trade in self.hedge_trades:
            pnl += float(trade["lots_change"]) * (
                synth_mid - float(trade["trade_price"])
            ) * multiplier / 1000
        return pnl


class HeadlessFrozenIvState(HeadlessPortfolioState):
    def __init__(self, session: ExpirySession) -> None:
        super().__init__(session=session)
        self.session = session
        self.frozen_ivs: dict[tuple[int, str], float] = {}

    def update(
        self,
        result: AnalyticsResult,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> HeadlessPortfolioMetrics:
        if not self.frozen_ivs:
            if not is_hedge_start_time(timestamp):
                return HeadlessPortfolioMetrics()
            portfolio = build_sample_portfolio(self.session, result)
            if portfolio is None:
                return HeadlessPortfolioMetrics()
            frozen_ivs = self._capture_position_ivs(
                portfolio.positions,
                result,
                funding_rate,
            )
            if len(frozen_ivs) != len(portfolio.positions):
                return HeadlessPortfolioMetrics()
            self.portfolio = portfolio
            self.frozen_ivs = frozen_ivs

        risk_rows = self._frozen_risk_rows(result, funding_rate)
        totals = total_row_numeric_values(risk_rows, result)
        options_bs_delta_lots = totals[19] if len(totals) > 19 else None
        options_pv = weighted_price_total(risk_rows, "mid_mkt")
        total_pnl = self._update_pnl(
            result,
            timestamp,
            options_pv,
            options_bs_delta_lots,
            funding_rate,
            brokerage_rate,
            hedge_threshold,
        )
        return HeadlessPortfolioMetrics(total_pnl=total_pnl)

    def _capture_position_ivs(
        self,
        positions: list,
        result: AnalyticsResult,
        funding_rate: float,
    ) -> dict[tuple[int, str], float]:
        rows_by_strike = {row.strike: row for row in result.rows}
        frozen_ivs: dict[tuple[int, str], float] = {}
        for position in positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None:
                continue
            bid_mkt, ask_mkt = option_market_prices(market_row, position.option_type)
            mid_mkt = (bid_mkt + ask_mkt) / 2
            try:
                iv = implied_volatility(
                    mid_mkt,
                    result.universal_spot,
                    position.strike,
                    result.time,
                    funding_rate,
                    position.option_type,
                )
            except (ValueError, ZeroDivisionError, OverflowError):
                iv = None
            if iv is not None:
                frozen_ivs[position_key(position)] = iv
        return frozen_ivs

    def _frozen_risk_rows(
        self,
        result: AnalyticsResult,
        funding_rate: float,
    ) -> list[RiskRow]:
        rows_by_strike = {row.strike: row for row in result.rows}
        risk_rows = []
        for position in self.positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None or position_key(position) not in self.frozen_ivs:
                risk_rows.append(blank_frozen_row(position))
                continue
            risk_rows.append(
                frozen_position_row(
                    position,
                    result,
                    funding_rate,
                    self.frozen_ivs[position_key(position)],
                )
            )
        return risk_rows


class GammaDiffTracker:
    def __init__(self) -> None:
        self.universal_mid_points: list[tuple[datetime, float]] = []
        self.gamma_l_points: list[tuple[datetime, float]] = []

    def update(
        self,
        timestamp: datetime,
        universal_mid: float | None,
        gamma_l: float | None,
    ) -> float | None:
        record_metric_point(self.universal_mid_points, timestamp, universal_mid)
        record_metric_point(self.gamma_l_points, timestamp, gamma_l)
        return self.total()

    def total(self) -> float | None:
        gamma_by_timestamp = {
            timestamp: gamma_l for timestamp, gamma_l in self.gamma_l_points
        }
        rows = []
        for (previous_time, previous_mid), (timestamp, mid) in zip(
            self.universal_mid_points,
            self.universal_mid_points[1:],
        ):
            if not is_top_move_time(timestamp) or previous_mid == 0:
                continue
            move_return = mid / previous_mid - 1
            if abs(move_return) <= GAMMA_MOVE_STEP:
                continue
            gamma_l = gamma_by_timestamp.get(previous_time)
            if gamma_l is None:
                continue
            gamma_pnl = gamma_pnl_for_move(gamma_l, move_return)
            capped_gamma_pnl = capped_gamma_pnl_for_move(gamma_l, move_return)
            rows.append(capped_gamma_pnl - gamma_pnl)
        return sum(rows) if rows else None


class ParkGammaTracker:
    def __init__(self) -> None:
        self.previous_bar: FutureBar | None = None
        self.previous_gamma_l: float | None = None
        self.park_gamma_pnl_diff_total = 0.0
        self.gk_gamma_pnl_diff_total = 0.0
        self.has_values = False

    def update(
        self,
        bar: FutureBar | None,
        gamma_l: float | None,
    ) -> ParkGammaMetrics:
        if bar is None:
            return ParkGammaMetrics(
                park_gamma_pnl_diff_total=(
                    self.park_gamma_pnl_diff_total if self.has_values else None
                ),
                gk_gamma_pnl_diff_total=(
                    self.gk_gamma_pnl_diff_total if self.has_values else None
                ),
            )

        metrics = ParkGammaMetrics(
            future_open=bar.open,
            future_high=bar.high,
            future_low=bar.low,
            future_close=bar.close,
            park_gamma_pnl_diff_total=(
                self.park_gamma_pnl_diff_total if self.has_values else None
            ),
            gk_gamma_pnl_diff_total=(
                self.gk_gamma_pnl_diff_total if self.has_values else None
            ),
        )
        if (
            self.previous_bar is None
            or self.previous_gamma_l is None
            or self.previous_bar.close <= 0
            or bar.close <= 0
        ):
            self.previous_bar = bar
            self.previous_gamma_l = gamma_l
            return metrics

        previous_close = self.previous_bar.close
        metrics.future_prev_close = previous_close
        close_return = bar.close / previous_close - 1
        park_move = parkinson_move(bar.high, bar.low)
        park_gamma_pnl = gamma_pnl_for_move(self.previous_gamma_l, park_move)
        park_c2c_gamma_pnl = gamma_pnl_for_move(self.previous_gamma_l, close_return)
        park_gamma_pnl_diff = park_gamma_pnl - park_c2c_gamma_pnl
        gk_move_value = garman_klass_move(bar.open, bar.high, bar.low, bar.close)
        gk_gamma_pnl = gamma_pnl_for_move(self.previous_gamma_l, gk_move_value)
        gk_gamma_pnl_diff = gk_gamma_pnl - park_c2c_gamma_pnl
        self.park_gamma_pnl_diff_total += park_gamma_pnl_diff
        self.gk_gamma_pnl_diff_total += gk_gamma_pnl_diff
        self.has_values = True
        metrics.park_move = park_move
        metrics.park_gamma_pnl = park_gamma_pnl
        metrics.park_c2c_gamma_pnl = park_c2c_gamma_pnl
        metrics.park_gamma_pnl_diff = park_gamma_pnl_diff
        metrics.park_gamma_pnl_diff_total = self.park_gamma_pnl_diff_total
        metrics.gk_move = gk_move_value
        metrics.gk_gamma_pnl = gk_gamma_pnl
        metrics.gk_gamma_pnl_diff = gk_gamma_pnl_diff
        metrics.gk_gamma_pnl_diff_total = self.gk_gamma_pnl_diff_total
        self.previous_bar = bar
        self.previous_gamma_l = gamma_l
        return metrics


def record_metric_point(
    points: list[tuple[datetime, float]],
    timestamp: datetime,
    value: float | None,
) -> None:
    if value is None:
        return
    if points and points[-1][0] == timestamp:
        points[-1] = (timestamp, value)
    else:
        points.append((timestamp, value))


def is_hedge_start_time(timestamp: datetime) -> bool:
    return (timestamp.hour, timestamp.minute) >= (9, 20)


def is_top_move_time(timestamp: datetime) -> bool:
    return (9, 20) <= (timestamp.hour, timestamp.minute) <= (15, 24)


def nearest_result_strike(result: AnalyticsResult, value: float) -> int:
    strikes = [row.strike for row in result.rows]
    return min(strikes, key=lambda strike: (abs(strike - value), strike))


def hedge_prices(
    strike: int,
    result: AnalyticsResult,
    rows_by_strike: dict,
    funding_rate: float,
    brokerage_rate: float,
) -> tuple[float, float, float] | None:
    market_row = rows_by_strike.get(strike)
    if market_row is None:
        return None
    synth_bid, synth_ask = synthetic_prices(
        strike=strike,
        ce_bid=market_row.ce_bid,
        ce_ask=market_row.ce_ask,
        pe_bid=market_row.pe_bid,
        pe_ask=market_row.pe_ask,
        funding_rate=funding_rate,
        brokerage_rate=brokerage_rate,
        time=result.time,
    )
    return synth_bid, synth_ask, (synth_bid + synth_ask) / 2


def hedge_multiplier(positions: list) -> float:
    for position in positions:
        if position.mult:
            return float(position.mult)
    return 65.0


def position_key(position) -> tuple[int, str]:
    return (position.strike, position.option_type)


def frozen_position_row(
    position,
    result: AnalyticsResult,
    funding_rate: float,
    frozen_iv: float,
) -> RiskRow:
    spot = result.universal_spot
    time = result.time
    qty = position.qty
    mult = position.mult

    price_fit = black_scholes_price(
        spot,
        position.strike,
        time,
        funding_rate,
        frozen_iv,
        position.option_type,
    )
    intrinsic_value = intrinsic_value_from_synthetic_mid(
        position.option_type,
        result.universal_mid,
        position.strike,
    )
    time_value = price_fit - intrinsic_value
    delta = black_scholes_delta(
        spot,
        position.strike,
        time,
        funding_rate,
        frozen_iv,
        position.option_type,
    )
    gamma = black_scholes_gamma(spot, position.strike, time, funding_rate, frozen_iv)
    vega = black_scholes_vega(spot, position.strike, time, funding_rate, frozen_iv)
    time_unit = (15 / 375) * 0.4 / 255.5
    theta_price_change = black_scholes_theta_price_change(
        spot,
        position.strike,
        time,
        time_unit,
        funding_rate,
        frozen_iv,
        position.option_type,
    )

    bs_delta_pct = delta
    bs_delta_ccy = bs_delta_pct * spot * qty / 100000
    bs_delta_lots = bs_delta_ccy * 100000 / mult / spot if mult and spot else 0
    gamma_ccy_10bps = gamma * spot * spot * 0.01 * qty / 100000 / 10
    gamma_lots_10bps = (
        gamma_ccy_10bps * 100000 / spot / mult if spot and mult else 0
    )
    vega_ccy_10bps = vega / 100 / 10 * qty
    bs_theta_ccy = theta_price_change * qty
    std_1w_vega = (
        vega_ccy_10bps / math.sqrt(time) * math.sqrt(5 / 248)
        if time > 0
        else 0
    )

    return RiskRow(
        book=position.book,
        lots=position.lots,
        underlying=position.underlying,
        maturity=position.maturity,
        strike=position.strike,
        option_type=position.option_type,
        qty=qty,
        mult=mult,
        time_value=time_value,
        price_fit=price_fit,
        bid_mkt=price_fit,
        ask_mkt=price_fit,
        mid_mkt=price_fit,
        model_iv=frozen_iv * 100,
        bs_delta_pct=bs_delta_pct,
        bs_delta_ccy=bs_delta_ccy,
        bs_delta_lots=bs_delta_lots,
        gamma_ccy_10bps=gamma_ccy_10bps,
        gamma_lots_10bps=gamma_lots_10bps,
        vega_ccy_10bps=vega_ccy_10bps,
        bs_theta_ccy=bs_theta_ccy,
        std_1w_vega=std_1w_vega,
    )


def blank_frozen_row(position) -> RiskRow:
    return RiskRow(
        book=position.book,
        lots=position.lots,
        underlying=position.underlying,
        maturity=position.maturity,
        strike=position.strike,
        option_type=position.option_type,
        qty=position.qty,
        mult=position.mult,
        time_value="",
        price_fit="",
        bid_mkt="",
        ask_mkt="",
        mid_mkt="",
        model_iv="",
        bs_delta_pct="",
        bs_delta_ccy="",
        bs_delta_lots="",
        gamma_ccy_10bps="",
        gamma_lots_10bps="",
        vega_ccy_10bps="",
        bs_theta_ccy="",
        std_1w_vega="",
    )


def gamma_pnl_for_move(gamma_l: float, move_return: float) -> float:
    return 0.5 * (gamma_l * 100000 * 10) * move_return * move_return * 100 / 1000


def parkinson_move(high: float, low: float) -> float:
    if high <= 0 or low <= 0:
        return 0.0
    return abs(math.log(high / low)) / math.sqrt(4 * math.log(2))


def garman_klass_move(open_price: float, high: float, low: float, close: float) -> float:
    if open_price <= 0 or high <= 0 or low <= 0 or close <= 0:
        return 0.0
    range_term = 0.5 * math.log(high / low) ** 2
    close_open_term = (2 * math.log(2) - 1) * math.log(close / open_price) ** 2
    variance = max(range_term - close_open_term, 0.0)
    return math.sqrt(variance)


def capped_gamma_pnl_for_move(gamma_l: float, move_return: float) -> float:
    remaining_move = abs(move_return)
    capped_pnl = 0.0
    max_chunk = GAMMA_MOVE_STEP
    while remaining_move > 0:
        chunk = min(max_chunk, remaining_move)
        capped_pnl += gamma_pnl_for_move(gamma_l, chunk)
        remaining_move -= chunk
    return capped_pnl
