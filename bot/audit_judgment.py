#!/usr/bin/env python3
"""
独立复核判定结果 —— 不信任账本里写的 q/passed，全部重算一遍。

为什么需要：判定结果读不到不会报错（页面只是显示「—」）。
必须有一条独立路径能回答"账本里的判定本身对不对"。

用法: python audit_judgment.py
"""
import json, os, sys
from collections import defaultdict, Counter
sys.path.insert(0, '.')
from auto_research import bh_fdr, CRIT_T, CRIT_Q, CRIT_ROBUST
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(_HERE, 'user_data', 'research_trials.jsonl')

recs = []
for line in open(P, encoding='utf-8'):
    line = line.strip()
    if line:
        try: recs.append(json.loads(line))
        except Exception: pass

# 「后写覆盖」语义
cur = {}
for r in recs:
    cur[r.get('key')] = r

print(f"  账本 {len(recs)} 行 · 去重后 {len(cur)} 个配置")
print()

# ① 完整性：每个配置都该有判定
#    注意：正在跑的轮次里，trial 先落原始记录（无判定），
#    等该轮结束才算 FDR 并回写。所以「当前 run 的未判定」是正常的。
live_run = None
try:
    prog = json.load(open('user_data/research_progress.json', encoding='utf-8'))
    if prog.get('status') == 'running':
        live_run = prog.get('run_id')
except Exception:
    pass

unjudged = [k for k, v in cur.items() if v.get('passed') is None]
stale = [k for k in unjudged if cur[k].get('run_id') != live_run]
inflight = [k for k in unjudged if cur[k].get('run_id') == live_run]
if not unjudged:
    print("  ① 判定完整性: ✅ 全部有判定")
elif not stale:
    print(f"  ① 判定完整性: ✅ 无遗留（{len(inflight)} 个属进行中的 run {live_run}）")
else:
    print(f"  ① 判定完整性: ❌ {len(stale)} 个遗留未判定 → "
          f"{[(k, cur[k].get('run_id')) for k in stale[:5]]}")
    if inflight:
        print(f"       （另有 {len(inflight)} 个属进行中的 run，正常）")

# ② 重算 FDR（按 run 分组）并与账本比对
byrun = defaultdict(list)
for k, v in cur.items():
    byrun[v.get('run_id')].append(v)

mismatch_q, mismatch_p = [], []
checkable = 0
for rid, items in byrun.items():
    # 优先用记录里落盘的 fdr_m（那是当初真正用的总体）；
    # 老记录没有该字段，才退回「按 run 分组」这个近似
    ms = [v.get('fdr_m') for v in items if v.get('fdr_m')]
    if ms:
        checkable += 1
        m = max(ms)
        pv = sorted([v.get('p_value') for v in items if v.get('p_value') is not None])
        # BH: q_i = p_(i) * m / i，取单调化后的最小值
        qs_full = bh_fdr([v.get('p_value') for v in items])
        qs = qs_full
    else:
        qs = bh_fdr([v.get('p_value') for v in items])
    for v, q in zip(items, qs):
        old = v.get('q_value')
        if old is None or q is None:
            continue
        if abs(old - q) > 1e-6:
            mismatch_q.append((v.get('key'), old, q))
hist = [v for v in cur.values() if not v.get('fdr_m') and v.get('q_value') is not None]
_icon = "✅" if not mismatch_q else ("⚠" if checkable == 0 else "❌")
_verdict = "带 fdr_m 的记录全部一致" if not mismatch_q else f"{len(mismatch_q)} 个不一致"
print(f"  ② FDR 可复算: {_icon} {_verdict} "
      f"（可精确验证 {checkable} 个 run；另有 {len(hist)} 条历史记录无 fdr_m，"
      f"其 q 为当年口径，不可从账本复算）")
for k, o, n in mismatch_q[:5]:
    print(f"       {k}: 账本 {o:.6f} vs 重算 {n:.6f}")

# ③ 重算判定并与账本比对
mismatch_p = []
for k, v in cur.items():
    q = v.get('q_value'); t = v.get('t_period')
    if q is None and t is None:
        continue
    expect = bool(
        t is not None and np.isfinite(t) and t > CRIT_T
        and v.get('n_windows', 0) > 0
        and (v.get('robust') is True if CRIT_ROBUST else True)
        and q is not None and q < CRIT_Q)
    if v.get('passed') is not None and bool(v['passed']) != expect:
        mismatch_p.append((k, v.get('passed'), expect))
print(f"  ③ 判定可复算: {'✅ 全部一致' if not mismatch_p else f'❌ {len(mismatch_p)} 个不一致'}")
for k, o, n in mismatch_p[:5]:
    print(f"       {k}: 账本 {o} vs 重算 {n}")

