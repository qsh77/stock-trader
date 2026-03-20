#!/usr/bin/env python3
"""自动分析交易系统 - 三层漏斗: 本地扫描 → Cheap Model 初筛 → SOTA 深度分析"""
import json, logging, os, re, sys
from datetime import date, datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler

from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screener import STRATEGIES, calc, get_kline, get_stock_list
from trade import MARKET_LABELS, cmd_buy, cmd_sell, detect_market, get_price, load_account

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
CFG = None
client = None
log = logging.getLogger("auto_analyzer")

BJT = timezone(timedelta(hours=8))
EST = timezone(timedelta(hours=-4))  # EDT

# 各市场交易时段 (hour_start, hour_end) — 用于判断是否值得扫描
# 实际用日线数据，非交易时段也能扫，但盘中扫描更有意义
MARKET_HOURS = {
    "a":  {"tz": BJT, "hours": (9, 16)},   # A股 9:30-15:00，放宽到 9-16
    "hk": {"tz": BJT, "hours": (9, 17)},   # 港股 9:30-16:00，放宽到 9-17
    "us": {"tz": EST, "hours": (9, 17)},    # 美股 9:30-16:00 ET
}

def load_cfg():
    if not os.path.exists(CFG_PATH):
        raise RuntimeError(f"缺少配置文件: {CFG_PATH}。请先复制 config.example.json 为 config.json")
    try:
        with open(CFG_PATH) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"配置文件格式错误: {CFG_PATH} | {e}") from e
    api = cfg.get("api") or {}
    if not api.get("base_url"):
        raise RuntimeError("config.json 缺少 api.base_url")
    if not api.get("cheap_model") or not api.get("sota_model"):
        raise RuntimeError("config.json 缺少 cheap_model 或 sota_model")
    return cfg

def init_runtime():
    global CFG, client
    CFG = load_cfg()
    api_key = os.environ.get("STOCK_TRADER_API_KEY") or (CFG.get("api") or {}).get("api_key")
    if not api_key:
        raise RuntimeError("缺少 API Key。请设置 STOCK_TRADER_API_KEY 或 config.json.api.api_key")
    client = OpenAI(
        base_url=CFG["api"]["base_url"],
        api_key=api_key,
        default_headers={
            "User-Agent": "codex_cli_rs/0.77.0 (Windows 10.0.26100; x86_64) WindowsTerminal"
        }
    )


def active_markets():
    """根据当前时间返回在交易时段的市场列表，非交易时段返回全部（用日线数据兜底）"""
    now_bjt = datetime.now(BJT)
    if now_bjt.weekday() >= 5:  # 周末全扫
        return CFG["scan"]["markets"]
    active = []
    for m in CFG["scan"]["markets"]:
        info = MARKET_HOURS.get(m)
        if not info:
            active.append(m)
            continue
        now_local = datetime.now(info["tz"])
        h = now_local.hour
        if info["hours"][0] <= h < info["hours"][1]:
            active.append(m)
    return active or CFG["scan"]["markets"]  # 都不在交易时段就全扫


def extract_json(text):
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\[[\s\S]*\])", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return json.loads(text)


def risk_check(market):
    acc = load_account(market)
    today = date.today().isoformat()
    daily_loss = sum(r.get("profit", 0) for r in acc["history"]
                     if r["time"].startswith(today) and r["action"] == "sell")
    total = acc["cash"] + sum(
        (get_price(c)[0] or p["avg_cost"]) * p["shares"]
        for c, p in acc["positions"].items())
    if daily_loss < -(total * CFG["risk"]["max_daily_loss_pct"]):
        log.warning(f"{MARKET_LABELS[market]}账户日亏损熔断: {daily_loss:.2f}")
        return False
    if acc["cash"] < CFG["risk"]["min_cash_reserve"]:
        log.warning(f"{MARKET_LABELS[market]}账户现金不足: {acc['cash']:.2f}")
        return False
    return True

def check_positions():
    alerts = []
    for market in MARKET_LABELS:
        acc = load_account(market)
        for code, pos in acc["positions"].items():
            cur_price, _ = get_price(code)
            if cur_price is None:
                continue
            pct = (cur_price - pos["avg_cost"]) / pos["avg_cost"]
            threshold = CFG["risk"]["stop_loss_pct"]
            if abs(pct) >= threshold:
                alerts.append({
                    "code": code, "name": pos["name"], "shares": pos["shares"],
                    "avg_cost": pos["avg_cost"], "cur_price": cur_price,
                    "pct": round(pct * 100, 2), "market": market,
                    "type": "take_profit" if pct > 0 else "stop_loss"
                })
    return alerts


