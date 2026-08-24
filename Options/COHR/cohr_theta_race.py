#!/usr/bin/env python3
"""
COHR: theta decay schedule vs the probability of reaching $380.

Two things people get wrong about theta, both fixed here:

  1. Theta is a one-day derivative, not a rate. Multiplying it by days-left
     UNDER-states the loss, because theta accelerates as expiry approaches.
     This revalues the option day by day with Black-Scholes instead.
  2. "Probability of $380" is ambiguous. P(finish above) and P(ever touch)
     differ by ~2x for a driftless process, and if you intend to SELL on a
     spike, touch probability is the one that governs your decision.

Positions priced: 2x COHR Aug-21 $350C (cost $9.95), 1x Jan-27 $400C ($46.55),
21 shares (cost $325.67).

Run: ./.venv/bin/python cohr_theta_race.py
"""
from __future__ import annotations

import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
from optlib import find, chain_for, data_dir   # noqa: E402


ND = NormalDist()
N = ND.cdf
MULT, RFR = 100, 0.04
S = 354.30
TODAY = pd.Timestamp("2026-08-17")

# (label, expiry, strike, iv, contracts, cost_per_share, bid, ask)
POS = [
    ("Aug-21 $350C", "2026-08-21", 350, 0.982, 2, 9.95, 15.80, 17.80),
    ("Jan-27 $400C", "2027-01-15", 400, 0.833, 1, 46.55, 59.50, 62.20),
]
TARGET = 380.0


