"""
research/engine/backtest_runner.py
====================================
Canonical NexusTrader backtest engine — importable module.

Wraps the run_scenario() logic from scripts/mr_pbl_slc_research/backtest_v9_system.py
using the EXACT same production classes:
  - SignalGenerator.generate()
  - PositionSizer.calculate_pos_frac()
  - ResearchRegimeClassifier.classify_series() + regime_to_string()

Parameter injection is via config.settings (in-memory only, never saved to disk).
Use BacktestRunner.run(params) with a dict of settings_key → value.

IMPORTANT: This module is the ONLY canonical backtest engine for NexusTrader.
Do not create alternative simulation paths. All Research Lab experiments
must go through this class.
"""
from __future__ import annotations

import hashlib
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── Constants (match backtest_v9_system.py exactly) ───────────────────────────
SYMBOLS         = ["BTC/USDT", "SOL/USDT", "ETH/USDT"]
PRIMARY_TF      = "30m"
HTF_4H_TF       = "4h"
SLC_1H_TF       = "1h"
INITIAL_CAPITAL = 100_000.0
DEFAULT_COST    = 0.0004
POS_FRAC        = 0.35
MAX_HEAT        = 0.80
MAX_POSITIONS   = 10
WARMUP_BARS     = 120
MODEL_LOOKBACK  = 350
HTF_LOOKBACK    = 60
SLC_1H_LOOKBACK = 150
DATA_DIR        = ROOT / "backtest_data"


def _fingerprint_parquet(path: Path) -> str:
    """SHA-256 of first 64 KB of a parquet file (fast, stable)."""
    if not path.exists():
        return "missing"
    with open(path, "rb") as f:
        data = f.read(65536)
    return hashlib.sha256(data).hexdigest()[:16]


