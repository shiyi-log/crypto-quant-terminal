import torch, numpy as np, pandas as pd, warnings, time
warnings.filterwarnings("ignore")
torch.set_num_threads(1)
import ml_seq
from ml_regime import ic_t

def walk(fs, L=30, kind="lstm", epochs=8, hidden=64):
    seq,meta,feats=ml_seq.build_panel(dense=True,seq_len=L,feature_set=fs)
    F=seq.shape[2]; dates=pd.to_datetime(meta["date"]); y=meta["label"].values
    cuts=[]; t=pd.Timestamp("2021-07-01"); end=dates.max()
    while t<end:
        cuts.append((t,min(t+pd.DateOffset(months=6),end+pd.Timedelta(days=1)))); t=t+pd.DateOffset(months=6)
    sc=np.full(len(meta),np.nan)
    for a,b in cuts:
        trm=(dates<a).values; tem=((dates>=a)&(dates<b)).values
        if trm.sum()<2000 or tem.sum()<100: continue
        mu=seq[trm].reshape(-1,F).mean(0); sd=seq[trm].reshape(-1,F).std(0)+1e-6
        Xtr=np.nan_to_num(((seq[trm]-mu)/sd).astype(np.float32))
        Xte=np.nan_to_num(((seq[tem]-mu)/sd).astype(np.float32))
        m=ml_seq.train_seq(Xtr,y[trm],F,kind,"mps",epochs=epochs,hidden=hidden)
        sc[tem]=ml_seq.predict_seq(m,Xte,"mps")
    # 移动 6 个月窗口
    ics=[]
    dts=pd.date_range(pd.Timestamp("2021-07-01"),dates.max()-pd.DateOffset(months=6),freq="MS")
    for a in dts:
        b=a+pd.DateOffset(months=6)
        m=((dates>=a)&(dates<b)).values & (~np.isnan(sc))
        if m.sum()<150: continue
        ic,_=ic_t(sc[m],meta["ret"].values[m])
        if np.isfinite(ic): ics.append(ic)
    arr=np.array(ics)
    tv=arr.mean()/(arr.std(ddof=1)/np.sqrt(len(arr))) if len(arr)>2 and arr.std(ddof=1)>0 else np.nan
    return len(feats), arr.mean(), tv, int((arr>0).sum()), len(arr), sc, meta, dates

print("="*98)
print("特征集对照实验（LSTM · 序列 30 天 · 稠密采样）")
print("="*98)
print(f"  {'特征集':<10}{'特征数':>8}{'平均IC':>10}{'t值':>8}{'正窗口':>10}{'占比':>8}   耗时")
print("  "+"-"*72)
res={}
for fs,lab in [("base","基线"),("xs","+横截面"),("ext","+外部数据"),("full","全部")]:
    t0=time.time()
    nf,ic,tv,pos,n,sc,meta,dates=walk(fs)
    res[fs]=(ic,tv,pos,n,sc,meta,dates,nf)
    print(f"  {lab:<10}{nf:>8}{ic:>+10.4f}{tv:>8.2f}{str(pos)+'/'+str(n):>10}{pos/n*100:>7.0f}%   ({time.time()-t0:.0f}s)")
print("  "+"-"*72)
print()
b=res["base"]; f=res["full"]
print(f"  基线 t={b[1]:.2f}  →  全部特征 t={f[1]:.2f}   变化 {(f[1]/b[1]-1)*100:+.0f}%")
print(f"  基线 IC={b[0]:+.4f}  →  全部特征 IC={f[0]:+.4f}   变化 {(f[0]/b[0]-1)*100:+.0f}%")
print()
best=max(res.items(),key=lambda kv:kv[1][1])
print(f"  最优特征集: {best[0]}  t={best[1][1]:.2f}  IC={best[1][0]:+.4f}  正窗口 {best[1][2]}/{best[1][3]}")
# 保存预测供后续用
for fs in res:
    np.save(f"/tmp/featcmp_{fs}_sc.npy",res[fs][4])
    res[fs][5].to_pickle(f"/tmp/featcmp_{fs}_meta.pkl")
print("  ✅ 各特征集的预测已保存到 /tmp/featcmp_*.npy")