def bs(S, K, T, r, s, cp="C"):
    if T <= 1e-9 or s <= 1e-9:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    q = s * math.sqrt(T)
    d1 = (math.log(S / K) + (r + .5 * s * s) * T) / q
    d2 = d1 - q
    if cp == "C":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def prob_above(S, K, T, sigma, drift=0.0):
    """Risk-neutral-style P(S_T > K), lognormal, zero drift by default."""
    if T <= 0:
        return float(S > K)
    d2 = (math.log(S / K) + (drift - .5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return N(d2)


def prob_touch(S, B, T, sigma):
    """P(max_{t<=T} S_t >= B) for driftless GBM (reflection principle)."""
    if T <= 0 or B <= S:
        return 1.0 if B <= S else 0.0
    x = math.log(B / S)
    a = sigma * math.sqrt(T)
    return N((-x - .5 * sigma * sigma * T) / a) + N((-x + .5 * sigma * sigma * T) / a)


def main():
    print("=" * 92)
    print(f"COHR ${S:.2f}   {TODAY:%Y-%m-%d}   target ${TARGET:.0f} "
          f"({TARGET/S-1:+.2%} away)")
    print("=" * 92)

    # ---- 1 theta decay, revalued day by day
    print("\n[1] THETA DECAY — revalued daily, spot held FLAT at $354.30")
    for lbl, exp, K, iv, n, cost, bid, ask in POS:
        dte = (pd.Timestamp(exp) - TODAY).days
        print(f"\n  {lbl}  x{n}   IV {iv:.1%}   {dte}d left   "
              f"now bid ${bid:.2f} (${bid*MULT*n:,.0f} to close)")
        print(f"{'date':<13}{'dte':>5}{'value':>9}{'x'+str(n)+' $':>10}"
              f"{'day loss':>10}{'% of value':>12}{'cum loss':>10}")
        prev = bs(S, K, dte / 365, RFR, iv)
        v0 = prev
        step = 1 if dte <= 21 else 7      # weekly rows once the tenor is long
        days = sorted(set(list(range(0, dte + 1, step)) + [dte]))
        for d in days:
            t = (dte - d) / 365
            v = bs(S, K, t, RFR, iv)
            dl = (v - prev) * MULT * n if d else 0.0
            per_day = dl / step if d else 0.0
            print(f"{(TODAY+pd.Timedelta(days=d)).strftime('%a %m-%d'):<13}{dte-d:>5}"
                  f"{v:>9.2f}{v*MULT*n:>10,.0f}{dl:>10,.0f}"
                  f"{(v/prev-1) if d else 0:>11.1%}{(v-v0)*MULT*n:>10,.0f}")
            prev = v
        naive = -1.926 * dte * MULT * n if "Aug" in lbl else -0.264 * dte * MULT * n
        print(f"    naive theta x days = ${naive:,.0f}   actual BS decay = "
              f"${(prev-v0)*MULT*n:,.0f}   understated by "
              f"${abs(prev-v0)*MULT*n-abs(naive):,.0f}")

    # ---- 2 probability of 380
    print(f"\n[2] PROBABILITY OF ${TARGET:.0f}  (needs {TARGET/S-1:+.2%})")
    d = json.load(open(find("cohr_chain_2026-08-14.json")))
    px = pd.DataFrame(d["stock_hist"])
    px["date"] = px.time_key.str[:10]
    px = px.set_index("date")[["high", "close"]].astype(float)
    r = px.close.pct_change().dropna()
    rv = {w: r.tail(w).std() * math.sqrt(252) for w in (20, 60, 126, 252)}
    print(f"  COHR realized vol: " + "  ".join(f"{w}d {v:.0%}" for w, v in rv.items()))
    print(f"\n{'horizon':<16}{'days':>6}{'vol used':>10}{'P(finish>380)':>15}"
          f"{'P(ever touch)':>15}")
    for lbl, exp, K, iv, n, cost, bid, ask in POS:
        dte = (pd.Timestamp(exp) - TODAY).days
        for vlbl, sig in [("implied", iv), ("realized 60d", rv[60]), ("realized 252d", rv[252])]:
            T = dte / 365
            print(f"{lbl+' / '+vlbl:<16}{dte:>6}{sig:>9.0%}"
                  f"{prob_above(S, TARGET, T, sig):>15.1%}"
                  f"{min(prob_touch(S, TARGET, T, sig),1):>15.1%}")
        print()

    # empirical: how often has COHR done +7.25% in 4 sessions?
    need = TARGET / S - 1
    for h, lbl in [(4, "4 sessions"), (21, "1 month"), (105, "5 months")]:
        fwd = (px.close.shift(-h) / px.close - 1).dropna()
        # max over the NEXT h sessions (t+1..t+h), using intraday highs -- a real touch
        mx = (px.high.rolling(h).max().shift(-h) / px.close - 1).dropna()
        print(f"  empirical {lbl:<12}: P(finish >{need:+.1%}) = {(fwd>need).mean():.1%}   "
              f"P(high >{need:+.1%}) = {(mx>need).mean():.1%}   (n={len(fwd)}, since 2021)")

    # ---- 3 the race
    print(f"\n[3] THE RACE — for the Aug-21 $350C, is waiting for $380 worth the theta?")
    lbl, exp, K, iv, n, cost, bid, ask = POS[0]
    dte = (pd.Timestamp(exp) - TODAY).days
    print(f"  close NOW at bid ${bid:.2f}: ${bid*MULT*n:,.0f}  "
          f"(cost ${cost*MULT*n:,.0f}, profit ${(bid-cost)*MULT*n:+,.0f}, "
          f"{bid/cost-1:+.0%})")
    print(f"\n{'COHR at 08/21':>14}{'option':>9}{'x2 value':>11}{'vs closing now':>16}")
    for s1 in (330, 340, 350, 354.30, 360, 370, 380, 390, 400):
        v = max(s1 - K, 0)
        print(f"{s1:>14.2f}{v:>9.2f}{v*MULT*n:>11,.0f}{(v-bid)*MULT*n:>+16,.0f}")
    ev_i = sum(max(S * math.exp((-.5*iv**2)*(dte/365) + iv*math.sqrt(dte/365)*z) - K, 0)
               for z in np.random.default_rng(3).standard_normal(200000)) / 200000
    print(f"\n  EV at expiry (implied vol {iv:.0%}, zero drift): ${ev_i:.2f} = ${ev_i*MULT*n:,.0f}")
    print(f"  vs closing now at bid                          : ${bid*MULT*n:,.0f}")
    print(f"  difference: ${(ev_i-bid)*MULT*n:+,.0f}  "
          f"({'holding is fairly priced' if abs(ev_i-bid)<1 else 'edge to closing' if ev_i<bid else 'edge to holding'})")
    print()


if __name__ == "__main__":
    main()
