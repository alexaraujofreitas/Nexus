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
CACHE_DIR       = ROOT / "cache" / "indicators"   # persistent parquet/npy cache
INDICATOR_VERSION = "v2.0"   # bump this when indicator_library changes

# ── Confluence mode constants ──────────────────────────────────────────────────
CONFLUENCE_NONE      = "none"           # highest-strength single-winner (default)
CONFLUENCE_TECHNICAL = "technical_only" # technical-only ConfluenceScorer gate

# ── Orchestration mode constants ───────────────────────────────────────────────
# Controls how candidate signals from different model families are selected when
# multiple models fire on the same bar/symbol in _run_unified_scenario().
#
# NAIVE            — original behavior: all models compete, highest-strength wins.
#                    Produces the SessionX37 full_system result (PF=1.01 zero fees).
#
# RESEARCH_PRIORITY — Session 42 fix: research-backed models (PBL/SLC) take priority
#                    over HMM models (Trend/MB) when both fire on the same bar/symbol.
#                    HMM models only fire when no research signal exists for that bar.
#                    Rationale: PBL/SLC have established edge via dedicated backtest
#                    (pbl_slc PF=1.37 zero fees). Crowding them out with MB/Trend
#                    (which have PF<1.0 in the unified pool) hurts combined performance.
#
# This parameter does NOT affect mode="pbl_slc" which always calls _run_scenario().
ORCHESTRATION_NAIVE             = "naive"             # original; all compete by strength
ORCHESTRATION_RESEARCH_PRIORITY = "research_priority" # Session 42: PBL/SLC beat Trend/MB

# ── HMM confidence gate constants ─────────────────────────────────────────────
# Controls the minimum HMM posterior probability required before TrendModel and
# MomentumBreakout are allowed to generate signals in _run_unified_scenario().
#
# The HMMRegimeClassifier.classify() always returns a confidence score (0–1)
# representing the posterior probability of the winning regime.  A low score
# (e.g. <0.60) means the HMM sees the bar as ambiguous — TrendModel/MB entries
# in that condition have historically produced PF<1.0 in the unified pool.
#
# HMM_CONFIDENCE_GATE_OFF (0.0) — no gating; original behavior (Naive/RP baseline).
# Any positive value (e.g. 0.60, 0.70, 0.80) blocks HMM-family signals when the
# HMM posterior for the chosen regime is below the threshold.
#
# This parameter does NOT affect mode="pbl_slc", which always calls _run_scenario().
# It does NOT affect PBL or SLC, which use the ResearchRegimeClassifier.
HMM_CONFIDENCE_GATE_OFF = 0.0   # no gating (default — preserves prior behavior)


def _fingerprint_parquet(path: Path) -> str:
    """SHA-256 of first 64 KB of a parquet file (fast, stable)."""
    if not path.exists():
        return "missing"
    with open(path, "rb") as f:
        data = f.read(65536)
    return hashlib.sha256(data).hexdigest()[:16]


def _indicator_cache_key(sym: str, tf: str, raw_fp: str) -> str:
    """12-char stable key encoding (symbol, tf, raw fingerprint, indicator version)."""
    raw = f"{sym}_{tf}_{raw_fp}_{INDICATOR_VERSION}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _indicator_cache_path(sym: str, tf: str, raw_fp: str) -> Path:
    """Path to the cached indicator parquet for a given symbol/tf/raw fingerprint."""
    slug = sym.replace("/", "_")
    key  = _indicator_cache_key(sym, tf, raw_fp)
    return CACHE_DIR / f"{slug}_{tf}_ind_{key}.parquet"


