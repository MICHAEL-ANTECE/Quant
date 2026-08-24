#!/usr/bin/env python3
"""
Reprice the whole Webull book across the NBIS Q2 earnings event (2026-08-12).

Why this exists: the book's NBIS legs carry 118-126% IV and the front weeklies
sit at ~176%. After a binary event that vol collapses, so a "right direction"
call can still lose. Freezing IV (what webull_portfolio_analysis.py's grid does)
understates that. Here spot move and IV crush move together.

Valuation date = 2026-08-13, the session after earnings (10 days from the
2026-08-03 snapshot). Non-NBIS names keep spot flat and just bleed 10 days of
theta -- they have their own catalysts, not this one.

Run: ./.venv/bin/python nbis_earnings_scenarios.py
"""

from __future__ import annotations
import math
from statistics import NormalDist

N = NormalDist().cdf
MULT = 100
SNAP_DATE = "2026-08-03"
EARN_DATE = "2026-08-12"
DAYS_FWD = 10                      # snapshot -> day after earnings
RFR = 0.04
SPOT = {"NBIS": 212.58, "BE": 218.32, "CRDO": 218.35, "ASX": 36.68}

# leg: (ticker, strike, dte_at_snapshot, iv_pct, contracts, mark, cost, account)
OPTIONS = [
    ("NBIS", 270,  74, 126.363, 1, 30.275, 1505.00, "margin"),
    ("NBIS", 280, 109, 123.875, 1, 37.300, 2795.00, "margin"),
    ("NBIS", 300, 165, 118.079, 1, 43.250, 6150.00, "margin"),
    ("ASX",   45, 165,  81.059, 2,  5.400, 1513.33, "margin"),
    ("BE",   300, 137, 116.125, 1, 38.750, 4795.00, "margin"),
    ("CRDO", 340, 165, 105.945, 1, 31.450, 4570.00, "margin"),
    ("NBIS", 270,  74, 126.363, 1, 30.275, 1356.00, "roth"),
]
# leveraged ETFs: (ticker, economic underlying, leverage, shares, mark, cost)
ETFS = [
    ("NBIG", "NBIS", 2.0, 89, 18.70, 1317.35),
    ("CRDU", "CRDO", 2.0, 68, 14.09, 1085.96),
    ("BEG",  "BE",   2.0, 70, 36.95, 1850.80),
]
BOOK_MV = sum(o[5] * MULT * o[4] for o in OPTIONS) + sum(e[3] * e[4] for e in ETFS)
BOOK_COST = sum(o[6] for o in OPTIONS) + sum(e[5] for e in ETFS)


def bs_call(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return S * N(d1) - K * math.exp(-r * T) * N(d1 - sig * math.sqrt(T))


def book_value(nbis_move, iv_mult_nbis, iv_mult_other=1.0):
    """Value the book DAYS_FWD later given an NBIS move and an IV crush factor."""
    tot = 0.0
    for tk, K, dte, iv, n, mark, cost, _ in OPTIONS:
        if tk == "NBIS":
            S = SPOT[tk] * (1 + nbis_move)
            sig = iv / 100.0 * iv_mult_nbis
        else:
            S = SPOT[tk]
            sig = iv / 100.0 * iv_mult_other
        T = max(dte - DAYS_FWD, 0) / 365.0
        tot += bs_call(S, K, T, RFR, sig) * MULT * n
    for tk, econ, lev, sh, mark, cost in ETFS:
        move = nbis_move if econ == "NBIS" else 0.0
        tot += sh * mark * (1 + lev * move)
    return tot


def main():
    print(f"\n=== NBIS Q2 earnings ({EARN_DATE}) — book repriced {DAYS_FWD}d out ===")
    print(f"snapshot {SNAP_DATE}: book MV ${BOOK_MV:,.0f}, cost ${BOOK_COST:,.0f}")
    print(f"market-implied move into 08-14: +/-24.8%  (pre-earnings weekly +/-14.0%)\n")

    moves = [-0.35, -0.25, -0.15, -0.05, 0.0, 0.05, 0.15, 0.25, 0.35]
    for label, ivm in [("no IV crush (IV stays 118-126%)", 1.00),
                       ("moderate crush (IV x0.75 -> ~90%)", 0.75),
                       ("hard crush (IV x0.60 -> ~72%)", 0.60)]:
        print(f"--- {label} ---")
        head = "NBIS move " + "".join(f"{m:>+9.0%}" for m in moves)
        print(head)
        vals = [book_value(m, ivm) for m in moves]
        print("book value " + "".join(f"{v:>9,.0f}" for v in vals))
        print("vs today   " + "".join(f"{v/BOOK_MV-1:>+9.0%}" for v in vals))
        print()

    print("--- NBIS legs only, at the implied +/-24.8% move ---")
    print(f"{'leg':<22}{'mark':>8}{'-24.8% crush':>14}{'flat crush':>12}{'+24.8% crush':>14}{'+24.8% no crush':>17}")
    for tk, K, dte, iv, n, mark, cost, acct in OPTIONS:
        if tk != "NBIS":
            continue
        T = max(dte - DAYS_FWD, 0) / 365.0
        row = []
        for mv, ivm in [(-0.248, 0.75), (0.0, 0.75), (0.248, 0.75), (0.248, 1.0)]:
            S = SPOT[tk] * (1 + mv)
            row.append(bs_call(S, K, T, RFR, iv / 100.0 * ivm) * MULT * n)
        print(f"{tk} {K}C {acct:<12}{mark*MULT*n:>8,.0f}" + "".join(f"{v:>14,.0f}" for v in row[:3])
              + f"{row[3]:>17,.0f}")

    print("\n--- breakeven check: what NBIS spot each leg needs at expiry ---")
    for tk, K, dte, iv, n, mark, cost, acct in OPTIONS:
        entry = cost / (MULT * n)
        be = K + entry
        print(f"{tk} {K}C {acct:<10} entry {entry:>7.2f}  breakeven {be:>7.2f}  "
              f"spot {SPOT[tk]:>7.2f}  needs {be/SPOT[tk]-1:>+7.1%}")
    print()


if __name__ == "__main__":
    main()
