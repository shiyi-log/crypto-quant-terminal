#!/usr/bin/env python3
"""
序列面板磁盘缓存

为什么需要：
    build_panel() 要跑 pandas 特征工程，实测 66~220 秒 ——
    比一个 trial 的训练还贵，而且每启动一个新进程都要重来。

做法：
    user_data/cache/panel_<seq_len>_seq.npy      序列张量
    user_data/cache/panel_<seq_len>_meta.parquet 事件元数据
    user_data/cache/panel_<seq_len>_feats.json   特征列名

    · 首次调用构建并落盘；后续调用直接加载（约 1~2 秒）
    · 只缓存 user_data 下的文件，不碰任何实盘 config
    · 数据源更新后用 --refresh 或删除 cache 目录即可重建

用法:
    from panel_cache import load_or_build
    seq, meta, feats = load_or_build(30, log=print)
"""

import json
import os
import sys
import time

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "user_data", "cache")


def data_source() -> str:
    """当前行情来源：database（默认）或 feather。"""
    return (os.environ.get("QUANT_OHLCV_SOURCE") or "database").lower()


def paths(seq_len: int, dense: bool = True, source: str | None = None):
    """缓存路径。

    ⚠ 缓存键【必须包含数据来源】。
      feather 与 database 两条路的数据虽然等价（实测重排后零差异），
      但【行序不同】—— 共用缓存会让切换静默失效，或者读到另一种排序的面板，
      进而让跨轮比较、以及"到底用的哪份数据"变得不可知。
      （同第 58 轮那个 bug 的教训：静默的错比响亮的错危险得多。）
    """
    src = (source or data_source()).lower()
    # ⚠ 缓存键必须包含【schema 版本】。
    #   v1 的 meta 没有 t1 列；若沿用旧文件名，加了 t1 之后会静默读到缺列的老缓存，
    #   purge 就会变成"看起来做了、实际没做"。
    #   与第 58 轮"缓存键漏了数据来源"是同一类错误：静默的错比响亮的错危险。
    tag = f"v2_{seq_len}" + ("" if dense else "_sparse")
    if src != "feather":
        tag += f"_{src}"
    return (os.path.join(CACHE_DIR, f"panel_{tag}_seq.npy"),
            os.path.join(CACHE_DIR, f"panel_{tag}_meta.parquet"),
            os.path.join(CACHE_DIR, f"panel_{tag}_feats.json"))


def load_or_build(seq_len: int, dense: bool = True, log=print, refresh: bool = False):
    f_seq, f_meta, f_feats = paths(seq_len, dense)
    if not refresh and os.path.exists(f_seq) and os.path.exists(f_meta):
        t0 = time.time()
        try:
            seq = np.load(f_seq)
            meta = pd.read_parquet(f_meta)
            if seq.ndim != 3 or len(meta) != seq.shape[0]:
                raise ValueError(f"缓存形状不一致 seq={getattr(seq, 'shape', None)} "
                                 f"meta={len(meta)}")
            feats = []
            if os.path.exists(f_feats):
                with open(f_feats, encoding="utf-8") as f:
                    feats = json.load(f)
            log(f"    面板来自磁盘缓存 seq_len={seq_len} · {seq.shape} · "
                f"{seq.nbytes / 1e6:.0f} MB · {time.time() - t0:.1f}s")
            return seq, meta, feats
        except Exception as e:
            # 上一次构建被中断会留下残缺文件 —— 直接重建，别让坏缓存污染结果
            log(f"    ⚠ 缓存不可用（{type(e).__name__}: {e}），重新构建")

    if BASE not in sys.path:
        sys.path.insert(0, BASE)
    import ml_seq

    t0 = time.time()
    seq, meta, feats = ml_seq.build_panel(dense=dense, seq_len=seq_len)
    build_s = time.time() - t0
    log(f"    面板构建完成 seq_len={seq_len} · {seq.shape} · "
        f"{seq.nbytes / 1e6:.0f} MB · {build_s:.0f}s（写入缓存）")
    try:
        # 原子写：先写 .tmp 再 replace，中途被 kill 也不会留下半个文件
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.save(f_seq + ".tmp.npy", seq)
        os.replace(f_seq + ".tmp.npy", f_seq)
        meta.to_parquet(f_meta + ".tmp.parquet")
        os.replace(f_meta + ".tmp.parquet", f_meta)
        with open(f_feats + ".tmp", "w", encoding="utf-8") as f:
            json.dump(list(feats), f, ensure_ascii=False)
        os.replace(f_feats + ".tmp", f_feats)
    except Exception as e:  # 缓存失败不影响主流程
        log(f"    ⚠ 缓存写入失败（不影响本轮）：{type(e).__name__}: {e}")
    return seq, meta, list(feats)


def clear(seq_len: int = None) -> int:
    """删除缓存（数据源更新后调用）。返回删除的文件数。"""
    n = 0
    if not os.path.isdir(CACHE_DIR):
        return 0
    for fn in os.listdir(CACHE_DIR):
        if seq_len is not None and f"panel_{seq_len}_" not in fn:
            continue
        if fn.startswith("panel_") and fn.endswith((".npy", ".parquet", ".json")):
            os.remove(os.path.join(CACHE_DIR, fn))
            n += 1
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="面板缓存管理")
    ap.add_argument("--build", type=int, nargs="*", help="预构建这些 seq_len")
    ap.add_argument("--refresh", action="store_true", help="强制重建")
    ap.add_argument("--clear", type=int, nargs="?", const=-1, help="删除缓存（可指定 seq_len）")
    a = ap.parse_args()
    if a.clear is not None:
        print(f"已删除 {clear(None if a.clear == -1 else a.clear)} 个缓存文件")
    if a.build:
        for sl in a.build:
            load_or_build(sl, refresh=a.refresh)
