"""
催化剂识别 —— 两条独立证据链，互为交叉验证。

1) SEC EDGAR 8-K（免费、无需 key、带结构化 item 代码）
   item 代码是"公司自己在法律文件里承认这事重大"的证据，比标题关键词可靠得多。
   acceptanceDateTime 精确到秒，能判断 8-K 是不是今天盘前刚发的。

2) moomoo get_search_news 标题（Benzinga / GlobeNewswire / MT Newswires 等）
   用正负词典打分。负面词典是这里最值钱的部分：小盘股盘前暴涨最常见的
   真实原因是"宣布增发"，那是做空信号。

两条链取加权合成，任一条命中强负面则整体判负 —— 宁可漏做多，不可做反。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests
from futu import RET_OK, NewsSubType

from .data import _Throttle
from .config import (
    CATALYST_THRESHOLDS,
    EDGAR_DIRECTIONAL_ITEMS,
    EDGAR_ITEM_NAMES as ITEM_NAMES,
    EDGAR_ITEM_WEIGHTS,
    EDGAR_LOOKBACK_HOURS,
    EDGAR_MATERIALITY_ITEMS,
    EDGAR_NEGATIVE_OVERRIDE,
    HEADLINE_NEGATIVE,
    HEADLINE_POSITIVE,
    NEWS_LOOKBACK_HOURS,
    NEWS_MAX_PER_TICKER,
    RUNTIME,
)

ET = ZoneInfo("America/New_York")

# get_search_news 限频 10 次/30 秒。超限后接口返回错误而不是空结果，
# 如果不节流又把异常吞掉，表现就是"大部分票都没有新闻" —— 静默降级，
# 而且恰好丢的是抓增发用的方向信号。实测第 10 次调用就开始报错。
_news_throttle = _Throttle(max_calls=9, window=30.0)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

_POS = [(re.compile(p, re.I), w) for p, w in HEADLINE_POSITIVE.items()]
_NEG = [(re.compile(p, re.I), w) for p, w in HEADLINE_NEGATIVE.items()]

# EDGAR 要求 <10 req/s，这里保守到 ~7/s
_edgar_lock = threading.Lock()
_edgar_last = [0.0]


def _edgar_pace() -> None:
    with _edgar_lock:
        gap = time.time() - _edgar_last[0]
        if gap < 0.14:
            time.sleep(0.14 - gap)
        _edgar_last[0] = time.time()


@dataclass
class Catalyst:
    code: str
    materiality: float = 0.0            # 0~1，方向中性：确有重大事件吗
    direction: float = 0.0              # -1~+1，正 = 利多，负 = 利空
    kind: str = ""                      # 人类可读的催化剂类型
    headline: str = ""                  # 最能代表催化剂的那条标题
    edgar_items: list[str] = field(default_factory=list)
    edgar_materiality: float = 0.0
    edgar_direction: float = 0.0
    news_score: float = 0.0
    news_ok: bool = True      # False = 查询失败（多半是限频），不等于"没有新闻"
    evidence: list[str] = field(default_factory=list)

    def label_for(self, side: str = "long") -> str:
        """催化剂档位取决于交易方向：财报暴雷对做多是反向信号，对做空是硬催化。"""
        aligned = self.direction if side == "long" else -self.direction
        th = CATALYST_THRESHOLDS
        if aligned <= th["negative"]:
            return "negative"
        if self.materiality >= th["hard"] or aligned >= th["hard"]:
            return "hard"
        if self.materiality >= th["soft"] or aligned >= th["soft"]:
            return "soft"
        return "none"

    def score_for(self, side: str = "long") -> float:
        """展示用的合成分，符号已对齐交易方向。"""
        aligned = self.direction if side == "long" else -self.direction
        return round(aligned if aligned < 0 else max(self.materiality, aligned), 3)


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------
def ticker_cik_map(max_age_days: int = 7) -> dict[str, int]:
    """ticker -> CIK。SEC 官方映射表，本地缓存 7 天。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "company_tickers.json")
    fresh = (
        os.path.exists(path)
        and (time.time() - os.path.getmtime(path)) < max_age_days * 86400
    )
    if not fresh:
        try:
            r = requests.get(
                TICKERS_URL,
                headers={"User-Agent": RUNTIME["sec_user_agent"]},
                timeout=20,
            )
            r.raise_for_status()
            with open(path, "w") as f:
                f.write(r.text)
        except Exception as exc:
            print(f"  [edgar] 下载 ticker 映射失败 ({exc})，尝试用旧缓存")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}


