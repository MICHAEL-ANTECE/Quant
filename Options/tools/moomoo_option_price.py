#!/usr/bin/env python3
"""
Pull REAL option prices from your own moomoo / Futu OpenAPI (via local OpenD).

Confirmed working for CURRENT + recent daily klines. NOTE: moomoo only keeps a
SHORT history of option klines (observed ~5 trading days for a given contract),
so this is great for today's/recent marks but CANNOT reconstruct an entry price
from months ago. For a deep historical entry price use your actual fill or a
historical-option-chain source (see backtest_position.py notes).

Requires: OpenD running locally (the same gateway the moomoo skills use) and the
`futu` package. Port defaults to 11111.

CLI:  python3 moomoo_option_price.py NVDA 2026-08-21 207.5 C
Import:
    from moomoo_option_price import futu_option_code, moomoo_option_klines, moomoo_option_last
"""

from __future__ import annotations
import sys
import pandas as pd


def futu_option_code(underlying: str, expiry: str, strike: float, kind: str,
                     market: str = "US") -> str:
    """Build a Futu option code, e.g. ('NVDA','2026-08-21',207.5,'C') -> 'US.NVDA260821C207500'.
    Strike is encoded as strike*1000 with no zero-padding."""
    yymmdd = pd.Timestamp(expiry).strftime("%y%m%d")
    cp = "C" if kind.upper().startswith("C") else "P"
    strike_code = str(int(round(strike * 1000)))
    return f"{market}.{underlying.upper()}{yymmdd}{cp}{strike_code}"


def moomoo_option_klines(code: str, start: str, end: str,
                         host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """Return a DataFrame (time_key, open, high, low, close, volume) for an option code.
    Raises RuntimeError with the API message on failure."""
    from futu import OpenQuoteContext, KLType, AuType, RET_OK
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data, _ = ctx.request_history_kline(
            code, start=start, end=end,
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
        if ret != RET_OK:
            raise RuntimeError(f"moomoo error for {code}: {data}")
        cols = [c for c in ["time_key", "open", "high", "low", "close", "volume"]
                if c in data.columns]
        return data[cols].reset_index(drop=True)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def moomoo_option_last(underlying: str, expiry: str, strike: float, kind: str,
                       lookback_days: int = 10, **kw):
    """Convenience: latest available (date, close) for a contract, or None."""
    code = futu_option_code(underlying, expiry, strike, kind)
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=lookback_days)
    df = moomoo_option_klines(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), **kw)
    if df.empty:
        return None
    row = df.iloc[-1]
    return str(row["time_key"])[:10], float(row["close"])


def _cli():
    if len(sys.argv) < 5:
        print("usage: python3 moomoo_option_price.py UNDERLYING EXPIRY STRIKE C|P [START] [END]")
        print("   e.g python3 moomoo_option_price.py NVDA 2026-08-21 207.5 C")
        return
    underlying, expiry, strike, kind = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    code = futu_option_code(underlying, expiry, strike, kind)
    end = sys.argv[6] if len(sys.argv) > 6 else pd.Timestamp.today().strftime("%Y-%m-%d")
    start = sys.argv[5] if len(sys.argv) > 5 else (pd.Timestamp(end) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    print(f"contract: {code}   window: {start} -> {end}")
    df = moomoo_option_klines(code, start, end)
    if df.empty:
        print("no klines returned (moomoo keeps only a short option history).")
    else:
        print(df.to_string(index=False))
        print(f"\nlatest close: {df['close'].iloc[-1]}  on {str(df['time_key'].iloc[-1])[:10]}")


if __name__ == "__main__":
    _cli()
