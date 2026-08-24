#!/usr/bin/env python3
"""
NBIS Q2 2026 post-mortem — 2026-08-12, reported before the open.

Compares the build_nbis_model.py forecast (run 2026-08-03) against actuals, re-derives the
model's two calibration constants, re-checks the ARR bridge to the $7-9B year-end target,
and re-runs the valuation with the funding assumptions the print actually implies.

Also scores yesterday's option-chain analysis (nbis_option_strategy.py) against the realised
move, because the whole point of writing down a distribution is to check it afterwards.
"""
M, B = 1e6, 1e9

# ------------------------------------------------------------- actuals ----
ACT = dict(
    revenue=582.3*M,
    adj_ebitda=236.2*M,
    adj_ebitda_margin=0.406,
    gaap_net=-190.4*M,
    adj_net=-33.2*M,
    eps_diluted=-0.68,
    capex=5.66*B,
    cash=8.04*B,
    ppe=13.05*B,
    yoy=4.54,
)
CONSENSUS = dict(revenue=569.89*M, eps=-0.72)

# forecast as written on 2026-08-03
FCST = {
    "Bear": dict(revenue=538*M, adj_ebitda=211*M, margin=0.39, eps=-0.59, arr_exit=2.35*B),
    "Base": dict(revenue=595*M, adj_ebitda=260*M, margin=0.44, eps=-0.32, arr_exit=2.80*B),
    "Bull": dict(revenue=646*M, adj_ebitda=302*M, margin=0.47, eps=-0.10, arr_exit=3.20*B),
}

PX_PRE, PX_NOW = 189.65, 233.88

print("=" * 84)
print("NBIS Q2 2026 POST-MORTEM   reported 2026-08-12 pre-market   stock $233.88 (+21.0%)")
print("=" * 84)
print(f"\n{'metric':26s}{'Bear':>11s}{'Base':>11s}{'Bull':>11s}{'ACTUAL':>12s}{'vs Base':>11s}")
print("-" * 84)
for label, key, fmt in [("Revenue ($M)", "revenue", "m"),
                        ("Adj. EBITDA ($M)", "adj_ebitda", "m"),
                        ("Adj. EBITDA margin", "margin", "p")]:
    a = ACT["adj_ebitda_margin"] if key == "margin" else ACT[key]
    row = f"{label:26s}"
    for s in ("Bear", "Base", "Bull"):
        v = FCST[s][key]
        row += f"{v:>11.1%}" if fmt == "p" else f"{v/M:>11,.1f}"
    row += f"{a:>12.1%}" if fmt == "p" else f"{a/M:>12,.1f}"
    row += f"{a/FCST['Base'][key]-1:>+11.1%}"
    print(row)

print(f"\n{'Revenue vs consensus':26s}{'':33s}{ACT['revenue']/CONSENSUS['revenue']-1:>+12.1%}")
print(f"{'EPS vs consensus':26s}{'':33s}{ACT['eps_diluted']:>12.2f}  (est {CONSENSUS['eps']:.2f}, beat by "
      f"${ACT['eps_diluted']-CONSENSUS['eps']:+.2f})")

# ------------------------------------------------- recalibrate the bridge --
# Model: revenue = k x average(entry ARR, exit ARR) / 4.  Entry ARR (end-Q1) = $1.92B.
ARR_ENTRY = 1.92*B
K_OLD = 1.0133
implied_exit = ACT["revenue"] / K_OLD * 8 - ARR_ENTRY

print("\n" + "=" * 84)
print("CALIBRATION CHECK")
print("=" * 84)
print(f"  ARR bridge constant k was {K_OLD:.4f} (fitted on Q1 2026).")
print(f"  Holding k, the Q2 revenue of ${ACT['revenue']/M:.1f}M implies an exit-June ARR of "
      f"${implied_exit/B:.2f}B.")
print(f"  The model's base case assumed ${FCST['Base']['arr_exit']/B:.2f}B -> the ramp came in "
      f"{implied_exit/FCST['Base']['arr_exit']-1:+.1%} vs base, still well above the bear "
      f"${FCST['Bear']['arr_exit']/B:.2f}B.")
