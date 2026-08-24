#!/usr/bin/env python3
"""
NBIS earnings option-strategy analysis — 2026-08-11.

Event:  Q2 2026 earnings 2026-08-12 (tomorrow).
Chain:  14 Aug 26 weekly, 3 DTE, from the user's moomoo option screenshot.
Spot:   $189.65 (+3.01%).  Headline IV 109.30%, HV 167.30%.

Method
------
1. Back out what the chain is pricing (ATM straddle -> implied move; the strike-by-strike
   IV term shows the skew).
2. Take the earnings distribution from the NBIS financial model (build_nbis_model.py) and
   map each fundamental scenario to a stock price.
3. Price every candidate structure at expiry under each scenario. Because expiry is only
   two days after the print, post-event extrinsic value is small — options are valued as
   intrinsic plus a residual time value at a crushed IV.
4. Rank by expected value AND by shape (worst case, probability of loss), not EV alone.

This is analysis of what the market is pricing versus what the model implies. It is not
personalised investment advice — I am not a licensed advisor.
"""
import math

SPOT = 189.65
DTE = 3.0 / 365.0
POST_EVENT_DTE = 2.0 / 365.0
IV_CRUSH = 0.70          # front-weekly IV after the print, from ~157% ATM today
R = 0.045

# ---------------------------------------------------------------- chain ----
# strike: (call_bid, call_ask, call_iv, call_delta, call_oi,
#          put_bid,  put_ask,  put_iv,  put_delta,  put_oi)
CHAIN = {
    210.0: (4.50, 4.90, 1.6427, 0.2818, 2805, 24.50, 25.00, 1.6427, -0.7193, 2091),
    207.5: (5.20, 5.55, 1.6333, 0.3072, 159, 22.55, 23.45, 1.6333, -0.6938, 88),
    205.0: (5.75, 6.25, 1.6241, 0.3343, 691, 20.60, 21.60, 1.6241, -0.6666, 381),
    202.5: (6.45, 6.90, 1.6151, 0.3631, 396, 18.85, 19.60, 1.6151, -0.6379, 112),
    200.0: (7.20, 7.50, 1.6065, 0.3933, 2803, 17.05, 18.05, 1.6065, -0.6075, 1236),
    197.5: (8.10, 8.65, 1.5982, 0.4250, 266, 15.45, 16.65, 1.5982, -0.5758, 124),
    195.0: (9.00, 9.60, 1.5904, 0.4580, 939, 13.85, 14.85, 1.5904, -0.5427, 629),
    192.5: (10.00, 10.65, 1.5832, 0.4921, 296, 12.40, 13.00, 1.5832, -0.5086, 148),
    190.0: (11.05, 11.65, 1.5766, 0.5270, 1276, 11.05, 11.50, 1.5766, -0.4736, 1602),
    187.5: (12.15, 12.95, 1.5707, 0.5624, 361, 9.75, 10.30, 1.5707, -0.4381, 585),
    185.0: (13.30, 14.15, 1.5657, 0.5981, 873, 8.55, 9.00, 1.5657, -0.4023, 1124),
    182.5: (14.45, 15.45, 1.5617, 0.6336, 152, 7.55, 8.10, 1.5617, -0.3668, 458),
    180.0: (16.25, 16.90, 1.5587, 0.6686, 3246, 6.65, 6.90, 1.5587, -0.3317, 2594),
    177.5: (17.85, 18.55, 1.5570, 0.7026, 50, 5.70, 5.95, 1.5570, -0.2976, 249),
    175.0: (19.30, 20.20, 1.5566, 0.7354, 196, 4.60, 5.10, 1.5566, -0.2648, 1319),
    172.5: (21.10, 21.90, 1.5576, 0.7664, 29, 3.85, 4.20, 1.5576, -0.2337, 235),
    170.0: (22.90, 23.85, 1.5603, 0.7955, 1756, 3.40, 3.65, 1.5603, -0.2046, 2331),
    167.5: (24.80, 25.70, 1.5647, 0.8225, 24, 2.82, 3.05, 1.5647, -0.1776, 542),
}


def mid(k, kind):
    c = CHAIN[k]
    return (c[0] + c[1]) / 2 if kind == "C" else (c[5] + c[6]) / 2


def ask(k, kind):
    return CHAIN[k][1] if kind == "C" else CHAIN[k][6]


