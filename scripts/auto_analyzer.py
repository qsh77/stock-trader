#!/usr/bin/env python3
"""自动分析交易系统 - 三层漏斗: 本地扫描 → Cheap Model 初筛 → SOTA 深度分析"""
import json, logging, os, re, sys
from collections import Counter
from datetime import date, datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler

from openai import OpenAI

LAST_REPORT = None

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
综合技术面、仓位约束和风险给出决策。返回JSON(不要markdown):
{{"action":"buy"或"skip","code":"代码","shares":股数(100整数倍),"confidence":0到100,"reason":"一句话结论","thesis":"2到4句详细分析","positives":["看多依据1","看多依据2"],"risks":["风险1","风险2"],"plan":"后续观察或执行计划"}}"""
    else:
        prompt = f"""你是专业股票分析师。持仓触发阈值:
{json.dumps(item, ensure_ascii=False)}

分析应止盈/止损/持有。返回JSON(不要markdown):
{{"action":"sell"或"hold","code":"代码","shares":卖出股数,"confidence":0到100,"reason":"一句话结论","thesis":"2到4句详细分析","positives":["支持卖出或继续持有的依据1"],"risks":["风险1","风险2"],"plan":"后续处理计划"}}"""
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
    shares = _to_int(decision.get("shares", 0), 0)
    reason = decision.get("reason", "")
    confidence = decision.get("confidence")
    confidence = _to_int(confidence, None) if confidence is not None else None
    result = False
    status = "skipped"
    if action == "buy" and shares > 0:
        log.info(f"执行买入: [{MARKET_LABELS.get(market, '?')}] {code} {name or ''} x {shares} | {reason}")
        result = cmd_buy(code, shares)
        status = "success" if result else "failed"
        log.info(f"买入结果: {'成功' if result else '失败'}")
    elif action == "sell" and shares > 0:
        log.info(f"执行卖出: [{MARKET_LABELS.get(market, '?')}] {code} {name or ''} x {shares} | {reason}")
        result = cmd_sell(code, shares)
        status = "success" if result else "failed"
        log.info(f"卖出结果: {'成功' if result else '失败'}")
    else:
        log.info(f"跳过: {code} - {reason}")
    return {
        "status": status,
        "result": result,
        "action": action,
        "code": code,
        "market": market,
        "name": name or code,
        "shares": shares,
        "reason": reason,
        "confidence": confidence,
        "thesis": decision.get("thesis", ""),
        "positives": decision.get("positives") or [],
        "risks": decision.get("risks") or [],
        "plan": decision.get("plan", ""),
    }


def _to_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _compact_text(value):
    return " ".join(str(value or "").strip().split())


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _decision_label(action, status):
    if status == "success" and action == "buy":
        return "已买入"
    if status == "success" and action == "sell":
        return "已卖出"
    return {
        "buy": "跳过买入",
        "sell": "跳过卖出",
        "skip": "跳过",
        "hold": "继续持有",
    }.get(action, "未执行")


def _account_snapshot():
    snapshots = []
    total_cash = 0.0
    total_positions = 0
    for market, label in MARKET_LABELS.items():
        acc = load_account(market)
        positions = acc["positions"]
        total_cash += acc["cash"]
        total_positions += len(positions)
        snapshots.append({
            "market": market,
            "label": label,
            "cash": acc["cash"],
            "positions": len(positions),
            "codes": list(positions.keys()),
        })
    return snapshots, total_cash, total_positions


def _market_trade_context(market):
    acc = load_account(market)
    total = acc["cash"] + sum(
        (get_price(code)[0] or pos["avg_cost"]) * pos["shares"]
        for code, pos in acc["positions"].items()
    )
    return {
        "cash": acc["cash"],
        "max_pos": total * CFG["risk"]["max_position_pct"],
    }


def _position_snapshot():
    snapshots = {}
    for market in MARKET_LABELS:
        acc = load_account(market)
        for code, pos in acc["positions"].items():
            cur_price = get_price(code)[0] or pos["avg_cost"]
            pct = (cur_price / pos["avg_cost"] - 1) * 100 if pos["avg_cost"] else 0.0
            snapshots[code] = {
                "market": market,
                "shares": pos["shares"],
                "avg_cost": pos["avg_cost"],
                "cur_price": cur_price,
                "pct": pct,
            }
    return snapshots


def _build_overall_summary(decisions):
    if not decisions:
        return "本轮未进入交易决策阶段，系统仅完成市场扫描。"
    buy_success = sum(1 for item in decisions if item.get("status") == "success" and item.get("action") == "buy")
    sell_success = sum(1 for item in decisions if item.get("status") == "success" and item.get("action") == "sell")
    skipped = [item for item in decisions if item.get("status") != "success"]
    risk_hits = sum(1 for item in skipped if "风控" in (item.get("reason") or ""))
    reasons = []
    for item in skipped:
        reasons.extend(_to_list(item.get("risks")))
        reason = _compact_text(item.get("reason"))
        if reason:
            reasons.append(reason)
    top_reasons = [text for text, _ in Counter(reasons).most_common(3)]
    if buy_success or sell_success:
        summary = f"本轮共执行买入 {buy_success} 笔、卖出 {sell_success} 笔。"
    else:
        summary = "本轮未触发实际成交，系统继续以观察为主。"
    if risk_hits:
        summary += f" 其中 {risk_hits} 笔被风控直接拦截。"
    if top_reasons:
        summary += " 主要制约因素：" + "；".join(top_reasons[:3]) + "。"
    return summary


def _decision_price(item, signal_by_code, alert_by_code):
    signal = signal_by_code.get(item.get("code"))
    if signal and signal.get("price") is not None:
        return signal.get("price")
    alert = alert_by_code.get(item.get("code"))
    if alert and alert.get("cur_price") is not None:
        return alert.get("cur_price")
    return None


def build_report(start_time, markets, alerts, signals, screened, decisions, risk_events=None):
    lines = []
    signal_by_code = {item["code"]: item for item in screened}
    alert_by_code = {item["code"]: item for item in alerts}
    market_context = {market: _market_trade_context(market) for market in MARKET_LABELS}
    position_by_code = _position_snapshot()
    buy_success = sum(1 for item in decisions if item.get("status") == "success" and item.get("action") == "buy")
    sell_success = sum(1 for item in decisions if item.get("status") == "success" and item.get("action") == "sell")
    blocked = sum(1 for item in decisions if item.get("status") == "blocked")
    skipped = sum(1 for item in decisions if item.get("status") != "success")
    market_counter = Counter(item["market"] for item in signals)
    account_rows, total_cash, total_positions = _account_snapshot()

    lines.append(f"{start_time.strftime('%H:%M')} 自动分析已完成。")
    lines.append("")
    lines.append("本轮结果")
    market_names = [MARKET_LABELS.get(m, m) for m in markets] if markets else []
    lines.append(f"• 扫描市场：{'、'.join(market_names) if market_names else '无'}")
    if market_counter:
        market_breakdown = "；".join(f"{MARKET_LABELS.get(m, m)} {c} 个" for m, c in market_counter.items())
        lines.append(f"• 市场信号分布：{market_breakdown}")
    lines.append(f"• 本地扫描信号：{len(signals)} 个")
    lines.append(f"• 初筛保留：{len(screened)} 个")
    lines.append(f"• 持仓告警：{len(alerts)} 个")
    lines.append(f"• 买入执行：{buy_success} 笔")
    lines.append(f"• 卖出执行：{sell_success} 笔")
    lines.append(f"• 未成交决策：{skipped} 笔")
    if blocked or risk_events:
        lines.append(f"• 风控拦截：{max(blocked, len(risk_events or []))} 笔")

    if risk_events:
        lines.append("")
        lines.append("风控事件")
        for item in risk_events[:8]:
            lines.append(f"• {item}")

    if screened:
        lines.append("")
        lines.append("候选信号概览")
        for item in screened:
            market = MARKET_LABELS.get(item.get("market"), item.get("market") or "?")
            strategies = "、".join(item.get("strategies") or [])
            context = market_context.get(item.get("market"), {})
            lines.append(
                f"• [{market}] {item['code']} {item.get('name') or item['code']} | 现价 {item.get('price', 0):.2f}"
                + (f" | 仓位上限 {context.get('max_pos', 0):.2f}" if context else "")
            )
            lines.append(
                f"  指标：策略 {strategies or '无'} | "
                f"MACD {item.get('macd', 0):.4f} | RSI {item.get('rsi', 0):.1f} | J {item.get('j', 0):.1f}"
            )

    if decisions:
        lines.append("")
        lines.append("逐标的决策")
        for item in decisions:
            code = item.get("code")
            market = MARKET_LABELS.get(item.get("market"), item.get("market") or "?")
            name = item.get("name") or code
            label = _decision_label(item.get("action"), item.get("status"))
            confidence = item.get("confidence")
            signal = signal_by_code.get(code)
            price = _decision_price(item, signal_by_code, alert_by_code)
            context = market_context.get(item.get("market"), {})
            position = position_by_code.get(code)
            parts = []
            header = f"• [{market}] {code} {name}"
            if price is not None:
                parts.append(f"现价 {float(price):.2f}")
            if item.get("action") in {"buy", "skip"}:
                if item.get("shares"):
                    parts.append(f"建议 {item['shares']} 股")
                if price is not None and item.get("shares"):
                    parts.append(f"约 {float(price) * item['shares']:.2f}")
                if context:
                    parts.append(f"仓位上限 {context.get('max_pos', 0):.2f}")
            else:
                alert = alert_by_code.get(code)
                base = alert or position
                if base:
                    parts.append(f"持仓 {base.get('shares', 0)} 股")
                    parts.append(f"成本 {base.get('avg_cost', 0):.2f}")
                    parts.append(f"浮动 {base.get('pct', 0):+.2f}%")
            if parts:
                header += " | " + " | ".join(parts)
            header += f"：{label}"
            if isinstance(confidence, (int, float)):
                header += f"（置信度 {int(confidence)}）"
            lines.append(header)
            if signal:
                strategies = "、".join(signal.get("strategies") or [])
                lines.append(
                    f"  触发信号：{strategies or '无'} | 现价 {signal.get('price', 0):.2f} | "
                    f"MACD {signal.get('macd', 0):.4f} | RSI {signal.get('rsi', 0):.1f} | J {signal.get('j', 0):.1f}"
                )
            reason = _compact_text(item.get("reason")) or "无明确原因"
            lines.append(f"  结论：{reason}")
            thesis = _compact_text(item.get("thesis"))
            if thesis:
                lines.append(f"  分析：{thesis}")
            positives = _to_list(item.get("positives"))
            if positives:
                lines.append(f"  支撑点：{'；'.join(positives[:3])}")
            risks = _to_list(item.get("risks"))
            if risks:
                lines.append(f"  风险点：{'；'.join(risks[:3])}")
            plan = _compact_text(item.get("plan"))
            if plan:
                lines.append(f"  后续：{plan}")
            if item.get("status") == "success" and item.get("shares"):
                lines.append(f"  执行：{item['shares']} 股")

    if alerts:
        lines.append("")
        lines.append("持仓告警明细")
        for item in alerts[:8]:
            market = MARKET_LABELS.get(item.get("market"), item.get("market") or "?")
            lines.append(
                f"• [{market}] {item['code']} {item.get('name') or item['code']} | 现价 {item.get('cur_price', 0):.2f} | "
                f"类型 {item.get('type')} | 成本 {item.get('avg_cost', 0):.2f} | "
                f"浮动 {item.get('pct', 0):+.2f}%"
            )

    lines.append("")
    lines.append("当前持仓与资金")
    for item in account_rows:
        codes = "、".join(item["codes"][:5]) if item["codes"] else "空仓"
        lines.append(
            f"• {item['label']}账户：现金 {item['cash']:.2f}，持仓 {item['positions']} 个，标的 {codes}"
        )
    lines.append(f"• 总持仓：{total_positions} 个")
    lines.append(f"• 总现金：{total_cash:.2f}")

    lines.append("")
    lines.append("整体结论")
    lines.append(f"• {_build_overall_summary(decisions)}")
    return "\n".join(lines)


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
    start_time = datetime.now(BJT)
    markets = active_markets()
    decisions = []
    risk_events = []
    log.info(f"=== Auto Analyzer 启动 ({start_time.strftime('%H:%M')}) ===")

    # 1. 检查持仓止盈止损
    alerts = check_positions()
    if alerts:
        log.info(f"持仓告警: {len(alerts)} 只触发阈值")
        for alert in alerts:
            decision = llm_analyze(alert, action_type="sell")
            decision["market"] = alert.get("market") or detect_market(alert["code"])
            decisions.append(execute_decision(decision, name=alert.get("name")))

    # 2. 扫描新信号
    signals = scan_signals()
    log.info(f"本地扫描到 {len(signals)} 个信号")
    if not signals:
        report = build_report(start_time, markets, alerts, signals, [], decisions, risk_events)
        log.info("=== 本轮完成 ===")
        print("\n" + report)
        return report

    # 3. Cheap model 初筛
    screened = llm_screen(signals)
    log.info(f"初筛保留 {len(screened)} 个信号")

    # 4. SOTA 逐个深度分析
    for signal in screened:
        if not risk_check(signal["market"]):
            msg = f"{MARKET_LABELS[signal['market']]}账户风控拦截，跳过买入 {signal['code']}"
            log.info(msg)
            risk_events.append(msg)
            decisions.append({
                "status": "blocked",
                "result": False,
                "action": "buy",
                "code": signal["code"],
                "market": signal["market"],
                "name": signal.get("name") or signal["code"],
                "shares": 0,
                "reason": "风控拦截",
            })
            continue
        decision = llm_analyze(signal, action_type="buy")
        decision["market"] = signal["market"]
        decisions.append(execute_decision(decision, name=signal.get("name")))

    report = build_report(start_time, markets, alerts, signals, screened, decisions, risk_events)
    log.info("=== 本轮完成 ===")
    print("\n" + report)
    return report


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"\n⚠️ Stock Trader 自动分析失败 ⚠️\n错误: {e}\n请检查 API 配置或网络连接。")
        raise
