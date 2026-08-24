#!/usr/bin/env python3
"""
TEAM (Atlassian) IV-decay calculator for a position opened BEFORE the
2026-08-06 (after close) fiscal-Q4 earnings report.

All quotes are real moomoo mid prices pulled 2026-08-04, spot 108.245.
The term structure is a textbook event structure:

    08-07 (3 DTE)   ATM IV 207.0%   <- spans earnings, almost pure event
    08-14 (10 DTE)  ATM IV 128.0%
    08-21 (17 DTE)  ATM IV 108.7%
    08-28 (24 DTE)  ATM IV  93.0%
    09-18 (45 DTE)  ATM IV  86.2%
    10-16 (73 DTE)  ATM IV  80.0%

Variance decomposition on the front week gives the earnings jump the market
is paying for; everything past 08-28 is the "no-event" baseline (~80-93%,
itself elevated because realized vol is running ~85%).

What this prints:
  1. how many vol points each expiry gives up when the event premium leaves
  2. the pure IV-decay cost, separated from theta
  3. what the position is worth the morning after, at several spot moves
  4. the move each strike needs just to break even

Run: ./.venv/bin/python team_iv_decay.py
"""

from __future__ import annotations
import math
from statistics import NormalDist

N = NormalDist().cdf
SPOT = 108.245
EARN = "2026-08-06 (after close)"
VAL_DATE_SHIFT = 3          # 08-04 -> 08-07, the reaction day
RFR = 0.04
MULT = 100
BASELINE_IV = 0.80          # post-event "no-event" level implied by Oct chain
HV = 0.8535                 # Webull realized vol at capture

# expiry -> (dte_today, {strike: (mid, iv_pct, vega, delta)})
CHAIN = {
    "2026-08-07": (3, {
        105: (9.250, 189.773, 0.0388, 0.603), 110: (7.050, 194.892, 0.0402, 0.501),
        115: (5.200, 196.399, 0.0391, 0.406), 120: (3.850, 199.934, 0.0362, 0.323),
        125: (2.625, 196.811, 0.0316, 0.244), 130: (1.650, 190.715, 0.0258, 0.173),
    }),
    "2026-08-14": (10, {
        105: (10.200, 118.886, 0.0697, 0.602), 110: (8.300, 125.560, 0.0720, 0.513),
        115: (6.300, 124.726, 0.0709, 0.428), 120: (4.650, 123.301, 0.0667, 0.347),
        125: (3.450, 123.499, 0.0605, 0.277), 130: (2.575, 124.556, 0.0535, 0.220),
    }),
    "2026-08-21": (17, {
        105: (11.350, 105.385, 0.0906, 0.601), 110: (9.250, 106.398, 0.0935, 0.521),
        115: (7.300, 106.468, 0.0927, 0.445), 120: (5.700, 106.546, 0.0889, 0.373),
        125: (4.350, 105.933, 0.0825, 0.307), 130: (3.200, 104.324, 0.0739, 0.245),
    }),
    "2026-09-18": (45, {
        105: (14.200, 82.298, 0.1466, 0.605), 110: (12.550, 86.482, 0.1509, 0.546),
        115: (10.550, 86.299, 0.1518, 0.487), 120: (8.800, 85.990, 0.1496, 0.431),
        125: (7.350, 86.398, 0.1449, 0.380), 130: (6.050, 85.630, 0.1378, 0.329),
    }),
    "2026-10-16": (73, {
        105: (17.300, 80.521, 0.1856, 0.613), 110: (15.000, 80.027, 0.1910, 0.562),
        115: (13.000, 79.832, 0.1932, 0.512), 120: (11.250, 79.752, 0.1926, 0.465),
        125: (9.700, 79.622, 0.1893, 0.419), 130: (8.300, 79.270, 0.1838, 0.376),
    }),
}
# post-earnings IV assumption per expiry (mild / hard crush), in decimals
CRUSH = {
    "2026-08-07": (0.90, 0.70),   # expires same day; IV barely matters, intrinsic rules
    "2026-08-14": (0.88, 0.72),
    "2026-08-21": (0.85, 0.70),
    "2026-09-18": (0.80, 0.68),
    "2026-10-16": (0.78, 0.68),
}


