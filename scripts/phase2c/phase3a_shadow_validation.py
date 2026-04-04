#!/usr/bin/env python3
"""
Phase 3a — Shadow Mode Backtest Validation

Runs the full production-configured system (PBL + SLC + Enhancement Layer +
RangeBreakout with_buffer) on the OOS period to validate:
  1. Signal distribution (per-model, per-asset, per-direction)
  2. Capital allocation (RB capital cap enforcement, position limits)
  3. Model contribution (per-model PF, WR, Avg R, PnL share)
  4. Rolling PF (20-trade and 50-trade windows)
  5. Anti-amplification rule behavior

This script mirrors the exact production configuration:
  - Enhancement: enabled, mode_b_level=1 (Level 1 safe)
  - RangeBreakout: enabled, with_buffer (0.1×ATR)
  - RB controls: max_positions=1, max_capital_pct=3%
  - Cost model: 0.04% fee + 0.03% slippage = 0.07% per side

Usage:
    python scripts/phase2c/phase3a_shadow_validation.py
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
logger = logging.getLogger("phase3a_shadow")

for _name in ("core.signals", "core.regime", "core.features", "core.meta_decision", "core.rl"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from research.engine.backtest_runner import BacktestRunner
from core.regime.feature_transition_detector import (
    FeatureTransitionDetector,
    TransitionEvent,
)

# ── Constants ─────────────────────────────────────────────────────────────────
# Full 4-year period for data loading (cache warm), but we only measure
# the OOS period for shadow validation
DATE_START      = "2022-03-22"
DATE_END        = "2026-03-21"
SYMBOLS         = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
FEE_PER_SIDE    = 0.0004
SLIP_PER_SIDE   = 0.0003
COST_PER_SIDE   = FEE_PER_SIDE + SLIP_PER_SIDE

# Shadow validation period = OOS only (most recent 6 months)
SHADOW_START    = "2025-09-22"
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS   = 10
POS_FRAC        = 0.35

# RB production controls
RB_MAX_POSITIONS   = 1
RB_MAX_CAPITAL_PCT = 0.03  # 3%

REPORT_DIR = ROOT_DIR / "reports" / "phase2c"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _predetect_all_events(runner: BacktestRunner) -> tuple[dict, dict]:
    """Pre-detect pullback_continuation and range_breakout events on 1h data."""
    from research.engine.backtest_runner import SLC_1H_TF

    pb_events_by_sym: dict = {}
    rb_events_by_sym: dict = {}

    for sym in runner.symbols:
        df_1h = runner._ind[sym].get(SLC_1H_TF)
        if df_1h is None or df_1h.empty:
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

            ts = df_1h.index[loc]
            for ev in events:
                if ev.event_type == "pullback_continuation":
                    sym_pb[ts] = ev
                elif ev.event_type == "range_breakout":
                    sym_rb[ts] = ev

        pb_events_by_sym[sym] = sym_pb
        rb_events_by_sym[sym] = sym_rb
        logger.info(
            "Event detection %s: %d pb, %d rb over %d 1h bars",
            sym, len(sym_pb), len(sym_rb), len(df_1h),
        )

    return pb_events_by_sym, rb_events_by_sym


def _run_shadow_scenario(
    runner: BacktestRunner,
    cost_per_side: float,
    pb_events: dict,
    rb_events: dict,
    shadow_start_ts,
    progress_cb=None,
) -> dict:
    """
    Run the full production-configured combined system.

    Identical to Step 2c combined validation but with:
    - RB position limit enforcement (max_positions=1)
    - RB capital cap enforcement (max_capital_pct=3%)
    - Rolling PF tracking per 20/50 trade windows
    - Shadow period metrics (only counts trades after shadow_start_ts)
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
    from research.engine.backtest_runner import (
        PRIMARY_TF, HTF_4H_TF, SLC_1H_TF,
        WARMUP_BARS, MODEL_LOOKBACK, HTF_LOOKBACK, SLC_1H_LOOKBACK,
    )

    # Configure production settings
    _s.set("phase_2c.pullback_enhancement.enabled", True, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_flat", 0.10, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_confidence_scale", 0.30, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.mode_b_level", 1, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.relaxed_strength_cap", 0.75, auto_save=False)
    _s.set("phase_2c.range_breakout.enabled", True, auto_save=False)
    _s.set("phase_2c.range_breakout.entry_buffer_atr", 0.1, auto_save=False)
    _s.set("phase_2c.range_breakout.max_positions", RB_MAX_POSITIONS, auto_save=False)
    _s.set("phase_2c.range_breakout.max_capital_pct", RB_MAX_CAPITAL_PCT, auto_save=False)

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

    _active_pb: dict = {}
    _active_rb: dict = {}

    SL_RANGE_PCT = 0.10
    TP_RANGE_MULT = 1.0
    MIN_RB_CONFIDENCE = 0.35

    equity          = INITIAL_CAPITAL
    positions:      dict = {}
    pending_entries: dict = {}
    all_trades:     list = []
    equity_curve:   list = [INITIAL_CAPITAL]

    # Shadow period tracking
    shadow_trades:  list = []

    # Rolling PF tracking
    rolling_pf_20: list = []
    rolling_pf_50: list = []

    # Counters
    n_pbl_signals = n_slc_signals = n_rb_signals = 0
    n_boosted = n_relaxed = n_anti_amplification = 0
    n_rb_capped = n_rb_pos_blocked = n_rb_cap_applied = 0
    n_conflict_pbl_rb = n_conflict_slc_rb = 0
    n_rb_false_breakout = 0
    rejected_heat = rejected_max = rejected_entry_gap = 0

    t_sim = time.time()
    total = len(runner._master_ts)
    _last_prog_t = t_sim

    _sym_pool = _cf.ThreadPoolExecutor(max_workers=len(runner.symbols))

    try:
        for bar_idx, ts in enumerate(runner._master_ts):
            if bar_idx < WARMUP_BARS:
                continue

            # Progress
            if progress_cb:
                _now = time.time()
                if _now - _last_prog_t >= 1.0:
                    _last_prog_t = _now
                    _elapsed = _now - t_sim
                    _bars_done = max(bar_idx - WARMUP_BARS, 1)
                    _bars_total = max(total - WARMUP_BARS, 1)
                    _rate = _bars_done / _elapsed
                    _eta = (_bars_total - _bars_done) / max(_rate, 0.001)
                    progress_cb(
                        f"Shadow sim: {bar_idx:,}/{total:,} bars | "
                        f"{_elapsed:.1f}s elapsed | ETA {_eta:.0f}s",
                        10 + int(bar_idx / total * 80),
                    )

            # Update active events
            for sym in runner.symbols:
                if ts in pb_events.get(sym, {}):
                    ev = pb_events[sym][ts]
                    _active_pb[sym] = (ev, ev.expires_bars * 2)
                if sym in _active_pb:
                    ev, remaining = _active_pb[sym]
                    remaining -= 1
                    if remaining <= 0:
                        del _active_pb[sym]
                    else:
                        _active_pb[sym] = (ev, remaining)

                if ts in rb_events.get(sym, {}):
                    ev = rb_events[sym][ts]
                    _active_rb[sym] = (ev, ev.expires_bars * 2)
                if sym in _active_rb:
                    ev, remaining = _active_rb[sym]
                    remaining -= 1
                    if remaining <= 0:
                        del _active_rb[sym]
                    else:
                        _active_rb[sym] = (ev, remaining)

            # Fill pending entries
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue

                ep_raw = float(runner._opens[sym][loc])
                model = pend["model"]

                if model == "range_breakout":
                    sl, tp = pend["sl"], pend["tp"]
                    direction = pend["direction"]
                else:
                    sig = pend["signal"]
                    sl, tp = sig.stop_loss, sig.take_profit
                    direction = sig.direction

                if direction == "long":
                    valid = sl < ep_raw < tp
                    ep_fill = ep_raw * (1 + cost_per_side)
                else:
                    valid = tp < ep_raw < sl
                    ep_fill = ep_raw * (1 - cost_per_side)

                del pending_entries[sym]
                if not valid:
                    if model == "range_breakout":
                        n_rb_false_breakout += 1
                    else:
                        rejected_entry_gap += 1
                    continue

                positions[sym] = {
                    "direction": direction, "model": model,
                    "entry_price": ep_fill, "sl": sl, "tp": tp,
                    "size_usdt": pend["size_usdt"], "entry_bar": bar_idx,
                    "entry_ts": ts, "was_boosted": pend.get("was_boosted", False),
                    "was_relaxed": pend.get("was_relaxed", False),
                }

            # SL/TP check
            closed = []
            for sym, pos in list(positions.items()):
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                hi = float(runner._highs[sym][loc])
                lo = float(runner._lows[sym][loc])
                d, sl, tp = pos["direction"], pos["sl"], pos["tp"]
                ep, size = pos["entry_price"], pos["size_usdt"]
                exit_px = reason = None
                if d == "long":
                    if lo <= sl: exit_px, reason = sl, "sl"
                    elif hi >= tp: exit_px, reason = tp, "tp"
                else:
                    if hi >= sl: exit_px, reason = sl, "sl"
                    elif lo <= tp: exit_px, reason = tp, "tp"
                if reason:
                    exit_adj = exit_px * (1 - cost_per_side) if d == "long" else exit_px * (1 + cost_per_side)
                    qty = size / ep
                    pnl = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
                    equity += pnl
                    r_val = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
                    trade = {
                        "symbol": sym, "direction": d, "model": pos["model"],
                        "entry_ts": str(pos["entry_ts"]), "exit_ts": str(ts),
                        "entry_price": ep, "exit_price": exit_px,
                        "size_usdt": size, "pnl": round(pnl, 4),
                        "r_value": round(r_val, 4), "exit_reason": reason,
                        "bars_held": bar_idx - pos["entry_bar"],
                        "was_boosted": pos.get("was_boosted", False),
                        "was_relaxed": pos.get("was_relaxed", False),
                    }
                    all_trades.append(trade)

                    # Track rolling PF
                    _n = len(all_trades)
                    if _n >= 20:
                        _w20 = all_trades[-20:]
                        _gp20 = sum(t["pnl"] for t in _w20 if t["pnl"] > 0)
                        _gl20 = abs(sum(t["pnl"] for t in _w20 if t["pnl"] <= 0))
                        _rpf20 = round(_gp20 / _gl20, 4) if _gl20 > 0 else 999.0
                        rolling_pf_20.append({"trade_n": _n, "ts": str(ts), "rpf_20": _rpf20})
                    if _n >= 50:
                        _w50 = all_trades[-50:]
                        _gp50 = sum(t["pnl"] for t in _w50 if t["pnl"] > 0)
                        _gl50 = abs(sum(t["pnl"] for t in _w50 if t["pnl"] <= 0))
                        _rpf50 = round(_gp50 / _gl50, 4) if _gl50 > 0 else 999.0
                        rolling_pf_50.append({"trade_n": _n, "ts": str(ts), "rpf_50": _rpf50})

                    # Shadow period tracking
                    if ts >= shadow_start_ts:
                        shadow_trades.append(trade)

                    closed.append(sym)
            for sym in closed:
                del positions[sym]
            equity_curve.append(equity)

            # Signal generation
            _eligible = [
                sym for sym in runner.symbols
                if sym not in positions and sym not in pending_entries
            ]
            if not _eligible:
                continue

            def _gen_pbl_slc(sym):
                _loc = int(idx30[sym].searchsorted(ts))
                if _loc >= len(idx30[sym]) or idx30[sym][_loc] != ts:
                    return sym, []
                if _loc < WARMUP_BARS:
                    return sym, []

                _res30 = runner._reg30.get(sym, np.array([]))
                _reg30m = int(_res30[_loc]) if _loc < len(_res30) else 0
                _l1h = int(idx1h[sym].searchsorted(ts, side="right")) - 1
                _res1h = runner._reg1h.get(sym, np.array([]))
                _reg1h = int(_res1h[_l1h]) if 0 <= _l1h < len(_res1h) else 0

                if _reg30m != RES_BULL_TREND and _reg1h != RES_BEAR_TREND:
                    return sym, []

                _s30 = max(0, _loc - MODEL_LOOKBACK + 1)
                _dfw = runner._ind[sym][PRIMARY_TF].iloc[_s30 : _loc + 1]
                if len(_dfw) < 70:
                    return sym, []

                _sigs = []
                _sg = sig_gens[sym]
                _rb_active = sym in _active_rb
                _trans_ev = None
                if sym in _active_pb:
                    _trans_ev, _ = _active_pb[sym]

                if _reg30m == RES_BULL_TREND:
                    _pbl_ctx = {}
                    if _trans_ev is not None:
                        _pbl_ctx["transition_event"] = _trans_ev
                    _pbl_ctx["breakout_active"] = _rb_active
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
                    except Exception:
                        pass

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
                    except Exception:
                        pass

                return sym, _sigs

            _sym_results = dict(_sym_pool.map(_gen_pbl_slc, _eligible))

            for sym in _eligible:
                candidates = []

                for sig in _sym_results.get(sym, []):
                    _was_boosted = "Phase2c ModeA" in sig.rationale
                    _was_relaxed = "ModeA+B" in sig.rationale
                    candidates.append({
                        "type": "signal", "model": sig.model_name,
                        "signal": sig, "strength": sig.strength,
                        "direction": sig.direction,
                        "was_boosted": _was_boosted, "was_relaxed": _was_relaxed,
                    })

                # RB candidates
                if sym in _active_rb:
                    ev, _ = _active_rb[sym]
                    snap = ev.features_snapshot
                    rh, rl = snap.get("range_high"), snap.get("range_low")
                    if rh is not None and rl is not None:
                        rw = rh - rl
                        if rw > 0 and ev.confidence >= MIN_RB_CONFIDENCE:
                            loc = int(idx30[sym].searchsorted(ts))
                            if loc < len(idx30[sym]) and idx30[sym][loc] == ts and loc >= 30:
                                df30 = runner._ind[sym].get(PRIMARY_TF)
                                atr_col = df30.get("atr_14") if df30 is not None else None
                                if atr_col is not None and loc < len(atr_col):
                                    atr = float(atr_col.iloc[loc])
                                    if atr > 0 and not np.isnan(atr):
                                        buf = 0.1 * atr  # with_buffer
                                        d = ev.direction
                                        if d == "long":
                                            ep = rh + buf; sl = rl - 0.10 * rw; tp = ep + 1.0 * rw
                                        else:
                                            ep = rl - buf; sl = rh + 0.10 * rw; tp = ep - 1.0 * rw
                                        valid = (d == "long" and sl < ep < tp) or (d == "short" and tp < ep < sl)
                                        risk = abs(ep - sl); reward = abs(tp - ep)
                                        if valid and risk > 0 and reward / risk >= 0.8:
                                            vr = snap.get("vol_ratio", 1.0)
                                            mc = snap.get("mom_count", 0)
                                            rb_ = snap.get("range_bars", 10)
                                            st = 0.35 + max(0, min(0.20, (ev.confidence - 0.35) * 0.40))
                                            st += min(0.15, (vr - 1.5) * 0.15 / 0.5) if vr > 1.5 else 0
                                            st += min(0.10, mc * 0.05)
                                            st += max(0, min(0.10, (rb_ - 10) / 30 * 0.10))
                                            st = round(min(0.80, st), 4)
                                            candidates.append({
                                                "type": "rb_event", "model": "range_breakout",
                                                "strength": st, "direction": d,
                                                "sl": sl, "tp": tp, "entry_price": ep,
                                                "range_width": rw, "range_high": rh,
                                                "range_low": rl, "confidence": ev.confidence,
                                                "was_boosted": False, "was_relaxed": False,
                                            })

                if not candidates:
                    continue

                # Conflict tracking
                has_pbl_slc = any(c["model"] in ("pullback_long", "swing_low_continuation") for c in candidates)
                has_rb = any(c["model"] == "range_breakout" for c in candidates)
                if has_pbl_slc and has_rb:
                    if any(c["model"] == "pullback_long" for c in candidates):
                        n_conflict_pbl_rb += 1
                    if any(c["model"] == "swing_low_continuation" for c in candidates):
                        n_conflict_slc_rb += 1
                if has_rb and has_pbl_slc:
                    for c in candidates:
                        if c["model"] == "pullback_long" and c.get("was_relaxed"):
                            n_anti_amplification += 1

                best = max(candidates, key=lambda c: c["strength"])

                # Portfolio gate
                open_count = len(positions)
                if open_count >= MAX_POSITIONS:
                    rejected_max += 1
                    continue

                # RB position limit (production control)
                if best["model"] == "range_breakout":
                    rb_open = sum(
                        1 for p in positions.values() if p["model"] == "range_breakout"
                    )
                    if rb_open >= RB_MAX_POSITIONS:
                        n_rb_pos_blocked += 1
                        # Fall back to best non-RB candidate
                        non_rb = [c for c in candidates if c["model"] != "range_breakout"]
                        if non_rb:
                            best = max(non_rb, key=lambda c: c["strength"])
                        else:
                            continue

                # Position sizing
                open_by_sym = defaultdict(int)
                for ps in positions:
                    open_by_sym[ps] += 1

                size_usdt = sizer.calculate_pos_frac(
                    equity, open_positions_count=open_count,
                    open_positions_by_symbol=dict(open_by_sym), symbol=sym,
                )
                if size_usdt <= 0:
                    rejected_heat += 1
                    continue

                # RB capital cap (production control)
                if best["model"] == "range_breakout":
                    rb_cap = equity * RB_MAX_CAPITAL_PCT
                    if size_usdt > rb_cap:
                        n_rb_cap_applied += 1
                        size_usdt = rb_cap

                # Count signals
                if best["model"] == "pullback_long": n_pbl_signals += 1
                elif best["model"] == "swing_low_continuation": n_slc_signals += 1
                elif best["model"] == "range_breakout": n_rb_signals += 1
                if best.get("was_boosted"): n_boosted += 1
                if best.get("was_relaxed"): n_relaxed += 1

                if best["type"] == "signal":
                    pending_entries[sym] = {
                        "model": best["model"], "signal": best["signal"],
                        "size_usdt": size_usdt, "bar_signal": bar_idx,
                        "was_boosted": best.get("was_boosted", False),
                        "was_relaxed": best.get("was_relaxed", False),
                    }
                else:
                    pending_entries[sym] = {
                        "model": "range_breakout", "direction": best["direction"],
                        "sl": best["sl"], "tp": best["tp"],
                        "size_usdt": size_usdt, "bar_signal": bar_idx,
                        "was_boosted": False, "was_relaxed": False,
                    }

    finally:
        _sym_pool.shutdown(wait=False, cancel_futures=True)

    # Force-close remaining
    if runner._master_ts:
        last_ts = runner._master_ts[-1]
        for sym, pos in list(positions.items()):
            df30 = runner._ind[sym].get(PRIMARY_TF)
            if df30 is None or df30.empty:
                continue
            last_close = float(df30["close"].iloc[-1])
            ep, size = pos["entry_price"], pos["size_usdt"]
            sl, d = pos["sl"], pos["direction"]
            exit_adj = last_close * (1 - cost_per_side) if d == "long" else last_close * (1 + cost_per_side)
            qty = size / ep
            pnl = (exit_adj - ep) * qty if d == "long" else (ep - exit_adj) * qty
            equity += pnl
            r_val = pnl / (abs(ep - sl) * qty) if abs(ep - sl) > 0 else 0.0
            trade = {
                "symbol": sym, "direction": d, "model": pos["model"],
                "entry_ts": str(pos["entry_ts"]), "exit_ts": str(last_ts),
                "entry_price": ep, "exit_price": last_close,
                "size_usdt": size, "pnl": round(pnl, 4),
                "r_value": round(r_val, 4), "exit_reason": "force_close",
                "bars_held": 0, "was_boosted": pos.get("was_boosted", False),
                "was_relaxed": pos.get("was_relaxed", False),
            }
            all_trades.append(trade)
            if last_ts >= shadow_start_ts:
                shadow_trades.append(trade)

    elapsed = time.time() - t_sim

    # ── KPIs ──────────────────────────────────────────────────────────────
    def _kpis(trades):
        n = len(trades)
        w = [t for t in trades if t["pnl"] > 0]
        _gp = sum(t["pnl"] for t in w)
        _gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
        _sl = [t for t in trades if t["exit_reason"] == "sl"]
        return {
            "n": n,
            "wr": round(len(w) / n, 4) if n else 0.0,
            "pf": round(_gp / _gl, 4) if _gl > 0 else 999.0,
            "avg_r": round(np.mean([t["r_value"] for t in trades]), 4) if trades else 0.0,
            "total_pnl": round(sum(t["pnl"] for t in trades), 2),
            "sl_rate": round(len(_sl) / n, 4) if n else 0.0,
        }

    eq_arr = np.array(equity_curve)
    peak = np.maximum.accumulate(eq_arr)
    dd = (eq_arr - peak) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.min()) * 100

    years = (pd.Timestamp(DATE_END) - pd.Timestamp(DATE_START)).days / 365.25
    cagr = ((equity / INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100 if years > 0 else 0.0

    # Per-model breakdown
    pbl_trades = [t for t in all_trades if t["model"] == "pullback_long"]
    slc_trades = [t for t in all_trades if t["model"] == "swing_low_continuation"]
    rb_trades = [t for t in all_trades if t["model"] == "range_breakout"]

    # Shadow period per-model
    shadow_pbl = [t for t in shadow_trades if t["model"] == "pullback_long"]
    shadow_slc = [t for t in shadow_trades if t["model"] == "swing_low_continuation"]
    shadow_rb = [t for t in shadow_trades if t["model"] == "range_breakout"]

    # Per-asset
    per_asset = {}
    for sym in runner.symbols:
        sym_trades = [t for t in all_trades if t["symbol"] == sym]
        per_asset[sym] = _kpis(sym_trades)
        per_asset[sym]["pbl_n"] = len([t for t in sym_trades if t["model"] == "pullback_long"])
        per_asset[sym]["slc_n"] = len([t for t in sym_trades if t["model"] == "swing_low_continuation"])
        per_asset[sym]["rb_n"] = len([t for t in sym_trades if t["model"] == "range_breakout"])

    # PnL contribution share
    total_pnl = sum(t["pnl"] for t in all_trades)
    pbl_pnl = sum(t["pnl"] for t in pbl_trades)
    slc_pnl = sum(t["pnl"] for t in slc_trades)
    rb_pnl = sum(t["pnl"] for t in rb_trades)

    # Capital allocation analysis (RB trades)
    rb_sizes = [t["size_usdt"] for t in rb_trades]
    rb_avg_size = round(np.mean(rb_sizes), 2) if rb_sizes else 0.0
    rb_max_size = round(max(rb_sizes), 2) if rb_sizes else 0.0
    rb_pct_of_trades = round(len(rb_trades) / len(all_trades) * 100, 1) if all_trades else 0.0
    rb_pct_of_pnl = round(rb_pnl / total_pnl * 100, 1) if total_pnl != 0 else 0.0

    result = {
        "full_period": {
            "n_trades": len(all_trades),
            "profit_factor": _kpis(all_trades)["pf"],
            "win_rate": _kpis(all_trades)["wr"],
            "avg_r": _kpis(all_trades)["avg_r"],
            "cagr_pct": round(cagr, 2),
            "max_dd_pct": round(max_dd, 2),
            "final_equity": round(equity, 2),
        },
        "shadow_period": {
            "start": SHADOW_START,
            "combined": _kpis(shadow_trades),
            "pbl": _kpis(shadow_pbl),
            "slc": _kpis(shadow_slc),
            "rb": _kpis(shadow_rb),
        },
        "model_contribution": {
            "pbl": {**_kpis(pbl_trades), "pnl_share_pct": round(pbl_pnl / total_pnl * 100, 1) if total_pnl else 0},
            "slc": {**_kpis(slc_trades), "pnl_share_pct": round(slc_pnl / total_pnl * 100, 1) if total_pnl else 0},
            "rb": {**_kpis(rb_trades), "pnl_share_pct": round(rb_pnl / total_pnl * 100, 1) if total_pnl else 0},
        },
        "capital_allocation": {
            "rb_avg_size_usdt": rb_avg_size,
            "rb_max_size_usdt": rb_max_size,
            "rb_pct_of_trades": rb_pct_of_trades,
            "rb_pct_of_pnl": rb_pct_of_pnl,
            "rb_cap_applied_count": n_rb_cap_applied,
            "rb_pos_blocked_count": n_rb_pos_blocked,
        },
        "per_asset": per_asset,
        "enhancement_stats": {
            "n_boosted": n_boosted,
            "n_relaxed": n_relaxed,
            "n_anti_amplification": n_anti_amplification,
            "n_conflict_pbl_rb": n_conflict_pbl_rb,
            "n_conflict_slc_rb": n_conflict_slc_rb,
        },
        "rolling_pf": {
            "rpf_20_last": rolling_pf_20[-1]["rpf_20"] if rolling_pf_20 else None,
            "rpf_50_last": rolling_pf_50[-1]["rpf_50"] if rolling_pf_50 else None,
            "rpf_20_min": min(r["rpf_20"] for r in rolling_pf_20) if rolling_pf_20 else None,
            "rpf_50_min": min(r["rpf_50"] for r in rolling_pf_50) if rolling_pf_50 else None,
            "rpf_20_series_last_10": rolling_pf_20[-10:] if rolling_pf_20 else [],
            "rpf_50_series_last_10": rolling_pf_50[-10:] if rolling_pf_50 else [],
        },
        "signal_counts": {
            "pbl": n_pbl_signals, "slc": n_slc_signals, "rb": n_rb_signals,
        },
        "rejections": {
            "heat": rejected_heat, "max_positions": rejected_max,
            "entry_gap": rejected_entry_gap, "rb_false_breakout": n_rb_false_breakout,
        },
        "elapsed_s": round(elapsed, 1),
    }

    logger.info(
        "Shadow sim done: n=%d PF=%.4f WR=%.1f%% CAGR=%.1f%% MaxDD=%.1f%% | "
        "PBL=%d SLC=%d RB=%d | shadow_n=%d shadow_PF=%.4f | %.1fs",
        len(all_trades), _kpis(all_trades)["pf"],
        _kpis(all_trades)["wr"] * 100, cagr, max_dd,
        len(pbl_trades), len(slc_trades), len(rb_trades),
        len(shadow_trades), _kpis(shadow_trades)["pf"],
        elapsed,
    )

    return result


def main():
    from config.settings import settings as _s

    _s.set("mr_pbl_slc.enabled", True, auto_save=False)
    _s.set("disabled_models", [
        "mean_reversion", "liquidity_sweep", "trend", "donchian_breakout",
        "momentum_breakout", "funding_rate", "sentiment", "range_accumulation",
    ], auto_save=False)

    logger.info("=" * 70)
    logger.info("Phase 3a — Shadow Mode Backtest Validation")
    logger.info("=" * 70)
    logger.info("Config: Enhancement=ON (Level 1), RB=ON (with_buffer 0.1×ATR)")
    logger.info("Controls: RB max_positions=%d, RB max_capital_pct=%.0f%%",
                RB_MAX_POSITIONS, RB_MAX_CAPITAL_PCT * 100)
    logger.info("Cost model: %.2f%% fee + %.2f%% slippage = %.2f%% per side",
                FEE_PER_SIDE * 100, SLIP_PER_SIDE * 100, COST_PER_SIDE * 100)

    t0 = time.time()

    runner = BacktestRunner(
        date_start=DATE_START, date_end=DATE_END,
        symbols=SYMBOLS, mode="pbl_slc",
    )

    def _progress(msg, pct):
        logger.info("  [%d%%] %s", pct, msg)

    runner.load_data(progress_cb=_progress)
    logger.info("Data loaded in %.1fs", time.time() - t0)

    # Pre-detect events
    logger.info("Pre-detecting transition events...")
    t1 = time.time()
    pb_events, rb_events = _predetect_all_events(runner)
    logger.info(
        "Events: %d pb, %d rb (%.1fs)",
        sum(len(v) for v in pb_events.values()),
        sum(len(v) for v in rb_events.values()),
        time.time() - t1,
    )

    # Shadow start timestamp
    shadow_start_ts = pd.Timestamp(SHADOW_START, tz="UTC")

    # Run simulation
    result = _run_shadow_scenario(
        runner=runner, cost_per_side=COST_PER_SIDE,
        pb_events=pb_events, rb_events=rb_events,
        shadow_start_ts=shadow_start_ts,
        progress_cb=_progress,
    )

    # ── Report ────────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 70)
    logger.info("SHADOW VALIDATION REPORT")
    logger.info("=" * 70)

    fp = result["full_period"]
    logger.info(
        "FULL PERIOD: n=%d PF=%.4f WR=%.1f%% AvgR=%+.4f CAGR=%.1f%% MaxDD=%.1f%%",
        fp["n_trades"], fp["profit_factor"], fp["win_rate"] * 100,
        fp["avg_r"], fp["cagr_pct"], fp["max_dd_pct"],
    )

    sp = result["shadow_period"]
    sc = sp["combined"]
    logger.info(
        "\nSHADOW PERIOD (%s → %s): n=%d PF=%.4f WR=%.1f%% AvgR=%+.4f PnL=$%.0f",
        SHADOW_START, DATE_END, sc["n"], sc["pf"], sc["wr"] * 100, sc["avg_r"], sc["total_pnl"],
    )
    for mn in ("pbl", "slc", "rb"):
        mk = sp[mn]
        logger.info(
            "  %-5s: n=%d PF=%.4f WR=%.1f%% AvgR=%+.4f PnL=$%.0f",
            mn.upper(), mk["n"], mk["pf"], mk["wr"] * 100, mk["avg_r"], mk["total_pnl"],
        )

    mc = result["model_contribution"]
    logger.info("\nMODEL CONTRIBUTION (full period):")
    for mn in ("pbl", "slc", "rb"):
        mk = mc[mn]
        logger.info(
            "  %-5s: n=%d PF=%.4f WR=%.1f%% PnL share=%.1f%%",
            mn.upper(), mk["n"], mk["pf"], mk["wr"] * 100, mk["pnl_share_pct"],
        )

    ca = result["capital_allocation"]
    logger.info(
        "\nCAPITAL ALLOCATION (RB): avg_size=$%.0f max_size=$%.0f "
        "trades=%.1f%% pnl=%.1f%% cap_applied=%d pos_blocked=%d",
        ca["rb_avg_size_usdt"], ca["rb_max_size_usdt"],
        ca["rb_pct_of_trades"], ca["rb_pct_of_pnl"],
        ca["rb_cap_applied_count"], ca["rb_pos_blocked_count"],
    )

    es = result["enhancement_stats"]
    logger.info(
        "\nENHANCEMENT: boosted=%d relaxed=%d anti_amp=%d "
        "conflicts(PBL×RB=%d SLC×RB=%d)",
        es["n_boosted"], es["n_relaxed"], es["n_anti_amplification"],
        es["n_conflict_pbl_rb"], es["n_conflict_slc_rb"],
    )

    rp = result["rolling_pf"]
    logger.info(
        "\nROLLING PF: last_20=%.4f min_20=%.4f last_50=%.4f min_50=%.4f",
        rp["rpf_20_last"] or 0, rp["rpf_20_min"] or 0,
        rp["rpf_50_last"] or 0, rp["rpf_50_min"] or 0,
    )

    # Save
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "enhancement_enabled": True,
            "mode_b_level": 1,
            "range_breakout_enabled": True,
            "entry_buffer_atr": 0.1,
            "rb_max_positions": RB_MAX_POSITIONS,
            "rb_max_capital_pct": RB_MAX_CAPITAL_PCT,
            "cost_per_side": COST_PER_SIDE,
        },
        **result,
    }

    report_path = REPORT_DIR / "phase3a_shadow_validation_results.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("\nResults saved to: %s", report_path)
    logger.info("Total runtime: %.1fs", time.time() - t0)

    return report


if __name__ == "__main__":
    report = main()
