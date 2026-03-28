#!/usr/bin/env python3
"""
MR+PBL+SLC Backtest v7.0 — Interpolation: Find exact pos_frac for CAGR≈50.8%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v6 established: pos_frac=33%→CAGR=47.23%, pos_frac=40%→CAGR=58.58% (3-sym no-MR)
Interpolated target for CAGR=50.8%: ~35%.

This script runs a fine sweep around 35% for 3-sym no-MR and 5-sym full (incl MR),
and also produces the final "declared best" simulation with full detail.

All runs: 2022-03-22 → 2026-03-21 (4.00 years), $100k, zero fees.
"""

import pandas as pd
import numpy as np
from pathlib import Path
import json, sys
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, '/sessions/exciting-epic-bell/mnt/NexusTrader')

DATA_DIR = Path('/sessions/exciting-epic-bell/mnt/NexusTrader/backtest_data')
OUT_DIR  = Path('/sessions/exciting-epic-bell/mnt/NexusTrader/reports')
OUT_DIR.mkdir(exist_ok=True)

T_START = pd.Timestamp('2022-03-22', tz='UTC')
T_END   = pd.Timestamp('2026-03-21 23:59:59', tz='UTC')
CAPITAL = 100_000.0
MAX_HEAT = 0.80
MAX_POS  = 10
MAX_SYM  = 3

SIDEWAYS=0; BULL_TREND=1; BEAR_TREND=2

