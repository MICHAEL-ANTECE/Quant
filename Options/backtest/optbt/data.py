#!/usr/bin/env python3
"""
Data layer: US stock/ETF history from YOUR moomoo OpenD, cached to local parquet.

Why moomoo and not yfinance: OpenD is already running, it is the same source your
terminal shows, and it goes deep (verified: ASX/SPY daily to 2006-07, NBIS to
2011, BE to 2018). yfinance is kept as an automatic fallback if OpenD is down.

The public API is deliberately tiny and source-agnostic so a paid option-chain
vendor (Polygon / Alpha Vantage Premium) can be dropped in later without the
backtest engine changing:

    from optbt.data import get_bars
    df = get_bars("NBIS", "2020-01-01")      # -> DataFrame indexed by date,
                                             #    columns open/high/low/close/volume

Cache lives in optbt/_cache/<TICKER>_<ktype>.parquet and is refreshed
incrementally — re-running only pulls the missing tail.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")
OPEND_HOST, OPEND_PORT = "127.0.0.1", 11111
MAX_PER_REQ = 1000  # OpenD hard cap per request; we paginate with page_req_key

os.makedirs(CACHE_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
#  moomoo / Futu
# --------------------------------------------------------------------------- #
def _futu_ctx():
    from futu import OpenQuoteContext
    return OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)


def fetch_moomoo(ticker: str, start: str, end: str, ktype: str = "K_DAY",
                 market: str = "US") -> pd.DataFrame:
    """Paginated daily/intraday klines. OpenD caps a single request at 1000 bars,
    so we follow page_req_key until exhausted."""
    from futu import KLType, AuType, RET_OK

    code = f"{market}.{ticker.upper()}"
    ctx = _futu_ctx()
    frames, page_key = [], None
    try:
        while True:
            ret, data, page_key = ctx.request_history_kline(
                code, start=start, end=end,
                ktype=getattr(KLType, ktype), autype=AuType.QFQ,
                max_count=MAX_PER_REQ, page_req_key=page_key)
            if ret != RET_OK:
                raise RuntimeError(f"moomoo error for {code}: {data}")
            frames.append(data)
            if page_key is None:
                break
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["time_key"].str[:10])
    keep = ["date", "open", "high", "low", "close", "volume"]
    return df[keep].drop_duplicates("date").set_index("date").sort_index()


def fetch_yfinance(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Fallback when OpenD is not running."""
    import yfinance as yf
    df = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned nothing for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df.sort_index()


# --------------------------------------------------------------------------- #
#  cached accessor
# --------------------------------------------------------------------------- #
def _cache_path(ticker: str, ktype: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker.upper()}_{ktype}.parquet")


def get_bars(ticker: str, start: str = "2010-01-01", end: str | None = None,
             ktype: str = "K_DAY", refresh: bool = False) -> pd.DataFrame:
    """Bars for `ticker`, served from local parquet, topped up from moomoo as needed.

    refresh=True forces a full re-download (use after a split/ticker change)."""
    ticker = ticker.upper()
    end = end or date.today().isoformat()
    path = _cache_path(ticker, ktype)

    cached = pd.DataFrame()
    if os.path.exists(path) and not refresh:
        cached = pd.read_parquet(path)

    need_from = start
    if not cached.empty:
        have_lo, have_hi = cached.index.min(), cached.index.max()
        covered_head = have_lo <= pd.Timestamp(start)
        covered_tail = have_hi >= pd.Timestamp(end) - pd.Timedelta(days=4)
        if covered_head and covered_tail:
            return cached.loc[str(start):str(end)].copy()
        # only ever extend the tail incrementally; a start earlier than the cache
        # forces a full refetch (cheap enough, and keeps the file contiguous)
        need_from = start if not covered_head else (have_hi + timedelta(days=1)).date().isoformat()

    try:
        fresh = fetch_moomoo(ticker, need_from, end, ktype)
        src = "moomoo"
    except Exception as e:
        print(f"[data] moomoo failed ({str(e)[:80]}) -> yfinance fallback")
        fresh = fetch_yfinance(ticker, need_from, end)
        src = "yfinance"

    out = (pd.concat([cached, fresh]) if not cached.empty else fresh)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.to_parquet(path)
    print(f"[data] {ticker} {ktype}: {len(out)} bars "
          f"{out.index.min().date()} -> {out.index.max().date()} (+{len(fresh)} from {src})")
    return out.loc[str(start):str(end)].copy()


