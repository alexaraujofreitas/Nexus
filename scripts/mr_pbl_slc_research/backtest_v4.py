#!/usr/bin/env python3
"""
MR+PBL+SLC Backtest Engine v4.0 — Phase 5 Gap Investigation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gap from v3.0: CAGR=35.87% (zero fees) vs target ~50.8%.
Two hypotheses tested here:

  H1 — Fixed-dollar sizing: pos_frac=20% of INITIAL $100k ($20k fixed)
       rather than 20% of current equity. This changes the capital
       allocation dynamic as equity grows.

  H2 — SLC TP=3.0×ATR instead of 2.0×ATR: larger profit target,
       changes win:loss ratio and WR.

  H3 — Combined: H1 + H2 together.

All runs: 2022-03-22 → 2026-03-21 (4.00 years), $100k start, zero fees.
Asset map (locked): BTC(MR+PBL+SLC), SOL(SLC), ETH(SLC).
Regime: NexusTrader 6-regime labels (SIDEWAYS=0, BULL_TREND=1, BEAR_TREND=2).
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
POS_FRAC = 0.20       # 20% — as fraction of equity (equity-mode) or of CAPITAL (fixed-mode)
MAX_HEAT = 0.80
MAX_POS  = 10
MAX_SYM  = 3

ASSET_STRATEGIES = {'BTC': ['MR','PBL','SLC'], 'SOL': ['SLC'], 'ETH': ['SLC']}
SYMBOLS = list(ASSET_STRATEGIES.keys())

SIDEWAYS   = 0
BULL_TREND = 1
BEAR_TREND = 2

# ─── Indicator helpers ────────────────────────────────────────────────────────
def _atr(h, l, c, n=14):
    tr = pd.concat([(h-l), (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()

def _rsi(c, n=14):
    d = c.diff()
    g  = d.clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    lo = (-d).clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + g / lo.replace(0, np.nan))

def _ema(c, n):
    return c.ewm(span=n, adjust=False).mean()

def _bband(c, n=20, k=2.0):
    m = c.rolling(n).mean()
    s = c.rolling(n).std(ddof=1)
    return m+k*s, m, m-k*s

def _adx(h, l, c, n=14):
    up = h.diff(); dn = -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    tr  = pd.concat([(h-l), (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    trs = tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1/n, min_periods=n, adjust=False).mean() / trs
    mdi = 100 * mdm.ewm(alpha=1/n, min_periods=n, adjust=False).mean() / trs
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, min_periods=n, adjust=False).mean()

# ─── Load OHLCV ───────────────────────────────────────────────────────────────
def _load(sym, tf):
    df = pd.read_parquet(DATA_DIR / f'{sym}_USDT_{tf}.parquet')
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()[['open','high','low','close','volume']].loc[T_START:T_END]

# ─── Load/compute regime labels ───────────────────────────────────────────────
def load_regime(sym, tf):
    from scripts.btc_regime_labeler import prepare_indicators, label_regimes, apply_hysteresis, PARAMS
    if sym == 'BTC' and tf == '30m':
        df = pd.read_csv(
            '/sessions/exciting-epic-bell/mnt/NexusTrader/regime_output/btc_regime_labeled.csv',
            index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        return df.loc[T_START:T_END, 'regime'].astype(int)
    df2 = _load(sym, tf).copy()
    df2 = prepare_indicators(df2, PARAMS)
    labels = label_regimes(df2, PARAMS)
    labels = apply_hysteresis(labels, PARAMS['hysteresis_bars'])
    return pd.Series(labels, index=df2.index, name='regime')

# ─── Signal generators ────────────────────────────────────────────────────────
def gen_mr(sym):
    """Mean Reversion — 30m — SIDEWAYS regime. SL=2×ATR, TP=BB_mid."""
    df = _load(sym, '30m').copy()
    df['regime'] = load_regime(sym, '30m').reindex(df.index).fillna(-1).astype(int)
    df['rsi']  = _rsi(df.close)
    bbu, bbm, bbl = _bband(df.close)
    df['bbu'] = bbu; df['bbm'] = bbm; df['bbl'] = bbl
    df['rsi1'] = df['rsi'].shift(1)
    df['atr']  = _atr(df.high, df.low, df.close)
    bbr = (df.bbu - df.bbl).replace(0, np.nan)
    sw  = (df.regime == SIDEWAYS)
    long_c  = sw & (df.rsi < 30) & ((df.close - df.bbl) / bbr <= 0.10) & (df.rsi > df.rsi1)
    short_c = sw & (df.rsi > 70) & ((df.bbu - df.close) / bbr <= 0.10) & (df.rsi < df.rsi1)
    idx = df.index
    c_v = df.close.values; a_v = df.atr.values; m_v = df.bbm.values; o_v = df.open.values
    events = []
    for i in range(len(df) - 1):
        ep = o_v[i+1]; ai = a_v[i]; ci = c_v[i]; mi = m_v[i]
        if long_c.iloc[i]:
            sl = ci - 2.0*ai; tp = mi
            if sl < ep < tp:
                events.append((idx[i+1], sym, 'MR', 1, ep, sl, tp))
        if short_c.iloc[i]:
            sl = ci + 2.0*ai; tp = mi
            if sl > ep > tp:
                events.append((idx[i+1], sym, 'MR', -1, ep, sl, tp))
    return events

def gen_pbl(sym):
    """Pullback Long — 30m — BULL_TREND regime. SL=2.5×ATR, TP=3.0×ATR."""
    df   = _load(sym, '30m').copy()
    df4h = _load(sym, '4h').copy()
    df['regime'] = load_regime(sym, '30m').reindex(df.index).fillna(-1).astype(int)
    df4h['e50_4h'] = _ema(df4h.close, 50)
    df4h_m = df4h[['close', 'e50_4h']].rename(columns={'close': 'c4h'})
    df = pd.merge_asof(df.sort_index(), df4h_m.sort_index(),
                       left_index=True, right_index=True, direction='backward')
    df['atr_'] = _atr(df.high, df.low, df.close)
    df['rsi_'] = _rsi(df.close)
    df['e50']  = _ema(df.close, 50)
    bull = (df.regime == BULL_TREND)
    prox = (df.close - df.e50).abs() <= 0.5 * df.atr_
    body = (df.close - df.open).abs()
    lw   = df[['open','close']].min(axis=1) - df.low
    uw   = df.high - df[['open','close']].max(axis=1)
    rej  = (df.close > df.open) & (lw > uw) & (lw > body)
    htf  = df.c4h > df.e50_4h
    sig  = bull & prox & rej & (df.rsi_ > 40) & htf
    idx = df.index
    c_v = df.close.values; a_v = df.atr_.values; o_v = df.open.values; sv = sig.values
    events = []
    for i in range(len(df) - 1):
        if sv[i]:
            ep = o_v[i+1]; ai = a_v[i]; ci = c_v[i]
            sl = ci - 2.5*ai; tp = ci + 3.0*ai
            if sl < ep < tp:
                events.append((idx[i+1], sym, 'PBL', 1, ep, sl, tp))
    return events

def gen_slc(sym, tp_mult=2.0):
    """Swing Low Continuation — 1h — BEAR_TREND regime.
    SL=2.5×ATR, TP=tp_mult×ATR below entry.
    tp_mult=2.0 → locked definition; tp_mult=3.0 → H2 hypothesis.
    """
    df = _load(sym, '1h').copy()
    df['regime'] = load_regime(sym, '1h').reindex(df.index).fillna(-1).astype(int)
    df['atr_']   = _atr(df.high, df.low, df.close)
    df['adx_']   = _adx(df.high, df.low, df.close)
    df['sw10']   = df.close.shift(1).rolling(10).min()
    bear    = (df.regime == BEAR_TREND)
    short_c = bear & (df.adx_ >= 28) & (df.close < df.sw10)
    idx = df.index
    c_v = df.close.values; a_v = df.atr_.values; o_v = df.open.values; sc = short_c.values
    events = []
    for i in range(len(df) - 1):
        if sc[i]:
            ep = o_v[i+1]; ai = a_v[i]; ci = c_v[i]
            sl = ci + 2.5*ai
            tp = ci - tp_mult * ai
            if sl > ep > tp:
                events.append((idx[i+1], sym, 'SLC', -1, ep, sl, tp))
    return events

# ─── Portfolio simulation ─────────────────────────────────────────────────────
def simulate(all_events, cost_per_side=0.0, sizing_mode='equity'):
    """
    sizing_mode='equity'      → size = min(equity * POS_FRAC, cash)
    sizing_mode='fixed_dollar'→ size = min(CAPITAL * POS_FRAC, cash)  [= $20k fixed]
    """
    by_time = defaultdict(list)
    for ts, sym, strat, direction, ep, sl, tp in all_events:
        by_time[ts].append({'symbol': sym, 'strategy': strat, 'direction': direction,
                            'entry_price': ep, 'sl': sl, 'tp': tp})

    # Pre-load high/low bars for all symbols on 30m grid
    hl = {}
    master_set = set()
    for sym in SYMBOLS:
        df = _load(sym, '30m')
        hl[sym] = {ts: (row['high'], row['low']) for ts, row in df[['high','low']].iterrows()}
        master_set.update(df.index)
    master = sorted(master_set)

    cash = CAPITAL
    positions = []
    trades    = []
    eq_curve  = []
    COST      = cost_per_side

    for ts in master:
        # ── Close open positions that hit SL or TP ──
        to_close = []
        for pos in positions:
            bar = hl[pos['symbol']].get(ts)
            if bar is None:
                continue
            hi, lo = bar
            d = pos['direction']; slp = pos['sl']; tpp = pos['tp']
            hit_type = hit_price = None
            if d == 1:
                if   lo <= slp: hit_type, hit_price = 'SL', slp
                elif hi >= tpp: hit_type, hit_price = 'TP', tpp
            else:
                if   hi >= slp: hit_type, hit_price = 'SL', slp
                elif lo <= tpp: hit_type, hit_price = 'TP', tpp
            if hit_type:
                ep_ = pos['entry_price']; sz = pos['size_usd']
                gpnl = sz * (hit_price - ep_) / ep_ * d
                efee = sz * COST
                cash += sz + gpnl - efee
                trades.append({
                    'symbol': pos['symbol'], 'strategy': pos['strategy'],
                    'direction': d, 'entry_time': pos['entry_time'], 'exit_time': ts,
                    'entry_price': ep_, 'exit_price': hit_price, 'exit_type': hit_type,
                    'size_usd': sz, 'gross_pnl': round(gpnl, 4),
                    'entry_fee': pos['entry_fee'], 'exit_fee': round(efee, 4),
                    'net_pnl': round(gpnl - pos['entry_fee'] - efee, 4)})
                to_close.append(pos)
        for p in to_close:
            positions.remove(p)

        # ── Enter new positions at bar open ──
        if ts in by_time:
            for sig in by_time[ts]:
                deployed = sum(p['size_usd'] for p in positions)
                equity   = cash + deployed
                if equity <= 0:
                    continue
                heat = deployed / equity
                if heat + POS_FRAC > MAX_HEAT + 1e-9:
                    continue
                if len(positions) >= MAX_POS:
                    continue
                if sum(1 for p in positions if p['symbol'] == sig['symbol']) >= MAX_SYM:
                    continue
                # Sizing: equity-fraction vs fixed-dollar
                if sizing_mode == 'fixed_dollar':
                    target_size = CAPITAL * POS_FRAC   # always $20,000
                else:
                    target_size = equity * POS_FRAC    # grows with equity
                size_usd = min(target_size, cash)
                if size_usd < 5:
                    continue
                efee  = size_usd * COST
                cash -= (size_usd + efee)
                positions.append({
                    'symbol': sig['symbol'], 'strategy': sig['strategy'],
                    'direction': sig['direction'], 'entry_time': ts,
                    'entry_price': sig['entry_price'], 'sl': sig['sl'], 'tp': sig['tp'],
                    'size_usd': size_usd, 'entry_fee': efee})

        # ── Record equity snapshot ──
        deployed = sum(p['size_usd'] for p in positions)
        eq_curve.append({'time': ts, 'equity': cash + deployed,
                         'n_positions': len(positions)})

    # ── Force-close remaining open positions at last bar close ──
    lc = {sym: _load(sym, '30m').iloc[-1]['close'] for sym in SYMBOLS}
    for pos in positions:
        c_ = lc.get(pos['symbol'])
        if c_ is None:
            continue
        ep_ = pos['entry_price']; sz = pos['size_usd']; d = pos['direction']
        gpnl = sz * (c_ - ep_) / ep_ * d
        efee = sz * COST
        cash += sz + gpnl - efee
        trades.append({
            'symbol': pos['symbol'], 'strategy': pos['strategy'], 'direction': d,
            'entry_time': pos['entry_time'], 'exit_time': master[-1],
            'entry_price': ep_, 'exit_price': c_, 'exit_type': 'EXPIRY',
            'size_usd': sz, 'gross_pnl': round(gpnl, 4),
            'entry_fee': pos['entry_fee'], 'exit_fee': round(efee, 4),
            'net_pnl': round(gpnl - pos['entry_fee'] - efee, 4)})

    return trades, eq_curve

# ─── Metrics ──────────────────────────────────────────────────────────────────
def metrics(trades, eq_curve, label):
    df_t = pd.DataFrame(trades)
    df_e = pd.DataFrame(eq_curve).set_index('time')
    fe  = df_e['equity'].iloc[-1]
    yrs = (T_END - T_START).days / 365.25
    cagr = (fe / CAPITAL) ** (1 / yrs) - 1
    wins = df_t.loc[df_t.net_pnl > 0, 'net_pnl'].sum()
    loss = df_t.loc[df_t.net_pnl < 0, 'net_pnl'].abs().sum()
    pf   = wins / loss if loss > 0 else float('inf')
    wr   = (df_t.net_pnl > 0).mean()
    eq   = df_e['equity']
    mdd  = ((eq - eq.cummax()) / eq.cummax()).min()
    fees = (df_t.entry_fee + df_t.exit_fee).sum()
    avg_np = df_e['n_positions'].mean()
    avg_heat = avg_np * POS_FRAC  # approx for equity mode; exact for fixed mode

    print(f'\n  ── {label} ──')
    print(f'  Period:        2022-03-22 → 2026-03-21  (4.00 years)')
    print(f'  Trades:        {len(df_t):,}')
    print(f'  Final equity:  ${fe:>12,.2f}')
    print(f'  Total return:  {(fe/CAPITAL-1)*100:>8.2f}%')
    print(f'  CAGR:          {cagr*100:>8.2f}%  [target: ~50.8%]')
    print(f'  PF:            {pf:>8.4f}  [target: ~1.361]')
    print(f'  WR:            {wr*100:>8.2f}%')
    print(f'  MaxDD:         {mdd*100:>8.2f}%  [target: ~-29%]')
    print(f'  Total fees:    ${fees:>12,.2f}')
    print(f'  Avg positions: {avg_np:.2f}')
    for strat, grp in df_t.groupby('strategy'):
        sw  = grp.loc[grp.net_pnl > 0, 'net_pnl'].sum()
        sl  = grp.loc[grp.net_pnl < 0, 'net_pnl'].abs().sum()
        spf = sw / sl if sl > 0 else float('inf')
        print(f'    {strat:<5}  n={len(grp):4d}  WR={((grp.net_pnl>0).mean()*100):.1f}%  '
              f'PF={spf:.4f}  pnl=${grp.net_pnl.sum():>10,.2f}')
    return {'label': label, 'cagr': cagr, 'pf': pf, 'wr': wr, 'mdd': mdd,
            'n': len(df_t), 'fe': fe, 'fees': fees, 'avg_pos': avg_np}

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    SEP = '=' * 72
    print(SEP)
    print('  MR+PBL+SLC v4.0 — Phase 5 Gap Investigation')
    print('  Period: 2022-03-22 → 2026-03-21  (4.00 years)')
    print('  Capital: $100,000  |  Fees: ZERO (all runs)')
    print(SEP)

    # ── Generate signals — two sets (TP=2×ATR and TP=3×ATR for SLC) ──
    print('\nGenerating signals...')
    base_events_tp2 = []   # SLC TP=2.0×ATR (locked definition)
    base_events_tp3 = []   # SLC TP=3.0×ATR (H2 hypothesis)

    for sym, strats in ASSET_STRATEGIES.items():
        if 'MR'  in strats:
            ev = gen_mr(sym)
            base_events_tp2.extend(ev)
            base_events_tp3.extend(ev)
            print(f'  {sym} MR:  {len(ev):5d} signals')
        if 'PBL' in strats:
            ev = gen_pbl(sym)
            base_events_tp2.extend(ev)
            base_events_tp3.extend(ev)
            print(f'  {sym} PBL: {len(ev):5d} signals')
        if 'SLC' in strats:
            ev2 = gen_slc(sym, tp_mult=2.0)
            ev3 = gen_slc(sym, tp_mult=3.0)
            base_events_tp2.extend(ev2)
            base_events_tp3.extend(ev3)
            print(f'  {sym} SLC(TP=2×ATR): {len(ev2):5d}   SLC(TP=3×ATR): {len(ev3):5d}')

    base_events_tp2.sort(key=lambda x: x[0])
    base_events_tp3.sort(key=lambda x: x[0])
    print(f'\n  Total events  TP=2×ATR: {len(base_events_tp2):,}   TP=3×ATR: {len(base_events_tp3):,}')

    # ── Run 4 combinations (all zero fees) ──
    results = []

    print('\n' + '─'*72)
    print('Run A — Baseline (equity-fraction sizing, SLC TP=2×ATR)')
    print('         [Should reproduce v3 zero-fee result]')
    t, eq = simulate(base_events_tp2, cost_per_side=0.0, sizing_mode='equity')
    results.append(metrics(t, eq, 'A: equity-frac  TP=2×ATR [v3 baseline]'))

    print('\n' + '─'*72)
    print('Run B — H1: Fixed-dollar sizing ($20k/trade), SLC TP=2×ATR')
    t, eq = simulate(base_events_tp2, cost_per_side=0.0, sizing_mode='fixed_dollar')
    results.append(metrics(t, eq, 'B: fixed-$20k   TP=2×ATR [H1]'))

    print('\n' + '─'*72)
    print('Run C — H2: Equity-fraction sizing, SLC TP=3×ATR')
    t, eq = simulate(base_events_tp3, cost_per_side=0.0, sizing_mode='equity')
    results.append(metrics(t, eq, 'C: equity-frac  TP=3×ATR [H2]'))

    print('\n' + '─'*72)
    print('Run D — H3: Fixed-dollar sizing ($20k/trade) + SLC TP=3×ATR')
    t, eq = simulate(base_events_tp3, cost_per_side=0.0, sizing_mode='fixed_dollar')
    r_d = metrics(t, eq, 'D: fixed-$20k   TP=3×ATR [H1+H2]')
    results.append(r_d)
    pd.DataFrame(t).to_csv(OUT_DIR / 'mr_pbl_slc_v4d_trades.csv', index=False)

    # ── Summary ──
    print('\n' + SEP)
    print('  SUMMARY — Period: 2022-03-22 → 2026-03-21 (4.00 years), $100k, zero fees')
    print(SEP)
    print(f'  {"Run":<36} {"CAGR":>8} {"PF":>8} {"MaxDD":>8} {"Trades":>7} {"AvgPos":>7}')
    print('  ' + '─'*78)
    for r in results:
        print(f'  {r["label"]:<36} {r["cagr"]*100:>8.2f}% {r["pf"]:>8.4f} '
              f'{r["mdd"]*100:>8.2f}% {r["n"]:>7,} {r["avg_pos"]:>7.2f}')
    print(f'  {"TARGET":<36} {"~50.8%":>8} {"~1.361":>8} {"~-29%":>8} {"~2116":>7} {"~3.25":>7}')
    print()

    # ── Save summary JSON ──
    out = []
    for r in results:
        out.append({k: (float(v) if isinstance(v, (np.floating, float)) else v)
                    for k, v in r.items()})
    with open(OUT_DIR / 'mr_pbl_slc_v4_summary.json', 'w') as f:
        json.dump({'period': '2022-03-22 to 2026-03-21', 'years': 4.0,
                   'capital': CAPITAL, 'fees': 'zero', 'runs': out}, f, indent=2)
    print(f'  Saved: {OUT_DIR}/mr_pbl_slc_v4_summary.json')
    print(f'  Saved: {OUT_DIR}/mr_pbl_slc_v4d_trades.csv')

if __name__ == '__main__':
    main()
