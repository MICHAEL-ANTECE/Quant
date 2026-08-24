#!/usr/bin/env python3
"""
LEAPS strike/expiry selector, driven by a live moomoo chain.

A LEAP is not "a call with more time" -- it is a financing decision. You are
paying rent (extrinsic value) to control shares, and you are long vega for a
year+. So this ranks candidates on the three things that actually decide the
outcome, none of which are visible on a broker's option chain:

  1. RENT      extrinsic value per day, as % of the share price you control.
               This is the real carry, and it is what kills 90% of LEAP trades.
  2. LEVERAGE  delta-dollars controlled per dollar of premium, vs just buying
               the stock on margin -- the honest benchmark, not "10x upside".
  3. VOL       LEAPS IV vs the stock's OWN long-horizon realized vol. Buying a
               2-year option is a 2-year vol bet whether you meant it or not.

Break-even is quoted against the stock's realized drift, so "needs +18%" is
scored against how often this stock has actually done +18% over that horizon.

Usage: ./.venv/bin/python leaps_selector.py COHR
"""
from __future__ import annotations

import json
import math
import sys
from statistics import NormalDist

import numpy as np
import pandas as pd

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
from optlib import find, chain_for, data_dir   # noqa: E402


N = NormalDist().cdf
MULT, RFR = 100, 0.04
TODAY = pd.Timestamp("2026-08-14")


def bs(S, K, T, r, s, cp="C"):
    if T <= 0 or s <= 0:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    q = s * math.sqrt(T)
    d1 = (math.log(S / K) + (r + .5 * s * s) * T) / q
    if cp == "C":
        return S * N(d1) - K * math.exp(-r * T) * N(d1 - q)
    return K * math.exp(-r * T) * N(-(d1 - q)) - S * N(-d1)


def load(tk):
    global TODAY
    path = chain_for(tk, __import__("os").environ.get("ASOF"))
    TODAY = pd.Timestamp(_pl.Path(path).stem.split("_chain_")[1])
    d = json.load(open(path))
    S = d["spot"]
    exps = {}
    for e, rows in d["expiries"].items():
        df = pd.DataFrame(rows)
        if df.empty:
            continue
        df["cp"] = np.where(df.cp.str.contains("CALL", case=False), "C", "P")
        df["mid"] = (df.bid + df.ask) / 2
        df["spr"] = np.where(df.mid > 0, (df.ask - df.bid) / df.mid, np.nan)
        df["dte"] = (pd.Timestamp(e) - TODAY).days
        exps[e] = df[(df.bid > 0) & (df.iv > 0)].reset_index(drop=True)
    px = pd.DataFrame(d["stock_hist"])
    px["date"] = px.time_key.str[:10]
    px = px.set_index("date")[["open", "high", "low", "close"]].astype(float)
    px["ret"] = px.close.pct_change()
    return d, S, exps, px


