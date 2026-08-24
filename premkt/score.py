"""
特征工程 + 打分 + 交易计划。

打分结构:
    base  = Σ weight_i * factor_i        (各 factor 已归一化到 0~1)
    total = 100 * base * catalyst_mult

催化剂用乘数而不是加分项，是因为它是条件而非维度：
低流通盘 + 高 RVOL + 无消息，各分项都很漂亮，但历史上就是最容易日内崩掉的
那一类（本次实测样本 US.MB 盘前 +278% 收盘只剩 +138%，从盘前高点回落 37%）。
加分项会被其他维度平均掉，乘数不会。
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

from .config import (
    CATALYST_MULT,
    FLOAT_BUCKETS,
    GAP_CURVE,
    GATES,
    RVOL_SCALE,
    TRADE,
    WEIGHTS,
)


def _f(v, default=np.nan) -> float:
    """futu 的 N/A 会以字符串形式出现，统一转 float。"""
    try:
        if v is None or (isinstance(v, str) and v.strip() in ("", "N/A", "--")):
            return default
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 分项打分
# ---------------------------------------------------------------------------
def gap_quality(gap_pct: float) -> float:
    """对数正态曲线。peak 处 1.0，极端 gap 快速衰减 —— 因为开盘价已把消息 price in。"""
    g = abs(_f(gap_pct, 0.0))
    if g <= 0.5:
        return 0.0
    peak, sigma = GAP_CURVE["peak_pct"], GAP_CURVE["sigma"]
    return float(math.exp(-((math.log(g / peak)) ** 2) / (2 * sigma**2)))


def rvol_score(dollar_rvol: float) -> float:
    """盘前成交额 / 20日平均日成交额。对数刻度，floor 以下 0 分。"""
    r = _f(dollar_rvol, 0.0)
    lo, hi = RVOL_SCALE["floor"], RVOL_SCALE["saturate"]
    if r <= lo:
        return 0.0
    return float(min(1.0, math.log10(r / lo) / math.log10(hi / lo)))


def technical_score(pre_price: float, high52: float, low52: float, ma20: float,
                    side: str = "long") -> float:
    """相对 52 周极值和 MA20 的位置 —— 本质是"路径上还剩多少反向筹码"。

    这个分项必须区分方向：突破 52 周高的 gap 上方没有套牢盘；
    但同一只票对做空来说，创新高意味着趋势与你为敌。
    """
    p = _f(pre_price)
    if math.isnan(p) or p <= 0:
        return 0.3
    long = side == "long"
    parts, wts = [], []

    # 做多看离 52 周高多远，做空看离 52 周低多远
    ref = _f(high52) if long else _f(low52)
    if not math.isnan(ref) and ref > 0:
        ratio = p / ref if long else ref / p
        if ratio >= 1.0:
            s = 1.0                       # 创新高 / 创新低
        elif ratio >= 0.95:
            s = 0.85
        elif ratio >= 0.80:
            s = 0.60
        elif ratio >= 0.50:
            s = 0.35
        else:
            s = 0.15                      # 反向筹码密集区
        parts.append(s)
        wts.append(0.65)

    m = _f(ma20)
    if not math.isnan(m) and m > 0:
        ext = (p / m - 1.0) if long else (1.0 - p / m)
        # 顺势于 MA20 是必要条件；但偏离超过 60% 属于极端超买/超卖，反而扣分
        if ext <= 0:
            s = 0.25
        elif ext <= 0.60:
            s = 0.6 + 0.4 * min(ext / 0.30, 1.0)
        else:
            s = max(0.35, 1.0 - (ext - 0.60))
        parts.append(min(s, 1.0))
        wts.append(0.35)

    if not parts:
        return 0.3
    return float(np.average(parts, weights=wts))


def float_squeeze_score(float_shares: float, short_avail: float, enable_short: bool,
                        side: str = "long") -> float:
    """流通盘与融券可得性 —— 对多空是完全相反的信号。

    做多：小流通盘 + 难融券 = 逼空燃料（但只有催化剂成立时才有意义）。
    做空：同样的条件是"被轧空的风险"，在 7M 流通盘的难借票上做空正是爆仓的经典路径。
    """
    fs = _f(float_shares)
    base = 0.15
    if not math.isnan(fs) and fs > 0:
        for cap, s in FLOAT_BUCKETS:
            if fs <= cap:
                base = s
                break

    if side == "long":
        # 融券不可得 / 可借量占流通盘极低 -> 空头难以压制
        htb = 0.0
        if enable_short is False:
            htb = 0.20
        else:
            sa = _f(short_avail)
            if not math.isnan(sa) and not math.isnan(fs) and fs > 0:
                if sa / fs < 0.005:
                    htb = 0.15
                elif sa / fs < 0.02:
                    htb = 0.07
        return float(min(1.0, base + htb))

    # 做空侧：借不到券就根本无法执行，直接 0 分
    if enable_short is False:
        return 0.0
    score = 1.0 - base                    # 流通盘越大越安全
    sa = _f(short_avail)
    if not math.isnan(sa) and not math.isnan(fs) and fs > 0 and sa / fs < 0.005:
        score *= 0.5                      # 券源紧张 = 轧空风险
    return float(max(0.0, min(1.0, score)))


def liquidity_score(spread_pct: float, pre_turnover: float) -> float:
    """能不能真的成交。点差决定滑点，名义成交额决定容量。"""
    parts = []
    sp = _f(spread_pct)
    if math.isnan(sp):
        parts.append(0.5)
    elif sp <= 0.15:
        parts.append(1.0)
    elif sp <= 0.50:
        parts.append(0.8)
    elif sp <= 1.0:
        parts.append(0.5)
    else:
        parts.append(0.2)

    to = _f(pre_turnover, 0.0)
    if to >= 50_000_000:
        parts.append(1.0)
    elif to >= 10_000_000:
        parts.append(0.85)
    elif to >= 3_000_000:
        parts.append(0.6)
    elif to >= 1_000_000:
        parts.append(0.4)
    else:
        parts.append(0.15)
    return float(np.mean(parts))


# ---------------------------------------------------------------------------
# 特征工程
# ---------------------------------------------------------------------------
def _atr(df: pd.DataFrame, period: int) -> float:
    if df is None or len(df) < period + 1:
        return float("nan")
    h, l = df["high"].astype(float), df["low"].astype(float)
    pc = df["last_close"].astype(float)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(period).mean())


def enrich(rank: pd.DataFrame, snap: pd.DataFrame, klines: dict, today_et: dt.date) -> pd.DataFrame:
    """把盘前榜 + 快照 + 日线合成一张带全部派生指标的宽表。"""
    df = rank.merge(snap, left_on="security", right_on="code", how="inner", suffixes=("_rk", ""))

    rows = []
    for _, r in df.iterrows():
        code = r["code"]
        k = klines.get(code)
        # 关键：剔除"今天"这根 K 线，盘前扫描不能用当日数据
        if k is not None and len(k):
            k = k.copy()
            k["_d"] = pd.to_datetime(k["time_key"]).dt.date
            k = k[k["_d"] < today_et]

        avg_dollar_20 = float(k["turnover"].tail(20).astype(float).mean()) if k is not None and len(k) >= 5 else float("nan")
        avg_vol_20 = float(k["volume"].tail(20).astype(float).mean()) if k is not None and len(k) >= 5 else float("nan")
        ma20 = float(k["close"].tail(20).astype(float).mean()) if k is not None and len(k) >= 20 else float("nan")
        atr14 = _atr(k, TRADE["atr_period"]) if k is not None else float("nan")

        # 新鲜度交叉校验：快照的 prev_close_price 应当等于最近一根已完成日线的收盘。
        # 不相等就说明 pre_* 字段还停留在更早的交易日（04:00 ET 之前必然如此）。
        snap_prev = _f(r.get("prev_close_price"))
        kline_prev = float(k["close"].iloc[-1]) if k is not None and len(k) else float("nan")
        prev_close = snap_prev if not math.isnan(snap_prev) else kline_prev
        stale = False
        if not math.isnan(snap_prev) and not math.isnan(kline_prev) and kline_prev > 0:
            stale = abs(snap_prev - kline_prev) / kline_prev > 0.005

        # pre_* 数据实际属于哪个交易日：找到收盘价等于 snap_prev 的那根日线，
        # 它的下一根就是盘前数据所属的交易日。催化剂的时间窗口必须以它为基准，
        # 否则用墙上时钟会把真正的催化剂新闻滤掉。
        session_date = today_et
        if k is not None and len(k) and not math.isnan(snap_prev) and snap_prev > 0:
            closes = k["close"].astype(float).tolist()
            dates = k["_d"].tolist()
            for j in range(len(closes) - 1, -1, -1):
                if abs(closes[j] - snap_prev) / snap_prev < 0.002:
                    session_date = dates[j + 1] if j + 1 < len(dates) else today_et
                    break

        pre_price = _f(r.get("pre_price")) if not math.isnan(_f(r.get("pre_price"))) else _f(r.get("pre_market_price"))
        pre_to = _f(r.get("pre_turnover"), 0.0) or _f(r.get("pre_market_turnover"), 0.0)
        pre_vol = _f(r.get("pre_volume"), 0.0) or _f(r.get("pre_market_volume"), 0.0)
        gap = _f(r.get("pre_change_rate"))
        if math.isnan(gap):
            gap = _f(r.get("pre_market_change_ratio"), 0.0)

        ask, bid = _f(r.get("ask_price")), _f(r.get("bid_price"))
        mid = (ask + bid) / 2 if not (math.isnan(ask) or math.isnan(bid)) and (ask + bid) > 0 else float("nan")
        spread_pct = (ask - bid) / mid * 100 if not math.isnan(mid) and mid > 0 else float("nan")

        rows.append(dict(
            code=code,
            name=r.get("name", ""),
            side=r.get("side", "long"),
            pre_price=pre_price,
            pre_high=_f(r.get("pre_high_price")),
            pre_low=_f(r.get("pre_low_price")),
            gap_pct=gap,
            pre_turnover=pre_to,
            pre_volume=pre_vol,
            prev_close=prev_close,
            kline_prev_close=kline_prev,
            pre_stale=stale,
            session_date=session_date,
            avg_dollar_vol_20=avg_dollar_20,
            avg_vol_20=avg_vol_20,
            dollar_rvol=pre_to / avg_dollar_20 if avg_dollar_20 and avg_dollar_20 > 0 else float("nan"),
            share_rvol=pre_vol / avg_vol_20 if avg_vol_20 and avg_vol_20 > 0 else float("nan"),
            ma20=ma20,
            atr14=atr14,
            high52=_f(r.get("highest52weeks_price")),
            low52=_f(r.get("lowest52weeks_price")),
            float_shares=_f(r.get("outstanding_shares")),
            market_cap=_f(r.get("total_market_val")),
            spread_pct=spread_pct,
            enable_short_sell=bool(r.get("enable_short_sell", True)),
            short_available_volume=_f(r.get("short_available_volume")),
            exchange_type=r.get("exchange_type", ""),
            listing_date=r.get("listing_date", ""),
            update_time=r.get("update_time", ""),
        ))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 硬门槛
# ---------------------------------------------------------------------------
def apply_gates(df: pd.DataFrame, today_et: dt.date, side: str = "long") -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (通过, 被剔除并附原因)。剔除原因保留下来，方便复盘是不是门槛设错了。"""
    g = GATES
    reasons = []
    for _, r in df.iterrows():
        why = []
        p = _f(r["pre_price"])
        if math.isnan(p) or not (g["min_price"] <= p <= g["max_price"]):
            why.append(f"价格 {p:.2f} 越界")
        gp = _f(r["gap_pct"], 0.0)
        if side == "long" and gp < g["min_gap_pct"]:
            why.append(f"涨幅 {gp:.1f}% 不足")
        if side == "short" and gp > -g["min_gap_pct"]:
            why.append(f"跌幅 {gp:.1f}% 不足")
        if abs(gp) > g["max_gap_pct"]:
            why.append(f"涨幅 {gp:.0f}% 过极端")
        if _f(r["pre_turnover"], 0.0) < g["min_pre_turnover"]:
            why.append(f"盘前成交额 ${_f(r['pre_turnover'], 0)/1e6:.2f}M 不足")
        if _f(r["pre_volume"], 0.0) < g["min_pre_volume"]:
            why.append("盘前成交量不足")
        mc = _f(r["market_cap"])
        if not math.isnan(mc) and mc < g["min_market_cap"]:
            why.append(f"市值 ${mc/1e6:.0f}M 过小")
        sp = _f(r["spread_pct"])
        if not math.isnan(sp) and sp > g["max_spread_pct"]:
            why.append(f"点差 {sp:.2f}% 过宽")
        ex = str(r.get("exchange_type", ""))
        if ex and ex not in g["allowed_exchanges"]:
            why.append(f"交易所 {ex} 不在白名单")
        ld = str(r.get("listing_date", ""))
        if ld and not ld.startswith("1970"):
            try:
                d = dt.datetime.strptime(ld[:10], "%Y-%m-%d").date()
                if (today_et - d).days < g["min_days_listed"]:
                    why.append(f"上市仅 {(today_et - d).days} 天")
            except ValueError:
                pass
        reasons.append("; ".join(why))

    out = df.copy()
    out["reject_reason"] = reasons
    passed = out[out["reject_reason"] == ""].drop(columns=["reject_reason"]).reset_index(drop=True)
    rejected = out[out["reject_reason"] != ""].reset_index(drop=True)
    return passed, rejected


