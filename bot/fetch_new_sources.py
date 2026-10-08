#!/usr/bin/env python3
"""
新收益来源探测 —— 数据下载

已确认可达的数据源：
    ✅ Binance 合约资金费率      fapi/v1/fundingRate
    ✅ OKX 合约资金费率          api/v5/public/funding-rate-history
    ✅ OKX 现货/合约行情
    ✅ Deribit 波动率指数 DVOL
    ✅ Blockchain.info 链上数据

本脚本下载并落盘，供后续可行性检验使用。

用法:
    python fetch_new_sources.py --funding     # 资金费率（两所）
    python fetch_new_sources.py --dvol        # 期权波动率指数
    python fetch_new_sources.py --all
"""

import argparse
import json
import os
import time
import urllib.request

import pandas as pd

OUT = "user_data/newsrc"
os.makedirs(OUT, exist_ok=True)

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC",
         "DOT", "NEAR", "SUI", "APT", "AAVE", "UNI", "FIL", "TRX", "ZEC", "SAND"]

UA = {"User-Agent": "Mozilla/5.0"}


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as exc:
            if i == tries - 1:
                raise
            time.sleep(1.5)


# ══════════════ Binance 资金费率 ══════════════

def binance_funding(coin, start_ms=1577836800000):
    """Binance 资金费率历史（每 8 小时一次，每次最多 1000 条）"""
    out = []
    cur = start_ms
    while True:
        url = (f"https://fapi.binance.com/fapi/v1/fundingRate?"
               f"symbol={coin}USDT&startTime={cur}&limit=1000")
        try:
            d = get(url)
        except Exception:
            break
        if not d:
            break
        out += d
        if len(d) < 1000:
            break
        cur = d[-1]["fundingTime"] + 1
        time.sleep(0.25)
    if not out:
        return None
    df = pd.DataFrame(out)
    df["time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True).dt.tz_localize(None)
    df["rate"] = df["fundingRate"].astype(float)
    return df.set_index("time")["rate"].sort_index()


# ══════════════ OKX 资金费率 ══════════════

def okx_funding(coin, max_pages=30):
    """OKX 资金费率历史（每 8 小时一次，每次最多 100 条，需分页往前翻）"""
    out = []
    before = ""
    for _ in range(max_pages):
        url = ("https://www.okx.com/api/v5/public/funding-rate-history?"
               f"instId={coin}-USDT-SWAP&limit=100")
        if before:
            url += f"&before={before}"
        try:
            d = get(url)
        except Exception:
            break
        rows = d.get("data") or []
        if not rows:
            break
        out += rows
        before = rows[-1]["fundingTime"]
        time.sleep(0.2)
        if len(rows) < 100:
            break
    if not out:
        return None
    df = pd.DataFrame(out)
    df["time"] = pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms",
                                utc=True).dt.tz_localize(None)
    df["rate"] = df["realizedRate"].astype(float)
    s = df.set_index("time")["rate"].sort_index()
    s = s[~s.index.duplicated(keep="first")]     # OKX 分页会重叠
    return s


# ══════════════ Deribit DVOL ══════════════

def deribit_dvol(currency="BTC", days=1500):
    """Deribit 波动率指数（日线）"""
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    url = ("https://www.deribit.com/api/v2/public/get_volatility_index_data?"
           f"currency={currency}&start_timestamp={start}"
           f"&end_timestamp={end}&resolution=1D")
    d = get(url)
    rows = (d.get("result") or {}).get("data") or []
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    return df.set_index("time")["close"].sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--funding", action="store_true")
    ap.add_argument("--dvol", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    do_all = args.all or not (args.funding or args.dvol)

    if do_all or args.funding:
        print("=" * 88)
        print("下载资金费率（Binance + OKX）")
        print("=" * 88)
        bn, ok = {}, {}
        for c in COINS:
            try:
                s = binance_funding(c)
                if s is not None and len(s) > 100:
                    bn[c] = s
                    print(f"  {c:<6} Binance {len(s):>5} 条  "
                          f"{s.index[0].date()} ~ {s.index[-1].date()}", end="")
            except Exception as exc:
                print(f"  {c:<6} Binance 失败: {type(exc).__name__}", end="")
            try:
                s2 = okx_funding(c)
                if s2 is not None and len(s2) > 100:
                    ok[c] = s2
                    print(f"   | OKX {len(s2):>5} 条  "
                          f"{s2.index[0].date()} ~ {s2.index[-1].date()}")
                else:
                    print(f"   | OKX 数据不足")
            except Exception as exc:
                print(f"   | OKX 失败: {type(exc).__name__}")
        if bn:
            pd.DataFrame(bn).to_pickle(f"{OUT}/funding_binance.pkl")
            print(f"\n  ✅ Binance 资金费率 {len(bn)} 币 → {OUT}/funding_binance.pkl")
        if ok:
            # 各币时间戳不完全对齐，用 outer join 再排序
            okdf = pd.DataFrame({k: v for k, v in ok.items()})
            okdf = okdf[~okdf.index.duplicated(keep="first")].sort_index()
            okdf.to_pickle(f"{OUT}/funding_okx.pkl")
            print(f"  ✅ OKX 资金费率 {len(ok)} 币 → {OUT}/funding_okx.pkl")

    if do_all or args.dvol:
        print()
        print("=" * 88)
        print("下载期权波动率指数 DVOL")
        print("=" * 88)
        for cur in ["BTC", "ETH"]:
            try:
                s = deribit_dvol(cur)
                if s is not None:
                    s.to_pickle(f"{OUT}/dvol_{cur.lower()}.pkl")
                    print(f"  ✅ {cur} DVOL {len(s)} 条  "
                          f"{s.index[0].date()} ~ {s.index[-1].date()}  "
                          f"当前 {s.iloc[-1]:.1f}")
            except Exception as exc:
                print(f"  ❌ {cur} 失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
