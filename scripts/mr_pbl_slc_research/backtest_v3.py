#!/usr/bin/env python3
"""
MR+PBL+SLC Backtest Engine v3.0 — NexusTrader Regime Labels
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KEY FIX from v2: Use NexusTrader's 6-regime system for regime labels
(not simple ADX<20). This matches the original optimization context.

  BTC MR  → SIDEWAYS regime (0)    on 30m, NexusTrader labels
  BTC PBL → BULL_TREND regime (1)  on 30m, NexusTrader labels
  BTC SLC → BEAR_TREND regime (2)  on 1h,  NexusTrader labels
  SOL SLC → BEAR_TREND regime (2)  on 1h,  NexusTrader labels
  ETH SLC → BEAR_TREND regime (2)  on 1h,  NexusTrader labels
"""

import pandas as pd
import numpy as np
from pathlib import Path
import json, sys
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, '/sessions/exciting-epic-bell/mnt/NexusTrader')

DATA_DIR  = Path('/sessions/exciting-epic-bell/mnt/NexusTrader/backtest_data')
OUT_DIR   = Path('/sessions/exciting-epic-bell/mnt/NexusTrader/reports')
OUT_DIR.mkdir(exist_ok=True)

T_START  = pd.Timestamp('2022-03-22', tz='UTC')
T_END    = pd.Timestamp('2026-03-21 23:59:59', tz='UTC')
CAPITAL  = 100_000.0
POS_FRAC = 0.20
MAX_HEAT = 0.80
MAX_POS  = 10
MAX_SYM  = 3

# Asset → strategy mapping (from transcript line 34356)
ASSET_STRATEGIES = {'BTC': ['MR','PBL','SLC'], 'SOL': ['SLC'], 'ETH': ['SLC']}
SYMBOLS = list(ASSET_STRATEGIES.keys())

# Regime codes
SIDEWAYS   = 0
BULL_TREND = 1
BEAR_TREND = 2

# ─── Indicator helpers ────────────────────────────────────────────────────────
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

# ─── Load OHLCV ───────────────────────────────────────────────────────────────
def _load(sym, tf):
    df=pd.read_parquet(DATA_DIR/f'{sym}_USDT_{tf}.parquet')
    df.index=pd.to_datetime(df.index,utc=True)
    return df.sort_index()[['open','high','low','close','volume']].loc[T_START:T_END]

