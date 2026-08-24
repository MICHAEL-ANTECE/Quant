#!/usr/bin/env python3
"""
Dealer gamma-exposure (GEX) profile from a live moomoo chain, plus the strike-
selection metrics that actually differ between OI, gamma and IV.

Why this exists: "there is a gamma wall at X" is the most repeated and least
verified claim in options retail. It is checkable -- OI and per-contract gamma
are both in the chain. This computes the profile, finds the flip point, and
shows how fast the wall decays, because a wall built out of THIS week's expiry
stops existing on Friday.

Sign convention (SqueezeMetrics): dealers long call gamma, short put gamma.
GEX(K) = S^2 * 0.01 * 100 * (gamma_call*OI_call - gamma_put*OI_put)
= dollar change in dealer delta per 1% move in spot. Positive total GEX means
dealers sell rallies / buy dips (vol suppressed); negative means they chase.

Usage: ./.venv/bin/python gex_profile.py NBIS [near_expiries]
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
from optlib import find, chain_for, data_dir   # noqa: E402


TODAY = pd.Timestamp(__import__("os").environ.get("ASOF", "2026-08-14"))


def load(tk):
    global TODAY
    path = chain_for(tk, __import__("os").environ.get("ASOF"))
    TODAY = pd.Timestamp(_pl.Path(path).stem.split("_chain_")[1])
    d = json.load(open(path))
    S = d["spot"]
    frames = []
    for e, rows in d["expiries"].items():
        df = pd.DataFrame(rows)
        if df.empty:
            continue
        df["cp"] = np.where(df.cp.str.contains("CALL", case=False), "C", "P")
        df["exp"] = e
        df["dte"] = (pd.Timestamp(e) - TODAY).days
        df["mid"] = (df.bid + df.ask) / 2
        frames.append(df)
    return d, S, pd.concat(frames, ignore_index=True)


def gex_by_strike(df, S):
    df = df.copy()
    sign = np.where(df.cp == "C", 1.0, -1.0)
    df["gex"] = sign * df.gamma * df.oi * 100 * S * S * 0.01
    return df


def main():
    tk = (sys.argv[1] if len(sys.argv) > 1 else "NBIS").upper()
    d, S, all_df = load(tk)
    df = gex_by_strike(all_df, S)
    print("=" * 96)
    print(f"{tk} GEX profile   spot ${S:.2f}   {len(df)} contracts across "
          f"{df.exp.nunique()} expiries   {TODAY:%Y-%m-%d}")
    print("=" * 96)

    tot = df.gex.sum()
    print(f"\n[1] TOTAL DEALER GAMMA: ${tot/1e6:+,.1f}M per 1% move")
    print(f"    {'POSITIVE -> dealers dampen moves (pinning, vol suppressed)' if tot > 0 else 'NEGATIVE -> dealers amplify moves (squeeze / air pocket)'}")

    print("\n[2] GEX BY STRIKE — where the walls actually are")
    g = (df.groupby("strike")
           .agg(gex=("gex", "sum"), call_oi=("oi", lambda s: s[df.loc[s.index, "cp"] == "C"].sum()),
                put_oi=("oi", lambda s: s[df.loc[s.index, "cp"] == "P"].sum()))
           .reset_index())
    g = g[(g.strike >= S * 0.6) & (g.strike <= S * 1.6)].sort_values("strike")
    peak = g.loc[g.gex.abs().idxmax()]
    mx = g.gex.abs().max()
    print(f"{'strike':>8}{'GEX $M':>10}{'callOI':>9}{'putOI':>8}  profile")
    for _, r in g.iterrows():
        n = int(abs(r.gex) / mx * 44)
        bar = ("+" if r.gex > 0 else "-") * n
        mark = "  <== SPOT" if abs(r.strike - S) < 5 else ""
        print(f"{r.strike:>8.0f}{r.gex/1e6:>+10.1f}{r.call_oi:>9,.0f}{r.put_oi:>8,.0f}  {bar}{mark}")
    print(f"\n    largest wall: strike {peak.strike:.0f}  ({peak.gex/1e6:+.1f}M)")

    print("\n[3] WALL DURABILITY — how much of each wall dies at the next expiry")
    top = g.reindex(g.gex.abs().sort_values(ascending=False).index).head(6)
    print(f"{'strike':>8}{'total $M':>11}" + "".join(f"{e[5:]:>9}" for e in sorted(df.exp.unique())[:6]))
    for _, r in top.iterrows():
        sub = df[df.strike == r.strike].groupby("exp").gex.sum()
        row = "".join(f"{sub.get(e,0)/1e6:>9.1f}" for e in sorted(df.exp.unique())[:6])
        print(f"{r.strike:>8.0f}{r.gex/1e6:>+11.1f}{row}")
    front = df[df.dte <= 7].gex.sum()
    print(f"\n    {front/tot*100 if tot else 0:.0f}% of total GEX sits in expiries <= 7 days "
          f"(${front/1e6:+.1f}M of ${tot/1e6:+.1f}M)")
    print("    -> a wall made of front-week OI is gone after Friday. Do not plan a")
    print("       multi-week thesis around it.")

    print("\n[4] STRIKE SELECTION — the four metrics, and what each one answers")
    exp = sys.argv[2] if len(sys.argv) > 2 else "2026-09-18"
    c = df[(df.exp == exp) & (df.cp == "C")].sort_values("strike")
    c = c[(c.strike >= S * 0.85) & (c.strike <= S * 1.35)]
    print(f"    expiry {exp} ({c.dte.iloc[0]}d)")
    print(f"{'K':>6}{'bid':>8}{'ask':>8}{'spr%':>7}{'IV':>7}{'delta':>7}{'gamma':>8}"
          f"{'theta':>8}{'vega':>7}{'OI':>8}{'vol':>7}{'$/delta':>9}")
    for _, r in c.iterrows():
        mid = r.mid if r.mid > 0 else np.nan
        print(f"{r.strike:>6.0f}{r.bid:>8.2f}{r.ask:>8.2f}{(r.ask-r.bid)/mid:>6.1%}"
              f"{r.iv:>6.1f}%{r.delta:>7.3f}{r.gamma:>8.4f}{r.theta:>8.3f}{r.vega:>7.3f}"
              f"{r.oi:>8,.0f}{r.volume:>7,.0f}{mid/max(r.delta,1e-6):>9.1f}")
    print("\n    $/delta = premium paid per unit of directional exposure. Lower = more")
    print("    efficient way to own the move. This is the number strike-pickers ignore.")
    print()


if __name__ == "__main__":
    main()
