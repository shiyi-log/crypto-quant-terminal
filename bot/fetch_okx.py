#!/usr/bin/env python3
"""
OKX 永续数据下载 —— 扩大横截面宽度（第 14 轮）

动机（第 13 轮的结构解释）：
    实测：活跃币数 23（2022）→ 51（2026）时，ML 信号从「不稳健」变「稳健」，
    横截面离散度同步从 2.90% 升到 4.33%。
    → **标的池宽度是信号强度的直接来源。**

    Binance 只有 57 个永续，OKX 有 485 个（2023 年底前 100 个）。
    扩大池子是目前最有理论依据的方向。

产出格式与 Binance 一致，便于现有 ml_lab / ml_seq / walkforward 直接复用：
    user_data/data/okx/futures/{COIN}_USDT_USDT-1d-futures.feather
    列: date, open, high, low, close, volume

用法:
    python fetch_okx.py --list                 # 列出候选
    python fetch_okx.py --top 100              # 下载前 100 个（按上线时间）
    python fetch_okx.py --top 100 --min-year 2023
"""

import argparse
import json
import os
import time
import urllib.request

import pandas as pd

OUT = "user_data/data/okx/futures"
UA = {"User-Agent": "Mozilla/5.0"}


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.0 + i)


def list_swaps():
    d = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    rows = [x for x in d.get("data", [])
            if x.get("settleCcy") == "USDT" and x.get("state") == "live"]
    df = pd.DataFrame(rows)
    df["listTime"] = pd.to_datetime(df["listTime"].astype("int64"), unit="ms",
                                    utc=True).dt.tz_localize(None)
    df = df.sort_values("listTime").reset_index(drop=True)
    return df


def fetch_one(inst_id, max_pages=40):
    """分页下载单币日线（OKX 每页 100 条，往前翻）"""
    rows, after = [], None
    for _ in range(max_pages):
        u = ("https://www.okx.com/api/v5/market/history-candles?"
             f"instId={inst_id}&bar=1D&limit=100")
        if after:
            u += f"&after={after}"
        try:
            d = get(u)
        except Exception:
            break
        r = d.get("data", [])
        if not r:
            break
        rows += r
        after = r[-1][0]
        time.sleep(0.12)
        if len(r) < 100:
            break
    if len(rows) < 200:
        return None
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "vol",
                                     "volCcy", "volCcyQuote", "confirm"])
    df["date"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms",
                                utc=True).dt.tz_localize(None)
    df = df.drop_duplicates("date").sort_values("date")
    for c in ["o", "h", "l", "c", "vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = pd.DataFrame({
        "date": df["date"].values,
        "open": df["o"].values, "high": df["h"].values,
        "low": df["l"].values, "close": df["c"].values,
        "volume": df["volCcy"].values,      # 用币计价的成交量
    }).dropna()
    return out.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=100, help="下载前 N 个（按上线时间）")
    ap.add_argument("--min-year", type=int, default=0,
                    help="只要在某年前上线的（缩小历史长度、加快下载）")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--only-new", action="store_true", default=False,
                    help="只要 Binance 没有的币（扩池 + 降低幸存者偏差）")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    df = list_swaps()
    print(f"  OKX USDT 永续 {len(df)} 个")

    if args.list:
        print(f"\n  {'#':>4}{'合约':<24}{'上线':<12}")
        print("  " + "-" * 42)
        for i, r in df.head(60).iterrows():
            print(f"  {i+1:>4}{r['instId']:<24}{str(r['listTime'].date()):<12}")
        return

    if args.min_year:
        df = df[df["listTime"] < f"{args.min_year}-12-31"]
        print(f"  {args.min_year} 年底前上线: {len(df)} 个")

    if args.only_new:
        import glob as _g
        bn = {os.path.basename(f).split("_")[0]
              for f in _g.glob("user_data/data/binance/futures/*-1d-futures.feather")}
        before = len(df)
        df = df[~df["instId"].str.split("-").str[0].isin(bn)]
        print(f"  排除 Binance 已有币: {before} → {len(df)} 个 OKX 独有")

    targets = df.head(args.top)["instId"].tolist()
    print(f"  目标 {len(targets)} 个\n")

    ok, skip, fail = 0, 0, []
    t0 = time.time()
    for i, inst in enumerate(targets):
        coin = inst.split("-")[0]
        f = f"{OUT}/{coin}_USDT_USDT-1d-futures.feather"
        if args.skip_existing and os.path.exists(f):
            skip += 1
            continue
        try:
            d = fetch_one(inst)
            if d is None or len(d) < 200:
                fail.append(coin); continue
            d.to_feather(f)
            ok += 1
            if ok % 10 == 0 or ok <= 3:
                print(f"  [{i+1}/{len(targets)}] {coin:<10} {len(d):>5} 天  "
                      f"{d['date'].min().date()}~{d['date'].max().date()}  "
                      f"({time.time()-t0:.0f}s)")
        except Exception as exc:
            fail.append(coin)
            print(f"  [{i+1}/{len(targets)}] {coin:<10} 失败 {type(exc).__name__}")

    print(f"\n  ✅ 成功 {ok} · 跳过已存在 {skip} · 失败 {len(fail)} · "
          f"耗时 {time.time()-t0:.0f}s")
    if fail:
        print(f"  失败列表（前20）: {fail[:20]}")

    files = [f for f in os.listdir(OUT) if f.endswith(".feather")]
    print(f"  {OUT} 现有 {len(files)} 个币")


if __name__ == "__main__":
    main()
