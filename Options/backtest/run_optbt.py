#!/usr/bin/env python3
"""
CLI for the option backtester.

    # calibrate the vol surface to today's real chain and show it
    python3 run_optbt.py NBIS --mode calib

    # compare canonical long-call policies (incl. one shaped like your book)
    python3 run_optbt.py NBIS --start 2021-01-01 --mode compare

    # sweep DTE x delta x exit-DTE
    python3 run_optbt.py NBIS --start 2021-01-01 --mode grid

    # one rule, fully specified
    python3 run_optbt.py NBIS --mode single --dte 120 --delta 0.30 --exit-dte 30 \
        --take-profit 1.0 --stop-loss -0.5 --signal dip

    # equity-only signal backtest via backtesting.py / vectorbt
    python3 run_optbt.py NBIS --mode equity --engine backtesting

Run it with the project venv so the heavy libs stay out of your anaconda base:
    ./.venv/bin/python run_optbt.py ...
"""

from __future__ import annotations

import argparse
import itertools
import sys

import pandas as pd

from optbt import get_bars, calibrate_surface, Rule, run, report, signals
from optbt.data import clean_bars

SIGNALS = {
    "always": signals.always,
    "monthly": signals.monthly,
    "dip": lambda: signals.dip(0.10, 20),
    "dip20": lambda: signals.dip(0.20, 40),
    "breakout": lambda: signals.breakout(60),
    "trend": lambda: signals.sma_cross(20, 100),
    "rsi": lambda: signals.rsi_below(35, 14),
    "cheapvol": lambda: signals.vol_percentile_below(0.40, 252),
    "trend+cheapvol": lambda: signals.all_of(signals.sma_cross(20, 100),
                                             signals.vol_percentile_below(0.50, 252)),
}


def presets() -> list[Rule]:
    """Canonical policies, ending with one shaped like the user's actual book."""
    return [
        Rule(name="A 25Δ 90DTE, exit 21DTE", target_delta=0.25, target_dte=90,
             exit_dte=21, signal=signals.always()),
        Rule(name="B 25Δ 90DTE, +100% TP / -50% SL", target_delta=0.25, target_dte=90,
             exit_dte=21, take_profit=1.0, stop_loss=-0.5, signal=signals.always()),
        Rule(name="C 40Δ 90DTE (closer to ATM)", target_delta=0.40, target_dte=90,
             exit_dte=21, signal=signals.always()),
        Rule(name="D 25Δ 90DTE, buy dips only", target_delta=0.25, target_dte=90,
             exit_dte=21, signal=SIGNALS["dip"]()),
        Rule(name="E 25Δ 90DTE, trend + cheap vol", target_delta=0.25, target_dte=90,
             exit_dte=21, signal=SIGNALS["trend+cheapvol"]()),
        Rule(name="F YOUR SHAPE: 30Δ 165DTE, hold to expiry", target_delta=0.30,
             target_dte=165, exit_dte=0, signal=signals.always()),
    ]


def grid_rules(dtes, deltas, exits) -> list[Rule]:
    out = []
    for d, dl, ex in itertools.product(dtes, deltas, exits):
        if ex >= d:
            continue
        out.append(Rule(name=f"{dl:.2f}Δ {d}DTE x{ex}", target_delta=dl,
                        target_dte=d, exit_dte=ex, signal=signals.always()))
    return out


