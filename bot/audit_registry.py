#!/usr/bin/env python3
"""
注册表健康检查 —— 冠军是否落后于证据？

为什么需要：promote 是人工闸门，但没人会主动去查
「现在挂着的冠军，按今天的判据还过不过」。
判据改过之后，老冠军就会静默地留在原地。

用法: python audit_registry.py   （非阻断，只报告）
"""
import json, os, sys

# 自定位：无论从项目根还是 bot/ 运行都能找到
_HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(_HERE, 'user_data', 'model_versions.json')
try:
    d = json.load(open(P, encoding='utf-8'))
except Exception as e:
    print(f"  ❌ 读取注册表失败: {e}"); sys.exit(1)

vers = d.get('versions', [])
champ = d.get('champions', {})

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

ml = [v for v in vers if v.get('layer') == 'ml_model']
n_chal = sum(1 for v in ml if v.get('status') == 'challenger')
n_note = sum(1 for v in vers if v.get('note'))
print(f"  ── 堆积 ──")
print(f"    ml_model 挑战者 {n_chal} 个（未清理）")
print(f"    带人工上线理由的版本 {n_note}/{len(vers)}")

if problems:
    print("  ── ⚠ 问题 ──")
    for p in problems:
        print(f"    {p}")
    sys.exit(2)
print("  ✅ 注册表一致")
