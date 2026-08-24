# optbt — 期权策略回测（基于你自己的 moomoo 数据）

用你本地 moomoo OpenD 的美股历史数据 + 校准过的 Black-Scholes 代理定价，回测
「系统性买入并滚动期权」这类策略。为你实际在做的事（长期虚值 call）而写。

## 环境

所有依赖装在项目自带的 venv 里，**不碰 anaconda base**：

```bash
./.venv/bin/python run_optbt.py NBIS --mode calib
```

已装：`numpy 1.26.4 / pandas 2.2.3 / scipy 1.13.1 / vectorbt 1.0.0 / backtesting 0.6.6 / yfinance / futu-api`。

## 快速上手

```bash
# 1. 看波动率曲面校准结果（用今天的真实期权链拟合）
./.venv/bin/python run_optbt.py NBIS --mode calib

# 2. 对比几种典型的买 call 策略（含一条按你当前持仓形状构造的）
./.venv/bin/python run_optbt.py ASX --start 2018-01-01 --mode compare

# 3. 扫参数：DTE × delta × 提前平仓时点
./.venv/bin/python run_optbt.py BE --start 2019-01-01 --mode grid \
    --fixed-cash 1000 --max-contracts 20

# 4. 单条规则，全参数指定
./.venv/bin/python run_optbt.py CRDO --mode single --dte 120 --delta 0.30 \
    --exit-dte 30 --take-profit 1.0 --stop-loss -0.5 --signal dip

# 5. 纯股票信号回测（backtesting.py 或 vectorbt）
./.venv/bin/python run_optbt.py ASX --mode equity --engine vectorbt
```

## 架构

```
optbt/data.py      moomoo OpenD 分页取数 → parquet 增量缓存；停牌/换公司检测
optbt/vol.py       Yang-Zhang 已实现波动率 + 用真实期权链校准 vrp 和 skew
optbt/pricing.py   BS 定价、希腊字母、隐含波动率反解、按 delta 反求行权价
optbt/engine.py    事件驱动回测循环（开仓/盯市/平仓/到期结算）+ 绩效指标
optbt/signals.py   入场信号：always / dip / breakout / trend / rsi / cheapvol …
run_optbt.py       命令行入口
```

数据层刻意做成**源无关**：以后接 Polygon 或 Alpha Vantage Premium 的真实历史期权链，
只需要新写一个 `iv()` provider，引擎一行都不用改。

## 三个真实的坑（已内置防护）

**1. 波动率不能直接用已实现波动率。** 隐含波动率长期高于已实现（方差风险溢价），
而且虚值 call 的 IV 和平值差很远。直接用 RV 定价会让你「买得太便宜、卖得太贵」，
凭空造出根本不存在的收益。`vol.py` 的做法是从**今天的真实期权链**反推
`vrp = ATM_IV / RV` 和 skew 形状，历史 IV = `RV(t) × vrp × smile(moneyness)`。
仍然是代理，但误差方向是已知的。用 `--vrp` 可以做压力测试。

**2. 停牌 / 换公司。** NBIS 带着 Yandex 的历史，中间有 397 天和 572 天两段空档，
2024-10-21 之前根本是另一家公司；空档本身还会伪造出一根巨大的波动率尖峰。
`clean_bars()` 默认截断到最后一段空档之后并明确告知，`--allow-gaps` 可关闭。

**3. 复利幻觉 + 无限流动性。** 按权益百分比下注 + 不限手数，回测会把几次尾部大赢
复利成 `$10k → $7.5M` 这种没人能成交的数字。用 `--fixed-cash 1000
--max-contracts 20` 看真实的单笔边际；超过 20 倍收益时程序会主动警告。

## 摩擦成本设定（刻意偏悲观）

- 滑点：权利金的 **3%**，开仓平仓**各收一次**（深虚 call 的买卖价差很宽）
- 佣金：每张 **$0.65**，每边
- 到期结算不收滑点；归零到期不收佣金（本来就没有成交）

## 已知局限

- BS 代理定价，不是真实成交价。绝对收益别当真，**只看不同参数之间的相对形状**。
- 波动率曲面用**今天**的形状套整段历史，没有历史 skew 的时间变化。
- 没有股息、没有提前行权（美式期权按欧式处理）、没有保证金约束。
- 样本量小：长期期权策略每年只有几笔交易，7 年也就几十笔，绝大部分收益来自
  两三笔尾部。程序在交易数 < 20 或历史 < 3 年时会警告。
