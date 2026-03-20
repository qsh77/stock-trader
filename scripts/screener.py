#!/usr/bin/env python3
"""A股选股策略扫描器 - 轻量并发版"""
import argparse, json, sys, time, warnings
from datetime import datetime, timedelta
import akshare as ak, pandas as pd, ta
import yfinance as yf

warnings.filterwarnings("ignore")


def get_stock_list(market="a", count=100):
    if market == "a":
        df = ak.index_stock_cons_csindex(symbol="000300")
        return [{"代码": r["成分券代码"], "名称": r["成分券名称"]} for _, r in df.head(count).iterrows()]
    if market == "us":
        pool = [
            ("AAPL","Apple"),("MSFT","Microsoft"),("GOOGL","Alphabet"),("AMZN","Amazon"),
            ("NVDA","NVIDIA"),("META","Meta"),("TSLA","Tesla"),("BRK-B","Berkshire"),
            ("JPM","JPMorgan"),("V","Visa"),("UNH","UnitedHealth"),("MA","Mastercard"),
            ("HD","Home Depot"),("PG","P&G"),("JNJ","J&J"),("COST","Costco"),
            ("ABBV","AbbVie"),("CRM","Salesforce"),("NFLX","Netflix"),("AMD","AMD"),
            ("LLY","Eli Lilly"),("AVGO","Broadcom"),("PEP","PepsiCo"),("KO","Coca-Cola"),
            ("TMO","Thermo Fisher"),("MRK","Merck"),("ADBE","Adobe"),("WMT","Walmart"),
            ("ORCL","Oracle"),("CSCO","Cisco"),
        ]
        return [{"代码": c, "名称": n} for c, n in pool[:count]]
    # 港股蓝筹
    pool = [
        ("00700","腾讯控股"),("09988","阿里巴巴-W"),("09618","京东集团-SW"),
        ("03690","美团-W"),("01810","小米集团-W"),("00941","中国移动"),
        ("02318","中国平安"),("01211","比亚迪股份"),("09888","百度集团-SW"),
        ("00388","香港交易所"),("02020","安踏体育"),("01024","快手-W"),
        ("00005","汇丰控股"),("00011","恒生银行"),("01398","工商银行"),
        ("00883","中海油"),("02628","中国人寿"),("00027","银河娱乐"),
        ("01928","金沙中国"),("00016","新鸿基地产"),("00001","长和"),
        ("09999","网易-S"),("02269","药明生物"),("00175","吉利汽车"),
        ("02382","舜宇光学"),("01109","华润置地"),("00669","创科实业"),
        ("00003","香港中华煤气"),("00006","电能实业"),("01038","长江基建"),
    ]
    return [{"代码": c, "名称": n} for c, n in pool[:count]]


def get_kline(code, market="a", days=90):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    for attempt in range(2):
        try:
            if market == "us":
                df = yf.Ticker(code).history(period=f"{days}d")
                if df is None or len(df) < 10: return None
                df = df.reset_index()
                df.rename(columns={"Date":"date","Open":"open","High":"high",
                                   "Low":"low","Close":"close","Volume":"volume"}, inplace=True)
            elif market == "a":
                # 新浪接口，不限流。代码格式: sh600519 / sz000001
                prefix = "sh" if code.startswith("6") else "sz"
                df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
                if df is None or len(df) < 30: return None
                df = df.tail(days).reset_index(drop=True)
                df.rename(columns={"date":"date","open":"open","high":"high",
                                   "low":"low","close":"close","volume":"volume"}, inplace=True)
            else:
                df = ak.stock_hk_hist(symbol=code, period="daily",
                                      start_date=start, end_date=end, adjust="qfq")
                if df is None or len(df) < 10: return None
                df.columns = [c.lower().replace("日期","date").replace("开盘","open")
                    .replace("收盘","close").replace("最高","high")
                    .replace("最低","low").replace("成交量","volume") for c in df.columns]
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
            for col in ["open","high","low","close","volume"]:
                if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception:
            if attempt == 0: time.sleep(0.5)
    return None


def calc(df):
    df["ma5"] = ta.trend.sma_indicator(df["close"], window=5)
    df["ma10"] = ta.trend.sma_indicator(df["close"], window=10)
    df["ma20"] = ta.trend.sma_indicator(df["close"], window=20)
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"], df["signal"] = macd.macd(), macd.macd_signal()
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"])
    df["k"], df["d"] = stoch.stoch(), stoch.stoch_signal()
    df["j"] = 3 * df["k"] - 2 * df["d"]
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    return df