def find_gaps(bars: pd.DataFrame, min_days: int = 30) -> list[tuple]:
    """Calendar gaps longer than `min_days` — trading halts, suspensions, or a
    ticker that changed identity. Returns [(gap_days, resume_date), ...]."""
    g = bars.index.to_series().diff().dt.days
    return [(int(v), d) for d, v in g[g > min_days].items()]


def clean_bars(bars: pd.DataFrame, ticker: str = "", min_days: int = 30,
               truncate: bool = True) -> pd.DataFrame:
    """Guard against silently backtesting across a corporate discontinuity.

    NBIS is the motivating case: it carries Yandex's price history through two
    multi-month suspensions (397d ending 2023-03-29, 572d ending 2024-10-21).
    Anything before the last resume is a DIFFERENT COMPANY, and the gap itself
    manufactures a fake volatility spike. Default behaviour is to truncate to the
    post-gap segment and say so loudly."""
    gaps = find_gaps(bars, min_days)
    if not gaps:
        return bars
    days, resume = gaps[-1]
    msg = (f"[data] {ticker}: {len(gaps)} halt/suspension gap(s) > {min_days}d; "
           f"last = {days}d resuming {resume.date()}")
    if not truncate:
        print(msg + "  -- NOT truncated (--allow-gaps): pre-gap history may be a "
                    "different company")
        return bars
    out = bars.loc[resume:]
    print(msg + f"  -> truncated to {len(out)} bars from {resume.date()} "
                f"(pre-gap history discarded)")
    return out


def live_snapshot(codes: list[str]) -> dict:
    """Current moomoo snapshot for stock and/or option codes (used to calibrate the
    vol model against today's real chain). Returns {code: dict}."""
    from futu import RET_OK
    ctx = _futu_ctx()
    out = {}
    try:
        for i in range(0, len(codes), 200):
            ret, data = ctx.get_market_snapshot(codes[i:i + 200])
            if ret != RET_OK:
                print(f"[data] snapshot failed: {data}")
                continue
            for _, r in data.iterrows():
                out[r["code"]] = r.to_dict()
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return out


def option_chain(ticker: str, expiry: str, market: str = "US") -> pd.DataFrame:
    """Today's listed chain for one expiry, with IV/greeks — the input to the
    smile calibration in optbt.vol."""
    from futu import RET_OK, OptionType
    ctx = _futu_ctx()
    try:
        ret, data = ctx.get_option_chain(f"{market}.{ticker.upper()}",
                                         start=expiry, end=expiry,
                                         option_type=OptionType.CALL)
        if ret != RET_OK:
            raise RuntimeError(f"chain error {ticker} {expiry}: {data}")
        codes = data["code"].tolist()
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    snaps = live_snapshot(codes)
    rows = []
    for c, s in snaps.items():
        rows.append({"code": c,
                     "strike": s.get("option_strike_price"),
                     "iv": s.get("option_implied_volatility"),
                     "delta": s.get("option_delta"),
                     "last": s.get("last_price"),
                     "oi": s.get("option_open_interest")})
    return pd.DataFrame(rows).dropna(subset=["strike"]).sort_values("strike")


def expiry_dates(ticker: str, market: str = "US") -> list[str]:
    """All listed expiries for a ticker."""
    from futu import RET_OK
    ctx = _futu_ctx()
    try:
        ret, data = ctx.get_option_expiration_date(f"{market}.{ticker.upper()}")
        if ret != RET_OK:
            raise RuntimeError(f"expiry error {ticker}: {data}")
        return data["strike_time"].tolist()
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "NBIS"
    df = get_bars(t, "2015-01-01")
    print(df.tail())