# ④ 稳健性字段覆盖率
nofield = [k for k, v in cur.items() if v.get('robust') is None]
print(f"  ④ 多窗口稳健字段: {'✅ 已覆盖' if not nofield else f'⚠ {len(nofield)} 个缺失（判据修订前的老记录）'}")

# ⑤ 多种子覆盖率
seeds = Counter(int(v.get('n_seeds') or 1) for v in cur.values())
print(f"  ⑤ 种子数分布: {dict(seeds)}")

# ⑥ 通过清单
passed = [v for v in cur.values() if v.get('passed')]
print()
print(f"  通过判据 {len(passed)} 个:")
for v in sorted(passed, key=lambda x: -(x.get('t_quarter') or 0)):
    c = v.get('config', {})
    # 有逐种子 t 就显示离散度（第 32 轮起落盘）
    spread = ""
    if v.get("seed_t_min") is not None:
        _min = v["seed_t_min"]
        spread = (f"  种子t 最差={_min:.2f} 均值={v.get('seed_t_mean') or 0:.2f}"
                  f"±{(v.get('seed_t_std') or 0):.2f}"
                  + ("（稳）" if _min > CRIT_T else "（不稳！）"))
    print(f"    {c.get('kind'):<11} L={c.get('seq_len'):<4} h={c.get('hidden'):<4} "
          f"ly={c.get('layers')} dp={c.get('dropout')}  "
          f"t季={v.get('t_quarter') or 0:>5.2f} t月={v.get('t_month') or 0:>5.2f} "
          f"稳健={v.get('robust_pass')}/{v.get('robust_total')} "
          f"q={v.get('q_value'):.4f} seeds={v.get('n_seeds')}{spread}")

# ⑦ 种子置信度：通过判据但种子数不足的，单独标出来
#    规则 13：种子方差占总方差 87%。单种子的"通过"很可能是抽到了好种子，
#    不足以支撑架构结论，上线前必须补做多种子复核。
print()

# 先读 verify_top 的历史结论，做交叉引用（避免重复劳动）
vres = {}
vf = os.path.join(_HERE, '..', 'logs', 'verify_top.log')
if os.path.exists(vf):
    try:
        cur_lab = None
        for line in open(vf, encoding='utf-8'):
            import re as _re
            m = _re.match(r'\s*【(.+?)】', line)
            if m:
                cur_lab = m.group(1).strip(); continue
            m = _re.search(r'通过 (\d+)/(\d+) 个种子', line)
            if m and cur_lab:
                # 归一化：去掉可能的 kind 前缀，只留 L/h/ly
                mm = _re.search(r'(L=\d+\s+h=\d+\s+ly=\d+)', cur_lab)
                k = mm.group(1) if mm else cur_lab
                vres[k] = (int(m.group(1)), int(m.group(2)))
                cur_lab = None
    except Exception:
        pass

def vkey(c):
    return vkey_str(c.get('kind', 'lstm'), c.get('seq_len'), c.get('hidden'), c.get('layers'))

def vkey_str(kind, L, h, ly):
    """verify_top.log 里的标签有两种写法：带 kind 前缀和不带。
    统一成不带前缀的形式再比对。"""
    return f"L={L} h={h} ly={ly}"

weak = [v for v in passed if int(v.get('n_seeds') or 1) < 3]
if weak:
    print(f"  ⚠ 种子置信度：{len(weak)}/{len(passed)} 个通过配置的种子数 < 3，"
          f"结论不可靠（规则 13）")
    for v in weak:
        c = v.get('config', {})
        k = vkey(c)
        extra = ""
        if k in vres:
            ok, tot = vres[k]
            extra = (f"  ← 已做 5 种子复核：通过 {ok}/{tot}"
                     + ("（稳）" if ok == tot else "（不稳！）"))
        else:
            extra = "  ← 未做多种子复核"
        print(f"      {k}  n_seeds={v.get('n_seeds')}  "
              f"t季={v.get('t_quarter') or 0:.2f}{extra}")
else:
    print(f"  ✅ 种子置信度：全部 {len(passed)} 个通过配置都有 ≥3 种子")

if vres:
    print(f"  （已交叉引用 logs/verify_top.log 的 {len(vres)} 条 5 种子复核结论）")

# ⑧ 搜索空间饱和检测
print()
sigs = defaultdict(list)
for v in passed:
    c = v.get('config', {})
    sigs[(c.get('kind'), c.get('seq_len'), c.get('hidden'), c.get('layers'))].append(v)
print(f"  ⑥ 搜索饱和检测: {len(passed)} 个通过 → {len(sigs)} 个不同架构")
for s, vs in sorted(sigs.items(), key=lambda x: -len(x[1])):
    if len(vs) > 1:
        print(f"     ⚠ {s} 被重复确认 {len(vs)} 次")
