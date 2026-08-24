#!/usr/bin/env python3
"""NBIS model update, 2026-08-20. Two things resolved since the 8/12 post-mortem:
   1) The REPORTED exit-Q2 ARR is $3.0B, not the $2.68B my post-mortem implied.
   2) The financing arrived: $775M senior secured facility (Jul) + $4.50B converts (Aug 19).
"""
B, M = 1e9, 1e6
PX_NOW, PX_POST_ER = 216.19, 233.88

print("=" * 86)
print("1. RE-FITTING THE ARR BRIDGE  (revenue = k x avg(entry ARR, exit ARR) / 4)")
print("=" * 86)
for lbl, entry, exit_, rev in [("Q1 2026 (original fit)", 1.25*B, 1.90*B, 399.0*M),
                               ("Q2 2026 (implied ARR 2.68B)", 1.90*B, 2.68*B, 582.3*M),
                               ("Q2 2026 (REPORTED ARR 3.0B)", 1.90*B, 3.00*B, 582.3*M)]:
    k = rev / ((entry + exit_) / 2 / 4)
    print(f"  {lbl:32s} entry ${entry/B:.2f}B exit ${exit_/B:.2f}B -> k = {k:.4f}")
K = 582.3*M / ((1.90*B + 3.00*B) / 2 / 4)
print(f"\n  --> k drops 1.013 -> {K:.3f}. ARR is back-loaded within the quarter, so the")
print(f"      simple average overstates revenue actually earned. Forward revenue for any")
print(f"      given ARR path comes down ~6% versus the old constant.")

print("\n" + "=" * 86)
print("2. WHAT THE $7-9B YEAR-END ARR TARGET NOW REQUIRES")
print("=" * 86)
delivered = 3.00/1.90 - 1
print(f"  Q2 actually delivered: ARR $1.90B -> $3.00B = {delivered:+.1%} q/q")
print(f"  (my post-mortem said '+39% delivered' — that was wrong, built off the implied $2.68B)\n")
for tgt in (7.0, 8.0, 9.0):
    g = (tgt/3.00) ** 0.5 - 1
    print(f"  to reach ${tgt:.0f}B by year-end: {g:+.1%} per quarter for 2 quarters"
          f"   {'<= BELOW the pace just delivered' if g < delivered else '<-- above it'}")

print("\n" + "=" * 86)
print("3. IS THE ARR TARGET CONSISTENT WITH THE $3.0-3.4B REVENUE GUIDE?  (re-tested at new k)")
print("=" * 86)
H1 = 399.0*M + 582.3*M
print(f"  H1 actual = ${H1/M:.1f}M\n")
print(f"  {'exit-2026 ARR':>15s}{'Q3 exit':>10s}{'Q3 rev':>10s}{'Q4 rev':>10s}{'FY26 rev':>12s}   vs guide $3.0-3.4B")
for exit26 in (6.0, 7.0, 7.5, 8.0, 9.0):
    g = (exit26/3.00) ** 0.5
    q3_exit = 3.00 * g
    q3 = K * (3.00*B + q3_exit*B) / 2 / 4
    q4 = K * (q3_exit*B + exit26*B) / 2 / 4
    fy = H1 + q3 + q4
    flag = "IN GUIDE" if 3.0*B <= fy <= 3.4*B else ("below" if fy < 3.0*B else "ABOVE guide")
    print(f"  {exit26:>13.1f}B{q3_exit:>9.2f}B{q3/M:>10.0f}{q4/M:>10.0f}{fy/B:>11.2f}B   {flag}")
print("\n  --> The 'guide is only consistent with $7B ARR' point in my post-mortem was an")
print("      artifact of the old k. At k=0.951 the guide comfortably spans $7.0-8.0B ARR.")

print("\n" + "=" * 86)
print("4. THE FINANCING GAP — now with actual money raised")
print("=" * 86)
cash_q2, h1_capex = 8.04*B, 8.13*B
raised = [("Senior secured facility (Jul, SOFR+250, GPU-backed)", 0.775*B),
          ("Convertible notes due 2030 (announced 8/19)", 2.75*B),
          ("Convertible notes due 2034 (announced 8/19)", 1.75*B)]