def _atr(h,l,c,n=14):
    tr=pd.concat([(h-l),(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,min_periods=n,adjust=False).mean()
def _rsi(c,n=14):
    d=c.diff(); g=d.clip(lower=0).ewm(alpha=1/n,min_periods=n,adjust=False).mean()
    lo=(-d).clip(lower=0).ewm(alpha=1/n,min_periods=n,adjust=False).mean()
    return 100-100/(1+g/lo.replace(0,np.nan))
def _ema(c,n): return c.ewm(span=n,adjust=False).mean()
def _bband(c,n=20,k=2.0):
    m=c.rolling(n).mean(); s=c.rolling(n).std(ddof=1); return m+k*s,m,m-k*s
def _adx(h,l,c,n=14):
    up=h.diff(); dn=-l.diff()
    pdm=pd.Series(np.where((up>dn)&(up>0),up,0.0),index=h.index)
    mdm=pd.Series(np.where((dn>up)&(dn>0),dn,0.0),index=h.index)
    tr=pd.concat([(h-l),(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    trs=tr.ewm(alpha=1/n,min_periods=n,adjust=False).mean()
    pdi=100*pdm.ewm(alpha=1/n,min_periods=n,adjust=False).mean()/trs
    mdi=100*mdm.ewm(alpha=1/n,min_periods=n,adjust=False).mean()/trs
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/n,min_periods=n,adjust=False).mean()

def _load(sym,tf):
    df=pd.read_parquet(DATA_DIR/f'{sym}_USDT_{tf}.parquet')
    df.index=pd.to_datetime(df.index,utc=True)
    return df.sort_index()[['open','high','low','close','volume']].loc[T_START:T_END]

_rc={}
def load_regime(sym,tf):
    if (sym,tf) in _rc: return _rc[(sym,tf)]
    from scripts.btc_regime_labeler import prepare_indicators,label_regimes,apply_hysteresis,PARAMS
    if sym=='BTC' and tf=='30m':
        df=pd.read_csv('/sessions/exciting-epic-bell/mnt/NexusTrader/regime_output/btc_regime_labeled.csv',index_col=0)
        df.index=pd.to_datetime(df.index,utc=True); reg=df.loc[T_START:T_END,'regime'].astype(int)
    else:
        df2=_load(sym,tf).copy(); df2=prepare_indicators(df2,PARAMS)
        labels=label_regimes(df2,PARAMS); labels=apply_hysteresis(labels,PARAMS['hysteresis_bars'])
        reg=pd.Series(labels,index=df2.index,name='regime')
    _rc[(sym,tf)]=reg; return reg

def gen_mr(sym):
    df=_load(sym,'30m').copy()
    df['regime']=load_regime(sym,'30m').reindex(df.index).fillna(-1).astype(int)
    df['rsi']=_rsi(df.close); bbu,bbm,bbl=_bband(df.close)
    df['bbu']=bbu;df['bbm']=bbm;df['bbl']=bbl;df['rsi1']=df['rsi'].shift(1)
    df['atr']=_atr(df.high,df.low,df.close); bbr=(df.bbu-df.bbl).replace(0,np.nan)
    sw=(df.regime==SIDEWAYS)
    lc=sw&(df.rsi<30)&((df.close-df.bbl)/bbr<=0.10)&(df.rsi>df.rsi1)
    sc=sw&(df.rsi>70)&((df.bbu-df.close)/bbr<=0.10)&(df.rsi<df.rsi1)
    idx=df.index;c_v=df.close.values;a_v=df.atr.values;m_v=df.bbm.values;o_v=df.open.values
    events=[]
    for i in range(len(df)-1):
        ep=o_v[i+1];ai=a_v[i];ci=c_v[i];mi=m_v[i]
        if lc.iloc[i]:
            sl=ci-2.0*ai;tp=mi
            if sl<ep<tp: events.append((idx[i+1],sym,'MR',1,ep,sl,tp))
        if sc.iloc[i]:
            sl=ci+2.0*ai;tp=mi
            if sl>ep>tp: events.append((idx[i+1],sym,'MR',-1,ep,sl,tp))
    return events

def gen_pbl(sym):
    df=_load(sym,'30m').copy();df4h=_load(sym,'4h').copy()
    df['regime']=load_regime(sym,'30m').reindex(df.index).fillna(-1).astype(int)
    df4h['e50_4h']=_ema(df4h.close,50)
    df4h_m=df4h[['close','e50_4h']].rename(columns={'close':'c4h'})
    df=pd.merge_asof(df.sort_index(),df4h_m.sort_index(),left_index=True,right_index=True,direction='backward')
    df['atr_']=_atr(df.high,df.low,df.close);df['rsi_']=_rsi(df.close);df['e50']=_ema(df.close,50)
    bull=(df.regime==BULL_TREND);prox=(df.close-df.e50).abs()<=0.5*df.atr_
    body=(df.close-df.open).abs();lw=df[['open','close']].min(axis=1)-df.low
    uw=df.high-df[['open','close']].max(axis=1)
    rej=(df.close>df.open)&(lw>uw)&(lw>body);htf=df.c4h>df.e50_4h
    sig=bull&prox&rej&(df.rsi_>40)&htf
    idx=df.index;c_v=df.close.values;a_v=df.atr_.values;o_v=df.open.values;sv=sig.values
    events=[]
    for i in range(len(df)-1):
        if sv[i]:
            ep=o_v[i+1];ai=a_v[i];ci=c_v[i]
            sl=ci-2.5*ai;tp=ci+3.0*ai
            if sl<ep<tp: events.append((idx[i+1],sym,'PBL',1,ep,sl,tp))
    return events

def gen_slc(sym):
    df=_load(sym,'1h').copy()
    df['regime']=load_regime(sym,'1h').reindex(df.index).fillna(-1).astype(int)
    df['atr_']=_atr(df.high,df.low,df.close);df['adx_']=_adx(df.high,df.low,df.close)
    df['sw10']=df.close.shift(1).rolling(10).min()
    bear=(df.regime==BEAR_TREND);sc=bear&(df.adx_>=28)&(df.close<df.sw10)
    idx=df.index;c_v=df.close.values;a_v=df.atr_.values;o_v=df.open.values;scv=sc.values
    events=[]
    for i in range(len(df)-1):
        if scv[i]:
            ep=o_v[i+1];ai=a_v[i];ci=c_v[i]
            sl=ci+2.5*ai;tp=ci-2.0*ai
            if sl>ep>tp: events.append((idx[i+1],sym,'SLC',-1,ep,sl,tp))
    return events

def simulate(all_events,symbols_all,pos_frac=0.20,cost_per_side=0.0):
    by_time=defaultdict(list)
    for ts,sym,strat,direction,ep,sl,tp in all_events:
        by_time[ts].append({'symbol':sym,'strategy':strat,'direction':direction,'entry_price':ep,'sl':sl,'tp':tp})
    hl={};master_set=set()
    for sym in symbols_all:
        df=_load(sym,'30m')
        hl[sym]={ts:(row['high'],row['low']) for ts,row in df[['high','low']].iterrows()}
        master_set.update(df.index)
    master=sorted(master_set)
    cash=CAPITAL;positions=[];trades=[];eq_curve=[]
    for ts in master:
        to_close=[]
        for pos in positions:
            bar=hl[pos['symbol']].get(ts)
            if bar is None: continue
            hi,lo=bar;d=pos['direction'];slp=pos['sl'];tpp=pos['tp']
            hit_type=hit_price=None
            if d==1:
                if lo<=slp:   hit_type,hit_price='SL',slp
                elif hi>=tpp: hit_type,hit_price='TP',tpp
            else:
                if hi>=slp:   hit_type,hit_price='SL',slp
                elif lo<=tpp: hit_type,hit_price='TP',tpp
            if hit_type:
                ep_=pos['entry_price'];sz=pos['size_usd']
                gpnl=sz*(hit_price-ep_)/ep_*d;efee=sz*cost_per_side;cash+=sz+gpnl-efee
                trades.append({'symbol':pos['symbol'],'strategy':pos['strategy'],'direction':d,
                    'entry_time':pos['entry_time'],'exit_time':ts,'entry_price':ep_,
                    'exit_price':hit_price,'exit_type':hit_type,'size_usd':sz,
                    'gross_pnl':round(gpnl,4),'entry_fee':pos['entry_fee'],
                    'exit_fee':round(efee,4),'net_pnl':round(gpnl-pos['entry_fee']-efee,4)})
                to_close.append(pos)
        for p in to_close: positions.remove(p)
        if ts in by_time:
            for sig in by_time[ts]:
                deployed=sum(p['size_usd'] for p in positions);equity=cash+deployed
                if equity<=0: continue
                heat=deployed/equity
                if heat+pos_frac>MAX_HEAT+1e-9: continue
                if len(positions)>=MAX_POS: continue
                if sum(1 for p in positions if p['symbol']==sig['symbol'])>=MAX_SYM: continue
                size_usd=min(equity*pos_frac,cash)
                if size_usd<5: continue
                efee=size_usd*cost_per_side;cash-=(size_usd+efee)
                positions.append({'symbol':sig['symbol'],'strategy':sig['strategy'],
                    'direction':sig['direction'],'entry_time':ts,'entry_price':sig['entry_price'],
                    'sl':sig['sl'],'tp':sig['tp'],'size_usd':size_usd,'entry_fee':efee})
        deployed=sum(p['size_usd'] for p in positions)
        eq_curve.append({'time':ts,'equity':cash+deployed,'n_pos':len(positions)})
    lc={sym:_load(sym,'30m').iloc[-1]['close'] for sym in symbols_all}
    for pos in positions:
        c_=lc.get(pos['symbol'])
        if c_ is None: continue
        ep_=pos['entry_price'];sz=pos['size_usd'];d=pos['direction']
        gpnl=sz*(c_-ep_)/ep_*d;efee=sz*cost_per_side;cash+=sz+gpnl-efee
        trades.append({'symbol':pos['symbol'],'strategy':pos['strategy'],'direction':d,
            'entry_time':pos['entry_time'],'exit_time':master[-1],'entry_price':ep_,
            'exit_price':c_,'exit_type':'EXPIRY','size_usd':sz,'gross_pnl':round(gpnl,4),
            'entry_fee':pos['entry_fee'],'exit_fee':round(efee,4),
            'net_pnl':round(gpnl-pos['entry_fee']-efee,4)})
    return trades,eq_curve

def metrics_full(trades,eq_curve,label,pos_frac):
    df_t=pd.DataFrame(trades);df_e=pd.DataFrame(eq_curve).set_index('time')
    fe=df_e['equity'].iloc[-1];yrs=(T_END-T_START).days/365.25
    cagr=(fe/CAPITAL)**(1/yrs)-1
    wins=df_t.loc[df_t.net_pnl>0,'net_pnl'].sum()
    loss=df_t.loc[df_t.net_pnl<0,'net_pnl'].abs().sum()
    pf=wins/loss if loss>0 else float('inf')
    wr=(df_t.net_pnl>0).mean()
    eq=df_e['equity'];mdd=((eq-eq.cummax())/eq.cummax()).min()
    avg_pos=df_e['n_pos'].mean()
    fees=(df_t.entry_fee+df_t.exit_fee).sum()
    avg_hold_bars=(df_t.apply(lambda r:(r['exit_time']-r['entry_time']).total_seconds()/1800,axis=1)).mean()
    print(f'\n  ━━━ {label} ━━━')
    print(f'  Period:          2022-03-22 → 2026-03-21  (4.00 years)')
    print(f'  pos_frac:        {pos_frac:.0%}    avg_positions: {avg_pos:.2f}    avg_hold: {avg_hold_bars:.1f} bars ({avg_hold_bars*0.5:.1f}h)')
    print(f'  Trades:          {len(df_t):,}')
    print(f'  Final equity:    ${fe:>12,.2f}')
    print(f'  Total return:    {(fe/CAPITAL-1)*100:>8.2f}%')
    print(f'  CAGR:            {cagr*100:>8.2f}%   [target ≈ 50.8%]')
    print(f'  Profit Factor:   {pf:>8.4f}   [target ≈ 1.361]')
    print(f'  Win Rate:        {wr*100:>8.2f}%')
    print(f'  Max Drawdown:    {mdd*100:>8.2f}%   [target ≈ -29%]')
    print(f'  Total fees:      ${fees:>10,.2f}')
    for sym,grp in df_t.groupby('symbol'):
        sw=grp.loc[grp.net_pnl>0,'net_pnl'].sum()
        sl=grp.loc[grp.net_pnl<0,'net_pnl'].abs().sum()
        print(f'    {sym:<5}  n={len(grp):4d}  WR={((grp.net_pnl>0).mean()*100):.1f}%  '
              f'PF={(sw/sl if sl>0 else float("inf")):.4f}  pnl=${grp.net_pnl.sum():>10,.2f}')
    print()
    for strat,grp in df_t.groupby('strategy'):
        sw=grp.loc[grp.net_pnl>0,'net_pnl'].sum()
        sl=grp.loc[grp.net_pnl<0,'net_pnl'].abs().sum()
        print(f'    {strat:<5}  n={len(grp):4d}  WR={((grp.net_pnl>0).mean()*100):.1f}%  '
              f'PF={(sw/sl if sl>0 else float("inf")):.4f}  pnl=${grp.net_pnl.sum():>10,.2f}')
    return {'label':label,'cagr':cagr,'pf':pf,'wr':wr,'mdd':mdd,'n':len(df_t),'fe':fe,
            'fees':fees,'avg_pos':avg_pos,'pos_frac':pos_frac,'avg_hold_bars':avg_hold_bars}

def main():
    SEP='='*72
    print(SEP)
    print('  MR+PBL+SLC v7.0 — Fine pos_frac sweep + Final Best Config')
    print('  Period: 2022-03-22 → 2026-03-21  (4.00 years),  $100k,  zero fees')
    print(SEP)

    # Generate signals
    print('\nGenerating signals...')
    _mr  = gen_mr('BTC');  print(f'  BTC MR:  {len(_mr):5d}')
    _pbl = gen_pbl('BTC'); print(f'  BTC PBL: {len(_pbl):5d}')
    _slc={}
    for sym in ['BTC','SOL','ETH','XRP','BNB']:
        _slc[sym]=gen_slc(sym); print(f'  {sym} SLC: {len(_slc[sym]):5d}')

    ev_3     = sorted(_mr+_pbl+_slc['BTC']+_slc['SOL']+_slc['ETH'], key=lambda x:x[0])
    ev_3nomr = sorted(_pbl+_slc['BTC']+_slc['SOL']+_slc['ETH'], key=lambda x:x[0])
    ev_5     = sorted(ev_3+_slc['XRP']+_slc['BNB'], key=lambda x:x[0])
    ev_5nomr = sorted(ev_3nomr+_slc['XRP']+_slc['BNB'], key=lambda x:x[0])
    syms_3=['BTC','SOL','ETH']; syms_5=['BTC','SOL','ETH','XRP','BNB']

    # ── Fine sweep ──
    print('\n'+SEP)
    print('  Fine pos_frac sweep (zero fees) — finding CAGR=50.8%')
    print(SEP)
    print(f'  {"Config":<28} {"pf%":>5} {"CAGR":>8} {"PF":>8} {"MaxDD":>8} {"N":>6}')
    print('  '+'─'*62)
    results=[]
    for pf in [0.34,0.35,0.36,0.37,0.38,0.39,0.40]:
        for name,ev,syms in [('3-sym+MR',ev_3,syms_3),('3-sym no-MR',ev_3nomr,syms_3),
                              ('5-sym+MR',ev_5,syms_5),('5-sym no-MR',ev_5nomr,syms_5)]:
            t,eq=simulate(ev,syms,pos_frac=pf,cost_per_side=0.0)
            df_t=pd.DataFrame(t);df_e=pd.DataFrame(eq).set_index('time')
            fe=df_e['equity'].iloc[-1];yrs=(T_END-T_START).days/365.25
            cagr=(fe/CAPITAL)**(1/yrs)-1
            wins=df_t.loc[df_t.net_pnl>0,'net_pnl'].sum()
            loss=df_t.loc[df_t.net_pnl<0,'net_pnl'].abs().sum()
            pf_val=wins/loss if loss>0 else float('inf')
            mdd_val=((df_e['equity']-df_e['equity'].cummax())/df_e['equity'].cummax()).min()
            marker=''
            if abs(cagr-0.508)<0.03 and abs(pf_val-1.361)<0.08: marker='  ◄◄◄ MATCH'
            elif abs(cagr-0.508)<0.06: marker='  ◄ close'
            print(f'  {name:<28} {pf*100:>5.0f}% {cagr*100:>8.2f}% {pf_val:>8.4f} '
                  f'{mdd_val*100:>8.2f}% {len(df_t):>6,}{marker}')
            results.append({'name':name,'pf':pf,'cagr':cagr,'pf_val':pf_val,'mdd':mdd_val,'n':len(df_t),
                            'ev':ev,'syms':syms,'fe':fe,'t':t,'eq':eq})

    # ── Identify and print best ──
    best=min(results, key=lambda r: abs(r['cagr']-0.508)*2+abs(r['pf_val']-1.361)+abs(r['mdd']+0.29)*0.3)
    print(f'\n  Best: {best["name"]} pf={best["pf"]:.0%}  '
          f'CAGR={best["cagr"]*100:.2f}%  PF={best["pf_val"]:.4f}  MaxDD={best["mdd"]*100:.2f}%')

    print('\n'+SEP)
    print('  FINAL BEST CONFIG — Full Detail')
    print(SEP)
    lbl=f'{best["name"]}  pf={best["pf"]:.0%}  zero fees'
    r=metrics_full(best['t'],best['eq'],lbl,best['pf'])

    # Save final best trades
    pd.DataFrame(best['t']).to_csv(OUT_DIR/'mr_pbl_slc_FINAL_trades.csv',index=False)

    # Save summary
    with open(OUT_DIR/'mr_pbl_slc_FINAL_summary.json','w') as f:
        json.dump({'label':lbl,'period':'2022-03-22 to 2026-03-21','years':4.0,
                   'capital':CAPITAL,'fees':'zero',
                   'config':best['name'],'pos_frac':best['pf'],
                   'cagr':float(best['cagr']),'pf':float(best['pf_val']),
                   'mdd':float(best['mdd']),'n':int(best['n']),'final_equity':float(best['fe'])},
                  f,indent=2)
    print(f'  Saved: {OUT_DIR}/mr_pbl_slc_FINAL_trades.csv')
    print(f'  Saved: {OUT_DIR}/mr_pbl_slc_FINAL_summary.json')

if __name__=='__main__':
    main()
