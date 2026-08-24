#!/usr/bin/env python3
"""
The vol model — the part that decides whether a BS-proxy option backtest is
believable or garbage.

Naive approach (what most blog-post backtests do): price historical options with
BS at realized volatility. That systematically UNDERPRICES options, because
implied vol trades at a premium to realized (the variance risk premium), and it
ignores skew — for these AI/semi names an OTM call's IV is nowhere near ATM IV.
Both errors flatter a long-call strategy: you buy too cheap and sell too dear.

What this module does instead:

  1. realized vol from OHLC via Yang-Zhang (uses the whole bar, ~7x more
     efficient than close-to-close, and handles overnight gaps — which matter a
     lot for names that move 27% in a day).
  2. calibrate a variance risk premium `vrp = ATM_IV_today / RV_today` from the
     ticker's REAL option chain in your moomoo terminal.
  3. calibrate a smile `iv(m)/iv_atm` as a quadratic in standardised moneyness
     m = ln(K/S) / (iv_atm * sqrt(T)), again from the real chain.

Historical IV for any (S, K, T, date) is then
     iv = RV(date) * vrp * smile(m)
so the backtest inherits today's *shape* of the surface while its *level*
breathes with realized vol through history. Still a proxy — but a calibrated one,
and it errs in a known direction rather than an unknown one.

Swap this for a real historical chain (Polygon / AV Premium) later and the engine
does not change: it only ever asks `VolSurface.iv(...)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
#  realized vol
# --------------------------------------------------------------------------- #
def yang_zhang(df: pd.DataFrame, window: int = 30) -> pd.Series:
    """Rolling annualised Yang-Zhang realized vol from open/high/low/close."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_c = c.shift(1)

    log_oc = np.log(o / prev_c)          # overnight jump
    log_co = np.log(c / o)               # intraday drift
    log_ho, log_lo = np.log(h / o), np.log(l / o)

    n = window
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    var_o = log_oc.rolling(n).var(ddof=1)
    var_c = log_co.rolling(n).var(ddof=1)
    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)   # Rogers-Satchell
    var_rs = rs.rolling(n).mean()

    return np.sqrt((var_o + k * var_c + (1 - k) * var_rs).clip(lower=0) * TRADING_DAYS)


def ewma_vol(df: pd.DataFrame, halflife: int = 20) -> pd.Series:
    """Close-to-close EWMA vol — reacts faster than a flat window."""
    r = np.log(df["close"]).diff()
    return r.ewm(halflife=halflife).std() * np.sqrt(TRADING_DAYS)


def blended_rv(df: pd.DataFrame, window: int = 30, halflife: int = 20,
               w_fast: float = 0.4) -> pd.Series:
    """Yang-Zhang for stability + EWMA for responsiveness. Implied vol behaves
    like a blend of the two far more than like either alone."""
    return ((1 - w_fast) * yang_zhang(df, window) + w_fast * ewma_vol(df, halflife))


# --------------------------------------------------------------------------- #
#  the surface
# --------------------------------------------------------------------------- #
@dataclass
class VolSurface:
    """iv(S, K, T, rv) = rv * vrp * smile(standardised moneyness), floored/capped."""
    ticker: str
    vrp: float = 1.15                 # IV / RV level ratio
    smile: tuple = (1.0, 0.0, 0.0)    # quadratic coeffs a + b*m + c*m^2  (ratio to ATM)
    term_slope: float = 0.0           # d(iv_ratio)/d(sqrt(T)) — 0 = flat term structure
    iv_floor: float = 0.10
    iv_cap: float = 3.00
    rv_window: int = 63               # the RV window vrp was measured against;
                                      # the engine MUST feed rv from this same window
    calibration: dict = field(default_factory=dict)

    def smile_ratio(self, m: float) -> float:
        a, b, c = self.smile
        return max(a + b * m + c * m * m, 0.35)

    def iv(self, S: float, K: float, T: float, rv: float) -> float:
        if not np.isfinite(rv) or rv <= 0 or T <= 0 or S <= 0:
            return float("nan")
        atm = rv * self.vrp
        m = np.log(K / S) / max(atm * np.sqrt(T), 1e-6)
        iv = atm * self.smile_ratio(m) * (1 + self.term_slope * (np.sqrt(T) - np.sqrt(0.25)))
        return float(np.clip(iv, self.iv_floor, self.iv_cap))

    def describe(self) -> str:
        c = self.calibration
        head = (f"VolSurface[{self.ticker}]  vrp={self.vrp:.2f}  "
                f"smile={self.smile[0]:.2f}{self.smile[1]:+.3f}m{self.smile[2]:+.3f}m²")
        if not c:
            return head + "   (DEFAULTS — not calibrated to a live chain)"
        return (head + f"\n   calibrated {c.get('date')} on {c.get('expiry')} "
                f"({c.get('dte')}DTE, {c.get('n_strikes')} strikes): spot "
                f"{c.get('spot'):.2f}, ATM IV {c.get('atm_iv'):.1%} vs "
                f"RV{c.get('rv_window')} {c.get('rv'):.1%}"
                f"   [RV20 {c.get('rv20', float('nan')):.0%} / "
                f"RV252 {c.get('rv252', float('nan')):.0%}]")


