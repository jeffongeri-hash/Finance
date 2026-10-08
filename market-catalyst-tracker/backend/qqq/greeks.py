"""
Black-Scholes-Merton pricing and Greeks (European, continuous dividend yield).

QQQ options are American-style. For short-dated OTM puts/calls with a small
dividend yield the European approximation is close, but values computed here
are always labelled `model_from_quote_iv` so they are never confused with
vendor Greeks. They are only ever computed from a quote's *own* contemporaneous
implied volatility — never from present-day data applied to historical quotes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

_SQRT_2PI = math.sqrt(2 * math.pi)


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1 vol point (0.01)


def bsm(spot: float, strike: float, t_years: float, vol: float,
        rate: float, div: float, right: str) -> Greeks:
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        raise ValueError("spot, strike, time and vol must be positive")
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_r = math.exp(-rate * t_years)
    disc_q = math.exp(-div * t_years)
    gamma = disc_q * _n(d1) / (spot * vol * sqrt_t)
    vega = spot * disc_q * _n(d1) * sqrt_t / 100.0
    common = -spot * disc_q * _n(d1) * vol / (2 * sqrt_t)
    if right == "C":
        price = spot * disc_q * _N(d1) - strike * disc_r * _N(d2)
        delta = disc_q * _N(d1)
        theta = common - rate * strike * disc_r * _N(d2) + div * spot * disc_q * _N(d1)
    elif right == "P":
        price = strike * disc_r * _N(-d2) - spot * disc_q * _N(-d1)
        delta = -disc_q * _N(-d1)
        theta = common + rate * strike * disc_r * _N(-d2) - div * spot * disc_q * _N(-d1)
    else:
        raise ValueError("right must be 'C' or 'P'")
    return Greeks(price=price, delta=delta, gamma=gamma, theta=theta / 365.0, vega=vega)


def implied_vol(price: float, spot: float, strike: float, t_years: float,
                rate: float, div: float, right: str,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> Optional[float]:
    """Bisection IV. Returns None if the price is outside no-arbitrage bounds."""
    if price <= 0 or t_years <= 0:
        return None
    try:
        p_lo = bsm(spot, strike, t_years, lo, rate, div, right).price
        p_hi = bsm(spot, strike, t_years, hi, rate, div, right).price
    except ValueError:
        return None
    if not (p_lo <= price <= p_hi):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        p = bsm(spot, strike, t_years, mid, rate, div, right).price
        if abs(p - price) < tol:
            return mid
        if p < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
