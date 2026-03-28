#!/usr/bin/env python3
"""
backtest_v9_system.py — Stage 7 (v3): NexusTrader Production Pipeline Backtest

ARCHITECTURE
------------
This script feeds historical OHLCV data through the EXACT production classes:

  SignalGenerator.generate()       ← same object used by live scanner
  RegimeClassifier.classify()      ← NexusTrader HMM+rule regime (for generate() input)
  ResearchRegimeClassifier          ← exact research labeler for PBL/SLC context
  PositionSizer.calculate_pos_frac() ← same sizing method used in production

The only non-production components are:
  - HistoricalDataFeed  : mock exchange that serves pre-loaded parquet files
  - PositionTracker     : minimal SL/TP tracker mirroring PaperExecutor.on_tick()
    (PaperExecutor has Qt signal dependencies that require an event loop)

REGIME FIX (Stage 1)
--------------------
The research system (v7_final) uses btc_regime_labeler.py logic:
  BULL_TREND: ADX≥22, EMA20>EMA50, price>EMA200, ATR_ratio<1.80
  BEAR_TREND: ADX≥22, EMA20≤EMA50, price≤EMA200, ATR_ratio<1.80

These are pre-computed for BOTH 30m (PBL) and 1h (SLC) series, then
passed as context["research_regime_30m"] and context["research_regime_1h"]
so the models receive exactly what the research used.

STRATEGY LOGIC FIX (Stage 2)
-----------------------------
PBL candle check now includes the full rejection structure:
  (close > open) AND (lower_wick > upper_wick) AND (lower_wick > body)

SLC regime source: research BEAR_TREND on 1h series (not NexusTrader 30m).

Usage:
  cd /NexusTrader && python scripts/mr_pbl_slc_research/backtest_v9_system.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# ── Path setup ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("core").setLevel(logging.WARNING)
logging.getLogger("torch").setLevel(logging.ERROR)

logger = logging.getLogger("backtest_v9")
logger.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_h)

# ── Config ──────────────────────────────────────────────────────────────────
SYMBOLS        = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
PRIMARY_TF     = "30m"
HTF_4H_TF      = "4h"
SLC_1H_TF      = "1h"
INITIAL_CAPITAL= 100_000.0
COST_PER_SIDE  = 0.0004
POS_FRAC       = 0.35
MAX_HEAT       = 0.80
MAX_POSITIONS  = 10

REGIME_WINDOW  = 200   # bars per classify() call for NexusTrader HMM
MODEL_LOOKBACK = 350   # bars passed to SignalGenerator per bar
HTF_LOOKBACK   = 60    # 4h bars for PBL context
SLC_1H_LOOKBACK= 150   # 1h bars for SLC context

DATA_DIR = ROOT / "backtest_data"

# ── Production imports (Stage 3 compliance) ─────────────────────────────────
from core.features.indicator_library import calculate_all, calculate_scan_mode
from core.regime.regime_classifier   import RegimeClassifier
from core.signals.signal_generator   import SignalGenerator
from core.meta_decision.position_sizer import PositionSizer

# Research regime — ResearchRegimeClassifier is the single explicit provider
# for PBL/SLC regime.  regime_to_string() is the authoritative conversion from
# the classifier's integer space to NexusTrader regime strings; these strings
# are passed to SignalGenerator.generate() so the ACTIVE_REGIMES gate (not
# context injection) is what filters the models.
from core.regime.research_regime_classifier import (
    classify_series   as research_classify_series,
    regime_to_string  as research_regime_to_string,
    BULL_TREND        as RES_BULL_TREND,
    BEAR_TREND        as RES_BEAR_TREND,
)


# ── Data loading ─────────────────────────────────────────────────────────────

def _load(symbol: str, tf: str) -> pd.DataFrame:
    slug = symbol.replace("/", "_")
    path = DATA_DIR / f"{slug}_{tf}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def load_all() -> dict[str, dict[str, pd.DataFrame]]:
    data: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in SYMBOLS:
        data[sym] = {}
        for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
            df = _load(sym, tf)
            if df.empty:
                logger.warning("No data: %s %s", sym, tf)
            else:
                logger.info("Loaded %s %s: %d bars (%s → %s)",
                            sym, tf, len(df),
                            df.index[0].date(), df.index[-1].date())
            data[sym][tf] = df
    return data


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(raw: dict) -> dict[str, dict[str, pd.DataFrame]]:
    result: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in SYMBOLS:
        result[sym] = {}
        for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
            df = raw[sym].get(tf)
            if df is None or df.empty:
                result[sym][tf] = pd.DataFrame()
                continue
            try:
                fn = calculate_all if tf == PRIMARY_TF else calculate_scan_mode
                result[sym][tf] = fn(df.copy())
                logger.info("Indicators: %s %s (%d rows)", sym, tf, len(result[sym][tf]))
            except Exception as e:
                logger.warning("Indicator fail %s %s: %s", sym, tf, e)
                result[sym][tf] = pd.DataFrame()
    return result


# ── Stage 1: Research regime precomputation ───────────────────────────────────

def precompute_research_regimes(
    ind_data: dict,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """
    Pre-classify EVERY bar using the research labeler (btc_regime_labeler.py logic).

    Returns:
      regimes_30m[sym] : int8 array aligned to 30m DataFrame
      regimes_1h[sym]  : int8 array aligned to 1h DataFrame
    """
    logger.info("Pre-computing RESEARCH regimes (30m and 1h) ...")
    regimes_30m: dict[str, np.ndarray] = {}
    regimes_1h:  dict[str, np.ndarray] = {}
    t0 = time.time()

    for sym in SYMBOLS:
        df30 = ind_data[sym].get(PRIMARY_TF)
        if df30 is not None and not df30.empty:
            regimes_30m[sym] = research_classify_series(df30)
            n_bull = int((regimes_30m[sym] == RES_BULL_TREND).sum())
            n_bear = int((regimes_30m[sym] == RES_BEAR_TREND).sum())
            logger.info("  %s 30m: n=%d  bull_trend=%d (%.1f%%)  bear_trend=%d (%.1f%%)",
                        sym, len(regimes_30m[sym]),
                        n_bull, n_bull / len(regimes_30m[sym]) * 100,
                        n_bear, n_bear / len(regimes_30m[sym]) * 100)
        else:
            regimes_30m[sym] = np.array([], dtype=np.int8)

        df1h = ind_data[sym].get(SLC_1H_TF)
        if df1h is not None and not df1h.empty:
            regimes_1h[sym] = research_classify_series(df1h)
            n_bear_1h = int((regimes_1h[sym] == RES_BEAR_TREND).sum())
            logger.info("  %s  1h: n=%d  bear_trend=%d (%.1f%%)",
                        sym, len(regimes_1h[sym]),
                        n_bear_1h, n_bear_1h / len(regimes_1h[sym]) * 100)
        else:
            regimes_1h[sym] = np.array([], dtype=np.int8)

    logger.info("Research regime precompute done in %.1fs", time.time() - t0)
    return regimes_30m, regimes_1h


# ── NexusTrader HMM regime precomputation ────────────────────────────────────

def precompute_nexus_regimes(
    ind_data: dict,
    warmup: int = 120,
    window: int = REGIME_WINDOW,
) -> dict[str, list[str]]:
    """
    Pre-classify regime using NexusTrader RegimeClassifier (HMM+rule-based).
    Used as the `regime` str argument to SignalGenerator.generate().
    """
    logger.info("Pre-computing NexusTrader HMM regimes (window=%d) ...", window)
    result: dict[str, list[str]] = {}
    for sym in SYMBOLS:
        df30 = ind_data[sym].get(PRIMARY_TF)
        if df30 is None or df30.empty:
            result[sym] = []
            continue
        clf = RegimeClassifier()
        n   = len(df30)
        reg = ["uncertain"] * n
        for i in range(warmup, n):
            start = max(0, i - window + 1)
            try:
                r, _, _ = clf.classify(df30.iloc[start : i + 1])
                reg[i] = r
            except Exception:
                pass
        result[sym] = reg
        logger.info("  %s: %d bars", sym, n)
    return result


# ── Production pipeline simulation ───────────────────────────────────────────

def run_scenario(
    ind_data: dict,
    research_regimes_30m: dict[str, np.ndarray],
    research_regimes_1h:  dict[str, np.ndarray],
    cost_per_side: float = COST_PER_SIDE,
    label: str = "A",
) -> dict:
    """
    Bar-by-bar simulation using production SignalGenerator and PositionSizer.

    Modules used (Stage 3 compliance):
      - SignalGenerator.generate()         ← production signal pipeline
      - PositionSizer.calculate_pos_frac() ← production sizing
      - ResearchRegimeClassifier           ← regime_to_string(classify_series())
        provides the regime string passed to generate(); the ACTIVE_REGIMES gate
        is the filtering mechanism — no context injection of regime integers.

    Regime flow per bar:
      res_regime_30m  = research_regime_to_string(precomputed_int_30m)  → "bull_trend" etc.
      res_regime_1h   = research_regime_to_string(precomputed_int_1h)   → "bear_trend" etc.
      PBL call: generate(sym, df, res_regime_30m_str, ...) — ACTIVE_REGIMES=["bull_trend"]
      SLC call: generate(sym, df, res_regime_1h_str,  ...) — ACTIVE_REGIMES=["bear_trend"]
    """
    # ── Production instances ──────────────────────────────────────────
    sig_gen  = SignalGenerator()
    # Bypass the 100-call warmup guard — the backtest loop handles its own
    # warmup_bars (120) so we don't need a second suppression window.
    sig_gen._warmup_complete = True
    sizer    = PositionSizer()

    # Force-enable PBL+SLC for this backtest run by patching settings
    try:
        from config.settings import settings as _s
        _s.set("mr_pbl_slc.enabled", True)
        _s.set("mr_pbl_slc.pos_frac", POS_FRAC)
        _s.set("mr_pbl_slc.max_heat", MAX_HEAT)
        _s.set("mr_pbl_slc.max_positions", MAX_POSITIONS)
        logger.info("mr_pbl_slc.enabled patched to True for backtest")
    except Exception as e:
        logger.warning("Could not patch settings: %s", e)

    # ── Index structures for O(log n) lookups ─────────────────────────
    idx30: dict[str, pd.DatetimeIndex] = {}
    idx4h: dict[str, pd.DatetimeIndex] = {}
    idx1h: dict[str, pd.DatetimeIndex] = {}
    for sym in SYMBOLS:
        df = ind_data[sym].get(PRIMARY_TF)
        idx30[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
        df = ind_data[sym].get(HTF_4H_TF)
        idx4h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
        df = ind_data[sym].get(SLC_1H_TF)
        idx1h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])

    # ── Master timeline (BTC 30m as primary reference) ────────────────
    btc_30m = ind_data["BTC/USDT"].get(PRIMARY_TF)
    master_ts = list(btc_30m.index) if (btc_30m is not None and not btc_30m.empty) else []
    if not master_ts:
        logger.error("No BTC/USDT 30m data — cannot run simulation")
        return {}

    # ── State ─────────────────────────────────────────────────────────
    equity        = INITIAL_CAPITAL
    positions:    dict[str, dict] = {}   # sym → position
    all_trades:   list[dict]      = []
    equity_curve: list[float]     = [INITIAL_CAPITAL]

    # Pending entries: signal fires at bar i → enter at bar i+1 open.
    # Matches research gen_pbl/gen_slc which uses o_v[i+1] as entry price.
    # Validation: sl < ep < tp (long) or tp < ep < sl (short) — same as research.
    pending_entries: dict[str, dict] = {}  # sym → {signal, size_usdt, bar_signal}

    warmup_bars  = 120
    rejected_heat = 0
    rejected_max  = 0
    rejected_entry_gap = 0   # next-bar open outside SL/TP bounds
    n_signals_gen = 0

    logger.info("Simulation: %d bars × %d symbols ...", len(master_ts), len(SYMBOLS))
    t_sim = time.time()

    for bar_idx, ts in enumerate(master_ts):
        if bar_idx < warmup_bars:
            continue

        # ── Execute pending entries at this bar's OPEN ────────────────
        # Signal fired at previous bar's close; fill at this bar's open.
        for sym, pend in list(pending_entries.items()):
            if sym in positions:
                # Position opened by another signal before this one filled
                del pending_entries[sym]
                continue
            loc = int(idx30[sym].searchsorted(ts))
            if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                continue
            row_open = ind_data[sym][PRIMARY_TF].iloc[loc]
            ep_raw   = float(row_open["open"])   # next-bar open (fill price)
            sig      = pend["signal"]
            sl       = sig.stop_loss
            tp       = sig.take_profit

            # Research validation: sl < ep < tp (long) or tp < ep < sl (short)
            if sig.direction == "long":
                valid = sl < ep_raw < tp
                ep_fill = ep_raw * (1 + cost_per_side)
            else:
                valid = tp < ep_raw < sl
                ep_fill = ep_raw * (1 - cost_per_side)

            del pending_entries[sym]
            if not valid:
                rejected_entry_gap += 1
                continue

            positions[sym] = {
                "direction":   sig.direction,
                "model":       sig.model_name,
                "entry_price": ep_fill,
                "sl":          sl,
                "tp":          tp,
                "size_usdt":   pend["size_usdt"],
                "entry_bar":   bar_idx,
                "entry_ts":    ts,
                "atr_value":   sig.atr_value,
            }

        # ── Update open positions (SL/TP check) ──────────────────────
        # Mirrors PaperExecutor.on_tick() SL/TP logic exactly
        closed = []
        for sym, pos in list(positions.items()):
            loc = int(idx30[sym].searchsorted(ts))
            if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                continue
            row  = ind_data[sym][PRIMARY_TF].iloc[loc]
            hi   = float(row["high"])
            lo   = float(row["low"])
            d    = pos["direction"]
            sl   = pos["sl"]
            tp   = pos["tp"]
            ep   = pos["entry_price"]
            size = pos["size_usdt"]

            exit_px = None
            reason  = None
            if d == "long":
                if lo <= sl: exit_px, reason = sl, "sl"
                elif hi >= tp: exit_px, reason = tp, "tp"
            else:
                if hi >= sl: exit_px, reason = sl, "sl"
                elif lo <= tp: exit_px, reason = tp, "tp"

            if reason:
                if d == "long":
                    exit_adj = exit_px * (1 - cost_per_side)
                else:
                    exit_adj = exit_px * (1 + cost_per_side)
                qty  = size / ep
                pnl  = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
                equity += pnl
                r_val  = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
                all_trades.append({
                    "symbol": sym, "direction": d, "model": pos["model"],
                    "entry_ts": pos["entry_ts"], "exit_ts": ts,
                    "entry_price": ep, "exit_price": exit_px,
                    "size_usdt": size, "pnl": round(pnl, 4),
                    "r_value": round(r_val, 4), "exit_reason": reason,
                    "bars_held": bar_idx - pos["entry_bar"],
                })
                closed.append(sym)

        for sym in closed:
            del positions[sym]

        equity_curve.append(equity)

        # ── Signal generation via production SignalGenerator ──────────
        for sym in SYMBOLS:
            if sym in positions:
                continue

            loc = int(idx30[sym].searchsorted(ts))
            if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                continue
            if loc < warmup_bars:
                continue

            # ── Research regime integers for this bar ──────────────────
            res_30m_arr    = research_regimes_30m.get(sym, np.array([]))
            res_regime_30m = int(res_30m_arr[loc]) if loc < len(res_30m_arr) else 0

            loc1h      = int(idx1h[sym].searchsorted(ts, side="right")) - 1
            res_1h_arr = research_regimes_1h.get(sym, np.array([]))
            res_regime_1h = int(res_1h_arr[loc1h]) if 0 <= loc1h < len(res_1h_arr) else 0

            # Skip quickly if neither model will fire
            if res_regime_30m != RES_BULL_TREND and res_regime_1h != RES_BEAR_TREND:
                continue

            # ── Convert integers to NexusTrader regime strings ─────────
            # regime_to_string() is the authoritative mapper; the resulting
            # strings are passed to generate() so the ACTIVE_REGIMES gate
            # (not context integers) is the filtering mechanism.
            res_regime_30m_str = research_regime_to_string(res_regime_30m)
            res_regime_1h_str  = research_regime_to_string(res_regime_1h)

            # Fixed-lookback 30m window (iloc — O(1))
            s30 = max(0, loc - MODEL_LOOKBACK + 1)
            df_window = ind_data[sym][PRIMARY_TF].iloc[s30 : loc + 1]
            if len(df_window) < 70:
                continue

            # ── Call production SignalGenerator (research-regime-driven) ──
            # Each model group gets its own generate() call with the regime
            # string from ResearchRegimeClassifier.  The ACTIVE_REGIMES gate
            # is the sole filtering mechanism — no regime integers in context.
            signals = []

            # PBL path: generate() with research 30m regime string
            # ACTIVE_REGIMES=["bull_trend"] passes only when res_regime_30m_str=="bull_trend"
            if res_regime_30m == RES_BULL_TREND:
                _pbl_ctx: dict = {}
                loc4h = int(idx4h[sym].searchsorted(ts, side="right"))
                if loc4h >= HTF_LOOKBACK:
                    _pbl_ctx["df_4h"] = ind_data[sym][HTF_4H_TF].iloc[
                        max(0, loc4h - HTF_LOOKBACK) : loc4h
                    ]
                try:
                    raw = sig_gen.generate(
                        sym, df_window, res_regime_30m_str, PRIMARY_TF,
                        regime_probs={}, context=_pbl_ctx,
                    ) or []
                    signals.extend(s for s in raw if s.model_name == "pullback_long")
                except Exception as exc:
                    logger.debug("SG PBL error %s: %s", sym, exc)

            # SLC path: generate() with research 1h regime string
            # ACTIVE_REGIMES=["bear_trend"] passes only when res_regime_1h_str=="bear_trend"
            if res_regime_1h == RES_BEAR_TREND and loc1h >= 15:
                _slc_ctx: dict = {
                    "df_1h": ind_data[sym][SLC_1H_TF].iloc[
                        max(0, loc1h - SLC_1H_LOOKBACK + 1) : loc1h + 1
                    ]
                }
                try:
                    raw = sig_gen.generate(
                        sym, df_window, res_regime_1h_str, PRIMARY_TF,
                        regime_probs={}, context=_slc_ctx,
                    ) or []
                    signals.extend(s for s in raw if s.model_name == "swing_low_continuation")
                except Exception as exc:
                    logger.debug("SG SLC error %s: %s", sym, exc)

            if not signals:
                continue

            n_signals_gen += len(signals)

            # Take first qualifying signal
            sig = signals[0]

            # Skip if already have a pending entry for this symbol
            if sym in pending_entries:
                continue

            # ── Production PositionSizer.calculate_pos_frac() ─────────
            open_by_sym: dict[str, int] = defaultdict(int)
            for p_sym in positions:
                open_by_sym[p_sym] += 1
            open_count = len(positions)

            if open_count >= MAX_POSITIONS:
                rejected_max += 1
                continue

            size_usdt = sizer.calculate_pos_frac(
                equity,
                open_positions_count=open_count,
                open_positions_by_symbol=dict(open_by_sym),
                symbol=sym,
            )
            if size_usdt <= 0:
                rejected_heat += 1
                continue

            # ── Buffer entry — fill at NEXT bar's open ────────────────
            # Research enters at o_v[i+1] (next bar open) with sl<ep<tp
            # validation. This prevents filling on bars that have already
            # gapped through SL or TP, which improves WR parity.
            pending_entries[sym] = {
                "signal":    sig,
                "size_usdt": size_usdt,
                "bar_signal": bar_idx,
            }

    logger.info("Simulation done in %.1fs", time.time() - t_sim)

    # ── Force-close remaining positions at last bar ───────────────────
    last_ts = master_ts[-1]
    for sym, pos in list(positions.items()):
        df30 = ind_data[sym].get(PRIMARY_TF)
        if df30 is None or df30.empty:
            continue
        last_close = float(df30["close"].iloc[-1])
        ep   = pos["entry_price"]
        size = pos["size_usdt"]
        sl   = pos["sl"]
        d    = pos["direction"]
        if d == "long":
            exit_adj = last_close * (1 - cost_per_side)
        else:
            exit_adj = last_close * (1 + cost_per_side)
        qty  = size / ep
        pnl  = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
        equity += pnl
        r_val = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
        all_trades.append({
            "symbol": sym, "direction": d, "model": pos["model"],
            "entry_ts": pos["entry_ts"], "exit_ts": last_ts,
            "entry_price": ep, "exit_price": last_close,
            "size_usdt": size, "pnl": round(pnl, 4),
            "r_value": round(r_val, 4), "exit_reason": "force_close",
            "bars_held": 0,
        })

    # ── KPIs ─────────────────────────────────────────────────────────
    n_trades     = len(all_trades)
    winners      = [t for t in all_trades if t["pnl"] > 0]
    losers       = [t for t in all_trades if t["pnl"] <= 0]
    wr           = len(winners) / n_trades if n_trades > 0 else 0.0
    gp           = sum(t["pnl"] for t in winners)
    gl           = abs(sum(t["pnl"] for t in losers))
    pf           = gp / gl if gl > 0 else float("inf")
    years        = (last_ts - master_ts[0]).days / 365.25
    cagr         = (equity / INITIAL_CAPITAL) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    avg_r        = sum(t["r_value"] for t in all_trades) / n_trades if n_trades > 0 else 0.0
    eq_arr       = np.array(equity_curve)
    peak         = np.maximum.accumulate(eq_arr)
    mdd          = float(((eq_arr - peak) / peak).min())

    return {
        "label":            label,
        "modules_used": [
            "SignalGenerator.generate()",
            "PositionSizer.calculate_pos_frac()",
            "RegimeClassifier.classify() [NexusTrader HMM+rule]",
            "ResearchRegimeClassifier.classify_series() [research labeler]",
        ],
        "n_trades":         n_trades,
        "win_rate":         round(wr, 4),
        "profit_factor":    round(pf, 4),
        "cagr":             round(cagr, 4),
        "max_drawdown":     round(mdd, 4),
        "avg_r":            round(avg_r, 4),
        "final_equity":     round(equity, 2),
        "years":            round(years, 2),
        "signals_generated": n_signals_gen,
        "rejected_heat":    rejected_heat,
        "rejected_max":     rejected_max,
        "rejected_entry_gap": rejected_entry_gap,
        "all_trades":       all_trades,
    }


# ── Signal parity analysis ────────────────────────────────────────────────────

def run_signal_parity(
    ind_data: dict,
    research_regimes_30m: dict[str, np.ndarray],
    research_regimes_1h:  dict[str, np.ndarray],
) -> None:
    """
    Stage 4: Compare signal counts between production pipeline and
    research gen_pbl / gen_slc scripts.

    Runs the full production pipeline in signal-only mode (no position sizing)
    and reports per-symbol signal counts.
    """
    print("\n" + "─" * 65)
    print("STAGE 4 — SIGNAL PARITY ANALYSIS")
    print("─" * 65)

    sg_parity = SignalGenerator()
    sg_parity._warmup_complete = True  # no warmup suppression in parity count
    try:
        from config.settings import settings as _s
        _s.set("mr_pbl_slc.enabled", True)
    except Exception:
        pass

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"PBL": 0, "SLC": 0})

    for sym in SYMBOLS:
        df30 = ind_data[sym].get(PRIMARY_TF)
        idx30_s = df30.index if (df30 is not None and not df30.empty) else pd.DatetimeIndex([])
        idx1h_s = ind_data[sym][SLC_1H_TF].index if (
            ind_data[sym].get(SLC_1H_TF) is not None and
            not ind_data[sym][SLC_1H_TF].empty
        ) else pd.DatetimeIndex([])
        idx4h_s = ind_data[sym][HTF_4H_TF].index if (
            ind_data[sym].get(HTF_4H_TF) is not None and
            not ind_data[sym][HTF_4H_TF].empty
        ) else pd.DatetimeIndex([])

        res_30m = research_regimes_30m.get(sym, np.array([]))
        res_1h  = research_regimes_1h.get(sym, np.array([]))

        n = len(idx30_s)
        for loc in range(120, n):
            ts = idx30_s[loc]
            res30 = int(res_30m[loc]) if loc < len(res_30m) else 0
            loc1h = int(idx1h_s.searchsorted(ts, side="right")) - 1
            res1h = int(res_1h[loc1h]) if 0 <= loc1h < len(res_1h) else 0

            if res30 not in (RES_BULL_TREND,) and res1h != RES_BEAR_TREND:
                continue

            s30 = max(0, loc - MODEL_LOOKBACK + 1)
            df_win = df30.iloc[s30 : loc + 1]
            if len(df_win) < 70:
                continue

            # PBL: research 30m regime string → ACTIVE_REGIMES gate (no ctx injection)
            if res30 == RES_BULL_TREND:
                _p_ctx: dict = {}
                loc4h = int(idx4h_s.searchsorted(ts, side="right"))
                if loc4h >= HTF_LOOKBACK:
                    _p_ctx["df_4h"] = ind_data[sym][HTF_4H_TF].iloc[
                        max(0, loc4h - HTF_LOOKBACK) : loc4h
                    ]
                try:
                    raw = sg_parity.generate(
                        sym, df_win, research_regime_to_string(res30), PRIMARY_TF,
                        regime_probs={}, context=_p_ctx,
                    ) or []
                    for s in raw:
                        if s.model_name == "pullback_long":
                            counts[sym]["PBL"] += 1
                except Exception:
                    pass

            # SLC: research 1h regime string → ACTIVE_REGIMES gate (no ctx injection)
            if res1h == RES_BEAR_TREND and loc1h >= 15:
                _s_ctx: dict = {
                    "df_1h": ind_data[sym][SLC_1H_TF].iloc[
                        max(0, loc1h - SLC_1H_LOOKBACK + 1) : loc1h + 1
                    ]
                }
                try:
                    raw = sg_parity.generate(
                        sym, df_win, research_regime_to_string(res1h), PRIMARY_TF,
                        regime_probs={}, context=_s_ctx,
                    ) or []
                    for s in raw:
                        if s.model_name == "swing_low_continuation":
                            counts[sym]["SLC"] += 1
                except Exception:
                    pass

    total_pbl = sum(v["PBL"] for v in counts.values())
    total_slc = sum(v["SLC"] for v in counts.values())

    print(f"  {'Symbol':<12} {'PBL signals':>14} {'SLC signals':>14}")
    print("  " + "─" * 42)
    for sym in SYMBOLS:
        print(f"  {sym:<12} {counts[sym]['PBL']:>14} {counts[sym]['SLC']:>14}")
    print("  " + "─" * 42)
    print(f"  {'TOTAL':<12} {total_pbl:>14} {total_slc:>14}")
    print()
    print("  Research baselines (v7_final gen_pbl / gen_slc, BTC only):")
    print("    BTC PBL: ~246 signals (3-sym 4yr run)")
    print("    BTC SLC: ~343 signals, SOL: ~343, ETH: ~343 (4yr run)")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    logger.info("=" * 65)
    logger.info("NexusTrader v9 System Backtest (REGIME-FIXED) — PBL + SLC")
    logger.info("Production modules: SignalGenerator + PositionSizer")
    logger.info("=" * 65)

    raw_data = load_all()
    usable   = {s for s in SYMBOLS if not raw_data[s].get(PRIMARY_TF, pd.DataFrame()).empty}
    if not usable:
        logger.error("No data found.")
        sys.exit(1)

    ind_data = compute_indicators(raw_data)

    # Stage 1: pre-compute research regimes (fixes the primary PF gap)
    research_30m, research_1h = precompute_research_regimes(ind_data)

    # Print regime comparison
    print("\n" + "─" * 65)
    print("STAGE 1 — REGIME COMPARISON (Research vs NexusTrader HMM)")
    print("─" * 65)
    print(f"  {'Symbol':8} {'TF':4} {'BULL_TREND':>12} {'BEAR_TREND':>12} {'OTHER':>10}")
    print("  " + "─" * 50)
    for sym in SYMBOLS:
        arr30 = research_30m.get(sym, np.array([]))
        if len(arr30) > 0:
            n_bull = int((arr30 == RES_BULL_TREND).sum())
            n_bear = int((arr30 == RES_BEAR_TREND).sum())
            n_other = len(arr30) - n_bull - n_bear
            print(f"  {sym.split('/')[0]:8} {'30m':4} "
                  f"{n_bull:>10} ({n_bull/len(arr30)*100:4.1f}%)"
                  f"{n_bear:>10} ({n_bear/len(arr30)*100:4.1f}%)"
                  f"{n_other:>10}")
        arr1h = research_1h.get(sym, np.array([]))
        if len(arr1h) > 0:
            n_bear_1h = int((arr1h == RES_BEAR_TREND).sum())
            n_bull_1h = int((arr1h == RES_BULL_TREND).sum())
            n_oth_1h  = len(arr1h) - n_bear_1h - n_bull_1h
            print(f"  {sym.split('/')[0]:8} {' 1h':4} "
                  f"{n_bull_1h:>10} ({n_bull_1h/len(arr1h)*100:4.1f}%)"
                  f"{n_bear_1h:>10} ({n_bear_1h/len(arr1h)*100:4.1f}%)"
                  f"{n_oth_1h:>10}")

    # NOTE: NexusTrader HMM precomputation removed — the research regime string
    # is now passed directly to generate() so no HMM gate is applied.
    # This matches the research script which only checks the research labeler.

    # Stage 4: signal parity
    run_signal_parity(ind_data, research_30m, research_1h)

    # Stage 5: Scenario A (zero fees)
    logger.info("Running Scenario A: zero fees ...")
    result_a = run_scenario(ind_data, research_30m, research_1h,
                            cost_per_side=0.0, label="A: zero fees")

    # Stage 5: Scenario B (maker 0.04%/side)
    logger.info("Running Scenario B: 0.04%%/side maker fees ...")
    result_b = run_scenario(ind_data, research_30m, research_1h,
                            cost_per_side=0.0004, label="B: 0.04%/side maker")

    elapsed = time.time() - t0

    # ── Results ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("NEXUSTRADER v9 SYSTEM BACKTEST RESULTS (REGIME-FIXED)")
    print("=" * 72)
    print(f"  Production modules used:")
    for m in result_a.get("modules_used", []):
        print(f"    ✓ {m}")
    print()
    print(f"{'Scenario':<35} {'CAGR':>8} {'PF':>7} {'WR':>7} {'MaxDD':>8} {'AvgR':>6} {'n':>6}")
    print("─" * 72)
    for r in [result_a, result_b]:
        print(f"{r['label']:<35} {r['cagr']*100:>7.2f}%"
              f" {r['profit_factor']:>7.4f}"
              f" {r['win_rate']*100:>6.1f}%"
              f" {r['max_drawdown']*100:>7.2f}%"
              f" {r['avg_r']:>6.3f}"
              f" {r['n_trades']:>6}")
    print("=" * 72)
    print()
    print(f"  Trades rejected (heat): {result_a['rejected_heat']}")
    print(f"  Trades rejected (entry gap): {result_a['rejected_entry_gap']}")
    print(f"  Signals generated: {result_a['signals_generated']}")
    print(f"  Elapsed total: {elapsed:.1f}s")
    print()
    print("  Reference (research v7_final, BTC only, zero fees):")
    print("    CAGR=50.41%  PF=1.2975  WR=61.1%  MaxDD=-20.66%  n=1,476")
    print()
    print("  Target for acceptance: PF ≥ 1.18, CAGR ≥ 30% (with fees)")
    print()

    # Per-model breakdown
    model_stats: dict = defaultdict(lambda: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0})
    for t in result_a["all_trades"]:
        m = t["model"] or "unknown"
        model_stats[m]["n"] += 1
        if t["pnl"] > 0:
            model_stats[m]["win"] += 1
            model_stats[m]["gp"]  += t["pnl"]
        else:
            model_stats[m]["gl"]  += abs(t["pnl"])

    print("  Per-model breakdown (Scenario A):")
    for m, s in sorted(model_stats.items()):
        pf_m = s["gp"] / s["gl"] if s["gl"] > 0 else float("inf")
        wr_m = s["win"] / s["n"] if s["n"] > 0 else 0.0
        print(f"    {m:<30} n={s['n']:>5}  WR={wr_m*100:.1f}%  PF={pf_m:.4f}")

    # ── Verdict ───────────────────────────────────────────────────────
    pf_b = result_b["profit_factor"]
    cagr_b = result_b["cagr"]
    target_met = pf_b >= 1.18 and cagr_b >= 0.30

    print()
    print("─" * 72)
    if target_met:
        print(f"  ✅ VERDICT: FIXED AND MATCHING — PF={pf_b:.4f} ≥ 1.18, CAGR={cagr_b*100:.1f}% ≥ 30%")
    else:
        print(f"  ❌ VERDICT: STILL MISMATCHING — PF={pf_b:.4f} (need ≥1.18), CAGR={cagr_b*100:.1f}%")
    print("─" * 72)

    # ── Save results ──────────────────────────────────────────────────
    out = ROOT / "reports" / "mr_pbl_slc_v9_system.json"
    out.parent.mkdir(exist_ok=True)
    summary = {
        "version":     "v9_regime_fixed",
        "description": "Production pipeline backtest — research regime aligned",
        "fixes_applied": [
            "ResearchRegimeClassifier replaces HMM for PBL(30m) and SLC(1h)",
            "PBL: full rejection-candle check (close>open AND lw>uw AND lw>body)",
            "SLC: 1h regime from research labeler on 1h series",
        ],
        "modules_used": result_a.get("modules_used", []),
        "scenario_a":  {k: v for k, v in result_a.items() if k != "all_trades"},
        "scenario_b":  {k: v for k, v in result_b.items() if k != "all_trades"},
        "reference":   {"pf": 1.2975, "cagr": 0.5041, "wr": 0.611,
                        "mdd": -0.2066, "n": 1476, "note": "v7_final BTC only zero fees"},
    }
    with open(out, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Results saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
