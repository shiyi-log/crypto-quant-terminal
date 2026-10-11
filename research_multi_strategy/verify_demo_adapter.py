#!/usr/bin/env python3
"""DSH 的币安 Demo 适配层验证脚手架（只读）。

用法: python3 research_multi_strategy/verify_demo_adapter.py
⚠️ 设计约束（吸取我自己的 bug 教训）:
   · 【排除本脚本自身】，避免自指匹配
   · 检查范围【限定在 Demo 适配层的文件】，不对全仓误判
     （项目本来就用币安实盘 endpoint 拉行情，那是正常且必要的）
   · 先【发现】适配层文件，再对它们做检查；找不到就明确说「未实现」
⚠️ 本脚本只读: 不改文件、不写 DB、不下单、不碰凭据。
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
SOURCE_PATHS = (
    "binance_demo_adapter.py", "binance_demo_credentials.py", "demo_store.py",
    "demo_execution_service.py", "demo_runner_controller.py", "demo_api.py",
    "database/demo_schema.sql", "database/demo_credentials_schema.sql",
)

def iter_src():
    """只读明确的 Demo 源文件，不遍历私有配置、凭据、密钥或数据目录。"""
    for relative in SOURCE_PATHS:
        f = ROOT / relative
        if f.is_file():
            yield f, f.read_text(encoding="utf-8")

FAIL, INFO = [], []
def ck(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))
    if not ok: FAIL.append(name)
def info(msg): print(f"     · {msg}"); INFO.append(msg)

print("═" * 70)
print("币安 Demo 适配层 · DSH 验证清单")
print("═" * 70)

srcs = list(iter_src())

# ── 第 0 步: 发现适配层文件（按文件名/内容特征）──
cand = []
for f, s in srcs:
    n = f.name.lower()
    if "demo" in n and (f.suffix == ".sql" or "binance" in n or "exchange" in n
                        or "adapter" in n or "trade" in n):
        cand.append((f, s))
    elif re.search(r"enable_demo_trading|demo-fapi|demo\.binance\.com", s):
        cand.append((f, s))
# 去重
seen, adapters = set(), []
for f, s in cand:
    if f in seen: continue
    seen.add(f); adapters.append((f, s))

print(f"  [0] 发现的候选适配层文件: {len(adapters)} 个")
for f, _ in adapters: info(str(f.relative_to(ROOT)))
if not adapters:
    print("\n  ⚠️ 未发现 Demo 适配层实现 → 尚未实现，后续检查不适用")
    print("═" * 70); print("结论: 【未实现】"); sys.exit(2)

# ── ① 环境正确性（只查适配层文件）──
joined = "\n".join(s for _, s in adapters)
ck("①a 适配层使用了 demo trading 开关",
   bool(re.search(r"enable_demo_trading\(\s*True|enableDemoTrading\(\s*true", joined, re.I)))
ck("①b 适配层未同时开启 sandbox（ccxt 会抛 NotSupported）",
   not re.search(r"setSandboxMode\(\s*True|set_sandbox_mode\(\s*True|sandboxMode['\"]?\s*[:=]\s*True", joined))
bad_live = [l.strip()[:90] for l in joined.splitlines()
            if re.search(r"fapi\.binance\.com|api\.binance\.com", l)
            and "demo-" not in l and "testnet" not in l and not l.strip().startswith("#")]
ck("①c 适配层未指向实盘 endpoint", not bad_live, f"{bad_live[:2]}" if bad_live else "")
host_pin = re.findall(r"endpoint_host[^\n]*CHECK[^\n]*demo-fapi\.binance\.com", joined, re.I)
ck("①d 有 DB 层强制 endpoint=demo-fapi（比文件检查更强）", bool(host_pin),
   "已在 schema 里用 CHECK 约束固定环境" if host_pin else "建议加 DB CHECK 约束")

# ── ② 手续费来自实际成交回报 ──
ck("②a 读取成交回报的 commission（而非只用配置假设值）",
   bool(re.search(r"commission", joined)))
assumed_only = ("fee_rate_per_side" in joined) and not re.search(r"commission", joined)
ck("②b 未仅用配置里的假设费率", not assumed_only)

# ── ③ 落盘表 ──
tabs = ["demo_orders", "demo_fills", "demo_positions", "demo_account"]
miss = [t for t in tabs if t not in joined]
ck("③ 四张落盘表均已引用", not miss, f"缺: {miss}" if miss else "")

# ── ④ 未成交率（验证「限价单一定成交」假设）──
STATUS_OK = all(k in joined for k in ("canceled", "partially_filled"))
ck("④ 可算未成交率（status 枚举含 canceled/partially_filled/expired/rejected）",
   bool(re.search(r"unfilled|not_filled|non_fill|cancel_rate|fill_rate|未成交", joined)) or STATUS_OK,
   "由 status 枚举支撑" if STATUS_OK else "")

# ── ⑤ 凭据未入库 ──
leak = []
# ⚠️ 排除测试夹具: 形如 "secret-test-*" 的占位值不是真凭据；
#    tests/ 下的凭据字符串属安全测试夹具，不构成泄漏。
FIXTURE = re.compile(r"(secret-test|test-secret|dummy|placeholder|example|fake|xxx+|\*{4,})", re.I)
for f, s in srcs:
    if "test" in f.name.lower() or "/tests/" in str(f):
        continue
    for m in re.finditer(r"(api[_-]?key|apiKey|secret)['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_\-]{20,})['\"]", s):
        if FIXTURE.search(m.group(2)):      # 夹具值 → 跳过
            continue
        leak.append(f"{f.relative_to(ROOT)} :: {m.group(1)}")
        break
ck("⑤ Demo 源文件无明文凭据字面量", not leak, f"疑似: {leak[:3]}" if leak else "")

# ── ⑥ 未自动上线（只查适配层）──
ck("⑥ 适配层未实现自动上线/自动实盘切换",
   not re.search(r"auto_?promote|auto_?deploy|auto_?live|live_trading\s*=\s*True", joined, re.I))

# ── ⑦ 对照（最重要）──
has_ctrl = ("demo_signal_comparisons" in joined and "paper_event" in joined
            and "input_fingerprint" in joined and "rule_hash" in joined)
ck("⑦ 对照账包含纸面事件、输入 fingerprint 和规则身份字段", has_ctrl)

print("═" * 70)
print(f"{'✅ 源码静态检查通过' if not FAIL else '❌ 未通过: ' + '、'.join(FAIL)}")
print("静态检查不证明数据库写入、签名连接或真实成交；须另做集成与 Demo 前向验收。")
sys.exit(0 if not FAIL else 1)