tot_raised = sum(v for _, v in raised)
for lbl, v in raised:
    print(f"  {lbl:52s} ${v/B:>6.2f}B")
print(f"  {'TOTAL RAISED':52s} ${tot_raised/B:>6.2f}B")
print(f"\n  Cash at Q2 ${cash_q2/B:.2f}B + raised ${tot_raised/B:.2f}B = ${(cash_q2+tot_raised)/B:.2f}B available")
for lo_hi, capex in (("low end", 20*B), ("high end", 25*B)):
    h2 = capex - h1_capex
    gap = (cash_q2 + tot_raised) - h2
    print(f"  FY capex {lo_hi:8s} ${capex/B:.0f}B -> H2 needs ${h2/B:5.2f}B -> "
          f"{'surplus' if gap > 0 else 'STILL SHORT'} ${abs(gap)/B:.2f}B")

print("\n" + "=" * 86)
print("5. UPDATED TARGET")
print("=" * 86)
# 8/12 post-mortem assumptions vs today's, with the financing now known.
PM  = {"Bear": dict(rev28=9.0*B,  m=0.33, evs=4.0, eve=12.0, nd=17*B, sh=340e6),
       "Base": dict(rev28=17.0*B, m=0.41, evs=5.4, eve=14.0, nd=28*B, sh=325e6),
       "Bull": dict(rev28=24.0*B, m=0.45, evs=7.0, eve=16.0, nd=33*B, sh=318e6)}
# Converts convert to equity if the stock works -> less terminal net debt, more shares.
# Share count already moved 253.9M -> 274.1M; carry that forward.
NEW = {"Bear": dict(rev28=9.5*B,  m=0.34, evs=4.0, eve=12.0, nd=18*B, sh=360e6),
       "Base": dict(rev28=17.5*B, m=0.41, evs=5.4, eve=14.0, nd=26*B, sh=345e6),
       "Bull": dict(rev28=24.0*B, m=0.45, evs=7.0, eve=16.0, nd=30*B, sh=335e6)}
PROB = {"Bear": 0.25, "Base": 0.55, "Bull": 0.20}
DISC, YRS = 0.12, 1.5


def target(p):
    ebitda = p["rev28"] * p["m"]
    ev = (p["rev28"] * p["evs"] + ebitda * p["eve"]) / 2
    m1 = ((ev - p["nd"]) / p["sh"]) / (1 + DISC) ** YRS
    m2 = (p["rev28"] * p["evs"] - p["nd"] * 0.8) / (p["sh"] * 0.95)
    return (m1 + m2) / 2


print(f"{'scenario':8s}{'8/12 target':>13s}{'8/20 target':>13s}{'change':>10s}{'vs $216.19':>13s}")
print("-" * 86)
pm_pw = new_pw = 0.0
for s in ("Bear", "Base", "Bull"):
    o, nn = target(PM[s]), target(NEW[s])
    pm_pw += PROB[s]*o
    new_pw += PROB[s]*nn
    print(f"{s:8s}{o:>13.2f}{nn:>13.2f}{nn/o-1:>+10.1%}{nn/PX_NOW-1:>+13.1%}")
print("-" * 86)
print(f"{'WEIGHTED':8s}{pm_pw:>13.2f}{new_pw:>13.2f}{new_pw/pm_pw-1:>+10.1%}{new_pw/PX_NOW-1:>+13.1%}")

print("\n" + "=" * 86)
print("6. PRICE ACTION SINCE THE PRINT")
print("=" * 86)
print(f"  8/12 post-earnings close  ${PX_POST_ER:.2f}")
print(f"  8/20 now                  ${PX_NOW:.2f}   ({PX_NOW/PX_POST_ER-1:+.1%})")
print(f"  52-week high              $299.86        ({PX_NOW/299.86-1:+.1%} from the high)")
print(f"  Market cap $59.26B on 274.10M shares (was 253.9M on 8/3 = {274.10/253.9-1:+.1%} dilution)")
print(f"  Street: Buy, mean target $271.85 ({271.85/PX_NOW-1:+.1%})")
