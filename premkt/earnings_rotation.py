"""
财报轮动扫描 —— 找"下一个要出财报的同业票"，并量化它历史上炸不炸。

起因：MSFT / PLTR / SHOP / TEAM / SE 在 2026 年 7~8 月看起来像"一个一个拉升"，
实测下来 5 只全部精确落在财报后第一个交易日，没有例外 —— 所谓轮动其实是
财报日历。那么"下一个"就不该去找资金流入的票，而是查下一批要报的同业。

本模块对候选票计算三件事：
  1. 历史财报后首日实际涨跌幅（BEFORE 当日 / AFTER 次日）—— 这只票炸不炸
  2. 过去几个季度的 EPS 超预期幅度 —— 它有没有持续超预期的习惯
  3. 当前 iv_rank —— 市场已经price in了多少，决定这笔交易贵不贵

注意 get_earnings_calendar 的 price / iv_rank / market_cap 是**当前快照**，
不是财报当日的值（实测：MSFT 07-29 那行的 price 等于今天的最新价）。
所以只有"未来要报"的票，iv_rank 才等于财报前 IV；已报过的票不能这样用。

    python -m premkt.earnings_rotation --days 20 --min-cap 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import numpy as np
import pandas as pd
from futu import RET_OK, Market

from .data import _throttle, cached_daily_kline, now_et, quote_ctx
from .fmt import c as _c, lj, rj, trunc

# 行业板块（get_plate_list(Market.US, Plate.INDUSTRY) 里筛出来的）
PLATES = {
    "US.LIST2470": "应用软件",
    "US.LIST2508": "基础软件",
    "US.LIST2252": "IT服务",
    "US.LIST2004": "互联网内容",
    "US.LIST2431": "互联网零售",
}


def _cal_chunks(b: dt.date, e: dt.date, size: int = 7):
    cur = b
    while cur <= e:
        stop = min(cur + dt.timedelta(days=size - 1), e)
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def earnings_calendar(q, begin: dt.date, end: dt.date) -> pd.DataFrame:
    """OpenD 的财报日历一次最多 7 天，必须分片。"""
    frames = []
    for b, e in _cal_chunks(begin, end):
        _throttle.wait()
        res = q.get_earnings_calendar(market=Market.US, begin_date=str(b), end_date=str(e))
        if res[0] == RET_OK and res[1] is not None and len(res[1]):
            frames.append(res[1])
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["security", "earnings_date"])
    for col in ("eps_actual", "eps_predict", "revenue_actual", "revenue_predict",
                "market_cap", "iv", "iv_rank", "iv_percentile", "price"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def industry_map(q) -> dict[str, str]:
    out = {}
    for pc, label in PLATES.items():
        _throttle.wait()
        ret, d = q.get_plate_stock(pc)
        if ret == RET_OK and d is not None:
            for code in d["code"]:
                out.setdefault(code, label)
    return out


# ---------------------------------------------------------------------------
def reaction_moves(q, code: str, events: pd.DataFrame, lookback_days: int = 400) -> dict:
    """把历史财报事件对齐到"反应交易日"，算那天的实际涨跌幅。

    pub_type == BEFORE -> 反应在当日；AFTER -> 反应在下一个交易日。
    """
    end = now_et().date()
    start = end - dt.timedelta(days=lookback_days)
    k = cached_daily_kline(q, code, start, end)
    if k is None or k.empty:
        return {}

    k = k.copy()
    k["d"] = pd.to_datetime(k["time_key"]).dt.date
    k = k.sort_values("d").reset_index(drop=True)
    days = list(k["d"])
    chg = dict(zip(k["d"], pd.to_numeric(k["change_rate"], errors="coerce")))

    moves, surprises = [], []
    for _, ev in events.iterrows():
        try:
            ed = dt.datetime.strptime(str(ev["earnings_date"])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        after = str(ev.get("pub_type", "")).upper() == "AFTER"
        # 找到反应日：BEFORE 取当天(或之后第一个交易日)，AFTER 取之后第一个交易日
        cand = [d for d in days if (d > ed if after else d >= ed)]
        if not cand:
            continue
        rd = cand[0]
        if rd in chg and not pd.isna(chg[rd]):
            moves.append(float(chg[rd]))
        a, p = ev.get("eps_actual"), ev.get("eps_predict")
        if pd.notna(a) and pd.notna(p) and abs(p) > 1e-9:
            surprises.append((a - p) / abs(p) * 100)

    if not moves:
        return {}
    arr = np.array(moves)
    return dict(
        n_events=len(arr),
        avg_move=float(arr.mean()),
        avg_abs_move=float(np.abs(arr).mean()),
        max_move=float(arr.max()),
        min_move=float(arr.min()),
        up_rate=float((arr > 0).mean() * 100),
        big_up_rate=float((arr >= 8).mean() * 100),   # 单日 ≥8% 的比例
        avg_surprise=float(np.mean(surprises)) if surprises else np.nan,
        beat_rate=float(np.mean([s > 0 for s in surprises]) * 100) if surprises else np.nan,
    )


def positioning(q, code: str) -> dict:
    """财报前的位置：近 20 日涨幅 + 距 52 周高。已经涨完的票，beat 早被 price in。"""
    end = now_et().date()
    k = cached_daily_kline(q, code, end - dt.timedelta(days=400), end)
    if k is None or len(k) < 21:
        return {}
    close = pd.to_numeric(k["close"], errors="coerce").dropna()
    high = pd.to_numeric(k["high"], errors="coerce").dropna()
    out = dict(ret_20d=float(close.iloc[-1] / close.iloc[-21] - 1) * 100)
    if len(high) >= 60:
        h52 = float(high.tail(252).max())
        if h52 > 0:
            out["pct_of_52w"] = float(close.iloc[-1] / h52) * 100
    return out


# ---------------------------------------------------------------------------
def run(days_ahead: int = 20, min_cap_b: float = 3.0, hist_months: int = 19,
        industries: set[str] | None = None, codes: list[str] | None = None) -> pd.DataFrame:
    today = now_et().date()
    with quote_ctx() as q:
        print(_c("[1/4] 拉行业板块成分…", "cyn"))
        ind = industry_map(q)

        print(_c(f"[2/4] 拉未来 {days_ahead} 天财报日历…", "cyn"))
        upcoming = earnings_calendar(q, today, today + dt.timedelta(days=days_ahead))
        if upcoming.empty:
            return pd.DataFrame()
        upcoming["行业"] = upcoming["security"].map(ind)
        if codes:
            want = {c if c.startswith("US.") else f"US.{c}" for c in codes}
            cand = upcoming[upcoming["security"].isin(want)].copy()
            cand["行业"] = cand["行业"].fillna("-")
        else:
            cand = upcoming[upcoming["行业"].notna()].copy()
            if industries:
                cand = cand[cand["行业"].isin(industries)]
            cand = cand[cand["market_cap"] >= min_cap_b * 1e9]
        cand = cand.sort_values("earnings_date").reset_index(drop=True)
        print(f"      候选 {len(cand)} 只")

        print(_c(f"[3/4] 拉过去 {hist_months} 个月财报历史…", "cyn"))
        hist = earnings_calendar(q, today - dt.timedelta(days=int(hist_months * 30.4)),
                                 today - dt.timedelta(days=1))

        print(_c(f"[4/4] 对齐反应交易日，算历史财报后首日涨跌（{len(cand)} 只）…", "cyn"))
        recs = []
        for _, r in cand.iterrows():
            code = r["security"]
            ev = hist[hist["security"] == code]
            stats = reaction_moves(q, code, ev) if len(ev) else {}
            pos = positioning(q, code)
            recs.append(dict(
                code=code, name=r.get("name", ""), date=str(r["earnings_date"])[:10],
                when=r.get("pub_type", ""), industry=r["行业"],
                cap_b=r["market_cap"] / 1e9 if pd.notna(r["market_cap"]) else np.nan,
                iv_rank=r.get("iv_rank", np.nan),
                iv_pct=r.get("iv_percentile", np.nan),
                **stats, **pos,
            ))
    return pd.DataFrame(recs)


def print_table(df: pd.DataFrame) -> None:
    if df.empty:
        print(_c("没有候选。", "yel"))
        return
    print()
    print(_c(lj("代码", 9) + lj("名称", 20) + lj("财报日", 12) + lj("时段", 8)
             + lj("行业", 12) + rj("市值B", 8) + rj("IV分位", 8)
             + rj("历史次数", 9) + rj("均|涨跌|", 9) + rj("上涨率", 8)
             + rj("≥8%率", 8) + rj("最大", 8) + rj("最小", 8)
             + rj("超预期率", 9) + rj("近20日", 8) + rj("距52高", 8), "bold"))
    print(_c("─" * 152, "dim"))
    for _, r in df.iterrows():
        def f(v, nd=1, suf=""):
            return "n/a" if pd.isna(v) else f"{v:.{nd}f}{suf}"
        ivc = "red" if (r["iv_rank"] or 0) >= 75 else ("yel" if (r["iv_rank"] or 0) >= 50 else "grn")
        print(lj(r["code"], 9) + lj(trunc(r["name"], 18), 20) + lj(r["date"], 12)
              + lj("盘前" if r["when"] == "BEFORE" else "盘后", 8)
              + lj(r["industry"], 12) + rj(f(r["cap_b"]), 8)
              + rj(_c(f(r["iv_rank"], 0), ivc), 8)
              + rj(f(r.get("n_events"), 0), 9) + rj(f(r.get("avg_abs_move"), 1, "%"), 9)
              + rj(f(r.get("up_rate"), 0, "%"), 8) + rj(f(r.get("big_up_rate"), 0, "%"), 8)
              + rj(f(r.get("max_move"), 1, "%"), 8) + rj(f(r.get("min_move"), 1, "%"), 8)
              + rj(f(r.get("beat_rate"), 0, "%"), 9) + rj(f(r.get("ret_20d"), 1, "%"), 8)
              + rj(f(r.get("pct_of_52w"), 0, "%"), 8))
    print(_c("─" * 152, "dim"))
    print(_c("均|涨跌| = 历史财报后首日绝对涨跌幅均值（这只票炸不炸）  ≥8%率 = 单日涨≥8%的比例", "dim"))
    print(_c("IV分位 绿<50 黄50-75 红≥75 —— 越高说明市场已经price in越多，同样的beat赚得越少", "dim"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="找下一批要出财报的同业票")
    p.add_argument("--days", type=int, default=20, help="往后看多少天")
    p.add_argument("--min-cap", type=float, default=3.0, help="市值下限（十亿美元）")
    p.add_argument("--industry", nargs="*", help="限定行业，如 应用软件 基础软件")
    p.add_argument("--sort", default="big_up_rate", help="排序字段")
    p.add_argument("--codes", nargs="*", help="只看指定代码，如 INTU ZM CRWD")
    a = p.parse_args(argv)

    print(_c(f"\n财报轮动扫描  {now_et():%Y-%m-%d %H:%M ET}", "bold"))
    df = run(days_ahead=a.days, min_cap_b=a.min_cap,
             industries=set(a.industry) if a.industry else None, codes=a.codes)
    if not df.empty and a.sort in df:
        df = df.sort_values(["date", a.sort], ascending=[True, False])
    print_table(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