print("  NOTE: this is IMPLIED by the bridge, not the company's reported ARR figure. Replace it")
print("        when the reported number is in hand — that also re-fits k.")
for arr_reported in (2.60*B, 2.70*B, 2.80*B, 2.90*B):
    k = ACT["revenue"] / ((ARR_ENTRY + arr_reported) / 2 / 4)
    print(f"        if reported exit ARR = ${arr_reported/B:.2f}B  ->  k re-fits to {k:.4f}")

# ------------------------------------------------ the bridge to year-end ---
print("\n" + "=" * 84)
print("THE BRIDGE TO THE $7-9B YEAR-END ARR TARGET — now the whole story")
print("=" * 84)
for target in (7.0*B, 8.0*B, 9.0*B):
    g = (target / implied_exit) ** 0.5 - 1
    print(f"  From ${implied_exit/B:.2f}B to ${target/B:.1f}B in 2 quarters requires "
          f"{g:+.1%} per quarter compounded.")
print(f"\n  For reference, the ramp just delivered was "
      f"{implied_exit/ARR_ENTRY-1:+.1%} q/q. The required pace is materially faster.")

# implied H2 revenue if the target is met
def rev_from_arr(entry, exit_, k=K_OLD):
    return k * (entry + exit_) / 2 / 4


for q3_exit, q4_exit, tag in [(4.0*B, 7.0*B, "low end $7B"),
                              (4.4*B, 8.0*B, "mid $8B"),
                              (4.8*B, 9.0*B, "high end $9B")]:
    q3 = rev_from_arr(implied_exit, q3_exit)
    q4 = rev_from_arr(q3_exit, q4_exit)
    fy = 399*M + ACT["revenue"] + q3 + q4
    print(f"  {tag:14s} -> Q3 ${q3/M:6.0f}M, Q4 ${q4/M:7.0f}M, FY2026 ${fy/B:.2f}B "
          f"({'inside' if 3.0*B <= fy <= 3.4*B else 'OUTSIDE'} the $3.0-3.4B guide)")

# ----------------------------------------------------- the funding hole ----
print("\n" + "=" * 84)
print("THE FUNDING GAP — this is what actually changed today")
print("=" * 84)
capex_h1 = 2.47*B + ACT["capex"]
print(f"  Q1 capex $2.47B + Q2 capex ${ACT['capex']/B:.2f}B = ${capex_h1/B:.2f}B spent in H1.")
print(f"  Q2 capex alone was 10x the year-ago quarter ($510.6M).")
for guide in (20*B, 25*B):
    print(f"  FY2026 capex guide ${guide/B:.0f}B  ->  H2 still to spend ${(guide-capex_h1)/B:5.2f}B "
          f"(${(guide-capex_h1)/2/B:.2f}B per quarter)")
print(f"\n  Cash on hand at 30-Jun: ${ACT['cash']/B:.2f}B.")
print(f"  H2 capex at the LOW end of guide (${(20*B-capex_h1)/B:.2f}B) already exceeds cash by "
      f"${(20*B-capex_h1-ACT['cash'])/B:.2f}B — before any opex or interest.")
print("  The company also spent $3.41B more on infrastructure than operations generated.")
print("  => Large external financing in H2 is not a risk scenario, it is arithmetic.")
print("     Customer prepayments (the +$3.2B 'other working capital' line in Q1) cover part of it;")
print("     the rest is converts / term loans / asset-backed SPVs / equity. Model dilution up.")

# --------------------------------------------------------- revaluation ----
print("\n" + "=" * 84)
print("REVISED VALUATION")
print("=" * 84)
NET_CASH_NOW = ACT["cash"]

OLD = {"Bear": dict(rev28=9.0*B, m=0.32, evs=4.0, eve=12.0, nd=13*B, sh=320e6),
       "Base": dict(rev28=17.0*B, m=0.40, evs=5.4, eve=14.0, nd=22*B, sh=305e6),
       "Bull": dict(rev28=24.0*B, m=0.44, evs=7.0, eve=16.0, nd=26*B, sh=300e6)}
