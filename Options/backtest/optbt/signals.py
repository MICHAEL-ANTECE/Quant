#!/usr/bin/env python3
"""Entry signals. Each factory returns a callable (i, bars) -> bool.

Signals only ever look at bars up to and including index i — no lookahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def always():
    """Always be in a position (roll continuously). The 'is this strategy viable
    at all' baseline."""
    return lambda i, bars: True


def monthly():
    """Enter on the first trading day of each month."""
    def sig(i, bars):
        if i == 0:
            return False
        return bars.index[i].month != bars.index[i - 1].month
    return sig


def dip(pct: float = 0.10, lookback: int = 20):
    """Enter when price is `pct` below its rolling `lookback`-day high."""
    def sig(i, bars):
        if i < lookback:
            return False
        w = bars["close"].iloc[i - lookback:i + 1]
        return bool(w.iloc[-1] <= w.max() * (1 - pct))
    return sig


def breakout(lookback: int = 60):
    """Enter on a new `lookback`-day closing high (momentum)."""
    def sig(i, bars):
        if i < lookback:
            return False
        w = bars["close"].iloc[i - lookback:i + 1]
        return bool(w.iloc[-1] >= w.max())
    return sig


def sma_cross(fast: int = 20, slow: int = 100):
    """Enter while fast SMA is above slow SMA (trend filter)."""
    def sig(i, bars):
        if i < slow:
            return False
        c = bars["close"]
        return bool(c.iloc[i - fast + 1:i + 1].mean() > c.iloc[i - slow + 1:i + 1].mean())
    return sig


def rsi_below(level: float = 35, period: int = 14):
    """Enter when RSI is oversold."""
    def sig(i, bars):
        if i < period + 1:
            return False
        d = bars["close"].iloc[i - period:i + 1].diff().dropna()
        up, dn = d.clip(lower=0).mean(), -d.clip(upper=0).mean()
        if dn == 0:
            return False
        return bool(100 - 100 / (1 + up / dn) < level)
    return sig


def vol_percentile_below(pct: float = 0.40, lookback: int = 252):
    """Only buy options when realized vol is in the cheap part of its own range —
    the single most useful filter for a long-premium strategy."""
    def sig(i, bars):
        if i < lookback or "rv" not in bars.columns:
            return False
        w = bars["rv"].iloc[i - lookback:i + 1]
        return bool(w.iloc[-1] <= w.quantile(pct))
    return sig


def all_of(*sigs):
    return lambda i, bars: all(s(i, bars) for s in sigs)


def any_of(*sigs):
    return lambda i, bars: any(s(i, bars) for s in sigs)
