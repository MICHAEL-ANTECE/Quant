#!/usr/bin/env python3
"""
Build a historical IV series for SMH and export it as a CSV that
smh_put_hedge_backtest.py can consume via IV_MODE="csv".

WHY THIS EXISTS
---------------
A real, free, SMH-specific historical implied-vol index does not exist.
The 'hv' proxy in the backtest (realized vol * constant) is crude because
IV leads/diverges from realized vol around events. A much better *free*
proxy is CBOE's Nasdaq-100 Volatility Index (^VXN): SMH (semis) lives in
the Nasdaq-100 vol regime, and semis vol is a fairly stable multiple of it.

This script:
  1. downloads ^VXN (forward-looking 30-day IV, in vol points) from yfinance
  2. downloads SMH to measure its realized vol
  3. calibrates a single multiplier k so that mean(k * VXN) matches the
     mean of SMH's own realized vol over the sample (semis-vs-nasdaq premium)
  4. writes smh_iv.csv with columns: date, iv   (iv as a decimal, e.g. 0.32)

It also prints the calibration so you can sanity-check / override k.

For production accuracy, replace ^VXN with a real EOD SMH option chain's
30-day ATM IV (ORATS / OptionMetrics / CBOE DataShop / Polygon).

Deps: numpy, pandas, yfinance
    pip install numpy pandas yfinance
Run:  python3 build_smh_iv.py
Then in smh_put_hedge_backtest.py set:
    IV_MODE     = "csv"
    IV_CSV_PATH = "smh_iv.csv"
"""

from __future__ import annotations
import math
import numpy as np
import pandas as pd

# ----------------------------- CONFIG ---------------------------------------
VOL_TICKER   = "^VXN"        # Nasdaq-100 vol index (semis proxy). "^VIX" also works.
UNDERLYING   = "SMH"
START        = "2019-01-01"
END          = None          # None = today
HV_WINDOW    = 21            # trading days for SMH realized vol (for calibration)
K_OVERRIDE   = None          # set a float to force the multiplier instead of auto-calibrating
OUT_CSV      = "smh_iv.csv"
# ----------------------------------------------------------------------------


def dl(ticker):
    import yfinance as yf
    df = yf.download(ticker, start=START, end=END, auto_adjust=True, progress=False)
    if df.empty:
        raise SystemExit(f"No data for {ticker} from yfinance.")
    s = df["Close"]
    if isinstance(s, pd.DataFrame):        # yfinance sometimes returns a 1-col frame
        s = s.iloc[:, 0]
    s.index = pd.to_datetime(s.index)
    return s.rename(ticker)


def main():
    try:
        vol = dl(VOL_TICKER)               # in vol points, e.g. 22.5
        smh = dl(UNDERLYING)
    except Exception as e:
        raise SystemExit(f"Download failed ({e}). Install yfinance or supply CSVs.")

    iv_raw = (vol / 100.0).rename("vxn")   # -> decimal, e.g. 0.225

    # SMH realized vol (annualized) for calibration
    ret = np.log(smh / smh.shift(1))
    hv = (ret.rolling(HV_WINDOW).std() * math.sqrt(252)).rename("hv")

    df = pd.concat([iv_raw, hv], axis=1).dropna()
    if df.empty:
        raise SystemExit("No overlapping dates between vol index and SMH.")

    if K_OVERRIDE is not None:
        k = float(K_OVERRIDE)
        how = "override"
    else:
        # multiplier so mean(k * VXN) == mean(SMH realized vol)
        k = df["hv"].mean() / df["vxn"].mean()
        how = "auto"

    iv = (df["vxn"] * k).rename("iv")
    out = iv.reset_index()
    out.columns = ["date", "iv"]
    out.to_csv(OUT_CSV, index=False)

    print(f"\n=== SMH IV series built from {VOL_TICKER} ===")
    print(f"period          : {out['date'].iloc[0].date()} -> {out['date'].iloc[-1].date()}  ({len(out)} rows)")
    print(f"calibration      : k = {k:.3f}  ({how}); semis IV = {k:.2f} x Nasdaq-100 IV")
    print(f"mean {VOL_TICKER:<6}    : {df['vxn'].mean():.1%}   mean SMH realized vol: {df['hv'].mean():.1%}")
    print(f"resulting IV     : min {iv.min():.1%}  mean {iv.mean():.1%}  max {iv.max():.1%}")
    print(f"correlation IV~HV: {df['vxn'].corr(df['hv']):.2f}")
    print(f"written          : {OUT_CSV}")
    print("\nNext: in smh_put_hedge_backtest.py set IV_MODE='csv', IV_CSV_PATH='smh_iv.csv'.")
    print("Caveat: ^VXN is a Nasdaq-100 proxy, not SMH's own option-implied vol.")
    print("For exact numbers, feed a real EOD SMH ATM-IV series instead.\n")


if __name__ == "__main__":
    main()
