#!/usr/bin/env python3
"""
NBIS Q2 2026 earnings (2026-08-12, pre-market) — option strategy engine.

Three things no single-number "implied move" tells you, all computed here:

  1. Term-structure fit  -> splits the surface into a BASE diffusion vol and a
     one-shot EVENT jump. Sell/buy decisions differ for the two.
  2. Realized-vol audit  -> the same split on the stock's own history, so the
     event premium is compared with NBIS earnings jumps and the base vol with
     NBIS non-event days. Comparing a 3-day IV to a 30-day HV is the usual way
     to get this backwards.
  3. Strategy menu       -> every structure priced at real bid/ask from moomoo,
     scored under the market's OWN risk-neutral density (Breeden-Litzenberger
     off the 08/14 chain) and under an empirical density built from NBIS's six
     reported quarters. The gap between the two columns IS the edge.

Legs that outlive the event are repriced with IV crushed to the fitted base
level and the live skew re-applied -- a "right direction, still lost" check.

Run: ./.venv/bin/python nbis_q2_strategy.py
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

ND = NormalDist()
N = ND.cdf
MULT = 100
RFR = 0.04
TODAY = pd.Timestamp("2026-08-11")
EARN = pd.Timestamp("2026-08-12")          # pre-market, confirmed via EDGAR cadence
CHAIN = "nbis_chain_2026-08-11.json"

# Reaction days confirmed from EDGAR 6-K press-release dates (see nbis_q2_chain_pull.py notes).
# (label, reaction date, gap %, close-to-close %)
EARNINGS_HIST = [
    ("Q4'24", "2025-02-20", -10.68, +3.17),
    ("Q1'25", "2025-05-20", +1.49, +4.21),
    ("Q2'25", "2025-08-07", +16.85, +18.55),
    ("Q3'25", "2025-11-12", +1.94, -7.69),
    ("Q4'25", "2026-02-12", -5.09, +1.26),
    ("Q1'26", "2026-05-13", +13.81, +15.72),
]


# ---------------------------------------------------------------- black-scholes
def bs(S, K, T, r, sig, cp="C"):
    if T <= 1e-9 or sig <= 1e-9:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if cp == "C":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


# ---------------------------------------------------------------- load & shape
def load():
    d = json.load(open(CHAIN))
    S = d["spot"]
    exps = {}
    for exp, rows in d["expiries"].items():
        df = pd.DataFrame(rows)
        df["cp"] = np.where(df.cp.str.contains("CALL", case=False), "C", "P")
        df["mid"] = (df.bid + df.ask) / 2
        df["spread"] = df.ask - df.bid
        df = df[(df.bid > 0) & (df.ask > 0) & (df.iv > 0)]
        df["dte"] = (pd.Timestamp(exp) - TODAY).days
        exps[exp] = df.sort_values(["cp", "strike"]).reset_index(drop=True)
    px = pd.DataFrame(d["stock_hist"])
    px["date"] = px.time_key.str[:10]
    px = px.set_index("date")[["open", "high", "low", "close"]].astype(float)
    px["ret"] = px.close.pct_change()
    return S, exps, px


def atm_row(df, S, cp):
    sub = df[df.cp == cp]
    return sub.iloc[(sub.strike - S).abs().argsort()].iloc[0]


# ------------------------------------------------------- 1. term structure fit
def fit_surface(S, exps):
    """total_var(T) = base_vol^2 * T + event_var  -> least squares on ATM IV."""
    rows = []
    for exp, df in exps.items():
        c, p = atm_row(df, S, "C"), atm_row(df, S, "P")
        T = df.dte.iloc[0] / 365.0
        iv = (c.iv + p.iv) / 200.0
        rows.append((exp, df.dte.iloc[0], T, iv, c.mid + p.mid, c.strike))
    ts = pd.DataFrame(rows, columns=["exp", "dte", "T", "atm_iv", "straddle", "K"])
    A = np.column_stack([ts["T"].values, np.ones(len(ts))])
    y = (ts.atm_iv ** 2 * ts["T"]).values
    (base_var, event_var), *_ = np.linalg.lstsq(A, y, rcond=None)
    base_vol = math.sqrt(max(base_var, 1e-6))
    event_sig = math.sqrt(max(event_var, 1e-9))
    ts["fit_iv"] = np.sqrt((base_var * ts["T"] + event_var) / ts["T"])
    ts["base_only_iv"] = base_vol
    ts["event_pts"] = (ts.atm_iv - base_vol) * 100
    return ts, base_vol, event_sig


def skew_fn(df, S, base_ref):
    """log-moneyness -> IV multiplier vs that expiry's ATM, from the live chain."""
    sub = df[(df.cp == "C") & (df.strike > S * 0.7)]
    a = atm_row(df, S, "C").iv
    x = np.log(sub.strike.values / S)
    y = sub.iv.values / a
    co = np.polyfit(x, y, 2)
    return lambda m: float(np.clip(np.polyval(co, m), 0.55, 2.2))


