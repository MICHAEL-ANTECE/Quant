"""
市场风险驾驶舱 — 数据层
从本地 moomoo/Futu OpenD (127.0.0.1:11111) 拉取真实数据。

已验证可用的接口（futu 10.09.6908）:
  get_earnings_calendar(market, begin_date, end_date)  -> 财报日历，含 eps_actual/eps_predict/market_cap
  get_economic_calendar(begin_date, end_date, ...)     -> 经济日历，含 star/previous/consensus/actual
  get_search_news(keyword, max_count)                  -> 新闻搜索
  request_history_kline(code, start, end, ktype)       -> 历史 K 线
  get_market_snapshot([codes])                         -> 实时快照

已知约束（实测）:
  * 两个日历接口的日期跨度都不能超过 7 天 -> 必须分片
  * get_economic_calendar 返回 4 元组, get_search_news 返回 2 元组,
    request_history_kline 返回 3 元组 -> 统一用 res[0]/res[1] 取
  * VIX 指数取不到 (US.VIX 报 "Unknown stock")，只有 VIXY 这个期货 ETF。
    真实 VIX 点位需外部输入，否则用 SPY 已实现波动率代理。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import time
from dataclasses import dataclass, field

from futu import (
    RET_OK,
    KLType,
    Market,
    NewsSubType,
    OpenQuoteContext,
)

HOST = "127.0.0.1"
PORT = 11111

# 一次 API 调用的最大日期跨度（OpenD 限制 7 天）
CHUNK_DAYS = 7

# 经济日历单页上限 50 条。窗口超限时改用递归二分而非翻页（游标有缺陷），
# 这里限制递归深度防止病态输入下无限拆分。
ECON_MAX_SPLIT_DEPTH = 5

# 经济日历用 3 天分片而非 7 天：3 天的事件数通常已在单页 50 条以内，
# 极少触发二分，总请求数远低于「7 天分片 + 频繁拆半」。
ECON_CHUNK_DAYS = 3

# OpenD 限流：经济日历 60 次/30 秒。二分会显著抬高请求数，
# 实测 7 天分片时直接打满并丢掉最后 12 天日历。留 10% 余量。
ECON_RATE_LIMIT = 54
ECON_RATE_WINDOW = 30.0
_econ_calls: list[float] = []


def _econ_throttle() -> None:
    """滑动窗口限流：只在逼近上限时才睡，平时零开销。"""
    now = time.monotonic()
    cutoff = now - ECON_RATE_WINDOW
    while _econ_calls and _econ_calls[0] < cutoff:
        _econ_calls.pop(0)
    if len(_econ_calls) >= ECON_RATE_LIMIT:
        sleep_for = ECON_RATE_WINDOW - (now - _econ_calls[0]) + 0.2
        if sleep_for > 0:
            time.sleep(sleep_for)
        cutoff = time.monotonic() - ECON_RATE_WINDOW
        while _econ_calls and _econ_calls[0] < cutoff:
            _econ_calls.pop(0)
    _econ_calls.append(time.monotonic())


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _d(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def _s(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def date_chunks(begin: str, end: str, size: int = CHUNK_DAYS):
    """把日期区间切成 <=size 天的片段，绕开 OpenD 的 7 天限制。"""
    b, e = _d(begin), _d(end)
    cur = b
    while cur <= e:
        stop = min(cur + dt.timedelta(days=size - 1), e)
        yield _s(cur), _s(stop)
        cur = stop + dt.timedelta(days=1)


def parse_num(v) -> float | None:
    """解析经济日历里的 '3.730%' / '16.50K' / '-0.2%' / '1.2M' 等字符串。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("N/A", "--", "-"):
        return None
    s = s.replace(",", "").replace("%", "")
    mult = 1.0
    if s and s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def num_or_none(v) -> float | None:
    """财报表里的数值字段可能是 'N/A' 字符串或 NaN。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "N/A", "nan", "None"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if math.isnan(f) else f


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def bi(en: str, zh: str) -> dict:
    """双语字符串。前端按当前语言取值，默认英文。"""
    return {"en": en, "zh": zh}


def clean_text(s) -> str:
    """去掉源数据里的转义反斜杠和多余空白。"""
    t = str(s or "")
    t = t.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    return re.sub(r"\s+", " ", t).strip()


def parse_news_time(pt: str, as_of: str) -> dt.date | None:
    """
    Futu 新闻的 publish_time 只有 'M/D' 或 'HH:MM'（当天），没有年份。
    以 as_of 为基准推断年份：若推出的日期明显在未来，则算上一年。
    """
    s = str(pt or "").strip()
    today = _d(as_of)
    if not s:
        return None
    if ":" in s and "/" not in s:
        return today  # 只有时间 = 当天
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if not m:
        return None
    mo, day = int(m.group(1)), int(m.group(2))
    for year in (today.year, today.year - 1):
        try:
            cand = dt.date(year, mo, day)
        except ValueError:
            continue
        if cand <= today + dt.timedelta(days=2):
            return cand
    return None


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------
@dataclass
class Snapshot:
    """一次完整取数的原始结果。"""

    as_of: str
    earnings: list = field(default_factory=list)
    econ: list = field(default_factory=list)
    news: list = field(default_factory=list)
    spy_klines: list = field(default_factory=list)
    tlt_klines: list = field(default_factory=list)
    hyg_klines: list = field(default_factory=list)
    lqd_klines: list = field(default_factory=list)
    spy_last: float | None = None
    vixy_last: float | None = None
    ticker: list = field(default_factory=list)
    kline_from_cache: bool = False
    kline_cache_stale: bool = False
    errors: list = field(default_factory=list)


# --------------------------------------------------------------------------
# K 线本地缓存（绕开历史 K 线配额）
# --------------------------------------------------------------------------
CACHE_DIR = pathlib.Path(__file__).parent / ".cache"


def _cache_path(code: str) -> pathlib.Path:
    return CACHE_DIR / f"kline_{code.replace('.', '_')}.json"


def load_kline_cache(
    code: str, as_of: str, allow_stale: bool = False
) -> list[dict] | None:
    p = _cache_path(code)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not allow_stale and blob.get("as_of") != as_of:
        return None
    rows = blob.get("rows") or []
    return rows or None


def save_kline_cache(code: str, as_of: str, rows: list[dict]) -> None:
    if not rows:
        return
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(code).write_text(
        json.dumps({"as_of": as_of, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )


def fetch_all(
    as_of: str,
    earn_back_days: int = 7,
    earn_fwd_days: int = 14,
    econ_back_days: int = 35,
    news_keywords: tuple[str, ...] = (
        "Federal Reserve",
        "stock market",
        "inflation",
        "earnings",
        "S&P 500",
    ),
    news_max_age_days: int = 5,
) -> Snapshot:
    """连一次 OpenD，把所有需要的数据一次性拉完。"""
    snap = Snapshot(as_of=as_of)
    q = OpenQuoteContext(host=HOST, port=PORT)
    try:
        today = _d(as_of)
        e_begin = _s(today - dt.timedelta(days=earn_back_days))
        e_end = _s(today + dt.timedelta(days=earn_fwd_days))

        # ---- 财报日历（分片） ----
        for b, e in date_chunks(e_begin, e_end):
            res = q.get_earnings_calendar(
                market=Market.US, begin_date=b, end_date=e
            )
            if res[0] != RET_OK:
                snap.errors.append(f"earnings {b}~{e}: {res[1]}")
                continue
            df = res[1]
            for _, r in df.iterrows():
                snap.earnings.append(
                    {
                        "security": r.get("security"),
                        "name": r.get("name"),
                        "date": r.get("earnings_date"),
                        "pub_type": r.get("pub_type"),
                        "period": r.get("period_text"),
                        "eps_actual": num_or_none(r.get("eps_actual")),
                        "eps_predict": num_or_none(r.get("eps_predict")),
                        "rev_actual": num_or_none(r.get("revenue_actual")),
                        "rev_predict": num_or_none(r.get("revenue_predict")),
                        "market_cap": num_or_none(r.get("market_cap")),
                        "price": num_or_none(r.get("price")),
                        "iv": num_or_none(r.get("iv")),
                    }
                )

        # ---- 经济日历（分片） ----
        # 回看 econ_back_days 天：CPI/PCE 是月度数据，只回看几天会整月拿不到
        # （实测 8/2 运行时 CPI 是 7/14 发布的，回看 3 天完全捞不着）。
        # 不要用 next_page 翻页 —— OpenD 的游标是**时间戳**，同一时刻的多条
        # 事件跨页时会被整组跳过。实测分片 7/28~8/03 共 89 条，page1 满 50 条后
        # token=1785414600000（正是 7/30 05:30），page2 从该时刻之后续取，
        # 把同一时刻剩下的事件全丢了：Core PCE Price Index (YoY) 与
        # Core PCE Prices 就此消失，而同批发布的 (MoM) 却在 —— 极难察觉。
        #
        # 改为递归二分：窗口若报 has_next 就说明超过单页容量，直接把窗口拆半
        # 重取，永远只用每个窗口的第一页。单日事件数远低于 50，必然收敛。
        def fetch_econ_window(b: str, e: str, depth: int = 0) -> list:
            _econ_throttle()
            res = q.get_economic_calendar(
                begin_date=b, end_date=e, market_list=[Market.US]
            )
            if res[0] != RET_OK:
                # 限流错误单独处理：等一个窗口后重试一次再放弃
                if "high frequency" in str(res[1]).lower():
                    time.sleep(ECON_RATE_WINDOW / 2)
                    _econ_throttle()
                    res = q.get_economic_calendar(
                        begin_date=b, end_date=e, market_list=[Market.US]
                    )
                if res[0] != RET_OK:
                    snap.errors.append(f"econ {b}~{e}: {res[1]}")
                    return []
            rows = res[1].to_dict("records")
            has_next = bool(res[3]) if len(res) > 3 else False
            if not has_next:
                return rows
            bd, ed = _d(b), _d(e)
            if bd >= ed or depth >= ECON_MAX_SPLIT_DEPTH:
                # 单日仍溢出（或递归过深）：只能接受截断，但要明确记录
                snap.errors.append(
                    f"econ {b}~{e}: 单窗口超过 50 条且无法再拆，可能有遗漏"
                )
                return rows
            mid = bd + (ed - bd) // 2
            return (fetch_econ_window(b, _s(mid), depth + 1)
                    + fetch_econ_window(_s(mid + dt.timedelta(days=1)), e, depth + 1))

        for b, e in date_chunks(_s(today - dt.timedelta(days=econ_back_days)),
                                _s(today + dt.timedelta(days=14)),
                                size=ECON_CHUNK_DAYS):
            for r in fetch_econ_window(b, e):
                ts = r.get("timestamp")
                snap.econ.append(
                    {
                        "title": r.get("title"),
                        "ts": float(ts) if ts is not None else None,
                        "star": r.get("star"),
                        "previous": parse_num(r.get("previous")),
                        "consensus": parse_num(r.get("consensus")),
                        "actual": parse_num(r.get("actual")),
                        "raw_actual": str(r.get("actual") or "").strip(),
                        "raw_consensus": str(r.get("consensus") or "").strip(),
                    }
                )

        # 按 (标题, 时间戳) 去重：分页与分片边界都可能带出重复事件，
        # 重复会让宏观意外的加权平均被同一条数据投多次票。
        _seen_ev, _uniq = set(), []
        for ev in snap.econ:
            key = (str(ev.get("title")), ev.get("ts"))
            if key in _seen_ev:
                continue
            _seen_ev.add(key)
            _uniq.append(ev)
        snap.econ = _uniq

        # ---- 新闻 ----
        # news_sub_type=NEWS 是关键：默认的 ALL 会混入 NOTICE(交易所公告) 和
        # RATING(评级报告)。实测「Federal Reserve」会命中 A 股「联储证券」的
        # 数月前公告，全部来自 NOTICE，指定 NEWS 即可根治。
        seen = set()
        for kw in news_keywords:
            res = q.get_search_news(
                keyword=kw, max_count=15, news_sub_type=NewsSubType.NEWS
            )
            if res[0] != RET_OK:
                snap.errors.append(f"news {kw}: {res[1]}")
                continue
            for _, r in res[1].iterrows():
                title = clean_text(r.get("title"))
                if not title or title in seen:
                    continue
                pt = str(r.get("publish_time") or "")
                pdate = parse_news_time(pt, as_of)
                # 只保留近 news_max_age_days 天的新闻，剔除陈旧命中
                if pdate is None or (_d(as_of) - pdate).days > news_max_age_days:
                    continue
                seen.add(title)
                snap.news.append(
                    {
                        "title": title,
                        "source": clean_text(r.get("source")),
                        "publish_time": pt,
                        "date": _s(pdate),
                        "url": r.get("url"),
                        "keyword": kw,
                    }
                )
        snap.news.sort(key=lambda n: n["date"], reverse=True)

        # ---- SPY 日线（算技术面 & 已实现波动率） ----
        # 历史 K 线有配额限制（实测 100 次/30 天，超了报
        # "Insufficient historical K-line quota"），所以先查本地缓存。
        # 缓存当天有效，反复重建页面不会烧配额。
        # 恐慌贪婪代理指数需要的债券/信用 ETF，与 SPY 一起用订阅式取
        for code, attr in (("US.TLT", "tlt_klines"), ("US.HYG", "hyg_klines"),
                           ("US.LQD", "lqd_klines")):
            c = load_kline_cache(code, as_of)
            if c is None:
                sub = q.subscribe([code], [KLType.K_DAY])
                if sub[0] == RET_OK:
                    time.sleep(1.2)
                    r = q.get_cur_kline(code, num=300, ktype=KLType.K_DAY)
                    if r[0] == RET_OK:
                        c = [
                            {"date": str(x["time_key"])[:10],
                             "close": float(x["close"])}
                            for _, x in r[1].iterrows()
                        ]
                        save_kline_cache(code, as_of, c)
                    else:
                        snap.errors.append(f"{code} cur_kline: {r[1]}")
                    q.unsubscribe([code], [KLType.K_DAY])
                else:
                    snap.errors.append(f"{code} subscribe: {sub[1]}")
            setattr(snap, attr, c or [])

        cached = load_kline_cache("US.SPY", as_of)
        if cached is not None:
            snap.spy_klines = cached
            snap.kline_from_cache = True
        else:
            # 优先走订阅式 get_cur_kline：它不消耗历史 K 线配额
            # （request_history_kline 配额实测 100 次/30 天，反复重建极易打满，
            #  报 "Insufficient historical K-line quota"）。
            rows = []
            sub = q.subscribe(["US.SPY"], [KLType.K_DAY])
            if sub[0] == RET_OK:
                time.sleep(1.5)  # 等订阅推送落地
                res = q.get_cur_kline("US.SPY", num=300, ktype=KLType.K_DAY)
                if res[0] == RET_OK:
                    rows = [
                        {"date": str(r["time_key"])[:10], "close": float(r["close"])}
                        for _, r in res[1].iterrows()
                    ]
                else:
                    snap.errors.append(f"spy cur_kline: {res[1]}")
                q.unsubscribe(["US.SPY"], [KLType.K_DAY])
            else:
                snap.errors.append(f"spy subscribe: {sub[1]}")

            # 订阅路径失败才退回历史接口（会消耗配额）
            if not rows:
                start = _s(today - dt.timedelta(days=400))
                res = q.request_history_kline(
                    "US.SPY", start=start, end=as_of,
                    ktype=KLType.K_DAY, max_count=400,
                )
                if res[0] == RET_OK:
                    rows = [
                        {"date": str(r["time_key"])[:10], "close": float(r["close"])}
                        for _, r in res[1].iterrows()
                    ]
                else:
                    snap.errors.append(f"spy history kline: {res[1]}")

            if rows:
                snap.spy_klines = rows
                save_kline_cache("US.SPY", as_of, rows)
            else:
                # 都失败时退回最近一次缓存（即使过期），并标注出来
                stale = load_kline_cache("US.SPY", as_of, allow_stale=True)
                if stale:
                    snap.spy_klines = stale
                    snap.kline_from_cache = True
                    snap.kline_cache_stale = True
        if snap.spy_klines:
            snap.spy_last = snap.spy_klines[-1]["close"]

        # ---- VIXY 快照（VIX 指数取不到，仅作参考） ----
        res = q.get_market_snapshot(["US.VIXY"])
        if res[0] == RET_OK and len(res[1]):
            snap.vixy_last = float(res[1]["last_price"].iloc[0])

        # ---- 顶部行情条 ----
        snap.ticker = market_ticker(q, snap)

    finally:
        q.close()

    return snap


# --------------------------------------------------------------------------
# 技术面 / 波动率
# --------------------------------------------------------------------------
def realized_vol(closes: list[float], window: int = 21) -> float | None:
    """年化已实现波动率(%)，作为 VIX 不可得时的代理。"""
    if len(closes) < window + 1:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100


def technical_score(klines: list[dict]) -> tuple[float | None, dict]:
    """
    技术面因子分 (-100..100)，数据不足时返回 None。

    返回 None 而不是 0 是刻意的：0 在页面上等同于"市场中性"，
    会把"取数失败"伪装成一个真实判断。上游必须把 None 当作
    "该因子不可用"处理，并重新归一化其余因子的权重。
    """
    closes = [k["close"] for k in klines]
    if len(closes) < 200:
        return None, {
            "unavailable": True,
            "note": bi(f"Only {len(closes)} bars (200 required) — "
                       f"technical factor unavailable",
                       f"K 线仅 {len(closes)} 根（需 200 根），技术面因子不可用"),
        }

    last = closes[-1]
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200
    mom20 = (last / closes[-21] - 1) * 100

    # 每项归一到 -100..100
    s_ma50 = clamp((last / ma50 - 1) * 100 * 20, -100, 100)
    s_ma200 = clamp((last / ma200 - 1) * 100 * 8, -100, 100)
    s_mom = clamp(mom20 * 12, -100, 100)

    score = (s_ma50 + s_ma200 + s_mom) / 3
    detail = {
        "last": round(last, 2),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "mom20_pct": round(mom20, 2),
        "above_ma50": last > ma50,
        "above_ma200": last > ma200,
        "rv21": round(realized_vol(closes) or 0, 1),
    }
    return round(clamp(score, -100, 100), 1), detail


# --------------------------------------------------------------------------
# 恐慌贪婪指数（代理）
# --------------------------------------------------------------------------
# CNN 官方 Fear & Greed 用 7 个分项，其中看跌看涨期权比率与 McClellan
# 广度指标 OpenD 拿不到，所以这里用 **5 个能自动取数的分项** 自建代理。
# 它不是 CNN 官方值，量级会接近但不会一致 —— 页面上必须标注清楚。
#
# 每个分项归一到 0–100：0 = 极度恐慌，50 = 中性，100 = 极度贪婪。
FG_ZONES = [
    (0, 25, ("Extreme Fear", "极度恐慌")),
    (25, 45, ("Fear", "恐慌")),
    (45, 55, ("Neutral", "中性")),
    (55, 75, ("Greed", "贪婪")),
    (75, 101, ("Extreme Greed", "极度贪婪")),
]


def _norm(v: float, lo: float, hi: float) -> float:
    """把 v 从 [lo,hi] 线性映射到 0–100 并夹紧。"""
    if hi == lo:
        return 50.0
    return clamp((v - lo) / (hi - lo) * 100, 0, 100)


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-1 - n] <= 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


def fear_greed(
    spy: list[dict],
    tlt: list[dict],
    hyg: list[dict],
    lqd: list[dict],
) -> dict:
    """
    恐慌贪婪代理指数 0–100 + 各分项明细。
    数据不足的分项会被跳过并在 missing 里列出，总分只对可用分项取平均
    （与四因子一致：缺数据就剔除，绝不按中性 50 填充）。
    """
    comps: list[dict] = []
    missing: list[str] = []
    sc = [k["close"] for k in spy]

    # 1 动量：SPY 相对 125 日均线。
    # 区间取 ±14%：原先用 ±8% 会在任何一轮正常上涨中就顶到 100
    # （实测 SPY 高于 MA125 8.0% 时读数 99.9），和硬截断一样失去分辨率。
    # 牛市中 SPY 高于 125 日线 12–15% 并不罕见，±14% 才留得住区分度。
    if len(sc) >= 125:
        ma125 = sum(sc[-125:]) / 125
        dev = (sc[-1] / ma125 - 1) * 100
        comps.append({
            "key": "momentum", "name": bi("Market Momentum", "市场动量"),
            "value": round(_norm(dev, -14, 14), 1),
            "detail": bi(f"SPY {sc[-1]:.2f} vs MA125 {ma125:.2f} ({dev:+.1f}%)",
                         f"SPY {sc[-1]:.2f} vs MA125 {ma125:.2f} ({dev:+.1f}%)"),
        })
    else:
        missing.append(bi("Market Momentum", "市场动量"))

    # 2 价格强度：当前价在 52 周区间中的位置
    if len(sc) >= 252:
        win = sc[-252:]
        lo, hi = min(win), max(win)
        pos = (sc[-1] - lo) / (hi - lo) * 100 if hi > lo else 50
        comps.append({
            "key": "strength", "name": bi("Price Strength", "价格强度"),
            "value": round(clamp(pos, 0, 100), 1),
            "detail": bi(f"52w range {lo:.0f}–{hi:.0f}, now at {pos:.0f}%",
                         f"52周区间 {lo:.0f}–{hi:.0f}，当前位于 {pos:.0f}%"),
        })
    else:
        missing.append(bi("Price Strength", "价格强度"))

    # 3 波动率：21 日已实现波动率 相对 100 日均值。波动低=贪婪，故反向
    rv_now = realized_vol(sc, 21)
    rv_base = realized_vol(sc, 100)
    if rv_now is not None and rv_base is not None and rv_base > 0:
        ratio = rv_now / rv_base
        comps.append({
            "key": "vol", "name": bi("Volatility", "波动率"),
            # ratio 0.6(极静) → 100 贪婪; 1.6(剧烈) → 0 恐慌
            "value": round(_norm(-ratio, -1.6, -0.6), 1),
            "detail": bi(f"21d realized {rv_now:.1f} vs 100d {rv_base:.1f}"
                         f" (ratio {ratio:.2f})",
                         f"21日已实现 {rv_now:.1f} vs 100日 {rv_base:.1f}"
                         f"（比值 {ratio:.2f}）"),
        })
    else:
        missing.append(bi("Volatility", "波动率"))

    # 4 避险需求：股 vs 长债 20 日相对收益。股票跑赢=贪婪
    # 同样放宽到 ±12%：20 日股债差在趋势期轻易超过 8%
    s20, t20 = _ret(sc, 20), _ret([k["close"] for k in tlt], 20)
    if s20 is not None and t20 is not None:
        spread = s20 - t20
        comps.append({
            "key": "haven", "name": bi("Safe-Haven Demand", "避险需求"),
            "value": round(_norm(spread, -12, 12), 1),
            "detail": bi(f"20d SPY {s20:+.1f}% vs TLT {t20:+.1f}%"
                         f" (gap {spread:+.1f}%)",
                         f"20日 SPY {s20:+.1f}% vs TLT {t20:+.1f}%"
                         f"（差 {spread:+.1f}%）"),
        })
    else:
        missing.append(bi("Safe-Haven Demand", "避险需求"))

    # 5 信用偏好：垃圾债 vs 投资级 20 日相对收益。垃圾债跑赢=风险偏好高
    h20, l20 = _ret([k["close"] for k in hyg], 20), _ret([k["close"] for k in lqd], 20)
    if h20 is not None and l20 is not None:
        spread = h20 - l20
        comps.append({
            "key": "credit", "name": bi("Junk Bond Demand", "信用偏好"),
            "value": round(_norm(spread, -4, 4), 1),
            "detail": bi(f"20d HYG {h20:+.1f}% vs LQD {l20:+.1f}%"
                         f" (gap {spread:+.1f}%)",
                         f"20日 HYG {h20:+.1f}% vs LQD {l20:+.1f}%"
                         f"（差 {spread:+.1f}%）"),
        })
    else:
        missing.append(bi("Junk Bond Demand", "信用偏好"))

    if not comps:
        return {"score": None, "zone": None, "components": [],
                "missing": missing,
                "note": bi("No component data available",
                           "所有分项数据均不可用")}

    score = round(sum(c["value"] for c in comps) / len(comps), 1)
    _z = next(z for lo, hi, z in FG_ZONES if lo <= score < hi)
    zone = bi(_z[0], _z[1])
    return {
        "score": score,
        "zone": zone,
        "components": comps,
        "missing": missing,
        "componentCount": len(comps),
    }


# --------------------------------------------------------------------------
# 财报因子
# --------------------------------------------------------------------------
def top_universe(earnings: list[dict], top_n: int = 100) -> list[dict]:
    """
    Top100 口径: 在取到的财报日历里按 market_cap 降序取前 N。
    注意这是"本窗口内有财报的公司中市值最大的 N 家"，
    不等同于"全市场市值前 100"——但对财报驱动的研判正是我们要的口径。
    """
    have_cap = [e for e in earnings if e.get("market_cap")]
    have_cap.sort(key=lambda x: x["market_cap"], reverse=True)
    # 同一公司可能在分片重叠处出现多次，去重
    seen, out = set(), []
    for e in have_cap:
        if e["security"] in seen:
            continue
        seen.add(e["security"])
        out.append(e)
        if len(out) >= top_n:
            break
    return out


# 判定"单位/币种口径不一致"的倍数阈值。
# 实测 SK hynix 预期 4.87 / 实际 90.36 = 18.6 倍（韩元 vs 美元）——
# 这种量级不可能是真实业绩，剔除。
#
# 关键区分：只有"同号且倍数极端"才算口径问题。
# Intel 预期 0.08 / 实际 -2.16 换算成百分比是 -2800%，但它是
# 真实的大额减值，只是基数太小把百分比放大了。若按百分比阈值
# 一刀切剔除，就把真实利空从计分里抹掉，因子会系统性偏多。
# 这类符号翻转一律保留，按 ±25% 截断计入。
SUSPECT_RATIO = 5.0

# 实际与预期相差在此比例内视为"符合预期"，避免 0.3203 vs 0.3200
# 这种四舍五入后显示相同、却被判成"不及"的情况。
INLINE_TOLERANCE_PCT = 0.5


def classify_surprise(act: float | None, est: float | None) -> str:
    """
    beat / miss / inline / suspect / na —— 与计分逻辑保持一致。

    suspect 仅用于"同号且量级差 5 倍以上"的单位或币种错配；
    符号翻转（预期盈利、实际亏损）是真实业绩，不算 suspect。
    """
    if act is None or est is None or est == 0:
        return "na"
    same_sign = (act >= 0) == (est >= 0)
    if same_sign and abs(act) > abs(est) * SUSPECT_RATIO:
        return "suspect"
    pct = (act - est) / abs(est) * 100
    if abs(pct) <= INLINE_TOLERANCE_PCT:
        return "inline"
    return "beat" if pct > 0 else "miss"


def earnings_score(universe: list[dict]) -> tuple[float, dict]:
    """
    财报因子分 (-100..100)：已公布公司的 EPS 超预期幅度，按 √市值 加权。

    两道数据清洗:
      1. |surprise| > 100% 判为口径异常，整条剔除并单独计数
      2. |surprise| <= 0.5% 记为"符合预期"，计入样本但不算超预期
    剩余 surprise 仍截断在 ±25%，避免小基数 EPS 放大噪声。
    """
    num = den = 0.0
    reported = beats = misses = inline = suspect = 0
    suspect_names = []

    for e in universe:
        act, est = e.get("eps_actual"), e.get("eps_predict")
        kind = classify_surprise(act, est)
        if kind == "na":
            continue
        if kind == "suspect":
            suspect += 1
            if len(suspect_names) < 4:
                suspect_names.append(
                    str(e.get("security") or "").replace("US.", "")
                )
            continue

        reported += 1
        if kind == "beat":
            beats += 1
        elif kind == "miss":
            misses += 1
        else:
            inline += 1

        surprise = clamp((act - est) / abs(est) * 100, -25, 25)
        w = math.sqrt(e["market_cap"])  # 市值开方，压缩巨头的绝对统治力
        num += surprise * w
        den += w

    if den == 0:
        return 0.0, {"reported": 0, "beats": 0, "beat_rate": None,
                     "suspect": suspect, "note": "窗口内尚无有效已公布财报"}

    avg_surprise = num / den
    score = clamp(avg_surprise * 4, -100, 100)
    return round(score, 1), {
        "reported": reported,
        "beats": beats,
        "misses": misses,
        "inline": inline,
        "beat_rate": round(beats / reported * 100) if reported else None,
        "avg_surprise_pct": round(avg_surprise, 2),
        "suspect": suspect,
        "suspect_names": suspect_names,
    }


# --------------------------------------------------------------------------
# 宏观 / Fed 因子
# --------------------------------------------------------------------------
# 指标方向: +1 表示"实际值高于预期"对股市偏多; -1 表示偏空
# 通胀高于预期 -> 鹰派 -> 利空; 失业率高于预期 -> 利空;
# 增长/消费高于预期 -> 温和利多
INDICATOR_DIRECTION = [
    (r"\bcore\s+pce|pce price|\bcpi\b|\bppi\b|inflation", -1.0),
    (r"unemployment rate|jobless claims|continuing claims", -1.0),
    (r"nonfarm|payroll|adp employment", +0.6),
    (r"\bgdp\b|retail sales|durable goods|industrial production", +0.8),
    (r"consumer confidence|consumer sentiment|ism|pmi", +0.7),
]


def macro_surprise(econ: list[dict], as_of: str) -> tuple[float, list[dict]]:
    """
    宏观意外分 (-100..100)：只统计已公布(actual 存在)且有 consensus 的事件。
    对每个能识别方向的指标计算 (actual-consensus)/|consensus| 的标准化意外，
    乘方向系数与重要度权重后取加权平均。
    """
    star_w = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}
    # 严格用"真实当前时刻"作为已公布的分界。
    # 实测 Futu 经济日历会给未来事件预填 actual（145 个事件里 46 个如此，
    # 例如 8/6 的初请失业金在 8/2 就写着 199），这些是占位值不是真实公布值。
    # 早先版本留了 +1 天宽限，会把这类占位数据放进计分，必须去掉。
    as_of_ts = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp()
    cutoff = min(dt.datetime.now().timestamp(), as_of_ts + 86400)
    num = den = 0.0
    hits = []
    skipped_future = 0
    for ev in econ:
        act, cons = ev.get("actual"), ev.get("consensus")
        if act is None or cons is None or cons == 0:
            continue
        if ev.get("ts") and ev["ts"] >= cutoff:
            skipped_future += 1
            continue  # 未公布事件的 actual 是占位值，一律不计分
        title = (ev.get("title") or "").lower()
        direction = None
        for pat, d in INDICATOR_DIRECTION:
            if re.search(pat, title):
                direction = d
                break
        if direction is None:
            continue
        w = star_w.get(str(ev.get("star")).upper(), 0.2)
        surprise = clamp((act - cons) / abs(cons) * 100, -50, 50)
        contrib = surprise * direction
        num += contrib * w
        den += w
        hits.append(
            {
                "title": ev["title"],
                "star": ev["star"],
                "actual": ev["raw_actual"],
                "consensus": ev["raw_consensus"],
                "direction": direction,
                "surprise_pct": round(surprise, 2),
            }
        )
    if den == 0:
        return 0.0, hits
    return round(clamp(num / den * 2.5, -100, 100), 1), hits


# 通胀追踪：这几个是最能推动市场的价格指标，单独提出来做面板。
# 匹配用精确标题（Futu 的 title 很规整），避免 "Cleveland CPI"、
# "CPI Index, n.s.a." 这些次要口径混进来。
INFLATION_SERIES = [
    ("CPI (YoY)", ("CPI YoY", "CPI 同比"), 2.0),
    ("Core CPI (YoY)", ("Core CPI YoY", "核心 CPI 同比"), 2.0),
    ("CPI (MoM)", ("CPI MoM", "CPI 环比"), None),
    ("Core CPI (MoM)", ("Core CPI MoM", "核心 CPI 环比"), None),
    ("PCE Price index (YoY)", ("PCE YoY", "PCE 同比"), 2.0),
    ("Core PCE Price Index (YoY)", ("Core PCE YoY", "核心 PCE 同比"), 2.0),
]


def inflation_tracker(econ: list[dict], as_of: str) -> dict:
    """
    抽出通胀指标的「最近一次已公布」与「下一次待公布」。

    严格按真实时刻切分 —— Futu 会给未来事件预填 actual，
    不能凭 actual 是否存在来判断有没有发布。
    """
    as_of_ts = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp()
    cutoff = min(dt.datetime.now().timestamp(), as_of_ts + 86400)

    out: dict = {"latest": [], "next_release": None}
    by_title: dict[str, list] = {}
    for ev in econ:
        t = str(ev.get("title") or "").strip()
        by_title.setdefault(t, []).append(ev)

    next_ts = None
    for title, label, target in INFLATION_SERIES:
        evs = sorted(by_title.get(title, []), key=lambda e: e.get("ts") or 0)
        released = [e for e in evs if (e.get("ts") or 0) < cutoff
                    and e.get("actual") is not None]
        pending = [e for e in evs if (e.get("ts") or 0) >= cutoff]

        if pending:
            ts = pending[0].get("ts")
            if ts and (next_ts is None or ts < next_ts):
                next_ts = ts

        if not released:
            continue
        last = released[-1]
        prev = released[-2] if len(released) > 1 else None
        act, cons = last.get("actual"), last.get("consensus")
        out["latest"].append(
            {
                "label": bi(label[0], label[1]),
                "title": title,
                "date": dt.datetime.fromtimestamp(last["ts"]).strftime("%Y-%m-%d")
                if last.get("ts") else "",
                "actual": act,
                "consensus": cons,
                "previous": last.get("previous"),
                # 相对预期：低于预期为"降温"
                "vs_consensus": (round(act - cons, 2)
                                 if act is not None and cons is not None else None),
                # 相对上期：判断趋势方向
                "vs_previous": (round(act - last["previous"], 2)
                                if act is not None and last.get("previous") is not None
                                else None),
                "target": target,
                "above_target": (act > target) if (target and act is not None) else None,
                "prev_reading": prev.get("actual") if prev else None,
            }
        )

    if next_ts:
        out["next_release"] = dt.datetime.fromtimestamp(next_ts).strftime(
            "%Y-%m-%d %H:%M"
        )
    return out


def fed_score(policy_stance: float, macro: float, w_stance: float = 0.65):
    """
    Fed 因子分 = 政策立场分(人工输入) × w + 宏观意外分(自动) × (1-w)。

    policy_stance 必须人工给定并注明依据 —— 点阵图、票委表态、
    市场隐含路径这些无法从 OpenD 自动推导。范围 -100(极鹰) .. +100(极鸽)。
    """
    return round(clamp(policy_stance * w_stance + macro * (1 - w_stance),
                       -100, 100), 1)


# --------------------------------------------------------------------------
# 新闻因子（关键词启发式 —— 强度有限，默认可被人工覆盖）
# --------------------------------------------------------------------------
# 强否定/反转短语 —— 必须最先判定，否则 "hopes for a rate cut dashed"
# 会因含 "rate cut" 被误判为利多。实测踩到过这个假阳性。
OVERRIDE_RULES: list[tuple[str, str]] = [
    (r"(hope|bet|expectation)s?\b.{0,30}\b(dash|fade|evaporat|crush|dampen)", "neg"),
    (r"\b(no|not|without|rules? out|off the table)\b.{0,15}\brate cut", "neg"),
    (r"rate cut.{0,20}\b(unlikely|delayed|pushed back|off the table)", "neg"),
    (r"\bdissent\w*\b.{0,30}\brate hike", "neg"),
    # 通胀方向：同一个动词在通胀语境下含义与股市相反 ——
    # 通胀"升温"是利空，"降温"是利好。必须成对写，且要先于词表判定。
    (r"inflation\w*\b.{0,25}\b(heat|accelerat|re-?accelerat|pick(s|ed)? up|"
     r"climb|jump|surge|rise|rising|hotter)", "neg"),
    (r"\b(hot|hotter|sticky|stubborn)\w*\b.{0,15}inflation", "neg"),
    (r"inflation\w*\b.{0,25}\b(cool|ease|eased|easing|slow|decelerat|"
     r"retreat|fall|fell|drop|moderat)", "pos"),
]

# ---- 紧缩预期方向 ----------------------------------------------------------
# 「加息预期降温」对股市是利好，「加息预期升温」是利空。
# 难点在于两者都会出现 fall/fade 这类词，必须和「股市在下跌」区分开：
#   "Dollar Falls Sharply on Fading Prospect of Fed Raising Rates"  → 鸽派利好
#   "Stocks fall as Fed signals rate hikes"                          → 利空
# 判别依据是 fade 词附近有没有「预期/前景/概率」这类名词 —— 降温的是
# *预期*才算鸽派。只按词序写正则会漏（实测这两条都是 fade 词在前）。
TIGHTEN_RE = re.compile(
    r"(rate hikes?|rate rises?|rate increases?|rais(e|ing) rates|"
    r"hike rates|tighten\w*|higher rates)"
)
EXPECT_RE = re.compile(
    r"(prospect|probabilit|odds|chance|expectation|bet|outlook|forecast|"
    r"pricing|wager)"
)
FADE_RE = re.compile(
    r"(fade|fading|faded|drop|dropped|fall|falls|fell|declin\w*|dampen\w*|"
    r"dimin\w*|eas(e|ed|ing)|lower|reduc\w*|unlikely|off the table|"
    r"retreat\w*|recede\w*|slim\w*|cool\w*)"
)
RISE_RE = re.compile(
    r"(ris(e|es|ing)|rose|climb\w*|increas\w*|jump\w*|mount\w*|grow\w*|"
    r"surg\w*|intensif\w*|firm\w*|higher odds|stronger)"
)


def tightening_expectation_verdict(t: str) -> str | None:
    """紧缩预期方向 → 'pos'(降温) / 'neg'(升温) / None(不适用)。"""
    if not TIGHTEN_RE.search(t) or not EXPECT_RE.search(t):
        return None
    fade, rise = FADE_RE.search(t), RISE_RE.search(t)
    if fade and not rise:
        return "pos"
    if rise and not fade:
        return "neg"
    if fade and rise:
        # 两类词都有时，取更靠近「预期」名词的那个
        e = EXPECT_RE.search(t).start()
        return "pos" if abs(fade.start() - e) < abs(rise.start() - e) else "neg"
    return None

# 兜底规则 —— 仅在词表判不出方向时生效。做成兜底而非覆盖，
# 否则 "SPY, QQQ Lose Steam as Fed Holds Rates" 会被"维持利率"
# 这条规则判成中性，把明确的利空信息盖掉。
FALLBACK_RULES: list[tuple[str, str]] = [
    (r"\b(holds?|held|maintain\w*|leaves?|unchanged|steady)\b.{0,25}"
     r"\b(rate|policy|funds rate)", "neu"),
]

# 用**词干**而非枚举词形。之前列表里同时有 beat/beats、miss/misses，
# 一个词会被数两次，把强度虚推到极端（"Tesla misses EPS" 被判成强利空）。
# 计数时统一用 \b{stem}(s|es|ed|ing)?\b 匹配，每个词干只记一次。
POS_WORDS = [
    "beat", "surge", "rally", "record high", "strong", "upgrade",
    "optimism", "gain", "jump", "soar", "top estimate", "boost",
    "rate cut", "dovish", "recovery", "outperform", "all-time high",
    "better than expected", "raise guidance",
    # 常见行情动词（原先缺失，导致 "Stocks Rise"、"Futures Climb"
    # 这类最普通的标题全被判成中性，程度维度几乎不区分）
    "rise", "climb", "advance", "rebound", "higher", "firmer",
    "hit record", "lead gain", "extend gain",
]
NEG_WORDS = [
    "miss", "plunge", "slump", "fear", "warning", "downgrade",
    "selloff", "sell-off", "tumble", "fall", "drop", "weak", "concern",
    "hawkish", "recession", "layoff", "probe", "lawsuit", "tariff",
    "sanction", "conflict", "sticky inflation", "lose steam",
    "cut guidance", "worse than expected", "slowdown", "default",
    "sink", "slide", "retreat", "dampen", "weigh on", "drag",
    "lower", "pressure", "caution", "uncertainty",
]


def count_stems(text: str, stems: list[str]) -> int:
    """统计命中的**不同**词干数，容忍 s/es/ed/ing 词形变化。"""
    n = 0
    for stem in stems:
        pat = r"\b" + re.escape(stem).replace(r"\ ", r"\s+") + r"(s|es|ed|ing)?\b"
        if re.search(pat, text):
            n += 1
    return n


def news_sentiment(title: str) -> str:
    """
    关键词/短语启发式情绪判定。

    这不是语义分析：它靠词表和少量否定规则，对反讽、条件句、
    多主体句式都会误判。页面上明确标注为"启发式"，
    新闻因子权重也因此只给 20%。
    """
    t = title.lower()
    # 紧缩预期方向优先于词表：这类标题里的 fall/fade 说的是"预期"而非行情
    tv = tightening_expectation_verdict(t)
    if tv:
        return tv
    for pat, verdict in OVERRIDE_RULES:
        if re.search(pat, t):
            return verdict
    pos = count_stems(t, POS_WORDS)
    neg = count_stems(t, NEG_WORDS)
    if pos > neg:
        return "pos"
    if neg > pos:
        return "neg"
    for pat, verdict in FALLBACK_RULES:
        if re.search(pat, t):
            return verdict
    return "neu"


# ---- 单条新闻的两个维度：风险星级 与 利好利空程度 ----------------------
#
# 风险 = 这条消息给市场带来多少下行/不确定性。分三层：
#   系统性 (SYSTEMIC)：波及整个市场的尾部事件
#   政策/宏观 (MACRO)：影响面广但可定价
#   个体 (IDIOSYNCRATIC)：单一公司或板块层面
RISK_SYSTEMIC = [
    "recession", "crisis", "crash", "default", "collapse", "contagion",
    "war", "invasion", "sanction", "shutdown", "downgrade of u.s.",
    "systemic", "bank run", "bankruptcy", "meltdown",
]
RISK_MACRO = [
    "federal reserve", "fed ", "fomc", "rate hike", "rate cut", "hawkish",
    "dovish", "inflation", "cpi", "pce", "tariff", "trade war", "jobs report",
    "unemployment", "yield", "treasury", "debt ceiling", "election",
    "oil price", "geopolitic",
]
RISK_IDIO = [
    "earnings", "guidance", "upgrade", "downgrade", "probe", "lawsuit",
    "layoff", "recall", "ceo", "buyback", "dividend", "merger", "acquisition",
]

# 放大词：出现则程度往两端推
AMPLIFIERS = [
    "surge", "surges", "soar", "soars", "plunge", "plunges", "crash",
    "record", "all-time", "sharply", "steep", "historic", "biggest",
    "worst", "best", "shock", "spike", "tumble", "slump", "intensif",
]


def news_risk_stars(title: str) -> int:
    """
    这条新闻的风险星级 1–5（越多星风险越高）。

    纯关键词分层启发式：系统性词 +3、宏观词 +2、个体词 +1，
    放大词再 +1，最后夹到 1–5。不是语义分析，只是把
    "这条消息波及面有多大" 粗略排序。
    """
    t = title.lower()
    score = 1
    if any(w in t for w in RISK_SYSTEMIC):
        score += 3
    elif any(w in t for w in RISK_MACRO):
        score += 2
    elif any(w in t for w in RISK_IDIO):
        score += 1
    if any(w in t for w in AMPLIFIERS):
        score += 1
    return int(clamp(score, 1, 5))


def news_impact_level(title: str, senti: str) -> int:
    """
    利好利空程度 1–5，与全局 5 级刻度同向:
      1 强利空 · 2 偏利空 · 3 中性 · 4 偏利好 · 5 强利好

    方向取自 news_sentiment()，强度由**不同**词干命中数与放大词决定。
    注意这个维度只衡量消息自身的方向强度，"波及面有多大"由
    news_risk_stars() 单独表达 —— 单只股票的暴涨可以是"强利好"
    但风险星级很低（影响面窄），两个维度不要混。
    """
    t = title.lower()
    hits = count_stems(t, POS_WORDS if senti == "pos" else NEG_WORDS)
    # 门槛设 3 而非 2：两个同向词很常见（"beats and raises guidance"），
    # 不足以称"强"。放大词才是判强的主要依据。
    strong = any(w in t for w in AMPLIFIERS) or hits >= 3
    if senti == "pos":
        return 5 if strong else 4
    if senti == "neg":
        return 1 if strong else 2
    return 3


def news_score(news: list[dict]) -> tuple[float, list[dict]]:
    """
    关键词情绪计分。注意：这是粗糙的启发式，不是真正的语义分析。

    每条新闻附带 senti(方向)、impact(1–5 程度)、risk(1–5 星)。
    因子分按 impact 相对中性档(3)的偏离加权，比只用 ±1 的方向
    更能体现"强利空"与"偏利空"的差别。
    """
    scored = []
    num = den = 0.0
    for n in news:
        s = news_sentiment(n["title"])
        impact = news_impact_level(n["title"], s)
        risk = news_risk_stars(n["title"])
        scored.append({**n, "senti": s, "impact": impact, "risk": risk})
        # impact 3 为中性，每档 ±27.5 → 1档=-55, 5档=+55
        num += (impact - 3) * 27.5
        den += 1
    if den == 0:
        return 0.0, scored
    return round(clamp(num / den, -100, 100), 1), scored


# --------------------------------------------------------------------------
# 风险等级
# --------------------------------------------------------------------------
def event_density(econ: list[dict], universe: list[dict], as_of: str) -> tuple[int, dict]:
    """未来 7 天的事件密集度 0..100：高重要度宏观事件 + 大市值公司财报。"""
    t0 = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp()
    t1 = t0 + 7 * 86400
    high_econ = sum(
        1
        for e in econ
        if e.get("ts") and t0 <= e["ts"] <= t1
        and str(e.get("star")).upper() == "HIGH"
    )
    upcoming_earn = sum(
        1
        for e in universe[:30]
        if e.get("eps_actual") is None
        and e.get("date")
        and as_of <= str(e["date"]) <= _s(_d(as_of) + dt.timedelta(days=7))
    )
    raw = high_econ * 7 + upcoming_earn * 9
    # 用平滑饱和曲线而不是硬截断。硬 clamp 到 100 会让密集期彻底失去
    # 分辨率（实测 8 个高重要事件 + 7 家财报 raw=119，和 raw=300 一样都显示 100，
    # 两者的风险显然不同）。1-exp(-raw/k) 单调递增且永不真正触顶。
    density = 100 * (1 - math.exp(-raw / 70))
    return int(round(density)), {
        "high_econ_7d": high_econ,
        "top30_earnings_7d": upcoming_earn,
        "raw": raw,
    }


def risk_level(vix: float, evt: int) -> tuple[int, float]:
    """风险 1..5：波动率 60% + 事件密集度 40%。"""
    vix_n = clamp((vix - 10) / 35 * 100, 0, 100)
    score = vix_n * 0.6 + evt * 0.4
    lvl = 5 if score >= 78 else 4 if score >= 58 else 3 if score >= 38 else 2 if score >= 20 else 1
    return lvl, round(score, 1)


# --------------------------------------------------------------------------
# 顶部实时行情条
# --------------------------------------------------------------------------
# 注意代码冲突陷阱：Futu 里 US.CL 是高露洁(Colgate)、US.WTI 是 W&T Offshore、
# US.BZ 是 BOSS 直聘 —— 这些"看起来像原油"的代码全是真实股票。
# Futu 没有原油期货/现货，只有 USO(WTI ETF) / BNO(Brent ETF)，
# 它们跟踪期货且有滚动损耗，**不等于油价**，只能当方向代理用。
# 真实 Brent/WTI 报价须外部填入并标注日期。
TICKER_SYMBOLS = [
    ("US.SPY", "SPY", ("S&P 500 ETF", "标普500 ETF")),
    ("US.QQQ", "QQQ", ("Nasdaq 100 ETF", "纳指100 ETF")),
    ("US.USO", "USO", ("WTI Crude ETF", "WTI 原油 ETF")),
    ("US.BNO", "BNO", ("Brent Crude ETF", "布伦特原油 ETF")),
    ("US.TLT", "TLT", ("20Y Treasury ETF", "20年期美债 ETF")),
    ("US.GLD", "GLD", ("Gold ETF", "黄金 ETF")),
]

# 招标标题 -> (显示名, 排序用年数)
AUCTION_TENORS = [
    ("4-Week Bill Auction", ("4W", "4周"), 4 / 52),
    ("8-Week Bill Auction", ("8W", "8周"), 8 / 52),
    ("3-Month Bill Auction", ("3M", "3月"), 0.25),
    ("6-Month Bill Auction", ("6M", "6月"), 0.5),
    ("52-Week Bill Auction", ("1Y", "1年"), 1.0),
    ("2-Year Note Auction", ("2Y", "2年"), 2.0),
    ("3-Year Note Auction", ("3Y", "3年"), 3.0),
    ("5-Year Note Auction", ("5Y", "5年"), 5.0),
    ("7-Year Note Auction", ("7Y", "7年"), 7.0),
    ("10-Year Note Auction", ("10Y", "10年"), 10.0),
    ("20-Year Bond Auction", ("20Y", "20年"), 20.0),
    ("30-Year Bond Auction", ("30Y", "30年"), 30.0),
]


def market_ticker(q, snap: Snapshot) -> list[dict]:
    """SPY/QQQ/原油/美债/黄金 的最新价与涨跌幅。"""
    codes = [c for c, _, _ in TICKER_SYMBOLS]
    res = q.get_market_snapshot(codes)
    if res[0] != RET_OK:
        snap.errors.append(f"ticker snapshot: {res[1]}")
        return []
    by_code = {str(r["code"]): r for _, r in res[1].iterrows()}
    out = []
    for code, label, desc in TICKER_SYMBOLS:
        r = by_code.get(code)
        if r is None:
            continue
        last = num_or_none(r.get("last_price"))
        prev = num_or_none(r.get("prev_close_price"))
        chg = ((last / prev - 1) * 100) if (last and prev) else None
        out.append({
            "code": code, "label": label, "desc": bi(desc[0], desc[1]),
            "last": last, "prev": prev,
            "chg": round(chg, 2) if chg is not None else None,
        })
    return out


def treasury_curve(econ: list[dict], as_of: str) -> list[dict]:
    """
    从经济日历的**国债招标结果**提取真实中标收益率曲线。

    这是唯一能自动取到的美债收益率来源：Futu 没有收益率指数
    （US.TNX / US.TYX / US.IRX 全报 Unknown stock）。
    招标只在特定日期发生，所以每个期限取"最近一次已完成招标"，
    并把招标日一起带出来 —— 不同期限的日期不同，必须显示出来，
    否则会被误读成同一天的曲线快照。
    """
    as_of_ts = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp()
    cutoff = min(dt.datetime.now().timestamp(), as_of_ts + 86400)
    latest: dict[str, dict] = {}
    for ev in econ:
        title = str(ev.get("title") or "").strip()
        ts = ev.get("ts") or 0
        if ts >= cutoff or ev.get("actual") is None:
            continue
        for pat, name, years in AUCTION_TENORS:
            if title != pat:
                continue
            cur = latest.get(name[0])
            if cur is None or ts > cur["ts"]:
                latest[name[0]] = {
                    "tenor": bi(name[0], name[1]), "years": years,
                    "yield": ev["actual"],
                    "ts": ts,
                    "date": dt.datetime.fromtimestamp(ts).strftime("%m-%d"),
                }
    rows = sorted(latest.values(), key=lambda r: r["years"])
    return [{k: v for k, v in r.items() if k != "ts"} for r in rows]
