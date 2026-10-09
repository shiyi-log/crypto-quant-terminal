#!/usr/bin/env python3
"""
状态：已禁用，仅保留历史源码；禁止以当前阈值重判和回填旧研究记录。

回填遗留的未判定记录。

为什么需要：trial 在评估后立即写账本（那时还没算 FDR），
判定结果在整轮结束后才回写。**如果轮次中途被打断**（重启守护、Ctrl-C、崩溃），
那批 trial 就永远停在 passed=None，前端「判定」列显示「—」且不报错。

用法: python backfill_judgment.py          # 回填
      python backfill_judgment.py --dry    # 只看要回填什么
"""
raise SystemExit(
    "LEGACY_DISABLED: backfill_judgment.py 已禁用。当前阈值不能重判历史 trial，"
    "旧 FDR 家族计数也不一致；未判定记录应保留未知状态。"
    "不会读取或改写账本，不会生成备份；证据口径见 docs/DEVELOPMENT_LEDGER.md。"
)

import json, os, shutil, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_research import bh_fdr, CRIT_T, CRIT_Q, CRIT_ROBUST
import numpy as np
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(_HERE, 'user_data', 'research_trials.jsonl')
PROG = os.path.join(_HERE, 'user_data', 'research_progress.json')

ap = argparse.ArgumentParser()
ap.add_argument('--dry', action='store_true', help='只看，不写')
a = ap.parse_args()

recs = [json.loads(l) for l in open(P, encoding='utf-8') if l.strip()]
cur = {}
for r in recs:
    cur[r.get('key')] = r

# 当前正在跑的 run 不算遗留（它的判定会在本轮结束时回写）
live = None
try:
    pg = json.load(open(PROG, encoding='utf-8'))
    if pg.get('status') == 'running':
        live = pg.get('run_id')
except Exception:
    pass

# 注意：不要因为 seed_ts_invalid 就跳过 —— 「逐种子字段作废」与
# 「判定缺失」是两件事，前者不影响后者需要回填。
stale = {k: v for k, v in cur.items()
         if v.get('passed') is None
         and v.get('run_id') != live}

if not stale:
    print(f"  ✅ 无遗留（当前 run={live}，其未判定属正常）")
    sys.exit(0)

byrun = defaultdict(list)
for k, v in stale.items():
    byrun[v.get('run_id')].append((k, v))

print(f"  待回填 {len(stale)} 条，分属 {len(byrun)} 个 run"
      + ("（--dry，不写盘）" if a.dry else ""))
out = []
for rid, items in byrun.items():
    same = [v for v in cur.values() if v.get('run_id') == rid]
    qs = bh_fdr([v.get('p_value') for v in same])
    qmap = {id(v): q for v, q in zip(same, qs)}
    m = len(same)
    for k, v in items:
        r = dict(v)
        r['q_value'] = qmap.get(id(v))
        r['fdr_m'] = m
        r['passed'] = bool(
            r.get('t_period') is not None and np.isfinite(r['t_period'])
            and r['t_period'] > CRIT_T and r.get('n_windows', 0) > 0
            and (r.get('robust') is True if CRIT_ROBUST else True)
            and r['q_value'] is not None and r['q_value'] < CRIT_Q)
        out.append(r)
        c = r.get('config') or {}
        print(f"    {rid} {c.get('kind')} L={c.get('seq_len')} h={c.get('hidden')} "
              f"t季={r.get('t_quarter') or 0:>5.2f} q={r['q_value']:.4f} "
              f"→ {'✅' if r['passed'] else '❌'}")

if not a.dry:
    shutil.copy(P, P + '.before-backfill')
    with open(P, 'a', encoding='utf-8') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False, default=float) + "\n")
    print(f"  ✅ 已回填 {len(out)} 条（备份 {os.path.basename(P)}.before-backfill）")
