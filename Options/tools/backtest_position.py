#!/usr/bin/env python3
"""
Position backtest: "I bought a stock and/or options on DATE X — what's my
return to today?"

WHAT IS EXACT vs APPROXIMATE
----------------------------
* STOCK leg  -> EXACT. Free daily prices (yfinance) give the entry close and
                today's close directly.
* OPTION leg -> the entry price is the hard part. Free data does NOT contain
                historical option prices. So each option leg supports 3 modes:
     mode="actual" : you type the premium you actually paid (and, if you want,
                     the current mark). Most accurate — use your real fills.
     mode="live"   : today's price is pulled live from yfinance's option chain
                     (works only if the contract is still listed / not expired);
                     entry price still comes from entry_premium OR BS below.
     mode="moomoo" : today's/recent price is pulled from YOUR moomoo OpenAPI
                     (needs OpenD running; matches your terminal). Entry price
                     still comes from entry_premium OR BS (moomoo keeps only a
                     short option history, so it can't price an old entry).
     mode="av"     : entry AND current prices come from Alpha Vantage's historical
                     option chain (deep history, needs a free ALPHAVANTAGE_API_KEY).
                     This is the one mode that gives an EXACT months-old entry price
                     without your own fill.
     mode="bs"     : both entry and current prices are RECONSTRUCTED with
                     Black-Scholes from the stock price + an IV you supply.
                     A proxy — good for what-ifs, not for exact fills.
  If a contract has already expired, the "current" value is its intrinsic
  value settled at expiry.

Deps: numpy, pandas, yfinance   ->   pip install numpy pandas yfinance
Run:  python3 backtest_position.py
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import NormalDist
import numpy as np
import pandas as pd

# ============================ YOUR POSITION ================================= #
TICKER     = "NVDA"
ENTRY_DATE = "2026-01-01"     # when you bought
TODAY      = None             # None = latest available close
RFR        = 0.04             # risk-free rate for BS reconstruction

STOCK_SHARES = 100            # shares of TICKER bought at ENTRY_DATE (0 for none)

# One dict per option leg. Multiplier is 100. contracts is number of contracts.
#   type: "call" | "put"
#   mode: "actual" | "live" | "bs"
#   entry_premium / current_premium: per-share prices (used by actual/live)
#   entry_iv: decimal IV for BS entry pricing (mode="bs"); if None, uses live IV
OPTION_LEGS = [
    {"type": "call", "strike": 150, "expiry": "2027-01-15", "contracts": 1,
     "mode": "live", "entry_premium": 22.50, "entry_iv": None},
    # {"type": "put", "strike": 120, "expiry": "2026-09-18", "contracts": 2,
    #  "mode": "actual", "entry_premium": 6.80, "current_premium": 3.10},
    # {"type": "call", "strike": 140, "expiry": "2026-12-18", "contracts": 1,
    #  "mode": "bs", "entry_iv": 0.45},
]
MULT = 100
# =========================================================================== #

N = NormalDist().cdf


def bs_price(kind, S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def load_stock():
    import yfinance as yf
    df = yf.download(TICKER, start="2015-01-01", end=TODAY, auto_adjust=True, progress=False)
    if df.empty:
        raise SystemExit(f"No price data for {TICKER}.")
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s.index = pd.to_datetime(s.index)
    return s


def price_on(s, when):
    """Close on-or-before `when` (handles weekends/holidays)."""
    when = pd.Timestamp(when)
    sub = s[s.index <= when]
    if sub.empty:
        raise SystemExit(f"No price for {TICKER} on/before {when.date()}.")
    return float(sub.iloc[-1]), sub.index[-1].date()


def live_option(kind, strike, expiry):
    """Return today's mid price and IV for a listed contract, or None if unavailable."""
    import yfinance as yf
    try:
        tk = yf.Ticker(TICKER)
        if expiry not in tk.options:
            return None
        chain = tk.option_chain(expiry)
        tbl = chain.calls if kind == "call" else chain.puts
        row = tbl[tbl["strike"] == strike]
        if row.empty:
            return None
        r = row.iloc[0]
        bid, ask, last = float(r.get("bid", 0)), float(r.get("ask", 0)), float(r.get("lastPrice", 0))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        return mid, float(r.get("impliedVolatility", float("nan")))
    except Exception:
        return None


