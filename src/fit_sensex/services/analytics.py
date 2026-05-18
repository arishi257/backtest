from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from math import log, sqrt
from zoneinfo import ZoneInfo

from fit_sensex.config import MarketConfig, ModelConfig
from fit_sensex.models import AnalyticsResult, AnalyticsRow, LatestQuote
from fit_sensex.pricing.black_scholes import (
    black_scholes_vega,
    implied_volatility,
)
from fit_sensex.pricing.vol_curve import ParametricVolCurve


class AnalyticsEngine:
    def __init__(
        self,
        market_config: MarketConfig,
        model_config: ModelConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_config = market_config
        self.model_config = model_config
        self.user_value = market_config.user_value
        self.atm_vol = market_config.atm_vol
        self.vol_curve = ParametricVolCurve(model_config.initial_params)
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))

    def calculate(self, latest_values: dict[int, LatestQuote]) -> AnalyticsResult | None:
        if not latest_values:
            return None

        intraday = self._intraday_remaining(self.clock())
        fraction_days = intraday * self.market_config.intraday_var
        time = (self.market_config.full_days + fraction_days) / (
            self.market_config.calendar_days
        )
        funding_factor = math.exp(time * self.market_config.funding_rate)
        discount_factor = math.exp(self.market_config.risk_free_rate * time)

        best_bid, best_ask, rows = self._synthetic_quotes(
            latest_values, funding_factor
        )
        if best_bid is None or best_ask is None:
            return None

        universal_mid = (best_bid + best_ask) / 2
        self.user_value = (
            round(universal_mid / self.market_config.strike_round_base)
            * self.market_config.strike_round_base
        )
        universal_spot = universal_mid / discount_factor

        self._fill_iv(rows, universal_spot, time)
        self._interpolate_atm_vol(rows, universal_mid, time)
        self._fill_normalized_strikes(rows, universal_mid, time)
        rows.sort(
            key=lambda row: (
                row.normalized_strike if row.normalized_strike is not None else 999
            )
        )

        slope_x, slope_y = self._fill_market_slopes(rows)
        market_ns, market_iv, market_vega = self._market_fit_inputs(
            rows, universal_spot, time
        )
        fitted_params, model_vols, fit_error, model_errors = self._fit_curve(
            market_ns, market_iv, market_vega
        )
        self._fill_model_iv(rows, market_ns, model_vols)

        return AnalyticsResult(
            user_value=self.user_value,
            time=time,
            full_days=self.market_config.full_days,
            fraction_days=fraction_days,
            intraday=intraday,
            intraday_var=self.market_config.intraday_var,
            best_bid=best_bid,
            best_ask=best_ask,
            universal_mid=universal_mid,
            universal_spot=universal_spot,
            atm_vol=self.atm_vol,
            fitted_params=fitted_params,
            fit_error=fit_error,
            rows=rows,
            market_ns=market_ns,
            market_iv=market_iv,
            market_vega=market_vega,
            model_vols=model_vols,
            model_errors=model_errors,
            slope_x=slope_x,
            slope_y=slope_y,
        )

    @staticmethod
    def _intraday_remaining(ist_curr: datetime) -> float:
        if ist_curr.tzinfo is None:
            ist_curr = ist_curr.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        start_time = ist_curr.replace(hour=9, minute=15, second=0, microsecond=0)
        end_time = ist_curr.replace(hour=15, minute=25, second=0, microsecond=0)
        total_session_seconds = (end_time - start_time).total_seconds()
        remaining_seconds = (end_time - ist_curr).total_seconds()
        return max(0.0, min(1.0, remaining_seconds / total_session_seconds))

    def _synthetic_quotes(
        self,
        latest_values: dict[int, LatestQuote],
        funding_factor: float,
    ) -> tuple[float | None, float | None, list[AnalyticsRow]]:
        best_bid = None
        best_ask = None
        rows: list[AnalyticsRow] = []

        for strike, quote in latest_values.items():
            synth_bid = (
                strike
                + (quote.ce_bid - quote.pe_ask) * funding_factor
                - self.market_config.brokerage_rate * (quote.ce_bid + quote.pe_ask)
            )
            synth_ask = (
                strike
                + (quote.ce_ask - quote.pe_bid) * funding_factor
                + self.market_config.brokerage_rate * (quote.ce_ask + quote.pe_bid)
            )

            if abs(strike - self.user_value) <= self.market_config.synthetic_search_width:
                best_bid = synth_bid if best_bid is None else max(best_bid, synth_bid)
                best_ask = synth_ask if best_ask is None else min(best_ask, synth_ask)

            rows.append(
                AnalyticsRow(
                    strike=strike,
                    ce_bid=quote.ce_bid,
                    ce_ask=quote.ce_ask,
                    pe_bid=quote.pe_bid,
                    pe_ask=quote.pe_ask,
                )
            )

        return best_bid, best_ask, rows

    def _fill_iv(
        self,
        rows: list[AnalyticsRow],
        universal_spot: float,
        time: float,
    ) -> None:
        for row in rows:
            try:
                if row.strike > self.user_value:
                    iv_bid = implied_volatility(
                        row.ce_bid,
                        universal_spot,
                        row.strike,
                        time,
                        self.market_config.risk_free_rate,
                        "CE",
                    )
                    iv_ask = implied_volatility(
                        row.ce_ask,
                        universal_spot,
                        row.strike,
                        time,
                        self.market_config.risk_free_rate,
                        "CE",
                    )
                else:
                    iv_bid = implied_volatility(
                        row.pe_bid,
                        universal_spot,
                        row.strike,
                        time,
                        self.market_config.risk_free_rate,
                        "PE",
                    )
                    iv_ask = implied_volatility(
                        row.pe_ask,
                        universal_spot,
                        row.strike,
                        time,
                        self.market_config.risk_free_rate,
                        "PE",
                    )

                if iv_bid is not None and iv_ask is not None:
                    row.iv_bid = round(iv_bid * 100, 2)
                    row.iv_ask = round(iv_ask * 100, 2)
                    row.iv_mid = round((iv_bid + iv_ask) * 50, 2)
            except (ValueError, ZeroDivisionError, OverflowError):
                row.iv_bid = ""
                row.iv_ask = ""
                row.iv_mid = ""

    def _interpolate_atm_vol(
        self,
        rows: list[AnalyticsRow],
        universal_mid: float,
        time: float,
    ) -> None:
        temp_points: list[tuple[float, float]] = []
        for row in rows:
            if row.iv_mid == "":
                continue
            try:
                temp_norm = log(row.strike / universal_mid) / (
                    sqrt(time) * self.atm_vol
                )
                temp_points.append((temp_norm, float(row.iv_mid)))
            except (ValueError, ZeroDivisionError):
                continue

        negative_point = max(
            (point for point in temp_points if point[0] < 0),
            key=lambda point: point[0],
            default=None,
        )
        positive_point = min(
            (point for point in temp_points if point[0] > 0),
            key=lambda point: point[0],
            default=None,
        )

        if negative_point is None or positive_point is None:
            return

        x1, y1 = negative_point
        x2, y2 = positive_point
        self.atm_vol = (y1 + ((0 - x1) / (x2 - x1)) * (y2 - y1)) / 100

    def _fill_normalized_strikes(
        self,
        rows: list[AnalyticsRow],
        universal_mid: float,
        time: float,
    ) -> None:
        for row in rows:
            try:
                ns = log(row.strike / universal_mid) / (sqrt(time) * self.atm_vol)
                row.normalized_strike = round(ns, 2)
            except (ValueError, ZeroDivisionError):
                row.normalized_strike = None

    @staticmethod
    def _fill_market_slopes(rows: list[AnalyticsRow]) -> tuple[list[float], list[float]]:
        plot_x: list[float] = []
        plot_y: list[float] = []

        for i, row in enumerate(rows):
            try:
                if i == 0 or i == len(rows) - 1:
                    continue
                left = rows[i - 1]
                right = rows[i + 1]
                x1 = left.normalized_strike
                x2 = right.normalized_strike
                y1 = left.iv_mid
                y2 = right.iv_mid
                if x1 is None or x2 is None or y1 == "" or y2 == "":
                    continue
                row.slope = round(-1 * ((float(y2) - float(y1)) / (x2 - x1)) * 0.1, 2)
                plot_x.append(row.normalized_strike)
                plot_y.append(float(row.slope))
            except (ValueError, ZeroDivisionError, TypeError):
                continue

        return plot_x, plot_y

    def _market_fit_inputs(
        self,
        rows: list[AnalyticsRow],
        universal_spot: float,
        time: float,
    ) -> tuple[list[float], list[float], list[float]]:
        market_ns: list[float] = []
        market_iv: list[float] = []
        market_vega: list[float] = []

        for row in rows:
            if row.normalized_strike is None or row.iv_mid == "":
                continue
            iv = float(row.iv_mid)
            vega = black_scholes_vega(
                universal_spot,
                row.strike,
                time,
                self.market_config.risk_free_rate,
                iv / 100,
            )
            market_ns.append(row.normalized_strike)
            market_iv.append(iv)
            market_vega.append(vega)

        return market_ns, market_iv, market_vega

    def _fit_curve(
        self,
        market_ns: list[float],
        market_iv: list[float],
        market_vega: list[float],
    ) -> tuple[list[float], list[float], float, list[float]]:
        fitted_params = self.model_config.initial_params
        model_vols: list[float] = []
        fit_error = 0.0
        model_errors: list[float] = []

        if len(market_ns) > 5:
            fitted_params, model_vols, fit_error = self.vol_curve.fit(
                market_ns,
                market_iv,
                market_vega,
                self.atm_vol * 100,
            )
            model_errors = [
                market_iv[i] - model_vols[i] for i in range(len(model_vols))
            ]

        return fitted_params, model_vols, fit_error, model_errors

    @staticmethod
    def _fill_model_iv(
        rows: list[AnalyticsRow],
        market_ns: list[float],
        model_vols: list[float],
    ) -> None:
        if len(market_ns) != len(model_vols):
            return

        model_by_ns = dict(zip(market_ns, model_vols))
        for row in rows:
            if row.normalized_strike is None:
                continue
            row.model_iv = model_by_ns.get(row.normalized_strike)
