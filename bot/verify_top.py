"""
状态：历史多种子训练入口已禁用；仅保留可导入的格式/诊断工具与历史源码。

验证配置在多种子下是否站得住。

用法:
  python verify_top.py                    # 默认验证硬编码的 4 个配置
  python verify_top.py --from-ledger      # 从账本取「通过判据」的全部配置
  python verify_top.py --key 18f699ac     # 只验证指定 key
  python verify_top.py --seeds 42,79,116  # 指定种子

为什么需要：规则 13 说种子方差占总方差 87%，
单次（或少数种子）的 t 值不足以支撑架构结论。
上线前必须知道「这个配置在多少个种子下站得住」。
"""
if __name__ == "__main__":
    raise SystemExit(
        "LEGACY_DISABLED: verify_top.py 旧训练未按 t1 净化，多种子训练保持暂停；"
        "不读取面板、不训练、不产生新的通过结论。"
    )

import numpy as np, pandas as pd, time, json, os, sys, argparse

DEFAULT_CFGS=[("L=45 h=64 ly=2", dict(kind="lstm",seq_len=45, hidden=64, layers=2, dropout=0.3, lr=0.001)),
      ("L=30 h=128 ly=2",dict(kind="lstm",seq_len=30, hidden=128,layers=2, dropout=0.3, lr=0.001)),
      ("L=30 h=64 ly=1", dict(kind="lstm",seq_len=30, hidden=64, layers=1, dropout=0.3, lr=0.001)),
      ("gru L=30 h=128 ly=2",dict(kind="gru",seq_len=30,hidden=128,layers=2,dropout=0.3,lr=0.001)),
      ("L=30 h=32 ly=2", dict(kind="lstm",seq_len=30, hidden=32, layers=2, dropout=0.3, lr=0.001))]
DEFAULT_SEEDS=[42,79,116,153,190]

def label_of(c):
    return (f"{c.get('kind','lstm')} L={c.get('seq_len')} h={c.get('hidden')} "
            f"ly={c.get('layers')} dp={c.get('dropout')}")

def cfgs_from_ledger(only_key=None):
    """从账本取通过判据的配置（后写覆盖语义）"""
    f=os.path.join(os.path.dirname(os.path.abspath(__file__)),'user_data','research_trials.jsonl')
    cur={}
    for line in open(f, encoding='utf-8'):
        line=line.strip()
        if not line: continue
        try: r=json.loads(line)
        except Exception: continue
        cur[r.get('key')]=r
    out=[]
    for k,r in cur.items():
        if not r.get('passed'): continue
        if only_key and k!=only_key: continue
        c=dict(r.get('config') or {})
        if not c: continue
        out.append((f"{label_of(c)} [{k}]", c))
    return sorted(out, key=lambda x: x[0])

_TRAIN_EPOCHS = 8
_TRAIN_BS = 512

cache={}
def panel(L):
    import ml_seq
    if L not in cache: cache[L]=ml_seq.build_panel(dense=True,seq_len=L,feature_set="base")
    return cache[L]

def run(cfg,seeds):
    raise RuntimeError(
        "LEGACY_DISABLED: verify_top.run 未按 t1 净化，禁止通过 import 恢复旧训练。"
    )
    import ml_seq
    L=int(cfg["seq_len"]); seq,meta,feats=panel(L)
    F=seq.shape[2]; dates=pd.to_datetime(meta["date"]); y=meta["label"].values
    cuts=[]; t=pd.Timestamp("2021-07-01"); end=dates.max()
    while t<end:
        cuts.append((t,min(t+pd.DateOffset(months=6),end+pd.Timedelta(days=1)))); t=t+pd.DateOffset(months=6)
    per=[np.full(len(meta),np.nan) for _ in seeds]
    for a,b in cuts:
        trm=(dates<a).values; tem=((dates>=a)&(dates<b)).values
        if trm.sum()<2000 or tem.sum()<100: continue
        mu=seq[trm].reshape(-1,F).mean(0); sd=seq[trm].reshape(-1,F).std(0)+1e-6
        Xtr=seq[trm].astype(np.float32); Xtr-=mu; Xtr/=sd; np.nan_to_num(Xtr,copy=False)
        Xte=seq[tem].astype(np.float32); Xte-=mu; Xte/=sd; np.nan_to_num(Xte,copy=False)
        for i,sd_ in enumerate(seeds):
            m=ml_seq.train_seq(Xtr,y[trm],F,cfg.get("kind","lstm"),"mps",
                               epochs=_TRAIN_EPOCHS, bs=_TRAIN_BS,
                               hidden=int(cfg["hidden"]),layers=int(cfg["layers"]),
                               dropout=float(cfg["dropout"]),lr=float(cfg["lr"]),seed=sd_)
            per[i][tem]=ml_seq.predict_seq(m,Xte,"mps"); del m
        del Xtr,Xte
    return meta,per

