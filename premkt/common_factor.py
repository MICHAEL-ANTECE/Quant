"""共同因子控制 —— 这个 gap 有多少是「今天大家都在涨」。

贡献自 Jose 的项目（Claude-Moomoo-Trading），分支 jose-premkt，2026-08-27。
**不改动任何既有权重或打分公式**，只增加一列诊断。改权重需要一个证明新权重更好
的检验，我没有；加一列可核对的诊断不需要。

问题
    `gap_quality` 是绝对量：对数正态曲线在 12% 达峰。它对「这只票涨 12%」和
    「今天整个池子都涨 12%，这只也涨 12%」给出完全相同的分数。前者是个股信息，
    后者是市场平移，两者的日内行为不一样。

    实测证据（Jose 项目，2026-08-27 盘前，n=16）：半导体复合体绝对涨幅 +1.2~8.6%
    看起来全线走强，按 beta 折算后**四个大盘名字里只有一个真正跑赢**（广度 25%），
    存储三只广度 **0%**、设备两只广度 **0%**。整条线是被 NVDA 一只拖着走的。
    只看绝对 gap 会把这批全部排进前列。

    同族的既有测量：Jose 项目 C0156 测得，一旦控制 MKT 与 TECH 因子，接下跌的刀
    没有 alpha，其宇宙的 bTECH ≈ 1.9；B23 测得 ASML/AMAT/TSM/MU 的个股独有方差
    份额只有 21–23%，即约 78% 的波动来自板块。

为什么用池子中位数，而不是对 SPY 估 beta
    1. 零额外数据。beta 需要基准的历史，`premkt/cache/klines/` 里没有 SPY/QQQ，
       拉它会给这个模块引入新的数据依赖和配额消耗。
    2. beta 会漂移。在一个逐日累积的样本上重估 beta，会让「测量口径变了」看起来
       像「世界变了」。
    3. 中位数对离群值稳健。一个 +278% 的小票（MB，2026-08-07）会把均值整个抬起来。

🔴 中位数必须在**硬门槛之前**的池子上算。
    在已经筛到 top-15 的名单上算中位数，等于用被筛选过的样本估计共同因子 ——
    那个样本恰恰是按「gap 大」选出来的，中位数会被自己要测的东西污染。
    `attach()` 因此接受 raw rank，并把 `n_pool` 一起带出来，让读者能判断这个
    中位数是在多大的池子上算的，而不是只能信。

读法
    gap_excess   gap 减去池子中位 gap。这是「相对今天大家」的超额。
    gap_ratio    gap ÷ 池子中位 gap。3.0 表示这只票走了共同幅度的三倍。
                 中位数接近 0 时不可用，此时返回 None 而不是一个巨大的比值。
    concentration p90 ÷ 中位。普涨时接近 1；少数名字远离池子时很大。
                 **不要用「上涨占比」**：本池是盘前涨幅榜，按构造几乎全为正，
                 该比例恒等于 ~100%，对任何输入都会给出「普涨」。

本模块只描述，不预测。
"""
from __future__ import annotations

import pandas as pd

GAP_COL_CANDIDATES = ("gap_pct", "pre_change_rate", "change_rate")
# 中位涨幅低于这个值时，比值失去意义（分母接近零）
MIN_MEDIAN_FOR_RATIO = 0.5


def _gap_col(df: pd.DataFrame) -> str | None:
    for c in GAP_COL_CANDIDATES:
        if c in df.columns:
            return c
    return None


def pool_stats(raw_rank: pd.DataFrame) -> dict:
    """在 **未经硬门槛筛选** 的池子上计算共同因子。"""
    col = _gap_col(raw_rank)
    if col is None or raw_rank.empty:
        return {"ok": False,
                "reason": f"池子里找不到 gap 列（试过 {GAP_COL_CANDIDATES}）"}
    g = pd.to_numeric(raw_rank[col], errors="coerce").dropna()
    if len(g) < 5:
        # 少于 5 只的池子算不出有意义的中位数；说出来而不是给一个数。
        return {"ok": False, "reason": f"池子仅 {len(g)} 只，不足以估计共同因子"}
    med = float(g.median())
    p90 = float(g.quantile(0.90))
    # 🔴 「上涨占比」在这里是废的，原因在数据的构造里。
    # 第一版直接搬了 Jose 项目 cockpit 的广度定义（涨的占几成）。那边的池子是
    # 双向 watchlist，广度有意义；**这里的池子是盘前涨幅榜**，按构造几乎全为正，
    # 于是广度恒等于 ~100%，对任何输入都给出「普涨」。构造测试里一只 +25%、
    # 其余 ~1% 的极端领涨池被判成了「普涨」，是这个模块自己的回归测试抓到的。
    # 有效的是**集中度**：p90 是中位的几倍。普涨接近 1，少数名字带动时很大。
    conc = (p90 / med) if abs(med) >= MIN_MEDIAN_FOR_RATIO else None
    return {"ok": True, "col": col, "n_pool": int(len(g)),
            "median_gap": round(med, 3),
            "mean_gap": round(float(g.mean()), 3),
            "p90_gap": round(p90, 3),
            "concentration": (round(conc, 2) if conc is not None else None),
            "breadth_pos": round(float((g > 0).mean()), 3)}


def attach(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """给候选表加三列诊断。stats 必须来自 `pool_stats(raw_rank)`。"""
    out = df.copy()
    if not stats.get("ok"):
        out["gap_excess"] = None
        out["gap_ratio"] = None
        out["pool_note"] = stats.get("reason", "共同因子不可用")
        return out
    col = _gap_col(out)
    if col is None:
        out["gap_excess"] = None
        out["gap_ratio"] = None
        out["pool_note"] = "候选表里找不到 gap 列"
        return out
    med = stats["median_gap"]
    g = pd.to_numeric(out[col], errors="coerce")
    out["gap_excess"] = (g - med).round(2)
    out["gap_ratio"] = ((g / med).round(2)
                        if abs(med) >= MIN_MEDIAN_FOR_RATIO else None)
    out["pool_note"] = (f"池 n={stats['n_pool']} · 中位 {med:+.2f}% · "
                        f"集中度 {stats.get('concentration')}")
    return out


def read(stats: dict) -> str:
    if not stats.get("ok"):
        return f"共同因子：{stats.get('reason')}"
    c = stats.get("concentration")
    if c is None:
        shape = "集中度不可用（中位涨幅接近 0，比值无意义）"
    elif c >= 3.0:
        shape = "领涨（少数名字远离池子）"
    elif c <= 1.6:
        shape = "普涨（个股信息被稀释）"
    else:
        shape = "分化"
    lines = [
        f"共同因子：池 n={stats['n_pool']} · 中位 gap "
        f"{stats['median_gap']:+.2f}% · p90 {stats['p90_gap']:+.2f}% · "
        f"集中度 p90/中位 " + (f"{c:.2f}" if c is not None else "—")
        + f" → {shape}",
        "  gap_excess 是减去中位数之后的部分；一只票 gap 很大但 "
        "gap_excess 很小，说明它只是跟着池子在动。",
        "  ⚠️ 不要用「上涨占比」判普涨/领涨：本池是涨幅榜，几乎全为正。",
    ]
    return "\n".join(lines)
