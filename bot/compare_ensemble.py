#!/usr/bin/env python3
"""
状态：旧集成结论入口已禁用，历史实现仅留档，导入不读取日志或输出结论。

对比「段内秩平均的集成分」与「逐种子分」—— 集成到底帮忙还是拖后腿？

为什么问这个：第 24 轮引入多种子时假设"秩平均降方差、只会更好"。
但第 36 轮修复逐种子 t 后发现**逐种子普遍高于集成**，与直觉相反。
如果集成系统性地压低信号，那多花 3 倍算力换来的可能是更差的分数。

数据来源：
  · 集成分 t  → 账本 research_trials.jsonl 的 t_period
  · 逐种子 t  → logs/verify_top.log（5 种子；前 3 个与主流程的 [42,79,116] 重合）

用法: python compare_ensemble.py
"""
import json, os, re, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(_HERE, 'user_data', 'research_trials.jsonl')
VLOG = os.path.join(_HERE, '..', 'logs', 'verify_top.log')

MAIN_SEEDS = [42, 79, 116]          # auto_research.py 用的：42 + si*37

def _legacy_main():
    raise SystemExit(
        "LEGACY_DISABLED: compare_ensemble.py 已禁用。旧账本/verify 日志含污染口径，"
        "匹配键还遗漏完整超参数与评估版本；不能据此宣称集成或种子稳健性。"
        "历史源码与记录保留；证据口径见 docs/DEVELOPMENT_LEDGER.md。"
    )
    # Historical implementation is preserved below, never executed.
    # ── 账本里的集成分 t ──
    cur = {}
    for line in open(LEDGER, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        cur[r.get('key')] = r

    ens = {}
    for v in cur.values():
        c = v.get('config') or {}
        if not c:
            continue
        k = (c.get('kind', 'lstm'), c.get('seq_len'), c.get('hidden'), c.get('layers'))
        t = v.get('t_quarter')
        if t is None:
            t = v.get('t_period')
        # 只收多种子记录：单种子结果与逐种子不可比
        if t is not None and int(v.get('n_seeds') or 1) >= 3:
            ens[k] = (t, v.get('n_seeds'), v.get('key'))

    # ── verify_top.log 的逐种子 t ──
    per = {}   # (L,h,ly) -> [(seed, t), ...]
    if os.path.exists(VLOG):
        lab = None
        for line in open(VLOG, encoding='utf-8'):
            m = re.match(r'\s*【(.+?)】', line)
            if m:
                mm = re.search(r'L=(\d+)\s+h=(\d+)\s+ly=(\d+)', m.group(1))
                kk = re.match(r'\s*(\w+)\s+L=', m.group(1))
                kind = kk.group(1) if kk else 'lstm'
                lab = ((kind, int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
                       if mm else None)
                if lab:
                    per.setdefault(lab, [])
                continue
            if lab is None:
                continue
            m = re.match(r'\s*(\d+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)', line)
            if m:
                per[lab].append((int(m.group(1)), float(m.group(3))))   # 第3列是 t(季)

    print(f"  账本集成分: {len(ens)} 个配置   verify_top 逐种子: {len(per)} 个配置")
    print()
    hdr = f"  {'配置':<22}{'集成t':>11}{'同种子逐t':>26}{'集成−均值':>11}{'集成−最差':>11}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    rows = []
    for k in sorted(set(ens) & set(per)):
        e, n, key = ens[k]
        ps = [t for sd, t in per[k] if sd in MAIN_SEEDS]
        if len(ps) < 3:
            continue
        mean = sum(ps) / len(ps)
        rows.append((k, e, ps, mean, min(ps)))
        lab = f"{k[0]} L={k[1]} h={k[2]} ly={k[3]}"
        print(f"  {lab:<22}{e:>11.2f}{str([round(x,2) for x in ps]):>26}"
              f"{e-mean:>11.2f}{e-min(ps):>11.2f}")

    if rows:
        d_mean = sum(r[1] - r[3] for r in rows) / len(rows)
        d_min = sum(r[1] - r[4] for r in rows) / len(rows)
        print()
        print(f"  平均（集成 − 逐种子均值）= {d_mean:+.2f}")
        print(f"  平均（集成 − 最差种子）  = {d_min:+.2f}")
        print()
        print("  解读：")
        if abs(d_mean) < 0.10:
            print(f"    · 集成 vs 逐种子均值 {d_mean:+.2f} → **基本中性**")
            print("      秩平均不是信号放大器，它不提升分数")
        elif d_mean < 0:
            print(f"    · 集成 vs 逐种子均值 {d_mean:+.2f} → 集成【低于】逐种子均值")
            print("      秩平均在压低信号（当各种子排序不一致时互相抵消）")
        else:
            print(f"    · 集成 vs 逐种子均值 {d_mean:+.2f} → 集成【高于】逐种子均值")
        if d_min > 0.10:
            print(f"    · 集成 vs 最差种子 {d_min:+.2f} → **集成确实防住了坏种子**")
            print("      这才是多种子的真正价值：**买的是稳健，不是更高的分数**")
        else:
            print(f"    · 集成 vs 最差种子 {d_min:+.2f} → 对最差种子的保护有限")
    else:
        print("  暂无可对比的配置（需要 verify_top 跑完、且种子与主流程重合）")


if __name__ == "__main__":
    _legacy_main()
