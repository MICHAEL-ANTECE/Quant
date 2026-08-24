#!/usr/bin/env python3
"""
Batch-backtest the four underlyings actually in the Webull book
(NBIS, BE, CRDO, ASX) with one consistent, deliberately conservative setup, and
answer the one question the book actually poses:

    does holding a long-dated OTM call to expiry beat closing it early?

Every leg in the margin account is a 78-169 DTE call with no exit plan, so that
is the comparison worth spending compute on. Everything else here is context for
it.

Run:  ./.venv/bin/python run_book_backtest.py
"""

from __future__ import annotations

import warnings

import pandas as pd

from optbt import get_bars, calibrate_surface, Rule, run, signals
from optbt.data import clean_bars

warnings.filterwarnings("ignore")

TICKERS = ["NBIS", "BE", "CRDO", "ASX"]
START = "2015-01-01"

# Fixed dollars per trade + a contract cap: no compounding fiction, no pretending
# you can fill 4,000 lots of a deep-OTM strike.
CASH_PER_TRADE = 1_000.0
MAX_CONTRACTS = 20
START_EQUITY = 20_000.0

# (label, target_dte, target_delta, exit_dte)
POLICIES = [
    ("165DTE 30Δ  hold to expiry", 165, 0.30, 0),
    ("165DTE 30Δ  exit @45DTE", 165, 0.30, 45),
    ("165DTE 30Δ  exit @90DTE", 165, 0.30, 90),
    ("90DTE  25Δ  hold to expiry", 90, 0.25, 0),
    ("90DTE  25Δ  exit @21DTE", 90, 0.25, 21),
    ("90DTE  25Δ  exit @45DTE", 90, 0.25, 45),
    ("45DTE  25Δ  hold to expiry", 45, 0.25, 0),
    ("45DTE  25Δ  exit @14DTE", 45, 0.25, 14),
]


def main():
    surfaces, book, rows = {}, {}, []

    print("=" * 100)
    print("VOL SURFACE CALIBRATION (fitted to today's real moomoo chain)")
    print("=" * 100)
    for t in TICKERS:
        bars = clean_bars(get_bars(t, START), t)
        surf = calibrate_surface(t, bars, target_dte=120)
        surfaces[t], book[t] = surf, bars
        print(surf.describe())
        print(f"   usable history: {len(bars)} bars {bars.index.min().date()} -> "
              f"{bars.index.max().date()} ({len(bars)/252:.1f}y)\n")

    print("=" * 100)
    print(f"POLICY x TICKER — total return, ${CASH_PER_TRADE:,.0f}/trade, "
          f"max {MAX_CONTRACTS} contracts, 3% slippage each side")
    print("=" * 100)

    for label, dte, delta, exit_dte in POLICIES:
        row = {"policy": label}
        for t in TICKERS:
            rule = Rule(target_dte=dte, target_delta=delta, exit_dte=exit_dte,
                        size_mode="fixed_cash", size=CASH_PER_TRADE,
                        max_contracts=MAX_CONTRACTS, max_open=1,
                        signal=signals.always(), name=label)
            try:
                res = run(book[t], surfaces[t], rule, START_EQUITY)
                s = res["stats"]
                row[t] = s["total_return"]
                row[f"{t}_n"] = s["n_trades"]
                row[f"{t}_dd"] = s["max_dd"]
                row[f"{t}_pf"] = s.get("profit_factor", float("nan"))
            except Exception as e:
                print(f"[skip] {label} / {t}: {str(e)[:60]}")
                row[t] = float("nan")
        rows.append(row)

    df = pd.DataFrame(rows).set_index("policy")

    ret = df[TICKERS]
    print("\n--- total return ---")
    print(ret.to_string(float_format=lambda x: f"{x:+,.0%}"))

    print("\n--- number of trades (sample size) ---")
    print(df[[f"{t}_n" for t in TICKERS]].rename(
        columns={f"{t}_n": t for t in TICKERS}).to_string())

    print("\n--- max drawdown ---")
    print(df[[f"{t}_dd" for t in TICKERS]].rename(
        columns={f"{t}_dd": t for t in TICKERS}).to_string(
        float_format=lambda x: f"{x:.0%}"))

    print("\n--- profit factor (gross win / gross loss) ---")
    print(df[[f"{t}_pf" for t in TICKERS]].rename(
        columns={f"{t}_pf": t for t in TICKERS}).to_string(
        float_format=lambda x: f"{x:,.2f}"))

    # ---- the actual question ----
    print("\n" + "=" * 100)
    print("HOLD TO EXPIRY vs CLOSE EARLY — same DTE and delta, only the exit differs")
    print("=" * 100)
    pairs = [("165DTE 30Δ  hold to expiry", "165DTE 30Δ  exit @45DTE"),
             ("90DTE  25Δ  hold to expiry", "90DTE  25Δ  exit @21DTE"),
             ("45DTE  25Δ  hold to expiry", "45DTE  25Δ  exit @14DTE")]
    cmp_rows = []
    for hold, early in pairs:
        for t in TICKERS:
            cmp_rows.append({"tenor": hold.split()[0], "ticker": t,
                             "hold_to_expiry": ret.loc[hold, t],
                             "close_early": ret.loc[early, t],
                             "delta": ret.loc[early, t] - ret.loc[hold, t]})
    c = pd.DataFrame(cmp_rows)
    print(c.to_string(index=False, float_format=lambda x: f"{x:+,.0%}"))
    wins = (c["delta"] > 0).sum()
    print(f"\nclosing early beat holding to expiry in {wins}/{len(c)} ticker-tenor cells")

    print("\n" + "=" * 100)
    print("CAVEATS — read before drawing any conclusion")
    print("=" * 100)
    print("* Option prices are calibrated Black-Scholes proxies, NOT real fills.")
    print("* NBIS history is truncated at the Yandex suspension; ~1.5y only.")
    print("* Sample sizes are tiny; long-call P&L lives in 2-3 tail trades per column.")
    print("* Today's smile shape is applied to all of history.")
    print("* This is a shape check on exit timing. It is not investment advice.")


if __name__ == "__main__":
    main()
