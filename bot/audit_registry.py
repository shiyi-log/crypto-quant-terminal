#!/usr/bin/env python3
"""
注册表健康检查 —— 冠军是否落后于证据？

为什么需要：promote 是人工闸门，但没人会主动去查
「现在挂着的冠军，按今天的判据还过不过」。
判据改过之后，老冠军就会静默地留在原地。

用法: python audit_registry.py   （非阻断，只报告）
"""
import json, os, re, sys

# 自定位：无论从项目根还是 bot/ 运行都能找到
_HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(_HERE, 'user_data', 'model_versions.json')
try:
    d = json.load(open(P, encoding='utf-8'))
except Exception as e:
    print(f"  ❌ 读取注册表失败: {e}"); sys.exit(1)

vers = d.get('versions', [])
champ = d.get('champions', {})

# ── verify_top.log 的 5 种子结论 ──
# 为什么注册表要看它：gate 是 3 种子判据，挡不住"运气型通过"。
# 实测：auto-r21（t季=3.33，3 种子通过）在 5 种子上只过 2/5。
_v5 = {}
_vf = os.path.join(_HERE, '..', 'logs', 'verify_top.log')
if os.path.exists(_vf):
    try:
        cur = None
        for line in open(_vf, encoding='utf-8'):
            m = re.match(r'\s*【(.+?)】', line)
            if m:
                cur = m.group(1); continue
            m = re.search(r'通过 (\d+)/(\d+) 个种子', line)
            if m and cur:
                mm = re.search(r'L=(\d+)\s+h=(\d+)\s+ly=(\d+)', cur)
                if mm:
                    kk = re.match(r'\s*(\w+)\s+L=', cur)
                    kind = kk.group(1) if kk else 'lstm'
                    _v5[(kind, int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))] = (
                        int(m.group(1)), int(m.group(2)))
                cur = None
    except Exception:
        pass


def spec5(v):
    """把版本记录映射到 5 种子结论"""
    sp = v.get('spec') or {}
    return _v5.get((sp.get('kind', 'lstm'), sp.get('seq_len'),
                    sp.get('hidden'), sp.get('layers')))

problems = []
for layer, cid in champ.items():
    c = next((v for v in vers if v.get('id') == cid), None)
    if c is None:
        problems.append(f"{layer} 冠军 {cid} 不在版本列表里")
        continue
    g = c.get('gate', {})
    if not g.get('passed'):
        n_better = sum(1 for v in vers
                       if v.get('layer') == layer
                       and v.get('gate', {}).get('passed')
                       and v.get('status') != 'champion')
        problems.append(
            f"{layer} 冠军 {cid} 未通过判据（用的是「{g.get('criteria','?')[:28]}…」）"
            f"，但有 {n_better} 个已通过的挑战者未被上线")

print("  ── 冠军指针 ──")
for layer, cid in champ.items():
    c = next((v for v in vers if v.get('id') == cid), None)
    mark = "✅" if (c or {}).get('gate', {}).get('passed') else "❌"
    print(f"    {mark} {layer:<14} {cid}")

# ── 5 种子复核：gate 通过但 5 种子不稳的，单独警告 ──
fragile = []
for v in vers:
    if v.get('layer') != 'ml_model':
        continue
    if not v.get('gate', {}).get('passed'):
        continue
    r5 = spec5(v)
    if r5 and r5[0] * 2 < r5[1]:          # 通过率 < 50%
        fragile.append((v['id'], v.get('spec', {}), r5))

if fragile:
    print("  ── ⚠ 5 种子复核失败（gate 过了但换种子就不行）──")
    for vid, sp, (ok, tot) in fragile:
        print(f"    {vid}  {sp.get('kind')} L={sp.get('seq_len')} "
              f"h={sp.get('hidden')} ly={sp.get('layers')}  → 5 种子只过 {ok}/{tot}")
    print("      （gate 是 3 种子判据；3 个种子恰好偏好时会被高估）")

ml = [v for v in vers if v.get('layer') == 'ml_model']
n_chal = sum(1 for v in ml if v.get('status') == 'challenger')
n_note = sum(1 for v in vers if v.get('note'))
print(f"  ── 堆积 ──")
print(f"    ml_model 挑战者 {n_chal} 个（未清理）")
print(f"    带人工上线理由的版本 {n_note}/{len(vers)}")

if fragile:
    problems.append(f"{len(fragile)} 个已通过 gate 的版本在 5 种子复核中通过率 <50%")

if problems:
    print("  ── ⚠ 问题 ──")
    for p in problems:
        print(f"    {p}")
    sys.exit(2)
print("  ✅ 注册表一致")
