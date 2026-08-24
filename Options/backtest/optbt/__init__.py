"""
optbt — a small option-strategy backtester built on your own moomoo data.

    from optbt import get_bars, calibrate_surface, Rule, run, report, signals

    bars = get_bars("NBIS", "2021-01-01")
    surf = calibrate_surface("NBIS", bars)          # fits vrp + smile to today's real chain
    res  = run(bars, surf, Rule(target_delta=0.25, target_dte=90, exit_dte=21))
    report(res)

Data layer is source-agnostic on purpose: replacing the BS proxy with a real
historical option chain (Polygon / Alpha Vantage Premium) means writing one new
`iv()` provider, not touching the engine.
"""

from .data import get_bars, live_snapshot, option_chain, expiry_dates
from .vol import VolSurface, calibrate_surface, blended_rv, yang_zhang, ewma_vol
from .pricing import bs_price, bs_greeks, implied_vol, strike_for_delta
from .engine import Rule, run, report, stats
from . import signals

__all__ = ["get_bars", "live_snapshot", "option_chain", "expiry_dates",
           "VolSurface", "calibrate_surface", "blended_rv", "yang_zhang", "ewma_vol",
           "bs_price", "bs_greeks", "implied_vol", "strike_for_delta",
           "Rule", "run", "report", "stats", "signals"]
