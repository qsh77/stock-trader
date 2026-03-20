#!/usr/bin/env python3
"""模拟交易系统 - 三市场独立账户买卖、持仓管理、盈亏统计"""
import argparse
import json
import os
import shutil
import sys
import warnings
from datetime import datetime

import akshare as ak
import yfinance as yf

warnings.filterwarnings("ignore")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
ACCOUNT_FILE = os.path.join(DATA_DIR, "account.json")
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
INITIAL_CASH = 50000.0
MARKET_LABELS = {"a": "A股", "hk": "港股", "us": "美股"}


def _now():
    return datetime.now().isoformat()


def _normalize_code(code):
    return str(code).strip().upper()


def detect_market(code):
    code = _normalize_code(code)
    if code.isdigit():
        return "a" if len(code) == 6 else "hk"
    return "us"


def _market_label(market):
    return MARKET_LABELS[market]


def _new_market_account(market, created=None):
    return {
        "market": market,
        "initial_cash": INITIAL_CASH,
        "cash": INITIAL_CASH,
        "positions": {},
        "history": [],
        "created": created or _now(),
    }


def _new_accounts(created=None):
    created = created or _now()
    return {
        "version": 2,
        "created": created,
        "accounts": {market: _new_market_account(market, created) for market in MARKET_LABELS},
    }


def _load_cfg():
    if not os.path.exists(CFG_PATH):
        return {}
    try:
        with open(CFG_PATH) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"配置文件格式错误: {CFG_PATH} | {e}") from e


def _get_fees():
    cfg = _load_cfg()
    fees = cfg.get("fees", {})
    return fees.get("commission", 0.0003), fees.get("stamp_tax", 0.001)


def _normalize_history(records, market):
    normalized = []
    for record in records or []:
        item = dict(record)
        item["code"] = _normalize_code(item.get("code", ""))
        item["market"] = item.get("market") or market
        normalized.append(item)
    return normalized


def _normalize_positions(positions):
    normalized = {}
    for code, pos in (positions or {}).items():
        normalized[_normalize_code(code)] = dict(pos)
    return normalized


def _normalize_market_account(data, market, created=None):
    base = _new_market_account(market, created)
    if not isinstance(data, dict):
        return base
    base["initial_cash"] = float(data.get("initial_cash", INITIAL_CASH))
    base["cash"] = float(data.get("cash", base["initial_cash"]))
    base["positions"] = _normalize_positions(data.get("positions"))
    base["history"] = _normalize_history(data.get("history"), market)
    base["created"] = data.get("created") or base["created"]
    return base


def _replay_legacy_records(records, market, created):
    acc = _new_market_account(market, created)
    for record in sorted(records, key=lambda item: item.get("time", "")):
        code = _normalize_code(record.get("code", ""))
        name = record.get("name") or code
        shares = int(record.get("shares") or 0)
        price = float(record.get("price") or 0)
        fee = float(record.get("fee") or 0)
        action = record.get("action")
        if not code or shares <= 0 or price <= 0 or action not in {"buy", "sell"}:
            continue
        if action == "buy":
            cost = price * shares
            acc["cash"] -= cost + fee
            pos = acc["positions"].get(code, {"name": name, "shares": 0, "avg_cost": 0.0})
            old_total = pos["shares"] * pos["avg_cost"]
            pos["shares"] += shares
            pos["avg_cost"] = (old_total + cost) / pos["shares"]
            pos["name"] = name
            acc["positions"][code] = pos
        else:
            revenue = price * shares
            acc["cash"] += revenue - fee
            pos = acc["positions"].get(code)
            if pos:
                pos["shares"] -= shares
                if pos["shares"] <= 0:
                    acc["positions"].pop(code, None)
                else:
                    acc["positions"][code] = pos
        item = dict(record)
        item["code"] = code
        item["market"] = market
        acc["history"].append(item)
    return acc


