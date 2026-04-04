#!/usr/bin/env python3
"""
Phase 2c Step 2c — Combined System Validation
(PBL + SLC + Enhancement Layer + RangeBreakout)

Validates that adding RangeBreakout to the PBL+SLC system does not degrade
combined portfolio quality, and measures the interaction effects.

Scenarios:
  1. BASELINE:        PBL + SLC only (no enhancement, no RangeBreakout)
  2. NO_BUFFER:       PBL + SLC + enhancement + RangeBreakout (entry_buffer_atr=0.0)
  3. WITH_BUFFER:     PBL + SLC + enhancement + RangeBreakout (entry_buffer_atr=0.1)

Pass criteria:
  - Combined PF ≥ baseline (1.2758)
  - MaxDD not unacceptably worse than baseline
  - SLC trade count ≥ baseline (no crowding)
  - PBL trade count stable (± small tolerance)
  - Anti-amplification: Mode B disabled when breakout_active=True

Reports:
  - Combined PF, CAGR, MaxDD, total trades
  - Per-model trade counts (PBL, SLC, RB)
  - SLC/PBL crowding vs baseline
  - Enhancement stats (boosted, relaxed)
  - Anti-amplification enforcement count
  - IS/OOS stability
  - Per-asset breakdown

Cost model: 0.07% per side (0.04% fee + 0.03% slippage)

Usage:
    python scripts/phase2c/step2c_combined_validation.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("step2c_validation")

# Suppress noisy sub-module loggers
for _m in ("core.signals", "core.regime", "core.features", "core.meta_decision", "core.rl"):
    logging.getLogger(_m).setLevel(logging.WARNING)

from research.engine.backtest_runner import BacktestRunner
from core.regime.feature_transition_detector import (
    FeatureTransitionDetector,
    TransitionEvent,
)

# ── Constants ─────────────────────────────────────────────────────────────────
DATE_START      = "2022-03-22"
DATE_END        = "2026-03-21"
SYMBOLS         = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
FEE_PER_SIDE    = 0.0004
SLIP_PER_SIDE   = 0.0003
COST_PER_SIDE   = FEE_PER_SIDE + SLIP_PER_SIDE  # 0.07%

IS_END          = "2025-09-22"
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS   = 10
POS_FRAC        = 0.35

REPORT_DIR = ROOT_DIR / "reports" / "phase2c"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Event Pre-Detection
# ═══════════════════════════════════════════════════════════════════════════════

def _predetect_all_events(runner: BacktestRunner) -> tuple[dict, dict]:
    """
    Pre-detect ALL pullback_continuation AND range_breakout events on 1h data.

    Returns:
        (pb_events, rb_events) where each is {symbol: {1h_timestamp: TransitionEvent}}
    """
    from research.engine.backtest_runner import SLC_1H_TF

    pb_events_by_sym: dict = {}
    rb_events_by_sym: dict = {}

    for sym in runner.symbols:
        df_1h = runner._ind[sym].get(SLC_1H_TF)
        if df_1h is None or df_1h.empty:
            logger.warning("No 1h data for %s — skipping", sym)
            pb_events_by_sym[sym] = {}
            rb_events_by_sym[sym] = {}
            continue

        df_4h = runner._ind[sym].get("4h")

        detector = FeatureTransitionDetector(params={
            "range_breakout.require_confirmation_bar": False,
        })

        sym_pb: dict = {}
        sym_rb: dict = {}

        for loc in range(30, len(df_1h)):
            idx_4h = None
            if df_4h is not None and not df_4h.empty:
                ts_1h = df_1h.index[loc]
                idx_4h = int(df_4h.index.searchsorted(ts_1h, side="right")) - 1
                if idx_4h < 0:
                    idx_4h = None

            try:
                events = detector.detect(df_1h, loc, df_4h=df_4h, idx_4h=idx_4h)
            except Exception:
                continue

            for ev in events:
                ts = df_1h.index[loc]
                if ev.event_type == "pullback_continuation":
                    sym_pb[ts] = ev
                elif ev.event_type == "range_breakout":
                    sym_rb[ts] = ev

        pb_events_by_sym[sym] = sym_pb
        rb_events_by_sym[sym] = sym_rb
        logger.info(
            "Events %s: %d pullback_continuation, %d range_breakout over %d 1h bars",
            sym, len(sym_pb), len(sym_rb), len(df_1h),
        )

    return pb_events_by_sym, rb_events_by_sym


# ═══════════════════════════════════════════════════════════════════════════════
# Combined Simulation
# ═══════════════════════════════════════════════════════════════════════════════

def _run_combined_scenario(
    runner: BacktestRunner,
    cost_per_side: float,
    pb_events: dict,
    rb_events: dict,
    enhancement_enabled: bool,
    rb_enabled: bool,
    rb_entry_buffer_atr: float,
    progress_cb=None,
) -> dict:
    """
    Run combined PBL + SLC + (optional) RangeBreakout simulation.

    This simulation runs PBL/SLC through the SignalGenerator (with enhancement
    layer) AND a parallel RangeBreakout standalone path. Both compete for the
    same position slots per symbol.

    Architecture:
      1. PBL/SLC signals generated via SignalGenerator (dual-call pattern)
      2. RangeBreakout signals generated from pre-detected events
      3. Per-symbol: if multiple models fire, highest-strength wins
      4. Anti-amplification: when RB fires for a symbol, Mode B is disabled
         for PBL on that symbol (breakout_active=True in context)
      5. Next-bar-open entry with SL<EP<TP validation
      6. Cost model applied at entry and exit
    """
    import concurrent.futures as _cf

    from core.signals.signal_generator import SignalGenerator
    from core.meta_decision.position_sizer import PositionSizer
    from core.regime.research_regime_classifier import (
        regime_to_string as research_regime_to_string,
        BULL_TREND as RES_BULL_TREND,
        BEAR_TREND as RES_BEAR_TREND,
    )
    from config.settings import settings as _s

    # ── Configure ─────────────────────────────────────────────────────────
    _s.set("phase_2c.pullback_enhancement.enabled", enhancement_enabled, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_flat", 0.10, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_confidence_scale", 0.30, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.mode_b_level", 1, auto_save=False)  # Level 1 safe
    _s.set("phase_2c.pullback_enhancement.relaxed_strength_cap", 0.75, auto_save=False)
    _s.set("phase_2c.range_breakout.enabled", rb_enabled, auto_save=False)
    _s.set("phase_2c.range_breakout.entry_buffer_atr", rb_entry_buffer_atr, auto_save=False)

    from research.engine.backtest_runner import (
        PRIMARY_TF, HTF_4H_TF, SLC_1H_TF,
        WARMUP_BARS, MODEL_LOOKBACK, HTF_LOOKBACK, SLC_1H_LOOKBACK,
    )

    # SignalGenerator per symbol (for PBL/SLC)
    sig_gens: dict = {}
    for _sym in runner.symbols:
        _sg = SignalGenerator()
        _sg._warmup_complete = True
        _sg._rl_model = None
        sig_gens[_sym] = _sg

    sizer = PositionSizer()

    # Index structures
    idx30: dict = {}
    idx4h: dict = {}
    idx1h: dict = {}
    for sym in runner.symbols:
        df = runner._ind[sym].get(PRIMARY_TF)
        idx30[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
        df = runner._ind[sym].get(HTF_4H_TF)
        idx4h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
        df = runner._ind[sym].get(SLC_1H_TF)
        idx1h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])

    # RangeBreakout SL/TP constants (match step2b)
    RB_SL_RANGE_PCT = 0.10
    RB_TP_RANGE_MULT = 1.0
    RB_MIN_CONFIDENCE = 0.35

    # Active events tracking
    _active_pb: dict = {}  # {sym: (TransitionEvent, remaining_30m_bars)}
    _active_rb: dict = {}  # {sym: (TransitionEvent, remaining_30m_bars)}

    equity          = INITIAL_CAPITAL
    positions:      dict = {}
    pending_entries: dict = {}
    all_trades:     list = []
    equity_curve:   list = [INITIAL_CAPITAL]
    n_boosted = n_relaxed = n_anti_amplification = 0
    n_rb_signals = n_rb_false_breakout = 0
    n_rb_displaced_by_pbl_slc = 0
    n_pbl_slc_displaced_by_rb = 0

    t_sim = time.time()
    total = len(runner._master_ts)
    _last_prog_t = t_sim

    _sym_pool = _cf.ThreadPoolExecutor(max_workers=len(runner.symbols))

    try:
        for bar_idx, ts in enumerate(runner._master_ts):
            if bar_idx < WARMUP_BARS:
                continue

            # ── Progress ──────────────────────────────────────────────────
            if progress_cb:
                _now = time.time()
                if _now - _last_prog_t >= 1.0:
                    _last_prog_t = _now
                    _elapsed = _now - t_sim
                    _bars_done  = max(bar_idx - WARMUP_BARS, 1)
                    _bars_total = max(total - WARMUP_BARS, 1)
                    _rate = _bars_done / _elapsed
                    _eta  = (_bars_total - _bars_done) / max(_rate, 0.001)
                    progress_cb(
                        f"Simulating {bar_idx:,}/{total:,} bars | "
                        f"{_elapsed:.1f}s elapsed | ETA {_eta:.0f}s",
                        10 + int(bar_idx / total * 80),
                    )

            # ── Update active events ──────────────────────────────────────
            for sym in runner.symbols:
                # Pullback continuation events
                if ts in pb_events.get(sym, {}):
                    ev = pb_events[sym][ts]
                    _active_pb[sym] = (ev, ev.expires_bars * 2)  # 1h→30m bars
                if sym in _active_pb:
                    ev, rem = _active_pb[sym]
                    rem -= 1
                    if rem <= 0:
                        del _active_pb[sym]
                    else:
                        _active_pb[sym] = (ev, rem)

                # Range breakout events
                if ts in rb_events.get(sym, {}):
                    ev = rb_events[sym][ts]
                    _active_rb[sym] = (ev, ev.expires_bars * 2)
                if sym in _active_rb:
                    ev, rem = _active_rb[sym]
                    rem -= 1
                    if rem <= 0:
                        del _active_rb[sym]
                    else:
                        _active_rb[sym] = (ev, rem)

            # ── Fill pending entries (next-bar-open) ──────────────────────
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue

                ep_raw = float(runner._opens[sym][loc])
                model_name = pend["model"]
                direction = pend["direction"]
                sl, tp = pend["sl"], pend["tp"]

                if direction == "long":
                    valid = sl < ep_raw < tp
                    ep_fill = ep_raw * (1 + cost_per_side)
                else:
                    valid = tp < ep_raw < sl
                    ep_fill = ep_raw * (1 - cost_per_side)

                del pending_entries[sym]
                if not valid:
                    if model_name == "range_breakout":
                        n_rb_false_breakout += 1
                    continue

                positions[sym] = {
                    "direction":   direction,
                    "model":       model_name,
                    "entry_price": ep_fill,
                    "sl": sl, "tp": tp,
                    "size_usdt":   pend["size_usdt"],
                    "entry_bar":   bar_idx,
                    "entry_ts":    ts,
                    "was_boosted": pend.get("was_boosted", False),
                    "was_relaxed": pend.get("was_relaxed", False),
                }

            # ── SL/TP check ───────────────────────────────────────────────
            closed = []
            for sym, pos in list(positions.items()):
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                hi = float(runner._highs[sym][loc])
                lo = float(runner._lows[sym][loc])
                d, sl, tp = pos["direction"], pos["sl"], pos["tp"]
                ep, size  = pos["entry_price"], pos["size_usdt"]
                exit_px = reason = None
                if d == "long":
                    if lo <= sl: exit_px, reason = sl, "sl"
                    elif hi >= tp: exit_px, reason = tp, "tp"
                else:
                    if hi >= sl: exit_px, reason = sl, "sl"
                    elif lo <= tp: exit_px, reason = tp, "tp"
                if reason:
                    exit_adj = exit_px * (1 - cost_per_side) if d == "long" else exit_px * (1 + cost_per_side)
                    qty  = size / ep
                    pnl  = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
                    equity += pnl
                    r_val  = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
                    all_trades.append({
                        "symbol": sym, "direction": d, "model": pos["model"],
                        "entry_ts": str(pos["entry_ts"]), "exit_ts": str(ts),
                        "entry_price": ep, "exit_price": exit_px,
                        "size_usdt": size, "pnl": round(pnl, 4),
                        "r_value": round(r_val, 4), "exit_reason": reason,
                        "bars_held": bar_idx - pos["entry_bar"],
                        "was_boosted": pos.get("was_boosted", False),
                        "was_relaxed": pos.get("was_relaxed", False),
                    })
                    closed.append(sym)
            for sym in closed:
                del positions[sym]
            equity_curve.append(equity)

            # ── Signal generation ─────────────────────────────────────────
            _eligible = [
                sym for sym in runner.symbols
                if sym not in positions and sym not in pending_entries
            ]
            if not _eligible:
                continue

            def _gen_sym(sym):
                """Generate PBL/SLC + RB candidates for one symbol."""
                _loc = int(idx30[sym].searchsorted(ts))
                if _loc >= len(idx30[sym]) or idx30[sym][_loc] != ts:
                    return sym, [], None
                if _loc < WARMUP_BARS:
                    return sym, [], None

                # ── Research regime lookup ────────────────────────────────
                _res30 = runner._reg30.get(sym, np.array([]))
                _reg30m = int(_res30[_loc]) if _loc < len(_res30) else 0
                _l1h = int(idx1h[sym].searchsorted(ts, side="right")) - 1
                _res1h = runner._reg1h.get(sym, np.array([]))
                _reg1h = int(_res1h[_l1h]) if 0 <= _l1h < len(_res1h) else 0

                # ── Check if RB event is active for this symbol ──────────
                _rb_event_active = sym in _active_rb
                _rb_candidate = None

                if rb_enabled and _rb_event_active:
                    rb_ev, _ = _active_rb[sym]
                    snap = getattr(rb_ev, "features_snapshot", {})
                    range_high = snap.get("range_high")
                    range_low = snap.get("range_low")
                    rb_dir = getattr(rb_ev, "direction", "")
                    rb_conf = getattr(rb_ev, "confidence", 0.0)

                    if (range_high is not None and range_low is not None
                            and rb_dir in ("long", "short")
                            and rb_conf >= RB_MIN_CONFIDENCE):
                        range_width = range_high - range_low
                        if range_width > 0:
                            df30 = runner._ind[sym].get(PRIMARY_TF)
                            atr_col = df30.get("atr_14") if df30 is not None else None
                            if atr_col is not None and _loc < len(atr_col):
                                atr = float(atr_col.iloc[_loc])
                                if atr > 0 and not np.isnan(atr):
                                    buffer = rb_entry_buffer_atr * atr
                                    if rb_dir == "long":
                                        entry_price = range_high + buffer
                                        sl = range_low - RB_SL_RANGE_PCT * range_width
                                        tp = entry_price + RB_TP_RANGE_MULT * range_width
                                        valid = sl < entry_price < tp
                                    else:
                                        entry_price = range_low - buffer
                                        sl = range_high + RB_SL_RANGE_PCT * range_width
                                        tp = entry_price - RB_TP_RANGE_MULT * range_width
                                        valid = tp < entry_price < sl

                                    risk = abs(entry_price - sl)
                                    reward = abs(tp - entry_price)
                                    rr_ok = risk > 0 and reward / risk >= 0.8

                                    if valid and rr_ok:
                                        # Compute strength (same as model)
                                        _str = 0.35
                                        _str += max(0.0, min(0.20, (rb_conf - 0.35) * 0.40))
                                        vol_ratio = snap.get("vol_ratio", 1.0)
                                        _str += min(0.15, max(0.0, (vol_ratio - 1.5) * 0.15 / 0.5))
                                        _str += min(0.10, snap.get("mom_count", 0) * 0.05)
                                        rb_bars = snap.get("range_bars", 10)
                                        _str += max(0.0, min(0.10, (rb_bars - 10) / 30 * 0.10))
                                        _str = round(min(0.80, _str), 4)

                                        _rb_candidate = {
                                            "model": "range_breakout",
                                            "direction": rb_dir,
                                            "sl": sl, "tp": tp,
                                            "strength": _str,
                                            "confidence": rb_conf,
                                            "range_width": range_width,
                                        }

                # ── PBL/SLC signals ───────────────────────────────────────
                if _reg30m != RES_BULL_TREND and _reg1h != RES_BEAR_TREND:
                    return sym, [], _rb_candidate

                _s30 = max(0, _loc - MODEL_LOOKBACK + 1)
                _dfw = runner._ind[sym][PRIMARY_TF].iloc[_s30 : _loc + 1]
                if len(_dfw) < 70:
                    return sym, [], _rb_candidate

                _sigs = []
                _sg = sig_gens[sym]

                # Get active transition event
                _trans_ev = None
                if sym in _active_pb:
                    _trans_ev, _ = _active_pb[sym]

                # PBL path
                if _reg30m == RES_BULL_TREND:
                    _pbl_ctx = {}
                    if _trans_ev is not None:
                        _pbl_ctx["transition_event"] = _trans_ev
                    # Anti-amplification: if RB fired, set breakout_active=True
                    _pbl_ctx["breakout_active"] = _rb_event_active and rb_enabled
                    _l4h = int(idx4h[sym].searchsorted(ts, side="right"))
                    if _l4h >= HTF_LOOKBACK:
                        _pbl_ctx["df_4h"] = runner._ind[sym][HTF_4H_TF].iloc[
                            max(0, _l4h - HTF_LOOKBACK) : _l4h
                        ]
                    try:
                        _raw = _sg.generate(
                            sym, _dfw, research_regime_to_string(_reg30m),
                            PRIMARY_TF, regime_probs={}, context=_pbl_ctx,
                        ) or []
                        _sigs.extend(s for s in _raw if s.model_name == "pullback_long")
                    except Exception as _e:
                        logger.debug("SG PBL %s: %s", sym, _e)

                # SLC path
                if _reg1h == RES_BEAR_TREND and _l1h >= 15:
                    _slc_ctx = {
                        "df_1h": runner._ind[sym][SLC_1H_TF].iloc[
                            max(0, _l1h - SLC_1H_LOOKBACK + 1) : _l1h + 1
                        ]
                    }
                    if _trans_ev is not None:
                        _slc_ctx["transition_event"] = _trans_ev
                    try:
                        _raw = _sg.generate(
                            sym, _dfw, research_regime_to_string(_reg1h),
                            PRIMARY_TF, regime_probs={}, context=_slc_ctx,
                        ) or []
                        _sigs.extend(s for s in _raw if s.model_name == "swing_low_continuation")
                    except Exception as _e:
                        logger.debug("SG SLC %s: %s", sym, _e)

                return sym, _sigs, _rb_candidate

            # Sequential is fine for 3 symbols
            _sym_results = {}
            for sym in _eligible:
                _, sigs, rb_cand = _gen_sym(sym)
                _sym_results[sym] = (sigs, rb_cand)

            # ── Candidate selection per symbol ────────────────────────────
            for sym in _eligible:
                sigs, rb_cand = _sym_results.get(sym, ([], None))
                if sym in pending_entries:
                    continue

                # Collect all candidates
                candidates = []  # (strength, model_name, entry_data)

                # PBL/SLC candidates
                for sig in sigs:
                    _was_boosted = "Phase2c ModeA" in sig.rationale
                    _was_relaxed = "ModeA+B" in sig.rationale
                    candidates.append((
                        sig.strength,
                        sig.model_name,
                        {
                            "model": sig.model_name,
                            "direction": sig.direction,
                            "sl": sig.stop_loss,
                            "tp": sig.take_profit,
                            "was_boosted": _was_boosted,
                            "was_relaxed": _was_relaxed,
                        },
                    ))

                # RB candidate
                if rb_cand is not None:
                    n_rb_signals += 1
                    candidates.append((
                        rb_cand["strength"],
                        "range_breakout",
                        {
                            "model": "range_breakout",
                            "direction": rb_cand["direction"],
                            "sl": rb_cand["sl"],
                            "tp": rb_cand["tp"],
                            "was_boosted": False,
                            "was_relaxed": False,
                        },
                    ))

                if not candidates:
                    continue

                # Select highest strength
                candidates.sort(key=lambda c: c[0], reverse=True)
                best_strength, best_model, best_data = candidates[0]

                # Track displacement
                has_pbl_slc = any(c[1] in ("pullback_long", "swing_low_continuation") for c in candidates)
                has_rb = any(c[1] == "range_breakout" for c in candidates)
                if has_pbl_slc and has_rb:
                    if best_model == "range_breakout":
                        n_pbl_slc_displaced_by_rb += 1
                    else:
                        n_rb_displaced_by_pbl_slc += 1

                # Track anti-amplification
                if rb_cand is not None and has_pbl_slc:
                    # Mode B was disabled for PBL because breakout_active=True
                    n_anti_amplification += 1

                # Track enhancement
                if best_data.get("was_boosted"):
                    n_boosted += 1
                if best_data.get("was_relaxed"):
                    n_relaxed += 1

                # Portfolio gate
                open_count = len(positions) + len(pending_entries)
                if open_count >= MAX_POSITIONS:
                    continue

                open_by_sym = defaultdict(int)
                for ps in positions:
                    open_by_sym[ps] += 1
                size_usdt = sizer.calculate_pos_frac(
                    equity,
                    open_positions_count=open_count,
                    open_positions_by_symbol=dict(open_by_sym),
                    symbol=sym,
                )
                if size_usdt <= 0:
                    continue

                pending_entries[sym] = {
                    **best_data,
                    "size_usdt":  size_usdt,
                    "bar_signal": bar_idx,
                }

    finally:
        _sym_pool.shutdown(wait=False, cancel_futures=True)

    # ── Force-close remaining ─────────────────────────────────────────────
    if runner._master_ts:
        last_ts = runner._master_ts[-1]
        for sym, pos in list(positions.items()):
            df30 = runner._ind[sym].get(PRIMARY_TF)
            if df30 is None or df30.empty:
                continue
            last_close = float(df30["close"].iloc[-1])
            ep, size = pos["entry_price"], pos["size_usdt"]
            sl, d    = pos["sl"], pos["direction"]
            exit_adj = last_close * (1 - cost_per_side) if d == "long" else last_close * (1 + cost_per_side)
            qty  = size / ep
            pnl  = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
            equity += pnl
            r_val = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
            all_trades.append({
                "symbol": sym, "direction": d, "model": pos["model"],
                "entry_ts": str(pos["entry_ts"]), "exit_ts": str(last_ts),
                "entry_price": ep, "exit_price": last_close,
                "size_usdt": size, "pnl": round(pnl, 4),
                "r_value": round(r_val, 4), "exit_reason": "force_close",
                "bars_held": 0,
                "was_boosted": pos.get("was_boosted", False),
                "was_relaxed": pos.get("was_relaxed", False),
            })

    # ── Compute KPIs ──────────────────────────────────────────────────────
    elapsed = time.time() - t_sim
    n_trades = len(all_trades)
    winners = [t for t in all_trades if t["pnl"] > 0]
    losers  = [t for t in all_trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in winners)
    gl = abs(sum(t["pnl"] for t in losers))
    wr = len(winners) / n_trades if n_trades else 0.0
    pf = round(gp / gl, 4) if gl > 0 else 999.0
    avg_r = round(np.mean([t["r_value"] for t in all_trades]), 4) if all_trades else 0.0

    eq_arr = np.array(equity_curve)
    peak = np.maximum.accumulate(eq_arr)
    dd = (eq_arr - peak) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.min()) * 100

    years = (pd.Timestamp(DATE_END) - pd.Timestamp(DATE_START)).days / 365.25
    cagr = ((equity / INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100 if years > 0 else 0.0

    # Per-model
    def _model_kpis(trades):
        n = len(trades)
        w = [t for t in trades if t["pnl"] > 0]
        _gp = sum(t["pnl"] for t in w)
        _gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
        return {
            "n": n,
            "wr": round(len(w) / n, 4) if n else 0.0,
            "pf": round(_gp / _gl, 4) if _gl > 0 else 999.0,
            "avg_r": round(np.mean([t["r_value"] for t in trades]), 4) if trades else 0.0,
        }

    pbl_trades = [t for t in all_trades if t["model"] == "pullback_long"]
    slc_trades = [t for t in all_trades if t["model"] == "swing_low_continuation"]
    rb_trades  = [t for t in all_trades if t["model"] == "range_breakout"]

    # IS/OOS
    is_end_ts = pd.Timestamp(IS_END, tz="UTC")
    def _ts_parse(v):
        t = pd.Timestamp(v)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t

    is_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) < is_end_ts]
    oos_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) >= is_end_ts]

    # Per-asset
    per_asset = {}
    for sym in runner.symbols:
        sym_trades = [t for t in all_trades if t["symbol"] == sym]
        per_asset[sym] = _model_kpis(sym_trades)
        # Also per-model per-asset
        per_asset[sym]["pbl_n"] = len([t for t in sym_trades if t["model"] == "pullback_long"])
        per_asset[sym]["slc_n"] = len([t for t in sym_trades if t["model"] == "swing_low_continuation"])
        per_asset[sym]["rb_n"]  = len([t for t in sym_trades if t["model"] == "range_breakout"])

    result = {
        "n_trades":        n_trades,
        "profit_factor":   pf,
        "win_rate":        round(wr, 4),
        "avg_r":           avg_r,
        "cagr_pct":        round(cagr, 2),
        "max_dd_pct":      round(max_dd, 2),
        "final_equity":    round(equity, 2),
        "elapsed_s":       round(elapsed, 1),
        "pbl":             _model_kpis(pbl_trades),
        "slc":             _model_kpis(slc_trades),
        "rb":              _model_kpis(rb_trades),
        "is":              _model_kpis(is_trades),
        "oos":             _model_kpis(oos_trades),
        "per_asset":       per_asset,
        "n_boosted":       n_boosted,
        "n_relaxed":       n_relaxed,
        "n_anti_amplification": n_anti_amplification,
        "n_rb_signals":    n_rb_signals,
        "n_rb_false_breakout": n_rb_false_breakout,
        "n_rb_displaced_by_pbl_slc": n_rb_displaced_by_pbl_slc,
        "n_pbl_slc_displaced_by_rb": n_pbl_slc_displaced_by_rb,
        "enhancement_enabled": enhancement_enabled,
        "rb_enabled":      rb_enabled,
        "rb_entry_buffer": rb_entry_buffer_atr,
    }

    logger.info(
        "Scenario done: n=%d PF=%.4f WR=%.1f%% CAGR=%.1f%% MaxDD=%.1f%% | "
        "PBL=%d SLC=%d RB=%d boosted=%d relaxed=%d anti_amp=%d | %.1fs",
        n_trades, pf, wr * 100, cagr, max_dd,
        len(pbl_trades), len(slc_trades), len(rb_trades),
        n_boosted, n_relaxed, n_anti_amplification, elapsed,
    )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    from config.settings import settings as _s

    # Enable PBL/SLC, disable noisy models
    _s.set("mr_pbl_slc.enabled", True, auto_save=False)
    _s.set("disabled_models", [
        "mean_reversion", "liquidity_sweep", "trend", "donchian_breakout",
        "momentum_breakout", "funding_rate", "sentiment", "range_accumulation",
    ], auto_save=False)

    logger.info("=" * 70)
    logger.info("Phase 2c Step 2c — Combined System Validation")
    logger.info("=" * 70)
    logger.info("Cost model: %.2f%% fee + %.2f%% slippage = %.2f%% per side",
                FEE_PER_SIDE * 100, SLIP_PER_SIDE * 100, COST_PER_SIDE * 100)

    # ── Load data ─────────────────────────────────────────────────────────
    logger.info("Loading data and computing indicators...")
    t0 = time.time()

    runner = BacktestRunner(
        date_start=DATE_START,
        date_end=DATE_END,
        symbols=SYMBOLS,
        mode="pbl_slc",
    )

    def _progress(msg, pct):
        logger.info("  [%d%%] %s", pct, msg)

    runner.load_data(progress_cb=_progress)
    t_load = time.time() - t0
    logger.info("Data loaded in %.1fs", t_load)

    # ── Pre-detect all events ─────────────────────────────────────────────
    logger.info("Pre-detecting pullback_continuation + range_breakout events...")
    t1 = time.time()
    pb_events, rb_events = _predetect_all_events(runner)
    t_detect = time.time() - t1

    total_pb = sum(len(v) for v in pb_events.values())
    total_rb = sum(len(v) for v in rb_events.values())
    logger.info(
        "Event detection complete: %d pullback, %d breakout across %d symbols (%.1fs)",
        total_pb, total_rb, len(runner.symbols), t_detect,
    )

    results = {}

    # ── Scenario 1: BASELINE (PBL+SLC only, no enhancement, no RB) ───────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 1: BASELINE (PBL+SLC only)")
    logger.info("=" * 50)

    results["baseline"] = _run_combined_scenario(
        runner=runner,
        cost_per_side=COST_PER_SIDE,
        pb_events=pb_events,
        rb_events=rb_events,
        enhancement_enabled=False,
        rb_enabled=False,
        rb_entry_buffer_atr=0.0,
        progress_cb=_progress,
    )

    # ── Scenario 2: NO_BUFFER (PBL+SLC+enh+RB, buffer=0) ─────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 2: COMBINED — no_buffer (entry_buffer=0.0)")
    logger.info("=" * 50)

    results["no_buffer"] = _run_combined_scenario(
        runner=runner,
        cost_per_side=COST_PER_SIDE,
        pb_events=pb_events,
        rb_events=rb_events,
        enhancement_enabled=True,
        rb_enabled=True,
        rb_entry_buffer_atr=0.0,
        progress_cb=_progress,
    )

    # ── Scenario 3: WITH_BUFFER (PBL+SLC+enh+RB, buffer=0.1) ─────────────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 3: COMBINED — with_buffer (entry_buffer=0.1)")
    logger.info("=" * 50)

    results["with_buffer"] = _run_combined_scenario(
        runner=runner,
        cost_per_side=COST_PER_SIDE,
        pb_events=pb_events,
        rb_events=rb_events,
        enhancement_enabled=True,
        rb_enabled=True,
        rb_entry_buffer_atr=0.1,
        progress_cb=_progress,
    )

    # ── Comparison ────────────────────────────────────────────────────────
    baseline = results["baseline"]

    logger.info("\n" + "=" * 70)
    logger.info("RESULTS COMPARISON")
    logger.info("=" * 70)

    for name, r in results.items():
        pf_delta = r["profit_factor"] - baseline["profit_factor"]
        logger.info(
            "%-15s  n=%4d  PF=%.4f (Δ%+.4f)  WR=%.1f%%  AvgR=%+.4f  "
            "CAGR=%.1f%%  MaxDD=%.1f%%  |  PBL=%d  SLC=%d  RB=%d",
            name, r["n_trades"], r["profit_factor"], pf_delta,
            r["win_rate"] * 100, r["avg_r"],
            r["cagr_pct"], r["max_dd_pct"],
            r["pbl"]["n"], r["slc"]["n"], r["rb"]["n"],
        )

    # Crowding analysis
    logger.info("\n--- Crowding / Displacement Analysis ---")
    for name in ["no_buffer", "with_buffer"]:
        r = results[name]
        slc_delta = r["slc"]["n"] - baseline["slc"]["n"]
        pbl_delta = r["pbl"]["n"] - baseline["pbl"]["n"]
        logger.info(
            "  %-15s  SLC: %d (Δ%+d vs baseline)  PBL: %d (Δ%+d vs baseline)  "
            "RB→PBL/SLC displaced: %d  PBL/SLC→RB displaced: %d  "
            "Anti-amplification: %d",
            name, r["slc"]["n"], slc_delta, r["pbl"]["n"], pbl_delta,
            r["n_rb_displaced_by_pbl_slc"], r["n_pbl_slc_displaced_by_rb"],
            r["n_anti_amplification"],
        )

    # Enhancement stats
    logger.info("\n--- Enhancement Stats ---")
    for name in ["no_buffer", "with_buffer"]:
        r = results[name]
        logger.info(
            "  %-15s  boosted=%d  relaxed=%d  RB_false_breakout=%d",
            name, r["n_boosted"], r["n_relaxed"], r["n_rb_false_breakout"],
        )

    # Per-model PF
    logger.info("\n--- Per-Model PF ---")
    for name, r in results.items():
        logger.info(
            "  %-15s  PBL: PF=%.4f n=%d  |  SLC: PF=%.4f n=%d  |  RB: PF=%.4f n=%d",
            name,
            r["pbl"]["pf"], r["pbl"]["n"],
            r["slc"]["pf"], r["slc"]["n"],
            r["rb"]["pf"], r["rb"]["n"],
        )

    # IS/OOS
    logger.info("\n--- IS / OOS Stability ---")
    for name, r in results.items():
        logger.info(
            "  %-15s  IS: n=%d PF=%.4f WR=%.1f%%  |  OOS: n=%d PF=%.4f WR=%.1f%%",
            name,
            r["is"]["n"], r["is"]["pf"], r["is"]["wr"] * 100,
            r["oos"]["n"], r["oos"]["pf"], r["oos"]["wr"] * 100,
        )

    # Per-asset
    logger.info("\n--- Per-Asset Breakdown ---")
    for name, r in results.items():
        logger.info("  %s:", name)
        for sym, a in r["per_asset"].items():
            logger.info(
                "    %-12s  n=%3d (PBL=%d SLC=%d RB=%d)  PF=%.4f  WR=%.1f%%  AvgR=%+.4f",
                sym, a["n"], a["pbl_n"], a["slc_n"], a["rb_n"],
                a["pf"], a["wr"] * 100, a["avg_r"],
            )

    # ── Pass/Fail Checks ──────────────────────────────────────────────────
    logger.info("\n" + "-" * 50)
    logger.info("PASS / FAIL CHECKS (vs baseline)")
    logger.info("-" * 50)

    verdicts = {}
    for name in ["no_buffer", "with_buffer"]:
        r = results[name]
        checks = {
            "pf_ge_baseline":     r["profit_factor"] >= baseline["profit_factor"],
            "maxdd_le_baseline":  r["max_dd_pct"] >= baseline["max_dd_pct"],  # more negative = worse
            "slc_n_ge_baseline":  r["slc"]["n"] >= baseline["slc"]["n"],
            "pbl_n_stable":       abs(r["pbl"]["n"] - baseline["pbl"]["n"]) <= max(10, baseline["pbl"]["n"] * 0.10),
        }
        overall = all(checks.values())
        verdicts[name] = {"checks": checks, "pass": overall}

        status = "PASS" if overall else "FAIL"
        logger.info(
            "  %-15s  %s  (PF>=base:%s  MaxDD<=base:%s  SLC_n>=base:%s  PBL_stable:%s)",
            name, status,
            "Y" if checks["pf_ge_baseline"] else "N",
            "Y" if checks["maxdd_le_baseline"] else "N",
            "Y" if checks["slc_n_ge_baseline"] else "N",
            "Y" if checks["pbl_n_stable"] else "N",
        )

    # ── Recommendation ────────────────────────────────────────────────────
    logger.info("\n" + "=" * 50)
    logger.info("RECOMMENDATION")
    logger.info("=" * 50)

    nb_pass = verdicts.get("no_buffer", {}).get("pass", False)
    wb_pass = verdicts.get("with_buffer", {}).get("pass", False)

    if nb_pass and wb_pass:
        # Both pass — pick by PF
        if results["no_buffer"]["profit_factor"] >= results["with_buffer"]["profit_factor"]:
            recommendation = "deploy_no_buffer"
        else:
            recommendation = "deploy_with_buffer"
    elif nb_pass:
        recommendation = "deploy_no_buffer"
    elif wb_pass:
        recommendation = "deploy_with_buffer"
    else:
        recommendation = "reject_range_breakout"

    logger.info("DECISION: %s", recommendation)

    # ── Save report ───────────────────────────────────────────────────────
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "date_range": f"{DATE_START} to {DATE_END}",
        "symbols": SYMBOLS,
        "cost_model": {
            "fee_per_side": FEE_PER_SIDE,
            "slippage_per_side": SLIP_PER_SIDE,
            "total_per_side": COST_PER_SIDE,
        },
        "is_oos_split": IS_END,
        "events": {
            "pullback_continuation_total": total_pb,
            "range_breakout_total": total_rb,
        },
        "scenarios": results,
        "verdicts": verdicts,
        "recommendation": recommendation,
    }

    report_path = REPORT_DIR / "step2c_combined_validation_results.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("\nResults saved to: %s", report_path)
    logger.info("Total runtime: %.1fs", time.time() - t0)

    return report


if __name__ == "__main__":
    report = main()