def main():
    s = load_stock()
    S_entry, d_entry = price_on(s, ENTRY_DATE)
    S_now, d_now = price_on(s, TODAY or s.index[-1])
    T_entry = pd.Timestamp(d_entry)

    print(f"\n=== {TICKER} position backtest ===")
    print(f"entry {d_entry}  @ ${S_entry:,.2f}   ->   {d_now}  @ ${S_now:,.2f}   "
          f"(stock {S_now/S_entry-1:+.1%})\n")

    total_cost = total_now = 0.0

    # ---- stock leg ----
    if STOCK_SHARES:
        c = S_entry * STOCK_SHARES
        v = S_now * STOCK_SHARES
        total_cost += c; total_now += v
        print(f"STOCK  {STOCK_SHARES} sh   cost ${c:,.0f}  ->  ${v:,.0f}   "
              f"P&L ${v-c:,.0f}  ({v/c-1:+.1%})")

    # ---- option legs ----
    for leg in OPTION_LEGS:
        kind, K, exp = leg["type"], leg["strike"], leg["expiry"]
        contracts, mode = leg["contracts"], leg["mode"]
        exp_ts = pd.Timestamp(exp)
        T0 = max((exp_ts - T_entry).days, 0) / 365.0

        # entry premium
        if leg.get("entry_premium") is not None and mode in ("actual", "live", "moomoo"):
            entry_px = leg["entry_premium"]
            entry_src = "actual"
        elif mode == "av":
            try:
                from alphavantage_options import av_option_price
                r = av_option_price(TICKER, str(d_entry), K, exp, kind)
                entry_px, entry_src = r["price"], f"av({r['date_used']})"
            except Exception as e:
                entry_px, entry_src = float("nan"), f"av-fail({str(e)[:30]})"
        else:
            iv0 = leg.get("entry_iv")
            if iv0 is None:  # fall back to live IV as a rough stand-in
                lv = live_option(kind, K, exp)
                iv0 = lv[1] if lv and not math.isnan(lv[1]) else 0.40
            entry_px = bs_price(kind, S_entry, K, T0, RFR, iv0)
            entry_src = f"BS(iv={iv0:.0%})"

        # current premium
        expired = exp_ts.date() <= d_now
        if expired:
            cur_px = max(S_now - K, 0) if kind == "call" else max(K - S_now, 0)
            cur_src = "expired-intrinsic"
        elif mode == "actual" and leg.get("current_premium") is not None:
            cur_px, cur_src = leg["current_premium"], "actual"
        elif mode == "moomoo":
            try:
                from moomoo_option_price import moomoo_option_last
                res = moomoo_option_last(TICKER, exp, K, kind)
                if res:
                    cur_px, cur_src = res[1], f"moomoo({res[0]})"
                else:
                    raise RuntimeError("no moomoo klines")
            except Exception as e:
                cur_px, cur_src = float("nan"), f"moomoo-fail({str(e)[:30]})"
        elif mode == "av":
            try:
                from alphavantage_options import av_option_price
                r = av_option_price(TICKER, str(d_now), K, exp, kind)
                cur_px, cur_src = r["price"], f"av({r['date_used']})"
            except Exception as e:
                cur_px, cur_src = float("nan"), f"av-fail({str(e)[:30]})"
        else:
            lv = live_option(kind, K, exp)
            if lv:
                cur_px, cur_src = lv[0], "live-mid"
            else:  # BS fallback with live/entry IV
                Tn = max((exp_ts - pd.Timestamp(d_now)).days, 0) / 365.0
                iv = leg.get("entry_iv") or 0.40
                cur_px, cur_src = bs_price(kind, S_now, K, Tn, RFR, iv), f"BS(iv={iv:.0%})"

        c = entry_px * MULT * contracts
        v = cur_px * MULT * contracts
        total_cost += c; total_now += v
        ret = (v / c - 1) if c else float("nan")
        print(f"{kind.upper():5} ${K} {exp} x{contracts}   "
              f"entry ${entry_px:6.2f} [{entry_src}]  ->  now ${cur_px:6.2f} [{cur_src}]   "
              f"P&L ${v-c:,.0f}  ({ret:+.1%})")

    # ---- total ----
    if total_cost:
        print(f"\nTOTAL  cost ${total_cost:,.0f}  ->  ${total_now:,.0f}   "
              f"P&L ${total_now-total_cost:,.0f}  ({total_now/total_cost-1:+.1%})\n")
    print("Note: STOCK is exact. Option 'live-mid' = today's chain; 'actual' = your fills;")
    print("'BS(...)' = reconstructed (proxy). For exact historical option entry prices,")
    print("use your real fills or a paid historical option-chain source.\n")


if __name__ == "__main__":
    main()