def _migrate_legacy_account(data):
    created = data.get("created") or _now()
    accounts = _new_accounts(created)
    positions_by_market = {market: {} for market in MARKET_LABELS}
    for code, pos in (data.get("positions") or {}).items():
        code = _normalize_code(code)
        positions_by_market[detect_market(code)][code] = dict(pos)
    history_by_market = {market: [] for market in MARKET_LABELS}
    for record in data.get("history") or []:
        code = _normalize_code(record.get("code", ""))
        if not code:
            continue
        history_by_market[detect_market(code)].append(dict(record))
    for market in MARKET_LABELS:
        if history_by_market[market]:
            acc = _replay_legacy_records(history_by_market[market], market, created)
        else:
            acc = _new_market_account(market, created)
        if positions_by_market[market] and not acc["positions"]:
            acc["positions"] = _normalize_positions(positions_by_market[market])
            acc["cash"] = acc["initial_cash"] - sum(
                float(item.get("avg_cost", 0)) * int(item.get("shares", 0))
                for item in acc["positions"].values()
            )
        accounts["accounts"][market] = _normalize_market_account(acc, market, created)
    return accounts


def load_accounts():
    if not os.path.exists(ACCOUNT_FILE):
        return _new_accounts()
    with open(ACCOUNT_FILE, "r") as f:
        data = json.load(f)
    changed = False
    if "accounts" in data:
        accounts = _new_accounts(data.get("created"))
        for market in MARKET_LABELS:
            raw = (data.get("accounts") or {}).get(market)
            accounts["accounts"][market] = _normalize_market_account(raw, market, data.get("created"))
            if raw != accounts["accounts"][market]:
                changed = True
        if data.get("version") != 2 or set((data.get("accounts") or {}).keys()) != set(MARKET_LABELS):
            changed = True
    else:
        accounts = _migrate_legacy_account(data)
        changed = True
    if changed:
        save_accounts(accounts)
    return accounts


def load_account(market):
    market = market or "a"
    if market not in MARKET_LABELS:
        raise RuntimeError(f"不支持的市场: {market}")
    return load_accounts()["accounts"][market]


def save_accounts(accounts):
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(ACCOUNT_FILE):
        shutil.copy2(ACCOUNT_FILE, ACCOUNT_FILE + ".bak")
    with open(ACCOUNT_FILE, "w") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def save_account(acc, market):
    accounts = load_accounts()
    accounts["accounts"][market] = _normalize_market_account(acc, market, accounts.get("created"))
    save_accounts(accounts)


