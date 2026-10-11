import os, sys, numpy as np, pandas as pd
sys.path.insert(0,'.')
from sweep import load, backtest, split
RT=0.0004
def run(label, fn, grid, H, pairs=('BTC/USDT:USDT','ETH/USDT:USDT'), tf='15m'):
    D=load(tf)
    print(f"\n  ══ {label}（{tf} · 往返 {RT*100:.2f}%）══")
    print(f"  {'参数':<24}{'内n':>7}{'内毛':>9}{'内净':>9}{'外n':>7}{'外毛':>9}{'外净':>9}{'同号':>5}")
    pos=0; tot=0
    for lbl,prm in grid:
        accA=[];accB=[]
        for p in pairs:
            if p not in D: continue
            A,B=split(D[p])
            sl,ss=fn(A,**prm); rA=backtest(A,sl,ss,H,fee=RT)
            sl,ss=fn(B,**prm); rB=backtest(B,sl,ss,H,fee=RT)
            if rA: accA.append(rA)
            if rB: accB.append(rB)
        if not accA or not accB: continue
        nA=sum(r['n'] for r in accA); nB=sum(r['n'] for r in accB)
        gA=sum(r['gross']*r['n'] for r in accA)/nA; gB=sum(r['gross']*r['n'] for r in accB)/nB
        ok=gA>RT and gB>RT; pos+=ok; tot+=1
        print(f"  {lbl:<24}{nA:>7}{gA*100:>+8.4f}%{(gA-RT)*100:>+8.4f}%{nB:>7}{gB*100:>+8.4f}%{(gB-RT)*100:>+8.4f}%{'✅' if ok else '  ':>5}")
    print(f"  → 内外皆正净: {pos}/{tot}")

def boll(df,N=32,Z=2.0):
    c=df['c']; ma=c.rolling(N).mean().shift(1); sd=c.rolling(N).std().shift(1); z=(c-ma)/sd
    return (z<=-Z).fillna(False).values,(z>=Z).fillna(False).values
def tsmom(df,L=96):
    c=df['c']; r=np.sign((c/c.shift(L)-1).fillna(0).values)
    return (r>0),(r<0)
def volclimax(df,VR=10.0,MV=0.012,vw=96,mw=3):
    v=df['v']; med=v.shift(1).rolling(vw,min_periods=20).median()
    vr=(v/med).fillna(0); mv3=(df['c']/df['c'].shift(mw)-1).fillna(0)
    up=(mv3>=MV)&(vr>=VR); dn=(mv3<=-MV)&(vr>=VR)
    return dn.fillna(False).values, up.fillna(False).values   # 反转：涨→空
def wick(df,W=0.65):
    h,l,o,c=df['h'],df['l'],df['o'],df['c']; rng=(h-l).replace(0,np.nan)
    uw=(h-np.maximum(o,c))/rng; lw=(np.minimum(o,c)-l)/rng
    return ((lw>=W)&(c>o)).fillna(False).values,((uw>=W)&(c<o)).fillna(False).values
def pullback(df,mf=8,ms=96,S=16,band=0.4,aw=14):
    c=df['c']; fas=c.rolling(mf).mean().shift(1); slo=c.rolling(ms).mean().shift(1)
    tr=pd.concat([df['h']-df['l'],(df['h']-c.shift(1)).abs(),(df['l']-c.shift(1)).abs()],axis=1).max(axis=1)
    atr=tr.rolling(aw).mean().shift(1)
    trend=np.sign((slo-slo.shift(S)).fillna(0)); near=(c-fas).abs()<=band*atr
    return ((trend>0)&near).fillna(False).values,((trend<0)&near).fillna(False).values

run('s03_tsmom_vol_target',tsmom,[(f'L={L}',{'L':L}) for L in (32,96,288)],32)
run('s06_volume_climax_reversal',volclimax,[(f'VR={v} MV={m}',{'VR':v,'MV':m}) for v in (5,10,20) for m in (0.008,0.012,0.02)],8)
run('s08_wick_rejection',wick,[(f'W={W}',{'W':W}) for W in (0.5,0.6,0.7,0.8)],8)
run('s09_trend_pullback',pullback,[(f'mf={a} band={b}',{'mf':a,'band':b}) for a in (8,16) for b in (0.3,0.6,1.0)],96)
