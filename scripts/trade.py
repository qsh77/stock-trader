#!/usr/bin/env python3
"""模拟交易系统 - 虚拟账户买卖、持仓管理、盈亏统计"""
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

def _load_cfg():
    with open(CFG_PATH) as f:
        return json.load(f)

def _get_fees():
    cfg = _load_cfg()
    fees = cfg.get("fees", {})
    return fees.get("commission", 0.0003), fees.get("stamp_tax", 0.001)

def load_account():
    if os.path.exists(ACCOUNT_FILE):
        with open(ACCOUNT_FILE, "r") as f:
            return json.load(f)
    return {"cash": INITIAL_CASH, "positions": {}, "history": [], "created": datetime.now().isoformat()}

def save_account(acc):
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(ACCOUNT_FILE):
        shutil.copy2(ACCOUNT_FILE, ACCOUNT_FILE + ".bak")
    with open(ACCOUNT_FILE, "w") as f:
        json.dump(acc, f, ensure_ascii=False, indent=2)

def get_price(code):
    code = str(code).strip()

    # 美股：代码含字母（非纯数字）
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

    # 1) 先查实时行情
    try:
        if code.isdigit() and len(code) == 6:
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

    # 2) 实时行情失败时，回退到日线接口（尤其适合非交易时段）
    try:
        if code.isdigit() and len(code) == 6:
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

def cmd_buy(code, shares, price=None):
    acc = load_account()
    commission, _ = _get_fees()
    if price is None:
        price, name = get_price(code)
        if price is None:
            print(f"❌ 无法获取 {code} 的价格"); return False
    else:
        _, name = get_price(code)
        name = name or code
    cost = price * shares
    fee = cost * commission
    total_cost = cost + fee
    if total_cost > acc["cash"]:
        print(f"❌ 资金不足。需要 {total_cost:.2f}，可用 {acc['cash']:.2f}"); return False
    acc["cash"] -= total_cost
    pos = acc["positions"].get(code, {"name": name, "shares": 0, "avg_cost": 0.0})
    old_total = pos["shares"] * pos["avg_cost"]
    pos["shares"] += shares
    pos["avg_cost"] = (old_total + cost) / pos["shares"]
    pos["name"] = name
    acc["positions"][code] = pos
    acc["history"].append({"time": datetime.now().isoformat(), "action": "buy", "code": code,
                           "name": name, "shares": shares, "price": price, "fee": round(fee, 2)})
    save_account(acc)
    print(f"✅ 买入 {name}({code}) {shares}股 @ {price:.2f}")
    print(f"   花费: {total_cost:.2f}（含手续费 {fee:.2f}）")
    print(f"   剩余资金: {acc['cash']:.2f}")
    return True

def cmd_sell(code, shares, price=None):
    acc = load_account()
    commission, stamp_tax = _get_fees()
    pos = acc["positions"].get(code)
    if not pos or pos["shares"] < shares:
        print(f"❌ 持仓不足。当前持有 {pos['shares'] if pos else 0} 股"); return False
    if price is None:
        price, _ = get_price(code)
        if price is None:
            print(f"❌ 无法获取 {code} 的价格"); return False
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
    acc["history"].append({"time": datetime.now().isoformat(), "action": "sell", "code": code,
                           "name": pos["name"], "shares": shares, "price": price,
                           "fee": round(fee + tax, 2), "profit": round(profit, 2)})
    save_account(acc)
    pct = profit / (pos["avg_cost"] * shares) * 100
    emoji = "🟢" if profit >= 0 else "🔴"
    print(f"✅ 卖出 {pos['name']}({code}) {shares}股 @ {price:.2f}")
    print(f"   {emoji} 盈亏: {profit:+.2f} ({pct:+.1f}%)")
    print(f"   剩余资金: {acc['cash']:.2f}")
    return True

def cmd_portfolio():
    acc = load_account()
    if not acc["positions"]:
        print("📋 当前无持仓"); return
    print("📋 当前持仓:\n")
    total_value = 0
    total_profit = 0
    for code, pos in acc["positions"].items():
        cur_price, _ = get_price(code)
        cur_price = cur_price or pos["avg_cost"]
        value = cur_price * pos["shares"]
        profit = (cur_price - pos["avg_cost"]) * pos["shares"]
        pct = (cur_price / pos["avg_cost"] - 1) * 100
        total_value += value
        total_profit += profit
        emoji = "🟢" if profit >= 0 else "🔴"
        print(f"  {pos['name']}({code})")
        print(f"    {pos['shares']}股 | 成本:{pos['avg_cost']:.2f} | 现价:{cur_price:.2f} | {emoji} {profit:+.2f} ({pct:+.1f}%)")
    print(f"\n  持仓市值: {total_value:.2f}")
    print(f"  持仓盈亏: {total_profit:+.2f}")
    print(f"  可用资金: {acc['cash']:.2f}")
    print(f"  总资产:   {acc['cash'] + total_value:.2f}")

def cmd_account():
    acc = load_account()
    total_value = 0
    for code, pos in acc["positions"].items():
        cur_price, _ = get_price(code)
        total_value += (cur_price or pos["avg_cost"]) * pos["shares"]
    total = acc["cash"] + total_value
    profit = total - INITIAL_CASH
    pct = profit / INITIAL_CASH * 100
    print(f"💰 模拟账户")
    print(f"  初始资金: {INITIAL_CASH:,.2f}")
    print(f"  可用资金: {acc['cash']:,.2f}")
    print(f"  持仓市值: {total_value:,.2f}")
    print(f"  总资产:   {total:,.2f}")
    print(f"  总盈亏:   {profit:+,.2f} ({pct:+.1f}%)")

def cmd_history(code=None):
    acc = load_account()
    records = acc["history"]
    if code:
        records = [r for r in records if r["code"] == code]
    if not records:
        print("📜 暂无交易记录"); return
    print("📜 交易记录:\n")
    for r in records[-20:]:
        action = "买入" if r["action"] == "buy" else "卖出"
        line = f"  {r['time'][:16]} {action} {r['name']}({r['code']}) {r['shares']}股 @ {r['price']:.2f}"
        if "profit" in r:
            line += f"  盈亏:{r['profit']:+.2f}"
        print(line)

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
    sub.add_parser("portfolio")
    sub.add_parser("account")
    hist_p = sub.add_parser("history")
    hist_p.add_argument("--code", default=None)
    args = parser.parse_args()
    if args.cmd == "buy": cmd_buy(args.code, args.shares, args.price)
    elif args.cmd == "sell": cmd_sell(args.code, args.shares, args.price)
    elif args.cmd == "portfolio": cmd_portfolio()
    elif args.cmd == "account": cmd_account()
    elif args.cmd == "history": cmd_history(args.code)
    else: parser.print_help()
