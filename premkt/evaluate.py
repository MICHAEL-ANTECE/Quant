"""
前向验证 —— 这个策略唯一诚实的检验方式。

为什么不能做常规回测：Futu OpenAPI 不保留历史盘前快照，
get_us_pre_market_rank 只有"当前"这一期。所以没有办法回溯生成
历史候选池 —— 任何声称回测过的盘前 gap 策略，要么用了别的数据源，
要么在用当日全量数据反推，那是前视偏差。

替代方案：每个交易日盘前跑 `scan --save` 存快照，收盘后跑本模块，
用当天真实的分钟线兑现每一条交易计划，逐步累积样本。
样本够了（≥100 笔）再回过头调 config.py 里的权重。

    python -m premkt.evaluate                  # 评估全部未评估的快照
    python -m premkt.evaluate --date 2026-08-10
    python -m premkt.evaluate --daily          # 只用日线（快，但无法判定止损/目标先后）
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

import numpy as np
import pandas as pd
from futu import RET_OK, AuType, KLType

from .data import HERE, quote_ctx, _throttle
from .fmt import c as _c, lj, rj

SNAP_DIR = os.path.join(HERE, "snapshots")


# ---------------------------------------------------------------------------
def _bars(q, code: str, day: dt.date, ktype=KLType.K_1M) -> pd.DataFrame | None:
    _throttle.wait()
    try:
        ret, df, _ = q.request_history_kline(
            code, start=day.strftime("%Y-%m-%d"), end=day.strftime("%Y-%m-%d"),
            ktype=ktype, autype=AuType.QFQ, max_count=1000,
        )
    except Exception:
        return None
    if ret != RET_OK or df is None or df.empty:
        return None
    return df


def _resolve(bars: pd.DataFrame, entry: float, stop: float, targets: list[float],
             side: str) -> dict:
    """按分钟线时间顺序兑现：先触发入场，再看止损和目标谁先到。"""
    o = bars.iloc[0]["open"]
    hi, lo = bars["high"].max(), bars["low"].min()
    close = bars.iloc[-1]["close"]

    long = side == "long"
    filled_i = None
    for i, b in enumerate(bars.itertuples()):
        if (long and b.high >= entry) or (not long and b.low <= entry):
            filled_i = i
            break
    if filled_i is None:
        return dict(filled=False, open=o, high=hi, low=lo, close=close,
                    exit_price=np.nan, exit_reason="未触发", ret_pct=np.nan, r_multiple=np.nan)

    risk = abs(entry - stop)
    t1 = targets[0] if targets else (entry + (1 if long else -1) * risk)
    exit_price, reason = close, "收盘平仓"
    for b in list(bars.itertuples())[filled_i:]:
        hit_stop = (long and b.low <= stop) or (not long and b.high >= stop)
        hit_tgt = (long and b.high >= t1) or (not long and b.low <= t1)
        if hit_stop and hit_tgt:
            exit_price, reason = stop, "同K线双触(保守判止损)"
            break
        if hit_stop:
            exit_price, reason = stop, "止损"
            break
        if hit_tgt:
            exit_price, reason = t1, "目标1"
            break

    sign = 1 if long else -1
    ret_pct = sign * (exit_price - entry) / entry * 100
    return dict(filled=True, open=o, high=hi, low=lo, close=close,
                exit_price=exit_price, exit_reason=reason,
                ret_pct=ret_pct,
                r_multiple=sign * (exit_price - entry) / risk if risk > 0 else np.nan)


def evaluate_snapshot(path: str, use_intraday: bool = True) -> pd.DataFrame:
    with open(path) as f:
        snap = json.load(f)
    rows = snap["rows"]
    day = dt.datetime.fromisoformat(snap["scanned_at"]).date()
    print(_c(f"\n评估 {os.path.basename(path)}  ({day}, {len(rows)} 只)", "bold"))

    recs = []
    with quote_ctx() as q:
        for r in rows:
            code, side = r["code"], r.get("side", "long")
            bars = _bars(q, code, day, KLType.K_1M) if use_intraday else None
            if bars is None:
                bars = _bars(q, code, day, KLType.K_DAY)
                if bars is None:
                    continue
                res = dict(filled=np.nan, open=bars.iloc[0]["open"], high=bars["high"].max(),
                           low=bars["low"].min(), close=bars.iloc[-1]["close"],
                           exit_price=np.nan, exit_reason="仅日线", ret_pct=np.nan,
                           r_multiple=np.nan)
            else:
                res = _resolve(bars, r["entry"], r["stop"], r.get("targets", []), side)

            prev_close = r.get("prev_close")
            open_gap = (res["open"] - prev_close) / prev_close * 100 if prev_close else np.nan
            pre_gap = r.get("gap_pct", np.nan)
            recs.append(dict(
                date=day, code=code, name=r.get("name", ""), side=side,
                score=r.get("score"), catalyst=r.get("catalyst_label"),
                kind=r.get("catalyst_kind"),
                pre_gap_pct=pre_gap,
                open_gap_pct=open_gap,
                # 开盘保住了多少盘前涨幅 —— 衡量"追不追得上"
                gap_capture=open_gap / pre_gap if pre_gap else np.nan,
                # 开盘买、收盘卖的裸收益，用来看动量本身有没有延续性
                open_to_close_pct=(res["close"] - res["open"]) / res["open"] * 100,
                mfe_pct=(res["high"] - res["open"]) / res["open"] * 100,
                mae_pct=(res["low"] - res["open"]) / res["open"] * 100,
                **{k: res[k] for k in ("filled", "exit_reason", "ret_pct", "r_multiple")},
            ))
    return pd.DataFrame(recs)


# ---------------------------------------------------------------------------
def report(df: pd.DataFrame) -> None:
    if df.empty:
        print(_c("没有可评估的记录。", "yel"))
        return

    print(_c(f"\n{'─'*104}\n逐笔结果\n{'─'*104}", "bold"))
    print(_c(lj("日期", 12) + lj("代码", 10) + lj("催化", 10) + rj("评分", 7)
             + rj("盘前%", 9) + rj("开盘%", 9) + rj("保留率", 9)
             + rj("开→收%", 9) + "  " + lj("出场", 20) + rj("R", 7), "dim"))
    for _, r in df.sort_values("score", ascending=False).iterrows():
        rc = "grn" if (r["r_multiple"] or 0) > 0 else "red"
        rm = "n/a" if pd.isna(r["r_multiple"]) else _c(f"{r['r_multiple']:+.2f}", rc)
        gc = "n/a" if pd.isna(r["gap_capture"]) else f"{r['gap_capture']*100:.0f}%"
        print(lj(r["date"], 12) + lj(r["code"], 10) + lj(r["catalyst"], 10)
              + rj(f"{r['score']:.1f}", 7) + rj(f"{r['pre_gap_pct']:.1f}", 9)
              + rj(f"{r['open_gap_pct']:.1f}", 9) + rj(gc, 9)
              + rj(f"{r['open_to_close_pct']:.1f}", 9) + "  "
              + lj(r["exit_reason"], 20) + rj(rm, 7))

    ok = df.dropna(subset=["r_multiple"])
    n_filled = int(df["filled"].sum()) if df["filled"].notna().any() else len(ok)
    print(_c(f"\n触发率: {n_filled}/{len(df)} ({n_filled/max(len(df),1)*100:.0f}%)"
             f"  —— 未触发不算亏损，突破入场天然会漏掉一部分行情", "dim"))
    if len(ok):
        wins = (ok["r_multiple"] > 0).sum()
        print(_c(f"已成交 {len(ok)} 笔  胜率 {wins/len(ok)*100:.0f}%  "
                 f"期望 {ok['r_multiple'].mean():+.2f}R  "
                 f"总计 {ok['r_multiple'].sum():+.1f}R", "bold"))

    print(_c(f"\n{'─'*112}\n按催化剂分组 —— 这是本策略的核心假设，先验证它\n{'─'*112}", "bold"))
    def _winrate(s):
        s = s.dropna()
        return (s > 0).mean() * 100 if len(s) else np.nan

    g = df.groupby("catalyst").agg(
        n=("code", "size"),
        mean_o2c=("open_to_close_pct", "mean"),
        median_o2c=("open_to_close_pct", "median"),
        mean_capture=("gap_capture", "mean"),
        mean_r=("r_multiple", "mean"),
        winrate=("r_multiple", _winrate),
    ).round(2)
    g.columns = ["笔数", "平均开→收%", "中位开→收%", "平均保留率", "平均R", "胜率%"]
    print(g.to_string())
    print(_c("\n假设成立的标志: hard 组的 平均开→收 / 平均R 显著高于 none 组，negative 组为负。", "dim"))

    if df["score"].notna().sum() >= 8:
        sub = df.dropna(subset=["score", "open_to_close_pct"])
        rho = sub["score"].corr(sub["open_to_close_pct"], method="spearman")
        print(_c(f"\n评分 vs 开→收 的 Spearman 相关: {rho:+.3f}  "
                 f"(n={len(sub)}, 需要 ≥100 笔才有统计意义)", "cyn"))
        sub = sub.copy()
        try:
            sub["bucket"] = pd.qcut(sub["score"], min(4, sub["score"].nunique()),
                                    labels=False, duplicates="drop")
            b = sub.groupby("bucket").agg(n=("code", "size"),
                                          mean_score=("score", "mean"),
                                          mean_o2c=("open_to_close_pct", "mean")).round(2)
            b.columns = ["笔数", "评分均值", "开→收均值%"]
            print(b.to_string())
        except ValueError:
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="盘前动量策略前向验证")
    p.add_argument("--date", help="只评估某天的快照 (YYYY-MM-DD)")
    p.add_argument("--daily", action="store_true", help="只用日线，不拉分钟线")
    p.add_argument("--csv", help="把逐笔结果写到 CSV")
    p.add_argument("--dir", default=SNAP_DIR,
                   help=f"快照目录，默认 {SNAP_DIR}；演练样本在 snapshots/dryrun")
    a = p.parse_args(argv)

    pattern = os.path.join(a.dir, f"scan_{a.date}*.json" if a.date else "scan_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        print(_c(f"没有快照文件。先在盘前跑 `python -m premkt.scan --save`。\n查找路径: {pattern}", "yel"))
        return 1

    frames = [evaluate_snapshot(f, use_intraday=not a.daily) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        print(_c("快照里的标的都取不到当日 K 线。", "yel"))
        return 1
    df = pd.concat(frames, ignore_index=True)
    report(df)
    if a.csv:
        df.to_csv(a.csv, index=False)
        print(_c(f"\n已写出 {a.csv}", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