def combine_items(codes: list[str]) -> tuple[float, float, list[str]]:
    """把一批 8-K item 合成 (重要性, 方向, 主导 item 排序)。

    重要性是方向中性的"确有重大事件"，方向自带符号（负 = 对做多不利）。
    分开的理由见 config 里的注释：item 2.02 不告诉你财报是好是坏。

    另一条关键规则：出现稀释类 item 时，同一份 8-K 里的 1.01 指的就是
    证券购买协议本身，此时不能再把它算成利好的"重大协议"。
    1.01+3.02+3.03+5.03 是定增融资的典型组合（实盘上 HCWC 就踩过）。
    """
    hits = [c for c in codes if c in EDGAR_ITEM_WEIGHTS]
    direction = min([EDGAR_DIRECTIONAL_ITEMS.get(c, 0.0) for c in hits], default=0.0)

    mat_items = {c: w for c, w in EDGAR_MATERIALITY_ITEMS.items() if c in hits}
    if direction <= EDGAR_NEGATIVE_OVERRIDE:
        mat_items.pop("1.01", None)      # 融资协议不算利好事件
    materiality = max(mat_items.values(), default=0.0)

    # 主导 item 排最前：判负时取最负的方向项，否则取最重要的中性项
    key_fn = ((lambda c: EDGAR_ITEM_WEIGHTS.get(c, 0.0)) if direction <= EDGAR_NEGATIVE_OVERRIDE
              else (lambda c: -EDGAR_ITEM_WEIGHTS.get(c, 0.0)))
    return materiality, direction, sorted(set(hits), key=key_fn)


def recent_8k(ticker: str, cik: int, ref: dt.datetime | None = None,
              hours: int = EDGAR_LOOKBACK_HOURS) -> tuple[float, float, list[str], list[str]]:
    """返回 (重要性, 方向, 命中的 item, 证据文本)。

    ref 是"盘前数据所属交易日的开盘时刻"，不是墙上时钟 —— 见 score.enrich 里
    session_date 的推断。用错基准会把真正的催化剂 8-K 整个滤掉。
    """
    _edgar_pace()
    try:
        r = requests.get(
            SUBMISSIONS_URL.format(cik=cik),
            headers={"User-Agent": RUNTIME["sec_user_agent"]},
            timeout=20,
        )
        r.raise_for_status()
        recent = r.json()["filings"]["recent"]
    except Exception:
        return 0.0, 0.0, [], []

    anchor = (ref or dt.datetime.now(ET)).astimezone(dt.timezone.utc)
    cutoff = anchor - dt.timedelta(hours=hours)
    forms = recent.get("form", [])
    items_col = recent.get("items", [""] * len(forms))
    accepted = recent.get("acceptanceDateTime", [])
    dates = recent.get("filingDate", [])

    hit_items, evidence = [], []
    for i, form in enumerate(forms[:60]):
        if not form.startswith("8-K"):
            continue
        ts = None
        if i < len(accepted) and accepted[i]:
            try:
                ts = dt.datetime.fromisoformat(accepted[i].replace("Z", "+00:00"))
            except ValueError:
                ts = None
        if ts is None and i < len(dates):
            try:
                ts = dt.datetime.strptime(dates[i], "%Y-%m-%d").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                continue
        # ts > anchor 的是"数据所属交易日之后"才披露的，算前视偏差，必须排除
        if ts is None or ts < cutoff or ts > anchor:
            continue

        raw_items = (items_col[i] if i < len(items_col) else "") or ""
        hit_items += [c.strip().split(" ")[0] for c in raw_items.split(",") if c.strip()]
        when = ts.astimezone(ET).strftime("%m-%d %H:%M ET")
        evidence.append(f"8-K {raw_items or '(no items)'} @ {when}")

    materiality, direction, ordered = combine_items(hit_items)
    return materiality, direction, ordered, evidence


