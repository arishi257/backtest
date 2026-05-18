from __future__ import annotations

import math
from math import erf, exp, log, sqrt


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return (1.0 / sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)


def black_scholes_price(
    spot: float,
    strike: float,
    time: float,
    rate: float,
    vol: float,
    option_type: str,
) -> float:
    if vol <= 0 or time <= 0:
        return 0.0

    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * time) / (
        vol * sqrt(time)
    )
    d2 = d1 - vol * sqrt(time)

    if option_type == "CE":
        return spot * norm_cdf(d1) - strike * exp(-rate * time) * norm_cdf(d2)

    return strike * exp(-rate * time) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def black_scholes_delta(
    spot: float,
    strike: float,
    time: float,
    rate: float,
    vol: float,
    option_type: str,
) -> float:
    if vol <= 0 or time <= 0:
        return 0.0

    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * time) / (
        vol * sqrt(time)
    )
    if option_type == "CE":
        return norm_cdf(d1)
    return norm_cdf(d1) - 1


def black_scholes_gamma(
    spot: float,
    strike: float,
    time: float,
    rate: float,
    vol: float,
) -> float:
    if vol <= 0 or time <= 0 or spot <= 0:
        return 0.0

    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * time) / (
        vol * sqrt(time)
    )
    return norm_pdf(d1) / (spot * vol * sqrt(time))


def black_scholes_vega(
    spot: float,
    strike: float,
    time: float,
    rate: float,
    vol: float,
) -> float:
    if vol <= 0 or time <= 0:
        return 0.0

    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * time) / (
        vol * sqrt(time)
    )
    return spot * norm_pdf(d1) * sqrt(time)


def black_scholes_theta_price_change(
    spot: float,
    strike: float,
    time: float,
    time_step: float,
    rate: float,
    vol: float,
    option_type: str,
) -> float:
    if vol <= 0 or time <= 0 or time_step <= 0:
        return 0.0

    current_price = black_scholes_price(spot, strike, time, rate, vol, option_type)
    next_time = max(time - time_step, 0.000001)
    rolled_price = black_scholes_price(spot, strike, next_time, rate, vol, option_type)
    return rolled_price - current_price


def implied_volatility(
    market_price: float | None,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    option_type: str,
    low: float = 0.0001,
    high: float = 5.0,
    iterations: int = 60,
) -> float | None:
    if market_price is None or market_price <= 0 or spot <= 0 or time <= 0:
        return None

    mid = low
    for _ in range(iterations):
        mid = (low + high) / 2
        model_price = black_scholes_price(
            spot, strike, time, rate, mid, option_type
        )
        if model_price > market_price:
            high = mid
        else:
            low = mid

    return mid
