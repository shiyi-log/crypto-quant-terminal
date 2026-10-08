#!/usr/bin/env python3
"""
全策略变体的干净口径重评（第 22 轮）

背景：第 21 轮发现临时引擎在高换手时有跨时点重复计价缺陷
      （V 的真实 Calmar 是 0.14 而非 1.74）。
      本脚本用【纯权重记法】重评所有变体，建立可信基线。

纯权重记法（无黑箱）：
    W_t   = 目标权重（当年信号 → 等权分总敞口）
    W     = W.shift(1)                  # T+1 开盘生效
    gross = (W.shift(1) * R).sum()      # 前一日权重 × 当日收益
    cost  = |ΔW|.sum() * 单边费率
    net   = gross - cost
"""
import pandas as pd, numpy as np, warnings, glob, os, json, itertools
warnings.filterwarnings("ignore")
import event_backtest as E

COST=0.0005; EXPO=0.30; START="2021-07-01"

data=E.load_ohlc([os.path.basename(f).split("_")[0] for f in glob.glob(f"{E.PERP}/*-1d-futures.feather")])
C=pd.DataFrame({s:d["close"] for s,d in data.items()}).sort_index()
O=pd.DataFrame({s:d["open"] for s,d in data.items()}).sort_index()
mask=C.index>=START
C=C.loc[mask].ffill(); O=O.loc[mask].ffill(); R=C.pct_change()

def met(net):
    eq=(1+net).cumprod(); yrs=len(net)/365
    if yrs<=0 or eq.iloc[-1]<=0: return None
    ann=eq.iloc[-1]**(1/yrs)-1; dd=((eq/eq.cummax())-1).min()
    sd=net.std()
    return dict(ann=ann*100,dd=dd*100,
                calmar=ann/abs(dd) if dd<0 else np.nan,
                sharpe=net.mean()/sd*np.sqrt(365) if sd>0 else np.nan)

def clean_bt(W, cost=COST):
    """标准记法：信号 t → 持仓 t+1 生效 → t+1 日赚 R[t+1]（单次 shift）"""
    W=W.shift(1).fillna(0.0)
    gross=(W*R[W.columns]).sum(axis=1)
    dW=W.diff().abs().sum(axis=1).fillna(0.0)
    return gross-dW*cost, dW

def equal_w(S, top_n=None, strength=None):
    """信号 → 等权权重。strength 用于 top_n 排序（缺省用 |S|，但会平局）"""
    if top_n:
        st = strength if strength is not None else S.abs()
        st = st.reindex(index=S.index, columns=S.columns)
        keep = st.rank(axis=1, ascending=False) <= top_n
        S = S.where(keep, 0.0)
    nz=(S!=0).sum(axis=1).replace(0,np.nan)
    return (S.div(nz,axis=0)*EXPO).fillna(0.0)

# ── 各策略信号 ──
S={}
for s,d in data.items():
    dd=d[d.index>=START]
    if len(dd)<150: continue
    S[s]=dd
def trend(d,en=20):
    c=d["close"]; hh=c.rolling(en).max().shift(1); ll=c.rolling(en).min().shift(1)
    cv=c.values; hv,llv=hh.values,ll.values
    st=np.zeros(len(cv)); cur=0.0
    for i in range(len(cv)):
        if cur==0.0:
            if np.isfinite(hv[i]) and cv[i]>hv[i]: cur=1.0
            elif np.isfinite(llv[i]) and cv[i]<llv[i]: cur=-1.0
        else:
            if cur>0 and np.isfinite(llv[i]) and cv[i]<llv[i]: cur=0.0
            elif cur<0 and np.isfinite(hv[i]) and cv[i]>hv[i]: cur=0.0
        st[i]=cur
    return pd.Series(st,index=c.index)