def run_equity(bars: pd.DataFrame, engine: str) -> None:
    """Stock-only signal backtest, for sanity-checking the underlying's behaviour."""
    if engine == "backtesting":
        from backtesting import Backtest, Strategy
        from backtesting.lib import crossover

        class SmaCross(Strategy):
            fast, slow = 20, 100

            def init(self):
                c = pd.Series(self.data.Close)
                self.f = self.I(lambda: c.rolling(self.fast).mean())
                self.s = self.I(lambda: c.rolling(self.slow).mean())

            def next(self):
                if crossover(self.f, self.s):
                    self.buy()
                elif crossover(self.s, self.f):
                    self.position.close()

        df = bars.rename(columns=str.capitalize)
        bt = Backtest(df, SmaCross, cash=10_000, commission=0.0005,
                      finalize_trades=True)   # close the open trade so stats include it
        print(bt.run())
    else:
        import vectorbt as vbt
        c = bars["close"]
        fast, slow = c.rolling(20).mean(), c.rolling(100).mean()
        entries, exits = fast > slow, fast < slow
        pf = vbt.Portfolio.from_signals(c, entries, exits, init_cash=10_000,
                                        fees=0.0005, freq="1D")  # freq -> risk ratios
        print(pf.stats())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--mode", default="compare",
                    choices=["calib", "single", "compare", "grid", "equity"])
    ap.add_argument("--equity0", type=float, default=10_000)
    # single-rule params
    ap.add_argument("--dte", type=int, default=90)
    ap.add_argument("--delta", type=float, default=0.25)
    ap.add_argument("--otm-pct", type=float, default=None)
    ap.add_argument("--exit-dte", type=int, default=21)
    ap.add_argument("--take-profit", type=float, default=None)
    ap.add_argument("--stop-loss", type=float, default=None)
    ap.add_argument("--size", type=float, default=0.10)
    ap.add_argument("--max-open", type=int, default=1)
    ap.add_argument("--fixed-cash", type=float, default=None,
                    help="bet a FIXED dollar amount per trade instead of %% of equity — "
                         "strips the compounding blow-up and shows the raw per-trade edge")
    ap.add_argument("--max-contracts", type=int, default=None,
                    help="cap contracts per trade (crude liquidity limit)")
    ap.add_argument("--slippage", type=float, default=0.03)
    ap.add_argument("--signal", default="always", choices=list(SIGNALS))
    ap.add_argument("--kind", default="call", choices=["call", "put"])
    # vol surface overrides
    ap.add_argument("--vrp", type=float, default=None,
                    help="override the calibrated IV/RV ratio (stress test)")
    ap.add_argument("--engine", default="backtesting", choices=["backtesting", "vectorbt"])
    ap.add_argument("--refresh", action="store_true", help="force re-download of bars")
    ap.add_argument("--allow-gaps", action="store_true",
                    help="do NOT truncate at trading halts (dangerous: NBIS pre-2024-10-21 "
                         "is Yandex, a different company)")
    a = ap.parse_args()

    bars = clean_bars(get_bars(a.ticker, a.start, a.end, refresh=a.refresh),
                      a.ticker, truncate=not a.allow_gaps)
    if len(bars) < 260:
        print(f"[warn] only {len(bars)} bars ({len(bars)/252:.1f}y) — results will be noisy")

    if a.mode == "equity":
        run_equity(bars, a.engine)
        return

    surf = calibrate_surface(a.ticker, bars, target_dte=a.dte)
    if a.vrp is not None:
        surf.vrp = a.vrp
        surf.calibration["overridden_vrp"] = a.vrp
    print("\n" + surf.describe())
    if a.mode == "calib":
        return

    size_mode = "fixed_cash" if a.fixed_cash else "pct_equity"
    size = a.fixed_cash if a.fixed_cash else a.size

    if a.mode == "single":
        rule = Rule(kind=a.kind, target_dte=a.dte,
                    target_delta=None if a.otm_pct is not None else a.delta,
                    otm_pct=a.otm_pct, exit_dte=a.exit_dte,
                    take_profit=a.take_profit, stop_loss=a.stop_loss,
                    size_mode=size_mode, size=size, max_open=a.max_open,
                    max_contracts=a.max_contracts, slippage=a.slippage,
                    signal=SIGNALS[a.signal](), name=f"{a.ticker} {a.signal}")
        report(run(bars, surf, rule, a.equity0))
        return

    rules = presets() if a.mode == "compare" else grid_rules(
        dtes=[45, 90, 165], deltas=[0.15, 0.25, 0.40], exits=[0, 21, 45])

    results = []
    for r in rules:
        r.slippage, r.max_open, r.max_contracts = a.slippage, a.max_open, a.max_contracts
        r.size_mode, r.size = size_mode, size
        try:
            res = run(bars, surf, r, a.equity0)
        except Exception as e:
            print(f"[skip] {r.name}: {str(e)[:80]}")
            continue
        results.append((r, res))
        if a.mode == "compare":
            report(res, r.name)

    if not results:
        return
    tbl = pd.DataFrame([{
        "rule": r.name, "CAGR": res["stats"]["cagr"],
        "total": res["stats"]["total_return"], "sharpe": res["stats"]["sharpe"],
        "maxDD": res["stats"]["max_dd"], "trades": res["stats"]["n_trades"],
        "win%": res["stats"].get("win_rate", float("nan")),
        "PF": res["stats"].get("profit_factor", float("nan")),
        "final$": res["stats"]["final_equity"],
    } for r, res in results]).sort_values("CAGR", ascending=False)

    bh = results[0][1]["stats"]
    print(f"\n================ RANKING ({a.ticker} {a.start} -> "
          f"{results[0][1]['stats']['end']}) ================")
    print(f"buy & hold the stock: {bh['buyhold_return']:+.1%} total, "
          f"{bh['buyhold_cagr']:+.1%} CAGR\n")
    print(tbl.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    if tbl["total"].max() > 20 and size_mode == "pct_equity":
        print(f"\n!! COMPOUNDING ARTIFACT: top rule returns {tbl['total'].max():+,.0%}. "
              f"Percent-of-equity sizing\n   compounds tail wins into numbers no real "
              f"book could fill"
              + ("" if a.max_contracts else " (and contracts are UNCAPPED —\n   nothing "
                 "stops it 'buying' 4,000 lots of an illiquid strike)")
              + ".\n   Re-run with --fixed-cash 1000 [--max-contracts N] for the raw "
                "per-trade edge.")

    thin = tbl[tbl["trades"] < 20]
    if bh["years"] < 3 or len(thin):
        print(f"\n!! SAMPLE-SIZE WARNING: {bh['years']:.1f} years of history"
              + (f"; {len(thin)}/{len(tbl)} rules have <20 trades" if len(thin) else "")
              + ".\n   Long-call P&L is driven by a handful of tail outcomes, so these"
                "\n   rankings are mostly noise. Treat as a shape check, not evidence.")
    print("\nAll option prices are BS proxies off a calibrated surface, not real fills.")


if __name__ == "__main__":
    sys.exit(main())