# ------------------------------------------------------ 2. realized vol audit
def realized(px):
    ev_days = {d for _, d, _, _ in EARNINGS_HIST}
    r = px.ret.dropna()
    out = {}
    for w in (10, 20, 30, 60, 90, 252):
        out[f"HV{w}"] = r.tail(w).std() * math.sqrt(252) * 100
    clean = r[~r.index.isin(ev_days)]
    # de-jumped: drop the fattest 4% of |moves| (news gaps), keep the diffusion
    thr = clean.abs().quantile(0.96)
    dj = clean[clean.abs() <= thr]
    out["HV252_ex_earn"] = clean.tail(252).std() * math.sqrt(252) * 100
    out["HV252_dejump"] = dj.tail(252).std() * math.sqrt(252) * 100
    out["HV60_dejump"] = dj.tail(60).std() * math.sqrt(252) * 100
    out["n_jump_days"] = int((clean.abs() > thr).tail(252).sum())
    return out, clean, dj


# ----------------------------------- 3. densities: risk-neutral vs empirical
def rn_density(df, S, T, grid):
    """Breeden-Litzenberger: q(K) = e^{rT} d2C/dK2, blended calls/puts, on `grid`."""
    c = df[df.cp == "C"].groupby("strike")["mid"].first()
    p = df[df.cp == "P"].groupby("strike")["mid"].first()
    ks = sorted(set(c.index) & set(p.index))
    # OTM-blend into one smooth call curve via put-call parity
    disc = math.exp(-RFR * T)
    call = {}
    for k in ks:
        call[k] = c[k] if k >= S else p[k] + S - k * disc
    ks = np.array(sorted(call))
    cv = np.array([call[k] for k in ks])
    iv = np.array([_implied(S, k, T, v, "C") for k, v in zip(ks, cv)])
    good = np.isfinite(iv) & (iv > 0.05)
    ks, iv = ks[good], iv[good]
    co = np.polyfit(np.log(ks / S), iv, 3)               # smooth the smile, then differentiate
    smooth_c = lambda k: bs(S, k, T, RFR, float(np.polyval(co, math.log(k / S))), "C")
    h = S * 0.004
    q = np.array([max((smooth_c(k + h) - 2 * smooth_c(k) + smooth_c(k - h)) / h ** 2, 0.0)
                  for k in grid]) * math.exp(RFR * T)
    return q / q.sum()