# ---------------------------------------------------------------------------
# 总分
# ---------------------------------------------------------------------------
def compute_scores(df: pd.DataFrame, catalysts: dict, side: str = "long") -> pd.DataFrame:
    """side 会改变催化剂的解读：财报暴雷对做多是反向信号，对做空是硬催化。"""
    out = df.copy()
    recs = []
    for _, r in out.iterrows():
        f_gap = gap_quality(r["gap_pct"])
        f_rvol = rvol_score(r["dollar_rvol"])
        f_tech = technical_score(r["pre_price"], r["high52"], r["low52"], r["ma20"], side)
        f_float = float_squeeze_score(r["float_shares"], r["short_available_volume"],
                                      r["enable_short_sell"], side)
        f_liq = liquidity_score(r["spread_pct"], r["pre_turnover"])

        base = (
            WEIGHTS["gap_quality"] * f_gap
            + WEIGHTS["rvol"] * f_rvol
            + WEIGHTS["technical"] * f_tech
            + WEIGHTS["float_squeeze"] * f_float
            + WEIGHTS["liquidity"] * f_liq
        )
        cat = catalysts.get(r["code"])
        label = cat.label_for(side) if cat else "none"
        mult = CATALYST_MULT[label]
        recs.append(dict(
            f_gap=round(f_gap, 3), f_rvol=round(f_rvol, 3), f_tech=round(f_tech, 3),
            f_float=round(f_float, 3), f_liq=round(f_liq, 3),
            base=round(base, 4),
            catalyst_label=label,
            catalyst_kind=cat.kind if cat else "未查询",
            catalyst_score=cat.score_for(side) if cat else 0.0,
            catalyst_materiality=cat.materiality if cat else 0.0,
            catalyst_direction=cat.direction if cat else 0.0,
            catalyst_headline=cat.headline if cat else "",
            catalyst_evidence=cat.evidence if cat else [],
            score=round(100 * base * mult, 1),
        ))
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(recs)], axis=1)


