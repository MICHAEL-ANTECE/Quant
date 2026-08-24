"""
盘前动量扫描 —— CLI 入口。

    python -m premkt.scan                    # 默认做多榜 top 15
    python -m premkt.scan --side both        # 同时给做空候选
    python -m premkt.scan --top 25 --save    # 存快照供事后前向验证
    python -m premkt.scan --no-news          # 跳过催化剂查询，快速预览

流水线（每一步都在缩小成本最高的下游调用量）:
    盘前榜(2 次调用, 400 只)
      -> 普通股名单交集 + 价格/成交额粗筛
      -> 快照(每 200 只 1 次调用)
      -> 硬门槛
      -> 日线(仅前 60 名, 每只 1 次调用)
      -> 初筛打分
      -> 催化剂(仅前 35 名: EDGAR 并发 + moomoo 新闻串行)
      -> 终评分 + 交易计划
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import pandas as pd

from .catalyst import build_catalysts
from .config import GATES, RUNTIME, TRADE, WEIGHTS
from .fmt import lj, rj, trunc
from .data import (
    ET,
    daily_klines,
    now_et,
    pre_market_rank,
    quote_ctx,
    save_snapshot,
    session_label,
    snapshots,
    us_common_stocks,
)
from .score import apply_gates, compute_scores, enrich, gap_quality, trade_plan

C = dict(dim="\033[2m", bold="\033[1m", red="\033[31m", grn="\033[32m",
         yel="\033[33m", cyn="\033[36m", mag="\033[35m", off="\033[0m")


def _c(s, k):
    return f"{C[k]}{s}{C['off']}"


def _money(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "  n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v/div:6.1f}{unit}"
    return f"{v:7.0f}"


def _pct(v, nd=1):
    return "  n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}%"


# ---------------------------------------------------------------------------
def run_scan(side: str = "long", top: int = 15, use_news: bool = True,
             verbose: bool = False) -> pd.DataFrame:
    today = now_et().date()
    sess = session_label()

    with quote_ctx() as q:
        print(_c(f"\n[1/6] 拉取盘前{'涨' if side == 'long' else '跌'}幅榜 …", "cyn"))
        rank = pre_market_rank(q, RUNTIME["rank_pull"], gainers=(side == "long"))
        if rank.empty:
            print(_c("盘前榜为空 —— OpenD 未登录或非交易时段。", "red"))
            return pd.DataFrame()
        print(f"      拿到 {len(rank)} 只")

        print(_c("[2/6] 交集普通股名单，剔除 ETF / 权证 / 粉单 …", "cyn"))
        basic = us_common_stocks(q)
        allowed = basic[
            basic["exchange_type"].isin(GATES["allowed_exchanges"])
            & (~basic.get("delisting", pd.Series(False, index=basic.index)).fillna(False).astype(bool))
        ]
        rank = rank[rank["security"].isin(set(allowed["code"]))].copy()
        rank = rank.merge(allowed[["code", "exchange_type", "listing_date"]],
                          left_on="security", right_on="code", how="left").drop(columns=["code"])

        # 粗筛：用榜单自带字段先砍掉明显不合格的，减少快照调用
        rank = rank[
            (rank["pre_market_price"].astype(float) >= GATES["min_price"])
            & (rank["pre_market_price"].astype(float) <= GATES["max_price"])
            & (rank["pre_market_turnover"].astype(float) >= GATES["min_pre_turnover"] * 0.5)
        ]
        rank["_pre_rank"] = rank.apply(
            lambda r: gap_quality(r["pre_market_change_ratio"])
            * math.log10(max(float(r["pre_market_turnover"]), 10)),
            axis=1,
        )
        rank = rank.sort_values("_pre_rank", ascending=False).head(300).reset_index(drop=True)
        print(f"      粗筛后 {len(rank)} 只")
        if rank.empty:
            return pd.DataFrame()

        print(_c("[3/6] 拉快照（盘前 OHLCV / 股本 / 融券 / 点差）…", "cyn"))
        snap = snapshots(q, rank["security"].tolist())

        # 先用无 K 线的版本过门槛，避免为被剔除的票浪费 K 线配额
        wide0 = enrich(rank, snap, {}, today)
        passed, rejected = apply_gates(wide0, today, side=side)
        print(f"      过硬门槛 {len(passed)} / {len(wide0)}")
        if verbose and len(rejected):
            print(_c("      剔除原因抽样:", "dim"))
            for _, r in rejected.head(8).iterrows():
                print(_c(f"        {r['code']:<10} {r['reject_reason']}", "dim"))
        if passed.empty:
            return pd.DataFrame()

        passed = passed.sort_values("pre_turnover", ascending=False).head(RUNTIME["enrich_top_n"])
        codes = passed["code"].tolist()

        print(_c(f"[4/6] 拉 {len(codes)} 只日线（RVOL 基准 / ATR / MA20）…", "cyn"))
        kl = daily_klines(q, codes)
        wide = enrich(rank[rank["security"].isin(codes)], snap, kl, today)
        wide, _ = apply_gates(wide, today, side=side)

        # 数据新鲜度：快照 prev_close 与最近一根日线收盘对不上，就说明
        # pre_* 还停留在上一个交易日。这种脏数据外观完全正常，必须显式拦截。
        stale_frac = float(wide["pre_stale"].mean()) if len(wide) else 0.0
        if stale_frac > 0.3:
            print(_c(
                f"\n  ✖ 数据陈旧：{stale_frac*100:.0f}% 的标的 prev_close 与最近日线收盘不符。\n"
                f"    pre_* 字段与盘前榜在美东 04:00 前不会翻新，现在拿到的是上一个交易日的盘前。\n"
                f"    结果仅供演练，不要据此下单；--save 已被禁用。", "red"))
        elif 0 < stale_frac <= 0.3:
            print(_c(f"      注意：{stale_frac*100:.0f}% 的标的疑似停牌或数据滞后", "yel"))

        prelim = compute_scores(wide, {}, side=side)
        prelim = prelim.sort_values("base", ascending=False)
        head = prelim.head(RUNTIME["catalyst_top_n"])

        # 催化剂时间窗必须锚定"盘前数据所属交易日"，而不是当前时刻
        modes = wide["session_date"].mode()
        eff_date = modes.iloc[0] if len(modes) else today
        ref = dt.datetime.combine(eff_date, dt.time(9, 0), tzinfo=ET)

        cats = {}
        if use_news:
            print(_c(f"[5/6] 查催化剂：SEC 8-K + moomoo 新闻（{len(head)} 只，"
                     f"时间窗锚定 {eff_date}）…", "cyn"))
            cats = build_catalysts(q, head[["code", "name"]].to_dict("records"), ref=ref)
        else:
            print(_c("[5/6] 跳过催化剂查询（--no-news）", "dim"))

    print(_c("[6/6] 终评分 …", "cyn"))
    final = compute_scores(wide, cats, side=side)
    final = final[final["code"].isin(head["code"])] if use_news else final
    final = final.sort_values("score", ascending=False).reset_index(drop=True)
    final["side"] = side

    plans = [trade_plan(r, side=side) for _, r in final.iterrows()]
    for k in ("entry", "stop", "stop_pct", "shares", "notional", "risk_usd", "targets", "too_wide"):
        final[k] = [p[k] for p in plans]

    final.attrs["session"] = sess
    final.attrs["scanned_at"] = now_et().isoformat()
    final.attrs["stale_frac"] = stale_frac
    return final.head(top)


# ---------------------------------------------------------------------------
def print_table(df: pd.DataFrame, side: str = "long") -> None:
    if df.empty:
        print(_c("\n没有标的通过筛选。", "yel"))
        return

    mark = {"hard": _c("硬", "grn"), "soft": _c("软", "yel"),
            "none": _c("无", "dim"), "negative": _c("负", "red")}

    print()
    print(_c(lj("#", 4) + lj("代码", 11) + lj("名称", 22) + rj("盘前价", 9)
             + rj("涨幅", 9) + rj("$RVOL", 8) + rj("盘前额", 10) + rj("流通盘", 10)
             + rj("催化", 6) + "  " + lj("类型", 18) + rj("评分", 7), "bold"))
    print(_c("─" * 122, "dim"))

    for i, r in df.iterrows():
        rv = r["dollar_rvol"]
        rv_s = "n/a" if (rv is None or (isinstance(rv, float) and math.isnan(rv))) else f"{rv*100:.0f}%"
        gap_c = "grn" if r["gap_pct"] > 0 else "red"
        sc = r["score"]
        sc_c = "grn" if sc >= 55 else ("yel" if sc >= 40 else "dim")
        print(lj(i + 1, 4) + lj(r["code"], 11) + lj(trunc(r["name"], 20), 22)
              + rj(f"{r['pre_price']:.2f}", 9)
              + rj(_c(f"{r['gap_pct']:+.1f}%", gap_c), 9)
              + rj(rv_s, 8) + rj(_money(r["pre_turnover"]).strip(), 10)
              + rj(_money(r["float_shares"]).strip(), 10)
              + rj(mark[r["catalyst_label"]], 6) + "  "
              + lj(trunc(r["catalyst_kind"], 16), 18)
              + rj(_c(f"{sc:.1f}", sc_c), 7))

    print(_c("─" * 122, "dim"))
    print(_c("催化: 硬=8-K item 2.02/1.01 或财报/FDA/并购级标题  软=一般消息  "
             "无=纯资金推动(乘数0.55)  负=增发/退市(乘数0.15)", "dim"))


def print_details(df: pd.DataFrame, n: int = 5, side: str = "long") -> None:
    if df.empty or n <= 0:
        return
    n = min(n, len(df))
    print(_c(f"\n\n{'═'*128}\n交易计划 · 前 {n} 名\n{'═'*128}", "bold"))
    for i, r in df.head(n).iterrows():
        title = _c(f"{i+1}. {r['code']}  {r['name']}", "bold")
        print(f"\n{title}   评分 {_c(f'{r['score']:.1f}', 'cyn')}  "
              f"({'做多' if side == 'long' else '做空'})")

        print(f"   分项  gap {r['f_gap']:.2f} · RVOL {r['f_rvol']:.2f} · 技术位 {r['f_tech']:.2f} · "
              f"流通/融券 {r['f_float']:.2f} · 流动性 {r['f_liq']:.2f}  "
              f"→ base {r['base']:.3f} × 催化剂[{r['catalyst_label']}]")

        rvol = r["dollar_rvol"]
        rvol_s = "n/a" if math.isnan(rvol) else f"{rvol*100:.0f}%"
        print(f"   盘前  {r['pre_price']:.2f} ({r['gap_pct']:+.1f}%)   "
              f"区间 {r['pre_low']:.2f}–{r['pre_high']:.2f}   "
              f"成交额 {_money(r['pre_turnover']).strip()}   $RVOL {rvol_s}")

        h52 = ("n/a" if math.isnan(r["high52"]) or not r["high52"]
               else f"{r['pre_price']/r['high52']*100:.0f}% of 52周高")
        print(f"   位置  {h52}   MA20 {r['ma20']:.2f}   ATR14 {r['atr14']:.2f}   "
              f"市值 {_money(r['market_cap']).strip()}   点差 {_pct(r['spread_pct'], 2)}")

        warn = _c("  ⚠ 止损过宽", "red") if r["too_wide"] else ""
        tgts = " / ".join(f"{t:.2f}" for t in r["targets"])
        print(f"   {_c('计划', 'mag')}  入场 {r['entry']:.2f}(破盘前{'高' if side == 'long' else '低'})  "
              f"止损 {r['stop']:.2f}(-{r['stop_pct']:.1f}%)  目标 {tgts}  "
              f"仓位 {r['shares']}股≈${r['notional']:,.0f}  风险 ${r['risk_usd']:,.0f}{warn}")

        if len(r["catalyst_evidence"]):
            print(_c(f"   催化  {r['catalyst_kind']}", "grn"))
            for e in list(r["catalyst_evidence"])[:3]:
                print(_c(f"         · {e}", "dim"))
        else:
            print(_c("   催化  未找到 —— 纯资金推动，日内回落概率显著更高", "yel"))


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="盘前动量选股（moomoo OpenD + SEC EDGAR）")
    p.add_argument("--side", choices=["long", "short", "both"], default="long")
    p.add_argument("--top", type=int, default=RUNTIME["final_n"])
    p.add_argument("--details", type=int, default=5, help="打印前 N 名的完整交易计划")
    p.add_argument("--no-news", action="store_true", help="跳过催化剂查询（快速预览）")
    p.add_argument("--account", type=float, help="账户规模，用于仓位计算")
    p.add_argument("--risk", type=float, help="单笔风险占账户百分比")
    p.add_argument("--min-turnover", type=float, help="盘前成交额下限（美元）")
    p.add_argument("--save", action="store_true", help="存 JSON 快照供事后前向验证")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)

    if a.account:
        TRADE["account_size"] = a.account
    if a.risk:
        TRADE["risk_per_trade_pct"] = a.risk
    if a.min_turnover:
        GATES["min_pre_turnover"] = a.min_turnover

    sess = session_label()
    banner = {
        "premarket": _c("✓ 盘前时段，数据实时", "grn"),
        "regular": _c("⚠ 已开盘 —— 盘前字段是今早的最终值，不再更新", "yel"),
        "afterhours": _c("⚠ 盘后时段 —— 盘前字段是今早的值", "yel"),
        "overnight": _c("✖ 夜盘时段（04:00 ET 前）—— 盘前字段仍是上一个交易日，仅供演练", "red"),
        "closed": _c("⚠ 休市 —— 盘前字段停留在最近一个交易日，仅供演练", "yel"),
    }[sess]
    print(_c(f"\n盘前动量扫描  {now_et():%Y-%m-%d %H:%M ET}  ", "bold") + banner)
    print(_c(f"权重 {WEIGHTS} | 账户 ${TRADE['account_size']:,.0f} 单笔风险 {TRADE['risk_per_trade_pct']}%", "dim"))

    sides = ["long", "short"] if a.side == "both" else [a.side]
    frames = []
    for s in sides:
        df = run_scan(side=s, top=a.top, use_news=not a.no_news, verbose=a.verbose)
        if df.empty:
            continue
        print(_c(f"\n\n{'█'*4} {'做多候选' if s == 'long' else '做空候选'} {'█'*4}", "bold"))
        print_table(df, side=s)
        print_details(df, n=a.details, side=s)
        frames.append(df)

    stale = any(float(f["pre_stale"].mean()) > 0.3 for f in frames if "pre_stale" in f)
    if a.save and stale:
        print(_c("\n--save 已跳过：盘前数据不是本交易日的，存下来会污染前向验证样本。", "red"))
    elif a.save and frames:
        allf = pd.concat(frames, ignore_index=True)
        cols = [c for c in allf.columns if c != "catalyst_evidence"]
        path = save_snapshot(dict(
            scanned_at=now_et().isoformat(),
            session=sess,
            weights=WEIGHTS,
            gates={k: (list(v) if isinstance(v, set) else v) for k, v in GATES.items()},
            rows=allf[cols].to_dict("records"),
        ))
        print(_c(f"\n快照已存: {path}", "dim"))
        print(_c("收盘后跑 `python -m premkt.evaluate` 做前向验证。", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
