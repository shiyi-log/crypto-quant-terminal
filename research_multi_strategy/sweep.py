"""参数扫描引擎（研究侧，不碰执行层）。

原则（用户 2026-10-11 指令：一切以数据说话）：
  · 每个参数取值都用真实 K 线回放，不用推断
  · 样本内(前70%)选参，样本外(后30%)验证，禁止只用全样本
  · 报告毛/净/笔数/胜率，成本用实测 maker 0.02%/边
  · 成交：信号在 t 收盘成立 → t+1 开盘成交；离场同理
  · 离场优先级：1) 反向信号  2) 持有上限 H（保险丝）
"""
import os, psycopg, pandas as pd, numpy as np

MAKER=0.0002; RT=MAKER*2

def load(tf):
    url=os.environ['QUANT_DATABASE_URL']
    with psycopg.connect(url) as conn:
        out={}
        for p in ('BTC/USDT:USDT','ETH/USDT:USDT'):
            r=conn.execute("""select timestamp,open,high,low,close,volume from quant.candles
                where pair=%s and timeframe=%s and candle_type='futures' and exchange='binance'
                order by timestamp""",(p,tf)).fetchall()
            if not r: continue
            df=pd.DataFrame(r,columns=['ts','o','h','l','c','v'])
            for k in ('o','h','l','c','v'): df[k]=df[k].astype(float)
            df['ts']=pd.to_datetime(df['ts'],unit='ms',utc=True)
            out[p]=df.reset_index(drop=True)
        return out

def backtest(df, sig_long, sig_short, H, fee=RT):
    """sig_* 为 bool 数组：在 t 收盘成立。t+1 开盘成交。"""
    o=df['o'].values; n=min(len(df), len(sig_long), len(sig_short))
    sig_long=np.asarray(sig_long)[:n]; sig_short=np.asarray(sig_short)[:n]
    pos=0; entry=0.0; entry_i=0; trades=[]
    for t in range(1,n-1):
        if pos==0:
            if sig_long[t]:
                pos=1; entry=o[t+1]; entry_i=t+1
            elif sig_short[t]:
                pos=-1; entry=o[t+1]; entry_i=t+1
        else:
            rev = (pos==1 and sig_short[t]) or (pos==-1 and sig_long[t])
            timeout = (t-entry_i)>=H
            if rev or timeout:
                ex=o[t+1]
                gross=(ex-entry)/entry if pos==1 else (entry-ex)/entry
                trades.append((gross, 'reverse' if rev else 'timeout'))
                pos=0
    if pos!=0:
        ex=o[-1]
        gross=(ex-entry)/entry if pos==1 else (entry-ex)/entry
        trades.append((gross,'eod'))
    if not trades: return None
    g=np.array([x[0] for x in trades])
    return {'n':len(g),'gross':g.mean(),'net':g.mean()-fee,
            'win':(g>fee).mean(),'rev_share':sum(1 for x in trades if x[1]=='reverse')/len(g)}

def split(df, frac=0.7):
    k=int(len(df)*frac); return df.iloc[:k].reset_index(drop=True), df.iloc[k:].reset_index(drop=True)
