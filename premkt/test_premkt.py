"""
单元测试。跑法：

    ./.venv/bin/python -m pytest premkt/test_premkt.py -q

不依赖 OpenD —— 全部是纯函数测试。涉及网络的部分在 selftest.py 里。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from .catalyst import (
    Catalyst,
    _parse_publish_time,
    combine_items,
    news_materiality,
    score_headline,
    search_keyword,
)
from .config import CATALYST_THRESHOLDS
from .data import premarket_data_is_current, session_label
from .score import (
    apply_gates,
    float_squeeze_score,
    gap_quality,
    liquidity_score,
    rvol_score,
    technical_score,
    trade_plan,
)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 标题分类 —— 策略最核心的判别器
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title,expect_sign", [
    ("Acme Corp Announces Pricing of $50 Million Public Offering", -1),
    ("XYZ Announces Underwritten Offering of Common Stock", -1),
    ("Biotech Announces 1-for-20 Reverse Stock Split", -1),
    ("Company Receives Nasdaq Deficiency Letter", -1),
    ("Auditor Raises Going Concern Doubt", -1),
    ("FDA Approves Acme's Lead Candidate", 1),
    ("Acme to be acquired by MegaCorp for $40/share", 1),
    ("FIGS, Inc. Lifts Outlook Amid Robust Growth", 1),
    ("Acme Beats Estimates, Raises FY27 Guidance", 1),
    ("Defense firm wins $200M Army contract", 1),
    ("MasterBeef Jumps 232% as Low-float Momentum Appears to Overpower Limited Fresh News", -1),
    # 2026-08-19 实盘漏判：MRK 单日 +11% 靠癌症疫苗试验成功，旧词典判成"无催化剂"
    ("Moderna & Merck Stocks Skyrocket on Their Cancer Vaccine Trial's Success", 1),
    ("Biotech announces successful Phase 3 trial in NSCLC", 1),
    ("XYZ hits primary endpoint in pivotal study", 1),
    # 2026-08-25 实盘漏判：RZLV +21% 靠拿下 Google，旧词典只认 "partnership"
    ("Google Selects Rezolve AI's Proprietary Distributed Database Platform", 1),
    ("Acme's platform selected by a major cloud provider", 1),
    ("Acme Announces Quarterly Cash Dividend", 0),
])
def test_headline_direction(title, expect_sign):
    s, _ = score_headline(title)
    assert (s > 0) - (s < 0) == expect_sign, f"{title!r} -> {s}"


def test_dilution_overrides_positive():
    """同一条标题里既有利好又有增发时，必须判负 —— 宁可漏做多，不可做反。"""
    s, kind = score_headline(
        "Acme Raises Guidance and Announces Pricing of $75M Public Offering")
    assert s <= -0.7
    assert kind in ("增发稀释", "稀释")


def test_no_news_headline_is_negative():
    s, kind = score_headline("Why Is Penny Stock XYZ Up Today? No clear catalyst")
    assert s < 0 and kind == "无实质消息"


# ---------------------------------------------------------------------------
# 8-K item 组合 —— 实盘上抓到的 HCWC 假阳性回归测试
# ---------------------------------------------------------------------------
def test_plain_earnings_8k_is_material_but_directionless():
    """item 2.02 只说明"披露了业绩"，不说明业绩好坏 —— 方向必须是 0。"""
    mat, direction, ordered = combine_items(["2.02", "9.01"])
    assert mat == pytest.approx(1.0)
    assert direction == 0.0
    assert ordered[0] == "2.02"


def test_financing_combo_overrides_material_agreement():
    """HCWC 2026-08-06 的真实 8-K：1.01+3.02+3.03+5.03 是定增，不是利好。

    修复前因为 |1.01 的 +0.90| > |3.02 的 -0.70| 被判成 hard，方向做反。
    """
    mat, direction, ordered = combine_items(["1.01", "3.02", "3.03", "5.03", "9.01"])
    assert direction <= -0.60, "含未注册发行的 8-K 方向必须判负"
    assert mat == 0.0, "同一份 8-K 里的 1.01 是证券购买协议，不能算利好事件"
    assert ordered[0] == "3.02", "主导 item 应当是稀释项"


def test_mild_negative_does_not_erase_materiality():
    """5.03(修改章程) 单独出现时不该把财报 8-K 掀翻。"""
    mat, direction, _ = combine_items(["2.02", "5.03"])
    assert mat == pytest.approx(1.0)
    assert direction > CATALYST_THRESHOLDS["negative"]


def test_unknown_items_ignored():
    assert combine_items(["9.01", "99.9"]) == (0.0, 0.0, [])


# ---------------------------------------------------------------------------
# 催化剂档位依赖交易方向 —— SEZL 做空侧误判的回归测试
# ---------------------------------------------------------------------------
def test_earnings_is_hard_catalyst_for_both_directions():
    """财报既能推涨也能推跌，两个方向都该算硬催化。gap 的符号才决定方向。"""
    c = Catalyst(code="US.X", materiality=1.0, direction=0.0)
    assert c.label_for("long") == "hard"
    assert c.label_for("short") == "hard"


def test_dilution_flips_with_direction():
    """增发对做多是反向信号，对做空恰恰是最好的催化剂。"""
    c = Catalyst(code="US.X", materiality=0.0, direction=-0.70)
    assert c.label_for("long") == "negative"
    assert c.label_for("short") == "hard"
    assert c.score_for("long") < 0 < c.score_for("short")


def test_buyout_flips_the_other_way():
    """并购要约对做空是灾难 —— 必须判成反向。"""
    c = Catalyst(code="US.X", materiality=0.9, direction=1.0)
    assert c.label_for("long") == "hard"
    assert c.label_for("short") == "negative"


def test_no_catalyst_stays_none_both_ways():
    c = Catalyst(code="US.X", materiality=0.0, direction=0.0)
    assert c.label_for("long") == c.label_for("short") == "none"


def test_no_news_headline_contributes_no_materiality():
    """"无实质消息"分数是负的，取绝对值会把纯逼空票误判成有催化剂。"""
    assert news_materiality(-0.60, "无实质消息") == 0.0
    assert news_materiality(0.90, "FDA/审批") > 0.5


# ---------------------------------------------------------------------------
# 打分曲线
# ---------------------------------------------------------------------------
def test_gap_quality_peaks_at_configured_point():
    assert gap_quality(12.0) == pytest.approx(1.0, abs=1e-6)
    # 极端 gap 必须被压到很低 —— 这是 MB(+278%) 那类票的处理方式
    assert gap_quality(278.0) < 0.05
    # 太小的 gap 也不是动量
    assert gap_quality(1.0) < 0.10
    # 单调性：从峰值往两边递减
    assert gap_quality(8) < gap_quality(12) > gap_quality(20)


def test_rvol_score_bounds():
    assert rvol_score(0.01) == 0.0          # floor 以下
    assert rvol_score(1.0) == pytest.approx(1.0)
    assert rvol_score(5.0) == 1.0           # 饱和后封顶
    assert 0 < rvol_score(0.2) < 1


def test_technical_prefers_breakout():
    new_high = technical_score(pre_price=100, high52=99, low52=40, ma20=85)
    deep_hole = technical_score(pre_price=100, high52=400, low52=40, ma20=85)
    assert new_high > deep_hole
    # 严重超买（远离 MA20）要被扣分
    assert technical_score(100, 99, 40, 40) < technical_score(100, 99, 40, 85)


def test_technical_is_mirrored_for_shorts():
    """SERV 只有 52 周高的 26%：做多是套牢区，做空是无支撑的下降趋势。"""
    breaking_down = dict(pre_price=40, high52=160, low52=41, ma20=55)
    assert technical_score(**breaking_down, side="short") > 0.8
    assert technical_score(**breaking_down, side="long") < 0.4
    # 反过来，创新高的票不该是好的做空标的
    at_highs = dict(pre_price=100, high52=99, low52=40, ma20=85)
    assert technical_score(**at_highs, side="short") < technical_score(**at_highs, side="long")


def test_float_squeeze_buckets_and_htb():
    assert float_squeeze_score(5e6, 1e6, True) > float_squeeze_score(5e8, 1e8, True)
    # 不可融券 -> 做多额外加分（空头无法压制）
    assert float_squeeze_score(2e7, 0, False) > float_squeeze_score(2e7, 1e7, True)


def test_float_score_inverts_for_shorts():
    """小流通盘对做多是燃料，对做空是轧空风险 —— 必须反号。"""
    tiny, huge = 5e6, 5e8
    assert float_squeeze_score(tiny, 1e6, True, "short") < float_squeeze_score(huge, 1e8, True, "short")
    assert float_squeeze_score(tiny, 1e6, True, "long") > float_squeeze_score(huge, 1e8, True, "long")
    # 借不到券就根本做不了空
    assert float_squeeze_score(1e8, 0, False, "short") == 0.0
    # 券源紧张要打折
    assert (float_squeeze_score(1e8, 1e5, True, "short")
            < float_squeeze_score(1e8, 1e7, True, "short"))


def test_liquidity_penalises_wide_spread():
    assert liquidity_score(0.1, 6e7) > liquidity_score(1.5, 6e7)
    assert liquidity_score(0.1, 6e7) > liquidity_score(0.1, 1.2e6)


# ---------------------------------------------------------------------------
# 交易计划
# ---------------------------------------------------------------------------
def _row(**kw):
    base = dict(pre_high=11.0, pre_low=9.5, pre_price=10.5, atr14=0.8)
    base.update(kw)
    return pd.Series(base)


def test_trade_plan_long_math():
    p = trade_plan(_row(), side="long")
    assert p["entry"] == 11.0                       # 破盘前高
    assert p["stop"] == pytest.approx(10.2, abs=1e-6)   # ATR 止损比盘前低点更紧
    risk = p["entry"] - p["stop"]
    assert p["targets"][0] == pytest.approx(round(11.0 + risk, 2))
    assert p["targets"][2] == pytest.approx(round(11.0 + 3 * risk, 2))
    # 仓位应当让风险贴近预算（默认 $100k × 0.5% = $500）
    assert 480 <= p["risk_usd"] <= 500


def test_trade_plan_short_is_mirrored():
    p = trade_plan(_row(), side="short")
    assert p["entry"] == 9.5                        # 破盘前低
    assert p["stop"] > p["entry"]
    assert p["targets"][0] < p["entry"]


def test_trade_plan_flags_wide_stop():
    p = trade_plan(_row(pre_high=11.0, pre_low=8.0, atr14=5.0), side="long")
    assert p["too_wide"] is True


# ---------------------------------------------------------------------------
# 硬门槛
# ---------------------------------------------------------------------------
def _cand(**kw):
    base = dict(
        code="US.TEST", name="Test", side="long", pre_price=10.0, pre_high=11.0,
        pre_low=9.0, gap_pct=15.0, pre_turnover=5e6, pre_volume=5e5,
        market_cap=5e8, spread_pct=0.2, exchange_type="US_NASDAQ",
        listing_date="2020-01-01",
    )
    base.update(kw)
    return base


def test_gates_reject_the_untradeable():
    today = dt.date(2026, 8, 10)
    df = pd.DataFrame([
        _cand(code="US.GOOD"),
        _cand(code="US.PENNY", pre_price=0.8),
        _cand(code="US.THIN", pre_turnover=2e5),
        _cand(code="US.OTC", exchange_type="US_PINK"),
        _cand(code="US.WIDE", spread_pct=5.0),
        _cand(code="US.FLAT", gap_pct=0.5),
        _cand(code="US.IPO", listing_date=str(today - dt.timedelta(days=3))),
        _cand(code="US.TINY", market_cap=1e7),
    ])
    passed, rejected = apply_gates(df, today, side="long")
    assert set(passed["code"]) == {"US.GOOD"}
    assert len(rejected) == 7
    assert all(r for r in rejected["reject_reason"])


def test_gates_short_side_wants_gap_down():
    today = dt.date(2026, 8, 10)
    df = pd.DataFrame([_cand(code="US.UP", gap_pct=15.0),
                       _cand(code="US.DOWN", gap_pct=-15.0)])
    passed, _ = apply_gates(df, today, side="short")
    assert set(passed["code"]) == {"US.DOWN"}


# ---------------------------------------------------------------------------
# 时间处理 —— 之前踩过的坑：04:00 ET 前 pre_* 是上一个交易日的
# ---------------------------------------------------------------------------
def test_session_labels():
    assert session_label(dt.datetime(2026, 8, 10, 7, 0, tzinfo=ET)) == "premarket"
    assert session_label(dt.datetime(2026, 8, 10, 11, 0, tzinfo=ET)) == "regular"
    assert session_label(dt.datetime(2026, 8, 10, 17, 0, tzinfo=ET)) == "afterhours"
    # 周一凌晨 2 点：盘前字段还是上周五的
    assert session_label(dt.datetime(2026, 8, 10, 2, 0, tzinfo=ET)) == "overnight"
    assert session_label(dt.datetime(2026, 8, 8, 12, 0, tzinfo=ET)) == "closed"  # 周六


def test_premarket_currency_guard():
    assert premarket_data_is_current(dt.datetime(2026, 8, 10, 8, 0, tzinfo=ET))
    assert not premarket_data_is_current(dt.datetime(2026, 8, 10, 2, 0, tzinfo=ET))
    assert not premarket_data_is_current(dt.datetime(2026, 8, 8, 12, 0, tzinfo=ET))


def test_publish_time_parsing():
    ref = dt.datetime(2026, 8, 10, 8, 0, tzinfo=ET)
    assert _parse_publish_time("8/7", ref).date() == dt.date(2026, 8, 7)
    assert _parse_publish_time("06:45", ref).hour == 6
    assert _parse_publish_time("2026-08-07 13:45:00", ref).date() == dt.date(2026, 8, 7)
    # 跨年回绕：1 月初看到 12/28 应该是去年的
    ref_jan = dt.datetime(2026, 1, 3, 8, 0, tzinfo=ET)
    assert _parse_publish_time("12/28", ref_jan).year == 2025
    assert _parse_publish_time("", ref) is None


# ---------------------------------------------------------------------------
# 新闻搜索关键词 —— MRK 漏判的根因回归测试
# ---------------------------------------------------------------------------
def test_search_keyword_strips_legal_suffix():
    """搜 "Merck & Co" 只出券商评级样板文，搜 "Merck" 才出真新闻。"""
    assert search_keyword("Merck & Co", "US.MRK") == "Merck"
    assert search_keyword("Cisco Systems Inc", "US.CSCO") == "Cisco Systems"
    assert search_keyword("Hims & Hers Health", "US.HIMS") == "Hims & Hers Health"
    assert search_keyword("Sea Limited", "US.SE") == "Sea"


def test_search_keyword_keeps_distinctive_words():
    """Systems / Genomics 这类词是辨识度的一部分，剥掉会变歧义。"""
    assert search_keyword("EPAM Systems", "US.EPAM") == "EPAM Systems"
    assert search_keyword("10x Genomics", "US.TXG") == "10x Genomics"


def test_search_keyword_falls_back_to_ticker():
    assert search_keyword("", "US.ABCD") == "ABCD"
    assert search_keyword("Inc", "US.ABCD") == "ABCD"
