---
name: moomoo-derivatives-anomaly
description: >-
  Detects derivatives anomaly signals for a user-mentioned stock with
  `get_derivative_unusual`, including 牛熊证街货比例、异动、anomaly, 牛熊证街货价格区间、期权大单、
  隐含波动率、期权量价、期权情绪、期权综合信号 and their unusual changes. Use
  when the user asks about 期权、衍生品、牛熊证、IV、隐含波动率、PCR、期权大单、
  期权异常成交、聪明钱、smart money、unusual options activity、做多做空情绪、
  波动率溢价、期权市场怎么看、有没有大单押注、大资金押注、适不适合卖期权，or gives a
  broad stock-anomaly request such as “英伟达异动”、“NVDA 异动”、“腾讯有没有异动/异常”
  without narrowing the dimension. For broad anomaly requests, this skill should
  be used together with `technical-anomaly` and `capital-anomaly`. Before
  calling the script, you must first normalize the user-mentioned stock name,
  Chinese company name, English company name, or ticker into a standard symbol
  such as `US.NVDA` or `HK.00700`.
metadata:
  version: 0.0.1
  author: Futu
license: MIT
---

# Derivatives Anomaly Skill

Detects derivatives anomalies for a specific stock and formats the result as a structured derivatives anomaly summary.

This skill is for **异动检测** rather than a regular derivatives overview. If the data has no qualifying anomaly, return `无异常` or `无异常（简要原因）`, and do not add extra market commentary.

If the user only says a broad request such as `英伟达异动`、`NVDA 异动`、`腾讯有没有异常` and does not specify a dimension, treat it as a bundled anomaly request. In that case, this skill should be used as one of the three default anomaly skills together with `technical-anomaly` and `capital-anomaly`.

---

## Workflow

### 1. Parse User Input

Extract the following from the user's request:

- `stock_target`: stock code, Chinese stock name, English company name, or ticker explicitly mentioned by the user
- `time_range`: default `7`; if the user says "最近 3 天" / "过去两周" / "last 5 days", convert it to a natural-day integer
- `analysis_dimensions`: optional; only extract when the user clearly asks about one or more specific derivatives dimensions
- `language_id`: infer from the user's language

If the target stock is missing, ask a follow-up question instead of guessing.

### 2. Normalize the Stock Target into a Standard Symbol

Before calling the script, convert the user-mentioned stock target into a standard symbol such as `US.NVDA`, `HK.00700`, `SH.600519`, or `SZ.000001`.

Normalization rules:

- If the user already gives a fully qualified symbol like `US.NVDA` or `HK.00700`, use it directly.
- If the user gives a Chinese company name, English company name, or common ticker, map it to the matching market-prefixed symbol.
- If the symbol is ambiguous, ask a follow-up question instead of guessing.

Common mappings:

| User mention | Standard symbol |
|--------------|-----------------|
| 腾讯 | `HK.00700` |
| 阿里巴巴、阿里 | `HK.09988` |
| 苹果、Apple | `US.AAPL` |
| 特斯拉、Tesla | `US.TSLA` |
| 英伟达、NVIDIA | `US.NVDA` |
| 微软、Microsoft | `US.MSFT` |
| 谷歌、Google、Alphabet | `US.GOOG` |
| 亚马逊、Amazon | `US.AMZN` |
| Meta、脸书、Facebook | `US.META` |
| 台积电、TSM | `US.TSM` |
| 贵州茅台、茅台 | `SH.600519` |
| 宁德时代 | `SZ.300750` |

Ticker inference guidance:

- `NVDA`, `AAPL`, `TSLA`, `MSFT`, `GOOG`, `META` usually mean US stocks, so normalize to `US.NVDA`, `US.AAPL`, `US.TSLA`, `US.MSFT`, `US.GOOG`, `US.META`.
- `00700` by itself is ambiguous in plain text; when the context clearly refers to Tencent, normalize to `HK.00700`.
- If the same company has both HK and US listings and the user does not specify the market, ask a clarifying question.

### 3. Map Derivatives Intent to `analysis_dimensions`

Prefer these canonical `analysis_dimensions` values:

- `warrant_ratio`: 牛熊证街货比例异动，仅港股
- `warrant_price_distribution`: 牛熊证街货价格区间异动，仅港股
- `option_unusual`: 期权大单异动
- `option_volatility`: 期权波动率异动
- `option_volume_price`: 期权量价异动
- `option_sentiment`: 期权情绪异动
- `option_comprehensive`: 期权综合信号异动

Selection guidance:

- If the user asks about `牛熊证街货比例/牛熊街货比例`, include `warrant_ratio`.
- If the user asks about `重货区/街货价格区间/支撑压力`, include `warrant_price_distribution`.
- If the user asks about `期权大单/大额成交/V/OI/聪明钱押注`, include `option_unusual`.
- If the user asks about `IV/IV percentile/IV rank/HV/波动率溢价`, include `option_volatility`.
- If the user asks about `成交量/持仓量/OI/正股联动`, include `option_volume_price`.
- If the user asks about `PCR/Put Call Ratio/做多做空情绪`, include `option_sentiment`.
- If the user asks about `综合信号/多维背离`, include `option_comprehensive`.
- If the user asks a broad 衍生品问题 and does not narrow scope, omit `analysis_dimensions` and use the full interface.

### 4. Infer `language_id`

Use the following language mapping:

- `0`: 简中
- `1`: 繁中
- `2`: 英文
- `4`: 泰语
- `5`: 日语

Default strategy:

- Chinese user -> `0`
- Traditional Chinese request -> `1`
- English user -> `2`
- If unclear, default to `0`

### 5. Call the Script

After extracting and normalizing the parameters, call the script with a Python 3 interpreter. Prefer `python3`; only fall back to `python` when `python3` is unavailable. This avoids the common macOS case where `python` is not installed or not on `PATH`.

Script entry:

```bash
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_derivatives_anomaly.py <STANDARD_SYMBOL> --time-range <DAYS> [--analysis-dimensions ...] [--language-id <ID>] --json
```

Examples:

```bash
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_derivatives_anomaly.py HK.00700 --time-range 7 --json
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_derivatives_anomaly.py US.NVDA --time-range 7 --analysis-dimensions option_unusual option_volatility --json
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_derivatives_anomaly.py US.AAPL --time-range 7 --analysis-dimensions option_sentiment option_comprehensive --json
```

### 6. Check the Result

- If the script exits successfully, use the returned data to build a structured derivatives anomaly summary.
- If the script returns an error, surface the error message and do not fabricate results.
- If a requested class has no anomaly in the window, explicitly say `无异常` or `无异常（简要原因）`.
- If the downstream service reports a permission problem or no accessible data, clearly state that the stock or account lacks the required permission.
- If the stock is not Hong Kong listed, warrant-related classes should be marked as `不适用` or `无异常`.

---

## Parameters

### Required

- `stock_symbol`: standard market-prefixed symbol, such as `US.NVDA`, `HK.00700`, `SH.600519`, `SZ.000001`

### Optional

- `time_range`: natural day window, default `7`
- `analysis_dimensions`: list of specific derivatives anomaly dimensions to inspect; omit for full scan
- `language_id`: output language, default `0`

---

## Output Rules

Present the output by anomaly class. The seven classes are:

- `牛熊证街货比例异动（港股）`
- `牛熊证街货价格区间异动（港股）`
- `期权大单异动`
- `期权波动率异动`
- `期权量价异动`
- `期权情绪异动`
- `期权综合信号异动`

Formatting rules:

- Always display `时间范围` as an absolute date range in the format `YYYY.M.D - YYYY.M.D`. Calculate the start date from the current date minus `time_range` days (e.g., if today is 2026.4.24 and time_range is 7, write `时间范围：2026.4.18 - 2026.4.24`).
- Always preserve the class order above.
- **Full scan** (no `analysis_dimensions` specified): always show all 7 class names. Write `无异常` for classes with no anomaly. Never omit a class name.
- **Subset request** (user specifies one or more dimensions): show only the classes that correspond to the requested dimensions. Write `无异常` for requested classes that have no anomaly. Do not show unrelated classes.
- If multiple abnormal dates or timestamps appear within one class, list them all.
- For `期权大单异动`, when multiple unusual option trades exist in the window, show all of them. Do not collapse them to only the highest-premium trade.
- Keep dates, timestamps, direction, volume, open interest, `V/OI`, premium amount, strike, expiry, percentile, price zone, and interpretation from the tool output.
- Do not merge different anomaly classes into one sentence.
- Warrant-related classes (`牛熊证街货比例异动（港股）` and `牛熊证街货价格区间异动（港股）`) apply to Hong Kong stocks only. If the stock is not Hong Kong listed, **omit these two classes entirely** from the output. Do not show them with `不适用`.
- Do not invent thresholds, rankings, or causal explanations beyond the returned content.

---

## Preferred Response Template

```markdown
时间范围：{YYYY.M.D - YYYY.M.D}

牛熊证街货比例异动（港股）：
{异常内容或“无异常”}

牛熊证街货价格区间异动（港股）：
{异常内容或“无异常”}

期权大单异动：
{逐条列出全部异常，或“无异常”}

期权波动率异动：
{异常内容或“无异常”}

期权量价异动：
{异常内容或“无异常”}

期权情绪异动：
{异常内容或“无异常”}

期权综合信号异动：
{异常内容或“无异常”}
```