def main():
    tk = (sys.argv[1] if len(sys.argv) > 1 else "COHR").upper()
    d, S, exps, px = load(tk)
    print("=" * 100)
    print(f"{tk} LEAPS selector   spot ${S:.2f}   mktcap ${d['total_market_val']/1e9:.1f}B   "
          f"as of {TODAY:%Y-%m-%d}")
    print("=" * 100)

    # ---- 1 term structure: where is vol cheapest per unit of time?
    print("\n[1] IV TERM STRUCTURE — what each expiry charges for vol")
    print(f"{'expiry':<13}{'dte':>5}{'ATM IV':>9}{'ATM straddle':>14}{'/spot':>8}{'bid-ask':>9}")
    ts = []
    for e, df in sorted(exps.items()):
        c = df[df.cp == "C"]
        p = df[df.cp == "P"]
        if c.empty or p.empty:
            continue
        cr = c.iloc[(c.strike - S).abs().argsort()].iloc[0]
        pr = p.iloc[(p.strike - S).abs().argsort()].iloc[0]
        iv = (cr.iv + pr.iv) / 2
        ts.append((e, df.dte.iloc[0], iv))
        print(f"{e:<13}{df.dte.iloc[0]:>5}{iv:>8.1f}%{cr.mid+pr.mid:>14.2f}"
              f"{(cr.mid+pr.mid)/S:>7.1%}{cr.spr:>8.1%}")

    # ---- 2 realized vol at the LEAP's own horizon
    print("\n[2] IS THAT VOL CHEAP? — implied vs this stock's OWN realized")
    r = px.ret.dropna()
    for w, lbl in [(60, "3m"), (126, "6m"), (252, "1y"), (504, "2y"), (len(r), "full")]:
        w = min(w, len(r))
        print(f"  realized vol {lbl:<5} ({w:>4}d) = {r.tail(w).std()*math.sqrt(252)*100:6.1f}%")
    leaps = [(e, dte, iv) for e, dte, iv in ts if dte > 120]
    for e, dte, iv in leaps:
        rv = r.tail(min(int(dte / 365 * 252), len(r))).std() * math.sqrt(252) * 100
        print(f"  -> {e} IV {iv:.1f}% vs matched-horizon realized {rv:.1f}%  "
              f"= {iv/rv:.2f}x  ({'RICH' if iv/rv > 1.15 else 'CHEAP' if iv/rv < 0.9 else 'fair'})")

    # ---- 3 realized drift: how often does this stock make the breakeven?
    print("\n[3] HOW OFTEN HAS THIS STOCK ACTUALLY MOVED THAT FAR?")
    print("    (overlapping windows since 2021; the denominator for every breakeven below)")
    hor = {}
    for e, dte, iv in leaps:
        h = int(dte / 365 * 252)
        fwd = (px.close.shift(-h) / px.close - 1).dropna()
        hor[e] = fwd
        print(f"  {e} ({dte}d ~ {h} sessions, n={len(fwd)}): "
              f"median {fwd.median():+6.1%}  mean {fwd.mean():+6.1%}  "
              f"P(>0) {(fwd>0).mean():.0%}  P(>+25%) {(fwd>.25).mean():.0%}  "
              f"P(>+50%) {(fwd>.50).mean():.0%}")

    # ---- 4 the candidate table
    print("\n[4] LEAPS CANDIDATES — rent, leverage, and the breakeven that must be cleared")
    print(f"{'expiry':<12}{'K':>6}{'ask':>8}{'delta':>7}{'IV':>7}{'extrin':>8}"
          f"{'rent/d':>8}{'rent/yr':>9}{'lever':>7}{'B/E':>9}{'B/E%':>8}{'P(hit)':>8}{'OI':>7}")
    rows = []
    for e, dte, iv0 in leaps:
        df = exps[e]
        c = df[df.cp == "C"].sort_values("strike")
        fwd = hor[e]
        for _, x in c.iterrows():
            if not (0.20 <= x.delta <= 0.92):
                continue
            ask = x.ask
            intr = max(S - x.strike, 0)
            extr = ask - intr
            rent_d = extr / dte
            lever = x.delta * S / ask
            be = x.strike + ask
            bepct = be / S - 1
            phit = float((fwd > bepct).mean())
            rows.append(dict(e=e, dte=dte, K=x.strike, ask=ask, delta=x.delta, iv=x.iv,
                             extr=extr, rent_d=rent_d, rent_y=rent_d * 365 / S,
                             lever=lever, be=be, bepct=bepct, phit=phit, oi=x.oi,
                             vega=x.vega, theta=x.theta, spr=x.spr))
            print(f"{e:<12}{x.strike:>6.0f}{ask:>8.2f}{x.delta:>7.3f}{x.iv:>6.1f}%"
                  f"{extr:>8.2f}{rent_d:>8.3f}{rent_d*365/S:>8.1%}{lever:>7.2f}x"
                  f"{be:>9.2f}{bepct:>+8.1%}{phit:>8.0%}{x.oi:>7,.0f}")

    R = pd.DataFrame(rows)
    print("\n  rent/yr = extrinsic value burned per year, as % of the SHARE PRICE you control.")
    print("  lever   = delta-dollars per premium dollar. Stock on 2x margin = 2.00x.")
    print("  P(hit)  = share of historical windows of that length that cleared the breakeven.")

    # ---- 5 ranking
    print("\n[5] RANKED — expected value using the stock's own realized distribution")
    print(f"{'expiry':<12}{'K':>6}{'ask':>8}{'EV/$':>8}{'P(hit)':>8}{'rent/yr':>9}"
          f"{'lever':>7}{'E[ret]':>9}{'spread':>8}")
    out = []
    for _, x in R.iterrows():
        fwd = hor[x.e].values
        pay = np.maximum(S * (1 + fwd) - x.K, 0) - x.ask
        ev = pay.mean()
        out.append((x.e, x.K, x.ask, ev / x.ask, x.phit, x.rent_y, x.lever,
                    ev / x.ask, x.spr))
    out.sort(key=lambda z: -z[3])
    for e, K, ask, evd, ph, ry, lv, er, sp in out[:14]:
        print(f"{e:<12}{K:>6.0f}{ask:>8.2f}{evd:>+8.2f}{ph:>8.0%}{ry:>9.1%}"
              f"{lv:>7.2f}x{er:>+9.0%}{sp:>8.1%}")
    print("\n  EV/$ = expected profit per dollar of premium, scored on overlapping historical")
    print("  windows of that exact length. It is descriptive of the past, not a forecast --")
    print("  and it inherits whatever trend the sample had, so read it beside P(hit).")
    print()


if __name__ == "__main__":
    main()
