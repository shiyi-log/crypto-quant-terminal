import torch, numpy as np, pandas as pd, warnings, time
warnings.filterwarnings("ignore")
torch.set_num_threads(1)
import ml_seq
from ml_regime import ic_t

seq,meta,feats=ml_seq.build_panel(dense=True,seq_len=30,feature_set="base")
F=seq.shape[2]; dates=pd.to_datetime(meta["date"]); y=meta["label"].values
cuts=[]; t=pd.Timestamp("2021-07-01"); end=dates.max()
while t<end:
    cuts.append((t,min(t+pd.DateOffset(months=6),end+pd.Timedelta(days=1)))); t=t+pd.DateOffset(months=6)

N_SEED=15
seeds=[100+i*17 for i in range(N_SEED)]
per=[np.full(len(meta),np.nan) for _ in seeds]
t0=time.time()
for a,b in cuts:
    trm=(dates<a).values; tem=((dates>=a)&(dates<b)).values
    if trm.sum()<2000 or tem.sum()<100: continue
    mu=seq[trm].reshape(-1,F).mean(0); sd=seq[trm].reshape(-1,F).std(0)+1e-6
    Xtr=np.nan_to_num(((seq[trm]-mu)/sd).astype(np.float32))
    Xte=np.nan_to_num(((seq[tem]-mu)/sd).astype(np.float32))
    for i,s in enumerate(seeds):
        m=ml_seq.train_seq(Xtr,y[trm],F,"lstm","mps",epochs=8,hidden=64,layers=1,dropout=0.3,seed=s)
        per[i][tem]=ml_seq.predict_seq(m,Xte,"mps")
    print(f"  段 {str(a.date())} 完成 ({time.time()-t0:.0f}s)", flush=True)

def pim(score):
    ics=[]
    dts=pd.date_range(pd.Timestamp("2021-07-01"),dates.max()-pd.DateOffset(months=6),freq="MS")
    for a in dts:
        b=a+pd.DateOffset(months=6)
        m=((dates>=a)&(dates<b)).values & np.isfinite(score)
        if m.sum()<150: continue
        ic,_=ic_t(score[m],meta["ret"].values[m])
        if np.isfinite(ic): ics.append(ic)
    arr=np.array(ics)
    tv=arr.mean()/(arr.std(ddof=1)/np.sqrt(len(arr))) if arr.std(ddof=1)>0 else np.nan
    return arr.mean(),tv,int((arr>0).sum()),len(arr)

print()
print("="*92)
print(f"种子分布研究（{N_SEED} 个种子，同架构同数据，只换随机初始化）")
print("="*92)
print(f"  {'种子':<8}{'平均IC':>10}{'t值':>8}{'正窗口':>10}{'占比':>8}")
print("  "+"-"*48)
res=[]
for i,s in enumerate(seeds):
    ok=np.isfinite(per[i])
    if ok.sum()<500: continue
    ic,tv,pos,n=pim(per[i]); res.append((s,ic,tv,pos,n))
    print(f"  {s:<8}{ic:>+10.4f}{tv:>8.2f}{str(pos)+'/'+str(n):>10}{pos/n*100:>7.0f}%")
print("  "+"-"*48)
ts=np.array([x[2] for x in res]); ps=np.array([x[3]/x[4] for x in res])
print(f"  t 值:    均值 {ts.mean():.2f} · 中位 {np.median(ts):.2f} · "
      f"范围 {ts.min():.2f}~{ts.max():.2f} · 标准差 {ts.std(ddof=1):.2f}")
print(f"  正窗口:  均值 {ps.mean()*100:.0f}% · 范围 {ps.min()*100:.0f}%~{ps.max()*100:.0f}%")
print(f"  t>2 的种子: {(ts>2).sum()}/{len(ts)}")
print(f"  95% 区间: [{np.percentile(ts,2.5):.2f}, {np.percentile(ts,97.5):.2f}]")
print()
print(f"  ⭐ 真实期望 t ≈ {ts.mean():.2f}（中位 {np.median(ts):.2f}）")
print(f"     单次实验报出的 t 是【均值 + 随机种子偏差】")
print(f"     最强种子 {ts.max():.2f} 比均值高 {ts.max()-ts.mean():.2f}")
# 集成
common=np.ones(len(meta),bool)
for p in per: common&=np.isfinite(p)
ens=np.vstack([pd.Series(p[common]).rank(pct=True).values for p in per]).mean(0)
sub=meta[common].copy()
full=np.full(len(meta),np.nan); full[common]=ens
ic,tv,pos,n=pim(full)
print(f"\n  全 {N_SEED} 种子集成: IC {ic:+.4f} · t={tv:.2f} · 正窗口 {pos}/{n} ({pos/n*100:.0f}%)")
print(f"  → 集成 t={tv:.2f} vs 单模型均值 {ts.mean():.2f} vs 最强单模型 {ts.max():.2f}")
print(f"  → 集成最接近【真实期望】；单模型的高分是种子抽签")