def _implied(S, K, T, price, cp):
    lo, hi = 1e-3, 8.0
    intrinsic = max(S - K, 0) if cp == "C" else max(K - S, 0)
    if price <= intrinsic + 1e-6:
        return np.nan
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs(S, K, T, RFR, mid, cp) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def empirical_density(S, grid, days_after, clean_rets, rng, n=200_000,
                      demean=False, pool_tail=None):
    """Event jump bootstrapped from the six reported quarters (+ kernel noise),
    then `days_after` sessions of NBIS's own non-event daily returns.

    demean=True re-centres the whole thing on zero drift. That matters: 5 of the
    6 reported quarters popped, so the raw sample carries a +6% drift that would
    make every long call look brilliant for reasons that are momentum, not edge.
    Drift-neutral is the honest way to ask "is this vol mispriced?".
    pool_tail limits the diffusion pool to the most recent N sessions (regime).
    """
    jumps = np.array([c for _, _, _, c in EARNINGS_HIST]) / 100.0
    if demean:
        jumps = jumps - jumps.mean()
    idx = rng.integers(0, len(jumps), n)
    bw = 1.06 * jumps.std(ddof=1) * len(jumps) ** (-1 / 5)   # Silverman on 6 obs
    tot = 1 + jumps[idx] + rng.normal(0, bw, n)
    pool = (clean_rets.tail(pool_tail) if pool_tail else clean_rets).values
    if demean:
        pool = pool - pool.mean()
    for _ in range(days_after):
        tot = tot * (1 + rng.choice(pool, n))
    ST = S * tot
    edges = np.concatenate([[grid[0] - (grid[1] - grid[0]) / 2],
                            (grid[:-1] + grid[1:]) / 2,
                            [grid[-1] + (grid[-1] - grid[-2]) / 2]])
    h, _ = np.histogram(ST, bins=edges)
    return h / h.sum(), ST


# ------------------------------------------------------------ 4. strategy menu
@dataclass
class Leg:
    exp: str
    cp: str
    strike: float
    qty: int          # +1 long, -1 short
    px: float = 0.0   # fill price (ask if long, bid if short)
    mid: float = 0.0
    iv: float = 0.0
    dte: int = 0


@dataclass
class Strat:
    name: str
    legs: list = field(default_factory=list)
    note: str = ""

    @property
    def cost(self):     # + = debit paid, - = credit received
        return sum(l.qty * l.px * MULT for l in self.legs)

    @property
    def cost_mid(self):
        return sum(l.qty * l.mid * MULT for l in self.legs)


def build_leg(exps, exp, cp, strike, qty):
    df = exps[exp]
    row = df[(df.cp == cp) & (df.strike == strike)]
    if row.empty:
        return None
    r = row.iloc[0]
    fill = r.ask if qty > 0 else r.bid            # cross the spread, honestly
    return Leg(exp, cp, strike, qty, float(fill), float(r.mid), float(r.iv), int(r.dte))


def value_at(leg, ST, base_vol, skews, days_fwd=3):
    """Value one leg `days_fwd` days out, post-event: IV crushed to base + skew."""
    if leg.dte <= days_fwd:
        return max(ST - leg.strike, 0.0) if leg.cp == "C" else max(leg.strike - ST, 0.0)
    T = (leg.dte - days_fwd) / 365.0
    sig = base_vol * skews[leg.exp](math.log(leg.strike / ST))
    return bs(ST, leg.strike, T, RFR, sig, leg.cp)


def payoff_curve(strat, grid, base_vol, skews):
    return np.array([sum(l.qty * value_at(l, ST, base_vol, skews) * MULT for l in strat.legs)
                     for ST in grid]) - strat.cost