def bid(k, kind):
    return CHAIN[k][0] if kind == "C" else CHAIN[k][5]


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, vol, kind):
    if T <= 0 or vol <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    d1 = (math.log(S / K) + (R + vol * vol / 2) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if kind == "C":
        return S * norm_cdf(d1) - K * math.exp(-R * T) * norm_cdf(d2)
    return K * math.exp(-R * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


# ------------------------------------------------------ what is priced ----
atm = 190.0
straddle_mid = mid(atm, "C") + mid(atm, "P")
straddle_ask = ask(atm, "C") + ask(atm, "P")
implied_move = straddle_mid / SPOT

print("=" * 78)
print("WHAT THE CHAIN IS PRICING  (14 Aug 26, 3 DTE, spot $189.65)")
print("=" * 78)
print(f"ATM 190 straddle   mid ${straddle_mid:.2f}   ask ${straddle_ask:.2f}")
print(f"Implied move       +/-{implied_move:6.2%}  (mid)   +/-{straddle_ask/SPOT:6.2%} (paying the ask)")
print(f"Breakevens at mid  ${SPOT*(1-implied_move):7.2f}  /  ${SPOT*(1+implied_move):7.2f}")
print(f"Headline IV 109.30%   HV 167.30%   front-week ATM IV {CHAIN[190.0][2]:.1%}")
print("\nSKEW — IV by strike (this is the single most exploitable feature on the board):")
for k in (170.0, 180.0, 190.0, 200.0, 210.0):
    print(f"   {k:6.1f}   call IV {CHAIN[k][2]:7.2%}   call delta {CHAIN[k][3]:.3f}   OI {CHAIN[k][4]:>5,}")
print(f"   -> 210 calls carry {CHAIN[210.0][2]-CHAIN[175.0][2]:+.2%} MORE vol than 175 calls.")
print("      Upside calls are the RICHEST vol on the board — an inverted (call) skew.")
print(f"      Theta at the money is {-1.9135:.2f}/day; over 3 days that is "
      f"${3*1.9135:.2f} of the ${mid(190.0,'C'):.2f} call, i.e. {3*1.9135/mid(190.0,'C'):.0%} of it.")

# -------------------------------------------------- model distribution ----
# Mapped from build_nbis_model.py: the stock trades on exit-June ARR and whether the
# $7-9B year-end ARR target survives, not on the revenue line.
SCEN = [
    ("Bear   ARR < 2.5B, FY26 guide credibility breaks", 0.25, -0.28),
    ("Base   ARR ~2.8B, guide reiterated, no surprise ", 0.55, +0.04),
    ("Bull   ARR >= 3.0B and/or FY guide raised       ", 0.20, +0.26),
]
exp_abs_move = sum(p * abs(m) for _, p, m in SCEN)
exp_move = sum(p * m for _, p, m in SCEN)

print("\n" + "=" * 78)
print("WHAT THE MODEL IMPLIES")
print("=" * 78)
for name, p, m in SCEN:
    print(f"  {name}  p={p:.0%}   move {m:+6.1%}   -> ${SPOT*(1+m):7.2f}")
print(f"\n  Expected ABSOLUTE move  {exp_abs_move:6.2%}   vs implied {implied_move:6.2%}"
      f"   -> {'LONG premium favoured' if exp_abs_move > implied_move else 'SHORT premium favoured'}")
print(f"  Expected DIRECTIONAL move {exp_move:+6.2%}   (mildly positive, but the mass is bimodal)")

# ------------------------------------------------------------ structures --
def value_at(px, legs):
    """Value a structure at post-event spot px, 2 days from expiry, crushed IV."""
    v = 0.0
    for qty, k, kind in legs:
        v += qty * bs(px, k, POST_EVENT_DTE, IV_CRUSH, kind)
    return v


def cost(legs):
    """Enter paying the ask on longs and receiving the bid on shorts."""
    c = 0.0
    for qty, k, kind in legs:
        c += qty * (ask(k, kind) if qty > 0 else bid(k, kind))
    return c


STRUCTURES = [
    ("Long 190 straddle (buy the event)",
     [(1, 190.0, "C"), (1, 190.0, "P")]),
    ("Long 200C / 180P strangle",
     [(1, 200.0, "C"), (1, 180.0, "P")]),
    ("Long 200 call (naked upside)",
     [(1, 200.0, "C")]),
    ("Long 210 call (lottery)",
     [(1, 210.0, "C")]),
    ("Call debit spread 190/210",
     [(1, 190.0, "C"), (-1, 210.0, "C")]),
    ("Call debit spread 195/210",
     [(1, 195.0, "C"), (-1, 210.0, "C")]),
    ("Deep-ITM 170 call (stock replacement, d=0.80)",
     [(1, 170.0, "C")]),
    ("Bull put spread: sell 180P / buy 170P",
     [(-1, 180.0, "P"), (1, 170.0, "P")]),
    ("Bull put spread: sell 175P / buy 165P-proxy(167.5)",
     [(-1, 175.0, "P"), (1, 167.5, "P")]),
    ("Short 190 straddle (sell the event)",
     [(-1, 190.0, "C"), (-1, 190.0, "P")]),
    ("Iron condor: 210C/180P short, 220C-proxy(210)/170P long"
     " -> sell 200C/180P, buy 210C/170P",
     [(-1, 200.0, "C"), (1, 210.0, "C"), (-1, 180.0, "P"), (1, 170.0, "P")]),
    ("RISK REVERSAL: sell 175P, buy 200C (synthetic long, financed)",
     [(-1, 175.0, "P"), (1, 200.0, "C")]),
    ("HEDGE OVERLAY on an existing long call book:"
     " sell 210C against each long",
     [(-1, 210.0, "C")]),
]

print("\n" + "=" * 78)
print("STRUCTURE COMPARISON  (per 1 contract, x100 for dollars; entry crosses the spread)")
print("=" * 78)
hdr = f"{'structure':52s}{'cost':>9s}"
for name, _, _ in SCEN:
    hdr += f"{name.split()[0]:>10s}"
hdr += f"{'EV':>10s}{'EV%':>8s}{'worst':>9s}"
print(hdr)
print("-" * 78)

results = []
for label, legs in STRUCTURES:
    c = cost(legs)
    pnls, ev = [], 0.0
    for name, p, m in SCEN:
        px = SPOT * (1 + m)
        pnl = value_at(px, legs) - c
        pnls.append(pnl)
        ev += p * pnl
    risk_base = abs(c) if abs(c) > 0.01 else max(abs(x) for x in pnls)
    line = f"{label[:52]:52s}{c:>9.2f}"
    for pnl in pnls:
        line += f"{pnl:>10.2f}"
    line += f"{ev:>10.2f}{ev/risk_base:>7.0%} {min(pnls):>8.2f}"
    print(line)
    results.append((label, c, pnls, ev, ev/risk_base, min(pnls)))

print("\n" + "=" * 78)
print("RANKED BY EXPECTED VALUE PER DOLLAR AT RISK")
print("=" * 78)
for label, c, pnls, ev, evr, worst in sorted(results, key=lambda x: -x[4]):
    print(f"  {evr:>7.0%}  EV ${ev*100:>8.0f}  worst ${worst*100:>8.0f}  "
          f"cost ${c*100:>8.0f}   {label[:46]}")

# ----------------------------------------- breakeven / probability view ----
print("\n" + "=" * 78)
print("WHERE THE MARKET'S BREAKEVENS SIT VS THE MODEL'S SCENARIOS")
print("=" * 78)
for label, legs in [("Long 190 straddle", [(1, 190.0, "C"), (1, 190.0, "P")]),
                    ("Long 200 call", [(1, 200.0, "C")]),
                    ("Call spread 190/210", [(1, 190.0, "C"), (-1, 210.0, "C")])]:
    c = cost(legs)
    lo, hi = None, None
    px = 100.0
    prev = value_at(px, legs) - c
    while px < 300:
        px += 0.25
        cur = value_at(px, legs) - c
        if prev < 0 <= cur:
            lo = px if lo is None else lo
        if prev > 0 >= cur:
            hi = px
        prev = cur
    print(f"  {label:24s} cost ${c*100:>7.0f}   breakeven "
          f"{'$%.2f' % lo if lo else '   n/a'}"
          f"{'  /  $%.2f' % hi if hi else ''}"
          f"   ({(lo/SPOT-1):+.1%} needed)" if lo else "")

print("\nScenario prices for reference:")
for name, p, m in SCEN:
    print(f"   {name.split()[0]:6s} ${SPOT*(1+m):7.2f}")