def _regime_cache_path(sym: str, tf: str, ind_key: str, kind: str) -> Path:
    """Path to a cached regime numpy array (.npy) keyed on the indicator fingerprint."""
    slug = sym.replace("/", "_")
    return CACHE_DIR / f"{slug}_{tf}_{kind}_{ind_key}.npy"


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

    # ── Mode constants ────────────────────────────────────────────────────────
    MODE_PBL_SLC     = "pbl_slc"      # reference implementation (exact parity)
    MODE_PBL         = "pbl"           # PullbackLong only
    MODE_SLC         = "slc"           # SwingLowContinuation only
    MODE_TREND       = "trend"         # TrendModel only (HMM regime)
    MODE_MOMENTUM    = "momentum"      # MomentumBreakout only (HMM regime)
    MODE_FULL_SYSTEM = "full_system"   # all strategies (research + HMM)
    MODE_CUSTOM      = "custom"        # strategy_subset list

    # Models that use the ResearchRegimeClassifier (vectorized)
    _RESEARCH_MODELS = frozenset({"pullback_long", "swing_low_continuation"})
    # Models that use HMMRegimeClassifier (bar-by-bar)
    _HMM_MODELS      = frozenset({"trend", "momentum_breakout"})

    def __init__(
        self,
        date_start:           Optional[str]        = None,
        date_end:             Optional[str]        = None,
        symbols:              Optional[list[str]]  = None,
        mode:                 str                  = "pbl_slc",
        strategy_subset:      Optional[list[str]]  = None,
        confluence_mode:      str                  = CONFLUENCE_NONE,
        orchestration_mode:   str                  = ORCHESTRATION_NAIVE,
        hmm_confidence_min:   float                = HMM_CONFIDENCE_GATE_OFF,
    ):
        """
        Parameters
        ----------
        mode : str
            "pbl_slc"     — canonical PBL+SLC path (exact parity guaranteed)
            "pbl"         — PullbackLong only
            "slc"         — SwingLowContinuation only
            "trend"       — TrendModel only (uses HMM regime)
            "momentum"    — MomentumBreakout only (uses HMM regime)
            "full_system" — all strategies via both regime classifiers
            "custom"      — strategy_subset list drives which models run
        strategy_subset : list[str] | None
            For mode="custom": explicit list of model_name strings.
            For all other modes: ignored (mode determines the subset).
        confluence_mode : str
            CONFLUENCE_NONE      ("none")           — highest-strength single-winner (default)
            CONFLUENCE_TECHNICAL ("technical_only") — technical-only ConfluenceScorer gate
            Only applies to _run_unified_scenario() (non pbl_slc modes).
            mode="pbl_slc" always calls _run_scenario() which is unaffected.
        orchestration_mode : str
            ORCHESTRATION_NAIVE             ("naive")             — all models compete by strength
            ORCHESTRATION_RESEARCH_PRIORITY ("research_priority") — PBL/SLC beat Trend/MB when both
                fire on the same bar/symbol (Session 42 fix for full_system underperformance).
            Only applies to _run_unified_scenario(). mode="pbl_slc" is never affected.
        hmm_confidence_min : float
            Session 43: minimum HMM posterior confidence for TrendModel / MomentumBreakout
            to generate signals.  When the HMMRegimeClassifier.classify() returns a
            confidence score below this value, HMM-family signals are suppressed for that
            bar/symbol.  0.0 (default) = no gating (HMM_CONFIDENCE_GATE_OFF).
            Typical thresholds: 0.60, 0.70, 0.80.
            Does NOT affect PBL/SLC (ResearchRegimeClassifier path) or mode="pbl_slc".
        """
        self.date_start           = pd.Timestamp(date_start, tz="UTC") if date_start else None
        self.date_end             = pd.Timestamp(date_end,   tz="UTC") if date_end   else None
        self.symbols              = symbols or SYMBOLS
        self.mode                 = mode
        self.strategy_subset      = strategy_subset
        self.confluence_mode      = confluence_mode
        self.orchestration_mode   = orchestration_mode
        self.hmm_confidence_min   = float(hmm_confidence_min)
        self._data_loaded    = False
        self._raw:  dict[str, dict[str, pd.DataFrame]] = {}
        self._ind:  dict[str, dict[str, pd.DataFrame]] = {}
        self._reg30: dict[str, np.ndarray] = {}
        self._reg1h: dict[str, np.ndarray] = {}
        self._master_ts: list = []
        self._fingerprints: dict[str, str] = {}
        # HMM classifiers: sym → HMMRegimeClassifier (populated only when _needs_hmm())
        self._hmm: dict[str, Any] = {}
        # Phase 3.1 Opt: pre-vectorized NexusTrader rule-based regime arrays
        # (eliminates per-bar RegimeClassifier._classify() calls in unified engine)
        self._nx_regime: dict[str, np.ndarray] = {}   # dtype=object  (regime label strings)
        self._nx_conf:   dict[str, np.ndarray] = {}   # dtype=float32 (confidence 0-1)
        # Session 45 Opt: indicator cache keys {sym → {tf → ind_key}} populated
        # in _compute_indicators(); used by _precompute_regimes/_precompute_nx_regimes
        # to build their own npy cache paths without re-hashing indicator data.
        self._ind_keys: dict[str, dict[str, str]] = {}
        # Session 45 Opt: pre-extracted high/low/open numpy arrays for O(1) access
        # in the SL/TP and pending-entry fill hot paths (replaces .iloc[loc]).
        self._highs: dict[str, np.ndarray] = {}
        self._lows:  dict[str, np.ndarray] = {}
        self._opens: dict[str, np.ndarray] = {}
        # Cache statistics for diagnostics / UI display
        self._cache_hits:   int = 0
        self._cache_misses: int = 0

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

        _cb("Loading parquet files…", 3)
        self._load_raw()

        # Session 45: indicator cache + multi-core computation (passes progress_cb
        # so per-symbol HIT/MISS updates are surfaced in the UI).
        _cb("Computing indicators (checking cache)…", 10)
        self._compute_indicators(progress_cb)

        # Pre-extract high/low/open numpy arrays for O(1) SL/TP access.
        _cb("Pre-extracting fast arrays…", 50)
        self._pre_extract_arrays()

        _cb("Pre-classifying regimes…", 55)
        self._precompute_regimes(progress_cb)

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

        # Fit HMM classifiers when mode requires bar-by-bar classification.
        # Phase 3.1 Opt: pre-vectorize NexusTrader rule-based regime FIRST so
        # _run_unified_scenario() can do O(1) array lookups instead of per-bar
        # RegimeClassifier._classify() calls (saves ~134s per simulation run).
        if self._needs_hmm():
            _cb("Pre-computing NexusTrader regimes…", 85)
            self._precompute_nx_regimes(progress_cb)
            _cb("Fitting HMM classifiers…", 92)
            self._fit_hmm(progress_cb)

        _cb("Data ready", 100)
        self._data_loaded = True
        logger.info(
            "BacktestRunner loaded: %d master bars, %d symbols, mode=%s",
            len(self._master_ts), len(self.symbols), self.mode,
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

        # ── Route to the appropriate scenario engine ───────────────────────
        # mode="pbl_slc" always calls the reference implementation unchanged
        # (guarantees n=1,731 / PF=1.3798 / PF(fees)=1.2682 parity).
        # All other modes use the unified engine that supports all strategies.
        if self.mode == self.MODE_PBL_SLC:
            result = self._run_scenario(cost_per_side, progress_cb)
        else:
            result = self._run_unified_scenario(cost_per_side, progress_cb)

        result["mode"]             = self.mode
        result["strategy_subset"]  = self.strategy_subset or self._get_active_models()
        result["params_applied"]   = _applied
        result["data_fingerprints"] = self._fingerprints
        result["cache_info"]        = self.cache_info()
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

    def cache_info(self) -> dict:
        """
        Return indicator cache statistics for display in the UI.

        Returns
        -------
        dict with keys:
          cache_hits      — number of TF datasets loaded from cache
          cache_misses    — number of TF datasets computed (cache miss)
          cache_dir       — absolute path to the cache directory (str)
          cached_files    — number of files currently in CACHE_DIR
          cache_size_mb   — total size of all cached files in MB
        """
        cached_files = list(CACHE_DIR.glob("*")) if CACHE_DIR.exists() else []
        size_bytes   = sum(f.stat().st_size for f in cached_files if f.is_file())
        return {
            "cache_hits":    self._cache_hits,
            "cache_misses":  self._cache_misses,
            "cache_dir":     str(CACHE_DIR),
            "cached_files":  len(cached_files),
            "cache_size_mb": round(size_bytes / 1024 / 1024, 1),
        }

    @staticmethod
    def clear_cache() -> int:
        """
        Delete all files in CACHE_DIR.  Returns number of files deleted.
        Useful when INDICATOR_VERSION is bumped or indicator_library changes.
        """
        if not CACHE_DIR.exists():
            return 0
        deleted = 0
        for f in CACHE_DIR.glob("*"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        logger.info("BacktestRunner.clear_cache: deleted %d cached files", deleted)
        return deleted

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

    def _compute_indicators(self, progress_cb=None) -> None:
        """
        Session 45 Optimisation — Persistent indicator cache + multi-core computation.

        Cache strategy
        --------------
        Each (symbol, timeframe) parquet is cached in CACHE_DIR with a filename that
        encodes a 12-char MD5 of (symbol, tf, raw-data SHA-256 fingerprint,
        INDICATOR_VERSION).  A stale raw parquet (new data appended) produces a new
        fingerprint → new cache path → automatic recomputation.  Bumping
        INDICATOR_VERSION also busts all cached files.

        On cache HIT:  loads from parquet   (~0.1–0.5s vs 3–10s for computation)
        On cache MISS: calls calculate_all / calculate_scan_mode, saves to parquet

        Multi-core
        ----------
        Three symbols are computed in parallel via ThreadPoolExecutor (GIL is
        released during numpy/pandas operations in the indicator library).
        Uses explicit pool + finally shutdown per project threading rules.
        """
        import concurrent.futures
        from core.features.indicator_library import calculate_all, calculate_scan_mode

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        def _compute_sym(sym: str) -> dict:
            """Compute (or load) all TF indicators for one symbol; returns result dict."""
            result: dict = {}
            slug = sym.replace("/", "_")
            keys: dict[str, str] = {}

            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                df_raw = self._raw[sym].get(tf, pd.DataFrame())
                if df_raw.empty:
                    result[tf] = pd.DataFrame()
                    keys[tf]   = "empty"
                    continue

                raw_fp    = self._fingerprints.get(f"{sym}/{tf}", "unknown")
                ind_key   = _indicator_cache_key(sym, tf, raw_fp)
                cache_p   = _indicator_cache_path(sym, tf, raw_fp)
                keys[tf]  = ind_key

                if cache_p.exists():
                    t0 = time.time()
                    try:
                        df_ind = pd.read_parquet(cache_p)
                        df_ind.index = pd.to_datetime(df_ind.index, utc=True)
                        elapsed = time.time() - t0
                        logger.info(
                            "Indicator cache HIT  → %s/%s  loaded in %.2fs  [%s]",
                            sym, tf, elapsed, cache_p.name,
                        )
                        result[tf] = df_ind
                        result[f"_hit_{tf}"] = True
                    except Exception as exc:
                        logger.warning("Cache load failed %s/%s: %s — recomputing", sym, tf, exc)
                        result[f"_hit_{tf}"] = False
                        # Fall through to compute below
                else:
                    result[f"_hit_{tf}"] = False

                if not result.get(f"_hit_{tf}"):
                    t0 = time.time()
                    try:
                        fn     = calculate_all if tf == PRIMARY_TF else calculate_scan_mode
                        df_ind = fn(df_raw.copy())
                    except Exception as exc:
                        logger.warning("Indicator fail %s/%s: %s", sym, tf, exc)
                        result[tf] = pd.DataFrame()
                        continue
                    elapsed = time.time() - t0
                    logger.info(
                        "Indicator cache MISS → %s/%s  computed in %.2fs  saving to %s",
                        sym, tf, elapsed, cache_p.name,
                    )
                    try:
                        df_ind.to_parquet(cache_p)
                    except Exception as exc:
                        logger.warning("Cache save failed %s/%s: %s", sym, tf, exc)
                    result[tf] = df_ind

            result["_keys"] = keys
            return result

        # ── Parallel per-symbol computation (max 3 workers = 3 symbols) ────────
        n_workers = min(len(self.symbols), 3)
        sym_results: dict[str, dict] = {}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
        try:
            futs = {pool.submit(_compute_sym, sym): sym for sym in self.symbols}
            done_count = 0
            for fut in concurrent.futures.as_completed(futs):
                sym = futs[fut]
                try:
                    sym_results[sym] = fut.result()
                except Exception as exc:
                    logger.error("Indicator computation failed for %s: %s", sym, exc)
                    sym_results[sym] = {
                        tf: pd.DataFrame() for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]
                    }
                done_count += 1
                if progress_cb:
                    sym_label = sym.replace("/USDT", "")
                    hit_30m   = sym_results[sym].get(f"_hit_{PRIMARY_TF}", False)
                    status    = "HIT" if hit_30m else "computed"
                    pct       = 10 + int(done_count / max(len(self.symbols), 1) * 38)
                    progress_cb(
                        f"Indicators {done_count}/{len(self.symbols)}: "
                        f"{sym_label} 30m cache {status}",
                        pct,
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # ── Populate self._ind and self._ind_keys ───────────────────────────────
        for sym in self.symbols:
            r = sym_results.get(sym, {})
            self._ind[sym] = {}
            self._ind_keys[sym] = {}
            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                self._ind[sym][tf] = r.get(tf, pd.DataFrame())
            self._ind_keys[sym] = r.get("_keys", {})

        # Tally global cache stats
        for sym in self.symbols:
            r = sym_results.get(sym, {})
            for tf in [PRIMARY_TF, HTF_4H_TF, SLC_1H_TF]:
                if r.get(f"_hit_{tf}"):
                    self._cache_hits += 1
                elif r.get(tf) is not None:
                    self._cache_misses += 1

        logger.info(
            "_compute_indicators: %d HIT / %d MISS (total %d TF datasets)",
            self._cache_hits, self._cache_misses,
            self._cache_hits + self._cache_misses,
        )

    def _pre_extract_arrays(self) -> None:
        """
        Session 45 Optimisation — Pre-extract high/low/open numpy float64 arrays.

        Replaces ~60 µs pandas .iloc[loc] calls in the SL/TP and pending-entry
        fill hot paths with ~50 ns numpy index operations.  Arrays are sized to
        match the indicator DataFrame index so loc-based access is always safe
        after the existing `loc < len(idx30[sym])` guard.
        """
        for sym in self.symbols:
            df = self._ind[sym].get(PRIMARY_TF)
            if df is not None and not df.empty:
                self._highs[sym] = df["high"].to_numpy(dtype=np.float64)
                self._lows[sym]  = df["low"].to_numpy(dtype=np.float64)
                self._opens[sym] = df["open"].to_numpy(dtype=np.float64)
            else:
                self._highs[sym] = np.array([], dtype=np.float64)
                self._lows[sym]  = np.array([], dtype=np.float64)
                self._opens[sym] = np.array([], dtype=np.float64)
        logger.info(
            "_pre_extract_arrays: extracted high/low/open arrays for %d symbols",
            len(self.symbols),
        )

    def _precompute_regimes(self, progress_cb=None) -> None:
        """
        Pre-classify research regimes for 30m and 1h series.
        Results cached as .npy files keyed on indicator fingerprint + kind.
        """
        from core.regime.research_regime_classifier import (
            classify_series as _classify,
            BULL_TREND, BEAR_TREND,
        )

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        def _load_or_classify(sym: str, tf: str, df: pd.DataFrame, kind: str) -> np.ndarray:
            if df.empty:
                return np.array([], dtype=np.int8)
            ind_key   = self._ind_keys.get(sym, {}).get(tf, "unknown")
            cache_p   = _regime_cache_path(sym, tf, ind_key, kind)
            if ind_key != "unknown" and cache_p.exists():
                try:
                    arr = np.load(cache_p)
                    logger.info(
                        "Regime cache HIT  → %s/%s [%s] loaded from %s",
                        sym, tf, kind, cache_p.name,
                    )
                    return arr
                except Exception as exc:
                    logger.warning("Regime cache load failed %s/%s %s: %s", sym, tf, kind, exc)
            t0  = time.time()
            arr = _classify(df)
            logger.info(
                "Regime cache MISS → %s/%s [%s] classified in %.2fs",
                sym, tf, kind, time.time() - t0,
            )
            try:
                np.save(cache_p, arr)
            except Exception as exc:
                logger.warning("Regime cache save failed %s/%s %s: %s", sym, tf, kind, exc)
            return arr

        for i, sym in enumerate(self.symbols):
            if progress_cb:
                progress_cb(
                    f"Research regimes {i + 1}/{len(self.symbols)}: {sym.replace('/USDT','')}",
                    55 + int((i + 1) / max(len(self.symbols), 1) * 20),
                )
            df30 = self._ind[sym].get(PRIMARY_TF, pd.DataFrame())
            self._reg30[sym] = _load_or_classify(sym, PRIMARY_TF, df30, "reg30")
            df1h = self._ind[sym].get(SLC_1H_TF, pd.DataFrame())
            self._reg1h[sym] = _load_or_classify(sym, SLC_1H_TF, df1h, "reg1h")

    def _precompute_nx_regimes(self, progress_cb=None) -> None:
        """
        Phase 3.1 Optimisation — Pre-vectorize NexusTrader rule-based regime
        classification over the FULL indicator series for each symbol.

        Problem eliminated
        ------------------
        _run_unified_scenario() called RegimeClassifier._classify(df_window) at
        every bar (99,653 times per simulation run = 134s / 54% of sim time).
        Each call re-computed pandas rolling statistics (BB width ratio, vol_trend,
        price_from_high) on a fresh 350-bar DataFrame slice — O(n) per bar = O(n²)
        total.

        Solution
        --------
        Compute all regime-classification features ONCE as vectorised pandas/numpy
        operations over the full series. Run the hysteresis loop on numpy scalars
        (no pandas overhead). Store results as:
          self._nx_regime[sym]  — np.ndarray(dtype=object)  regime label per bar
          self._nx_conf[sym]    — np.ndarray(dtype=float32) confidence per bar

        _run_unified_scenario() then replaces:
            hmm_regime, hmm_conf, hmm_probs = self._hmm[sym].classify(df_window)
        with:
            hmm_regime = self._nx_regime[sym][loc]
            hmm_conf   = float(self._nx_conf[sym][loc])

        Correctness
        -----------
        All features are computed identically to _classify():
          - bb_width_ratio uses rolling(20).mean() on the full series — same window
            as the original (which used .rolling(20).mean().iloc[-1] on the 350-bar
            slice; for loc ≥ 20 both methods produce bit-for-bit identical results).
          - ema_slope uses a 5-bar difference: ema[i] - ema[i-5], matching ema_slope_window=5.
          - vol_trend, price_from_high, ema_slope_current are all direct numpy equivalents.
          - Hysteresis state (regime_buffer, committed_regime) is maintained in the
            same sequential order as the original barwise calls.

        Only difference: the original hysteresis buffer was only advanced when the
        symbol was NOT in positions/pending_entries (gaps during open trades). The
        vectorized version advances through ALL bars. In practice this produces
        negligible differences because the committed regime persists across gaps.

        Expected gain: ~134s per zero-fee simulation run (54% → <1% of sim time).
        """
        from core.regime.regime_classifier import (
            REGIME_BULL_TREND, REGIME_BEAR_TREND, REGIME_RANGING,
            REGIME_VOL_EXPANSION, REGIME_VOL_COMPRESS,
            REGIME_ACCUMULATION, REGIME_DISTRIBUTION,
            REGIME_UNCERTAIN, REGIME_CRISIS, REGIME_RECOVERY,
            REGIME_LIQUIDATION_CASCADE, REGIME_SQUEEZE,
        )

        # Classifier thresholds — must match RegimeClassifier.__init__ defaults
        _ADX_TREND = 25.0
        _ADX_RANGE = 20.0
        _BB_EXP    = 1.5
        _BB_COMP   = 0.6
        _EMA_SL_W  = 5          # ema_slope_window
        _BB_ROLL   = 20         # bb_rolling_window
        _HYST      = 3          # _hysteresis_bars

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        for i_sym, sym in enumerate(self.symbols):
            if progress_cb:
                progress_cb(
                    f"NX regimes {i_sym + 1}/{len(self.symbols)}: {sym.replace('/USDT','')}",
                    85 + int((i_sym + 1) / max(len(self.symbols), 1) * 5),
                )

            df = self._ind[sym].get(PRIMARY_TF)
            if df is None or df.empty or len(df) < 30:
                self._nx_regime[sym] = np.array([], dtype=object)
                self._nx_conf[sym]   = np.array([], dtype=np.float32)
                continue

            # ── Cache check ──────────────────────────────────────────────────
            ind_key = self._ind_keys.get(sym, {}).get(PRIMARY_TF, "unknown")
            reg_p   = _regime_cache_path(sym, PRIMARY_TF, ind_key, "nx_regime")
            conf_p  = _regime_cache_path(sym, PRIMARY_TF, ind_key, "nx_conf")
            if ind_key != "unknown" and reg_p.exists() and conf_p.exists():
                try:
                    self._nx_regime[sym] = np.load(reg_p, allow_pickle=True)
                    self._nx_conf[sym]   = np.load(conf_p)
                    logger.info(
                        "NX regime cache HIT  → %s  loaded from %s", sym, reg_p.name
                    )
                    continue
                except Exception as exc:
                    logger.warning("NX regime cache load failed %s: %s — recomputing", sym, exc)

            n = len(df)

            # ── Vectorised feature extraction (one pass each) ─────────────────
            adx_s   = df["adx"].to_numpy(dtype=float, na_value=np.nan)   if "adx"    in df.columns else np.full(n, np.nan)
            ema20_s = df["ema_20"].to_numpy(dtype=float, na_value=np.nan) if "ema_20" in df.columns else np.full(n, np.nan)
            rsi_s   = df["rsi"].to_numpy(dtype=float, na_value=np.nan)   if "rsi"    in df.columns else np.full(n, np.nan)
            close_s = df["close"].to_numpy(dtype=float, na_value=np.nan) if "close"  in df.columns else np.full(n, np.nan)
            vol_s   = df["volume"].to_numpy(dtype=float, na_value=np.nan) if "volume" in df.columns else np.full(n, np.nan)

            # BB width ratio — THE bottleneck in the original: done ONCE here
            bb_width_ratio_s = np.full(n, np.nan)
            if all(c in df.columns for c in ("bb_upper", "bb_lower", "bb_mid")):
                bb_up  = df["bb_upper"].to_numpy(dtype=float, na_value=np.nan)
                bb_lo  = df["bb_lower"].to_numpy(dtype=float, na_value=np.nan)
                bb_mid = df["bb_mid"].to_numpy(dtype=float, na_value=np.nan)
                with np.errstate(divide="ignore", invalid="ignore"):
                    widths = np.where(bb_mid != 0, (bb_up - bb_lo) / bb_mid, np.nan)
                w_ser = pd.Series(widths)
                rolling_mean = w_ser.rolling(_BB_ROLL, min_periods=_BB_ROLL).mean().to_numpy()
                with np.errstate(divide="ignore", invalid="ignore"):
                    bb_width_ratio_s = np.where(
                        np.isfinite(rolling_mean) & (rolling_mean != 0),
                        widths / rolling_mean, np.nan,
                    )

            # EMA slope: 5-bar % change (matches ema_slope_window=5)
            ema_slope_s = np.full(n, np.nan)
            if not np.all(np.isnan(ema20_s)):
                prev = ema20_s[:-_EMA_SL_W]
                with np.errstate(divide="ignore", invalid="ignore"):
                    ema_slope_s[_EMA_SL_W:] = np.where(
                        prev != 0, (ema20_s[_EMA_SL_W:] - prev) / prev * 100.0, np.nan
                    )

            # EMA slope current: 1-bar % change
            ema_slope_cur_s = np.full(n, np.nan)
            if not np.all(np.isnan(ema20_s)):
                prev1 = ema20_s[:-1]
                with np.errstate(divide="ignore", invalid="ignore"):
                    ema_slope_cur_s[1:] = np.where(
                        prev1 != 0, (ema20_s[1:] - prev1) / prev1, np.nan
                    )

            # Volume trend: rolling-20 mean vs rolling-60 mean
            vol_trend_s = np.full(n, np.nan)
            if not np.all(np.isnan(vol_s)):
                v_ser = pd.Series(vol_s)
                vol_20 = v_ser.rolling(20, min_periods=20).mean().to_numpy()
                vol_60 = v_ser.rolling(60, min_periods=60).mean().to_numpy()
                with np.errstate(divide="ignore", invalid="ignore"):
                    vol_trend_s = np.where(
                        np.isfinite(vol_60) & (vol_60 > 0),
                        (vol_20 / vol_60 - 1.0) * 100.0, np.nan,
                    )

            # Price from 20-bar high
            price_from_high_s = np.full(n, np.nan)
            if not np.all(np.isnan(close_s)):
                c_ser = pd.Series(close_s)
                roll_max = c_ser.rolling(20, min_periods=20).max().to_numpy()
                with np.errstate(divide="ignore", invalid="ignore"):
                    price_from_high_s = np.where(
                        np.isfinite(roll_max) & (roll_max > 0),
                        (close_s / roll_max - 1.0) * 100.0, np.nan,
                    )

            # ── Hysteresis loop on numpy scalars (fast) ───────────────────────
            regimes = np.empty(n, dtype=object)
            confs   = np.zeros(n, dtype=np.float32)

            _buf: list   = []          # regime_buffer
            _com: str    = ""          # committed_regime (empty = no prior commitment)

            def _hyst(new_r: str, c: float) -> tuple:
                nonlocal _com
                _buf.append(new_r)
                if len(_buf) > _HYST:
                    _buf.pop(0)
                if len(_buf) == _HYST and all(r == _buf[0] for r in _buf):
                    _com = new_r
                    return new_r, c
                if not _com:
                    return new_r, round(c * 0.9, 3)
                return _com, c * 0.8

            for i in range(n):
                adx     = adx_s[i]
                bwr     = bb_width_ratio_s[i]
                rsi     = rsi_s[i]
                vol_t   = vol_trend_s[i]
                price_h = price_from_high_s[i]
                ema_sl  = ema_slope_s[i]
                ema_cur = ema_slope_cur_s[i]
                cl      = close_s[i]
                em20    = ema20_s[i]

                _nan = np.isnan  # local alias avoids repeated global lookup

                # Insufficient data warmup
                if i < 30:
                    r, c = _hyst(REGIME_UNCERTAIN, 0.0)
                    regimes[i] = r; confs[i] = c
                    continue

                # Priority 0 — Crisis / Liquidation / Squeeze
                if (not _nan(bwr) and bwr > 2.5 and
                        not _nan(rsi) and rsi < 22 and
                        not _nan(vol_t) and vol_t > 50):
                    c = min(1.0, (bwr - 2.5) * 0.5 + 0.7)
                    r, c = _hyst(REGIME_LIQUIDATION_CASCADE, c)
                    regimes[i] = r; confs[i] = round(c, 3); continue

                if (not _nan(rsi) and (rsi > 78 or rsi < 22) and
                        not _nan(bwr) and bwr < 0.5 and
                        not _nan(vol_t) and vol_t < -15):
                    c = min(1.0, abs(rsi - 50) / 50.0 * 0.8 + 0.4)
                    r, c = _hyst(REGIME_SQUEEZE, c)
                    regimes[i] = r; confs[i] = round(c, 3); continue

                if (not _nan(bwr) and bwr > 2.0 and
                        not _nan(rsi) and rsi < 28 and
                        not _nan(vol_t) and vol_t > 30):
                    c = min(1.0, (28.0 - rsi) / 28.0 * 0.5 + 0.5)
                    r, c = _hyst(REGIME_CRISIS, c)
                    regimes[i] = r; confs[i] = round(c, 3); continue

                # Recovery
                if (not _nan(rsi) and 38 <= rsi < 55 and
                        not _nan(vol_t) and vol_t > 5 and
                        not _nan(ema_cur) and ema_cur > 0 and
                        not _nan(adx) and adx < 25 and
                        not _nan(bwr) and 0.7 <= bwr <= 1.3 and
                        not _nan(cl) and not _nan(em20) and cl > em20):
                    c = 0.55 + min(0.25, (rsi - 38) / 50.0)
                    r, c = _hyst(REGIME_RECOVERY, c)
                    regimes[i] = r; confs[i] = round(c, 3); continue

                # Priority 1 — Volatility state
                if not _nan(bwr):
                    if bwr >= _BB_EXP:
                        c = min(1.0, (bwr - _BB_EXP) * 2.0 + 0.5)
                        r, c = _hyst(REGIME_VOL_EXPANSION, c)
                        regimes[i] = r; confs[i] = round(c, 3); continue
                    if bwr <= _BB_COMP:
                        c = min(1.0, (_BB_COMP - bwr) * 3.0 + 0.5)
                        r, c = _hyst(REGIME_VOL_COMPRESS, c)
                        regimes[i] = r; confs[i] = round(c, 3); continue

                # Priority 2 — Trend
                if not _nan(adx):
                    if adx >= _ADX_TREND:
                        if not _nan(ema_sl):
                            if ema_sl > 0:
                                c = min(1.0, (adx - _ADX_TREND) / 20.0 + 0.5)
                                r, c = _hyst(REGIME_BULL_TREND, c)
                            else:
                                c = min(1.0, (adx - _ADX_TREND) / 20.0 + 0.5)
                                r, c = _hyst(REGIME_BEAR_TREND, c)
                        else:
                            r, c = _hyst(REGIME_RANGING, 0.45)
                        regimes[i] = r; confs[i] = round(c, 3); continue

                    # Priority 3 — Accumulation / Distribution / Ranging
                    if adx < _ADX_RANGE:
                        if (not _nan(vol_t) and vol_t > 10.0 and
                                not _nan(rsi) and 30 <= rsi <= 55):
                            c = min(1.0, 0.50 + vol_t / 100.0)
                            r, c = _hyst(REGIME_ACCUMULATION, c)
                            regimes[i] = r; confs[i] = round(c, 3); continue
                        if (not _nan(vol_t) and vol_t < -10.0 and
                                not _nan(rsi) and rsi >= 60 and
                                not _nan(price_h) and price_h > -5.0):
                            c = min(1.0, 0.50 + abs(vol_t) / 100.0)
                            r, c = _hyst(REGIME_DISTRIBUTION, c)
                            regimes[i] = r; confs[i] = round(c, 3); continue
                        c = min(1.0, (_ADX_RANGE - adx) / _ADX_RANGE + 0.4)
                        r, c = _hyst(REGIME_RANGING, c)
                        regimes[i] = r; confs[i] = round(c, 3); continue
                    else:
                        # Dead zone: adx_range ≤ adx < adx_trend
                        dead_w = _ADX_TREND - _ADX_RANGE
                        c = min(1.0, 0.40 + (adx - _ADX_RANGE) / dead_w * 0.15)
                        r, c = _hyst(REGIME_RANGING, c)
                        regimes[i] = r; confs[i] = round(c, 3); continue

                # Fallback
                r, c = _hyst(REGIME_UNCERTAIN, 0.3)
                regimes[i] = r; confs[i] = round(c, 3)

            self._nx_regime[sym] = regimes
            self._nx_conf[sym]   = confs.astype(np.float32)
            logger.info(
                "NX regime cache MISS → %s  %d bars classified, saving to cache", sym, n
            )
            try:
                np.save(reg_p, regimes)
                np.save(conf_p, self._nx_conf[sym])
            except Exception as exc:
                logger.warning("NX regime cache save failed %s: %s", sym, exc)

    # ── Unified engine helpers ─────────────────────────────────────────────

    def _needs_hmm(self) -> bool:
        """True when any active model requires bar-by-bar HMM classification."""
        return bool(self._HMM_MODELS & set(self._get_active_models()))

    def _get_active_models(self) -> list[str]:
        """
        Return the list of model_name strings that should run for this mode.
        strategy_subset always overrides the mode-based default.
        """
        if self.strategy_subset is not None:
            return list(self.strategy_subset)
        return {
            self.MODE_PBL_SLC:     ["pullback_long", "swing_low_continuation"],
            self.MODE_PBL:         ["pullback_long"],
            self.MODE_SLC:         ["swing_low_continuation"],
            self.MODE_TREND:       ["trend"],
            self.MODE_MOMENTUM:    ["momentum_breakout"],
            self.MODE_FULL_SYSTEM: ["pullback_long", "swing_low_continuation",
                                    "trend", "momentum_breakout"],
        }.get(self.mode, ["pullback_long", "swing_low_continuation"])

    def _fit_hmm(self, progress_cb=None) -> None:
        """
        Fit HMMRegimeClassifier on full 30m history for each symbol.
        Falls back to rule-based classify() if hmmlearn is unavailable or fitting fails.
        Called automatically from load_data() when _needs_hmm() is True.
        """
        try:
            from core.regime.hmm_regime_classifier import HMMRegimeClassifier
        except ImportError:
            logger.warning("HMMRegimeClassifier not available — HMM modes will use rule-based fallback")
            return

        for i, sym in enumerate(self.symbols):
            df30 = self._ind[sym].get(PRIMARY_TF, pd.DataFrame())
            if df30.empty:
                logger.warning("No 30m data for %s — skipping HMM fit", sym)
                continue
            if progress_cb:
                progress_cb(
                    f"Fitting HMM for {sym.replace('/USDT','')}…",
                    88 + int(i / max(len(self.symbols), 1) * 8),
                )
            clf = HMMRegimeClassifier()
            try:
                ok = clf.fit(df30)
                self._hmm[sym] = clf  # always store — classify() falls back to rule-based if not fitted
                if ok:
                    logger.info("HMM fitted for %s (%d bars)", sym, len(df30))
                else:
                    logger.warning("HMM fit inconclusive for %s — rule-based fallback active", sym)
            except Exception as exc:
                logger.warning("HMM fit error %s: %s — rule-based fallback", sym, exc)
                self._hmm[sym] = clf  # still usable (rule-based path inside classify())

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
        # Disable RL inference during backtesting.
        # RL calls select_action() per bar which launches a CUDA GPU kernel for a
        # 1-sample batch (~50-100 µs kernel-launch overhead per call).  On a
        # 70k-bar × 3-symbol run this becomes ~630,000 tiny GPU dispatches that
        # saturate the GPU while producing near-zero useful compute.  RL is
        # shadow_only=True anyway so it never contributes to backtest trade
        # generation.  Setting _rl_model=None makes SignalGenerator skip the RL
        # path entirely for this backtest instance only (live production
        # SignalGenerator is unaffected).
        sig_gen._rl_model = None
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
        _last_prog_t = t_sim  # for time-based progress updates

        for bar_idx, ts in enumerate(self._master_ts):
            if bar_idx < WARMUP_BARS:
                continue

            # ── Time-based progress (every ~1 s instead of every 2000 bars) ──
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

            # ── Fill pending entries ──────────────────────────────────────
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                # Session 45: numpy array access replaces pandas .iloc[loc]
                ep_raw = float(self._opens[sym][loc])
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
                # Session 45: numpy array access replaces pandas .iloc[loc]
                hi = float(self._highs[sym][loc])
                lo = float(self._lows[sym][loc])
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

    # ─────────────────────────────────────────────────────────────────────────
    # Unified simulation engine — all strategies, single pipeline
    # ─────────────────────────────────────────────────────────────────────────

    def _run_unified_scenario(self, cost_per_side: float, progress_cb=None) -> dict:
        """
        Unified backtest simulation supporting ALL NexusTrader strategies.

        Architecture
        ------------
        Single loop, two parallel regime-classification paths:

          ResearchRegimeClassifier path  (vectorized, pre-computed)
            → PullbackLong (30m bull_trend)
            → SwingLowContinuation (1h bear_trend)

          HMMRegimeClassifier path  (bar-by-bar, fitted at load_data())
            → TrendModel (bull_trend / bear_trend)
            → MomentumBreakout (vol_expansion)

        Both paths run inside the SAME simulation loop using the SAME
        trade mechanics (pending_entries next-bar fill, SL/TP check,
        PositionSizer.calculate_pos_frac()) as _run_scenario().

        Signal selection: when multiple models fire on the same bar/symbol,
        the highest-strength signal is selected (deterministic, no adaptive
        ConfluenceScorer contamination in backtest).

        Called by run() for all modes except MODE_PBL_SLC.
        MODE_PBL_SLC always calls _run_scenario() (reference implementation).
        """
        from collections import defaultdict

        from core.signals.signal_generator import SignalGenerator
        from core.meta_decision.position_sizer import PositionSizer
        from core.regime.research_regime_classifier import (
            regime_to_string as research_regime_to_string,
            BULL_TREND as RES_BULL_TREND,
            BEAR_TREND as RES_BEAR_TREND,
        )

        active_models  = self._get_active_models()
        active_set     = set(active_models)
        use_research   = bool(self._RESEARCH_MODELS & active_set)
        use_hmm        = bool(self._HMM_MODELS & active_set)
        _use_rp        = (self.orchestration_mode == ORCHESTRATION_RESEARCH_PRIORITY)
        _conf_gate     = self.hmm_confidence_min  # 0.0 = no gating

        logger.info(
            "UnifiedEngine mode=%s active=%s research=%s hmm=%s "
            "confluence=%s orchestration=%s hmm_conf_min=%.2f",
            self.mode, active_models, use_research, use_hmm,
            self.confluence_mode, self.orchestration_mode, _conf_gate,
        )

        sig_gen = SignalGenerator()
        sig_gen._warmup_complete = True
        # Disable RL inference during backtesting (see comment in _run_scenario).
        sig_gen._rl_model = None
        sizer   = PositionSizer()

        # ── Technical-only ConfluenceScorer (optional gate) ───────────────────
        _use_conf = (self.confluence_mode == CONFLUENCE_TECHNICAL)
        _scorer: Optional[Any] = None
        if _use_conf:
            from core.meta_decision.confluence_scorer import ConfluenceScorer
            _scorer = ConfluenceScorer()
            logger.info("UnifiedEngine: confluence_mode=technical_only — ConfluenceScorer gate active")

        # ── Pre-build index structures for O(log n) timestamp lookups ────────
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

        equity           = INITIAL_CAPITAL
        positions:       dict[str, dict] = {}
        pending_entries: dict[str, dict] = {}
        all_trades:      list[dict]      = []
        equity_curve:    list[float]     = [INITIAL_CAPITAL]
        rejected_heat = rejected_max = rejected_entry_gap = n_signals_gen = 0
        rejected_confidence = 0  # Session 43: HMM bars blocked by confidence gate

        t_sim  = time.time()
        total  = len(self._master_ts)
        _last_prog_t = t_sim  # for time-based progress updates

        for bar_idx, ts in enumerate(self._master_ts):
            if bar_idx < WARMUP_BARS:
                continue

            # ── Time-based progress (every ~1 s instead of every 2000 bars) ──
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

            # ── Fill pending entries (identical to _run_scenario) ─────────────
            for sym, pend in list(pending_entries.items()):
                if sym in positions:
                    del pending_entries[sym]
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                # Session 45: numpy array access replaces pandas .iloc[loc]
                ep_raw = float(self._opens[sym][loc])
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

            # ── SL/TP check (identical to _run_scenario) ──────────────────────
            closed = []
            for sym, pos in list(positions.items()):
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                # Session 45: numpy array access replaces pandas .iloc[loc]
                hi = float(self._highs[sym][loc])
                lo = float(self._lows[sym][loc])
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

            # ── Signal generation ──────────────────────────────────────────────
            for sym in self.symbols:
                if sym in positions or sym in pending_entries:
                    continue
                loc = int(idx30[sym].searchsorted(ts))
                if loc >= len(idx30[sym]) or idx30[sym][loc] != ts:
                    continue
                if loc < WARMUP_BARS:
                    continue

                s30 = max(0, loc - MODEL_LOOKBACK + 1)
                df_window = self._ind[sym][PRIMARY_TF].iloc[s30 : loc + 1]
                if len(df_window) < 70:
                    continue

                candidate_signals: list = []

                # ── Research regime path (PBL / SLC) ──────────────────────────
                if use_research:
                    res30 = self._reg30.get(sym, np.array([]))
                    res_regime_30m = int(res30[loc]) if loc < len(res30) else 0
                    loc1h = int(idx1h[sym].searchsorted(ts, side="right")) - 1
                    res1h = self._reg1h.get(sym, np.array([]))
                    res_regime_1h  = int(res1h[loc1h]) if 0 <= loc1h < len(res1h) else 0

                    # PBL — fires in research bull_trend on 30m
                    if "pullback_long" in active_set and res_regime_30m == RES_BULL_TREND:
                        pbl_ctx: dict = {}
                        loc4h = int(idx4h[sym].searchsorted(ts, side="right"))
                        if loc4h >= HTF_LOOKBACK:
                            pbl_ctx["df_4h"] = self._ind[sym][HTF_4H_TF].iloc[
                                max(0, loc4h - HTF_LOOKBACK) : loc4h
                            ]
                        try:
                            raw = sig_gen.generate(
                                sym, df_window,
                                research_regime_to_string(res_regime_30m),
                                PRIMARY_TF, regime_probs={}, context=pbl_ctx,
                            ) or []
                            candidate_signals.extend(
                                s for s in raw if s.model_name == "pullback_long"
                            )
                        except Exception as exc:
                            logger.debug("UnifiedEngine PBL %s: %s", sym, exc)

                    # SLC — fires in research bear_trend on 1h
                    if ("swing_low_continuation" in active_set
                            and res_regime_1h == RES_BEAR_TREND
                            and loc1h >= 15):
                        slc_ctx = {
                            "df_1h": self._ind[sym][SLC_1H_TF].iloc[
                                max(0, loc1h - SLC_1H_LOOKBACK + 1) : loc1h + 1
                            ]
                        }
                        try:
                            raw = sig_gen.generate(
                                sym, df_window,
                                research_regime_to_string(res_regime_1h),
                                PRIMARY_TF, regime_probs={}, context=slc_ctx,
                            ) or []
                            candidate_signals.extend(
                                s for s in raw if s.model_name == "swing_low_continuation"
                            )
                        except Exception as exc:
                            logger.debug("UnifiedEngine SLC %s: %s", sym, exc)

                # ── HMM / NexusTrader regime path (Trend / Momentum) ──────────
                #
                # Phase 3.1 Optimisation: regime classification is now done via
                # O(1) array lookup into pre-computed self._nx_regime / _nx_conf
                # instead of calling RegimeClassifier._classify(df_window) per bar
                # (was 99,653 calls × 0.448ms = 134s per simulation).
                #
                # Session 43 — HMM Confidence Gate:
                # When hmm_confidence_min > 0, HMM-family signals (TrendModel,
                # MomentumBreakout) are suppressed unless the NexusTrader regime
                # confidence for the winning regime meets the threshold.
                _hmm_probs_this_sym: Optional[dict] = None
                if use_hmm:
                    try:
                        # Phase 3.1: O(1) lookup replaces O(n) classify(df_window)
                        _nx_reg = self._nx_regime.get(sym)
                        _nx_cf  = self._nx_conf.get(sym)
                        if _nx_reg is not None and loc < len(_nx_reg):
                            hmm_regime = str(_nx_reg[loc])
                            hmm_conf   = float(_nx_cf[loc])
                        else:
                            # Fallback: classify the window directly if precompute missing
                            if sym in self._hmm:
                                hmm_regime, hmm_conf, _ = self._hmm[sym].classify(df_window)
                            else:
                                hmm_regime, hmm_conf = "uncertain", 0.0
                        hmm_probs = {hmm_regime: hmm_conf}
                        _hmm_probs_this_sym = hmm_probs

                        # ── Confidence gate (Session 43) ────────────────────────
                        if _conf_gate > 0.0 and hmm_conf < _conf_gate:
                            rejected_confidence += 1
                            logger.debug(
                                "UnifiedEngine conf-gate %s: hmm_conf=%.3f < %.2f — HMM skipped",
                                sym, hmm_conf, _conf_gate,
                            )
                        # Skip signals in crisis/liquidation_cascade regimes
                        elif hmm_regime not in ("crisis", "liquidation_cascade"):
                            hmm_active = [
                                m for m in active_set if m in self._HMM_MODELS
                            ]
                            if hmm_active:
                                raw = sig_gen.generate(
                                    sym, df_window, hmm_regime, PRIMARY_TF,
                                    regime_probs=hmm_probs, context={},
                                ) or []
                                candidate_signals.extend(
                                    s for s in raw if s.model_name in hmm_active
                                )
                    except Exception as exc:
                        logger.debug("UnifiedEngine regime classify %s: %s", sym, exc)

                if not candidate_signals:
                    continue

                # ── Signal selection ───────────────────────────────────────────────
                #
                # Three selection policies, evaluated in order:
                #
                # 1. RESEARCH_PRIORITY (Session 42 orchestration fix):
                #    Research-backed models (PBL, SLC) beat HMM models (Trend, MB)
                #    when both fire on the same bar/symbol.  Within each family,
                #    highest-strength wins.  HMM models only fill in when no research
                #    signal exists for this bar/symbol.
                #    Rationale: PBL/SLC have established research edge (pbl_slc mode
                #    PF=1.37).  Naive strength competition crowds them out with MB/Trend
                #    signals that have PF<1.0 in the unified pool.
                #
                # 2. CONFLUENCE_TECHNICAL (Session 41):
                #    Technical-only ConfluenceScorer gate applied after family routing.
                #    Compatible with RESEARCH_PRIORITY — both can be active.
                #
                # 3. NAIVE (default):
                #    All models compete; highest-strength signal wins regardless of
                #    model family.  Original full_system behavior.
                #
                if _use_rp:
                    # Research-priority routing: partition by model family
                    _research_sigs = [
                        s for s in candidate_signals
                        if s.model_name in self._RESEARCH_MODELS
                    ]
                    _hmm_sigs = [
                        s for s in candidate_signals
                        if s.model_name in self._HMM_MODELS
                    ]
                    if _research_sigs:
                        # Research family wins — pick highest-strength research signal
                        _research_sigs.sort(key=lambda s: s.strength, reverse=True)
                        candidate_signals = _research_sigs  # narrow pool for scorer
                    elif _hmm_sigs:
                        # HMM family fires when research is silent
                        _hmm_sigs.sort(key=lambda s: s.strength, reverse=True)
                        candidate_signals = _hmm_sigs
                    else:
                        continue  # should never reach here given outer guard

                if _use_conf and _scorer is not None:
                    # Technical-only ConfluenceScorer gate: uses deterministic scoring
                    # (model weights, regime affinity, direction dominance, correlation
                    # dampening, dynamic threshold).  Excluded: Orchestrator, L1/L2,
                    # OI/Liq modifiers, paper_executor capital.
                    scored = _scorer.score(
                        signals=candidate_signals,
                        symbol=sym,
                        regime_probs=_hmm_probs_this_sym,
                        technical_only=True,
                        capital_usdt_override=equity,
                    )
                    if scored is None:
                        continue   # confluence gate rejected
                    # Extract the primary model signal that aligns with scored direction
                    target_dir = "long" if scored.side == "buy" else "short"
                    aligned = [s for s in candidate_signals if s.direction == target_dir]
                    if not aligned:
                        continue
                    aligned.sort(key=lambda s: s.strength, reverse=True)
                    sig = aligned[0]
                else:
                    # Default: highest-strength single-winner (deterministic, no scorer)
                    candidate_signals.sort(key=lambda s: s.strength, reverse=True)
                    sig = candidate_signals[0]
                n_signals_gen += 1

                open_count = len(positions)
                if open_count >= MAX_POSITIONS:
                    rejected_max += 1
                    continue

                open_by_sym: dict[str, int] = defaultdict(int)
                for ps in positions:
                    open_by_sym[ps] += 1

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

        # ── Force-close remaining open positions ──────────────────────────────
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

        # ── KPIs ──────────────────────────────────────────────────────────────
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

        elapsed = time.time() - t_sim
        logger.info(
            "UnifiedEngine done [%s/%s/conf≥%.2f]: %d trades in %.1fs  "
            "PF=%.4f  WR=%.1f%%  CAGR=%.1f%%  rej_conf=%d",
            self.mode, self.orchestration_mode, _conf_gate,
            n_trades, elapsed, pf, wr * 100, cagr * 100, rejected_confidence,
        )

        # ── Per-model breakdown (dynamic — covers all active models) ──────────
        _MODEL_KEY = {
            "pullback_long":          "pbl",
            "swing_low_continuation": "slc",
            "trend":                  "trend",
            "momentum_breakout":      "mb",
        }
        model_stats: dict = {}
        for model_name in active_models:
            m_trades  = [t for t in all_trades if t["model"] == model_name]
            m_winners = [t for t in m_trades if t["pnl"] > 0]
            m_gp = sum(t["pnl"] for t in m_winners)
            m_gl = abs(sum(t["pnl"] for t in m_trades if t["pnl"] <= 0))
            key  = _MODEL_KEY.get(model_name, model_name)
            model_stats[f"{key}_n"]  = len(m_trades)
            model_stats[f"{key}_pf"] = round(m_gp / m_gl, 4) if m_gl > 0 else 999.0
            model_stats[f"{key}_wr"] = round(len(m_winners) / len(m_trades), 4) if m_trades else 0.0

        result = {
            "n_trades":            n_trades,
            "profit_factor":       round(pf, 4),
            "win_rate":            round(wr, 4),
            "cagr":                round(cagr, 4),
            "max_drawdown":        round(mdd, 4),
            "final_equity":        round(equity, 2),
            "years":               round(years, 2),
            "cost_per_side":       cost_per_side,
            "signals_generated":   n_signals_gen,
            "rejected_heat":       rejected_heat,
            "rejected_max":        rejected_max,
            "rejected_entry_gap":  rejected_entry_gap,
            "rejected_confidence": rejected_confidence,   # Session 43: HMM conf gate
            "hmm_confidence_min":  _conf_gate,            # Session 43: gate threshold used
            "elapsed_s":           round(elapsed, 1),
            "all_trades":          all_trades,
            # backward-compat keys (populated from model_stats where available)
            "pbl_n":   model_stats.get("pbl_n",   0),
            "pbl_pf":  model_stats.get("pbl_pf",  999.0),
            "pbl_wr":  model_stats.get("pbl_wr",  0.0),
            "slc_n":   model_stats.get("slc_n",   0),
            "slc_pf":  model_stats.get("slc_pf",  999.0),
            "slc_wr":  model_stats.get("slc_wr",  0.0),
        }
        result.update(model_stats)
        return result
