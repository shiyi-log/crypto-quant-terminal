#!/usr/bin/env python3
"""实盘参数的唯一读取入口

背景（真实发生过的问题）：
    面板要显示「当前实盘用的是哪组参数」，但 `build_iteration.py` 写死了一份
    {"enter_period": 20, "exit_period": 20, "top_n": 8, ...}，
    `auto_iterate.py` 又写死了 CURRENT = (20, 20)。
    策略参数一改，这两个地方不会跟着变 —— 面板继续显示旧值，巡检也拿旧参数
    去排名，结论全是错的。

唯一权威来源是策略源码本身：
    · `xxx = IntParameter(..., default=N)` / `DecimalParameter` / `RealParameter`
    · 类属性 `stoploss = -0.6`
    行情周期与币对则来自实盘 config。

本模块只用正则解析源码，不 import 策略（freqtrade 依赖太重，服务端未必装）。

用法:
    from live_params import read_strategy_params, current_entry_exit, live_config
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_CONFIG = os.path.join(ROOT, "user_data", "config_trend_live.json")
STRATEGY_DIR = os.path.join(ROOT, "user_data", "strategies")

# name = IntParameter(...) / DecimalParameter(...) / RealParameter(...) / CategoricalParameter(...)
_PARAM_RE = re.compile(
    r"^\s*(\w+)\s*=\s*"
    r"(?:CategoricalParameter|DecimalParameter|IntParameter|RealParameter)\s*\(",
)
_DEFAULT_RE = re.compile(r"default\s*=\s*(-?[\d.]+)")
_STOPLOSS_RE = re.compile(r"^\s*stoploss\s*=\s*(-?[\d.]+)\s*$", re.M)
_TIMEFRAME_RE = re.compile(r"^\s*timeframe\s*=\s*[\"']([^\"']+)[\"']", re.M)


def live_config(path=LIVE_CONFIG):
    """读取实盘 config（不存在则返回空 dict）"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def strategy_name(cfg=None):
    return (cfg or live_config()).get("strategy") or ""


def strategy_file(name=None, cfg=None):
    name = name or strategy_name(cfg)
    if not name:
        return None
    path = os.path.join(STRATEGY_DIR, f"{name}.py")
    return path if os.path.exists(path) else None


def _parse_params(source: str) -> dict:
    """解析策略源码里的参数默认值（支持跨行书写的定义）"""
    out = {}
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        m = _PARAM_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        buf = lines[i]
        depth = buf.count("(") - buf.count(")")
        j = i
        while depth > 0 and j + 1 < len(lines):
            j += 1
            buf += " " + lines[j]
            depth += lines[j].count("(") - lines[j].count(")")
        d = _DEFAULT_RE.search(buf)
        if d:
            val = float(d.group(1))
            out[name] = int(val) if val.is_integer() else val
        i = j + 1
    return out


def read_strategy_params(name=None, cfg=None) -> dict:
    """策略参数默认值 + stoploss / timeframe；读不到时返回空 dict"""
    path = strategy_file(name, cfg)
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = _parse_params(src)
    sl = _STOPLOSS_RE.search(src)
    if sl:
        out["stoploss"] = float(sl.group(1))
    tf = _TIMEFRAME_RE.search(src)
    if tf:
        out["timeframe_attr"] = tf.group(1)
    return out


def current_entry_exit(cfg=None):
    """当前实盘的 (enter_period, exit_period) —— 给滚动走查排名用"""
    p = read_strategy_params(cfg=cfg)
    return int(p.get("enter_period", 20)), int(p.get("exit_period", 20))


def live_params_view(cfg=None) -> dict:
    """面板展示用的「当前实盘配置」快照"""
    cfg = cfg if cfg is not None else live_config()
    p = read_strategy_params(cfg=cfg)
    whitelist = (cfg.get("exchange") or {}).get("pair_whitelist") or []
    return {
        "strategy": strategy_name(cfg) or "—",
        "timeframe": cfg.get("timeframe") or p.get("timeframe_attr") or "—",
        "pairs": len(whitelist),
        "dry_run": cfg.get("dry_run"),
        "params": {k: p.get(k) for k in (
            "enter_period", "exit_period", "top_n", "target_exposure", "stoploss")},
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(live_params_view())
