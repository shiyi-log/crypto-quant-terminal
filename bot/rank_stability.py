"""自动迭代价值评估：配置排名在多种子下稳定吗？"""
import torch, numpy as np, pandas as pd, warnings, time
warnings.filterwarnings("ignore"); torch.set_num_threads(1)
import ml_seq
from ml_regime import ic_t

seq,meta,feats=ml_seq.build_panel(dense=True,seq_len=30,feature_set="base")
F=seq.shape[2]; dates=pd.to_datetime(meta["date"]); y=meta["label"].values
cuts=[]; t=pd.Timestamp("2021-07-01"); end=dates.max()
while t<end:
    cuts.append((t,min(t+pd.DateOffset(months=6),end+pd.Timedelta(days=1)))); t=t+pd.DateOffset(months=6)
passes=[]
for a,b in cuts:
    trm=(dates<a).values; tem=((dates>=a)&(dates<b)).values
    if trm.sum()<2000 or tem.sum()<100: continue
    passes.append((trm,tem))

CFGS=[("A h=64 l=1 d=0.3",dict(hidden=64,layers=1,dropout=0.3,lr=0.001)),
      ("B h=32 l=1 d=0.3",dict(hidden=32,layers=1,dropout=0.3,lr=0.001)),
      ("C h=64 l=2 d=0.3",dict(hidden=64,layers=2,dropout=0.3,lr=0.001))]
SEEDS=[11,22,33,44,55]

def pim(sc):
    ics=[]; dts=pd.date_range(pd.Timestamp("2021-07-01"),dates.max()-pd.DateOffset(months=6),freq="MS")
    for a in dts:
        b=a+pd.DateOffset(months=6)
        m=((dates>=a)&(dates<b)).values & np.isfinite(sc)
        if m.sum()<150: continue
        v,_=ic_t(sc[m],meta["ret"].values[m])
        if np.isfinite(v): ics.append(v)
    arr=np.array(ics)
    return arr.mean(), arr.mean()/(arr.std(ddof=1)/np.sqrt(len(arr))) if arr.std(ddof=1)>0 else np.nan

print("="*100); print("配置排名稳定性检验：3 个配置 × 5 个种子"); print("="*100)
res={}
for lab,c in CFGS:
    ts=[]
    for sd in SEEDS:
        sc=np.full(len(meta),np.nan)
        for trm,tem in passes:
            mu=seq[trm].reshape(-1,F).mean(0); sdv=seq[trm].reshape(-1,F).std(0)+1e-6
            Xtr=np.nan_to_num(((seq[trm]-mu)/sdv).astype(np.float32))
            Xte=np.nan_to_num(((seq[tem]-mu)/sdv).astype(np.float32))
            m=ml_seq.train_seq(Xtr,y[trm],F,"lstm","mps",epochs=8,seed=sd,**c)
            sc[tem]=ml_seq.predict_seq(m,Xte,"mps")
        _,tv=pim(sc); ts.append(tv)
        print(f"  {lab:<26} 种子{sd:<4} t={tv:.2f}", flush=True)
    res[lab]=np.array(ts)
print()
print("="*100); print("汇总"); print("="*100)
print(f"  {'配置':<26}{'t均值':>9}{'t标准差':>10}{'最小':>8}{'最大':>8}")
print("  "+"-"*62)
for lab,ts in res.items():
    print(f"  {lab:<26}{ts.mean():>9.2f}{ts.std(ddof=1):>10.2f}{ts.min():>8.2f}{ts.max():>8.2f}")
print("  "+"-"*62)
print()
print("  【每个种子下，谁能排第一】")
rank1={k:0 for k in res}
for i,sd in enumerate(SEEDS):
    order=sorted(res.items(),key=lambda kv:-kv[1][i])
    win=order[0][0]
    rank1[win]+=1
    seqs=" > ".join(f"{k.split()[0]}({v[i]:.2f})" for k,v in order)
    print(f"    种子{sd}: {seqs}   → 冠军 {win.split()[0]}")
print()
print("  冠军次数:", {k.split()[0]:v for k,v in rank1.items()})
best=max(rank1.values())
print()
if best < len(SEEDS):
    print(f"  ❌ **没有任何配置在所有种子下都最优**（最高只赢 {best}/{len(SEEDS)} 次）")
    print("     → 单种子跑出来的『最优配置』不可信")
else:
    print(f"  ✅ 有配置在全部 {len(SEEDS)} 个种子下都最优 → 配置差异真实存在")
print()
print("  若要多种子平均，成本：")
print(f"    现配置（1 种子/配置 × 12 配置）= 12 次训练")
print(f"    5 种子/配置 × 12 配置 = 60 次训练（5 倍）")
print(f"    16 种子/配置 × 12 配置 = 192 次训练（16 倍）")