# Q2 confirms the operating model (adj EBITDA positive at 40.6%, a quarter early) but makes the
# balance sheet worse: capex is running ~2.3x the Q1 pace. Raise net debt and share count.
NEW = {"Bear": dict(rev28=9.0*B, m=0.33, evs=4.0, eve=12.0, nd=17*B, sh=340e6),
       "Base": dict(rev28=17.0*B, m=0.41, evs=5.4, eve=14.0, nd=28*B, sh=325e6),
       "Bull": dict(rev28=24.0*B, m=0.45, evs=7.0, eve=16.0, nd=33*B, sh=318e6)}
PROB = {"Bear": 0.25, "Base": 0.55, "Bull": 0.20}
DISC, YRS = 0.12, 1.5


def target(p):
    ebitda = p["rev28"] * p["m"]
    ev = (p["rev28"] * p["evs"] + ebitda * p["eve"]) / 2
    px28 = (ev - p["nd"]) / p["sh"]
    m1 = px28 / (1 + DISC) ** YRS
    m2 = (p["rev28"] * p["evs"] - p["nd"] * 0.8) / (p["sh"] * 0.95)
    return (m1 + m2) / 2


print(f"{'scenario':8s}{'old target':>13s}{'new target':>13s}{'change':>10s}{'vs $233.88':>13s}")
print("-" * 84)
old_pw = new_pw = 0.0
for s in ("Bear", "Base", "Bull"):
    o, nn = target(OLD[s]), target(NEW[s])
    old_pw += PROB[s] * o
    new_pw += PROB[s] * nn
    print(f"{s:8s}{o:>13.2f}{nn:>13.2f}{nn/o-1:>+10.1%}{nn/PX_NOW-1:>+13.1%}")
print("-" * 84)
print(f"{'WEIGHTED':8s}{old_pw:>13.2f}{new_pw:>13.2f}{new_pw/old_pw-1:>+10.1%}{new_pw/PX_NOW-1:>+13.1%}")
print(f"\n  Net-debt sensitivity: every extra $5B of FY2028 net debt is worth about "
      f"${5*B/NEW['Base']['sh']:.0f} of base-case share price.")
print(f"  Share-count sensitivity: every extra 20M shares is worth about "
      f"${target(NEW['Base'])-target(dict(NEW['Base'], sh=NEW['Base']['sh']+20e6)):.0f}.")

# ------------------------------------------------- score the option call ---
print("\n" + "=" * 84)
print("SCORING YESTERDAY'S OPTION ANALYSIS")
print("=" * 84)
realised = PX_NOW / PX_PRE - 1
print(f"  Realised move {realised:+.2%} vs implied +/-11.93% -> the move EXCEEDED the straddle.")
print(f"  Model scenarios: bear -28% (${PX_PRE*0.72:.2f}), base +4% (${PX_PRE*1.04:.2f}), "
      f"bull +26% (${PX_PRE*1.26:.2f}).")
print(f"  Actual ${PX_NOW:.2f} landed almost exactly on the BULL case (${PX_PRE*1.26:.2f}).")
print(f"  Upper straddle breakeven was $212.27 — cleared by ${PX_NOW-212.27:.2f}.\n")
for label, cost, legs in [
        ("Long 200 call", 7.50, [(1, 200.0)]),
        ("Long 210 call", 4.90, [(1, 210.0)]),
        ("Call spread 190/210", 7.15, [(1, 190.0), (-1, 210.0)]),
        ("Long 190 straddle", 23.15, [(1, 190.0)]),
        ("SOLD 210 call (the overlay)", -4.50, [(-1, 210.0)])]:
    intrinsic = sum(q * max(0.0, PX_NOW - k) for q, k in legs)
    if label == "Long 190 straddle":
        intrinsic = max(0.0, PX_NOW - 190.0)
    pnl = intrinsic - cost
    print(f"  {label:28s} cost ${cost*100:>7.0f}  value ${intrinsic*100:>7.0f}  "
          f"P&L ${pnl*100:>+8.0f}  ({pnl/abs(cost):>+7.0%})")
print("\n  The EV argument against selling the 210 overlay held: capping the upside would have")
print("  cost about $1,938 per contract. Note this is one observation, not a validated edge —")
print("  the same distribution said 55% of the time the long-premium structures lose.")
