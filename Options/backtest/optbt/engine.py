#!/usr/bin/env python3
"""
Event-driven backtest engine for systematic option buying/rolling.

Why hand-rolled instead of backtrader/backtesting.py/vectorbt: those all model a
single fungible instrument per symbol. An option position is born, ages, decays,
expires and gets replaced — the instrument itself changes every roll. Bolting
that onto a bar-based framework costs more code than writing the loop, and hides
the two things that actually decide the P&L here: what you paid in spread, and
what IV you assumed. Both are explicit below.

(The equity-signal side still uses those libraries — see optbt.signals and
run_optbt.py --engine backtesting/vectorbt.)

Costs are deliberately pessimistic, because deep-OTM calls on high-vol names have
brutal spreads:
    slippage = `slippage` x premium, paid on BOTH entry and exit
    commission = $0.65 per contract per side
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .pricing import bs_price, bs_greeks, strike_for_delta
from .vol import blended_rv, VolSurface

MULT = 100


# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    """A systematic option-buying policy."""
    kind: str = "call"
    # --- what to buy ---
    target_dte: int = 90
    target_delta: float | None = 0.25   # preferred: comparable across names
    otm_pct: float | None = None        # alternative: fixed % out of the money
    # --- when to get out ---
    exit_dte: int = 21                  # never hold into the theta cliff
    take_profit: float | None = None    # 1.0 = close at +100%
    stop_loss: float | None = None      # -0.5 = close at -50%
    # --- sizing ---
    size_mode: str = "pct_equity"       # "pct_equity" | "fixed_cash"
    size: float = 0.10
    max_open: int = 1
    # Liquidity reality check. Without a cap, a compounding backtest happily "buys"
    # 4,000 contracts of a deep-OTM call that traded 30 lots that day, and the
    # equity curve becomes fiction. None = uncapped (and the report will say so).
    max_contracts: int | None = None
    # --- frictions ---
    commission: float = 0.65
    slippage: float = 0.03
    # --- entry signal: (i, bars) -> bool. None = enter whenever a slot is free ---
    signal: Callable[[int, pd.DataFrame], bool] | None = None
    name: str = "rule"


def strike_increment(S: float) -> float:
    if S < 25:
        return 0.5
    if S < 100:
        return 2.5
    if S < 250:
        return 5.0
    return 10.0


def round_strike(K: float, S: float) -> float:
    inc = strike_increment(S)
    return round(K / inc) * inc


# --------------------------------------------------------------------------- #
def run(bars: pd.DataFrame, surface: VolSurface, rule: Rule,
        start_equity: float = 10_000.0, rfr: float = 0.04,
        rv_window: int | None = None) -> dict:
    """Run `rule` over `bars`. Returns dict(equity, trades, stats, rule).

    rv_window defaults to the surface's own window — the vrp level was measured
    against that estimator, so using a different one silently mis-prices every
    option in the backtest."""
    bars = bars.copy()
    bars["rv"] = blended_rv(bars, rv_window or surface.rv_window)
    bars = bars.dropna(subset=["rv"])
    if bars.empty:
        raise SystemExit("not enough bars to estimate realized vol")

    cash = start_equity
    open_pos: list[dict] = []
    trades, curve = [], []

    def mark(p, S, rv, dt):
        T = max((p["expiry"] - dt).days, 0) / 365.0
        iv = surface.iv(S, p["strike"], T, rv)
        px = bs_price(p["kind"], S, p["strike"], T, rfr, iv)
        return px, iv, T

    def close(p, px, dt, reason, S, at_expiry: bool = False):
        """at_expiry: settlement, not a trade — no spread, and no commission at all
        if it lapses worthless (nothing gets executed)."""
        nonlocal cash
        if at_expiry:
            fee = 0.0 if px <= 0 else rule.commission * p["contracts"]
            proceeds = px * MULT * p["contracts"] - fee
        else:
            proceeds = (px * (1 - rule.slippage) * MULT * p["contracts"]
                        - rule.commission * p["contracts"])
        proceeds = max(proceeds, 0.0)
        cash += proceeds
        trades.append({**{k: p[k] for k in
                          ("entry_date", "kind", "strike", "expiry", "contracts",
                           "entry_px", "entry_spot", "entry_iv", "cost")},
                       "exit_date": dt, "exit_px": px, "exit_spot": S,
                       "proceeds": proceeds, "pnl": proceeds - p["cost"],
                       "ret": proceeds / p["cost"] - 1, "reason": reason,
                       "days_held": (dt - p["entry_date"]).days})

    for i, (dt, row) in enumerate(bars.iterrows()):
        S, rv = float(row["close"]), float(row["rv"])

        # ---------- mark & exit ----------
        still: list[dict] = []
        for p in open_pos:
            dte = (p["expiry"] - dt).days
            px, iv, T = mark(p, S, rv, dt)
            if dte <= 0:
                intrinsic = (max(S - p["strike"], 0.0) if p["kind"].startswith("c")
                             else max(p["strike"] - S, 0.0))
                close(p, intrinsic, dt, "expired", S, at_expiry=True)
                continue
            ret = (px * MULT * p["contracts"]) / p["cost"] - 1
            if rule.take_profit is not None and ret >= rule.take_profit:
                close(p, px, dt, "take_profit", S)
            elif rule.stop_loss is not None and ret <= rule.stop_loss:
                close(p, px, dt, "stop_loss", S)
            elif dte <= rule.exit_dte:
                close(p, px, dt, "dte_exit", S)
            else:
                p["_mark"] = px * MULT * p["contracts"]
                still.append(p)
        open_pos = still

        # ---------- entry ----------
        equity_now = cash + sum(p["_mark"] for p in open_pos)
        want = rule.signal is None or rule.signal(i, bars)
        if want and len(open_pos) < rule.max_open:
            T = rule.target_dte / 365.0
            atm_iv = surface.iv(S, S, T, rv)
            if np.isfinite(atm_iv):
                if rule.target_delta is not None:
                    K = strike_for_delta(rule.kind, rule.target_delta, S, T, rfr, atm_iv)
                else:
                    K = S * (1 + (rule.otm_pct or 0.0) *
                             (1 if rule.kind.startswith("c") else -1))
                K = round_strike(K, S)
                iv = surface.iv(S, K, T, rv)
                fair = bs_price(rule.kind, S, K, T, rfr, iv)
                ask = fair * (1 + rule.slippage)
                if ask > 0.05:
                    budget = (equity_now * rule.size if rule.size_mode == "pct_equity"
                              else rule.size)
                    n = int(budget // (ask * MULT + rule.commission))
                    if rule.max_contracts is not None:
                        n = min(n, rule.max_contracts)
                    cost = n * (ask * MULT + rule.commission)
                    if n >= 1 and cost <= cash:
                        cash -= cost
                        open_pos.append({
                            "entry_date": dt, "kind": rule.kind, "strike": K,
                            "expiry": dt + pd.Timedelta(days=rule.target_dte),
                            "contracts": n, "entry_px": ask, "entry_spot": S,
                            "entry_iv": iv, "cost": cost,
                            "_mark": fair * MULT * n})

        curve.append({"date": dt, "spot": S, "rv": rv, "cash": cash,
                      "pos_value": sum(p["_mark"] for p in open_pos),
                      "equity": cash + sum(p["_mark"] for p in open_pos),
                      "n_open": len(open_pos)})

    eq = pd.DataFrame(curve).set_index("date")
    tr = pd.DataFrame(trades)
    return {"equity": eq, "trades": tr, "rule": rule,
            "stats": stats(eq, tr, bars, start_equity)}


# --------------------------------------------------------------------------- #
def stats(eq: pd.DataFrame, tr: pd.DataFrame, bars: pd.DataFrame,
          start_equity: float) -> dict:
    e = eq["equity"]
    years = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    r = e.pct_change().dropna()
    dd = e / e.cummax() - 1
    bh = bars["close"].loc[e.index[0]:e.index[-1]]

    out = {
        "start": str(e.index[0].date()), "end": str(e.index[-1].date()),
        "years": years,
        "final_equity": float(e.iloc[-1]),
        "total_return": float(e.iloc[-1] / start_equity - 1),
        "cagr": float((e.iloc[-1] / start_equity) ** (1 / years) - 1) if e.iloc[-1] > 0 else -1.0,
        "vol_ann": float(r.std() * np.sqrt(252)),
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else float("nan"),
        "sortino": (float(r.mean() / r[r < 0].std() * np.sqrt(252))
                    if (r < 0).any() and r[r < 0].std() > 0 else float("nan")),
        "max_dd": float(dd.min()),
        "time_in_market": float((eq["n_open"] > 0).mean()),
        "buyhold_return": float(bh.iloc[-1] / bh.iloc[0] - 1),
        "buyhold_cagr": float((bh.iloc[-1] / bh.iloc[0]) ** (1 / years) - 1),
        "n_trades": int(len(tr)),
    }
    if len(tr):
        wins, losses = tr[tr["pnl"] > 0], tr[tr["pnl"] <= 0]
        out.update({
            "win_rate": float(len(wins) / len(tr)),
            "avg_win": float(wins["ret"].mean()) if len(wins) else float("nan"),
            "avg_loss": float(losses["ret"].mean()) if len(losses) else float("nan"),
            "best": float(tr["ret"].max()), "worst": float(tr["ret"].min()),
            "profit_factor": (float(wins["pnl"].sum() / abs(losses["pnl"].sum()))
                              if len(losses) and losses["pnl"].sum() != 0 else float("inf")),
            "expectancy_pct": float(tr["ret"].mean()),
            "avg_days_held": float(tr["days_held"].mean()),
            # a total loss, however it exited — not merely "held to expiry"
            "total_wipeouts": int((tr["ret"] <= -0.99).sum()),
        })
    return out


def report(res: dict, title: str = "") -> None:
    s, tr = res["stats"], res["trades"]
    r = res["rule"]
    tgt = (f"{r.target_delta:.2f}Δ" if r.target_delta is not None else f"{r.otm_pct:+.0%} OTM")
    print(f"\n=== {title or r.name} === {r.kind} {tgt} {r.target_dte}DTE "
          f"exit@{r.exit_dte}DTE"
          + (f" TP{r.take_profit:+.0%}" if r.take_profit else "")
          + (f" SL{r.stop_loss:+.0%}" if r.stop_loss else ""))
    print(f"{s['start']} -> {s['end']} ({s['years']:.1f}y)   "
          f"equity ${s['final_equity']:,.0f}  total {s['total_return']:+.1%}  "
          f"CAGR {s['cagr']:+.1%}")
    print(f"  buy&hold stock: {s['buyhold_return']:+.1%} total / {s['buyhold_cagr']:+.1%} CAGR")
    print(f"  sharpe {s['sharpe']:.2f}  sortino {s['sortino']:.2f}  "
          f"vol {s['vol_ann']:.0%}  maxDD {s['max_dd']:.1%}  "
          f"in-market {s['time_in_market']:.0%}")
    if s["n_trades"]:
        print(f"  {s['n_trades']} trades  win {s['win_rate']:.0%}  "
              f"avg win {s['avg_win']:+.0%} / avg loss {s['avg_loss']:+.0%}  "
              f"PF {s['profit_factor']:.2f}  expectancy {s['expectancy_pct']:+.0%}")
        print(f"  best {s['best']:+.0%}  worst {s['worst']:+.0%}  "
              f"avg hold {s['avg_days_held']:.0f}d  "
              f"total wipeouts {s['total_wipeouts']}/{s['n_trades']}")
        by = tr.groupby("reason")["ret"].agg(["count", "mean"])
        print("  exits: " + "  ".join(f"{k}×{v['count']}({v['mean']:+.0%})"
                                      for k, v in by.iterrows()))
    else:
        print("  no trades")
