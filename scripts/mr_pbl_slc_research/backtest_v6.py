#!/usr/bin/env python3
"""
MR+PBL+SLC Backtest Engine v6.0 — Best-Config Validation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v5 findings:
  - Fixed-dollar sizing:  REJECTED (lower CAGR)
  - TP=3×ATR:             NEUTRAL  (+1.2pp)
  - 5-sym pf=40% zero fees: CAGR=52.55%, MaxDD=-26.62% ← closest to target
  - 3-sym pf=40% zero fees: CAGR=56.78%, MaxDD=-21.81%

Remaining question: which fee level matches CAGR≈50.8%?
Also: does removing MR (PF drag) close the PF gap to ~1.361?

This script runs a targeted 3×3 grid:
  Configs:  [3-sym pf=40%, 5-sym pf=40%, 3-sym pf=33%]
  Fees:     [0.0%, 0.02%/side, 0.04%/side]

Plus a "no-MR" variant on the best config to test PF sensitivity.

All runs: 2022-03-22 → 2026-03-21 (4.00 years), $100k.
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

SIDEWAYS   = 0
BULL_TREND = 1
BEAR_TREND = 2

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

_regime_cache={}
def load_regime(sym,tf):
    key=(sym,tf)
    if key in _regime_cache: return _regime_cache[key]
    from scripts.btc_regime_labeler import prepare_indicators,label_regimes,apply_hysteresis,PARAMS
    if sym=='BTC' and tf=='30m':
        df=pd.read_csv('/sessions/exciting-epic-bell/mnt/NexusTrader/regime_output/btc_regime_labeled.csv',index_col=0)
        df.index=pd.to_datetime(df.index,utc=True)
        reg=df.loc[T_START:T_END,'regime'].astype(int)
    else:
        df2=_load(sym,tf).copy()
        df2=prepare_indicators(df2,PARAMS); labels=label_regimes(df2,PARAMS)
        labels=apply_hysteresis(labels,PARAMS['hysteresis_bars'])
        reg=pd.Series(labels,index=df2.index,name='regime')
    _regime_cache[key]=reg; return reg

def gen_mr(sym):
    df=_load(sym,'30m').copy()
    df['regime']=load_regime(sym,'30m').reindex(df.index).fillna(-1).astype(int)
    df['rsi']=_rsi(df.close); bbu,bbm,bbl=_bband(df.close)
    df['bbu']=bbu; df['bbm']=bbm; df['bbl']=bbl; df['rsi1']=df['rsi'].shift(1)
    df['atr']=_atr(df.high,df.low,df.close); bbr=(df.bbu-df.bbl).replace(0,np.nan)
    sw=(df.regime==SIDEWAYS)
    long_c=sw&(df.rsi<30)&((df.close-df.bbl)/bbr<=0.10)&(df.rsi>df.rsi1)
    short_c=sw&(df.rsi>70)&((df.bbu-df.close)/bbr<=0.10)&(df.rsi<df.rsi1)
    idx=df.index; c_v=df.close.values; a_v=df.atr.values; m_v=df.bbm.values; o_v=df.open.values
    events=[]
    for i in range(len(df)-1):
        ep=o_v[i+1]; ai=a_v[i]; ci=c_v[i]; mi=m_v[i]
        if long_c.iloc[i]:
            sl=ci-2.0*ai; tp=mi
            if sl<ep<tp: events.append((idx[i+1],sym,'MR',1,ep,sl,tp))
        if short_c.iloc[i]:
            sl=ci+2.0*ai; tp=mi
            if sl>ep>tp: events.append((idx[i+1],sym,'MR',-1,ep,sl,tp))
    return events

def gen_pbl(sym):
    df=_load(sym,'30m').copy(); df4h=_load(sym,'4h').copy()
    df['regime']=load_regime(sym,'30m').reindex(df.index).fillna(-1).astype(int)
    df4h['e50_4h']=_ema(df4h.close,50)
    df4h_m=df4h[['close','e50_4h']].rename(columns={'close':'c4h'})
    df=pd.merge_asof(df.sort_index(),df4h_m.sort_index(),left_index=True,right_index=True,direction='backward')
    df['atr_']=_atr(df.high,df.low,df.close); df['rsi_']=_rsi(df.close); df['e50']=_ema(df.close,50)
    bull=(df.regime==BULL_TREND); prox=(df.close-df.e50).abs()<=0.5*df.atr_
    body=(df.close-df.open).abs(); lw=df[['open','close']].min(axis=1)-df.low
    uw=df.high-df[['open','close']].max(axis=1)
    rej=(df.close>df.open)&(lw>uw)&(lw>body); htf=df.c4h>df.e50_4h
    sig=bull&prox&rej&(df.rsi_>40)&htf
    idx=df.index; c_v=df.close.values; a_v=df.atr_.values; o_v=df.open.values; sv=sig.values
    events=[]
    for i in range(len(df)-1):
        if sv[i]:
            ep=o_v[i+1]; ai=a_v[i]; ci=c_v[i]
            sl=ci-2.5*ai; tp=ci+3.0*ai
            if sl<ep<tp: events.append((idx[i+1],sym,'PBL',1,ep,sl,tp))
    return events

def gen_slc(sym):
    df=_load(sym,'1h').copy()
    df['regime']=load_regime(sym,'1h').reindex(df.index).fillna(-1).astype(int)
    df['atr_']=_atr(df.high,df.low,df.close)
    df['adx_']=_adx(df.high,df.low,df.close)
    df['sw10']=df.close.shift(1).rolling(10).min()
    bear=(df.regime==BEAR_TREND); short_c=bear&(df.adx_>=28)&(df.close<df.sw10)
    idx=df.index; c_v=df.close.values; a_v=df.atr_.values; o_v=df.open.values; sc=short_c.values
    events=[]
    for i in range(len(df)-1):
        if sc[i]:
            ep=o_v[i+1]; ai=a_v[i]; ci=c_v[i]
            sl=ci+2.5*ai; tp=ci-2.0*ai
            if sl>ep>tp: events.append((idx[i+1],sym,'SLC',-1,ep,sl,tp))
    return events

def simulate(all_events,symbols_all,pos_frac=0.20,cost_per_side=0.0):
    by_time=defaultdict(list)
    for ts,sym,strat,direction,ep,sl,tp in all_events:
        by_time[ts].append({'symbol':sym,'strategy':strat,'direction':direction,
                            'entry_price':ep,'sl':sl,'tp':tp})
    hl={}; master_set=set()
    for sym in symbols_all:
        df=_load(sym,'30m')
        hl[sym]={ts:(row['high'],row['low']) for ts,row in df[['high','low']].iterrows()}
        master_set.update(df.index)
    master=sorted(master_set)
    cash=CAPITAL; positions=[]; trades=[]; eq_curve=[]
    for ts in master:
        to_close=[]
        for pos in positions:
            bar=hl[pos['symbol']].get(ts)
            if bar is None: continue
            hi,lo=bar; d=pos['direction']; slp=pos['sl']; tpp=pos['tp']
            hit_type=hit_price=None
            if d==1:
                if lo<=slp:   hit_type,hit_price='SL',slp
                elif hi>=tpp: hit_type,hit_price='TP',tpp
            else:
                if hi>=slp:   hit_type,hit_price='SL',slp
                elif lo<=tpp: hit_type,hit_price='TP',tpp
            if hit_type:
                ep_=pos['entry_price']; sz=pos['size_usd']
                gpnl=sz*(hit_price-ep_)/ep_*d; efee=sz*cost_per_side; cash+=sz+gpnl-efee
                trades.append({'symbol':pos['symbol'],'strategy':pos['strategy'],
                    'direction':d,'entry_time':pos['entry_time'],'exit_time':ts,
                    'entry_price':ep_,'exit_price':hit_price,'exit_type':hit_type,
                    'size_usd':sz,'gross_pnl':round(gpnl,4),'entry_fee':pos['entry_fee'],
                    'exit_fee':round(efee,4),'net_pnl':round(gpnl-pos['entry_fee']-efee,4)})
                to_close.append(pos)
        for p in to_close: positions.remove(p)
        if ts in by_time:
            for sig in by_time[ts]:
                deployed=sum(p['size_usd'] for p in positions)
                equity=cash+deployed
                if equity<=0: continue
                heat=deployed/equity
                if heat+pos_frac>MAX_HEAT+1e-9: continue
                if len(positions)>=MAX_POS: continue
                if sum(1 for p in positions if p['symbol']==sig['symbol'])>=MAX_SYM: continue
                size_usd=min(equity*pos_frac,cash)
                if size_usd<5: continue
                efee=size_usd*cost_per_side; cash-=(size_usd+efee)
                positions.append({'symbol':sig['symbol'],'strategy':sig['strategy'],
                    'direction':sig['direction'],'entry_time':ts,
                    'entry_price':sig['entry_price'],'sl':sig['sl'],'tp':sig['tp'],
                    'size_usd':size_usd,'entry_fee':efee})
        deployed=sum(p['size_usd'] for p in positions)
        eq_curve.append({'time':ts,'equity':cash+deployed,'n_pos':len(positions)})
    lc={sym:_load(sym,'30m').iloc[-1]['close'] for sym in symbols_all}
    for pos in positions:
        c_=lc.get(pos['symbol'])
        if c_ is None: continue
        ep_=pos['entry_price']; sz=pos['size_usd']; d=pos['direction']
        gpnl=sz*(c_-ep_)/ep_*d; efee=sz*cost_per_side; cash+=sz+gpnl-efee
        trades.append({'symbol':pos['symbol'],'strategy':pos['strategy'],'direction':d,
            'entry_time':pos['entry_time'],'exit_time':master[-1],
            'entry_price':ep_,'exit_price':c_,'exit_type':'EXPIRY',
            'size_usd':sz,'gross_pnl':round(gpnl,4),'entry_fee':pos['entry_fee'],
            'exit_fee':round(efee,4),'net_pnl':round(gpnl-pos['entry_fee']-efee,4)})
    return trades,eq_curve

def metrics(trades,eq_curve,label,pos_frac=0.20,verbose=True):
    df_t=pd.DataFrame(trades); df_e=pd.DataFrame(eq_curve).set_index('time')
    fe=df_e['equity'].iloc[-1]; yrs=(T_END-T_START).days/365.25
    cagr=(fe/CAPITAL)**(1/yrs)-1
    wins=df_t.loc[df_t.net_pnl>0,'net_pnl'].sum()
    loss=df_t.loc[df_t.net_pnl<0,'net_pnl'].abs().sum()
    pf=wins/loss if loss>0 else float('inf')
    wr=(df_t.net_pnl>0).mean()
    eq=df_e['equity']; mdd=((eq-eq.cummax())/eq.cummax()).min()
    fees=(df_t.entry_fee+df_t.exit_fee).sum()
    avg_pos=df_e['n_pos'].mean()
    if verbose:
        print(f'\n  ── {label} ──')
        print(f'  Period:        2022-03-22 → 2026-03-21  (4.00 years)')
        print(f'  pos_frac={pos_frac:.0%}  avg_pos={avg_pos:.2f}')
        print(f'  Trades:        {len(df_t):,}')
        print(f'  Final equity:  ${fe:>12,.2f}')
        print(f'  CAGR:          {cagr*100:>8.2f}%  [target: ~50.8%]')
        print(f'  PF:            {pf:>8.4f}  [target: ~1.361]')
        print(f'  WR:            {wr*100:>8.2f}%')
        print(f'  MaxDD:         {mdd*100:>8.2f}%  [target: ~-29%]')
        print(f'  Total fees:    ${fees:>12,.2f}')
        for strat,grp in df_t.groupby('strategy'):
            sw=grp.loc[grp.net_pnl>0,'net_pnl'].sum()
            sl=grp.loc[grp.net_pnl<0,'net_pnl'].abs().sum()
            spf=sw/sl if sl>0 else float('inf')
            print(f'    {strat:<5}  n={len(grp):4d}  WR={((grp.net_pnl>0).mean()*100):.1f}%  '
                  f'PF={spf:.4f}  pnl=${grp.net_pnl.sum():>10,.2f}')
    return {'label':label,'cagr':cagr,'pf':pf,'wr':wr,'mdd':mdd,
            'n':len(df_t),'fe':fe,'fees':fees,'avg_pos':avg_pos,'pos_frac':pos_frac}

def main():
    SEP='='*72
    print(SEP)
    print('  MR+PBL+SLC v6.0 — Best-Config Validation + Fee Grid')
    print('  Period: 2022-03-22 → 2026-03-21  (4.00 years),  $100k')
    print(SEP)

    # ── Pre-generate all signals (cached) ──
    print('\nGenerating signals...')
    _mr   = gen_mr('BTC');  print(f'  BTC MR:  {len(_mr):5d}')
    _pbl  = gen_pbl('BTC'); print(f'  BTC PBL: {len(_pbl):5d}')
    _slc  = {}
    for sym in ['BTC','SOL','ETH','XRP','BNB']:
        _slc[sym]=gen_slc(sym); print(f'  {sym} SLC: {len(_slc[sym]):5d}')

    ev_3 = sorted(_mr+_pbl+_slc['BTC']+_slc['SOL']+_slc['ETH'], key=lambda x:x[0])
    ev_5 = sorted(ev_3+_slc['XRP']+_slc['BNB'], key=lambda x:x[0])
    ev_3_nomr = sorted(_pbl+_slc['BTC']+_slc['SOL']+_slc['ETH'], key=lambda x:x[0])
    ev_5_nomr = sorted(_pbl+_slc['BTC']+_slc['SOL']+_slc['ETH']+_slc['XRP']+_slc['BNB'], key=lambda x:x[0])

    syms_3 = ['BTC','SOL','ETH']
    syms_5 = ['BTC','SOL','ETH','XRP','BNB']

    FEES = [('0.00%', 0.0), ('0.02%/side', 0.0002), ('0.04%/side', 0.0004)]

    all_results = []

    # ════════════════════════════════════════════════════
    # GRID: [3-sym pf=40%, 5-sym pf=40%, 3-sym pf=33%] × fees
    # ════════════════════════════════════════════════════
    configs = [
        ('3-sym pf=40%', ev_3, syms_3, 0.40),
        ('5-sym pf=40%', ev_5, syms_5, 0.40),
        ('3-sym pf=33%', ev_3, syms_3, 0.33),
        ('5-sym pf=33%', ev_5, syms_5, 0.33),
        ('3-sym pf=20%', ev_3, syms_3, 0.20),   # v3 baseline
    ]

    print('\n' + SEP)
    print('  SECTION A: Fee grid across best configs')
    print(SEP)
    for cfg_name, ev, syms, pf in configs:
        print(f'\n  Config: {cfg_name}')
        for fee_label, fee in FEES:
            lbl = f'{cfg_name}  {fee_label}'
            t,eq = simulate(ev, syms, pos_frac=pf, cost_per_side=fee)
            r = metrics(t, eq, lbl, pos_frac=pf, verbose=False)
            all_results.append(r)
            marker = ''
            if abs(r['cagr']-0.508)<0.05 and abs(r['pf']-1.361)<0.12:
                marker = '  *** CLOSE MATCH ***'
            print(f'    {fee_label:>12}: CAGR={r["cagr"]*100:6.2f}%  PF={r["pf"]:6.4f}  '
                  f'MaxDD={r["mdd"]*100:7.2f}%  n={r["n"]:5,}{marker}')

    # ════════════════════════════════════════════════════
    # SECTION B: No-MR variants (PF sensitivity test)
    # ════════════════════════════════════════════════════
    print('\n' + SEP)
    print('  SECTION B: No-MR variants (MR currently drags PF)')
    print(SEP)
    nomr_configs = [
        ('3-sym NO-MR pf=40%', ev_3_nomr, syms_3, 0.40),
        ('5-sym NO-MR pf=40%', ev_5_nomr, syms_5, 0.40),
        ('3-sym NO-MR pf=33%', ev_3_nomr, syms_3, 0.33),
        ('5-sym NO-MR pf=33%', ev_5_nomr, syms_5, 0.33),
    ]
    nomr_results=[]
    for cfg_name,ev,syms,pf in nomr_configs:
        print(f'\n  Config: {cfg_name}')
        for fee_label,fee in [('0.00%',0.0),('0.04%/side',0.0004)]:
            lbl=f'{cfg_name}  {fee_label}'
            t,eq=simulate(ev,syms,pos_frac=pf,cost_per_side=fee)
            r=metrics(t,eq,lbl,pos_frac=pf,verbose=False)
            nomr_results.append(r)
            marker=''
            if abs(r['cagr']-0.508)<0.05 and abs(r['pf']-1.361)<0.10:
                marker='  *** CLOSE MATCH ***'
            print(f'    {fee_label:>12}: CAGR={r["cagr"]*100:6.2f}%  PF={r["pf"]:6.4f}  '
                  f'MaxDD={r["mdd"]*100:7.2f}%  n={r["n"]:5,}{marker}')

    # ════════════════════════════════════════════════════
    # BEST MATCH — detailed printout
    # ════════════════════════════════════════════════════
    all_r = all_results + nomr_results
    best = min(all_r, key=lambda r: abs(r['cagr']-0.508)+abs(r['pf']-1.361)*2+abs(r['mdd']+0.29)*0.5)
    print(f'\n{"─"*72}')
    print(f'  Best match by combined CAGR+PF+MaxDD distance: "{best["label"]}"')

    # Re-run best config with full verbose output
    for cfg_name,ev,syms,pf in (configs+nomr_configs):
        if cfg_name in best['label'] or best['label'].startswith(cfg_name):
            fee_val=0.0
            for fl,fv in FEES:
                if fl in best['label']: fee_val=fv; break
            t,eq=simulate(ev,syms,pos_frac=pf,cost_per_side=fee_val)
            metrics(t,eq,f'BEST: {best["label"]}',pos_frac=pf,verbose=True)
            pd.DataFrame(t).to_csv(OUT_DIR/'mr_pbl_slc_v6_best_trades.csv',index=False)
            break

    # ════════════════════════════════════════════════════
    # FULL SUMMARY TABLE
    # ════════════════════════════════════════════════════
    print('\n'+SEP)
    print('  FULL SUMMARY — 2022-03-22 → 2026-03-21 (4.00 years), $100k')
    print(SEP)
    print(f'  {"Label":<38} {"CAGR":>8} {"PF":>8} {"MaxDD":>8} {"N":>6} {"Fees$":>8}')
    print('  '+'─'*82)
    for r in all_r:
        marker=''
        if abs(r['cagr']-0.508)<0.06 and r['pf']>1.28: marker=' ◄◄'
        print(f'  {r["label"]:<38} {r["cagr"]*100:>8.2f}% {r["pf"]:>8.4f} '
              f'{r["mdd"]*100:>8.2f}% {r["n"]:>6,} {r["fees"]:>8,.0f}{marker}')
    print(f'\n  {"TARGET":<38} {"~50.8%":>8} {"~1.361":>8} {"~-29%":>8} {"~2116":>6}')

    out=[]
    for r in all_r:
        out.append({k:(float(v) if isinstance(v,(np.floating,float)) else v) for k,v in r.items()})
    with open(OUT_DIR/'mr_pbl_slc_v6_summary.json','w') as f:
        json.dump({'period':'2022-03-22 to 2026-03-21','years':4.0,
                   'capital':CAPITAL,'runs':out},f,indent=2)
    print(f'\n  Saved: {OUT_DIR}/mr_pbl_slc_v6_summary.json')

if __name__=='__main__':
    main()