def scan_signals():
    markets = active_markets()
    per_market_limit = max(CFG["scan"]["signal_limit"] // len(markets), 10)
    log.info(f"本轮扫描市场: {markets}，每市场限额: {per_market_limit}")
    signals = []
    for market in markets:
        count = 0
        stocks = get_stock_list(market, CFG["scan"]["stock_count"])
        for i, s in enumerate(stocks):
            if count >= per_market_limit:
                break
            try:
                df = get_kline(s["代码"], market)
                if df is None or len(df) < 15:
                    continue
                df = calc(df)
                triggered = [n for n, fn in STRATEGIES.items() if fn(df)]
                if triggered:
                    r = df.iloc[-1]
                    signals.append({
                        "code": s["代码"], "name": s["名称"], "market": market,
                        "price": round(float(r["close"]), 2),
                        "strategies": triggered,
                        "macd": round(float(r["macd"]), 4),
                        "rsi": round(float(r["rsi"]), 1),
                        "j": round(float(r["j"]), 1),
                    })
                    count += 1
            except Exception as e:
                log.debug(f"跳过 {s['代码']}: {e}")
            if (i + 1) % 30 == 0:
                log.info(f"  {market} 扫描进度: {i+1}/{len(stocks)}")
        log.info(f"  {market} 扫描完成，信号: {count}")
    return signals

def llm_screen(signals):
    if not signals:
        return []
    prompt = f"""你是股票分析助手。以下是技术指标扫描出的买入信号:
{json.dumps(signals, ensure_ascii=False, indent=2)}

从中筛选最值得深入分析的股票(最多5只)。优先选择:
- 多个策略同时触发的
- RSI 在 30-70 之间的
- J 值在合理范围的

只返回 JSON 数组，格式: ["code1", "code2"]"""
    try:
        resp = client.chat.completions.create(
            model=CFG["api"]["cheap_model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1)
        codes = extract_json(resp.choices[0].message.content)
        return [s for s in signals if s["code"] in codes]
    except Exception as e:
        log.error(f"初筛失败: {e}")
        return signals[:5]


def llm_analyze(item, action_type="buy"):
    market = item.get("market") or detect_market(item["code"])
    acc = load_account(market)
    total = acc["cash"] + sum(
        (get_price(c)[0] or p["avg_cost"]) * p["shares"]
        for c, p in acc["positions"].items())
    max_pos = total * CFG["risk"]["max_position_pct"]

    if action_type == "buy":
        prompt = f"""你是专业股票分析师。分析以下买入信号:
{json.dumps(item, ensure_ascii=False)}

账户: {MARKET_LABELS[market]}独立账户，可用资金 {acc['cash']:.0f}, 总资产 {total:.0f}, 单只最大仓位 {max_pos:.0f}
综合技术面给出决策。返回JSON(不要markdown):
{{"action":"buy"或"skip","code":"代码","shares":股数(100整数倍),"reason":"理由"}}"""
    else:
        prompt = f"""你是专业股票分析师。持仓触发阈值:
{json.dumps(item, ensure_ascii=False)}

分析应止盈/止损/持有。返回JSON(不要markdown):
{{"action":"sell"或"hold","code":"代码","shares":卖出股数,"reason":"理由"}}"""
    try:
        resp = client.chat.completions.create(
            model=CFG["api"]["sota_model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2)
        return extract_json(resp.choices[0].message.content)
    except Exception as e:
        log.error(f"深度分析失败: {e}")
        return {"action": "skip", "reason": str(e)}

def execute_decision(decision, name=None):
    action = decision.get("action")
    code = decision.get("code")
    market = decision.get("market") or (detect_market(code) if code else None)
    shares = decision.get("shares", 0)
    reason = decision.get("reason", "")
    if action == "buy" and shares > 0:
        log.info(f"执行买入: [{MARKET_LABELS.get(market, '?')}] {code} {name or ''} x {shares} | {reason}")
        result = cmd_buy(code, shares)
        log.info(f"买入结果: {'成功' if result else '失败'}")
    elif action == "sell" and shares > 0:
        log.info(f"执行卖出: [{MARKET_LABELS.get(market, '?')}] {code} {name or ''} x {shares} | {reason}")
        result = cmd_sell(code, shares)
        log.info(f"卖出结果: {'成功' if result else '失败'}")
    else:
        log.info(f"跳过: {code} - {reason}")


def run():
    init_runtime()
    LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s")
    fh = TimedRotatingFileHandler(os.path.join(LOG_DIR, "analyzer.log"), when="D", backupCount=7, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[sh, fh])
    log.info(f"=== Auto Analyzer 启动 ({datetime.now(BJT).strftime('%H:%M')}) ===")

    # 1. 检查持仓止盈止损
    alerts = check_positions()
    if alerts:
        log.info(f"持仓告警: {len(alerts)} 只触发阈值")
        for alert in alerts:
            decision = llm_analyze(alert, action_type="sell")
            execute_decision(decision, name=alert.get("name"))

    # 2. 扫描新信号
    signals = scan_signals()
    log.info(f"本地扫描到 {len(signals)} 个信号")
    if not signals:
        log.info("无信号，本轮结束")
        return

    # 3. Cheap model 初筛
    screened = llm_screen(signals)
    log.info(f"初筛保留 {len(screened)} 个信号")

    # 4. SOTA 逐个深度分析
    for signal in screened:
        if not risk_check(signal["market"]):
            log.info(f"{MARKET_LABELS[signal['market']]}账户风控拦截，跳过买入 {signal['code']}")
            continue
        decision = llm_analyze(signal, action_type="buy")
        decision["market"] = signal["market"]
        execute_decision(decision, name=signal.get("name"))

    log.info("=== 本轮完成 ===")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"\n⚠️ Stock Trader 自动分析失败 ⚠️\n错误: {e}\n请检查 API 配置或网络连接。")
        raise