When the user only asks for a subset, keep only the relevant classes.

---

## Behavior Rules

1. Always normalize the user-mentioned stock target into a standard symbol before calling the script.
2. If the target stock is missing, ask a follow-up question.
3. If the market is ambiguous, ask a follow-up question instead of guessing.
4. If the user asks a broad 衍生品问题, omit `analysis_dimensions` and use the full interface.
5. Do not output raw JSON by default when talking to the user, even if the script returns JSON.
6. Do not interpret the result as investment advice or trading guidance.
7. If the downstream service reports a permission issue, state that clearly and do not fabricate analysis.
8. For non-HK stocks, do not force warrant-specific conclusions; use `不适用` or `无异常`.

---

## Example User Requests

### Broad derivatives anomaly check

- "腾讯最近 7 天有没有衍生品异动？"
- "看看 NVDA 最近期权市场有没有异常"
- "帮我查一下 Apple 最近衍生品有没有异动"

Mapped request:

```json
{
  "stock_symbol": "HK.00700",
  "time_range": 7,
  "language_id": 0
}
```

### IV and sentiment only

- "AAPL 最近 IV 和期权情绪有没有异常？"
- "苹果最近期权波动率和 PCR 怎么样？"

Mapped request:

```json
{
  "stock_symbol": "US.AAPL",
  "time_range": 7,
  "analysis_dimensions": ["option_volatility", "option_sentiment"],
  "language_id": 0
}
```

### Unusual option trades only

- "NVDA 最近有没有期权大单押注？"
- "英伟达最近有没有异常期权成交？"

Mapped request:

```json
{
  "stock_symbol": "US.NVDA",
  "time_range": 7,
  "analysis_dimensions": ["option_unusual"],
  "language_id": 0
}
```

### Warrant-related only

- "腾讯最近牛熊证街货比例有没有异常？"
- "00700 最近重货区在哪里？"

Mapped request:

```json
{
  "stock_symbol": "HK.00700",
  "time_range": 7,
  "analysis_dimensions": ["warrant_ratio", "warrant_price_distribution"],
  "language_id": 0
}
```

---

## Example Interpretation Style

```markdown
时间范围：2026.4.2 - 2026.4.9

牛熊证街货比例异动（港股）：
4.3，牛证街货的占比达到82.2%，高于近一年90%的交易日，说明更多投资者持有牛证过夜，反映出看多情绪。
4.7，熊证街货的占比达到17.8%，高于近一年90%的交易日，说明更多投资者持有熊证过夜，反映出看空情绪。

牛熊证街货价格区间异动（港股）：
4.3，牛证的重货区位于95.0-100.0回收价区间，接近当日收市价，说明有较多投资者持有该价格区间的牛证，反映较多投资者认为该价位形成支撑位。
4.7，牛证的最多新增与重货区同时位于95.0-100.0回收价区间，说明较多投资者新增持有了该价格区间的牛证，反映较多投资者认为该价位形成支撑位。

期权大单异动：
4.4 15:31，产生了一笔看涨期权大单，成交量达到1000张，远超过未平仓数130张，V/OI值高达15.2，通常暗示有交易者在新建数量异常的头寸，该交易涉资7.5万美元，合约行权价是10美元，到期日为2025/09/08。
4.6 10:15，产生了一笔看跌期权大单，成交量达到800张，远超过未平仓数50张，V/OI值高达16.0，该交易涉资5.2万美元，合约行权价是165美元，到期日为2025/05/02。

期权波动率异动：
4.5，隐含波动率(IV)处于历史高位，且显著高于已实现的历史波动率(HV)，存在IV-HV值的高额溢价。此环境对期权卖方有利，可卖出期权博弈波动率的均值回归。
4.7，隐含波动率(IV)百分位数达到95，说明隐含波动率超越近一年的大多数日期，时间价值高，可以使用期权卖出策略。

期权量价异动：
4.5，期权成交量环比增长52%，持仓量环比增长48%，正股价格上涨3.5%，可能是做多资金在大量进场，未来上涨趋势可能继续。期权市场整体在260附近出现显著的成交和持仓集中现象，该价位可能成为重要的支撑或阻力位。

期权情绪异动：
4.3，期权Put/Call Ratio百分位达到89，高于近一年89%的交易日，且连续2日上升，看跌期权活跃度显著增加。

期权综合信号异动：
4.8，正股近期出现较大跌幅，但期权隐含波动率百分位变化不大，市场并未出现恐慌性定价，历史上类似情形后常孕育反弹机会。
```
