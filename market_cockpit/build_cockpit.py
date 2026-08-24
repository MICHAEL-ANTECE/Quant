#!/usr/bin/env python3
"""
市场风险驾驶舱 — 构建入口

用法:
    python3 build_cockpit.py                  # 用今天日期取数并生成 HTML
    python3 build_cockpit.py --date 2026-07-29
    python3 build_cockpit.py --dry-run        # 只打印评分，不写文件

产物: market_cockpit.html （单文件，可直接浏览器打开）

依赖: 本地 moomoo OpenD 必须在 127.0.0.1:11111 运行且已登录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import fetch_data as F

HERE = pathlib.Path(__file__).parent
TEMPLATE = HERE / "cockpit_template.html"
OUTPUT = HERE / "market_cockpit.html"

# ==========================================================================
# 人工输入区 —— 这些值无法从 OpenD 推导，必须手工维护并注明依据与日期。
# 每次更新请同步改 `source` 说明，页面会把它显示出来。
# ==========================================================================
MANUAL = {
    # 联邦基金目标利率区间
    "fed_rate_low": 3.50,
    "fed_rate_high": 3.75,
    # 政策立场分: -100 极鹰 .. +100 极鸽
    "fed_policy_stance": -55,
    "fed_stance_reason_en": (
        "Jul 29 meeting held rates 9–3, with all three dissenters "
        "(Hammack/Kashkari/Logan) favoring a 25bp hike — the first time since "
        "Sep 2016 that three dissented in the same direction. Chair Warsh cited "
        "\"no tolerance\" for persistently elevated inflation and has removed "
        "forward guidance. Combined with the June dot plot lifting the year-end "
        "median to 3.8% and core inflation to 3.3% → clearly hawkish."
    ),
    "fed_stance_reason": (
        "7/29 会议 9–3 票维持利率，三位反对者(Hammack/Kashkari/Logan)全部主张加息25bp，"
        "为2016年9月以来首次三人同向反对；主席 Warsh 称对持续高通胀「零容忍」，"
        "并已取消前瞻指引。叠加6月点阵图年末中值上调至3.8%、核心通胀预测上调至3.3% → 明确偏鹰"
    ),
    "fed_source_en": "FOMC 2026-07-29 decision + Bloomberg/CNBC vote tally + 2026-06-17 SEP",
    "fed_source": "FOMC 2026-07-29 决议 + Bloomberg/CNBC 票型 + 2026-06-17 SEP",
    # 注：立场分只跟随「美联储实际表态」变动，不因数据流自行调整 ——
    # 数据流由自动的宏观意外因子负责。8/12 CPI 同比 3.4%、核心 2.5%
    # 均与预期一致且继续回落，但美联储自 7/29 以来无新表态，故立场维持 -55。
    # 下次可更新节点：8/27–29 Jackson Hole、9/15–16 FOMC。
    #
    # 下次 FOMC 决议日（7/29 会议已结束，下次为 9/15–16，决议在第二日）
    "next_fomc": "2026-09-16",
    # 真实 VIX 点位（Futu 取不到 VIX 指数，需外部填；留 None 则用 SPY 已实现波动率代理）
    "vix": 15.84,
    "vix_source_en": "CBOE VIX close 15.84 on 2026-08-19",
    "vix_source": "CBOE VIX 2026-08-19 收盘 15.84",
    # 真实原油现货/期货报价。Futu 只有 USO/BNO 这类 ETF（跟踪期货、有滚动
    # 损耗），不等于油价，故真实报价须手工维护并注明日期。
    "brent_spot": 94.04,
    "wti_spot": 86.78,
    "oil_spot_source_en": "2026-08-21 close: Brent $94.04 / WTI $86.78 — second straight weekly gain as Hormuz shipments stall; new Iran sanctions detailed Mon 8/24",
    "oil_spot_source": "2026-08-21 收盘 Brent $94.04 / WTI $86.78 —— 霍尔木兹海峡运输近乎停摆，连续第二周上涨；8/24 周一公布对伊朗新制裁细节",
    # CNN 官方 Fear & Greed（0–100）作对照。页面主指标是自建代理指数，
    # 因为 CNN 的看跌看涨比率与广度分项 OpenD 取不到，无法自动化。
    "cnn_fear_greed": 65,
    "cnn_fear_greed_source_en": "CNN Fear & Greed reading 65 (Greed) on 2026-08-14",
    "cnn_fear_greed_source": "CNN Fear & Greed 2026-08-14 读数 65（贪婪）",
    # 权重（合计 1.0）
    "weights": {"fed": 0.30, "earnings": 0.30, "news": 0.20, "technical": 0.20},
}

LEVELS = [
    (-100, -60, 1,
     ("Strongly Bearish", "极度看空"),
     ("Multiple headwinds converging — defense first, favor cash and hedges.",
      "多重利空共振，防御优先，现金与对冲为主。")),
    (-60, -20, 2,
     ("Moderately Bearish", "谨慎看空"),
     ("Downside pressure dominates — trim into rallies, cap exposure.",
      "下行压力占优，逢反弹减仓，控制敞口。")),
    (-20, 20, 3,
     ("Neutral", "中性"),
     ("Bulls and bears deadlocked — stay balanced, wait for a catalyst.",
      "多空拉锯、方向不明，均衡配置、等待催化。")),
    (20, 60, 4,
     ("Moderately Bullish", "谨慎看多"),
     ("Risk appetite firming — participate but size down, avoid chasing.",
      "风偏温和抬升，可参与但控仓，警惕追高。")),
    (60, 100, 5,
     ("Strongly Bullish", "强烈看多"),
     ("Uptrend well established — ride it, treat dips as opportunity.",
      "多头趋势明确，顺势加仓，回调即机会。")),
]


def to_level(score: float):
    for lo, hi, n, label, sub in LEVELS:
        if lo <= score < hi or (n == 5 and score >= 60):
            return n, F.bi(*label), F.bi(*sub)
    return 3, F.bi(*LEVELS[2][3]), F.bi(*LEVELS[2][4])


# 市值权重档位：(key, 英文, 中文) —— key 供前端上色，文案随语言切换
CAP_TIERS = {
    "vhigh": ("Mega", "极高"),
    "high": ("Large", "高"),
    "mid": ("Mid", "中"),
    "low": ("Small", "低"),
}


def cap_tier(mc: float | None) -> str:
    if not mc:
        return "low"
    if mc >= 1.5e12:
        return "vhigh"
    if mc >= 5e11:
        return "high"
    if mc >= 1e11:
        return "mid"
    return "low"


def build(as_of: str, dry_run: bool = False) -> dict:
    print(f"[1/5] 连接 OpenD 取数 (as_of={as_of}) …", file=sys.stderr)
    snap = F.fetch_all(as_of)
    if snap.errors:
        print("  取数告警:", file=sys.stderr)
        for e in snap.errors[:8]:
            print("   -", e, file=sys.stderr)
    print(
        f"  财报 {len(snap.earnings)} 条 / 经济事件 {len(snap.econ)} 条 / "
        f"新闻 {len(snap.news)} 条 / SPY K线 {len(snap.spy_klines)} 根",
        file=sys.stderr,
    )

    print("[2/5] 构建 Top100 口径 …", file=sys.stderr)
    universe = F.top_universe(snap.earnings, top_n=100)
    print(f"  去重后 {len(universe)} 家（按市值降序）", file=sys.stderr)

    print("[3/5] 计算四因子 …", file=sys.stderr)
    e_score, e_detail = F.earnings_score(universe)
    t_score, t_detail = F.technical_score(snap.spy_klines)
    macro, macro_hits = F.macro_surprise(snap.econ, as_of)
    f_score = F.fed_score(MANUAL["fed_policy_stance"], macro)
    curve = F.treasury_curve(snap.econ, as_of)
    if snap.ticker:
        print("  行情条:", "  ".join(
            f"{t['label']} {t['last']}({t['chg']:+.2f}%)" if t['chg'] is not None
            else f"{t['label']} {t['last']}" for t in snap.ticker), file=sys.stderr)
    if curve:
        print("  美债招标收益率:", "  ".join(
            f"{c['tenor']['zh']} {c['yield']:.3f}" for c in curve), file=sys.stderr)

    fg = F.fear_greed(snap.spy_klines, snap.tlt_klines,
                      snap.hyg_klines, snap.lqd_klines)
    if fg["score"] is not None:
        print(f"  恐慌贪婪代理 {fg['score']} ({fg['zone']['zh']}) "
              f"— {fg['componentCount']}/5 分项可用", file=sys.stderr)
        for c in fg["components"]:
            print(f"    {c['name']['zh']:<8} {c['value']:>5}  {c['detail']['zh']}",
                  file=sys.stderr)
        if fg["missing"]:
            print(f"    [!] 缺失分项: {', '.join(m['zh'] for m in fg['missing'])}",
                  file=sys.stderr)
    infl = F.inflation_tracker(snap.econ, as_of)
    if infl["latest"]:
        print("  通胀追踪:", file=sys.stderr)
        for r in infl["latest"]:
            print(
                f"    {r['label']['zh']:<14} {r['actual']}"
                f"  (预期 {r['consensus']}, 前值 {r['previous']})  {r['date']}",
                file=sys.stderr,
            )
    n_score, news_scored = F.news_score(snap.news)

    # 因子不可用时（例如 K 线配额耗尽导致技术面取不到）必须把它剔除并
    # 重新归一化其余权重 —— 直接按 0 计入会让"没数据"看起来像"中性判断"。
    w = MANUAL["weights"]
    raw = {"fed": f_score, "earnings": e_score, "news": n_score,
           "technical": t_score}
    avail = {k: v for k, v in raw.items() if v is not None}
    missing = [k for k, v in raw.items() if v is None]
    wsum = sum(w[k] for k in avail) or 1.0
    eff_w = {k: w[k] / wsum for k in avail}
    composite = round(sum(avail[k] * eff_w[k] for k in avail), 1)
    lvl_n, lvl_label, lvl_sub = to_level(composite)
    if missing:
        print(
            f"  [!] 因子不可用: {', '.join(missing)} —— 已剔除并按 "
            f"{ {k: round(v, 3) for k, v in eff_w.items()} } 重新归一化权重",
            file=sys.stderr,
        )

    print("[4/5] 计算风险等级 …", file=sys.stderr)
    vix = MANUAL["vix"]
    vix_is_proxy = vix is None
    if vix_is_proxy:
        vix = t_detail.get("rv21") or 20.0
    evt, evt_detail = F.event_density(snap.econ, universe, as_of)
    r_lvl, r_score = F.risk_level(vix, evt)

    def _f(v):
        return f"{v:+.1f}" if v is not None else "  n/a"

    print(
        f"  Fed {_f(f_score)} | 财报 {_f(e_score)} | 新闻 {_f(n_score)} | "
        f"技术 {_f(t_score)}  =>  综合 {composite:+.1f} → 等级 {lvl_n} {lvl_label['zh']}",
        file=sys.stderr,
    )
    print(f"  VIX {vix} + 事件密集度 {evt} => 风险等级 {r_lvl}", file=sys.stderr)

    # ---- 组装给前端的 DATA ----
    upcoming = _s_date(as_of)
    earn_rows = []
    for e in universe[:40]:
        d = str(e.get("date") or "")[:10]
        earn_rows.append(
            {
                "date": d,
                # 前端按语言渲染，这里只给 key
                "session": {"BEFORE": "pre", "AFTER": "post"}.get(
                    str(e.get("pub_type")), "intra"
                ),
                "tk": str(e.get("security") or "").replace("US.", ""),
                "co": e.get("name"),
                "est": e.get("eps_predict"),
                "act": e.get("eps_actual"),
                "kind": F.classify_surprise(
                    e.get("eps_actual"), e.get("eps_predict")
                ),
                "mcap": e.get("market_cap"),
                "w": cap_tier(e.get("market_cap")),
            }
        )
    earn_rows.sort(key=lambda r: (r["date"], -(r["mcap"] or 0)))

    # 宏观日程：未来事件优先（那才是风险来源），其次才是刚公布的。
    # as_of 就是今天时用真实当前时刻，否则今天早些时候已公布的事件
    # 会被误判成"待公布"。
    if as_of == dt.date.today().strftime("%Y-%m-%d"):
        now_ts = dt.datetime.now().timestamp()
    else:
        now_ts = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp() + 86400
    econ_all = []
    for ev in snap.econ:
        star = str(ev.get("star")).upper()
        if star not in ("HIGH", "MEDIUM"):
            continue
        ts = ev.get("ts") or 0
        econ_all.append(
            {
                "title": ev["title"],
                "date": dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                if ts
                else "",
                "ts": ts,
                "star": ev["star"],
                "actual": ev["raw_actual"],
                "consensus": ev["raw_consensus"],
                "upcoming": ts >= now_ts,
            }
        )
    upcoming = sorted(
        [e for e in econ_all if e["upcoming"]],
        key=lambda r: (r["star"] != "HIGH", r["ts"]),
    )
    past = sorted(
        [e for e in econ_all if not e["upcoming"]],
        key=lambda r: -r["ts"],
    )
    econ_rows = upcoming[:10] + past[:4]

    # 按关键词轮转取新闻，否则第一个关键词(Federal Reserve)会占满整个面板
    by_kw: dict[str, list] = {}
    for n in news_scored:
        by_kw.setdefault(n.get("keyword", "?"), []).append(n)
    interleaved, idx = [], 0
    while len(interleaved) < 12:
        added = False
        for kw in by_kw:
            if idx < len(by_kw[kw]):
                interleaved.append(by_kw[kw][idx])
                added = True
                if len(interleaved) >= 12:
                    break
        if not added:
            break
        idx += 1

    news_rows = [
        {
            "hl": n["title"][:130],
            "src": n.get("source"),
            "when": str(n.get("date") or "")[5:] or n.get("publish_time"),
            "url": n.get("url"),
            "senti": n["senti"],
            "impact": n.get("impact"),
            "risk": n.get("risk"),
            "kw": n.get("keyword"),
        }
        for n in interleaved
    ]

    spark = [k["close"] for k in snap.spy_klines[-60:]]

    data = {
        "asOf": as_of,
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "composite": composite,
        "level": {"n": lvl_n, "label": lvl_label, "sub": lvl_sub},
        "risk": {
            "level": r_lvl,
            "score": r_score,
            "vix": vix,
            "vixIsProxy": vix_is_proxy,
            "vixSource": F.bi(MANUAL["vix_source_en"], MANUAL["vix_source"])
            if not vix_is_proxy
            else F.bi("Proxy: SPY 21-day realized volatility",
                      "SPY 21日已实现波动率代理"),
            "eventDensity": evt,
            "eventDetail": evt_detail,
        },
        "weightsRenormalized": bool(missing),
        "missingFactors": missing,
        "klineFromCache": snap.kline_from_cache,
        "klineCacheStale": snap.kline_cache_stale,
        "drivers": [
            {
                "key": "fed",
                "name": F.bi("Fed Policy Rate", "Fed 利率"),
                "weight": eff_w.get("fed", w["fed"]),
                "baseWeight": w["fed"],
                "score": f_score,
                "auto": False,
                "detail": {
                    "stance": MANUAL["fed_policy_stance"],
                    "stanceReason": F.bi(MANUAL["fed_stance_reason_en"],
                                         MANUAL["fed_stance_reason"]),
                    "macroSurprise": macro,
                    "macroHits": macro_hits[:6],
                    "rateLow": MANUAL["fed_rate_low"],
                    "rateHigh": MANUAL["fed_rate_high"],
                    "nextFOMC": MANUAL["next_fomc"],
                    "source": F.bi(MANUAL["fed_source_en"],
                                   MANUAL["fed_source"]),
                },
            },
            {
                "key": "earnings",
                "name": F.bi("Top 100 Earnings", "Top100 财报"),
                "weight": eff_w.get("earnings", w["earnings"]),
                "baseWeight": w["earnings"],
                "score": e_score,
                "auto": True,
                "detail": e_detail,
            },
            {
                "key": "news",
                "name": F.bi("Major News", "重大新闻"),
                "weight": eff_w.get("news", w["news"]),
                "baseWeight": w["news"],
                "score": n_score,
                "auto": True,
                "detail": {"count": len(news_scored), "method": "关键词启发式"},
            },
            {
                "key": "technical",
                "name": F.bi("Technical Sentiment", "技术面情绪"),
                "weight": eff_w.get("technical", w["technical"]),
                "baseWeight": w["technical"],
                "score": t_score,
                "auto": True,
                "detail": t_detail,
            },
        ],
        "ticker": snap.ticker,
        "treasuryCurve": curve,
        "oilSpot": {
            "brent": MANUAL.get("brent_spot"),
            "wti": MANUAL.get("wti_spot"),
            "source": F.bi(MANUAL.get("oil_spot_source_en", ""),
                           MANUAL.get("oil_spot_source", "")),
        },
        "fearGreed": {
            **fg,
            # CNN 官方值作对照 —— 人工维护，代理指数与它不会完全一致
            "cnnReference": MANUAL.get("cnn_fear_greed"),
            "cnnSource": F.bi(MANUAL.get("cnn_fear_greed_source_en", ""),
                              MANUAL.get("cnn_fear_greed_source", "")),
        },
        "inflation": infl,
        "earnings": earn_rows,
        "econ": econ_rows[:14],
        "news": news_rows,
        "spark": spark,
        "errors": snap.errors[:5],
    }

    if dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        return data

    print("[5/5] 渲染 HTML …", file=sys.stderr)
    render(data)
    print(f"  已写出 {OUTPUT}", file=sys.stderr)
    return data


def _s_date(s: str) -> str:
    return s


def render(data: dict) -> None:
    if not TEMPLATE.exists():
        raise SystemExit(f"模板缺失: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    start, end = "/*DATA_START*/", "/*DATA_END*/"
    i, j = html.find(start), html.find(end)
    if i == -1 or j == -1:
        raise SystemExit("模板里找不到 DATA_START / DATA_END 标记")
    html = html[: i + len(start)] + "\nconst DATA = " + payload + ";\n" + html[j:]
    OUTPUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    build(a.date, a.dry_run)