def bs_call(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return S * N(d1) - K * math.exp(-r * T) * N(d1 - sig * math.sqrt(T))


def event_premium():
    print(f"\n=== 1. how much of today's IV is pure event premium ===")
    print(f"spot ${SPOT}   earnings {EARN}   realized vol {HV:.0%}")
    print(f"{'expiry':<13}{'DTE':>5}{'ATM IV':>9}{'baseline':>10}{'event pts':>11}"
          f"{'event-only move':>17}")
    for exp, (dte, strikes) in CHAIN.items():
        iv = strikes[110][1] / 100.0
        T = dte / 365.0
        tot_var = iv * iv * T
        base_var = BASELINE_IV ** 2 * T
        ev = math.sqrt(max(tot_var - base_var, 0.0))
        print(f"{exp:<13}{dte:>5}{iv:>9.1%}{BASELINE_IV:>10.0%}"
              f"{(iv-BASELINE_IV)*100:>+11.0f}{ev:>16.1%}")
    print("event-only move = sqrt(total variance - baseline variance), i.e. the jump")
    print("the option market is charging you for, stripped of ordinary drift.")


def decay_table():
    print(f"\n=== 2. IV decay isolated from theta (spot unchanged at ${SPOT}) ===")
    print("valued on 08-07, the session after the report\n")
    print(f"{'expiry':<13}{'K':>5}{'cost':>8}{'theta only':>12}{'+ IV crush':>12}"
          f"{'IV decay $':>12}{'IV decay %':>12}{'total loss':>12}")
    for exp, (dte, strikes) in CHAIN.items():
        mild, hard = CRUSH[exp]
        for K in (110, 120, 130):
            mid, iv, vega, delta = strikes[K]
            T2 = max(dte - VAL_DATE_SHIFT, 0) / 365.0
            theta_only = bs_call(SPOT, K, T2, RFR, iv / 100.0)
            crushed = bs_call(SPOT, K, T2, RFR, mild)
            iv_cost = theta_only - crushed
            print(f"{exp:<13}{K:>5}{mid:>8.2f}{theta_only:>12.2f}{crushed:>12.2f}"
                  f"{iv_cost:>12.2f}{iv_cost/mid:>11.0%}{crushed/mid-1:>+12.0%}")
    print("\n'theta only' = 3 days gone, IV unchanged.  '+ IV crush' = same, with IV")
    print("falling to the post-event level.  The gap between them is what the vol")
    print("collapse alone costs you, before the stock has moved at all.")


def move_table():
    print(f"\n=== 3. value the morning after, by spot move (mild crush) ===")
    moves = [-0.25, -0.17, -0.10, 0.0, 0.10, 0.17, 0.25, 0.35]
    for exp, (dte, strikes) in CHAIN.items():
        mild, hard = CRUSH[exp]
        T2 = max(dte - VAL_DATE_SHIFT, 0) / 365.0
        print(f"\n{exp}  (IV {strikes[110][1]:.0f}% -> {mild:.0%}, {dte}->{dte-VAL_DATE_SHIFT} DTE)")
        print(f"{'K':>5}{'cost':>8}" + "".join(f"{m:>+9.0%}" for m in moves))
        for K in (110, 120, 130):
            mid, iv, vega, delta = strikes[K]
            row = [bs_call(SPOT * (1 + m), K, T2, RFR, mild) for m in moves]
            print(f"{K:>5}{mid:>8.2f}" + "".join(f"{v:>9.2f}" for v in row))
        print(f"{'':5}{'P&L%':>8}" + "".join(
            f"{bs_call(SPOT*(1+m),110,T2,RFR,mild)/strikes[110][0]-1:>+9.0%}" for m in moves)
            + "   <- K=110")


def breakevens():
    print(f"\n=== 4. what each strike needs, vs what the market prices ===")
    print(f"front-week implied move: +/-17.0%   (08-14: +/-18.2%, 08-21: +/-20.8%)")
    print(f"{'expiry':<13}{'K':>5}{'cost':>8}{'breakeven':>11}{'needs':>9}{'vs implied':>12}")
    for exp, (dte, strikes) in CHAIN.items():
        for K in (110, 120, 130):
            mid, iv, vega, delta = strikes[K]
            be = K + mid
            need = be / SPOT - 1
            flag = "harder" if need > 0.17 else "within"
            print(f"{exp:<13}{K:>5}{mid:>8.2f}{be:>11.2f}{need:>+9.1%}{flag:>12}")
    print("\n'harder' = the strike needs a bigger move than the option market itself")
    print("is pricing for the event.")


if __name__ == "__main__":
    event_premium()
    decay_table()
    move_table()
    breakevens()
    print()
