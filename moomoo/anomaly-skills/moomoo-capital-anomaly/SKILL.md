---
name: moomoo-capital-anomaly
description: >-
  Detects capital-flow anomaly signals for a user-mentioned stock with
  `get_financial_unusual`, including 资金分布、买卖经纪商、资金流向、卖空数量、
  卖空比例 and their unusual changes. Use when the user asks about 资金动向、资金异动、
  净流入、净流出、主力行为、大单小单、谁在买谁在卖、买卖经纪商、卖空异动、
  卖空/做空/沽空、主力是否在进场或离场，or gives a
  broad stock-anomaly request such as “英伟达异动”、“NVDA 异动”、“腾讯有没有异动/异常”
  without narrowing the dimension. For broad anomaly requests, this skill should
  be used together with `technical-anomaly` and `derivatives-anomaly`. Before
  calling the script, you must first normalize the user-mentioned stock name,
  Chinese company name, English company name, or ticker into a standard symbol
  such as `US.TSLA` or `HK.00700`.
metadata:
  version: 0.0.1
  author: Futu
license: MIT
---

# Capital Anomaly Skill

Detects capital-flow anomalies for a specific stock and formats the result as a structured capital anomaly summary.

This skill is for **异动检测** rather than a regular capital-flow overview. If the data has no qualifying anomaly, return `无异常` or the interface's no-data result, and do not add extra market commentary.

If the user only says a broad request such as `英伟达异动`、`NVDA 异动`、`腾讯有没有异常` and does not specify a dimension, treat it as a bundled anomaly request. In that case, this skill should be used as one of the three default anomaly skills together with `technical-anomaly` and `derivatives-anomaly`.

---

## Workflow

### 1. Parse User Input

Extract the following from the user's request:

- `stock_target`: stock code, Chinese stock name, English company name, or ticker explicitly mentioned by the user
- `time_range`: default `7`; if the user says "最近 3 天" / "过去两周" / "last 5 days", convert it to a natural-day integer
- `analysis_dimensions`: optional; only extract when the user clearly asks about one or more specific financial dimensions
- `language_id`: infer from the user's language

If the target stock is missing, ask a follow-up question instead of guessing.

### 2. Normalize the Stock Target into a Standard Symbol

Before calling the script, convert the user-mentioned stock target into a standard symbol such as `US.TSLA`, `HK.00700`, `SH.600519`, or `SZ.000001`.

Normalization rules:

- If the user already gives a fully qualified symbol like `US.TSLA` or `HK.00700`, use it directly.
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

- `TSLA`, `AAPL`, `NVDA`, `MSFT`, `GOOG`, `META` usually mean US stocks, so normalize to `US.TSLA`, `US.AAPL`, `US.NVDA`, `US.MSFT`, `US.GOOG`, `US.META`.
- `00700` by itself is ambiguous in plain text; when the context clearly refers to Tencent, normalize to `HK.00700`.
- If the same company has both HK and US listings and the user does not specify the market, ask a clarifying question.

### 3. Map Financial Intent to `analysis_dimensions`

Prefer these canonical `analysis_dimensions` values:

- `funds_distribution`: 资金分布
- `funds_broker`: 买卖经纪商
- `funds_flow`: 资金流向
- `short_sell_number`: 卖空数量
- `short_sell_ratio`: 卖空比例
- `short_sell_number_and_ratio`: 卖空数量和比例同时异动

Selection guidance:

- If the user asks about `谁在买/谁在卖/经纪商`, include `funds_broker`.
- If the user asks about `大单小单/资金分歧/资金分布`, include `funds_distribution`.
- If the user asks about `主力连续流入流出/资金流向`, include `funds_flow`.
- If the user asks about `卖空数量`, include `short_sell_number`.
- If the user asks about `卖空比例`, include `short_sell_ratio`.
- If the user asks about `卖空数量和比例是否同时异常`, include `short_sell_number_and_ratio`.
- If the user asks a broad 资金面问题 and does not narrow scope, omit `analysis_dimensions` and use the full interface.

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
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_capital_anomaly.py <STANDARD_SYMBOL> --time-range <DAYS> [--analysis-dimensions ...] [--language-id <ID>] --json
```

Examples:

```bash
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_capital_anomaly.py US.NVDA --time-range 7 --json
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_capital_anomaly.py HK.00700 --time-range 7 --analysis-dimensions funds_distribution funds_broker --json
PYTHON_BIN="$(command -v python3 || command -v python)" && "$PYTHON_BIN" scripts/handle_capital_anomaly.py US.AAPL --time-range 5 --analysis-dimensions funds_flow --json
```

### 6. Check the Result

- If the script exits successfully, use the returned data to build a structured capital-flow anomaly summary.
- If the script returns an error, surface the error message and do not fabricate results.
- If a requested class has no anomaly in the window, explicitly say `无异常`.
- If the downstream service reports a permission problem or no accessible data, clearly state that the stock or account lacks the required permission.

---

## Parameters

### Required

- `stock_symbol`: standard market-prefixed symbol, such as `US.TSLA`, `HK.00700`, `SH.600519`, `SZ.000001`

### Optional

- `time_range`: natural day window, default `7`
- `analysis_dimensions`: list of specific financial anomaly dimensions to inspect; omit for full scan
- `language_id`: output language, default `0`

---

## Output Rules

Present the output by anomaly class. The three high-level classes are:

- `资金分布与买卖经纪商`
- `资金流向`
- `卖空情况`

Formatting rules:

- Always display `时间范围` as an absolute date range in the format `YYYY.M.D - YYYY.M.D`. Calculate the start date from the current date minus `time_range` days (e.g., if today is 2026.4.24 and time_range is 7, write `时间范围：2026.4.18 - 2026.4.24`).
- If multiple abnormal dates appear within the window for one class, list them all.
- If a class has no anomaly in the window, write `无异常`.
- Preserve dates, direction, amount, ratio, broker names, and interpretation from the tool output.
- Do not merge different anomaly classes into one sentence.
- Do not invent thresholds, rankings, or causal explanations beyond the returned content.
- If the user only asked about a subset of financial dimensions, keep only the relevant classes.

---

## Preferred Response Template

```markdown
时间范围：{YYYY.M.D - YYYY.M.D}

- 资金分布与买卖经纪商：{异常内容或“无异常”}
- 资金流向：{异常内容或“无异常”}
- 卖空情况：{异常内容或“无异常”}
```

When the user only asks for a subset, keep only the relevant classes.

---

## Behavior Rules

1. Always normalize the user-mentioned stock target into a standard symbol before calling the script.
2. If the target stock is missing, ask a follow-up question.
3. If the market is ambiguous, ask a follow-up question instead of guessing.
4. If the user asks a broad 资金面问题, omit `analysis_dimensions` and use the full interface.
5. Do not output raw JSON by default when talking to the user, even if the script returns JSON.
6. Do not interpret the result as investment advice or trading guidance.
7. If the downstream service reports a permission issue, state that clearly and do not fabricate analysis.

---

## Example User Requests

### Broad capital anomaly check

- "特斯拉最近 7 天有没有资金面异动？"
- "看看腾讯最近主力有没有异常动作"
- "帮我查一下 Apple 最近资金面有没有异常"

Mapped request:

```json
{
  "stock_symbol": "US.TSLA",
  "time_range": 7,
  "language_id": 0
}
```

### Broker and capital-distribution only

- "腾讯最近谁在买谁在卖？"
- "00700 最近大单小单有没有分歧？"

Mapped request:

```json
{
  "stock_symbol": "HK.00700",
  "time_range": 7,
  "analysis_dimensions": ["funds_distribution", "funds_broker"],
  "language_id": 0
}
```

### Funds flow only

- "苹果最近主力资金有连续流入流出吗？"

Mapped request:

```json
{
  "stock_symbol": "US.AAPL",
  "time_range": 5,
  "analysis_dimensions": ["funds_flow"],
  "language_id": 0
}
```

### Short sell only

- "TSLA 最近卖空比例有没有异常？"

Mapped request:

```json
{
  "stock_symbol": "US.TSLA",
  "time_range": 7,
  "analysis_dimensions": ["short_sell_ratio"],
  "language_id": 0
}
```

---

## Example Interpretation Style

```markdown
时间范围：2026.4.2 - 2026.4.9

- 资金分布与买卖经纪商：4.3，特大单净流入1.13亿元且流入流出金额相差一倍以上，小单净流出1.78亿元，方向相反，代表大资金和小资金存在分歧；从买卖经纪商看，买入排名前二的是中国投资（沪港通）和富途证券，卖出排名前二的是瑞银和巴克莱亚洲。
- 资金流向：4.4，主力资金近4日持续净流出，当日净流出金额比前3日均值高120%，表明主力资金在加速离场。
- 卖空情况：4.5，卖空数量和卖空比例同时异动，卖空数量日环比上升，卖空比例也同步抬升，且两者均高于近一月均值，体现较强烈的看空预期。
```