# ------------------------------------------------------------------------ main
def main():
    rng = np.random.default_rng(7)
    S, exps, px = load()
    print("=" * 104)
    print(f"NBIS Q2 2026 earnings engine   spot ${S:.2f}   event {EARN:%Y-%m-%d} (pre-mkt)   "
          f"data {CHAIN}")
    print("=" * 104)

    # ---- 1 term structure
    ts, base_vol, event_sig = fit_surface(S, exps)
    print("\n[1] IV TERM STRUCTURE -> base diffusion vs one-shot event")
    print(f"{'expiry':<12}{'dte':>5}{'ATM IV':>9}{'fitted':>9}{'straddle':>10}{'str/S':>8}"
          f"{'event pts':>11}")
    for _, r in ts.iterrows():
        print(f"{r.exp:<12}{r.dte:>5.0f}{r.atm_iv*100:>8.1f}%{r.fit_iv*100:>8.1f}%"
              f"{r.straddle:>10.2f}{r.straddle/S:>7.1%}{r.event_pts:>10.0f}p")
    print(f"\n  fit:  base vol = {base_vol*100:.1f}%   event jump = +/-{event_sig*100:.2f}% (1 sigma)")
    print(f"  the 08/14 straddle at {ts.iloc[0].straddle:.2f} = "
          f"{event_sig*100:.1f}% event  +  {base_vol*math.sqrt(3/365)*100:.1f}% of 3-day drift")

    # ---- 2 realized audit
    rv, clean, dj = realized(px)
    print("\n[2] REALIZED VOL AUDIT  (is the surface actually expensive?)")
    print("  " + "  ".join(f"{k}={v:.0f}%" for k, v in rv.items() if k.startswith("HV")))
    print(f"  -> base vol implied {base_vol*100:.0f}%  vs  de-jumped realized "
          f"{rv['HV252_dejump']:.0f}% (1y) / {rv['HV60_dejump']:.0f}% (60d)   "
          f"VRP = {base_vol*100/rv['HV252_dejump']:.2f}x")
    jm = np.array([c for _, _, _, c in EARNINGS_HIST])
    print(f"\n  NBIS earnings reactions (EDGAR-confirmed, close-to-close):")
    for lbl, d, g, c in EARNINGS_HIST:
        print(f"    {lbl}  {d}   gap {g:+6.2f}%   close {c:+6.2f}%")
    print(f"    n=6  mean {jm.mean():+.2f}%  mean|move| {np.abs(jm).mean():.2f}%  "
          f"RMS {math.sqrt((jm**2).mean()):.2f}%  up {int((jm>0).sum())}/6")
    print(f"  -> market prices the jump at +/-{event_sig*100:.2f}%; history RMS is "
          f"{math.sqrt((jm**2).mean()):.2f}%  "
          f"(event premium {event_sig*100/math.sqrt((jm**2).mean())-1:+.0%})")

    # ---- 3 densities
    # recent regime census -- how often does NBIS gap on NON-earnings news?
    ev_days = {d for _, d, _, _ in EARNINGS_HIST}
    r60 = px.ret.dropna().tail(60)
    big = r60[(r60.abs() > 0.10) & (~r60.index.isin(ev_days))]
    print(f"\n  regime check: last 60 sessions had {len(big)} NON-earnings days beyond +/-10%:")
    print("    " + "  ".join(f"{d[5:]} {v:+.0%}" for d, v in big.items()))
    print(f"    -> the base-vol leg of this surface has to cover THOSE too, "
          f"not just quiet drift")

    grid = np.linspace(S * 0.45, S * 1.85, 281)
    T14 = 3 / 365
    q_rn = rn_density(exps["2026-08-14"], S, T14, grid)
    q_raw, _ = empirical_density(S, grid, 2, clean, rng)                    # history as-is
    q_em, _ = empirical_density(S, grid, 2, clean, rng, demean=True)        # drift-neutral
    q_rg, _ = empirical_density(S, grid, 2, clean, rng, demean=True, pool_tail=60)
    print("\n[3] DISTRIBUTION FOR 08/14  (market's own vs NBIS history)")
    hdr = ["P(<-20%)", "P(-20..-10)", "P(-10..0)", "P(0..+10)", "P(+10..+20)", "P(>+20%)"]
    cuts = [0.80, 0.90, 1.00, 1.10, 1.20]
    def buckets(q):
        b, prev = [], 0
        for c in cuts:
            i = np.searchsorted(grid, S * c)
            b.append(q[prev:i].sum()); prev = i
        b.append(q[prev:].sum())
        return b
    print("                   " + "".join(f"{h:>13}" for h in hdr))
    for lbl, q in [("market (RN)", q_rn), ("hist as-is", q_raw),
                   ("hist drift-neutral", q_em), ("hist recent regime", q_rg)]:
        print(f"  {lbl:<17}" + "".join(f"{v:>12.1%} " for v in buckets(q)))
    print()
    for lbl, q in [("market (RN)", q_rn), ("hist as-is", q_raw),
                   ("hist drift-neutral", q_em), ("hist recent regime", q_rg)]:
        e = (grid * q).sum()
        print(f"  {lbl:<20} E[S] {e:7.2f} ({e/S-1:+6.2%})   "
              f"E|move| {(np.abs(grid-S)*q).sum()/S:6.2%}   "
              f"sigma {math.sqrt((grid**2*q).sum()-e**2)/S:6.2%}")
    print("  -> if 'drift-neutral' sigma ~= market sigma, the front week is FAIRLY PRICED and")
    print("     there is no vol edge either way; any P&L then comes from direction, not from vol.")

    # ---- 4 strategies
    skews = {e: skew_fn(df, S, base_vol) for e, df in exps.items()}
    E1, E2, E3, E4 = "2026-08-14", "2026-08-21", "2026-09-18", "2026-10-16"
    K = lambda x: float(min(exps[E1].strike, key=lambda s: abs(s - x)))
    K2 = lambda e, x: float(min(exps[e].strike, key=lambda s: abs(s - x)))
    atm = K(S)

    specs = [
        ("long 190C 08/14", [(E1, "C", atm, +1)], "naked front call - the default"),
        ("long 210C 08/14", [(E1, "C", K(S * 1.10), +1)], "OTM lottery"),
        ("long straddle 08/14", [(E1, "C", atm, +1), (E1, "P", atm, +1)], "pure event vol buy"),
        ("long strangle 170/215 08/14",
         [(E1, "P", K(S * 0.89), +1), (E1, "C", K(S * 1.13), +1)], "cheaper vol buy"),
        ("short strangle 165/220 08/14",
         [(E1, "P", K(S * 0.87), -1), (E1, "C", K(S * 1.155), -1)], "the 8/10 floor trade"),
        ("iron condor 160/170/215/225 08/14",
         [(E1, "P", K(S * 0.84), +1), (E1, "P", K(S * 0.89), -1),
          (E1, "C", K(S * 1.13), -1), (E1, "C", K(S * 1.18), +1)], "defined-risk premium sale"),
        ("call spread 190/215 08/14", [(E1, "C", atm, +1), (E1, "C", K(S * 1.13), -1)], ""),
        ("call spread 200/230 08/21",
         [(E2, "C", K2(E2, S * 1.05), +1), (E2, "C", K2(E2, S * 1.21), -1)], ""),
        ("long 190C 09/18", [(E3, "C", K2(E3, S), +1)], "buy the CHEAP month"),
        ("long 210C 09/18", [(E3, "C", K2(E3, S * 1.10), +1)], ""),
        ("long straddle 09/18", [(E3, "C", K2(E3, S), +1), (E3, "P", K2(E3, S), +1)], ""),
        ("calendar 190C  s08/14 l09/18",
         [(E1, "C", atm, -1), (E3, "C", K2(E3, S), +1)], "sell event, own base vol"),
        ("calendar 190C  s08/14 l10/16",
         [(E1, "C", atm, -1), (E4, "C", K2(E4, S), +1)], ""),
        ("double calendar 175P/210C s08/14 l09/18",
         [(E1, "P", K(S * 0.92), -1), (E3, "P", K2(E3, S * 0.92), +1),
          (E1, "C", K(S * 1.10), -1), (E3, "C", K2(E3, S * 1.10), +1)], ""),
        ("diagonal  s08/14 210C  l09/18 190C",
         [(E1, "C", K(S * 1.10), -1), (E3, "C", K2(E3, S), +1)], "keeps upside, funded"),
        ("diagonal  s08/14 215C  l10/16 200C",
         [(E1, "C", K(S * 1.13), -1), (E4, "C", K2(E4, S * 1.05), +1)], ""),
        ("ratio 1x2 190/215C 08/14",
         [(E1, "C", atm, +1), (E1, "C", K(S * 1.13), -2)], "cheap but tail-naked"),
        ("risk reversal  s170P l215C 08/14",
         [(E1, "P", K(S * 0.89), -1), (E1, "C", K(S * 1.13), +1)], "synthetic long, skew-funded"),
        ("put spread 180/160 08/14",
         [(E1, "P", K(S * 0.945), +1), (E1, "P", K(S * 0.84), -1)], "downside"),
        ("jade lizard  s170P s215C l230C 08/14",
         [(E1, "P", K(S * 0.89), -1), (E1, "C", K(S * 1.13), -1), (E1, "C", K(S * 1.21), +1)],
         "no upside risk if credit > width"),
    ]

    strats = []
    for name, legs, note in specs:
        L = [build_leg(exps, e, cp, k, q) for e, cp, k, q in legs]
        if any(x is None for x in L):
            print(f"  !! skipped {name} (missing strike)")
            continue
        strats.append(Strat(name, L, note))

    print("\n[4] STRATEGY MENU  — filled at real bid/ask, scored on every density")
    print(f"{'structure':<40}{'net$':>8}{'EVmkt':>7}{'EVdrift':>8}{'EVregime':>9}{'EVraw':>7}"
          f"{'vol-edge':>9}{'PoP':>6}{'maxloss':>9}")
    print("-" * 104)
    res = []
    for s in strats:
        pay = payoff_curve(s, grid, base_vol, skews)
        ev_rn = float((pay * q_rn).sum())
        ev_dn = float((pay * q_em).sum())       # drift-neutral: pure vol/shape edge
        ev_rg = float((pay * q_rg).sum())
        ev_rw = float((pay * q_raw).sum())      # includes the +6% historical drift
        risk = max(abs(s.cost), abs(pay.min()), 1.0)
        pop = float(q_em[pay > 0].sum())
        res.append(dict(s=s, rn=ev_rn, dn=ev_dn, rg=ev_rg, rw=ev_rw,
                        edge=ev_dn - ev_rn, pop=pop, worst=pay.min(), risk=risk))
    res.sort(key=lambda x: -x["dn"] / x["risk"])
    for r in res:
        print(f"{r['s'].name:<40}{r['s'].cost:>8,.0f}{r['rn']:>7,.0f}{r['dn']:>8,.0f}"
              f"{r['rg']:>9,.0f}{r['rw']:>7,.0f}{r['edge']:>9,.0f}{r['pop']:>6.0%}"
              f"{r['worst']:>9,.0f}")
    print("-" * 104)
    print("net$: + = debit paid, - = credit received.  EV = $ per 1 structure after crossing the spread.")
    print("EVmkt = market's own density (must be ~0 minus slippage; it is the no-arb sanity check).")
    print("EVdrift = NBIS history re-centred to zero drift -> this column IS the volatility edge.")
    print("EVregime = same, diffusion resampled from the last 60 sessions only.")
    print("EVraw = history as-is, carrying the +5.9% mean earnings pop -> a DIRECTIONAL bet, not edge.")
    print("maxloss is grid-bounded (-55%/+85%), so naked-short rows understate the true tail.")

    # ---- 5 scenario grid on the top structures
    print("\n[5] P&L GRID for the 5 best by EV/risk   (08/14, IV crushed to base for live legs)")
    moves = [-0.30, -0.20, -0.12, -0.06, 0.0, 0.06, 0.12, 0.20, 0.30]
    print(f"{'structure':<40}" + "".join(f"{m:>+8.0%}" for m in moves))
    for r in res[:5]:
        s = r["s"]
        vals = [sum(l.qty * value_at(l, S * (1 + m), base_vol, skews) * MULT for l in s.legs)
                - s.cost for m in moves]
        print(f"{s.name:<40}" + "".join(f"{v:>8,.0f}" for v in vals))

    print("\n[6] LEG DETAIL for the top structure")
    top = res[0]["s"]
    for l in top.legs:
        print(f"   {'BUY ' if l.qty>0 else 'SELL'} {abs(l.qty)}x {l.exp} {l.strike:g}{l.cp}"
              f"   fill {l.px:6.2f}  (mid {l.mid:6.2f})  IV {l.iv:6.1f}%  dte {l.dte}")
    print(f"   net {'debit' if top.cost>0 else 'credit'} ${abs(top.cost):,.0f} "
          f"(at mid ${abs(top.cost_mid):,.0f})")
    print()


if __name__ == "__main__":
    main()
