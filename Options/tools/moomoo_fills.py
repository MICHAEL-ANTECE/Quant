#!/usr/bin/env python3
"""
Pull your REAL fills (成交记录) from moomoo / Futu OpenAPI, so option backtests
use the exact price you actually paid instead of a reconstruction.

Read-only: queries deal history only. It NEVER places/modifies orders and needs
NO trade-password unlock (querying is unrestricted; unlock is only for trading).

Confirmed working via SecurityFirm.FUTUINC (moomoo US) on local OpenD :11111.
Notes learned from probing:
  * history_deal_list_query caps each query at 360 days -> we auto-chunk.
  * fills come in as partial executions -> we aggregate to a weighted-avg price.
  * option codes look like US.NVDA260821C207500 (strike * 1000).

Deps: futu-api, pandas.  OpenD must be running.

CLI:
    python3 moomoo_fills.py               # last 360d, all fills
    python3 moomoo_fills.py --days 720    # go back 720 days (auto-chunked)
    python3 moomoo_fills.py --options     # only option fills, aggregated
    python3 moomoo_fills.py --legs        # print OPTION_LEGS block for the backtester
"""

from __future__ import annotations
import argparse
import re
import pandas as pd

OPT_RE = re.compile(r"^(?:\w+\.)?([A-Z.]+?)(\d{6})([CP])(\d+)$")


def decode_option_code(code: str):
    """US.NVDA260821C207500 -> dict(underlying, expiry, kind, strike) or None if not an option."""
    m = OPT_RE.match(code)
    if not m:
        return None
    underlying, yymmdd, cp, strike_code = m.groups()
    try:
        expiry = pd.to_datetime(yymmdd, format="%y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return None
    return {
        "underlying": underlying.strip("."),
        "expiry": expiry,
        "kind": "call" if cp == "C" else "put",
        "strike": int(strike_code) / 1000.0,
    }


def fetch_fills(days: int = 360, host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """All real fills across REAL accounts over the past `days`, auto-chunked by 360d."""
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm, RET_OK
    trd = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port,
                              security_firm=SecurityFirm.FUTUINC)
    frames = []
    try:
        ret, acc = trd.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"get_acc_list failed: {acc}")
        real_accs = acc[acc["trd_env"] == "REAL"]["acc_id"].tolist()
        end = pd.Timestamp.today().normalize()
        start_bound = end - pd.Timedelta(days=days)
        for aid in real_accs:
            cur_end = end
            while cur_end > start_bound:
                cur_start = max(cur_end - pd.Timedelta(days=359), start_bound)
                ret2, deals = trd.history_deal_list_query(
                    start=cur_start.strftime("%Y-%m-%d"),
                    end=cur_end.strftime("%Y-%m-%d"),
                    trd_env=TrdEnv.REAL, acc_id=aid)
                if ret2 == RET_OK and len(deals):
                    deals = deals.copy()
                    deals["acc_id"] = aid
                    frames.append(deals)
                cur_end = cur_start - pd.Timedelta(days=1)
    finally:
        try:
            trd.close()
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    keep = [c for c in ["code", "stock_name", "trd_side", "qty", "price",
                        "create_time", "acc_id"] if c in df.columns]
    return df[keep].drop_duplicates().reset_index(drop=True)


def fetch_positions(host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """Current holdings with cost basis across REAL accounts. cost_price is the exact
    entry price for a still-open position — the best backtest entry source, regardless
    of how long ago it was opened (deal history caps at 360 days; this does not)."""
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm, RET_OK
    trd = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port,
                              security_firm=SecurityFirm.FUTUINC)
    frames = []
    try:
        ret, acc = trd.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"get_acc_list failed: {acc}")
        for aid in acc[acc["trd_env"] == "REAL"]["acc_id"].tolist():
            ret2, pos = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=aid)
            if ret2 == RET_OK and len(pos):
                pos = pos.copy(); pos["acc_id"] = aid
                frames.append(pos)
    finally:
        try:
            trd.close()
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    keep = [c for c in ["code", "stock_name", "qty", "cost_price", "nominal_price",
                        "pl_ratio", "position_side", "acc_id"] if c in df.columns]
    return df[keep].reset_index(drop=True)