# ---------------------------------------------------------------------------
# 新闻标题
# ---------------------------------------------------------------------------
def _parse_publish_time(v, ref: dt.datetime) -> dt.datetime | None:
    """moomoo 的 publish_time 有 'M/D' / 'HH:MM' / 完整时间戳几种形态。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=ET)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)          # 8/7
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        year = ref.year
        cand = dt.datetime(year, mo, da, tzinfo=ET)
        if cand - ref > dt.timedelta(days=180):          # 跨年回绕
            cand = cand.replace(year=year - 1)
        return cand
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)            # 06:45 = 今天
    if m:
        return ref.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        )
    return None


def score_headline(title: str) -> tuple[float, str]:
    """单条标题打分，返回 (分数, 命中的类型描述)。负面优先级高于正面。"""
    t = title or ""
    neg_w, neg_kind = 0.0, ""
    for rx, w in _NEG:
        if rx.search(t) and w < neg_w:
            neg_w, neg_kind = w, rx.pattern
    pos_w, pos_kind = 0.0, ""
    for rx, w in _POS:
        if rx.search(t) and w > pos_w:
            pos_w, pos_kind = w, rx.pattern

    if neg_w <= -0.7:                    # 增发/退市/破产级别，直接判负
        return neg_w, _kind_name(neg_kind, negative=True)
    if pos_w > 0 and abs(neg_w) < pos_w:
        return pos_w + neg_w * 0.5, _kind_name(pos_kind)
    if neg_w < 0:
        return neg_w, _kind_name(neg_kind, negative=True)
    return 0.0, ""


_KIND_LABELS = [
    (r"acquir|merger|buyout|takeover|tender", "并购"),
    (r"fda|approval|clearance|breakthrough|orphan|priority review", "FDA/审批"),
    (r"topline|top-line|endpoint|phase 3|trial|study|vaccine|gene therapy", "临床数据"),
    (r"guidance|outlook|forecast", "上调指引"),
    (r"beats|tops|surpass", "财报超预期"),
    (r"earnings|results|revenue", "财报"),
    (r"record", "创纪录业绩"),
    (r"contract", "获得订单"),
    (r"partnership|partners|collaboration|joint venture", "战略合作"),
    (r"repurchase|buyback", "回购"),
    (r"upgrade|price target raised|initiated", "评级上调"),
    (r"offering|placement|at-the-market|atm|pricing of", "增发稀释"),
    (r"warrant|dilut|shelf", "稀释"),
    (r"reverse", "反向拆股"),
    (r"going concern|chapter 11|bankrupt|delisting|deficiency|compliance", "生存风险"),
    (r"restat|accounting", "财务重述"),
    (r"short seller|fraud", "做空报告"),
    (r"downgrade|target \(cut\|lowered\)|cuts", "评级下调"),
    (r"no .*news|limited fresh news|unclear catalyst", "无实质消息"),
]


def _kind_name(pattern: str, negative: bool = False) -> str:
    for rx, label in _KIND_LABELS:
        if re.search(rx, pattern, re.I):
            return label
    return "利空" if negative else "利好"


# 只剥离明确的法律后缀。不要碰 Systems / Genomics / Therapeutics 这类词 ——
# 它们是名字辨识度的一部分，剥掉会让关键词变得歧义。
_LEGAL_SUFFIX = re.compile(
    r"[\s,]*\b(&\s*Co|Inc|Corp(oration)?|Co|Company|Holdings?|Group|Ltd|Limited"
    r"|LLC|L\.?P\.?|plc|S\.?A\.?|N\.?V\.?|AG|Class\s+[A-C])\b\.?", re.I)


def search_keyword(name: str, code: str) -> str:
    """把快照里的正式全名压成适合搜索的短名。

    实测（2026-08-19）：搜 "Merck & Co" 只返回券商评级样板文，
    搜 "Merck" 才返回当天真正的催化剂
    ("Moderna & Merck Stocks Skyrocket on Their Cancer Vaccine Trial's Success")。
    按 ticker 搜同样无效。所以关键词必须去掉法律后缀。
    """
    n = _LEGAL_SUFFIX.sub("", name or "")
    n = re.sub(r"[&,.\s]+$", "", n).strip()
    return n or code.split(".")[-1]


def news_catalyst(q, code: str, name: str, ref: dt.datetime) -> tuple[float, str, str, list[str], bool]:
    """返回 (分数, 类型, 代表标题, 证据列表, 查询是否成功)。

    最后一位必须区分"查过了没有新闻"和"根本没查成"，否则限频失败会被
    当成"无催化剂"，而"无催化剂"在策略里是有含义的档位。
    """
    keyword = search_keyword(name, code)
    _news_throttle.wait()
    try:
        ret, df = q.get_search_news(keyword, max_count=NEWS_MAX_PER_TICKER,
                                    news_sub_type=NewsSubType.ALL)
    except Exception:
        return 0.0, "", "", [], False
    if ret != RET_OK:
        return 0.0, "", "", [], False
    if df is None or len(df) == 0:
        return 0.0, "", "", [], True

    cutoff = ref - dt.timedelta(hours=NEWS_LOOKBACK_HOURS)
    ticker = code.split(".")[-1].upper()
    best_score, best_kind, best_title = 0.0, "", ""
    worst_score, worst_kind, worst_title = 0.0, "", ""
    evidence: list[str] = []

    for _, row in df.iterrows():
        when = _parse_publish_time(row.get("publish_time"), ref)
        # 注意：moomoo 对隔日新闻只给到"月/日"，同一天内的先后无法区分。
        # 实盘在 08:00 ET 跑时这不是问题（当天的新闻本来就是盘前新闻）；
        # 用历史数据演练时，同日新闻可能存在轻微前视。
        if when is not None and (when < cutoff or when.date() > ref.date()):
            continue
        # 有 related_securities 时用它确认新闻确实关于这只票，避免同名污染
        rel = row.get("related_securities")
        if isinstance(rel, (list, tuple)) and rel:
            joined = " ".join(str(x) for x in rel).upper()
            if ticker not in joined and code.upper() not in joined:
                continue
        title = str(row.get("title", ""))
        s, kind = score_headline(title)
        if s == 0:
            continue
        stamp = when.strftime("%m-%d %H:%M") if when else str(row.get("publish_time", ""))
        evidence.append(f"[{row.get('source', '?')} {stamp}] {title[:110]}")
        if s > best_score:
            best_score, best_kind, best_title = s, kind, title
        if s < worst_score:
            worst_score, worst_kind, worst_title = s, kind, title

    if worst_score <= -0.7:              # 强利空一票否决
        return worst_score, worst_kind, worst_title, evidence, True
    if best_score > 0:
        return best_score + worst_score * 0.5, best_kind, best_title, evidence, True
    if worst_score < 0:
        return worst_score, worst_kind, worst_title, evidence, True
    return 0.0, "", "", evidence, True


def news_materiality(score: float, kind: str) -> float:
    """标题能提供多少"确有重大事件"的证据。

    "无实质消息"这类标题分数是负的，但它恰恰证明没有事件发生 ——
    直接取绝对值会把纯逼空票误判成有催化剂。
    """
    if kind == "无实质消息" or not kind:
        return 0.0
    return min(1.0, abs(score) * 0.75)   # 标题比 8-K 弱，打七五折


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------
def build_catalysts(q, rows: list[dict], ref: dt.datetime | None = None) -> dict[str, Catalyst]:
    """rows 需含 code 和 name。EDGAR 并发拉取，moomoo 新闻串行（共用一条连接）。"""
    ref = ref or dt.datetime.now(ET)
    cikmap = ticker_cik_map()

    def _edgar(row):
        code = row["code"]
        ticker = code.split(".")[-1].upper()
        cik = cikmap.get(ticker)
        if cik is None:
            return code, (0.0, 0.0, [], [])
        return code, recent_8k(ticker, cik, ref=ref)

    edgar_res: dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for code, res in pool.map(_edgar, rows):
            edgar_res[code] = res

    out: dict[str, Catalyst] = {}
    news_failed = 0
    for row in rows:
        code, name = row["code"], row.get("name", "")
        e_mat, e_dir, e_items, e_evi = edgar_res.get(code, (0.0, 0.0, [], []))
        n_score, n_kind, n_title, n_evi, n_ok = news_catalyst(q, code, name, ref)
        news_failed += 0 if n_ok else 1
        n_mat = news_materiality(n_score, n_kind)

        # 重要性取两条证据链的较高者，双链齐备时略加成（8-K 是法律证据，权重更高）
        materiality = max(e_mat, n_mat)
        if e_mat > 0 and n_mat > 0:
            materiality = min(1.0, e_mat + 0.25 * n_mat)

        # 方向：任一条明确判负就判负，否则取新闻情绪（8-K 中性项不带方向）
        direction = min(e_dir, n_score) if min(e_dir, n_score) < 0 else max(e_dir, n_score)

        # 类型标签取主导证据。判负时优先显示利空来源，否则 8-K 优先于标题。
        edgar_kind = ""
        if e_items:
            edgar_kind = f"{ITEM_NAMES.get(e_items[0], e_items[0])}(8-K {e_items[0]})"
        if direction <= CATALYST_THRESHOLDS["negative"]:
            kind = (n_kind if n_score <= e_dir else edgar_kind) or edgar_kind or n_kind
        else:
            kind = edgar_kind if e_mat >= n_mat and edgar_kind else (n_kind or edgar_kind)

        out[code] = Catalyst(
            code=code,
            materiality=round(materiality, 3),
            direction=round(direction, 3),
            kind=kind or "无催化剂",
            headline=n_title,
            edgar_items=e_items,
            edgar_materiality=round(e_mat, 3),
            edgar_direction=round(e_dir, 3),
            news_score=round(n_score, 3),
            news_ok=n_ok,
            evidence=(e_evi + n_evi)[:6],
        )
    if news_failed:
        print(f"  [news] {news_failed}/{len(rows)} 只查询失败，这些票的新闻方向信号缺失")
    return out