class BacktestRunner:
    """
    Canonical backtest engine using production NexusTrader classes.

    Usage
    -----
    runner = BacktestRunner()
    runner.load_data()                         # slow — once per instance
    result = runner.run_baseline()             # default params, 0.04%/side fees
    result = runner.run(params, cost=0.0)      # custom params, zero fees

    Parameters
    ----------
    date_start : str | None   e.g. "2022-03-22"
    date_end   : str | None   e.g. "2026-03-21"
    symbols    : list | None  subset of SYMBOLS
    """

    def __init__(
        self,
        date_start: Optional[str] = None,
        date_end:   Optional[str] = None,
        symbols:    Optional[list[str]] = None,
    ):
        self.date_start = pd.Timestamp(date_start, tz="UTC") if date_start else None
        self.date_end   = pd.Timestamp(date_end,   tz="UTC") if date_end   else None
        self.symbols    = symbols or SYMBOLS
        self._data_loaded = False
        self._raw:  dict[str, dict[str, pd.DataFrame]] = {}
        self._ind:  dict[str, dict[str, pd.DataFrame]] = {}
        self._reg30: dict[str, np.ndarray] = {}
        self._reg1h: dict[str, np.ndarray] = {}
        self._master_ts: list = []
        self._fingerprints: dict[str, str] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def load_data(self, progress_cb=None) -> None:
        """
        Load parquet files, compute indicators, pre-classify regimes.
        Call once; results are cached for multiple run() calls.

        progress_cb: optional callable(step: str, pct: float)
        """
        if self._data_loaded:
            return

        def _cb(msg, pct):
            if progress_cb:
                progress_cb(msg, pct)

        _cb("Loading parquet files…", 5)
        self._load_raw()

        _cb("Computing indicators…", 30)
        self._compute_indicators()

        _cb("Pre-classifying regimes…", 60)
        self._precompute_regimes()

        _cb("Building master timeline…", 80)
        btc_30m = self._ind.get("BTC/USDT", {}).get(PRIMARY_TF)
        if btc_30m is not None and not btc_30m.empty:
            ts = btc_30m.index
            if self.date_start:
                ts = ts[ts >= self.date_start]
            if self.date_end:
                ts = ts[ts <= self.date_end]
            self._master_ts = list(ts)
        else:
            logger.error("No BTC/USDT 30m data")
            self._master_ts = []

        _cb("Data ready", 100)
        self._data_loaded = True
        logger.info(
            "BacktestRunner loaded: %d master bars, %d symbols",
            len(self._master_ts), len(self.symbols),
        )

    def run_baseline(self) -> dict:
        """Run with production defaults and 0.04%/side fees."""
        return self.run(params={}, cost_per_side=DEFAULT_COST)

    def run(
        self,
        params: dict[str, Any] | None = None,
        cost_per_side: float = DEFAULT_COST,
        progress_cb=None,
    ) -> dict:
        """
        Run backtest with optional parameter overrides.

        params: dict mapping settings_key → value, e.g.:
            {"mr_pbl_slc.pullback_long.ema_prox_atr_mult": 0.6}
            Unspecified parameters use their production defaults.

        Returns a metrics dict including per-trade list.
        """
        if not self._data_loaded:
            self.load_data(progress_cb=progress_cb)

        if not self._master_ts:
            return {"error": "No master timeline — check BTC/USDT 30m data"}

        # ── Inject parameter overrides into settings (in-memory only) ─────
        _applied = {}
        if params:
            try:
                from config.settings import settings as _s
                for k, v in params.items():
                    _s.set(k, v)
                    _applied[k] = v
            except Exception as e:
                logger.warning("settings override failed: %s", e)

        # ── Ensure mr_pbl_slc is enabled ──────────────────────────────────
        try:
            from config.settings import settings as _s
            _s.set("mr_pbl_slc.enabled", True)
            _s.set("mr_pbl_slc.pos_frac", POS_FRAC)
            _s.set("mr_pbl_slc.max_heat", MAX_HEAT)
            _s.set("mr_pbl_slc.max_positions", MAX_POSITIONS)
        except Exception:
            pass

        result = self._run_scenario(cost_per_side, progress_cb)
        result["params_applied"] = _applied
        result["data_fingerprints"] = self._fingerprints
        return result

    def dataset_fingerprints(self) -> dict[str, str]:
        """Return SHA-256 fingerprints of all parquet files."""
        fps = {}
        for sym in self.symbols:
            key = sym.replace("/", "_")
            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                path = DATA_DIR / f"{key}_{tf}.parquet"
                fps[f"{sym}/{tf}"] = _fingerprint_parquet(path)
        return fps

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _load_raw(self):
        from core.regime.research_regime_classifier import (
            classify_series  as _classify,
            BULL_TREND, BEAR_TREND,
        )
        for sym in self.symbols:
            slug = sym.replace("/", "_")
            self._raw[sym] = {}
            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                fp = DATA_DIR / f"{slug}_{tf}.parquet"
                self._fingerprints[f"{sym}/{tf}"] = _fingerprint_parquet(fp)
                if fp.exists():
                    df = pd.read_parquet(fp)
                    if "timestamp" in df.columns:
                        df = df.set_index("timestamp")
                    df.index = pd.to_datetime(df.index, utc=True)
                    df = df.sort_index()
                    # Date slicing
                    if self.date_start:
                        df = df[df.index >= self.date_start]
                    if self.date_end:
                        df = df[df.index <= self.date_end]
                    self._raw[sym][tf] = df
                else:
                    self._raw[sym][tf] = pd.DataFrame()
                    logger.warning("Missing: %s", fp)

    def _compute_indicators(self):
        from core.features.indicator_library import calculate_all, calculate_scan_mode
        for sym in self.symbols:
            self._ind[sym] = {}
            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                df = self._raw[sym].get(tf, pd.DataFrame())
                if df.empty:
                    self._ind[sym][tf] = pd.DataFrame()
                    continue
                try:
                    fn = calculate_all if tf == PRIMARY_TF else calculate_scan_mode
                    self._ind[sym][tf] = fn(df.copy())
                except Exception as exc:
                    logger.warning("Indicator fail %s %s: %s", sym, tf, exc)
                    self._ind[sym][tf] = pd.DataFrame()

    def _precompute_regimes(self):
        from core.regime.research_regime_classifier import (
            classify_series as _classify,
            BULL_TREND, BEAR_TREND,
        )
        for sym in self.symbols:
            df30 = self._ind[sym].get(PRIMARY_TF, pd.DataFrame())
            self._reg30[sym] = (
                _classify(df30) if not df30.empty else np.array([], dtype=np.int8)
            )
            df1h = self._ind[sym].get(SLC_1H_TF, pd.DataFrame())
            self._reg1h[sym] = (
                _classify(df1h) if not df1h.empty else np.array([], dtype=np.int8)
            )

    def _run_scenario(self, cost_per_side: float, progress_cb=None) -> dict:
        """Core simulation — exact port of backtest_v9_system.run_scenario()."""
        from core.signals.signal_generator import SignalGenerator
        from core.meta_decision.position_sizer import PositionSizer
        from core.regime.research_regime_classifier import (
            regime_to_string as research_regime_to_string,
            BULL_TREND as RES_BULL_TREND,
            BEAR_TREND as RES_BEAR_TREND,
        )

        sig_gen = SignalGenerator()
        sig_gen._warmup_complete = True
        sizer   = PositionSizer()

        # Index structures for O(log n) lookups
        idx30: dict = {}
        idx4h: dict = {}
        idx1h: dict = {}
        for sym in self.symbols:
            df = self._ind[sym].get(PRIMARY_TF)
            idx30[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
            df = self._ind[sym].get(HTF_4H_TF)
            idx4h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])
            df = self._ind[sym].get(SLC_1H_TF)
            idx1h[sym] = df.index if (df is not None and not df.empty) else pd.DatetimeIndex([])

        equity          = INITIAL_CAPITAL
        positions:      dict[str, dict] = {}
        pending_entries:dict[str, dict] = {}
        all_trades:     list[dict]      = []
        equity_curve:   list[float]     = [INITIAL_CAPITAL]
        rejected_heat = rejected_max = rejected_entry_gap = n_signals_gen = 0

        t_sim = time.time()
        total = len(self._master_ts)

        for bar_idx, ts in enumerate(self._master_ts):
            if bar_idx < WARMUP_BARS:
                continue

            if progress_cb and bar_idx % 2000 == 0:
                progress_cb(
                    f"Simulating bar {bar_idx}/{total}…",
                    10 + int(bar_idx / total * 80),
                )

            # ── Fill pending entries ──────────────────────────────────────
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                row_open = self._ind[sym][PRIMARY_TF].iloc[loc]
                ep_raw   = float(row_open["open"])
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
                }

            # ── SL/TP check ───────────────────────────────────────────────
            closed = []
            for sym, pos in list(positions.items()):
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                row  = self._ind[sym][PRIMARY_TF].iloc[loc]
                hi, lo = float(row["high"]), float(row["low"])
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

            # ── Signal generation ─────────────────────────────────────────
            for sym in self.symbols:
                if sym in positions:
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                if loc < WARMUP_BARS:
                    continue

                res30 = self._reg30.get(sym, np.array([]))
                res_regime_30m = int(res30[loc]) if loc < len(res30) else 0
                loc1h = int(idx1h[sym].searchsorted(ts, side="right")) - 1
                res1h = self._reg1h.get(sym, np.array([]))
                res_regime_1h = int(res1h[loc1h]) if 0 <= loc1h < len(res1h) else 0

                if res_regime_30m != RES_BULL_TREND and res_regime_1h != RES_BEAR_TREND:
                    continue

                res_str_30m = research_regime_to_string(res_regime_30m)
                res_str_1h  = research_regime_to_string(res_regime_1h)

                s30 = max(0, loc - MODEL_LOOKBACK + 1)
                df_window = self._ind[sym][PRIMARY_TF].iloc[s30 : loc + 1]
                if len(df_window) < 70:
                    continue

                signals = []

                # PBL path
                if res_regime_30m == RES_BULL_TREND:
                    pbl_ctx: dict = {}
                    loc4h = int(idx4h[sym].searchsorted(ts, side="right"))
                    if loc4h >= HTF_LOOKBACK:
                        pbl_ctx["df_4h"] = self._ind[sym][HTF_4H_TF].iloc[
                            max(0, loc4h - HTF_LOOKBACK) : loc4h
                        ]
                    try:
                        raw = sig_gen.generate(
                            sym, df_window, res_str_30m, PRIMARY_TF,
                            regime_probs={}, context=pbl_ctx,
                        ) or []
                        signals.extend(s for s in raw if s.model_name == "pullback_long")
                    except Exception as exc:
                        logger.debug("SG PBL %s: %s", sym, exc)

                # SLC path
                if res_regime_1h == RES_BEAR_TREND and loc1h >= 15:
                    slc_ctx = {
                        "df_1h": self._ind[sym][SLC_1H_TF].iloc[
                            max(0, loc1h - SLC_1H_LOOKBACK + 1) : loc1h + 1
                        ]
                    }
                    try:
                        raw = sig_gen.generate(
                            sym, df_window, res_str_1h, PRIMARY_TF,
                            regime_probs={}, context=slc_ctx,
                        ) or []
                        signals.extend(s for s in raw if s.model_name == "swing_low_continuation")
                    except Exception as exc:
                        logger.debug("SG SLC %s: %s", sym, exc)

                if not signals:
                    continue
                n_signals_gen += len(signals)
                sig = signals[0]
                if sym in pending_entries:
                    continue

                open_by_sym: dict[str, int] = defaultdict(int)
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

                pending_entries[sym] = {
                    "signal":     sig,
                    "size_usdt":  size_usdt,
                    "bar_signal": bar_idx,
                }

        # ── Force-close remaining ─────────────────────────────────────────
        if self._master_ts:
            last_ts = self._master_ts[-1]
            for sym, pos in list(positions.items()):
                df30 = self._ind[sym].get(PRIMARY_TF)
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
                    "entry_ts": pos["entry_ts"], "exit_ts": last_ts,
                    "entry_price": ep, "exit_price": last_close,
                    "size_usdt": size, "pnl": round(pnl, 4),
                    "r_value": round(r_val, 4), "exit_reason": "force_close",
                    "bars_held": 0,
                })

        # ── KPIs ──────────────────────────────────────────────────────────
        n_trades = len(all_trades)
        winners  = [t for t in all_trades if t["pnl"] > 0]
        losers   = [t for t in all_trades if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in winners)
        gl = abs(sum(t["pnl"] for t in losers))
        wr = len(winners) / n_trades if n_trades else 0.0
        pf = gp / gl if gl > 0 else float("inf")

        if self._master_ts:
            years = (self._master_ts[-1] - self._master_ts[0]).days / 365.25
        else:
            years = 4.0
        cagr = (equity / INITIAL_CAPITAL) ** (1.0 / max(years, 0.1)) - 1.0

        eq_arr = np.array(equity_curve)
        peak   = np.maximum.accumulate(eq_arr)
        mdd    = float(((eq_arr - peak) / peak).min()) if len(eq_arr) > 1 else 0.0

        pbl_trades = [t for t in all_trades if t["model"] == "pullback_long"]
        slc_trades = [t for t in all_trades if t["model"] == "swing_low_continuation"]
        pbl_winners = [t for t in pbl_trades if t["pnl"] > 0]
        slc_winners = [t for t in slc_trades if t["pnl"] > 0]
        pbl_gl = abs(sum(t["pnl"] for t in pbl_trades if t["pnl"] <= 0))
        slc_gl = abs(sum(t["pnl"] for t in slc_trades if t["pnl"] <= 0))
        pbl_gp = sum(t["pnl"] for t in pbl_winners)
        slc_gp = sum(t["pnl"] for t in slc_winners)

        elapsed = time.time() - t_sim
        logger.info(
            "BacktestRunner done: %d trades in %.1fs  PF=%.4f  WR=%.1f%%  CAGR=%.1f%%",
            n_trades, elapsed, pf, wr * 100, cagr * 100,
        )

        return {
            "n_trades":        n_trades,
            "profit_factor":   round(pf, 4),
            "win_rate":        round(wr, 4),
            "cagr":            round(cagr, 4),
            "max_drawdown":    round(mdd, 4),
            "final_equity":    round(equity, 2),
            "years":           round(years, 2),
            "cost_per_side":   cost_per_side,
            "signals_generated": n_signals_gen,
            "rejected_heat":   rejected_heat,
            "rejected_max":    rejected_max,
            "rejected_entry_gap": rejected_entry_gap,
            "elapsed_s":       round(elapsed, 1),
            "pbl_n":           len(pbl_trades),
            "pbl_pf":          round(pbl_gp / pbl_gl, 4) if pbl_gl > 0 else 999.0,
            "pbl_wr":          round(len(pbl_winners) / len(pbl_trades), 4) if pbl_trades else 0.0,
            "slc_n":           len(slc_trades),
            "slc_pf":          round(slc_gp / slc_gl, 4) if slc_gl > 0 else 999.0,
            "slc_wr":          round(len(slc_winners) / len(slc_trades), 4) if slc_trades else 0.0,
            "all_trades":      all_trades,
        }