def s_macd(df):
    """MACD金叉：近3天内DIF上穿DEA"""
    if len(df) < 5: return False
    for i in range(-3, 0):
        r, p = df.iloc[i], df.iloc[i-1]
        if pd.notna(r["macd"]) and pd.notna(r["signal"]) and p["macd"] < p["signal"] and r["macd"] > r["signal"]:
            return True
    return False

def s_ma(df):
    """均线多头：收盘价站上MA5且MA5>MA10"""
    if len(df) < 3: return False
    r = df.iloc[-1]
    return pd.notna(r["ma5"]) and r["close"] > r["ma5"] > r["ma10"]

def s_kdj(df):
    """KDJ金叉：近3天内K上穿D"""
    if len(df) < 5: return False
    for i in range(-3, 0):
        r, p = df.iloc[i], df.iloc[i-1]
        if pd.notna(r["k"]) and p["k"] < p["d"] and r["k"] > r["d"] and r["j"] < 90:
            return True
    return False

def s_combined(df):
    return s_macd(df) and s_ma(df)

STRATEGIES = {"macd": s_macd, "ma": s_ma, "kdj": s_kdj, "combined": s_combined}

def emit(payload, json_output=False):
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)

def die(msg, json_output=False, **payload):
    if json_output:
        body = {"ok": False, "error": msg}
        body.update(payload)
        emit(body, json_output=True)
    else:
        print(f"❌ {msg}")
    sys.exit(1)

def normalize_args(limit, count, json_output=False):
    if count <= 0:
        die("--count 必须大于 0", json_output=json_output)
    if limit <= 0:
        die("--limit 必须大于 0", json_output=json_output)
    if limit > count:
        if not json_output:
            print(f"⚠️ --limit={limit} 大于 --count={count}，已自动调整为 {count}")
        limit = count
    return limit, count

def analyze(stock, market, fn):
    code, name = stock["代码"], stock["名称"]
    df = get_kline(code, market)
    if df is None or len(df) < 15: return None
    df = calc(df)
    if fn(df):
        r = df.iloc[-1]
        return {"代码": code, "名称": name, "最新价": round(float(r["close"]), 2),
                "MACD": round(float(r["macd"]), 4), "RSI": round(float(r["rsi"]), 1),
                "J值": round(float(r["j"]), 1)}
    return None

def run(market="a", strategy="combined", limit=20, count=100, json_output=False):
    started_at = time.time()
    fn = STRATEGIES.get(strategy)
    if not fn:
        die(f"未知策略: {strategy}", json_output=json_output, strategy=strategy)
    limit, count = normalize_args(limit, count, json_output=json_output)
    market_name = {"a": "A股", "hk": "港股", "us": "美股"}[market]
    if not json_output:
        print(f"📈 扫描{market_name} | 策略:{strategy} | 候选:{count}只")
    try:
        stocks = get_stock_list(market, count)
    except Exception as e:
        die(f"获取{market_name}股票列表失败: {e}", json_output=json_output,
            market=market, strategy=strategy, count=count, limit=limit)
    if not stocks:
        die(f"{market_name}股票列表为空", json_output=json_output,
            market=market, strategy=strategy, count=count, limit=limit)
    results = []
    failed = 0
    processed = 0
    for i, s in enumerate(stocks):
        if not json_output and (i + 1) % 20 == 0:
            print(f"  进度: {i+1}/{len(stocks)}")
        try:
            processed += 1
            r = analyze(s, market, fn)
            if r: results.append(r)
        except Exception:
            failed += 1
        if len(results) >= limit: break
    payload = {
        "ok": True,
        "market": market,
        "market_name": market_name,
        "strategy": strategy,
        "requested_count": count,
        "requested_limit": limit,
        "candidate_count": len(stocks),
        "processed": processed,
        "failed": failed,
        "matched": len(results),
        "duration_ms": int((time.time() - started_at) * 1000),
        "results": results,
    }
    if not results:
        if json_output:
            emit(payload, json_output=True)
            return
        msg = "未找到符合条件的股票。"
        if failed:
            msg += f" 其中 {failed} 只处理失败，可稍后重试。"
        print(msg)
        return
    if json_output:
        emit(payload, json_output=True)
        return
    print(f"\n✅ 找到 {len(results)} 只符合 [{strategy}] 的股票:\n")
    for r in results:
        print(f"  {r['代码']} {r['名称']}  价格:{r['最新价']}  RSI:{r['RSI']}  J:{r['J值']}")
    if failed:
        print(f"\n⚠️ 有 {failed} 只股票在扫描中处理失败，结果未包含这些标的。")
    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--market", default="a", choices=["a", "hk", "us"])
    p.add_argument("--strategy", default="combined", choices=list(STRATEGIES))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--json", action="store_true", dest="json_output")
    a = p.parse_args()
    run(a.market, a.strategy, a.limit, a.count, a.json_output)
