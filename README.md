# Stock Trader

A股/港股/美股技术指标选股 + LLM 分析 + 模拟交易系统。

## 架构

```
screener.py   ← 技术指标扫描 (MACD/MA/KDJ)
auto_analyzer.py ← 三层漏斗: 本地扫描 → Cheap Model 初筛 → SOTA 深度分析 → 自动执行
trade.py      ← 模拟交易引擎 (买卖/持仓/盈亏)
config.json   ← 配置 (API/费率/风控/扫描参数)
data/         ← 账户数据 & 日志
```

## 快速开始

### 依赖

```bash
pip install akshare yfinance openai
```

### 配置 API Key

```bash
export STOCK_TRADER_API_KEY="your-api-key"
```

或填入 `config.json` 的 `api.api_key` 字段（不推荐）。

### 选股扫描

```bash
python3 scripts/screener.py --strategy macd          # A股 MACD 金叉
python3 scripts/screener.py --strategy combined       # MACD + 均线多头
python3 scripts/screener.py --market hk --strategy ma # 港股均线
python3 scripts/screener.py --market us --strategy kdj # 美股 KDJ
```

策略说明：
- `macd` — MACD 金叉（近3天 DIF 上穿 DEA）
- `ma` — 均线多头（收盘价站上 MA5 且 MA5 > MA10）
- `kdj` — KDJ 金叉（近3天 K 上穿 D，J < 90）
- `combined` — MACD 金叉 + 均线多头

### 模拟交易

```bash
python3 scripts/trade.py buy --code 600519 --shares 100
python3 scripts/trade.py sell --code AAPL --shares 5
python3 scripts/trade.py portfolio    # 持仓
python3 scripts/trade.py account      # 账户概览
python3 scripts/trade.py history      # 交易记录
```

### 自动分析

```bash
python3 scripts/auto_analyzer.py
```

三层漏斗自动运行：本地技术指标扫描（零 token）→ Cheap Model 初筛 → SOTA 逐只深度分析 → 自动买卖执行。

## 风控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| max_position_pct | 20% | 单只最大仓位占比 |
| max_daily_loss_pct | 5% | 日亏损熔断线 |
| stop_loss_pct | 3% | 持仓跌幅触发止损分析 |
| take_profit_pct | 3% | 持仓涨幅触发止盈分析 |
| min_cash_reserve | 5000 | 最低现金保留 |

## 费率

在 `config.json` 的 `fees` 字段配置：
- `commission`: 佣金费率（默认万三）
- `stamp_tax`: 印花税率（默认千一，仅卖出）