def get_price(code):
    code = _normalize_code(code)

    if not code.isdigit():
        try:
            ticker = yf.Ticker(code)
            hist = ticker.history(period="5d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
                try:
                    name = ticker.info.get("shortName", code)
                except Exception:
                    name = code
                return price, name
        except Exception:
            pass
        return None, None

    try:
        if len(code) == 6:
            df = ak.stock_zh_a_spot_em()
            if "代码" in df.columns:
                code_col = df["代码"].astype(str).str.zfill(6).str.strip()
                row = df[code_col == code]
                if not row.empty:
                    return float(row.iloc[0]["最新价"]), str(row.iloc[0]["名称"])

        df = ak.stock_hk_spot_em()
        if "代码" in df.columns:
            code_col = df["代码"].astype(str).str.strip()
            row = df[code_col == code]
            if not row.empty:
                return float(row.iloc[0]["最新价"]), str(row.iloc[0]["名称"])
    except Exception:
        pass

    try:
        if len(code) == 6:
            prefix = "sh" if code.startswith("6") else "sz"
            df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
            if df is not None and not df.empty:
                last = df.iloc[-1]
                name = code
                try:
                    name_df = ak.stock_info_a_code_name()
                    match = name_df[name_df["code"].astype(str).str.zfill(6) == code]
                    if not match.empty:
                        name = str(match.iloc[0]["name"])
                except Exception:
                    pass
                return float(last["close"]), name
    except Exception:
        pass

    return None, None


def _account_totals(acc):
    total_value = 0.0
    total_profit = 0.0
    details = []
    for code, pos in acc["positions"].items():
        cur_price, _ = get_price(code)
        cur_price = cur_price or pos["avg_cost"]
        value = cur_price * pos["shares"]
        profit = (cur_price - pos["avg_cost"]) * pos["shares"]
        pct = (cur_price / pos["avg_cost"] - 1) * 100 if pos["avg_cost"] else 0.0
        total_value += value
        total_profit += profit
        details.append({
            "code": code,
            "name": pos["name"],
            "shares": pos["shares"],
            "avg_cost": pos["avg_cost"],
            "cur_price": cur_price,
            "value": value,
            "profit": profit,
            "pct": pct,
        })
    total = acc["cash"] + total_value
    profit = total - acc["initial_cash"]
    pct = profit / acc["initial_cash"] * 100 if acc["initial_cash"] else 0.0
    return {
        "details": details,
        "total_value": total_value,
        "position_profit": total_profit,
        "total": total,
        "profit": profit,
        "pct": pct,
    }


def _print_account_summary(market, acc):
    stats = _account_totals(acc)
    print(f"💰 {_market_label(market)}账户")
    print(f"  初始资金: {acc['initial_cash']:,.2f}")
    print(f"  可用资金: {acc['cash']:,.2f}")
    print(f"  持仓市值: {stats['total_value']:,.2f}")
    print(f"  总资产:   {stats['total']:,.2f}")
    print(f"  总盈亏:   {stats['profit']:+,.2f} ({stats['pct']:+.1f}%)")


def cmd_buy(code, shares, price=None):
    code = _normalize_code(code)
    market = detect_market(code)
    acc = load_account(market)
    commission, _ = _get_fees()
    if price is None:
        price, name = get_price(code)
        if price is None:
            print(f"❌ 无法获取 {code} 的价格")
            return False
    else:
        _, name = get_price(code)
        name = name or code
    cost = price * shares
    fee = cost * commission
    total_cost = cost + fee
    if total_cost > acc["cash"]:
        print(f"❌ {_market_label(market)}账户资金不足。需要 {total_cost:.2f}，可用 {acc['cash']:.2f}")
        return False
    acc["cash"] -= total_cost
    pos = acc["positions"].get(code, {"name": name, "shares": 0, "avg_cost": 0.0})
    old_total = pos["shares"] * pos["avg_cost"]
    pos["shares"] += shares
    pos["avg_cost"] = (old_total + cost) / pos["shares"]
    pos["name"] = name
    acc["positions"][code] = pos
    acc["history"].append({
        "time": _now(),
        "action": "buy",
        "market": market,
        "code": code,
        "name": name,
        "shares": shares,
        "price": price,
        "fee": round(fee, 2),
    })
    save_account(acc, market)
    print(f"✅ [{_market_label(market)}账户] 买入 {name}({code}) {shares}股 @ {price:.2f}")
    print(f"   花费: {total_cost:.2f}（含手续费 {fee:.2f}）")
    print(f"   剩余资金: {acc['cash']:.2f}")
    return True


def cmd_sell(code, shares, price=None):
    code = _normalize_code(code)
    market = detect_market(code)
    acc = load_account(market)
    commission, stamp_tax = _get_fees()
    pos = acc["positions"].get(code)
    if not pos or pos["shares"] < shares:
        print(f"❌ {_market_label(market)}账户持仓不足。当前持有 {pos['shares'] if pos else 0} 股")
        return False
    if price is None:
        price, _ = get_price(code)
        if price is None:
            print(f"❌ 无法获取 {code} 的价格")
            return False
    revenue = price * shares
    fee = revenue * commission
    tax = revenue * stamp_tax
    net = revenue - fee - tax
    profit = (price - pos["avg_cost"]) * shares
    acc["cash"] += net
    pos["shares"] -= shares
    if pos["shares"] == 0:
        del acc["positions"][code]
    else:
        acc["positions"][code] = pos
    acc["history"].append({
        "time": _now(),
        "action": "sell",
        "market": market,
        "code": code,
        "name": pos["name"],
        "shares": shares,
        "price": price,
        "fee": round(fee + tax, 2),
        "profit": round(profit, 2),
    })
    save_account(acc, market)
    pct = profit / (pos["avg_cost"] * shares) * 100 if pos["avg_cost"] else 0.0
    emoji = "🟢" if profit >= 0 else "🔴"
    print(f"✅ [{_market_label(market)}账户] 卖出 {pos['name']}({code}) {shares}股 @ {price:.2f}")
    print(f"   {emoji} 盈亏: {profit:+.2f} ({pct:+.1f}%)")
    print(f"   剩余资金: {acc['cash']:.2f}")
    return True


def cmd_portfolio(market=None):
    markets = [market] if market else list(MARKET_LABELS)
    for idx, current_market in enumerate(markets):
        acc = load_account(current_market)
        stats = _account_totals(acc)
        print(f"📋 {_market_label(current_market)}账户")
        if not stats["details"]:
            print("  当前无持仓")
        else:
            for item in stats["details"]:
                emoji = "🟢" if item["profit"] >= 0 else "🔴"
                print(f"  {item['name']}({item['code']})")
                print(
                    f"    {item['shares']}股 | 成本:{item['avg_cost']:.2f} | 现价:{item['cur_price']:.2f} | "
                    f"{emoji} {item['profit']:+.2f} ({item['pct']:+.1f}%)"
                )
            print(f"  持仓市值: {stats['total_value']:.2f}")
            print(f"  持仓盈亏: {stats['position_profit']:+.2f}")
        print(f"  可用资金: {acc['cash']:.2f}")
        print(f"  总资产:   {stats['total']:.2f}")
        if idx < len(markets) - 1:
            print()


def cmd_account(market=None):
    markets = [market] if market else list(MARKET_LABELS)
    total_initial = 0.0
    total_cash = 0.0
    total_value = 0.0
    for idx, current_market in enumerate(markets):
        acc = load_account(current_market)
        stats = _account_totals(acc)
        _print_account_summary(current_market, acc)
        total_initial += acc["initial_cash"]
        total_cash += acc["cash"]
        total_value += stats["total_value"]
        if idx < len(markets) - 1:
            print()
    if not market and len(markets) > 1:
        total_asset = total_cash + total_value
        total_profit = total_asset - total_initial
        pct = total_profit / total_initial * 100 if total_initial else 0.0
        print()
        print("💰 总览")
        print(f"  初始资金: {total_initial:,.2f}")
        print(f"  可用资金: {total_cash:,.2f}")
        print(f"  持仓市值: {total_value:,.2f}")
        print(f"  总资产:   {total_asset:,.2f}")
        print(f"  总盈亏:   {total_profit:+,.2f} ({pct:+.1f}%)")


def cmd_history(code=None, market=None):
    if code:
        code = _normalize_code(code)
        market = market or detect_market(code)
    markets = [market] if market else list(MARKET_LABELS)
    printed = False
    for current_market in markets:
        acc = load_account(current_market)
        records = acc["history"]
        if code:
            records = [record for record in records if record["code"] == code]
        if not records:
            continue
        printed = True
        print(f"📜 {_market_label(current_market)}账户交易记录")
        print()
        for record in records[-20:]:
            action = "买入" if record["action"] == "buy" else "卖出"
            line = (
                f"  {record['time'][:16]} {action} {record['name']}({record['code']}) "
                f"{record['shares']}股 @ {record['price']:.2f}"
            )
            if "profit" in record:
                line += f"  盈亏:{record['profit']:+.2f}"
            print(line)
        print()
    if not printed:
        print("📜 暂无交易记录")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    buy_p = sub.add_parser("buy")
    buy_p.add_argument("--code", required=True)
    buy_p.add_argument("--shares", type=int, required=True)
    buy_p.add_argument("--price", type=float, default=None)

    sell_p = sub.add_parser("sell")
    sell_p.add_argument("--code", required=True)
    sell_p.add_argument("--shares", type=int, required=True)
    sell_p.add_argument("--price", type=float, default=None)

    portfolio_p = sub.add_parser("portfolio")
    portfolio_p.add_argument("--market", choices=list(MARKET_LABELS), default=None)

    account_p = sub.add_parser("account")
    account_p.add_argument("--market", choices=list(MARKET_LABELS), default=None)

    hist_p = sub.add_parser("history")
    hist_p.add_argument("--code", default=None)
    hist_p.add_argument("--market", choices=list(MARKET_LABELS), default=None)

    args = parser.parse_args()
    try:
        if args.cmd == "buy":
            cmd_buy(args.code, args.shares, args.price)
        elif args.cmd == "sell":
            cmd_sell(args.code, args.shares, args.price)
        elif args.cmd == "portfolio":
            cmd_portfolio(args.market)
        elif args.cmd == "account":
            cmd_account(args.market)
        elif args.cmd == "history":
            cmd_history(args.code, args.market)
        else:
            parser.print_help()
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
