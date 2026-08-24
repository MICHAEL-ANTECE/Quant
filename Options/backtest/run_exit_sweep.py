#!/usr/bin/env python3
"""
Continuous sweep of the exit point for the 165DTE tenor — the one that matches
the Webull book — across all four underlyings and three deltas.

Purpose: last run showed exit@90DTE having the lowest max drawdown on 4/4
tickers. Three points per ticker is exactly the kind of evidence that turns out
to be noise, so this walks the exit in 15-day steps and asks whether the pattern
is MONOTONE, and whether it survives changing the delta.

A trap this guards against: pushing the exit earlier shortens the average holding
period, so drawdown can fall for a boring mechanical reason rather than a real
one. Time-in-market and trade count are reported alongside, and the headline
metric is return-per-unit-of-drawdown, not drawdown alone.

Run:  ./.venv/bin/python run_exit_sweep.py
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from optbt import get_bars, calibrate_surface, Rule, run, signals
from optbt.data import clean_bars

warnings.filterwarnings("ignore")

TICKERS = ["NBIS", "BE", "CRDO", "ASX"]
TENOR = 165
EXITS = [0, 15, 30, 45, 60, 75, 90, 105, 120, 135]
DELTAS = [0.20, 0.30, 0.40]
CASH_PER_TRADE, MAX_CONTRACTS, START_EQUITY = 1_000.0, 20, 20_000.0


def main():
    print(f"loading + calibrating {len(TICKERS)} tickers ...")
    surf, book = {}, {}
    for t in TICKERS:
        b = clean_bars(get_bars(t, "2015-01-01"), t)
        book[t], surf[t] = b, calibrate_surface(t, b, target_dte=120)

    rows = []
    for delta in DELTAS:
        for t in TICKERS:
            for ex in EXITS:
                rule = Rule(target_dte=TENOR, target_delta=delta, exit_dte=ex,
                            size_mode="fixed_cash", size=CASH_PER_TRADE,
                            max_contracts=MAX_CONTRACTS, max_open=1,
                            signal=signals.always(), name=f"x{ex}")
                s = run(book[t], surf[t], rule, START_EQUITY)["stats"]
                rows.append({
                    "delta": delta, "ticker": t, "exit_dte": ex,
                    "held_days": TENOR - ex,
                    "ret": s["total_return"], "dd": s["max_dd"],
                    "calmar": s["total_return"] / abs(s["max_dd"]) if s["max_dd"] else np.nan,
                    "sharpe": s["sharpe"], "trades": s["n_trades"],
                    "in_mkt": s["time_in_market"],
                    "pf": s.get("profit_factor", np.nan),
                    "win": s.get("win_rate", np.nan)})
    df = pd.DataFrame(rows)
    df.to_csv("exit_sweep_results.csv", index=False)

    for delta in DELTAS:
        d = df[df["delta"] == delta]
        print("\n" + "=" * 92)
        print(f"165DTE  {delta:.2f}Δ  — exit point sweep")
        print("=" * 92)
        for metric, fmt, label in [("dd", "{:.0%}", "max drawdown"),
                                   ("ret", "{:+.0%}", "total return"),
                                   ("calmar", "{:.2f}", "return / |maxDD|")]:
            p = d.pivot(index="exit_dte", columns="ticker", values=metric)[TICKERS]
            print(f"\n  --- {label} ---")
            print(p.to_string(float_format=lambda x: fmt.format(x)))

        print("\n  --- trades / time-in-market (degeneracy check) ---")
        p = d.pivot(index="exit_dte", columns="ticker", values="trades")[TICKERS]
        q = d.pivot(index="exit_dte", columns="ticker", values="in_mkt")[TICKERS]
        print(pd.concat({"trades": p, "in_mkt": q.round(2)}, axis=1).to_string())

    # ---------- monotonicity ----------
    print("\n" + "=" * 92)
    print("IS THE PATTERN MONOTONE? Spearman rank correlation vs exit_dte")
    print("(+1 = metric rises steadily as you exit earlier/later in DTE terms)")
    print("=" * 92)
    out = []
    for delta in DELTAS:
        for t in TICKERS:
            d = df[(df["delta"] == delta) & (df["ticker"] == t)].sort_values("exit_dte")
            r = {"delta": delta, "ticker": t}
            for m in ["dd", "ret", "calmar"]:
                rho, p = spearmanr(d["exit_dte"], d[m])
                r[f"rho_{m}"] = rho
                r[f"p_{m}"] = p
            out.append(r)
    mono = pd.DataFrame(out)
    print(mono.to_string(index=False, float_format=lambda x: f"{x:+.2f}"))

    print("\n  reading: rho_dd > 0 means drawdown gets SHALLOWER (less negative) the")
    print("  earlier you exit. rho_calmar > 0 means the risk-adjusted result improves.")
    n = len(mono)
    print(f"\n  drawdown improves with earlier exit : {(mono['rho_dd'] > 0.6).sum()}/{n} cells (rho > 0.6)")
    print(f"  return  improves with earlier exit  : {(mono['rho_ret'] > 0.6).sum()}/{n} cells")
    print(f"  calmar  improves with earlier exit  : {(mono['rho_calmar'] > 0.6).sum()}/{n} cells")
    print(f"  calmar  DEGRADES with earlier exit  : {(mono['rho_calmar'] < -0.6).sum()}/{n} cells")

    # ---------- where is the best risk-adjusted exit? ----------
    print("\n" + "=" * 92)
    print("BEST EXIT POINT BY return/|maxDD|, per ticker x delta")
    print("=" * 92)
    best = df.loc[df.groupby(["delta", "ticker"])["calmar"].idxmax()]
    print(best[["delta", "ticker", "exit_dte", "held_days", "ret", "dd",
                "calmar", "trades"]].to_string(
        index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\nmedian best exit_dte across all 12 cells: "
          f"{best['exit_dte'].median():.0f}DTE "
          f"(range {best['exit_dte'].min():.0f}-{best['exit_dte'].max():.0f})")

    print("\nfull grid written to exit_sweep_results.csv")
    print("\nCAVEAT: BS proxy prices, tiny samples, one smile shape applied to all "
          "history.\nShape check only — not investment advice.")


if __name__ == "__main__":
    main()