def ev(meta,score):
    from ml_regime import ic_t
    def one(freq):
        d=meta.copy(); d["p"]=score
        per=pd.to_datetime(d["date"]).dt.to_period(freq).astype(str); ics=[]
        for _,g in d.groupby(per.values):
            if len(g)<100: continue
            ic,_=ic_t(g["p"].values,g["ret"].values)
            if np.isfinite(ic): ics.append(ic)
        if len(ics)<4: return None
        a=np.array(ics)
        return a.mean(), (a.mean()/(a.std(ddof=1)/np.sqrt(len(a))) if a.std(ddof=1)>0 else np.nan)
    out={}
    for f,lab in [("M","月"),("Q","季"),("2Q","半")]:
        r=one(f)
        if r: out[lab]=r
    return out

def _legacy_main():
    raise SystemExit(
        "LEGACY_DISABLED: verify_top.py 多种子训练入口已禁用。旧训练未按 t1 净化，"
        "旧 pass 与稳健性结论不可继续使用；工具函数可导入，历史代码保留。"
    )
    # Historical parser/report implementation is retained for audit.
    ap=argparse.ArgumentParser()
    ap.add_argument('--from-ledger', action='store_true', help='从账本取通过判据的配置')
    ap.add_argument('--key', default=None, help='只验证指定 key')
    ap.add_argument('--seeds', default=None, help='逗号分隔的种子')
    ap.add_argument('--list', action='store_true', help='只列出将验证的配置，不训练')
    ap.add_argument('--epochs', type=int, default=8,
                    help='必须与主流程一致（auto_research.py 的 --epochs，默认 8）')
    ap.add_argument('--bs', type=int, default=512,
                    help='必须与主流程一致（auto_research.py 的 --bs，默认 512）。'
                         '不一致会让验证结果与账本不可比！')
    ap.add_argument('--spec', default=None,
                    help='直接指定配置，格式 kind:seq_len:hidden:layers:dropout:lr\n'
                         '        例 --spec lstm:30:64:2:0.2:0.001\n'
                         '        用途：验证【还没判定】的新配置（--key 要求 passed=True，轮次结束前不满足）')
    a=ap.parse_args()

    # ⚠ run() 内部有局部变量 a（切分日期 Timestamp），会遮蔽这里的命名空间 a。
    #    所以训练参数必须先取出来存成模块级常量。
    _TRAIN_EPOCHS = a.epochs
    _TRAIN_BS = a.bs

    SEEDS=[int(x) for x in a.seeds.split(',')] if a.seeds else DEFAULT_SEEDS
    if a.spec:
        # 直接按配置验证 —— 不查账本，因此不受 passed 状态限制
        try:
            k, L, h, ly, dp, lr = a.spec.split(':')
            c = {"kind": k.strip(), "seq_len": int(L), "hidden": int(h),
                 "layers": int(ly), "dropout": float(dp), "lr": float(lr)}
        except Exception as e:
            print(f"  --spec 格式错误: {e}"); sys.exit(1)
        CFGS = [(f"{label_of(c)} [spec]", c)]
    elif a.from_ledger or a.key:
        CFGS=cfgs_from_ledger(a.key)
        if not CFGS:
            print("  账本里没有匹配的通过配置"
                  "（--key 要求 passed=True；轮次结束前新配置还没判定，请用 --spec）")
            sys.exit(1)
    else:
        CFGS=DEFAULT_CFGS

    if a.list:
        print(f"  将验证 {len(CFGS)} 个配置 × {len(SEEDS)} 个种子:")
        for lab,_ in CFGS:
            print(f"    {lab}")
        sys.exit(0)

    print("="*104)
    print(f"验证：{len(CFGS)} 个配置在 {len(SEEDS)} 个种子下是否站得住？")
    print("="*104)
    for lab,cfg in CFGS:
        t0=time.time()
        meta,per=run(cfg,SEEDS)
        print(f"\n  【{lab}】")
        print(f"    {'种子':<8}{'t(月)':>9}{'t(季)':>9}{'t(半年)':>10}  稳健")
        print("    "+"-"*44)
        allok=[]
        for i,sd_ in enumerate(SEEDS):
            ok=np.isfinite(per[i])
            if ok.sum()<500: continue
            r=ev(meta[ok].reset_index(drop=True), per[i][ok])
            tm=r.get('月',(np.nan,)*2)[1]; tq=r.get('季',(np.nan,)*2)[1]; th=r.get('半',(np.nan,)*2)[1]
            good = all(np.isfinite(x) and x>2 for x in [tm,tq,th])
            allok.append(good)
            print(f"    {sd_:<8}{tm:>9.2f}{tq:>9.2f}{th:>10.2f}  {'✅' if good else '❌'}")
        print("    "+"-"*44)
        print(f"    通过 {sum(allok)}/{len(allok)} 个种子 · 耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    _legacy_main()
