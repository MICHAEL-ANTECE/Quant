#!/usr/bin/env python3
"""
Fetch a specific option contract's price on a HISTORICAL date from Alpha Vantage's
HISTORICAL_OPTIONS endpoint — the free-with-key path to deep option history that
moomoo (short kline window) and free chains (current only) can't provide.

Alpha Vantage HISTORICAL_OPTIONS returns the full option chain for a given trading
date, going back years, with last/mark/bid/ask/volume/OI/IV/greeks per contract.
Get a free key at https://www.alphavantage.co/support/#api-key and set it:
    export ALPHAVANTAGE_API_KEY=YOURKEY
Free tier is rate-limited (~25 req/day); each call = one date's whole chain.

Deps: requests (or urllib), pandas.

CLI:
    python3 alphavantage_options.py NVDA 2026-01-02 207.5 C 2026-08-21
Import:
    from alphavantage_options import av_option_price
    px = av_option_price("NVDA", "2026-01-02", 207.5, "2026-08-21", "call")
"""

from __future__ import annotations
import os
import sys
import json
import urllib.request
import urllib.parse
import pandas as pd

BASE = "https://www.alphavantage.co/query"


def _get_json(params: dict) -> dict:
    url = BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def av_chain(symbol: str, date: str, api_key: str | None = None) -> pd.DataFrame:
    """Full historical option chain for `symbol` on trading day `date` (YYYY-MM-DD)."""
    api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise SystemExit("Set ALPHAVANTAGE_API_KEY (free: alphavantage.co/support/#api-key).")
    js = _get_json({"function": "HISTORICAL_OPTIONS", "symbol": symbol,
                    "date": date, "apikey": api_key})
    if "data" not in js:
        # AV returns {'Information': ...} on rate limit / bad key / non-trading day
        msg = js.get("Information") or js.get("Note") or js.get("Error Message") or js
        raise RuntimeError(f"Alpha Vantage returned no data for {symbol} {date}: {msg}")
    df = pd.DataFrame(js["data"])
    for col in ["strike", "last", "mark", "bid", "ask", "implied_volatility"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def av_option_price(symbol: str, date: str, strike: float, expiry: str, kind: str,
                    api_key: str | None = None, field: str = "mark",
                    max_backstep: int = 5) -> dict:
    """Price of one contract on `date`. If `date` is a non-trading day, steps back up
    to `max_backstep` days. Returns dict(date_used, price, iv, bid, ask, raw_field)."""
    kind = "call" if kind.lower().startswith("c") else "put"
    d = pd.Timestamp(date)
    last_err = None
    for _ in range(max_backstep + 1):
        try:
            chain = av_chain(symbol, d.strftime("%Y-%m-%d"), api_key)
        except RuntimeError as e:
            last_err = e
            d -= pd.Timedelta(days=1)
            continue
        row = chain[(chain["strike"] == float(strike)) &
                    (chain["type"].str.lower() == kind) &
                    (chain["expiration"] == expiry)]
        if not row.empty:
            r = row.iloc[0]
            price = r.get(field)
            if pd.isna(price):
                price = r.get("last") if pd.notna(r.get("last")) else r.get("mark")
            return {"date_used": d.strftime("%Y-%m-%d"), "price": float(price),
                    "iv": float(r.get("implied_volatility")) if pd.notna(r.get("implied_volatility")) else None,
                    "bid": float(r.get("bid")) if pd.notna(r.get("bid")) else None,
                    "ask": float(r.get("ask")) if pd.notna(r.get("ask")) else None}
        d -= pd.Timedelta(days=1)  # try prior day if exact date had no such contract row
    raise RuntimeError(f"Contract {symbol} {expiry} {strike}{kind[0].upper()} not found near {date}. {last_err or ''}")


def main():
    if len(sys.argv) < 6:
        print("usage: python3 alphavantage_options.py SYMBOL DATE STRIKE C|P EXPIRY")
        print("   e.g python3 alphavantage_options.py NVDA 2026-01-02 207.5 C 2026-08-21")
        return
    symbol, date, strike, kind, expiry = sys.argv[1:6]
    res = av_option_price(symbol, date, float(strike), expiry, kind)
    print(f"{symbol} {expiry} {strike}{kind.upper()} on {res['date_used']}: "
          f"mark ${res['price']:.2f}  (bid {res['bid']} / ask {res['ask']}, IV "
          f"{res['iv']:.0%})" if res['iv'] is not None else
          f"{symbol} {expiry} {strike}{kind.upper()} on {res['date_used']}: mark ${res['price']:.2f}")


if __name__ == "__main__":
    main()
