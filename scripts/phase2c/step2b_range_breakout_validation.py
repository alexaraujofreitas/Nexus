#!/usr/bin/env python3
"""
Phase 2c Step 2b — RangeBreakout Standalone Validation

Validates the RangeBreakoutModel as a standalone execution model triggered
by range_breakout TransitionEvents from FeatureTransitionDetector.

Variants:
  A. WITH entry buffer  (long: breakout + 0.1×ATR, short: breakout − 0.1×ATR)
  B. WITHOUT entry buffer (entry at exact breakout level)

Execution realism:
  - Next-bar-open entry (pending_entries buffer, bar i signal → bar i+1 fill)
  - 0.04%/side fee + 0.03%/side slippage = 0.07% total per side

Reports:
  - PF, WR, Avg R, false breakout rate
  - IS/OOS stability
  - Per-asset breakdown
  - Confidence bucket breakdown
  - Buffered vs non-buffered explicit comparison

Usage:
    python scripts/phase2c/step2b_range_breakout_validation.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
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
logger = logging.getLogger("step2b_validation")

# Suppress noisy sub-module loggers
logging.getLogger("core.signals").setLevel(logging.WARNING)
logging.getLogger("core.regime").setLevel(logging.WARNING)
logging.getLogger("core.features").setLevel(logging.WARNING)
logging.getLogger("core.meta_decision").setLevel(logging.WARNING)
logging.getLogger("core.rl").setLevel(logging.WARNING)

from research.engine.backtest_runner import BacktestRunner
from core.regime.feature_transition_detector import (
    FeatureTransitionDetector,
    TransitionEvent,
)

# ── Constants ─────────────────────────────────────────────────────────────────
DATE_START      = "2022-03-22"
DATE_END        = "2026-03-21"
SYMBOLS         = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
FEE_PER_SIDE    = 0.0004   # 0.04%
SLIP_PER_SIDE   = 0.0003   # 0.03%
COST_PER_SIDE   = FEE_PER_SIDE + SLIP_PER_SIDE  # 0.07% total

IS_END          = "2025-09-22"
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS   = 10
POS_FRAC        = 0.35     # per-trade capital fraction

REPORT_DIR = ROOT_DIR / "reports" / "phase2c"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Confidence buckets for breakdown
CONF_BUCKETS = [
    ("low",  0.35, 0.50),
    ("mid",  0.50, 0.65),
    ("high", 0.65, 0.86),
]


def _predetect_range_breakout_events(runner: BacktestRunner) -> dict:
    """
    Pre-detect ALL range_breakout TransitionEvents on 1h data.

    Returns:
        {symbol: {1h_bar_timestamp: TransitionEvent}}
    """
    from research.engine.backtest_runner import SLC_1H_TF

    events_by_sym: dict = {}

    for sym in runner.symbols:
        df_1h = runner._ind[sym].get(SLC_1H_TF)
        if df_1h is None or df_1h.empty:
            logger.warning("No 1h data for %s — skipping", sym)
            events_by_sym[sym] = {}
            continue

        df_4h = runner._ind[sym].get("4h")

        detector = FeatureTransitionDetector(params={
            "range_breakout.require_confirmation_bar": False,
        })

        sym_events: dict = {}

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
                if ev.event_type == "range_breakout":
                    ts = df_1h.index[loc]
                    sym_events[ts] = ev

        events_by_sym[sym] = sym_events
        logger.info(
            "Range breakout detection %s: %d events over %d 1h bars",
            sym, len(sym_events), len(df_1h),
        )

    return events_by_sym


def _run_rb_scenario(
    runner: BacktestRunner,
    cost_per_side: float,
    rb_events: dict,
    entry_buffer_atr: float,
    progress_cb=None,
) -> dict:
    """
    Run standalone RangeBreakout simulation.

    Entry logic:
      - Signal at bar i when a range_breakout event is active
      - Fill at bar i+1's open (next-bar-open execution)
      - Entry price: event range_high/low ± buffer
      - SL: inside range with 10% cushion
      - TP: entry ± 1.0 × range_width

    Parameters
    ----------
    entry_buffer_atr : float
        Entry offset in ATR units. 0.0 = no buffer.
    """
    from research.engine.backtest_runner import (
        PRIMARY_TF, SLC_1H_TF, WARMUP_BARS,
    )

    # Index structures
    idx30: dict = {}
    idx1h: dict = {}
    for sym in runner.symbols:
        df = runner._ind[sym].get(PRIMARY_TF)
        idx30[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
        df = runner._ind[sym].get(SLC_1H_TF)
        idx1h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])

    # Track active events with expiry (decay per 30m bar)
    _active_events: dict = {}  # {sym: (TransitionEvent, remaining_30m_bars)}

    # SL/TP constants
    SL_RANGE_PCT = 0.10
    TP_RANGE_MULT = 1.0
    MIN_CONFIDENCE = 0.35

    equity          = INITIAL_CAPITAL
    positions:      dict = {}
    pending_entries: dict = {}
    all_trades:     list = []
    equity_curve:   list = [INITIAL_CAPITAL]
    n_signals = n_false_breakout = 0

    t_sim = time.time()
    total = len(runner._master_ts)
    _last_prog_t = t_sim

    for bar_idx, ts in enumerate(runner._master_ts):
        if bar_idx < WARMUP_BARS:
            continue

        # ── Progress ──────────────────────────────────────────────────────
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

        # ── Update active range_breakout events ──────────────────────────
        for sym in runner.symbols:
            if ts in rb_events.get(sym, {}):
                ev = rb_events[sym][ts]
                # 3 1h bars = ~6 30m bars validity
                _active_events[sym] = (ev, ev.expires_bars * 2)

            if sym in _active_events:
                ev, remaining = _active_events[sym]
                remaining -= 1
                if remaining <= 0:
                    del _active_events[sym]
                else:
                    _active_events[sym] = (ev, remaining)

        # ── Fill pending entries (next-bar-open) ──────────────────────────
        for sym, pend in list(pending_entries.items()):
            if sym in positions:
                del pending_entries[sym]
                continue
            loc = int(idx30[sym].searchsorted(ts))
            if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                continue

            ep_raw = float(runner._opens[sym][loc])
            direction = pend["direction"]
            sl, tp = pend["sl"], pend["tp"]

            # Validate SL < EP < TP (long) or TP < EP < SL (short)
            if direction == "long":
                valid = sl < ep_raw < tp
                ep_fill = ep_raw * (1 + cost_per_side)
            else:
                valid = tp < ep_raw < sl
                ep_fill = ep_raw * (1 - cost_per_side)

            del pending_entries[sym]
            if not valid:
                n_false_breakout += 1
                continue

            positions[sym] = {
                "direction":   direction,
                "entry_price": ep_fill,
                "sl": sl, "tp": tp,
                "size_usdt":   pend["size_usdt"],
                "entry_bar":   bar_idx,
                "entry_ts":    ts,
                "confidence":  pend["confidence"],
                "range_width": pend["range_width"],
                "range_high":  pend["range_high"],
                "range_low":   pend["range_low"],
                "event_ts":    pend["event_ts"],
            }

        # ── SL/TP check ──────────────────────────────────────────────────
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
                all_trades.append({
                    "symbol": sym, "direction": d,
                    "entry_ts": str(pos["entry_ts"]), "exit_ts": str(ts),
                    "entry_price": ep, "exit_price": exit_px,
                    "size_usdt": size, "pnl": round(pnl, 4),
                    "r_value": round(r_val, 4), "exit_reason": reason,
                    "bars_held": bar_idx - pos["entry_bar"],
                    "confidence": pos["confidence"],
                    "range_width": pos["range_width"],
                    "range_high": pos["range_high"],
                    "range_low": pos["range_low"],
                })
                closed.append(sym)
        for sym in closed:
            del positions[sym]
        equity_curve.append(equity)

        # ── Signal generation from active events ─────────────────────────
        for sym in runner.symbols:
            if sym in positions or sym in pending_entries:
                continue
            if sym not in _active_events:
                continue

            ev, _ = _active_events[sym]
            snap = ev.features_snapshot
            range_high = snap.get("range_high")
            range_low = snap.get("range_low")
            if range_high is None or range_low is None:
                continue

            range_width = range_high - range_low
            if range_width <= 0:
                continue

            direction = ev.direction
            confidence = ev.confidence
            if confidence < MIN_CONFIDENCE:
                continue

            # Get current ATR from 30m data
            loc = int(idx30[sym].searchsorted(ts))
            if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                continue
            if loc < 30:
                continue

            df30 = runner._ind[sym].get(PRIMARY_TF)
            atr_col = df30.get("atr_14") if df30 is not None else None
            if atr_col is None or loc >= len(atr_col):
                continue
            atr = float(atr_col.iloc[loc])
            if atr <= 0 or np.isnan(atr):
                continue

            # Entry price
            buffer = entry_buffer_atr * atr
            if direction == "long":
                entry_price = range_high + buffer
            else:
                entry_price = range_low - buffer

            # SL / TP
            cushion = SL_RANGE_PCT * range_width
            if direction == "long":
                sl = range_low - cushion
                tp = entry_price + TP_RANGE_MULT * range_width
            else:
                sl = range_high + cushion
                tp = entry_price - TP_RANGE_MULT * range_width

            # Validate levels
            if direction == "long":
                if not (sl < entry_price < tp):
                    continue
            else:
                if not (tp < entry_price < sl):
                    continue

            # R:R check
            risk = abs(entry_price - sl)
            reward = abs(tp - entry_price)
            if risk <= 0 or reward / risk < 0.8:
                continue

            # Position sizing
            open_count = len(positions) + len(pending_entries)
            if open_count >= MAX_POSITIONS:
                continue
            size_usdt = equity * POS_FRAC
            if size_usdt <= 0:
                continue

            n_signals += 1
            pending_entries[sym] = {
                "direction":   direction,
                "sl":          sl,
                "tp":          tp,
                "size_usdt":   size_usdt,
                "bar_signal":  bar_idx,
                "confidence":  confidence,
                "range_width": range_width,
                "range_high":  range_high,
                "range_low":   range_low,
                "event_ts":    str(ts),
            }

    # ── Force-close remaining ─────────────────────────────────────────────
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
            all_trades.append({
                "symbol": sym, "direction": d,
                "entry_ts": str(pos["entry_ts"]), "exit_ts": str(last_ts),
                "entry_price": ep, "exit_price": last_close,
                "size_usdt": size, "pnl": round(pnl, 4),
                "r_value": round(r_val, 4), "exit_reason": "force_close",
                "bars_held": 0,
                "confidence": pos["confidence"],
                "range_width": pos["range_width"],
                "range_high": pos["range_high"],
                "range_low": pos["range_low"],
            })

    # ── KPIs ──────────────────────────────────────────────────────────────
    elapsed = time.time() - t_sim
    n_trades = len(all_trades)
    winners = [t for t in all_trades if t["pnl"] > 0]
    losers = [t for t in all_trades if t["pnl"] <= 0]
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

    # False breakout rate: signals that failed next-bar-open validation
    false_bk_rate = n_false_breakout / n_signals if n_signals > 0 else 0.0

    # SL hit rate
    sl_trades = [t for t in all_trades if t["exit_reason"] == "sl"]
    sl_rate = len(sl_trades) / n_trades if n_trades else 0.0

    # ── IS/OOS split ──────────────────────────────────────────────────────
    is_end_ts = pd.Timestamp(IS_END, tz="UTC")

    def _ts_parse(v):
        t = pd.Timestamp(v)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t

    is_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) < is_end_ts]
    oos_trades = [t for t in all_trades if _ts_parse(t["entry_ts"]) >= is_end_ts]

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
            "sl_rate": round(len(_sl) / n, 4) if n else 0.0,
        }

    # ── Per-asset breakdown ───────────────────────────────────────────────
    per_asset = {}
    for sym in runner.symbols:
        sym_trades = [t for t in all_trades if t["symbol"] == sym]
        per_asset[sym] = _kpis(sym_trades)

    # ── Per-direction breakdown ───────────────────────────────────────────
    long_trades = [t for t in all_trades if t["direction"] == "long"]
    short_trades = [t for t in all_trades if t["direction"] == "short"]

    # ── Confidence bucket breakdown ───────────────────────────────────────
    conf_buckets = {}
    for name, lo_b, hi_b in CONF_BUCKETS:
        bucket_trades = [t for t in all_trades if lo_b <= t["confidence"] < hi_b]
        conf_buckets[name] = _kpis(bucket_trades)

    result = {
        "entry_buffer_atr": entry_buffer_atr,
        "cost_per_side":    cost_per_side,
        "n_trades":         n_trades,
        "profit_factor":    pf,
        "win_rate":         round(wr, 4),
        "avg_r":            avg_r,
        "cagr_pct":         round(cagr, 2),
        "max_dd_pct":       round(max_dd, 2),
        "final_equity":     round(equity, 2),
        "elapsed_s":        round(elapsed, 1),
        "n_signals":        n_signals,
        "n_false_breakout": n_false_breakout,
        "false_breakout_rate": round(false_bk_rate, 4),
        "sl_rate":          round(sl_rate, 4),
        "is":               _kpis(is_trades),
        "oos":              _kpis(oos_trades),
        "per_asset":        per_asset,
        "long":             _kpis(long_trades),
        "short":            _kpis(short_trades),
        "confidence_buckets": conf_buckets,
    }

    logger.info(
        "RB scenario (buffer=%.2f): n=%d PF=%.4f WR=%.1f%% AvgR=%.4f "
        "CAGR=%.1f%% MaxDD=%.1f%% FalseBK=%.1f%% SL_rate=%.1f%% | %.1fs",
        entry_buffer_atr, n_trades, pf, wr * 100, avg_r,
        cagr, max_dd, false_bk_rate * 100, sl_rate * 100, elapsed,
    )

    return result


def main():
    from config.settings import settings as _s

    # Enable required config gates
    _s.set("mr_pbl_slc.enabled", True, auto_save=False)
    _s.set("phase_2c.range_breakout.enabled", True, auto_save=False)
    # Disable all models except range_breakout (standalone test)
    _s.set("disabled_models", [
        "mean_reversion", "liquidity_sweep", "trend", "donchian_breakout",
        "momentum_breakout", "funding_rate", "sentiment", "range_accumulation",
        "pullback_long", "swing_low_continuation",
    ], auto_save=False)

    logger.info("=" * 70)
    logger.info("Phase 2c Step 2b — RangeBreakout Standalone Validation")
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
        mode="pbl_slc",  # mode doesn't matter; we use our own sim loop
    )

    def _progress(msg, pct):
        logger.info("  [%d%%] %s", pct, msg)

    runner.load_data(progress_cb=_progress)
    t_load = time.time() - t0
    logger.info("Data loaded in %.1fs", t_load)

    # ── Pre-detect range_breakout events ──────────────────────────────────
    logger.info("Pre-detecting range_breakout events on 1h data...")
    t1 = time.time()
    rb_events = _predetect_range_breakout_events(runner)
    t_detect = time.time() - t1

    total_events = sum(len(v) for v in rb_events.values())
    logger.info(
        "Range breakout detection complete: %d total events across %d symbols (%.1fs)",
        total_events, len(runner.symbols), t_detect,
    )
    for sym in runner.symbols:
        n_ev = len(rb_events.get(sym, {}))
        # Direction breakdown
        longs = sum(1 for ev in rb_events.get(sym, {}).values() if ev.direction == "long")
        shorts = n_ev - longs
        logger.info("  %s: %d events (%d long, %d short)", sym, n_ev, longs, shorts)

    results = {}

    # ── Variant A: WITH entry buffer (0.1 × ATR) ─────────────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Variant A: WITH entry buffer (0.1 × ATR)")
    logger.info("=" * 50)

    results["with_buffer"] = _run_rb_scenario(
        runner=runner,
        cost_per_side=COST_PER_SIDE,
        rb_events=rb_events,
        entry_buffer_atr=0.1,
        progress_cb=_progress,
    )

    # ── Variant B: WITHOUT entry buffer ───────────────────────────────────
    logger.info("\n" + "=" * 50)
    logger.info("Variant B: WITHOUT entry buffer")
    logger.info("=" * 50)

    results["no_buffer"] = _run_rb_scenario(
        runner=runner,
        cost_per_side=COST_PER_SIDE,
        rb_events=rb_events,
        entry_buffer_atr=0.0,
        progress_cb=_progress,
    )

    # ── Comparison ────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 70)
    logger.info("VARIANT COMPARISON")
    logger.info("=" * 70)

    for name, r in results.items():
        logger.info(
            "%-15s  n=%4d  PF=%.4f  WR=%.1f%%  AvgR=%+.4f  CAGR=%.1f%%  "
            "MaxDD=%.1f%%  FalseBK=%.1f%%  SL_rate=%.1f%%",
            name, r["n_trades"], r["profit_factor"], r["win_rate"] * 100,
            r["avg_r"], r["cagr_pct"], r["max_dd_pct"],
            r["false_breakout_rate"] * 100, r["sl_rate"] * 100,
        )

    # IS/OOS stability
    logger.info("\n--- IS / OOS Stability ---")
    for name, r in results.items():
        is_r = r["is"]
        oos_r = r["oos"]
        logger.info(
            "%-15s  IS: n=%d PF=%.4f WR=%.1f%% AvgR=%+.4f  |  "
            "OOS: n=%d PF=%.4f WR=%.1f%% AvgR=%+.4f",
            name, is_r["n"], is_r["pf"], is_r["wr"] * 100, is_r["avg_r"],
            oos_r["n"], oos_r["pf"], oos_r["wr"] * 100, oos_r["avg_r"],
        )

    # Per-asset
    logger.info("\n--- Per-Asset Breakdown ---")
    for name, r in results.items():
        logger.info("  %s:", name)
        for sym, a in r["per_asset"].items():
            logger.info(
                "    %-12s  n=%3d  PF=%.4f  WR=%.1f%%  AvgR=%+.4f  SL_rate=%.1f%%",
                sym, a["n"], a["pf"], a["wr"] * 100, a["avg_r"], a["sl_rate"] * 100,
            )

    # Confidence buckets
    logger.info("\n--- Confidence Bucket Breakdown ---")
    for name, r in results.items():
        logger.info("  %s:", name)
        for bname, b in r["confidence_buckets"].items():
            logger.info(
                "    %-6s  n=%3d  PF=%.4f  WR=%.1f%%  AvgR=%+.4f",
                bname, b["n"], b["pf"], b["wr"] * 100, b["avg_r"],
            )

    # Direction breakdown
    logger.info("\n--- Direction Breakdown ---")
    for name, r in results.items():
        l = r["long"]
        s = r["short"]
        logger.info(
            "%-15s  LONG: n=%d PF=%.4f WR=%.1f%%  |  SHORT: n=%d PF=%.4f WR=%.1f%%",
            name, l["n"], l["pf"], l["wr"] * 100,
            s["n"], s["pf"], s["wr"] * 100,
        )

    # ── Select winner ─────────────────────────────────────────────────────
    logger.info("\n" + "-" * 50)
    logger.info("WINNER SELECTION")
    logger.info("-" * 50)

    wb = results["with_buffer"]
    nb = results["no_buffer"]

    # Primary: PF, secondary: AvgR, tertiary: false_breakout_rate (lower = better)
    wb_score = (wb["profit_factor"], wb["avg_r"], -wb["false_breakout_rate"])
    nb_score = (nb["profit_factor"], nb["avg_r"], -nb["false_breakout_rate"])

    if wb_score > nb_score:
        winner = "with_buffer"
        logger.info("WINNER: WITH BUFFER (PF=%.4f > %.4f)", wb["profit_factor"], nb["profit_factor"])
    elif nb_score > wb_score:
        winner = "no_buffer"
        logger.info("WINNER: NO BUFFER (PF=%.4f > %.4f)", nb["profit_factor"], wb["profit_factor"])
    else:
        winner = "with_buffer"  # tie-break: buffer is safer
        logger.info("TIE — defaulting to WITH BUFFER (safer)")

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
        "rb_events_total": total_events,
        "rb_events_by_symbol": {s: len(v) for s, v in rb_events.items()},
        "variants": results,
        "winner": winner,
        "winner_rationale": (
            f"Selected '{winner}' based on PF={results[winner]['profit_factor']:.4f}, "
            f"AvgR={results[winner]['avg_r']:+.4f}, "
            f"FalseBK={results[winner]['false_breakout_rate']*100:.1f}%"
        ),
    }

    report_path = REPORT_DIR / "step2b_range_breakout_validation_results.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("\nResults saved to: %s", report_path)
    logger.info("Total runtime: %.1fs", time.time() - t0)

    return report


if __name__ == "__main__":
    report = main()
