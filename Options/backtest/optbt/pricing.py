#!/usr/bin/env python3
"""Black-Scholes pricing + greeks, vectorised where it matters."""

from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist().cdf
_n = NormalDist().pdf


def _d1d2(S, K, T, r, sig, q=0.0):
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / v
    return d1, d1 - v


def bs_price(kind: str, S: float, K: float, T: float, r: float, sig: float, q: float = 0.0) -> float:
    """European option price. T in years. Intrinsic when T<=0 or sig<=0."""
    call = kind.lower().startswith("c")
    if T <= 0 or sig <= 0 or S <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1, d2 = _d1d2(S, K, T, r, sig, q)
    if call:
        return S * math.exp(-q * T) * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * math.exp(-q * T) * _N(-d1)


def bs_greeks(kind: str, S: float, K: float, T: float, r: float, sig: float, q: float = 0.0) -> dict:
    """delta, gamma, vega (per 1 vol point), theta (per calendar day), rho."""
    call = kind.lower().startswith("c")
    if T <= 0 or sig <= 0 or S <= 0:
        itm = (S > K) if call else (S < K)
        return {"delta": (1.0 if call else -1.0) if itm else 0.0,
                "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
    d1, d2 = _d1d2(S, K, T, r, sig, q)
    sqT = math.sqrt(T)
    disc_r, disc_q = math.exp(-r * T), math.exp(-q * T)
    delta = disc_q * (_N(d1) if call else _N(d1) - 1)
    gamma = disc_q * _n(d1) / (S * sig * sqT)
    vega = S * disc_q * _n(d1) * sqT / 100.0
    theta_yr = (-S * disc_q * _n(d1) * sig / (2 * sqT)
                + (q * S * disc_q * _N(d1) - r * K * disc_r * _N(d2)) * (1 if call else 0)
                + (-q * S * disc_q * _N(-d1) + r * K * disc_r * _N(-d2)) * (0 if call else 1))
    rho = (K * T * disc_r * (_N(d2) if call else -_N(-d2))) / 100.0
    return {"delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta_yr / 365.0, "rho": rho}


def implied_vol(kind: str, price: float, S: float, K: float, T: float, r: float,
                q: float = 0.0, lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> float:
    """Bisection IV. Returns nan if the price is outside the no-arbitrage bounds."""
    if T <= 0 or price <= 0:
        return float("nan")
    if bs_price(kind, S, K, T, r, hi, q) < price:
        return float("nan")
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(kind, S, K, T, r, mid, q) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def strike_for_delta(kind: str, target_delta: float, S: float, T: float, r: float,
                     sig: float, q: float = 0.0) -> float:
    """Strike whose delta equals `target_delta` — the sane way to define 'how far OTM'
    when comparing across names with very different vol (an ASX 20-delta call and an
    NBIS 20-delta call are the same bet; '20% OTM' on both is not)."""
    lo, hi = S * 0.05, S * 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        d = abs(bs_greeks(kind, S, mid, T, r, sig, q)["delta"])
        if d > target_delta:      # strike too low -> too much delta
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return 0.5 * (lo + hi)
