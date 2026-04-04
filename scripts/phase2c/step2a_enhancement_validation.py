#!/usr/bin/env python3
"""
Phase 2c Step 2a — Enhancement Layer Validation (Pullback→Continuation)

Validates that the PBL/SLC timing enhancement layer (Mode A boost + Mode B
relaxation) improves or maintains combined PF vs the baseline.

Scenarios:
  1. BASELINE:          PBL + SLC, current optimized params (no enhancement)
  2. MODE_A_ONLY:       Enhancement enabled, mode_b_level=0 (boost only)
  3. MODE_A_B_LEVEL1:   Enhancement enabled, mode_b_level=1 (safe relaxation)

Pass criteria (all must hold):
  - Combined PF ≥ baseline (1.2758)
  - MaxDD ≤ baseline
  - SLC trade count ≥ baseline (anti-crowding check)

Architecture:
  This script uses the existing BacktestRunner _run_scenario() path but
  pre-detects pullback_continuation TransitionEvents on 1h data and injects
  them into the signal generation context.

Usage:
    python scripts/phase2c/step2a_enhancement_validation.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Project root ──────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))
os.chdir(ROOT_DIR)

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("step2a_validation")

# ── Imports ───────────────────────────────────────────────────────────────────
from research.engine.backtest_runner import BacktestRunner
from core.regime.feature_transition_detector import FeatureTransitionDetector

# ── Constants ─────────────────────────────────────────────────────────────────
DATE_START     = "2022-03-22"
DATE_END       = "2026-03-21"
SYMBOLS        = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
FEE_PER_SIDE   = 0.0004     # 0.04%/side maker fees

# IS/OOS split
IS_END         = "2025-09-22"

REPORT_DIR = ROOT_DIR / "reports" / "phase2c"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _predetect_transitions(runner: BacktestRunner) -> dict:
    """
    Pre-detect pullback_continuation TransitionEvents on 1h data for all symbols.

    Returns a dict:
        {symbol: {1h_bar_timestamp: TransitionEvent}}

    The detection runs the FeatureTransitionDetector over the entire 1h series
    for each symbol, collecting events. These are then indexed by timestamp
    for O(1) lookup during simulation.
    """
    from research.engine.backtest_runner import SLC_1H_TF

    events_by_sym: dict = {}

    for sym in runner.symbols:
        df_1h = runner._ind[sym].get(SLC_1H_TF)
        if df_1h is None or df_1h.empty:
            logger.warning("No 1h data for %s — skipping transition detection", sym)
            events_by_sym[sym] = {}
            continue

        # Also need 4h for HTF gate inside detector
        df_4h = runner._ind[sym].get("4h")

        detector = FeatureTransitionDetector(params={
            # Use confirm OFF for range_breakout (Step 1 finding)
            "range_breakout.require_confirmation_bar": False,
        })

        sym_events: dict = {}

        for loc in range(30, len(df_1h)):
            # Get matching 4h location
            idx_4h = None
            if df_4h is not None and not df_4h.empty:
                ts_1h = df_1h.index[loc]
                idx_4h = int(df_4h.index.searchsorted(ts_1h, side="right")) - 1
                if idx_4h < 0:
                    idx_4h = None

            try:
                events = detector.detect(df_1h, loc, df_4h=df_4h, idx_4h=idx_4h)
            except Exception as e:
                logger.debug("Detector error %s loc=%d: %s", sym, loc, e)
                continue

            for ev in events:
                if ev.event_type == "pullback_continuation":
                    ts = df_1h.index[loc]
                    sym_events[ts] = ev

        events_by_sym[sym] = sym_events
        logger.info(
            "Transition detection %s: %d pullback_continuation events over %d 1h bars",
            sym, len(sym_events), len(df_1h),
        )

    return events_by_sym


def _run_enhanced_scenario(
    runner: BacktestRunner,
    cost_per_side: float,
    transition_events: dict,
    enhancement_enabled: bool,
    mode_b_level: int,
    progress_cb=None,
) -> dict:
    """
    Run the PBL+SLC scenario with optional enhancement layer.

    This is a modified version of BacktestRunner._run_scenario() that:
    1. Looks up pullback_continuation events for each bar
    2. Passes them as context["transition_event"] to PBL and SLC
    3. Applies the configured enhancement settings

    Parameters
    ----------
    transition_events : dict
        {symbol: {timestamp: TransitionEvent}}
    enhancement_enabled : bool
        Whether to activate the enhancement layer
    mode_b_level : int
        0 = Mode A only, 1 = Level 1 safe, 2 = Level 2 aggressive
    """
    import concurrent.futures as _cf
    from collections import defaultdict

    from core.signals.signal_generator import SignalGenerator
    from core.meta_decision.position_sizer import PositionSizer
    from core.regime.research_regime_classifier import (
        regime_to_string as research_regime_to_string,
        BULL_TREND as RES_BULL_TREND,
        BEAR_TREND as RES_BEAR_TREND,
    )
    from config.settings import settings as _s

    # ── Configure enhancement ─────────────────────────────────────────────
    _s.set("phase_2c.pullback_enhancement.enabled", enhancement_enabled, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_flat", 0.10, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.boost_confidence_scale", 0.30, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.mode_b_level", mode_b_level, auto_save=False)
    _s.set("phase_2c.pullback_enhancement.relaxed_strength_cap", 0.75, auto_save=False)
    # RangeBreakout disabled for Step 2a
    _s.set("phase_2c.range_breakout.enabled", False, auto_save=False)

    from research.engine.backtest_runner import (
        PRIMARY_TF, HTF_4H_TF, SLC_1H_TF,
        WARMUP_BARS, MODEL_LOOKBACK, HTF_LOOKBACK, SLC_1H_LOOKBACK,
        INITIAL_CAPITAL, MAX_POSITIONS, POS_FRAC, MAX_HEAT,
    )

    # SignalGenerator per symbol
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

    # Track active transition events with expiry
    # For each symbol, track current event + remaining bars
    _active_events: dict = {}  # {sym: (TransitionEvent, remaining_bars)}

    equity          = INITIAL_CAPITAL
    positions:      dict = {}
    pending_entries:dict = {}
    all_trades:     list = []
    equity_curve:   list = [INITIAL_CAPITAL]
    rejected_heat = rejected_max = rejected_entry_gap = n_signals_gen = 0
    n_boosted = n_relaxed = 0

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

            # ── Update active transition events ───────────────────────────
            # Check if current timestamp corresponds to a 1h bar with a new event
            for sym in runner.symbols:
                if ts in transition_events.get(sym, {}):
                    ev = transition_events[sym][ts]
                    _active_events[sym] = (ev, ev.expires_bars)

                # Decrement remaining bars (1h events expire after N 30m bars ≈ 2×N)
                # Since we iterate 30m bars, a 1h event expires after 2× expires_bars
                if sym in _active_events:
                    ev, remaining = _active_events[sym]
                    remaining -= 1  # Each 30m bar decrements by 1
                    if remaining <= 0:
                        del _active_events[sym]
                    else:
                        _active_events[sym] = (ev, remaining)

            # ── Fill pending entries ──────────────────────────────────────
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                ep_raw = float(runner._opens[sym][loc])
                sig      = pend["signal"]
                sl, tp   = sig.stop_loss, sig.take_profit
                if sig.direction == "long":
                    valid   = sl < ep_raw < tp
                    ep_fill = ep_raw * (1 + cost_per_side)
                else:
                    valid   = tp < ep_raw < sl
                    ep_fill = ep_raw * (1 - cost_per_side)
                del pending_entries[sym]
                if not valid:
                    rejected_entry_gap += 1
                    continue
                positions[sym] = {
                    "direction":   sig.direction,
                    "model":       sig.model_name,
                    "entry_price": ep_fill,
                    "sl": sl, "tp": tp,
                    "size_usdt":   pend["size_usdt"],
                    "entry_bar":   bar_idx,
                    "entry_ts":    ts,
                    "atr_value":   sig.atr_value,
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
                    if pos.get("was_boosted"):
                        n_boosted += 1
                    if pos.get("was_relaxed"):
                        n_relaxed += 1
                    closed.append(sym)
            for sym in closed:
                del positions[sym]
            equity_curve.append(equity)

            # ── Signal generation ──────────────────────────────────────────
            _eligible = [
                sym for sym in runner.symbols
                if sym not in positions and sym not in pending_entries
            ]
            if not _eligible:
                continue

            def _gen_sym(sym):
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

                # Get active transition event for this symbol
                _trans_ev = None
                if sym in _active_events:
                    _trans_ev, _ = _active_events[sym]

                # PBL path
                if _reg30m == RES_BULL_TREND:
                    _pbl_ctx = {}
                    if _trans_ev is not None:
                        _pbl_ctx["transition_event"] = _trans_ev
                        _pbl_ctx["breakout_active"] = False  # No range_breakout in Step 2a
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

                return sym, _sigs

            _sym_results = dict(_sym_pool.map(_gen_sym, _eligible))

            # ── Portfolio gate ────────────────────────────────────────────
            for sym in _eligible:
                _s_sigs = _sym_results.get(sym, [])
                if not _s_sigs:
                    continue
                n_signals_gen += len(_s_sigs)
                sig = _s_sigs[0]
                if sym in pending_entries:
                    continue

                open_by_sym = defaultdict(int)
                for ps in positions:
                    open_by_sym[ps] += 1
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

                # Track if this signal was boosted/relaxed
                _was_boosted = "Phase2c ModeA" in sig.rationale
                _was_relaxed = "ModeA+B" in sig.rationale

                pending_entries[sym] = {
                    "signal":      sig,
                    "size_usdt":   size_usdt,
                    "bar_signal":  bar_idx,
                    "was_boosted": _was_boosted,
                    "was_relaxed": _was_relaxed,
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

    # ── KPIs ──────────────────────────────────────────────────────────────
    elapsed = time.time() - t_sim
    n_trades = len(all_trades)
    winners  = [t for t in all_trades if t["pnl"] > 0]
    losers   = [t for t in all_trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in winners)
    gl = abs(sum(t["pnl"] for t in losers))
    wr = len(winners) / n_trades if n_trades else 0.0
    pf = round(gp / gl, 4) if gl > 0 else 999.0

    eq_arr = np.array(equity_curve)
    peak   = np.maximum.accumulate(eq_arr)
    dd     = (eq_arr - peak) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.min()) * 100

    years = (pd.Timestamp(DATE_END) - pd.Timestamp(DATE_START)).days / 365.25
    cagr  = ((equity / INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100 if years > 0 else 0.0

    # Per-model breakdown
    pbl_trades = [t for t in all_trades if t["model"] == "pullback_long"]
    slc_trades = [t for t in all_trades if t["model"] == "swing_low_continuation"]

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

    # IS/OOS split (handle tz-aware timestamps from the index)
    is_end_ts = pd.Timestamp(IS_END, tz="UTC")
    def _ts_parse(v):
        t = pd.Timestamp(v)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t

    is_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) < is_end_ts]
    oos_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) >= is_end_ts]

    # Boosted/relaxed trade stats
    boosted_trades = [t for t in all_trades if t.get("was_boosted")]
    relaxed_trades = [t for t in all_trades if t.get("was_relaxed")]

    result = {
        "n_trades":        n_trades,
        "profit_factor":   pf,
        "win_rate":        round(wr, 4),
        "cagr_pct":        round(cagr, 2),
        "max_dd_pct":      round(max_dd, 2),
        "final_equity":    round(equity, 2),
        "elapsed_s":       round(elapsed, 1),
        "signals_generated": n_signals_gen,
        "rejected_heat":   rejected_heat,
        "rejected_max":    rejected_max,
        "rejected_entry_gap": rejected_entry_gap,
        # Per-model
        "pbl": _model_kpis(pbl_trades),
        "slc": _model_kpis(slc_trades),
        # IS/OOS
        "is": _model_kpis(is_trades),
        "oos": _model_kpis(oos_trades),
        # Enhancement stats
        "n_boosted":       len(boosted_trades),
        "n_relaxed":       len(relaxed_trades),
        "boosted_kpis":    _model_kpis(boosted_trades) if boosted_trades else {},
        "relaxed_kpis":    _model_kpis(relaxed_trades) if relaxed_trades else {},
    }

    logger.info(
        "Scenario done: n=%d PF=%.4f WR=%.1f%% CAGR=%.1f%% MaxDD=%.1f%% | "
        "PBL=%d SLC=%d boosted=%d relaxed=%d | %.1fs",
        n_trades, pf, wr * 100, cagr, max_dd,
        len(pbl_trades), len(slc_trades),
        len(boosted_trades), len(relaxed_trades), elapsed,
    )

    return result


def main():
    """Run Step 2a validation: baseline vs Mode A vs Mode A+B Level 1."""

    from config.settings import settings as _s

    # ── Ensure PBL/SLC is enabled + disable noisy models ─────────────────
    _s.set("mr_pbl_slc.enabled", True, auto_save=False)
    # Disable all models except PBL+SLC for this test (they're not relevant
    # and their log output drowns everything)
    _s.set("disabled_models", [
        "mean_reversion", "liquidity_sweep", "trend", "donchian_breakout",
        "momentum_breakout", "funding_rate", "sentiment", "range_accumulation",
        "range_breakout",
    ], auto_save=False)
    # Suppress noisy loggers — we only want step2a_validation at INFO
    logging.getLogger("core.signals").setLevel(logging.WARNING)
    logging.getLogger("core.regime").setLevel(logging.WARNING)
    logging.getLogger("core.features").setLevel(logging.WARNING)
    logging.getLogger("core.meta_decision").setLevel(logging.WARNING)
    logging.getLogger("core.rl").setLevel(logging.WARNING)

    logger.info("=" * 70)
    logger.info("Phase 2c Step 2a — Enhancement Layer Validation")
    logger.info("=" * 70)

    # ── Load data once (shared across all scenarios) ──────────────────────
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

    # ── Pre-detect transition events ──────────────────────────────────────
    logger.info("Pre-detecting pullback_continuation events on 1h data...")
    t1 = time.time()
    transition_events = _predetect_transitions(runner)
    t_detect = time.time() - t1

    total_events = sum(len(v) for v in transition_events.values())
    logger.info(
        "Transition detection complete: %d total events across %d symbols (%.1fs)",
        total_events, len(runner.symbols), t_detect,
    )

    results = {}

    # ── Scenario 1: BASELINE (no enhancement) ─────────────────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 1: BASELINE (no enhancement)")
    logger.info("=" * 50)

    results["baseline"] = _run_enhanced_scenario(
        runner=runner,
        cost_per_side=FEE_PER_SIDE,
        transition_events=transition_events,
        enhancement_enabled=False,
        mode_b_level=0,
        progress_cb=_progress,
    )

    # ── Scenario 2: MODE_A_ONLY (boost, no relaxation) ────────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 2: MODE A ONLY (boost, no relaxation)")
    logger.info("=" * 50)

    results["mode_a_only"] = _run_enhanced_scenario(
        runner=runner,
        cost_per_side=FEE_PER_SIDE,
        transition_events=transition_events,
        enhancement_enabled=True,
        mode_b_level=0,
        progress_cb=_progress,
    )

    # ── Scenario 3: MODE_A + B Level 1 (safe relaxation) ──────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Scenario 3: MODE A + MODE B Level 1 (safe relaxation)")
    logger.info("=" * 50)

    results["mode_a_b_level1"] = _run_enhanced_scenario(
        runner=runner,
        cost_per_side=FEE_PER_SIDE,
        transition_events=transition_events,
        enhancement_enabled=True,
        mode_b_level=1,
        progress_cb=_progress,
    )

    # ── Comparison & verdict ──────────────────────────────────────────────
    logger.info("\n" + "=" * 70)
    logger.info("RESULTS COMPARISON")
    logger.info("=" * 70)

    baseline = results["baseline"]

    for name, r in results.items():
        pf_delta = r["profit_factor"] - baseline["profit_factor"]
        logger.info(
            "%-20s  n=%4d  PF=%.4f (Δ%+.4f)  WR=%.1f%%  CAGR=%.1f%%  MaxDD=%.1f%%  "
            "PBL=%d  SLC=%d  boosted=%d  relaxed=%d",
            name, r["n_trades"], r["profit_factor"], pf_delta,
            r["win_rate"] * 100, r["cagr_pct"], r["max_dd_pct"],
            r["pbl"]["n"], r["slc"]["n"],
            r["n_boosted"], r["n_relaxed"],
        )

    # ── Pass/fail checks ──────────────────────────────────────────────────
    logger.info("\n" + "-" * 50)
    logger.info("PASS/FAIL CHECKS (vs baseline)")
    logger.info("-" * 50)

    verdicts = {}
    for name in ["mode_a_only", "mode_a_b_level1"]:
        r = results[name]
        checks = {
            "pf_ge_baseline":     r["profit_factor"] >= baseline["profit_factor"],
            "maxdd_le_baseline":  r["max_dd_pct"] >= baseline["max_dd_pct"],  # more negative = worse
            "slc_n_ge_baseline":  r["slc"]["n"] >= baseline["slc"]["n"],
        }
        overall = all(checks.values())
        verdicts[name] = {"checks": checks, "pass": overall}

        status = "✅ PASS" if overall else "❌ FAIL"
        logger.info(
            "  %-20s  %s  (PF≥base: %s, MaxDD≤base: %s, SLC_n≥base: %s)",
            name, status,
            "✓" if checks["pf_ge_baseline"] else "✗",
            "✓" if checks["maxdd_le_baseline"] else "✗",
            "✓" if checks["slc_n_ge_baseline"] else "✗",
        )

    # ── Save results ──────────────────────────────────────────────────────
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "date_range": f"{DATE_START} to {DATE_END}",
        "symbols": SYMBOLS,
        "fee_per_side": FEE_PER_SIDE,
        "is_oos_split": IS_END,
        "transition_events_total": total_events,
        "transition_events_by_symbol": {s: len(v) for s, v in transition_events.items()},
        "scenarios": results,
        "verdicts": verdicts,
    }

    report_path = REPORT_DIR / "step2a_enhancement_validation_results.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("\nResults saved to: %s", report_path)
    logger.info("Total runtime: %.1fs", time.time() - t0)

    return report


if __name__ == "__main__":
    report = main()