def legs_from_positions(pos: pd.DataFrame):
    """Emit an OPTION_LEGS block from current option HOLDINGS (uses cost_price as entry)."""
    print("OPTION_LEGS = [")
    for _, r in pos.iterrows():
        info = decode_option_code(r["code"])
        if not info:
            continue
        print(f'    {{"type": "{info["kind"]}", "strike": {info["strike"]:g}, '
              f'"expiry": "{info["expiry"]}", "contracts": {int(float(r["qty"]))}, '
              f'"mode": "moomoo", "entry_premium": {float(r["cost_price"]):.2f}}},'
              f'  # {info["underlying"]}')
    print("]")


def aggregate_options(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse partial fills into one row per option contract+side: weighted-avg price."""
    if df.empty:
        return df
    opts = df[df["code"].map(lambda c: decode_option_code(c) is not None)].copy()
    if opts.empty:
        return opts
    opts["qty"] = opts["qty"].astype(float)
    opts["price"] = opts["price"].astype(float)
    opts["notional"] = opts["qty"] * opts["price"]
    g = (opts.groupby(["code", "trd_side"], as_index=False)
              .agg(qty=("qty", "sum"), notional=("notional", "sum"),
                   first_fill=("create_time", "min"), last_fill=("create_time", "max")))
    g["avg_price"] = g["notional"] / g["qty"]
    meta = g["code"].map(decode_option_code).apply(pd.Series)
    out = pd.concat([g, meta], axis=1)
    return out[["code", "underlying", "kind", "strike", "expiry", "trd_side",
                "qty", "avg_price", "first_fill", "last_fill"]]


def print_legs(agg: pd.DataFrame):
    """Emit an OPTION_LEGS block (BUY side only) ready to paste into backtest_position.py."""
    buys = agg[agg["trd_side"] == "BUY"] if not agg.empty else agg
    print("OPTION_LEGS = [")
    for _, r in buys.iterrows():
        print(f'    {{"type": "{r.kind}", "strike": {r.strike:g}, "expiry": "{r.expiry}", '
              f'"contracts": {int(r.qty)}, "mode": "moomoo", "entry_premium": {r.avg_price:.2f}}},'
              f'  # {r.underlying}')
    print("]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=360)
    ap.add_argument("--options", action="store_true", help="only option fills, aggregated")
    ap.add_argument("--legs", action="store_true", help="print OPTION_LEGS from fills")
    ap.add_argument("--positions", action="store_true",
                    help="current holdings + cost basis (best entry source; not window-limited)")
    ap.add_argument("--position-legs", action="store_true",
                    help="print OPTION_LEGS from current option holdings' cost basis")
    args = ap.parse_args()

    if args.positions or args.position_legs:
        pos = fetch_positions()
        if pos.empty:
            print("No open positions in the connected REAL accounts.")
        elif args.position_legs:
            legs_from_positions(pos)
        else:
            print(pos.to_string(index=False))
        return

    df = fetch_fills(days=args.days)
    if df.empty:
        print("No fills returned. Widen --days, or check the account has trades / OpenD is running.")
        return
    if args.legs:
        print_legs(aggregate_options(df))
    elif args.options:
        agg = aggregate_options(df)
        print("No option fills in this window." if agg.empty else agg.to_string(index=False))
    else:
        print(df.to_string(index=False))
        n_opt = df["code"].map(lambda c: decode_option_code(c) is not None).sum()
        print(f"\n{len(df)} fills total, {n_opt} option fills. "
              f"Use --options to aggregate, --legs to emit a backtest block.")


if __name__ == "__main__":
    main()
