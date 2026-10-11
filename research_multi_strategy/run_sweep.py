"""全策略参数扫描：样本内(前70%)选参 → 样本外(后30%)验证。

关键纪律：
  · 指标在【切分后各自的片段上重算】，不用全样本指标（避免前视）
  · 毛优势为正才值得继续；毛≈0 或为负 → 机制无边际，调参无意义
  · 成本 = 实测 maker 0.02%/边 = 往返 0.04%
"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0,'.')
from sweep import load, backtest, split

TF='15m'; RT=0.0004
D=load(TF)
FW=lambda d: {k:v for k,v in d.items()}   # 全池

def bt(df, sl, ss, H):
    return backtest(df, sl, ss, H, fee=RT)

def eval_cfg(df, fn, H):
    """fn(df) -> (long_sig, short_sig)"""
    sl,ss=fn(df)
    return bt(df, sl, ss, H)

def report(name, grid, fn, H, tf=TF):
    print(f"\n  ══ {name}（{tf} · 往返成本 {RT*100:.2f}%）══")
    print(f"  {'参数':<26}{'样本内n':>8}{'内毛':>9}{'内净':>9}{'外n':>7}{'外毛':>9}{'外净':>9}{'符号':>5}")
    rows=[]
    for lbl,params in grid:
        resA=[]; resB=[]
        for pair,df in D.items():
            A,B=split(df)
            f=lambda d: fn(d,**params)
            rA=eval_cfg(A,f,H); rB=eval_cfg(B,f,H)
            # 逐笔加权：用 n 与均值合成
            if rA: resA.append(rA)
            if rB: resB.append(rB)
        if not resA or not resB: continue
        nA=sum(r['n'] for r in resA); nB=sum(r['n'] for r in resB)
        gA=sum(r['gross']*r['n'] for r in resA)/nA
        gB=sum(r['gross']*r['n'] for r in resB)/nB
        same='✅' if (gA>RT)==(gB>RT) else '❌'
        rows.append((lbl,nA,gA,gA-RT,nB,gB,gB-RT,gA>0 and gB>0))
        print(f"  {lbl:<26}{nA:>8}{gA*100:>+8.4f}%{(gA-RT)*100:>+8.4f}%{nB:>7}{gB*100:>+8.4f}%{(gB-RT)*100:>+8.4f}%{same:>5}")
    pos=[r for r in rows if r[7]]
    print(f"  → 内外皆正毛的参数组合: {len(pos)}/{len(rows)}")
    if pos:
        b=max(pos,key=lambda r:min(r[2],r[5]))
        print(f"  → 最稳组合: {b[0]}  (内毛 {b[2]*100:+.4f}% / 外毛 {b[5]*100:+.4f}%)")
    return rows

# ── s05 布林带回归 ──
def boll(df,N=32,Z=2.0):
    c=df['c']; ma=c.rolling(N).mean().shift(1); sd=c.rolling(N).std().shift(1)
    z=(c-ma)/sd
    return (z<=-Z).fillna(False).values,(z>=Z).fillna(False).values
report('s05_bollinger_fade',[(f'N={N} Z={Z}',{'N':N,'Z':Z}) for N in (20,32,48,96) for Z in (1.5,2.0,2.5,3.0)],boll,32)

# ── s01 开盘区间突破（UTC 日首 N 根）──
def orb(df,N=8):
    d=df.copy(); day=d['ts'].dt.strftime('%Y-%m-%d')
    hi=pd.Series(index=d.index,dtype=float); lo=pd.Series(index=d.index,dtype=float)
    for g,idx in d.groupby(day).groups.items():
        idx=sorted(idx)
        if len(idx)<N+1: continue
        h=d['h'].loc[idx[:N]].max(); l=d['l'].loc[idx[:N]].min()
        for i in idx[N:]: hi[i]=h; lo[i]=l
    sl=(d['c']>hi).fillna(False).values; ss=(d['c']<lo).fillna(False).values
    # 每日只取首次
    out_l=np.zeros(len(d),bool); out_s=np.zeros(len(d),bool); seen=set()
    for i,dd in enumerate(day):
        if dd in seen: continue
        if sl[i]: out_l[i]=True; seen.add(dd)
        elif ss[i]: out_s[i]=True; seen.add(dd)
    return out_l,out_s
report('s01_orb_breakout',[(f'N={N}',{'N':N}) for N in (4,8,12,24)],orb,16)

# ── s02 双推力 ──
def dual(df,N=20,k=0.8):
    pc=df['c'].shift(1); tr=pd.concat([df['h']-df['l'],(df['h']-pc).abs(),(df['l']-pc).abs()],axis=1).max(axis=1)
    rng=tr.rolling(N).mean().shift(1); up=df['o']+k*rng; dn=df['o']-k*rng
    return (df['c']>up).fillna(False).values,(df['c']<dn).fillna(False).values
report('s02_dual_thrust',[(f'N={N} k={k}',{'N':N,'k':k}) for N in (20,40) for k in (0.3,0.5,0.8,1.2)],dual,24)

# ── s10 领先滞后（BTC→ETH）──
def lead(df,R=0.004):
    e=D['ETH/USDT:USDT']; b=D['BTC/USDT:USDT']
    n=min(len(e),len(b)); e=e.iloc[:n].reset_index(drop=True); b=b.iloc[:n].reset_index(drop=True)
    rb=b['c'].pct_change()
    return (rb>=R).fillna(False).values,(rb<=-R).fillna(False).values
print("\n  ══ s10_lead_lag_btc_eth（交易 ETH，信号来自 BTC）══")
print(f"  {'参数':<16}{'样本内n':>8}{'内毛':>9}{'内净':>9}{'外n':>7}{'外毛':>9}{'外净':>9}{'符号':>5}")
for R in (0.002,0.004,0.006,0.010):
    e=D['ETH/USDT:USDT']; b=D['BTC/USDT:USDT']
    n=min(len(e),len(b)); e=e.iloc[:n].reset_index(drop=True); b=b.iloc[:n].reset_index(drop=True)
    A_e,A_b=split(e); B_e,B_b=split(b)
    def mk(ee,bb):
        rb=bb['c'].pct_change()
        return (rb>=R).fillna(False).values,(rb<=-R).fillna(False).values
    sl,ss=mk(A_e,A_b); rA=bt(A_e,sl,ss,2)
    sl,ss=mk(B_e,B_b); rB=bt(B_e,sl,ss,2)
    if not rA or not rB: continue
    same='✅' if (rA['gross']>RT)==(rB['gross']>RT) else '❌'
    print(f"  {'R='+str(R):<16}{rA['n']:>8}{rA['gross']*100:>+8.4f}%{rA['net']*100:>+8.4f}%{rB['n']:>7}{rB['gross']*100:>+8.4f}%{rB['net']*100:>+8.4f}%{same:>5}")