# ─── Load/compute regime labels ───────────────────────────────────────────────
def load_regime(sym, tf):
    """Load NexusTrader regime labels for given symbol/timeframe."""
    from scripts.btc_regime_labeler import prepare_indicators, label_regimes, apply_hysteresis, PARAMS
    
    if sym == 'BTC' and tf == '30m':
        # Use pre-computed file
        df = pd.read_csv('/sessions/exciting-epic-bell/mnt/NexusTrader/regime_output/btc_regime_labeled.csv', index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.loc[T_START:T_END]
        return df['regime'].astype(int)
    
    # Compute from scratch
    df = _load(sym, tf)
    df2 = df.copy()
    df2 = prepare_indicators(df2, PARAMS)
    labels = label_regimes(df2, PARAMS)
    labels = apply_hysteresis(labels, PARAMS['hysteresis_bars'])
    reg = pd.Series(labels, index=df.index, name='regime')
    return reg

# ─── Signal generators ────────────────────────────────────────────────────────
def gen_mr(sym):
    """Mean Reversion — 30m — SIDEWAYS regime (NexusTrader definition)"""
    df = _load(sym, '30m').copy()
    regime = load_regime(sym, '30m')
    df['regime'] = regime.reindex(df.index).fillna(-1).astype(int)
    
    df['rsi']  = _rsi(df.close)
    bbu,bbm,bbl = _bband(df.close)
    df['bbu']=bbu; df['bbm']=bbm; df['bbl']=bbl
    df['rsi1'] = df['rsi'].shift(1)
    df['atr']  = _atr(df.high,df.low,df.close)

    bbr = (df.bbu - df.bbl).replace(0, np.nan)
    sw  = (df.regime == SIDEWAYS)

    long_c  = sw & (df.rsi<30) & ((df.close-df.bbl)/bbr<=0.10) & (df.rsi>df.rsi1)
    short_c = sw & (df.rsi>70) & ((df.bbu-df.close)/bbr<=0.10) & (df.rsi<df.rsi1)

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
    """Pullback Long — 30m — BULL_TREND regime (NexusTrader definition)"""
    df   = _load(sym, '30m').copy()
    df4h = _load(sym, '4h').copy()
    regime = load_regime(sym, '30m')
    df['regime'] = regime.reindex(df.index).fillna(-1).astype(int)

    df4h['e50_4h'] = _ema(df4h.close, 50)
    df4h_m = df4h[['close','e50_4h']].rename(columns={'close':'c4h'})
    df = pd.merge_asof(df.sort_index(), df4h_m.sort_index(),
                       left_index=True, right_index=True, direction='backward')

    df['atr_'] = _atr(df.high, df.low, df.close)
    df['rsi_'] = _rsi(df.close)
    df['e50']  = _ema(df.close, 50)

    bull = (df.regime == BULL_TREND)
    prox = (df.close-df.e50).abs() <= 0.5*df.atr_
    body = (df.close-df.open).abs()
    lw   = df[['open','close']].min(axis=1) - df.low
    uw   = df.high - df[['open','close']].max(axis=1)
    rej  = (df.close>df.open)&(lw>uw)&(lw>body)
    htf  = df.c4h > df.e50_4h
    sig  = bull & prox & rej & (df.rsi_>40) & htf

    idx=df.index; c_v=df.close.values; a_v=df.atr_.values; o_v=df.open.values; sv=sig.values
    events=[]
    for i in range(len(df)-1):
        if sv[i]:
            ep=o_v[i+1]; ai=a_v[i]; ci=c_v[i]
            sl=ci-2.5*ai; tp=ci+3.0*ai
            if sl<ep<tp: events.append((idx[i+1],sym,'PBL',1,ep,sl,tp))
    return events

def gen_slc(sym):
    """Swing Low Continuation — 1h — BEAR_TREND regime (NexusTrader definition)"""
    df = _load(sym, '1h').copy()
    regime_1h = load_regime(sym, '1h')
    df['regime'] = regime_1h.reindex(df.index).fillna(-1).astype(int)

    df['atr_']  = _atr(df.high, df.low, df.close)
    df['adx_']  = _adx(df.high, df.low, df.close)
    df['sw10']  = df.close.shift(1).rolling(10).min()

    bear    = (df.regime == BEAR_TREND)
    short_c = bear & (df.adx_ >= 28) & (df.close < df.sw10)

    idx=df.index; c_v=df.close.values; a_v=df.atr_.values; o_v=df.open.values; sc=short_c.values
    events=[]
    for i in range(len(df)-1):
        if sc[i]:
            ep=o_v[i+1]; ai=a_v[i]; ci=c_v[i]
            sl=ci+2.5*ai; tp=ci-2.0*ai
            if sl>ep>tp: events.append((idx[i+1],sym,'SLC',-1,ep,sl,tp))
    return events

# ─── Portfolio simulation ─────────────────────────────────────────────────────
def simulate(all_events, cost_per_side=0.0):
    by_time = defaultdict(list)
    for ts,sym,strat,direction,ep,sl,tp in all_events:
        by_time[ts].append({'symbol':sym,'strategy':strat,'direction':direction,
                            'entry_price':ep,'sl':sl,'tp':tp})

    hl = {}; master_set = set()
    for sym in SYMBOLS:
        df=_load(sym,'30m')
        hl[sym]={ts:(row['high'],row['low']) for ts,row in df[['high','low']].iterrows()}
        master_set.update(df.index)
    master=sorted(master_set)

    cash=CAPITAL; positions=[]; trades=[]; eq_curve=[]
    COST=cost_per_side

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
                gpnl=sz*(hit_price-ep_)/ep_*d; efee=sz*COST; cash+=sz+gpnl-efee
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
                if heat+POS_FRAC>MAX_HEAT+1e-9: continue
                if len(positions)>=MAX_POS: continue
                if sum(1 for p in positions if p['symbol']==sig['symbol'])>=MAX_SYM: continue
                size_usd=min(equity*POS_FRAC, cash)
                if size_usd<5: continue
                efee=size_usd*COST; cash-=(size_usd+efee)
                positions.append({'symbol':sig['symbol'],'strategy':sig['strategy'],
                    'direction':sig['direction'],'entry_time':ts,
                    'entry_price':sig['entry_price'],'sl':sig['sl'],'tp':sig['tp'],
                    'size_usd':size_usd,'entry_fee':efee})

        deployed=sum(p['size_usd'] for p in positions)
        eq_curve.append({'time':ts,'equity':cash+deployed})

    # Force-close remaining
    lc={sym: _load(sym,'30m').iloc[-1]['close'] for sym in SYMBOLS}
    for pos in positions:
        c_=lc.get(pos['symbol'])
        if c_ is None: continue
        ep_=pos['entry_price']; sz=pos['size_usd']; d=pos['direction']
        gpnl=sz*(c_-ep_)/ep_*d; efee=sz*COST; cash+=sz+gpnl-efee
        trades.append({'symbol':pos['symbol'],'strategy':pos['strategy'],'direction':d,
            'entry_time':pos['entry_time'],'exit_time':master[-1],
            'entry_price':ep_,'exit_price':c_,'exit_type':'EXPIRY',
            'size_usd':sz,'gross_pnl':round(gpnl,4),'entry_fee':pos['entry_fee'],
            'exit_fee':round(efee,4),'net_pnl':round(gpnl-pos['entry_fee']-efee,4)})
    return trades, eq_curve

def metrics(trades, eq_curve, label):
    df_t=pd.DataFrame(trades); df_e=pd.DataFrame(eq_curve).set_index('time')
    fe=df_e['equity'].iloc[-1]; yrs=(T_END-T_START).days/365.25
    cagr=(fe/CAPITAL)**(1/yrs)-1
    wins=df_t.loc[df_t.net_pnl>0,'net_pnl'].sum()
    loss=df_t.loc[df_t.net_pnl<0,'net_pnl'].abs().sum()
    pf=wins/loss if loss>0 else float('inf')
    wr=(df_t.net_pnl>0).mean()
    eq=df_e['equity']
    mdd=((eq-eq.cummax())/eq.cummax()).min()
    fees=(df_t.entry_fee+df_t.exit_fee).sum()
    print(f'\n  ── {label} ──')
    print(f'  Trades:        {len(df_t):,}')
    print(f'  Final equity:  ${fe:>12,.2f}')
    print(f'  Total return:  {(fe/CAPITAL-1)*100:>8.2f}%')
    print(f'  CAGR:          {cagr*100:>8.2f}%  [target: ~50.8%]')
    print(f'  PF:            {pf:>8.4f}  [target: ~1.361]')
    print(f'  WR:            {wr*100:>8.2f}%')
    print(f'  MaxDD:         {mdd*100:>8.2f}%  [target: ~-29%]')
    print(f'  Total fees:    ${fees:>12,.2f}')
    for strat,grp in df_t.groupby('strategy'):
        sw=grp.loc[grp.net_pnl>0,'net_pnl'].sum()
        sl=grp.loc[grp.net_pnl<0,'net_pnl'].abs().sum()
        spf=sw/sl if sl>0 else float('inf')
        print(f'    {strat:<5}  n={len(grp):4d}  WR={((grp.net_pnl>0).mean()*100):.1f}%  PF={spf:.4f}  pnl=${grp.net_pnl.sum():>10,.2f}')
    return {'cagr':cagr,'pf':pf,'wr':wr,'mdd':mdd,'n':len(df_t),'fe':fe,'fees':fees}

def main():
    SEP='='*72
    print(SEP)
    print('  MR+PBL+SLC v3.0 — NexusTrader Regime Labels')
    print(SEP)

    print('\nGenerating signals with NexusTrader regime labels...')
    all_events=[]
    for sym,strats in ASSET_STRATEGIES.items():
        ev_sym=[]
        if 'MR'  in strats: ev=gen_mr(sym);  ev_sym.extend(ev);  print(f'  {sym} MR:  {len(ev):5d}')
        if 'PBL' in strats: ev=gen_pbl(sym); ev_sym.extend(ev);  print(f'  {sym} PBL: {len(ev):5d}')
        if 'SLC' in strats: ev=gen_slc(sym); ev_sym.extend(ev);  print(f'  {sym} SLC: {len(ev):5d}')
        all_events.extend(ev_sym)
        print(f'  {sym} total: {sum(1 for e in ev_sym if True)}')
    all_events.sort(key=lambda x: x[0])
    print(f'\n  Grand total: {len(all_events):,} signal events')

    # Run A: zero fees
    print('\nRun A — Zero fees:')
    t_a,eq_a=simulate(all_events, cost_per_side=0.0)
    r_a=metrics(t_a,eq_a,'Run A: Zero fees')

    # Run B: 0.09%/side (0.18% RT)
    print('\nRun B — 0.09%/side (0.18% RT):')
    t_b,eq_b=simulate(all_events, cost_per_side=0.0009)
    r_b=metrics(t_b,eq_b,'Run B: 0.18% RT fees')

    # Run C: 0.04%/side (0.08% RT, maker only)
    print('\nRun C — 0.04%/side (0.08% RT):')
    t_c,eq_c=simulate(all_events, cost_per_side=0.0004)
    r_c=metrics(t_c,eq_c,'Run C: 0.08% RT fees')

    print('\n'+SEP)
    print('  SUMMARY')
    print(SEP)
    print(f'  {"Run":<28} {"CAGR":>8} {"PF":>8} {"MaxDD":>8} {"Trades":>7}')
    print('  '+'-'*60)
    for label,r in [('A: Zero fees',r_a),('B: 0.18% RT',r_b),('C: 0.08% RT',r_c)]:
        print(f'  {label:<28} {r["cagr"]*100:>8.2f}% {r["pf"]:>8.4f} {r["mdd"]*100:>8.2f}% {r["n"]:>7,}')
    print(f'  {"TARGET":<28} {"~50.8%":>8} {"~1.361":>8} {"~-29%":>8} {"~2116":>7}')

    # Save zero-fee results
    pd.DataFrame(t_a).to_csv(OUT_DIR/'mr_pbl_slc_v3_trades.csv', index=False)
    print(f'\n  Saved trades to {OUT_DIR}/mr_pbl_slc_v3_trades.csv')

if __name__ == '__main__':
    main()