# ---------------------------------------------------------------------------
# 交易计划
# ---------------------------------------------------------------------------
def trade_plan(r: pd.Series, side: str = "long") -> dict:
    """入场 = 突破盘前高（做空则跌破盘前低）；止损取盘前极值与 ATR 的较紧者。"""
    t = TRADE
    atr = _f(r.get("atr14"))
    pre_high, pre_low = _f(r.get("pre_high")), _f(r.get("pre_low"))
    px = _f(r.get("pre_price"))

    if side == "long":
        entry = pre_high if not math.isnan(pre_high) and pre_high > 0 else px
        atr_stop = entry - t["atr_stop_mult"] * atr if not math.isnan(atr) else float("nan")
        cands = [x for x in (pre_low, atr_stop) if not math.isnan(x) and x < entry]
        stop = max(cands) if cands else entry * (1 - t["max_stop_pct"] / 100)
    else:
        entry = pre_low if not math.isnan(pre_low) and pre_low > 0 else px
        atr_stop = entry + t["atr_stop_mult"] * atr if not math.isnan(atr) else float("nan")
        cands = [x for x in (pre_high, atr_stop) if not math.isnan(x) and x > entry]
        stop = min(cands) if cands else entry * (1 + t["max_stop_pct"] / 100)

    risk_per_share = abs(entry - stop)
    stop_pct = risk_per_share / entry * 100 if entry else float("nan")
    budget = t["account_size"] * t["risk_per_trade_pct"] / 100
    shares = int(budget / risk_per_share) if risk_per_share > 0 else 0
    sign = 1 if side == "long" else -1
    targets = [round(entry + sign * m * risk_per_share, 2) for m in t["targets_r"]]

    return dict(
        entry=round(entry, 2),
        stop=round(stop, 2),
        stop_pct=round(stop_pct, 2),
        shares=shares,
        notional=round(shares * entry, 0),
        risk_usd=round(shares * risk_per_share, 0),
        targets=targets,
        too_wide=bool(stop_pct > t["max_stop_pct"]),
    )
