#!/usr/bin/env python3
"""
Pull the live NBIS option surface from moomoo OpenD ahead of the Q2 2026 print
(2026-08-12) and dump it to JSON for the strategy engine.

Writes: nbis_chain_YYYY-MM-DD.json  {spot, asof, expiries:{exp:[rows]}, stock_hist:[...]}
Row: strike, cp, bid, ask, last, iv, delta, gamma, theta, vega, oi, volume
"""
from __future__ import annotations
import json
import sys
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, OptionType, SubType

HOST, PORT = "127.0.0.1", 11111
UND = "US.NBIS"
WANT_EXP = 8          # how many expiries forward to keep


def chunk(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def num(v, d=0.0):
    """moomoo returns 'N/A' strings for fields it has no data for."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def call2(fn, *a, **kw):
    """futu returns (ret, data) or (ret, data, page_key) depending on the method."""
    r = fn(*a, **kw)
    return (r[0], r[1]) if isinstance(r, tuple) else (r, None)


def main():
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    out = {}
    try:
        ret, snap = call2(ctx.get_market_snapshot, [UND])
        if ret != RET_OK:
            raise SystemExit(f"snapshot fail: {snap}")
        spot = float(snap.iloc[0]["last_price"])
        out["spot"] = spot
        out["asof"] = str(pd.Timestamp.now())
        print(f"NBIS spot {spot}")

        ret, exps = call2(ctx.get_option_expiration_date, code=UND)
        if ret != RET_OK:
            raise SystemExit(f"expiry fail: {exps}")
        exp_list = list(exps["strike_time"])[:WANT_EXP]
        print("expiries:", exp_list)

        lo, hi = spot * 0.55, spot * 1.75
        out["expiries"] = {}
        for exp in exp_list:
            ret, chain = call2(ctx.get_option_chain,
                code=UND, start=exp, end=exp, option_type=OptionType.ALL)
            if ret != RET_OK:
                print(f"  chain fail {exp}: {chain}")
                continue
            chain = chain[(chain["strike_price"] >= lo) & (chain["strike_price"] <= hi)]
            codes = list(chain["code"])
            rows = []
            for grp in chunk(codes, 200):
                ret, s = call2(ctx.get_market_snapshot, grp)
                if ret != RET_OK:
                    print(f"  snap fail: {s}")
                    continue
                for _, r in s.iterrows():
                    rows.append(dict(
                        code=r["code"],
                        strike=num(r.get("option_strike_price")),
                        cp=str(r.get("option_type")),
                        bid=num(r.get("bid_price")),
                        ask=num(r.get("ask_price")),
                        last=num(r.get("last_price")),
                        iv=num(r.get("option_implied_volatility")),
                        delta=num(r.get("option_delta")),
                        gamma=num(r.get("option_gamma")),
                        theta=num(r.get("option_theta")),
                        vega=num(r.get("option_vega")),
                        oi=num(r.get("option_open_interest")),
                        volume=num(r.get("volume")),
                        net_oi_change=num(r.get("option_net_open_interest")),
                    ))
            out["expiries"][exp] = rows
            print(f"  {exp}: {len(rows)} contracts")

        # stock history for realized vol + earnings-day moves
        ret, k = call2(ctx.request_history_kline,
            UND, start="2024-10-01", end=pd.Timestamp.today().strftime("%Y-%m-%d"),
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
        if ret == RET_OK:
            out["stock_hist"] = k[["time_key", "open", "high", "low", "close", "volume"]] \
                .assign(time_key=lambda d: d["time_key"].astype(str)).to_dict("records")
            print(f"stock bars: {len(out['stock_hist'])} "
                  f"({out['stock_hist'][0]['time_key'][:10]} -> {out['stock_hist'][-1]['time_key'][:10]})")
        else:
            print("kline fail:", k)
            out["stock_hist"] = []
    finally:
        ctx.close()

    fn = f"nbis_chain_{pd.Timestamp.today():%Y-%m-%d}.json"
    with open(fn, "w") as fh:
        json.dump(out, fh)
    print("wrote", fn)


if __name__ == "__main__":
    main()
