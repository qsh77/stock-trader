---
name: stock-trader
description: A股/港股/美股选股策略 + 模拟交易系统。三市场独立账户，各 5 万起始资金。
metadata: {"clawdbot":{"emoji":"📈","requires":{"bins":["python3"]}}}
---

# Stock Trader - 选股与模拟交易

运行前置：
- 依赖：`python3`、`akshare`、`yfinance`、`openai`、`pandas`、`ta`
- 选股扫描可直接运行
- 模拟交易未提供 `config.json` 时使用默认费率
- 自动分析需提供 `config.json`，并使用 `STOCK_TRADER_API_KEY` 或 `config.json.api.api_key`

## 选股扫描

```bash
python3 {baseDir}/scripts/screener.py --strategy macd
python3 {baseDir}/scripts/screener.py --strategy ma
python3 {baseDir}/scripts/screener.py --strategy kdj
python3 {baseDir}/scripts/screener.py --strategy combined
python3 {baseDir}/scripts/screener.py --market hk --strategy macd
python3 {baseDir}/scripts/screener.py --market us --strategy macd
python3 {baseDir}/scripts/screener.py --count 50 --strategy macd
python3 {baseDir}/scripts/screener.py --strategy combined --json
```

策略：
- `macd`: MACD 金叉（近3天内 DIF 上穿 DEA）
- `ma`: 均线多头（收盘价站上 MA5 且 MA5 > MA10）
- `kdj`: KDJ 金叉（近3天内 K 上穿 D，J < 90）
- `combined`: MACD 金叉 + 均线多头

参数：`--market a|hk|us`  `--count 100`  `--limit 20`

OpenClaw 集成建议优先使用 `--json`，输出固定结构，包含 `ok`、`processed`、`failed`、`matched`、`duration_ms`、`results`。

## 模拟交易

```bash
python3 {baseDir}/scripts/trade.py buy --code 600519 --shares 100
python3 {baseDir}/scripts/trade.py buy --code AAPL --shares 10
python3 {baseDir}/scripts/trade.py buy --code 00700 --shares 200
python3 {baseDir}/scripts/trade.py sell --code AAPL --shares 5
python3 {baseDir}/scripts/trade.py portfolio
python3 {baseDir}/scripts/trade.py portfolio --market us
python3 {baseDir}/scripts/trade.py account
python3 {baseDir}/scripts/trade.py account --market hk
python3 {baseDir}/scripts/trade.py history
```

## 用户指令映射

| 用户消息 | 动作 |
|---------|------|
| 选股 / 扫描 / scan | 运行 combined 策略（A股） |
| 选股 macd / 选股 kdj / 选股 均线 | 对应策略 |
| 港股选股 | --market hk |
| 美股选股 / US stock scan | --market us |
| 买入 600519 100股 | 模拟买入 A 股 |
| 买入 AAPL 10股 | 模拟买入美股 |
| 买入 00700 200股 | 模拟买入港股 |
| 卖出 AAPL 5股 | 模拟卖出 |
| 持仓 / 仓位 | 查看三个独立账户持仓 |
| 账户 / 资金 | 查看三个独立账户 |
| 交易记录 | 查看历史 |

## 定时任务

可由 OpenClaw 外部调度按交易时段调用分析系统：

```bash
python3 {baseDir}/scripts/auto_analyzer.py
```

三层漏斗：本地技术指标扫描（零 token）→ Cheap Model 初筛 → SOTA 深度分析 → 自动买卖执行。
风控：单只最大仓位 20%、日亏损熔断 5%、持仓涨跌超 3% 触发止盈止损分析。