DEFAULT_SURFACE_ARGS = dict(vrp=1.15, smile=(1.0, 0.06, 0.05))


def calibrate_surface(ticker: str, bars: pd.DataFrame, target_dte: int = 90,
                      rv_window: int | None = None) -> VolSurface:
    """Fit vrp + smile from the ticker's live moomoo chain. Falls back to sane
    defaults (and says so) when the chain is unavailable.

    The RV window is matched to the option tenor by default: today's 90-day IV is
    a forecast of ~63 trading days of vol, so comparing it to a 30-day trailing RV
    (which spikes hard after a single 27% day) would mis-set the level. The window
    is stored on the surface so the engine feeds back the *same* estimator."""
    from .data import expiry_dates, option_chain

    if rv_window is None:
        rv_window = int(np.clip(round(target_dte * 252 / 365), 20, 252))
    rv = float(blended_rv(bars, rv_window).iloc[-1])
    spot = float(bars["close"].iloc[-1])

    try:
        exps = expiry_dates(ticker)
        if not exps:
            raise RuntimeError("no listed expiries")
        today = pd.Timestamp.today().normalize()
        dtes = {e: (pd.Timestamp(e) - today).days for e in exps}
        exp = min((e for e, d in dtes.items() if d > 5),
                  key=lambda e: abs(dtes[e] - target_dte))
        T = max(dtes[exp], 1) / 365.0

        ch = option_chain(ticker, exp)
        ch = ch.dropna(subset=["iv", "strike"])
        ch = ch[(ch["iv"] > 1) & (ch["iv"] < 400)]            # moomoo IV is in %
        ch = ch[(ch["strike"] > spot * 0.5) & (ch["strike"] < spot * 2.5)]
        if len(ch) < 6:
            raise RuntimeError(f"only {len(ch)} usable strikes")

        ch["iv"] = ch["iv"] / 100.0
        atm_iv = float(np.interp(spot, ch["strike"], ch["iv"]))
        m = np.log(ch["strike"] / spot) / (atm_iv * np.sqrt(T))
        ratio = ch["iv"] / atm_iv
        c2, c1, c0 = np.polyfit(m, ratio, 2)

        surf = VolSurface(ticker, vrp=atm_iv / rv, smile=(float(c0), float(c1), float(c2)),
                          rv_window=rv_window,
                          calibration={"date": str(today.date()), "expiry": exp,
                                       "n_strikes": len(ch), "spot": spot,
                                       "atm_iv": atm_iv, "rv": rv,
                                       "rv_window": rv_window, "dte": dtes[exp],
                                       "rv20": float(blended_rv(bars, 20).iloc[-1]),
                                       "rv252": float(blended_rv(bars, 252).iloc[-1])
                                       if len(bars) > 252 else float("nan")})
        return surf
    except Exception as e:
        print(f"[vol] {ticker}: chain calibration failed ({str(e)[:70]}) -> defaults")
        return VolSurface(ticker, rv_window=rv_window, **DEFAULT_SURFACE_ARGS)


if __name__ == "__main__":
    import sys
    from .data import get_bars
    t = sys.argv[1] if len(sys.argv) > 1 else "NBIS"
    b = get_bars(t, "2020-01-01")
    print(calibrate_surface(t, b).describe())