def volbreak(d,n=20,k=1.5,ef=0.2):
    c=d["close"]
    tr=pd.concat([(d["high"]-d["low"]),(d["high"]-c.shift()).abs(),(d["low"]-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(n).mean(); ma=c.rolling(n).mean(); up=ma+k*atr; dn=ma-k*atr
    s=pd.Series(0.0,index=c.index); pos=0.0
    for i in range(len(c)):
        if c.iloc[i]>up.iloc[i]: pos=1.0
        elif c.iloc[i]<dn.iloc[i]: pos=-1.0
        elif i>0 and abs(c.iloc[i]-ma.iloc[i])<ef*atr.iloc[i]: pos=0.0
        s.iloc[i]=pos
    return s

print("="*104); print("全策略变体 · 干净口径（纯权重记法）"); print("="*104)
rows=[]
# 趋势跟踪（不同周期）
for en in [10,20,40,55]:
    Sx=pd.DataFrame({s:trend(d,en) for s,d in S.items()}).sort_index().reindex(C.index).fillna(0.0)
    W=equal_w(Sx,top_n=8); net,dW=clean_bt(W); m=met(net)
    rows.append(("趋势跟踪",f"entry={en}",m,dW.mean()))
# 波动突破（网格）
for n,k,ef in itertools.product([10,20,40],[1.0,1.5,2.0],[0.2,0.5]):
    Sx=pd.DataFrame({s:volbreak(d,n,k,ef) for s,d in S.items()}).sort_index().reindex(C.index).fillna(0.0)
    W=equal_w(Sx,top_n=8); net,dW=clean_bt(W); m=met(net)
    rows.append(("波动突破",f"n={n} k={k} e={ef}",m,dW.mean()))
# 横截面动量/反转
for nm,fn in [("横截面动量",lambda r,n: r.rolling(n).sum()),
              ("横截面反转",lambda r,n: -r.rolling(n).sum())]:
    for n in [5,10,20,30,60]:
        mom=fn(R,n); rank=mom.rank(axis=1,pct=True)
        s=pd.DataFrame(0.0,index=rank.index,columns=rank.columns)
        s[rank>=0.7]=1.0; s[rank<=0.3]=-1.0
        W=equal_w(s); net,dW=clean_bt(W); m=met(net)
        rows.append((nm,f"n={n}",m,dW.mean()))

print(f"\n  {'策略':<14}{'参数':<18}{'年化':>9}{'回撤':>10}{'Calmar':>9}{'夏普':>8}{'日均换手':>10}")
print("  "+"-"*80)
best={}
for nm,par,m,dW in rows:
    if not m: continue
    best.setdefault(nm,[]).append((par,m))
    print(f"  {nm:<14}{par:<18}{m['ann']:>8.1f}%{m['dd']:>9.1f}%{m['calmar']:>9.2f}"
          f"{m['sharpe']:>8.2f}{dW*100:>9.1f}%")
print("  "+"-"*80)
print()
print("  ⭐ 各类最优（按 Calmar）:")
for nm,lst in best.items():
    b=max(lst,key=lambda x:x[1]["calmar"])
    print(f"    {nm:<14} {b[0]:<18} Calmar {b[1]['calmar']:.2f} · "
          f"年化 {b[1]['ann']:.1f}% · 回撤 {b[1]['dd']:.1f}%")
print()
print("  对照 · 旧引擎（第 20 轮报的数）:")
print("    V 波动突破 n=20 k=1.5 e=0.2  旧 Calmar 1.46 → 干净 0.14")
print("    M 横截面动量 n=30            旧 Calmar 1.09 → 干净 0.77")
import json
with open("user_data/clean_eval.json","w",encoding="utf-8") as f:
    json.dump({"generated_at":pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
               "rows":[{"strategy":nm,"params":par,"ann":round(m["ann"],2),
                        "dd":round(m["dd"],2),"calmar":round(m["calmar"],3),
                        "sharpe":round(float(m["sharpe"]),3),
                        "turnover_pct":round(float(dW)*100,2)}
                       for nm,par,m,dW in rows if m]},f,ensure_ascii=False,indent=2)
print("  ✅ user_data/clean_eval.json")
