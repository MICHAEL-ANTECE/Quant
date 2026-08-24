#!/usr/bin/env python3
"""
Pull a full option surface from moomoo OpenD for LEAPS selection.

Unlike nbis_q2_chain_pull.py this keeps EVERY expiry (the January LEAPS are 15+
expiries out, so a "nearest 8" cut silently drops the only ones that matter) and
widens the strike band, because a LEAP worth buying is often 30-60% OTM.

Usage: ./.venv/bin/python leaps_chain_pull.py COHR
Writes: <ticker>_chain_YYYY-MM-DD.json
"""
from __future__ import annotations
import json
import sys
import pandas as pd
from futu import OpenQuoteContext, RET_OK, KLType, AuType, OptionType

HOST, PORT = "127.0.0.1", 11111


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def call2(fn, *a, **kw):
    r = fn(*a, **kw)
    return (r[0], r[1])


def chunk(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def main():
    tk = (sys.argv[1] if len(sys.argv) > 1 else "COHR").upper()
    und = f"US.{tk}"
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    out = {"ticker": tk}
    try:
        ret, snap = call2(ctx.get_market_snapshot, [und])
        if ret != RET_OK:
            raise SystemExit(f"snapshot fail: {snap}")
        s0 = snap.iloc[0]
        spot = float(s0["last_price"])
        out["spot"] = spot
        out["asof"] = str(pd.Timestamp.now())
        for f in ("prev_close_price", "open_price", "high_price", "low_price", "volume",
                  "turnover", "pe_ttm", "total_market_val", "amplitude"):
            out[f] = num(s0.get(f))
        print(f"{tk} spot {spot}  prev {out['prev_close_price']}  "
              f"mktcap {out['total_market_val']/1e9:.2f}B  vol {out['volume']:,.0f}")

        ret, exps = call2(ctx.get_option_expiration_date, code=und)
        if ret != RET_OK:
            raise SystemExit(f"expiry fail: {exps}")
        exp_list = list(exps["strike_time"])
        print(f"{len(exp_list)} expiries: {exp_list[0]} ... {exp_list[-1]}")

        lo, hi = spot * 0.40, spot * 2.60     # LEAPS live far OTM
        out["expiries"] = {}
        for exp in exp_list:
            ret, chain = call2(ctx.get_option_chain, code=und, start=exp, end=exp,
                               option_type=OptionType.ALL)
            if ret != RET_OK:
                print(f"  chain fail {exp}: {chain}")
                continue
            chain = chain[(chain["strike_price"] >= lo) & (chain["strike_price"] <= hi)]
            rows = []
            for grp in chunk(list(chain["code"]), 200):
                ret, sn = call2(ctx.get_market_snapshot, grp)
                if ret != RET_OK:
                    print(f"  snap fail: {sn}")
                    continue
                for _, r in sn.iterrows():
                    rows.append(dict(
                        code=r["code"], strike=num(r.get("option_strike_price")),
                        cp=str(r.get("option_type")),
                        bid=num(r.get("bid_price")), ask=num(r.get("ask_price")),
                        last=num(r.get("last_price")), iv=num(r.get("option_implied_volatility")),
                        delta=num(r.get("option_delta")), gamma=num(r.get("option_gamma")),
                        theta=num(r.get("option_theta")), vega=num(r.get("option_vega")),
                        oi=num(r.get("option_open_interest")), volume=num(r.get("volume")),
                    ))
            out["expiries"][exp] = rows
            print(f"  {exp}: {len(rows)}")

        ret, k = call2(ctx.request_history_kline, und, start="2021-01-01",
                       end=pd.Timestamp.today().strftime("%Y-%m-%d"),
                       ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1500)
        if ret == RET_OK:
            out["stock_hist"] = (k[["time_key", "open", "high", "low", "close", "volume"]]
                                 .assign(time_key=lambda d: d["time_key"].astype(str))
                                 .to_dict("records"))
            print(f"stock bars: {len(out['stock_hist'])} "
                  f"({out['stock_hist'][0]['time_key'][:10]} -> {out['stock_hist'][-1]['time_key'][:10]})")
        else:
            print("kline fail:", k)
            out["stock_hist"] = []
    finally:
        ctx.close()

    fn = f"{tk.lower()}_chain_{pd.Timestamp.today():%Y-%m-%d}.json"
    with open(fn, "w") as fh:
        json.dump(out, fh)
    print("wrote", fn)


if __name__ == "__main__":
    main()
