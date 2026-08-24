#!/usr/bin/env python3
"""
All-cash, no position: the real question is not "which structure" but
"before the print or after it?"

A flat account owns a real option a positioned account does not -- the right to
skip the binary and buy the SAME upside two days later without the 61 vol points
of event premium. This prices that choice on NBIS's own six reported quarters:

  path A: buy 08/14 or 09/18 calls TODAY, carry the event.
  path B: sit out, buy 09/18-equivalent calls at the post-event vol level.

Post-earnings drift is measured from the reaction-day CLOSE (i.e. you already
missed the gap) so path B is scored honestly -- no hindsight on the jump itself.

Run: ./.venv/bin/python nbis_before_vs_after.py
"""
from __future__ import annotations
import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

N = NormalDist().cdf
MULT, RFR = 100, 0.04
BASE_VOL = 0.993            # fitted base diffusion, nbis_q2_strategy.py
EVENT_SIG = 0.1173          # fitted one-shot event jump
TODAY = pd.Timestamp("2026-08-11")
EARN_REACTIONS = ["2025-02-20", "2025-05-20", "2025-08-07",
                  "2025-11-12", "2026-02-12", "2026-05-13"]


def bs(S, K, T, s, cp="C"):
    if T <= 0 or s <= 0:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    q = s * math.sqrt(T)
    d1 = (math.log(S / K) + (RFR + .5 * s * s) * T) / q
    if cp == "C":
        return S * N(d1) - K * math.exp(-RFR * T) * N(d1 - q)
    return K * math.exp(-RFR * T) * N(-(d1 - q)) - S * N(-d1)


def main():
    d = json.load(open("nbis_chain_2026-08-11.json"))
    S = d["spot"]
    px = pd.DataFrame(d["stock_hist"])
    px["date"] = px.time_key.str[:10]
    px = px.set_index("date")[["open", "close"]].astype(float)

    print(f"\nNBIS ${S:.2f}   all-cash decision: carry the print, or buy it back after?\n")

    # ---- 1. does NBIS trend AFTER the reaction day, or mean-revert?
    print("[1] POST-EARNINGS DRIFT — measured from the reaction-day CLOSE (gap already missed)")
    print(f"{'quarter':<12}{'reaction':<12}{'day move':>10}" +
          "".join(f"{f'+{h}d':>9}" for h in (1, 3, 5, 10, 20)))
    rows = []
    for dt in EARN_REACTIONS:
        i = px.index.get_loc(dt)
        day = px.close.iloc[i] / px.close.iloc[i - 1] - 1
        fwd = []
        for h in (1, 3, 5, 10, 20):
            j = min(i + h, len(px) - 1)
            fwd.append(px.close.iloc[j] / px.close.iloc[i] - 1)
        rows.append((dt, day, fwd))
        print(f"{'Q':<12}{dt:<12}{day:>+9.1%}" + "".join(f"{v:>+9.1%}" for v in fwd))
    arr = np.array([r[2] for r in rows])
    print(f"{'MEAN':<24}{np.mean([r[1] for r in rows]):>+9.1%}" +
          "".join(f"{v:>+9.1%}" for v in arr.mean(axis=0)))
    print(f"{'MEDIAN':<24}{np.median([r[1] for r in rows]):>+9.1%}" +
          "".join(f"{v:>+9.1%}" for v in np.median(arr, axis=0)))
    print(f"{'% positive':<24}{'':>9}" +
          "".join(f"{v:>9.0%}" for v in (arr > 0).mean(axis=0)))
    same = np.sign(arr[:, 2]) == np.sign([r[1] for r in rows])
    print(f"\n  continuation (+5d same sign as the print day): {same.sum()}/6")
    print("  -> if this is ~50/50 there is NO free drift to harvest by waiting;")
    print("     waiting is then purely about paying less vol, not about direction.")

    # ---- 2. what does the SAME upside cost before vs after?
    print("\n[2] PRICE OF THE SAME UPSIDE, BEFORE vs AFTER the print")
    print("    (after = event premium gone, IV back to the fitted base 99.3%)")
    exps = {e: pd.DataFrame(v) for e, v in d["expiries"].items()}
    for e in ("2026-09-18", "2026-10-16"):
        df = exps[e]
        df["cp"] = np.where(df.cp.str.contains("CALL", case=False), "C", "P")
        df["mid"] = (df.bid + df.ask) / 2
        dte = (pd.Timestamp(e) - TODAY).days
        print(f"\n  {e}  ({dte} dte)")
        print(f"{'strike':>8}{'ask now':>10}{'IV now':>9}{'after (flat)':>14}"
              f"{'saving':>9}{'after +12%':>12}{'after -12%':>12}")
        for k in (200, 210, 220, 230, 250):
            r = df[(df.cp == "C") & (df.strike == k)]
            if r.empty:
                continue
            r = r.iloc[0]
            T2 = (dte - 3) / 365
            flat = bs(S, k, T2, BASE_VOL)
            up = bs(S * 1.12, k, T2, BASE_VOL)
            dn = bs(S * 0.88, k, T2, BASE_VOL)
            print(f"{k:>8}{r.ask:>10.2f}{r.iv:>8.1f}%{flat:>14.2f}"
                  f"{flat/r.ask-1:>+8.0%}{up:>12.2f}{dn:>12.2f}")
    print("\n  -> 'saving' is what sitting out earns you if NBIS goes NOWHERE.")
    print("     'after +12%' is what the same strike costs if you were right and waited:")
    print("     that is the price of the insight you would have skipped.")

    # ---- 3. breakeven: how big a move makes waiting the wrong call?
    print("\n[3] BREAK-EVEN — the move at which BUYING NOW beats WAITING")
    df = exps["2026-09-18"]          # cp already normalised in [2]; re-mapping would flip C -> P
    dte = (pd.Timestamp("2026-09-18") - TODAY).days
    print(f"{'strike':>8}{'buy now':>10}{'':>4}{'move where waiting costs the same':>36}")
    for k in (200, 210, 220, 230, 250):
        r = df[(df.cp == "C") & (df.strike == k)]
        if r.empty:
            continue
        cost_now = float(r.iloc[0].ask)
        lo, hi = 0.0, 1.2
        for _ in range(60):
            m = (lo + hi) / 2
            # waiting: same $ buys contracts at the post-event price, at spot S(1+m)
            if bs(S * (1 + m), k, (dte - 3) / 365, BASE_VOL) > cost_now:
                hi = m
            else:
                lo = m
        print(f"{k:>8}{cost_now:>10.2f}{'':>4}{f'NBIS +{(lo+hi)/2:.1%}':>36}")
    print("\n  read: below that move, waiting buys the SAME contract cheaper than today's ask.")
    print("  above it, the stock ran away and the event premium was worth paying.")
    print(f"  market-implied 1-sigma event jump = +/-{EVENT_SIG:.1%}\n")


if __name__ == "__main__":
    main()
