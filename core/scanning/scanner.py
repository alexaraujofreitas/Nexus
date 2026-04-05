# ============================================================
# NEXUS TRADER — Asset Scanner  (v2 — Parallel Pipeline)
#
# Orchestrates the full IDSS scan cycle:
#   1. Get active symbols from WatchlistManager
#   2. Apply UniverseFilter (liquidity, spread, ATR)
#   3. PHASE 1 (I/O): Concurrent OHLCV fetch for ALL symbols
#      and ALL timeframes (primary + 4h/1h context + MTF)
#   4. PHASE 2 (COMPUTE): Parallel per-symbol pipeline:
#      a. Calculate indicators
#      b. Classify regime (ensemble + HMM)
#      c. Run SignalGenerator
#      d. Score with ConfluenceScorer
#   5. PHASE 3 (FINALIZE): Risk gate + atomic result publish
#
# Architecture (v3 — sub-second optimised):
#   - OHLCV primary TFs fetched concurrently (20 workers)
#   - Context TFs (4h, 1h) served from TTL cache when not stale
#   - Per-symbol classifiers PERSISTED across cycles (no HMM refit)
#   - Per-symbol MS-GARCH instances (no singleton lock contention)
#   - Per-symbol compute in parallel (20 workers, one per symbol)
#   - Context DataFrames cached (skip calculate_scan_mode on hit)
#   - Candle-close buffer reduced from 2s to 0.5s
#   - Results collected atomically before emission
#
# Performance target: ≤1s for 20 symbols end-to-end (v3 optimised)
# ============================================================
from __future__ import annotations

import concurrent.futures
import logging
import time
import time as _time_mod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
from PySide6.QtCore import QObject, QThread, Signal, QTimer, Slot

from core.scanning.watchlist       import WatchlistManager
from core.scanning.universe_filter import UniverseFilter
from core.regime.regime_classifier  import RegimeClassifier
from core.signals.signal_generator  import SignalGenerator
from core.meta_decision.confluence_scorer import ConfluenceScorer
from core.meta_decision.order_candidate   import OrderCandidate
from core.risk.risk_gate           import RiskGate
from core.features.indicator_library import calculate_all, calculate_scan_mode
from core.scanning.closed_candle_guard import enforce_closed_candles
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)


# ── TTL-based OHLCV + DataFrame cache ───────────────────────────
# Persisted on AssetScanner across scan cycles. Context TFs (4h, 1h)
# change infrequently, so we avoid re-fetching until the TTL expires.
class _OHLCVCache:
    """TTL-based cache for OHLCV data and computed DataFrames."""
    __slots__ = ("_store", "_df_store")

    def __init__(self) -> None:
        self._store: dict[str, tuple[list, float]] = {}       # raw OHLCV
        self._df_store: dict[str, tuple[pd.DataFrame, float]] = {}  # computed DFs

    # ── Raw OHLCV ──
    def get(self, key: str) -> Optional[list]:
        entry = self._store.get(key)
        if entry is None:
            return None
        data, expiry = entry
        if _time_mod.time() > expiry:
            del self._store[key]
            return None
        return data

    def put(self, key: str, data: list, ttl_s: float) -> None:
        self._store[key] = (data, _time_mod.time() + ttl_s)

    # ── Computed DataFrames ──
    def get_df(self, key: str) -> Optional[pd.DataFrame]:
        entry = self._df_store.get(key)
        if entry is None:
            return None
        df, expiry = entry
        if _time_mod.time() > expiry:
            del self._df_store[key]
            return None
        return df

    def put_df(self, key: str, df: pd.DataFrame, ttl_s: float) -> None:
        self._df_store[key] = (df, _time_mod.time() + ttl_s)

    def clear_expired(self) -> None:
        now = _time_mod.time()
        for store in (self._store, self._df_store):
            expired = [k for k, v in store.items() if now > v[1]]
            for k in expired:
                del store[k]


# Context TF cache TTLs — aligned to candle durations
_CTX_TTL = {"4h": 4 * 3600, "1h": 3600}


# ── Scan Cycle Metrics ────────────────────────────────────────
@dataclass
class ScanCycleMetrics:
    """Professional-grade timing instrumentation for scan cycles."""
    cycle_start: float = 0.0
    ticker_fetch_ms: float = 0.0
    universe_filter_ms: float = 0.0
    ohlcv_prefetch_ms: float = 0.0
    compute_phase_ms: float = 0.0
    risk_gate_ms: float = 0.0
    post_scan_ms: float = 0.0
    total_cycle_ms: float = 0.0
    # Per-symbol timing
    per_symbol_ms: dict = field(default_factory=dict)
    # Concurrency info
    symbols_total: int = 0
    symbols_qualifying: int = 0
    symbols_fetched_ok: int = 0
    symbols_computed: int = 0
    symbols_failed: list = field(default_factory=list)
    fetch_concurrency: int = 0
    compute_concurrency: int = 0
    # Slowest / average
    slowest_symbol: str = ""
    slowest_symbol_ms: float = 0.0
    avg_symbol_ms: float = 0.0
    # Context fetch counts
    context_fetches_total: int = 0
    context_fetches_ok: int = 0
    retries: int = 0
    timeouts: int = 0

    # ── Sub-phase aggregates (populated from per-symbol _sym_diag) ──
    indicator_ms: float = 0.0       # Total indicator computation time
    regime_ms: float = 0.0          # Total regime classification time
    signal_ms: float = 0.0          # Total signal model evaluation time
    confluence_ms: float = 0.0      # Total confluence scoring time

    def to_dict(self) -> dict:
        """Serialise metrics for API responses."""
        return {
            "total_cycle_ms": round(self.total_cycle_ms, 1),
            "ohlcv_fetch_ms": round(self.ohlcv_prefetch_ms, 1),
            "indicator_ms": round(self.indicator_ms, 1),
            "regime_ms": round(self.regime_ms, 1),
            "signal_ms": round(self.signal_ms, 1),
            "confluence_ms": round(self.confluence_ms, 1),
            "ticker_fetch_ms": round(self.ticker_fetch_ms, 1),
            "universe_filter_ms": round(self.universe_filter_ms, 1),
            "compute_phase_ms": round(self.compute_phase_ms, 1),
            "risk_gate_ms": round(self.risk_gate_ms, 1),
            "post_scan_ms": round(self.post_scan_ms, 1),
            "symbols_total": self.symbols_total,
            "symbols_qualifying": self.symbols_qualifying,
            "symbols_fetched_ok": self.symbols_fetched_ok,
            "symbols_computed": self.symbols_computed,
            "symbols_failed": self.symbols_failed,
            "fetch_concurrency": self.fetch_concurrency,
            "compute_concurrency": self.compute_concurrency,
            "slowest_symbol": self.slowest_symbol,
            "slowest_symbol_ms": round(self.slowest_symbol_ms, 1),
            "avg_symbol_ms": round(self.avg_symbol_ms, 1),
            "per_symbol_ms": {k: round(v, 1) for k, v in self.per_symbol_ms.items()},
            "context_fetches_total": self.context_fetches_total,
            "context_fetches_ok": self.context_fetches_ok,
            "retries": self.retries,
            "timeouts": self.timeouts,
        }

    def log_summary(self):
        """Emit a structured performance summary to logs."""
        logger.info(
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  SCAN CYCLE PERFORMANCE REPORT                         ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            "║  Total cycle       : %7.0f ms                         ║\n"
            "║  ├─ Ticker fetch   : %7.0f ms                         ║\n"
            "║  ├─ Universe filter: %7.0f ms                         ║\n"
            "║  ├─ OHLCV prefetch : %7.0f ms  (%d workers)           ║\n"
            "║  ├─ Compute phase  : %7.0f ms  (%d workers)           ║\n"
            "║  ├─ Risk gate      : %7.0f ms                         ║\n"
            "║  └─ Post-scan      : %7.0f ms                         ║\n"
            "║                                                        ║\n"
            "║  Symbols: %d total → %d qualifying → %d fetched → %d computed ║\n"
            "║  Slowest : %-12s (%7.0f ms)                    ║\n"
            "║  Average : %7.0f ms/symbol                            ║\n"
            "║  Failed  : %s                                         ║\n"
            "║  Context fetches: %d/%d OK | Timeouts: %d | Retries: %d║\n"
            "╚══════════════════════════════════════════════════════════╝",
            self.total_cycle_ms,
            self.ticker_fetch_ms,
            self.universe_filter_ms,
            self.ohlcv_prefetch_ms, self.fetch_concurrency,
            self.compute_phase_ms, self.compute_concurrency,
            self.risk_gate_ms,
            self.post_scan_ms,
            self.symbols_total, self.symbols_qualifying,
            self.symbols_fetched_ok, self.symbols_computed,
            self.slowest_symbol or "N/A", self.slowest_symbol_ms,
            self.avg_symbol_ms,
            self.symbols_failed or "none",
            self.context_fetches_ok, self.context_fetches_total,
            self.timeouts, self.retries,
        )

# Timeframe → approximate poll interval in seconds
TF_POLL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "1d": 86400,
}

# Default buffer (seconds) added after candle close before scanning.
# Bybit REST finalizes bars within ~200ms; 0.5s is more than sufficient.
_CANDLE_CLOSE_BUFFER_S: float = 0.5


def _seconds_to_next_candle(timeframe: str, buffer_s: float = _CANDLE_CLOSE_BUFFER_S) -> int:
    """
    Return the number of seconds until the next candle of *timeframe* closes,
    plus *buffer_s* seconds so the exchange has time to finalize the bar.

    For example, if timeframe="1h" and the current UTC time is 13:22:47:
      - Next 1h candle closes at 14:00:00 UTC
      - Scan should fire at 14:00:30 UTC  (+ buffer_s)
      - Seconds to wait = (14*3600 + 30) - (13*3600 + 22*60 + 47)
                        = 50400 + 30 - 48167  = 2263s  (~37.7 min)

    This guarantees the scanner always fires on a candle-close boundary
    regardless of when NexusTrader was started.
    """
    import calendar

    interval_s = TF_POLL_SECONDS.get(timeframe, 3600)
    now_utc = datetime.utcnow()
    epoch_s = calendar.timegm(now_utc.timetuple()) + now_utc.microsecond / 1e6

    # Seconds elapsed since the last candle boundary
    elapsed_in_period = epoch_s % interval_s
    # Seconds remaining until the next candle close
    until_close = interval_s - elapsed_in_period
    # Add buffer (so we scan the closed bar, not the still-open one)
    delay = until_close + buffer_s
    # If we're extremely close to the boundary (within buffer), skip to the
    # next period so we never scan with a 0-second or negative delay.
    if delay <= buffer_s:
        delay += interval_s
    return int(delay)


class ScanWorker(QThread):
    """
    Runs one full scan cycle in a background thread.
    Emits results when done.
    """
    scan_complete    = Signal(list)  # list of OrderCandidate dicts (approved only)
    scan_error       = Signal(str)
    symbol_scanned   = Signal(str, str, float)  # symbol, regime, score (0 if no signal)
    df_cache_updated = Signal(object)            # dict[symbol, DataFrame] — for ATR filter next cycle
    # Full per-symbol scan results — approved + rejected + no-signal + filtered.
    # Each element is a dict with keys: symbol, regime, side, score, models_fired,
    # entry_price, stop_loss_price, take_profit_price, risk_reward_ratio,
    # position_size_usdt, generated_at, status, is_approved.
    scan_all_results = Signal(list)
    # Phase timing metrics dict — emitted after every scan cycle for UI display.
    scan_metrics_updated = Signal(object)

    def __init__(
        self,
        symbols:     list[str],
        timeframe:   str,
        exchange,                   # ccxt exchange instance
        open_positions: list[dict],
        capital_usdt:   float,
        drawdown_pct:   float,
        hmm_models: Optional[dict] = None,  # per-symbol HMM dict persisted across cycles
        prev_df_cache: Optional[dict] = None,  # indicator DFs from previous scan cycle (for ATR filter)
        sig_gen: Optional[object] = None,   # shared SignalGenerator from AssetScanner (preserves RL state)
        ensemble_classifiers: Optional[dict] = None,  # v3: persisted per-symbol EnsembleRegimeClassifier
        transition_controllers: Optional[dict] = None,  # v3: persisted per-symbol RegimeTransitionController
        garch_models: Optional[dict] = None,  # v3: per-symbol MSGARCHForecaster (eliminates singleton lock)
        ohlcv_cache: Optional[_OHLCVCache] = None,  # v3: TTL cache for context TFs
        parent=None,
    ):
        super().__init__(parent)
        self._symbols        = symbols
        self._timeframe      = timeframe
        self._exchange       = exchange
        self._open_positions = open_positions
        self._capital_usdt   = capital_usdt
        self._drawdown_pct   = drawdown_pct

        # Per-symbol HMM models: keyed by symbol string, persisted between scan cycles
        # via AssetScanner._hmm_models. Training one model per symbol eliminates
        # the statistical bias of reusing a single HMM instance across all assets.
        self._hmm_models: dict = hmm_models if hmm_models is not None else {}
        self._use_hmm = True  # always attempt; falls back gracefully per symbol

        # Indicator DataFrames from the previous scan cycle, used to enable the
        # ATR range filter in UniverseFilter before this cycle's OHLCV is fetched.
        self._prev_df_cache: dict = prev_df_cache if prev_df_cache is not None else {}

        # v3: Persist per-symbol EnsembleRegimeClassifier across cycles.
        # This is the single biggest optimisation — eliminates HMM retraining
        # every cycle (~30-100ms per symbol × 20 = 600-2000ms saved).
        self._ensemble_classifiers: dict = ensemble_classifiers if ensemble_classifiers is not None else {}

        # v3: Persist per-symbol RegimeTransitionController for effective hysteresis.
        self._transition_controllers: dict = transition_controllers if transition_controllers is not None else {}

        # v3: Per-symbol MS-GARCH instances (eliminates singleton lock serialisation).
        self._garch_models: dict = garch_models if garch_models is not None else {}

        # v3: TTL-based OHLCV cache for context timeframes.
        self._ohlcv_cache: Optional[_OHLCVCache] = ohlcv_cache

        try:
            from core.regime.ensemble_regime_classifier import EnsembleRegimeClassifier
            self._regime_clf = EnsembleRegimeClassifier()
            self._use_ensemble = True
        except ImportError:
            self._regime_clf = RegimeClassifier()
            self._use_ensemble = False

        # Regime transition controller for hysteresis/dwell
        try:
            from core.regime.regime_transition_controller import RegimeTransitionController
            self._transition_ctrl = RegimeTransitionController()
        except ImportError:
            self._transition_ctrl = None

        # Reuse a shared SignalGenerator (containing RLEnsemble) when provided by
        # AssetScanner so that RL models accumulate experience across scan cycles.
        # If none is supplied (e.g. standalone tests), create a fresh instance.
        if sig_gen is not None:
            self._sig_gen = sig_gen
        else:
            self._sig_gen = SignalGenerator()
            # The scanner fetches bars before calling generate(), so the live-feed
            # warmup guard is irrelevant here.  Skip it.
            self._sig_gen._warmup_complete = True
            self._sig_gen._warmup_bars_remaining = 0
        self._univ_filter = UniverseFilter()
        # Build ConfluenceScorer and RiskGate from live settings so that
        # parameter changes made in the Settings page take effect on the
        # very next scan cycle (no restart needed).
        from config.settings import settings as _s
        self._scorer = ConfluenceScorer(
            threshold=float(_s.get("idss.min_confluence_score", 0.55)),
        )
        self._risk_gate = RiskGate(
            max_concurrent_positions  = int(_s.get("risk.max_concurrent_positions", 3)),
            max_portfolio_drawdown_pct= float(_s.get("risk.max_portfolio_drawdown_pct", 15.0)),
            max_spread_pct            = float(_s.get("risk.max_spread_pct", 0.3)),
            min_risk_reward           = float(_s.get("risk.min_risk_reward", 1.3)),
        )

    @staticmethod
    def _empty_sym_result(symbol: str, status: str, regime: str = "") -> dict:
        """Build a baseline per-symbol result dict for symbols that never produced a candidate."""
        return {
            "symbol":               symbol,
            "regime":               regime,
            "side":                 "",
            "score":                0.0,
            "models_fired":         [],
            "entry_price":          None,
            "stop_loss_price":      0.0,
            "take_profit_price":    0.0,
            "risk_reward_ratio":    0.0,
            "position_size_usdt":   0.0,
            "generated_at":         "",
            "status":               status,
            "is_approved":          False,
        }

    def run(self):
        """
        Execute a full scan cycle with parallel I/O and parallel compute.

        Architecture (v2 — Parallel Pipeline):
          PHASE 1 (I/O-bound):  Concurrent OHLCV fetch for ALL symbols × ALL timeframes
          PHASE 2 (CPU-bound):  Parallel per-symbol compute (indicators, regime, signals, scoring)
          PHASE 3 (Sequential): Risk gate, atomic result emission, post-scan housekeeping

        This replaces the old sequential-per-symbol design that took ~3s/symbol × 20 = 60s.
        Target: ≤10s for 20 symbols.
        """
        metrics = ScanCycleMetrics()
        metrics.cycle_start = time.time()
        metrics.symbols_total = len(self._symbols)

        try:
            all_candidates: list[OrderCandidate] = []
            _regime_votes: dict[str, int] = {}
            _regime_confs: dict[str, float] = {}
            _all_sym_results: dict[str, dict] = {}

            # ══════════════════════════════════════════════════════════
            # PHASE 0: Ticker fetch + Universe filter
            # ══════════════════════════════════════════════════════════
            _t0 = time.time()
            tickers = {}
            _tp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = _tp.submit(self._exchange.fetch_tickers, self._symbols)
                tickers = _fut.result(timeout=15.0) or {}
            except concurrent.futures.TimeoutError:
                logger.warning("Scanner: fetch_tickers timed out after 15s — continuing with empty tickers")
                metrics.timeouts += 1
            except Exception as exc:
                logger.warning("Scanner: ticker fetch failed: %s", exc)
            finally:
                _tp.shutdown(wait=False, cancel_futures=True)
            metrics.ticker_fetch_ms = (time.time() - _t0) * 1000

            _t0 = time.time()
            qualifying = self._univ_filter.apply(
                self._symbols, tickers, feature_dfs=self._prev_df_cache or None
            )
            for _sym in self._symbols:
                if _sym not in qualifying:
                    _all_sym_results[_sym] = self._empty_sym_result(_sym, "Filtered")
            metrics.universe_filter_ms = (time.time() - _t0) * 1000
            metrics.symbols_qualifying = len(qualifying)

            if not qualifying:
                self.scan_complete.emit([])
                self.scan_all_results.emit(list(_all_sym_results.values()))
                metrics.total_cycle_ms = (time.time() - metrics.cycle_start) * 1000
                metrics.log_summary()
                return

            # ══════════════════════════════════════════════════════════
            # PHASE 1: Concurrent OHLCV prefetch — ALL timeframes
            #
            # Batch primary TF + context TFs (4h, 1h) + MTF confirmation
            # into a SINGLE concurrent fetch phase. This eliminates the
            # per-symbol sequential REST calls that were the #1 bottleneck.
            # ══════════════════════════════════════════════════════════
            _t0 = time.time()
            from config.settings import settings as _sc
            _ohlcv_limit = int(_sc.get("scanner.ohlcv_bars", 300))
            _pbl_slc_enabled = bool(_sc.get("mr_pbl_slc.enabled", False))
            _mtf_enabled = bool(_sc.get("multi_tf.confirmation_required", False))
            _slc_bars = int(_sc.get("mr_pbl_slc.slc_1h_bars", 150))

            # Determine the higher timeframe for MTF confirmation
            _tf_map = {"1m": "5m", "3m": "15m", "5m": "15m", "15m": "1h",
                       "30m": "4h", "1h": "4h", "2h": "4h", "4h": "1d",
                       "6h": "1d", "12h": "1d", "1d": "1w"}
            _higher_tf = _tf_map.get(self._timeframe)

            # Build fetch manifest: (symbol, timeframe, limit, cache_key)
            # v3 optimisations:
            #   1. De-duplicate 4h (ctx_4h + MTF both need 4h → one fetch)
            #   2. TTL cache hit for context TFs → skip REST call entirely
            _fetch_manifest: list[tuple[str, str, int, str]] = []
            _cache_hits = 0
            for sym in qualifying:
                # Primary OHLCV — always fetch fresh
                _fetch_manifest.append((sym, self._timeframe, _ohlcv_limit, f"{sym}|primary"))
                # Context TFs for PBL/SLC — check TTL cache first
                _ctx_4h_added = False
                if _pbl_slc_enabled:
                    _cached_4h = self._ohlcv_cache.get(f"{sym}|ctx_4h") if self._ohlcv_cache else None
                    if _cached_4h is not None:
                        _ohlcv_cache[f"{sym}|ctx_4h"] = _cached_4h  # pre-populate cycle cache
                        _cache_hits += 1
                    else:
                        _fetch_manifest.append((sym, "4h", 60, f"{sym}|ctx_4h"))
                    _ctx_4h_added = True

                    _cached_1h = self._ohlcv_cache.get(f"{sym}|ctx_1h") if self._ohlcv_cache else None
                    if _cached_1h is not None:
                        _ohlcv_cache[f"{sym}|ctx_1h"] = _cached_1h
                        _cache_hits += 1
                    else:
                        _fetch_manifest.append((sym, "1h", _slc_bars, f"{sym}|ctx_1h"))
                # MTF confirmation — skip if already fetching same TF via ctx
                if _mtf_enabled and _higher_tf:
                    if _higher_tf == "4h" and _ctx_4h_added:
                        pass  # ctx_4h already fetches 4h with 60 bars (≥50 needed)
                    else:
                        _fetch_manifest.append((sym, _higher_tf, 50, f"{sym}|mtf"))
            if _cache_hits:
                logger.info("Scanner: OHLCV cache hits: %d context TFs served from TTL cache", _cache_hits)

            metrics.context_fetches_total = len(_fetch_manifest) - len(qualifying)

            def _fetch_one_batched(sym: str, tf: str, limit: int, cache_key: str) -> tuple[str, str, list]:
                """Fetch OHLCV for one (symbol, timeframe) pair with timeout."""
                _t_start = time.time()
                _inner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    _f = _inner.submit(self._exchange.fetch_ohlcv, sym, tf, limit=limit)
                    try:
                        raw = _f.result(timeout=15.0)
                    except concurrent.futures.TimeoutError:
                        logger.warning("Scanner: prefetch TIMED OUT %s/%s after 15s", sym, tf)
                        return cache_key, tf, []
                    if raw and len(raw) >= 2:
                        raw, _dropped = enforce_closed_candles(raw, tf, log_symbol=f"{sym}/{tf}")
                    _elapsed = (time.time() - _t_start) * 1000
                    logger.debug("Scanner: fetch %s/%s OK — %d bars, %.0fms", sym, tf, len(raw) if raw else 0, _elapsed)
                    return cache_key, tf, raw or []
                except Exception as _exc:
                    logger.warning("Scanner: prefetch FAILED %s/%s: %s", sym, tf, _exc)
                    return cache_key, tf, []
                finally:
                    _inner.shutdown(wait=False, cancel_futures=True)

            # Execute all fetches concurrently — cap at 20 workers.
            # With 20 symbols × 3 TFs = 60 tasks, 10 workers hit the 20s
            # batch timeout on the tail end. Bybit rate limit is 120 req/5s
            # for market-data endpoints, so 20 concurrent is safe.
            _max_fetch_workers = min(len(_fetch_manifest), 20)
            metrics.fetch_concurrency = _max_fetch_workers
            _ohlcv_cache: dict[str, list] = {}  # cache_key → raw OHLCV

            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=_max_fetch_workers)
            try:
                _futures = {
                    _pool.submit(_fetch_one_batched, sym, tf, lim, key): key
                    for sym, tf, lim, key in _fetch_manifest
                }
                for _fut in concurrent.futures.as_completed(_futures, timeout=45):
                    try:
                        cache_key, tf, raw = _fut.result()
                        _ohlcv_cache[cache_key] = raw
                        if "|primary" in cache_key and raw and len(raw) >= 30:
                            metrics.symbols_fetched_ok += 1
                        if "|ctx_" in cache_key or "|mtf" in cache_key:
                            if raw and len(raw) >= 20:
                                metrics.context_fetches_ok += 1
                    except Exception as _exc:
                        _key = _futures[_fut]
                        logger.warning("Scanner: prefetch result error for %s: %s", _key, _exc)
                        _ohlcv_cache[_key] = []
            except concurrent.futures.TimeoutError:
                logger.warning("Scanner: OHLCV batch prefetch timed out after 45s")
                metrics.timeouts += 1
            except Exception as _exc:
                logger.warning("Scanner: OHLCV batch prefetch failed: %s", _exc)
            finally:
                _pool.shutdown(wait=False, cancel_futures=True)

            metrics.ohlcv_prefetch_ms = (time.time() - _t0) * 1000

            # v3: Write fetched context data back to the persistent TTL cache
            if self._ohlcv_cache:
                for key, raw in _ohlcv_cache.items():
                    if "|ctx_4h" in key and raw and len(raw) >= 20:
                        self._ohlcv_cache.put(key, raw, _CTX_TTL["4h"])
                    elif "|ctx_1h" in key and raw and len(raw) >= 20:
                        self._ohlcv_cache.put(key, raw, _CTX_TTL["1h"])

            _ok_primary = [s for s in qualifying if _ohlcv_cache.get(f"{s}|primary") and len(_ohlcv_cache[f"{s}|primary"]) >= 30]
            _fail_primary = [s for s in qualifying if s not in _ok_primary]
            logger.info(
                "Scanner: OHLCV batch prefetch complete — %d/%d primary OK, %d/%d context OK "
                "(%.0fms, %d workers, %d cache hits)%s",
                len(_ok_primary), len(qualifying),
                metrics.context_fetches_ok, metrics.context_fetches_total,
                metrics.ohlcv_prefetch_ms, _max_fetch_workers, _cache_hits,
                f", failed: {_fail_primary}" if _fail_primary else "",
            )

            # ── Retry failed primary fetches sequentially ─────────────
            # If any primary OHLCV fetches timed out in the batch phase,
            # retry them one-at-a-time. This handles rate-limit / queue
            # congestion without the compute-phase fallback overhead.
            if _fail_primary:
                logger.info("Scanner: retrying %d failed primary fetches: %s", len(_fail_primary), _fail_primary)
                for _retry_sym in _fail_primary:
                    try:
                        _retry_key = f"{_retry_sym}|primary"
                        _rt0 = time.time()
                        _rp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        try:
                            _rf = _rp.submit(self._exchange.fetch_ohlcv, _retry_sym, self._timeframe, limit=_ohlcv_limit)
                            _rraw = _rf.result(timeout=15.0)
                        except concurrent.futures.TimeoutError:
                            logger.warning("Scanner: retry fetch TIMED OUT for %s after 15s", _retry_sym)
                            continue
                        finally:
                            _rp.shutdown(wait=False, cancel_futures=True)
                        if _rraw and len(_rraw) >= 2:
                            _rraw, _ = enforce_closed_candles(_rraw, self._timeframe, log_symbol=_retry_sym)
                        if _rraw and len(_rraw) >= 30:
                            _ohlcv_cache[_retry_key] = _rraw
                            metrics.symbols_fetched_ok += 1
                            metrics.retries += 1
                            logger.info("Scanner: retry OK for %s — %d bars (%.0fms)",
                                        _retry_sym, len(_rraw), (time.time() - _rt0) * 1000)
                    except Exception as _re:
                        logger.warning("Scanner: retry FAILED for %s: %s", _retry_sym, _re)
                _ok_primary = [s for s in qualifying if _ohlcv_cache.get(f"{s}|primary") and len(_ohlcv_cache[f"{s}|primary"]) >= 30]
                _still_fail = [s for s in qualifying if s not in _ok_primary]
                if _still_fail:
                    logger.warning("Scanner: %d symbols still failed after retry: %s", len(_still_fail), _still_fail)

            # ══════════════════════════════════════════════════════════
            # PHASE 2: Parallel per-symbol compute pipeline
            #
            # Each symbol is processed independently in a thread pool.
            # Thread safety is ensured by:
            #   - Per-symbol EnsembleRegimeClassifier (created in worker)
            #   - Per-symbol RegimeTransitionController (created in worker)
            #   - Per-symbol HMM model (keyed dict, each has RLock)
            #   - Shared SignalGenerator (stateless model.evaluate() calls)
            #   - Shared ConfluenceScorer (has threading.Lock)
            #   - Shared MS-GARCH singleton (has threading.Lock)
            # ══════════════════════════════════════════════════════════
            _t0 = time.time()
            df_cache: dict[str, pd.DataFrame] = {}

            # Pre-read settings ONCE (avoids per-symbol lock contention on settings)
            _settings_snapshot = {
                "ms_garch_enabled": bool(_sc.get("ms_garch.enabled", True)),
                "hmm_enabled": bool(_sc.get("hmm_regime.enabled", True)),
                "pbl_slc_enabled": _pbl_slc_enabled,
                "mtf_enabled": _mtf_enabled,
                "higher_tf": _higher_tf,
                "disabled_models": list(_sc.get("disabled_models", [])),
                "p2c_enh_enabled": bool(_sc.get("phase_2c.pullback_enhancement.enabled", False)),
                "p2c_rb_enabled": bool(_sc.get("phase_2c.range_breakout.enabled", False)),
            }

            def _process_symbol(symbol: str) -> tuple[str, Optional[OrderCandidate], str, float, Optional[pd.DataFrame], str, dict]:
                """
                Full compute pipeline for one symbol. Thread-safe.
                Returns (symbol, candidate, regime, confidence, df, pre_rejection, diagnostics).
                """
                _sym_t0 = time.time()
                try:
                    result = self._scan_symbol_with_regime(
                        symbol, tickers.get(symbol, {}),
                        prefetched_ohlcv=_ohlcv_cache.get(f"{symbol}|primary"),
                        prefetched_ctx_4h=_ohlcv_cache.get(f"{symbol}|ctx_4h"),
                        prefetched_ctx_1h=_ohlcv_cache.get(f"{symbol}|ctx_1h"),
                        prefetched_mtf=_ohlcv_cache.get(f"{symbol}|mtf"),
                        settings_snapshot=_settings_snapshot,
                    )
                    _elapsed = (time.time() - _sym_t0) * 1000
                    logger.debug("Scanner: %s compute complete in %.0fms", symbol, _elapsed)
                    return (symbol, *result, _elapsed)
                except Exception as _exc:
                    import traceback as _tb
                    _tb_str = _tb.format_exc()
                    _elapsed = (time.time() - _sym_t0) * 1000
                    logger.error("Scanner: error scanning %s (%.0fms): %s\n%s", symbol, _elapsed, _exc, _tb_str)
                    try:
                        import pathlib as _pl, datetime as _dt
                        _diag_path = _pl.Path(__file__).parent.parent.parent / "data" / "scan_error_diag.txt"
                        _diag_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_diag_path, "a", encoding="utf-8") as _df:
                            _df.write(f"\n=== {_dt.datetime.utcnow().isoformat()} symbol={symbol} ===\n")
                            _df.write(_tb_str)
                            _df.flush()
                    except Exception:
                        pass
                    return (symbol, None, "", 0.0, None, "Scan error", {}, _elapsed)

            # Execute per-symbol compute in parallel — use up to 20 workers.
            # With persisted classifiers (no HMM refit), compute is CPU-light.
            # numpy/hmmlearn release the GIL, so real parallelism is achieved.
            _max_compute_workers = min(len(qualifying), 20)
            metrics.compute_concurrency = _max_compute_workers

            _compute_pool = concurrent.futures.ThreadPoolExecutor(max_workers=_max_compute_workers)
            try:
                _compute_futures = {
                    _compute_pool.submit(_process_symbol, sym): sym
                    for sym in qualifying
                }
                for _fut in concurrent.futures.as_completed(_compute_futures, timeout=90):
                    try:
                        result = _fut.result()
                        if len(result) == 8:
                            symbol, candidate, regime, confidence, df, pre_rejection, sym_diag, elapsed_ms = result
                        else:
                            # Shouldn't happen but be defensive
                            symbol = _compute_futures[_fut]
                            candidate, regime, confidence, df, pre_rejection, sym_diag = None, "", 0.0, None, "Parse error", {}
                            elapsed_ms = 0.0

                        metrics.per_symbol_ms[symbol] = elapsed_ms
                        metrics.symbols_computed += 1

                        # Aggregate sub-phase timings from sym_diag
                        if isinstance(sym_diag, dict):
                            metrics.indicator_ms += sym_diag.get("_indicator_ms", 0.0)
                            metrics.regime_ms += sym_diag.get("_regime_ms", 0.0)
                            metrics.signal_ms += sym_diag.get("_signal_ms", 0.0)
                            metrics.confluence_ms += sym_diag.get("_confluence_ms", 0.0)

                        if df is not None:
                            df_cache[symbol] = df
                        if regime:
                            _regime_votes[regime] = _regime_votes.get(regime, 0) + 1
                            if confidence > _regime_confs.get(regime, 0.0):
                                _regime_confs[regime] = confidence
                        if candidate:
                            all_candidates.append(candidate)
                            _all_sym_results[symbol] = {
                                **candidate.to_dict(),
                                "status": "pending",
                                "is_approved": False,
                                "diagnostics": sym_diag,
                            }
                        else:
                            _r = self._empty_sym_result(symbol, pre_rejection or "No signal", regime)
                            _r["diagnostics"] = sym_diag
                            _all_sym_results[symbol] = _r

                        # Emit per-symbol progress for UI
                        self.symbol_scanned.emit(symbol, regime, candidate.score if candidate else 0.0)

                    except Exception as _exc:
                        _sym = _compute_futures[_fut]
                        logger.error("Scanner: compute result error for %s: %s", _sym, _exc)
                        _r = self._empty_sym_result(_sym, "Scan error")
                        _r["diagnostics"] = {}
                        _all_sym_results[_sym] = _r
                        metrics.symbols_failed.append(_sym)
            except concurrent.futures.TimeoutError:
                logger.error("Scanner: parallel compute timed out after 30s")
                metrics.timeouts += 1
            finally:
                _compute_pool.shutdown(wait=False, cancel_futures=True)

            metrics.compute_phase_ms = (time.time() - _t0) * 1000

            # Compute slowest/average symbol metrics
            if metrics.per_symbol_ms:
                _slowest = max(metrics.per_symbol_ms, key=metrics.per_symbol_ms.get)
                metrics.slowest_symbol = _slowest
                metrics.slowest_symbol_ms = metrics.per_symbol_ms[_slowest]
                metrics.avg_symbol_ms = sum(metrics.per_symbol_ms.values()) / len(metrics.per_symbol_ms)

            # ══════════════════════════════════════════════════════════
            # PHASE 3: Risk gate + Atomic result emission
            # ══════════════════════════════════════════════════════════
            _t0 = time.time()
            spread_map = {}
            for sym, ticker in tickers.items():
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                if bid and ask and float(bid) > 0:
                    spread_map[sym] = (float(ask) - float(bid)) / float(bid) * 100.0

            approved, rejected = self._risk_gate.validate_batch(
                all_candidates,
                self._open_positions,
                self._capital_usdt,
                self._drawdown_pct,
                spread_map,
            )

            for r in rejected:
                logger.info("Scanner: rejected %s — %s", r.symbol, r.rejection_reason)
                if r.symbol in _all_sym_results:
                    _all_sym_results[r.symbol]["status"] = r.rejection_reason or "Rejected"
                    _all_sym_results[r.symbol]["is_approved"] = False
                    _all_sym_results[r.symbol]["rejection_reason"] = r.rejection_reason or "Rejected"

            _scan_ts = datetime.utcnow().isoformat()
            for c in approved:
                if c.symbol in _all_sym_results:
                    _all_sym_results[c.symbol]["status"] = "approved"
                    _all_sym_results[c.symbol]["is_approved"] = True
                    if not _all_sym_results[c.symbol].get("generated_at"):
                        _all_sym_results[c.symbol]["generated_at"] = _scan_ts

            for _sym, _row in _all_sym_results.items():
                if not _row.get("generated_at"):
                    _row["generated_at"] = _scan_ts

            metrics.risk_gate_ms = (time.time() - _t0) * 1000

            # Atomic emission — all results published together
            self.scan_complete.emit([c.to_dict() for c in approved])
            self.scan_all_results.emit(list(_all_sym_results.values()))

            # ── Post-scan housekeeping (non-critical path) ──────
            _t0 = time.time()
            try:
                from core.risk.crash_detector import get_crash_detector
                _cd = get_crash_detector()
                _cd.evaluate(tickers, df_cache)
            except Exception as exc:
                logger.debug("Scanner: CrashDetector update failed: %s", exc)

            try:
                from core.portfolio.correlation_controller import get_correlation_controller
                _corr_ctrl = get_correlation_controller()
                _symbols_with_data = [s for s in df_cache if df_cache[s] is not None
                                      and len(df_cache[s]) >= 30]
                for _i, _sym_a in enumerate(_symbols_with_data):
                    for _sym_b in _symbols_with_data[_i + 1:]:
                        _ret_a = df_cache[_sym_a]["close"].pct_change().dropna().tolist()
                        _ret_b = df_cache[_sym_b]["close"].pct_change().dropna().tolist()
                        _corr_ctrl.update_live_correlation(_sym_a, _sym_b, _ret_a, _ret_b)
            except Exception as exc:
                logger.debug("Scanner: correlation update failed: %s", exc)

            self.df_cache_updated.emit(df_cache)
            metrics.post_scan_ms = (time.time() - _t0) * 1000

            # ── Publish majority regime ───────────────────────
            if _regime_votes:
                dominant_regime = max(_regime_votes, key=_regime_votes.__getitem__)
                dom_confidence = _regime_confs.get(dominant_regime, 0.5)
                total_votes = sum(_regime_votes.values())
                regime_probs = {r: v / total_votes for r, v in _regime_votes.items()}
                bus.publish(
                    Topics.REGIME_CHANGED,
                    {
                        "new_regime": dominant_regime,
                        "confidence": dom_confidence,
                        "regime_probs": regime_probs,
                        "classifier": "rule-based (scanner)",
                        "symbol_count": len(_regime_votes),
                    },
                    source="scanner",
                )

            # ── Log performance report ────────────────────────
            metrics.total_cycle_ms = (time.time() - metrics.cycle_start) * 1000
            metrics.log_summary()
            # Emit metrics dict for UI consumption
            try:
                self.scan_metrics_updated.emit(metrics.to_dict())
            except Exception:
                pass

        except Exception as exc:
            logger.error("ScanWorker fatal error: %s", exc, exc_info=True)
            self.scan_error.emit(str(exc))

    def _scan_symbol_with_regime(
        self, symbol: str, ticker: dict,
        prefetched_ohlcv: Optional[list] = None,
        prefetched_ctx_4h: Optional[list] = None,
        prefetched_ctx_1h: Optional[list] = None,
        prefetched_mtf: Optional[list] = None,
        settings_snapshot: Optional[dict] = None,
    ) -> tuple[Optional[OrderCandidate], str, float, Optional[pd.DataFrame], str, dict]:
        """
        Run the full compute pipeline for one symbol. Thread-safe.

        Returns (candidate_or_None, regime_label, confidence, df_or_None, pre_rejection, diagnostics).

        v2: Accepts prefetched context data (4h, 1h, MTF) from the batch fetch
        phase, eliminating per-symbol REST calls inside the compute pipeline.
        Also accepts a settings_snapshot to avoid lock contention on the settings
        singleton from multiple parallel threads.
        """
        _sym_diag: dict = {}
        _ss = settings_snapshot or {}

        # Use pre-fetched OHLCV when available; fall back to live fetch otherwise.
        if prefetched_ohlcv is not None and len(prefetched_ohlcv) >= 30:
            raw = prefetched_ohlcv
        else:
            from config.settings import settings as _sc
            limit = int(_sc.get("scanner.ohlcv_bars", 300))
            _tp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = _tp.submit(
                    self._exchange.fetch_ohlcv, symbol, self._timeframe, limit
                )
                raw = _fut.result(timeout=15.0)
                raw, _dropped = enforce_closed_candles(
                    raw, self._timeframe, log_symbol=symbol,
                )
            except concurrent.futures.TimeoutError:
                logger.warning("Scanner: fallback fetch_ohlcv timed out for %s — skipping", symbol)
                raw = []
            finally:
                _tp.shutdown(wait=False, cancel_futures=True)
        if not raw or len(raw) < 30:
            return None, "", 0.0, None, "No data", _sym_diag

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").astype(float)

        # ── OHLCV freshness check ─────────────────────────────
        # Reject stale data: latest bar must be within 3× the timeframe duration.
        # This prevents generating signals from exchange outages or rate-limit stalls.
        try:
            from datetime import timezone as _tz
            _tf_seconds = TF_POLL_SECONDS.get(self._timeframe, 3600)
            _latest_ts  = df.index[-1]
            _now_ts     = pd.Timestamp.now(tz="UTC")
            _age_s      = (_now_ts - _latest_ts).total_seconds()
            _max_age_s  = _tf_seconds * 3
            if _age_s > _max_age_s:
                logger.warning(
                    "Scanner: %s OHLCV data is stale (age=%.0fs > max=%.0fs) — skipping",
                    symbol, _age_s, _max_age_s,
                )
                return None, "", 0.0, None, "Stale data", _sym_diag
        except Exception as _freshness_err:
            logger.debug("Scanner: freshness check failed for %s: %s", symbol, _freshness_err)

        # Calculate indicators — scan mode only (CORE set).
        # calculate_scan_mode() computes the minimum columns required by all
        # active live-scan consumers (TrendModel, MomentumBreakout, Regime,
        # ATR-based models, volatility pre-filter). BacktestEngine and
        # IDSSBacktester continue to call calculate_all() for the full set.
        _t_ind = time.time()
        df = calculate_scan_mode(df)
        _sym_diag["_indicator_ms"] = (time.time() - _t_ind) * 1000

        # ── Indicator presence guard ──────────────────────────────────
        # calculate_scan_mode() has a silent failure mode: if the 'ta'
        # library fails or raises an exception, it returns raw OHLCV
        # without any indicators.  Models then find None for ADX/RSI/EMA
        # and return no signal — showing "No signal" when the real issue
        # is missing indicators.  Catch this early and surface it.
        _required_cols = ("adx", "ema_9", "rsi_14")
        _missing_cols = [c for c in _required_cols if c not in df.columns or df[c].isna().all()]
        if _missing_cols:
            logger.error(
                "Scanner: %s — required indicator(s) %s NOT computed after "
                "calculate_scan_mode().  'ta' library may have failed silently.  "
                "Check OHLCV data quality and 'ta' installation.",
                symbol, _missing_cols,
            )
            _sym_diag["indicator_cols_missing"] = _missing_cols
            return None, "", 0.0, df, "Indicators missing", _sym_diag

        # ── Phase 1 pre-scan filters (time-of-day, volatility) ─────────
        try:
            from core.filters.trade_filters import apply_pre_scan_filters
            _pf_ok, _pf_reason = apply_pre_scan_filters(symbol, df, self._timeframe)
            if not _pf_ok:
                logger.debug("Scanner: %s pre-filter REJECTED — %s", symbol, _pf_reason)
                return None, "", 0.0, df, _pf_reason, _sym_diag
        except Exception as _pf_exc:
            logger.debug("Scanner: pre-filter error for %s: %s", symbol, _pf_exc)

        # ── Candle metadata for rationale panel ───────────────────────
        _sym_diag["candle_count"] = len(df)
        try:
            _latest_ts = df.index[-1]
            _now_ts_d  = pd.Timestamp.now(tz="UTC")
            _sym_diag["candle_age_s"]  = round((_now_ts_d - _latest_ts).total_seconds(), 1)
            _sym_diag["candle_ts_str"] = _latest_ts.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            _sym_diag["candle_age_s"]  = None
            _sym_diag["candle_ts_str"] = "Unknown"

        # Regime classification — v3: persist per-symbol instances across cycles.
        # This eliminates HMM 4-state retraining every cycle (~30-100ms per symbol).
        # Thread safety: each symbol has its own instance, and the ThreadPoolExecutor
        # processes each symbol in exactly one thread.
        _t_regime = time.time()
        try:
            from core.regime.ensemble_regime_classifier import EnsembleRegimeClassifier as _ERC
            if symbol not in self._ensemble_classifiers:
                self._ensemble_classifiers[symbol] = _ERC()
                logger.debug("Scanner: created new EnsembleRegimeClassifier for %s", symbol)
            _local_clf = self._ensemble_classifiers[symbol]
        except ImportError:
            _local_clf = RegimeClassifier()
        regime, confidence, features = _local_clf.classify(df)
        logger.debug("Scanner: %s regime=%s (conf=%.2f)", symbol, regime, confidence)

        # Apply transition controller — v3: persist per-symbol for effective hysteresis.
        try:
            from core.regime.regime_transition_controller import RegimeTransitionController as _RTC
            if symbol not in self._transition_controllers:
                self._transition_controllers[symbol] = _RTC()
            _local_tc = self._transition_controllers[symbol]
        except ImportError:
            _local_tc = None
        if _local_tc is not None:
            confirmed_regime, in_transition, blend_weight = _local_tc.update(
                regime, confidence, features
            )
            if in_transition:
                logger.debug("Scanner: %s regime transition in progress (blend=%.2f)", symbol, blend_weight)
            regime = confirmed_regime

        # MS-GARCH volatility forecast — v3: per-symbol instance (no singleton lock)
        try:
            if _ss.get("ms_garch_enabled", True):
                from core.regime.ms_garch_forecaster import MSGARCHForecaster
                if symbol not in self._garch_models:
                    self._garch_models[symbol] = MSGARCHForecaster()
                _sym_garch = self._garch_models[symbol]
                garch_forecast = _sym_garch.forecast(df, horizon=3)
                regime = _sym_garch.get_regime_adjustment(regime, garch_forecast)
                # Scale confidence by GARCH consistency
                if garch_forecast.get("confidence", 0) > 0.7:
                    confidence = min(confidence * 1.05, 1.0)
        except Exception:
            pass  # GARCH is advisory only

        # ── HMM probabilistic regime classification (per-symbol model) ──────
        # Each symbol has its own HMMRegimeClassifier instance stored in
        # self._hmm_models[symbol], persisted across scan cycles by AssetScanner.
        # This eliminates the bias of training on one symbol and reusing for all.
        regime_probs: dict = {}
        if self._use_hmm:
            try:
                from core.regime.hmm_regime_classifier import HMMRegimeClassifier
                if _ss.get("hmm_enabled", True):
                    # Create per-symbol HMM instance on first encounter
                    if symbol not in self._hmm_models:
                        self._hmm_models[symbol] = HMMRegimeClassifier()
                        logger.debug("ScanWorker: created new HMM for %s", symbol)

                    hmm_clf = self._hmm_models[symbol]

                    # Fit this symbol's model if not yet trained
                    if not hmm_clf.is_fitted:
                        hmm_clf.fit(df)

                    hmm_label, hmm_conf, regime_probs = hmm_clf.classify_combined(df)
                    # Blend: if HMM has higher confidence, prefer its label
                    if hmm_conf > confidence:
                        regime = hmm_label
                        confidence = hmm_conf
                    logger.debug("Scanner: %s HMM regime=%s (conf=%.2f) probs=%s",
                                 symbol, hmm_label, hmm_conf,
                                 {k: round(v, 2) for k, v in regime_probs.items() if v > 0.05})
            except Exception as exc:
                logger.debug("Scanner: HMM classification failed for %s: %s", symbol, exc)
                regime_probs = {}

        # Note: symbol_scanned signal is emitted in run() after result collection
        # to avoid cross-thread Qt signal emission from worker pool threads.

        # ── Regime diagnostics for rationale panel ────────────────────
        _sym_diag["_regime_ms"] = (time.time() - _t_regime) * 1000
        _sym_diag["regime_confidence"] = round(confidence, 3)
        _sym_diag["regime_probs"]      = regime_probs

        # ── PBL/SLC context data (using prefetched data from batch phase) ────────
        # v2: Context data (4h, 1h) was fetched in the batch OHLCV prefetch phase.
        # No per-symbol REST calls here — just DataFrame construction from raw data.
        _pbl_slc_enabled:    bool                  = _ss.get("pbl_slc_enabled", False)
        _df_4h_ctx:          Optional[pd.DataFrame] = None
        _df_1h_ctx:          Optional[pd.DataFrame] = None
        _res_regime_30m_str: str                    = "ranging"
        _res_regime_1h_str:  str                    = "ranging"

        try:
            if _pbl_slc_enabled:
                # v3: Check DF cache before computing indicators on context TFs.
                # Context DataFrames change only when the underlying OHLCV changes
                # (every 4h / 1h respectively), so we cache the computed result.
                for _tf_key, _raw_data in [("4h", prefetched_ctx_4h), ("1h", prefetched_ctx_1h)]:
                    _df_cache_key = f"{symbol}|ctx_{_tf_key}_df"
                    _cached_df = self._ohlcv_cache.get_df(_df_cache_key) if self._ohlcv_cache else None
                    if _cached_df is not None:
                        if _tf_key == "4h":
                            _df_4h_ctx = _cached_df
                        else:
                            _df_1h_ctx = _cached_df
                        logger.debug("Scanner: %s context %s — %d bars (DF cache hit)", symbol, _tf_key, len(_cached_df))
                        continue
                    if _raw_data and len(_raw_data) >= 20:
                        _df_tf = pd.DataFrame(
                            _raw_data,
                            columns=["timestamp", "open", "high", "low", "close", "volume"],
                        )
                        _df_tf["timestamp"] = pd.to_datetime(
                            _df_tf["timestamp"], unit="ms", utc=True
                        )
                        _df_tf = _df_tf.set_index("timestamp").astype(float)
                        _df_tf = calculate_scan_mode(_df_tf)
                        if _tf_key == "4h":
                            _df_4h_ctx = _df_tf
                        else:
                            _df_1h_ctx = _df_tf
                        # Cache the computed DF for next cycle
                        if self._ohlcv_cache:
                            _ttl = _CTX_TTL.get(_tf_key, 3600)
                            self._ohlcv_cache.put_df(_df_cache_key, _df_tf, _ttl)
                        logger.debug("Scanner: %s context %s — %d bars (computed, cached)", symbol, _tf_key, len(_df_tf))

                # Research regime strings
                try:
                    from core.regime.research_regime_classifier import (
                        classify_latest_bar as _res_classify,
                        regime_to_string    as _res_to_str,
                    )
                    _res_regime_30m_str = _res_to_str(_res_classify(df))
                    logger.debug("Scanner: %s research_regime_30m=%s", symbol, _res_regime_30m_str)
                    if _df_1h_ctx is not None and len(_df_1h_ctx) >= 20:
                        _res_regime_1h_str = _res_to_str(_res_classify(_df_1h_ctx))
                        logger.debug("Scanner: %s research_regime_1h=%s", symbol, _res_regime_1h_str)
                except Exception as _res_exc:
                    logger.debug("Scanner: %s research regime error: %s", symbol, _res_exc)

        except Exception as _ctx_outer:
            logger.debug("Scanner: context build error for %s: %s", symbol, _ctx_outer)

        # ── Phase 2c: Transition event detection on 1h data ────────────────
        _p2c_enh_enabled:    bool = _ss.get("p2c_enh_enabled", False)
        _p2c_rb_enabled:     bool = _ss.get("p2c_rb_enabled", False)
        _p2c_enabled:        bool = _p2c_enh_enabled or _p2c_rb_enabled
        _p2c_transition_ev   = None
        _p2c_rb_event        = None
        _p2c_breakout_active: bool = False

        if _p2c_enabled and _df_1h_ctx is not None and len(_df_1h_ctx) >= 30:
            try:
                from core.regime.feature_transition_detector import (
                    FeatureTransitionDetector,
                )
                _ftd = FeatureTransitionDetector(params={
                    "range_breakout.require_confirmation_bar": False,
                })
                _ftd_loc = len(_df_1h_ctx) - 1
                _ftd_idx_4h = None
                if _df_4h_ctx is not None and not _df_4h_ctx.empty:
                    _ts_1h_last = _df_1h_ctx.index[-1]
                    _ftd_idx_4h = int(_df_4h_ctx.index.searchsorted(_ts_1h_last, side="right")) - 1
                    if _ftd_idx_4h < 0:
                        _ftd_idx_4h = None

                _ftd_events = _ftd.detect(
                    _df_1h_ctx, _ftd_loc, df_4h=_df_4h_ctx, idx_4h=_ftd_idx_4h
                )

                for _ev in _ftd_events:
                    if _ev.event_type == "pullback_continuation" and _p2c_enh_enabled:
                        _p2c_transition_ev = _ev
                        logger.debug(
                            "Scanner: %s Phase2c pullback_continuation — dir=%s conf=%.3f",
                            symbol, _ev.direction, _ev.confidence,
                        )
                    elif _ev.event_type == "range_breakout" and _p2c_rb_enabled:
                        _p2c_rb_event = _ev
                        _p2c_breakout_active = True
                        logger.debug(
                            "Scanner: %s Phase2c range_breakout — dir=%s conf=%.3f",
                            symbol, _ev.direction, _ev.confidence,
                        )
            except Exception as _ftd_exc:
                logger.debug("Scanner: %s Phase2c FTD error: %s", symbol, _ftd_exc)

        # ── Main model signal generation (NexusTrader HMM regime) ────────────
        # TrendModel, MomentumBreakout, FundingRate, Sentiment, RL use the
        # HMM+rule-based blended regime from classify_combined() above.
        # PBL and SLC are excluded here: their ACTIVE_REGIMES=["bull_trend"] /
        # ["bear_trend"] will block them against the HMM regime string reliably,
        # but they are given dedicated calls below with the correct classifier.
        _t_signal = time.time()
        signals = self._sig_gen.generate(
            symbol, df, regime, self._timeframe,
            regime_probs=regime_probs,
            context={},
        )

        # ── PBL dedicated call (ResearchRegimeClassifier 30m → ACTIVE_REGIMES gate)
        if _pbl_slc_enabled:
            try:
                # Build PBL context with Phase 2c enhancement layer
                _pbl_ctx = {}
                if _df_4h_ctx is not None:
                    _pbl_ctx["df_4h"] = _df_4h_ctx
                if _p2c_transition_ev is not None:
                    _pbl_ctx["transition_event"] = _p2c_transition_ev
                _pbl_ctx["breakout_active"] = _p2c_breakout_active

                _pbl_raw = self._sig_gen.generate(
                    symbol, df, _res_regime_30m_str, "30m",
                    regime_probs={},
                    context=_pbl_ctx,
                ) or []
                _pbl_only = [s for s in _pbl_raw if s.model_name == "pullback_long"]
                if _pbl_only:
                    signals = list(signals or []) + _pbl_only
                    logger.debug(
                        "Scanner: %s PBL signal (%s) — regime=%s p2c_boost=%s",
                        symbol, _pbl_only[0].direction, _res_regime_30m_str,
                        _p2c_transition_ev is not None,
                    )
            except Exception as _pbl_exc:
                logger.debug("Scanner: PBL generate error %s: %s", symbol, _pbl_exc)

            # ── SLC dedicated call (ResearchRegimeClassifier 1h → ACTIVE_REGIMES gate)
            if _df_1h_ctx is not None:
                try:
                    _slc_ctx = {"df_1h": _df_1h_ctx}
                    if _p2c_transition_ev is not None:
                        _slc_ctx["transition_event"] = _p2c_transition_ev

                    _slc_raw = self._sig_gen.generate(
                        symbol, df, _res_regime_1h_str, "1h",
                        regime_probs={},
                        context=_slc_ctx,
                    ) or []
                    _slc_only = [s for s in _slc_raw if s.model_name == "swing_low_continuation"]
                    if _slc_only:
                        signals = list(signals or []) + _slc_only
                        logger.debug(
                            "Scanner: %s SLC signal (%s) — regime_1h=%s",
                            symbol, _slc_only[0].direction, _res_regime_1h_str,
                        )
                except Exception as _slc_exc:
                    logger.debug("Scanner: SLC generate error %s: %s", symbol, _slc_exc)

        # ── Phase 2c RangeBreakout dedicated call ────────────────────────────
        if _p2c_rb_enabled and _p2c_rb_event is not None:
            try:
                _rb_ctx = {"transition_event": _p2c_rb_event}
                _rb_raw = self._sig_gen.generate(
                    symbol, df, regime, self._timeframe,
                    regime_probs=regime_probs,
                    context=_rb_ctx,
                ) or []
                _rb_only = [s for s in _rb_raw if s.model_name == "range_breakout"]
                if _rb_only:
                    signals = list(signals or []) + _rb_only
                    logger.info(
                        "Scanner: %s RB signal (%s) — conf=%.3f range=[%.2f,%.2f]",
                        symbol, _rb_only[0].direction,
                        _p2c_rb_event.confidence,
                        _p2c_rb_event.features_snapshot.get("range_low", 0),
                        _p2c_rb_event.features_snapshot.get("range_high", 0),
                    )
            except Exception as _rb_exc:
                logger.debug("Scanner: RB generate error %s: %s", symbol, _rb_exc)

        # ── Phase 2c signal counts + shadow tracker ─────────────────────
        _signal_count_pbl = len([s for s in (signals or []) if s.model_name == "pullback_long"])
        _signal_count_slc = len([s for s in (signals or []) if s.model_name == "swing_low_continuation"])
        _signal_count_rb  = len([s for s in (signals or []) if s.model_name == "range_breakout"])
        if _signal_count_pbl or _signal_count_slc or _signal_count_rb:
            logger.info(
                "Scanner: %s Phase2c signals — PBL=%d SLC=%d RB=%d",
                symbol, _signal_count_pbl, _signal_count_slc, _signal_count_rb,
            )
        if (_signal_count_pbl + _signal_count_slc + _signal_count_rb) > 0:
            try:
                from core.scanning.shadow_tracker import shadow_tracker as _st
                for _sig in (signals or []):
                    if _sig.model_name not in ("pullback_long", "swing_low_continuation", "range_breakout"):
                        continue
                    _st.record_signal(
                        symbol=symbol,
                        model=_sig.model_name,
                        direction=_sig.direction,
                        strength=_sig.strength,
                        entry_price=getattr(_sig, "entry_price", 0.0) or 0.0,
                        stop_loss=_sig.stop_loss,
                        take_profit=_sig.take_profit,
                        regime=regime,
                        was_boosted="Phase2c ModeA" in (_sig.rationale or ""),
                        was_relaxed="ModeA+B" in (_sig.rationale or ""),
                        breakout_active=_p2c_breakout_active,
                        rb_confidence=_p2c_rb_event.confidence if (_sig.model_name == "range_breakout" and _p2c_rb_event) else 0.0,
                        rb_range_width=(
                            _p2c_rb_event.features_snapshot.get("range_high", 0) - _p2c_rb_event.features_snapshot.get("range_low", 0)
                        ) if (_sig.model_name == "range_breakout" and _p2c_rb_event) else 0.0,
                        rationale=_sig.rationale or "",
                    )
            except Exception as _st_exc:
                logger.debug("Scanner: shadow tracker error: %s", _st_exc)

        _sym_diag["_signal_ms"] = (time.time() - _t_signal) * 1000

        # ── Model-level diagnostics for rationale panel ───────────────
        _disabled_names = list(_ss.get("disabled_models", []))
        _all_m_names = [m.name for m in self._sig_gen._models]
        # Include RL model name if it exists
        if self._sig_gen._rl_model is not None:
            _rl_name = getattr(self._sig_gen._rl_model, "name", "rl_ensemble")
            if _rl_name not in _all_m_names:
                _all_m_names = list(_all_m_names) + [_rl_name]
        _fired_names = [s.model_name for s in signals]
        _no_sig_names = [m for m in _all_m_names if m not in _fired_names and m not in _disabled_names]
        _sym_diag["all_model_names"]   = _all_m_names
        _sym_diag["models_disabled"]   = _disabled_names
        _sym_diag["models_fired"]      = _fired_names
        _sym_diag["models_no_signal"]  = _no_sig_names
        # Capture raw signal strengths / directions for context
        _sym_diag["signal_details"]    = {
            s.model_name: {"direction": s.direction, "strength": round(s.strength, 3)}
            for s in signals
        }

        if not signals:
            return None, regime, confidence, df, "No signal", _sym_diag

        # Confluence scoring with regime probabilities
        _t_confl = time.time()
        candidate = self._scorer.score(signals, symbol, regime_probs=regime_probs)
        _sym_diag["_confluence_ms"] = (time.time() - _t_confl) * 1000
        logger.debug("Scanner: %s — scorer returned candidate=%s", symbol, candidate is not None)

        # ── Merge scorer diagnostics into sym_diag ────────────────────
        try:
            _scorer_d = dict(getattr(self._scorer, "_last_diagnostics", {}))
            _sym_diag.update(_scorer_d)
        except Exception:
            pass

        if not candidate:
            return None, regime, confidence, df, "Below threshold", _sym_diag

        logger.debug("Scanner: %s — candidate score=%.3f, about to enter MTF block", symbol, candidate.score)

        # ── Multi-timeframe confirmation (using prefetched data) ──────
        # v2: MTF data was fetched in the batch OHLCV prefetch phase.
        # No per-symbol REST calls here — just DataFrame construction.
        if candidate and _ss.get("mtf_enabled", False):
            try:
                _higher_tf = _ss.get("higher_tf")
                # Fall back to ctx_4h if mtf key was deduped (both are 4h)
                raw_htf = prefetched_mtf or prefetched_ctx_4h
                if raw_htf and len(raw_htf) >= 20:
                    df_htf = pd.DataFrame(raw_htf, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_htf["timestamp"] = pd.to_datetime(df_htf["timestamp"], unit="ms", utc=True)
                    df_htf = df_htf.set_index("timestamp").astype(float)
                    df_htf = calculate_scan_mode(df_htf)
                    htf_regime, _, _ = _local_clf.classify(df_htf)
                    candidate.higher_tf_regime = htf_regime
                    logger.debug("Scanner: %s higher-TF (%s) regime=%s (prefetched)", symbol, _higher_tf, htf_regime)
            except Exception as exc:
                logger.debug("Scanner: MTF classification failed for %s: %s", symbol, exc)

        logger.debug("Scanner: %s — _scan_symbol_with_regime RETURNING (candidate=%s)", symbol, candidate is not None)
        return candidate, regime, confidence, df, "", _sym_diag


class AssetScanner(QObject):
    """
    Manages the recurring scan cycle using dual QTimers:
      - HTF timer (1H): signal generation → staged candidates (CREATED)
      - LTF timer (15m): confirmation scan → CONFIRMED candidates → execution

    Emits candidates_ready(list[dict]) when the 1H scan produces approved candidates
    (for UI table display — NO execution from this signal).

    Emits confirmed_ready(list[dict]) when the 15m LTF scan confirms candidates
    (for execution pathway — this is the ONLY execution trigger).
    """
    candidates_ready  = Signal(list)   # list of approved OrderCandidate dicts (UI display only)
    confirmed_ready   = Signal(list)   # list of CONFIRMED candidate dicts (execution trigger)
    scan_all_results  = Signal(list)   # all per-symbol results with rejection reasons (UI only)
    scan_metrics_updated = Signal(object)  # ScanCycleMetrics dict — phase timing for UI
    scan_started      = Signal()
    scan_finished     = Signal(int)    # n candidates found (HTF)
    ltf_scan_finished = Signal(int)    # n confirmed (LTF)
    scan_error        = Signal(str)
    symbol_progress   = Signal(str, str, float)  # symbol, regime, score

    def __init__(self, timeframe: str = "1h", parent=None):
        super().__init__(parent)
        self._timeframe      = timeframe
        self._timer          = QTimer(self)     # HTF (1H) scan timer
        self._worker: Optional[ScanWorker] = None
        self._running        = False
        self._watchlist_mgr  = WatchlistManager()
        # Exposed RiskGate instance — used by the Risk Management page to read
        # current limits.  Rebuilt from settings on every SETTINGS_CHANGED event.
        self._risk_gate = self._make_risk_gate()

        # BTC-only mode support
        self._btc_only: bool = False

        # WebSocket feed integration
        self._ws_feed: Optional[object] = None

        # Per-symbol HMM models — persisted between scan cycles so each symbol
        # retains its trained HMM without re-fitting on every scan.
        self._hmm_models: dict = {}

        # v3: Per-symbol persistent objects — eliminates retraining/recreation overhead.
        self._ensemble_classifiers: dict = {}   # symbol → EnsembleRegimeClassifier
        self._transition_controllers: dict = {} # symbol → RegimeTransitionController
        self._garch_models: dict = {}           # symbol → MSGARCHForecaster
        self._ohlcv_cache = _OHLCVCache()       # TTL cache for context TFs

        # Indicator DataFrames from the most recent scan cycle, passed to the
        # next ScanWorker so the UniverseFilter can apply the ATR range filter.
        self._prev_df_cache: dict = {}

        # Timestamp of the most recently completed scan cycle (UTC).
        # Exposed so the health-check notification can report "Last scan: X min ago".
        self._last_scan_at: Optional[datetime] = None

        # Timestamp when the current scan worker was launched (UTC).
        # Used by the watchdog to detect stuck workers.
        self._worker_started_at: Optional[float] = None

        # Wall-clock time (time.time()) of the last watchdog fire.
        # Used to detect system sleep/wake: if the gap between two consecutive
        # watchdog fires exceeds 3× the watchdog interval (90s for a 30s timer),
        # the system almost certainly went to sleep.  On wake we trigger an
        # immediate recovery scan without waiting for the staleness timeout.
        self._watchdog_last_fired_at: Optional[float] = None

        # Maximum allowed scan duration in seconds before the watchdog
        # force-releases the worker reference.  Default: 120s (2 minutes).
        self._max_scan_duration_s: float = 120.0

        # ── Watchdog recovery cooldown ─────────────────────────
        # Prevents infinite restart loops when scans keep hanging.
        # After a watchdog-forced kill, wait at least this long before
        # allowing the staleness detector to trigger another scan.
        self._watchdog_last_kill_at: Optional[float] = None
        self._watchdog_recovery_cooldown_s: float = 300.0  # 5 minutes
        self._watchdog_consecutive_kills: int = 0
        self._watchdog_max_consecutive_kills: int = 3  # after 3 kills, extend cooldown

        # SignalGenerator is a heavyweight object (contains RLSignalModel →
        # RLEnsemble).  Create it ONCE here and reuse across all ScanWorker
        # instances so that RL models accumulate experience between scans instead
        # of being recreated (and reset) every hour.
        self._sig_gen: SignalGenerator = SignalGenerator()
        self._sig_gen._warmup_complete = True
        self._sig_gen._warmup_bars_remaining = 0

        interval_s = TF_POLL_SECONDS.get(timeframe, 3600)
        self._timer.setInterval(interval_s * 1000)
        self._timer.timeout.connect(self._trigger_scan)
        bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)

        # ── LTF (15m) confirmation timer ──────────────────────
        # Runs every 15 minutes (900s). Evaluates CREATED staged candidates
        # against 15m closed-candle data. Only active when staged_candidates
        # is enabled in settings.
        self._ltf_timer = QTimer(self)
        self._ltf_timer.setInterval(900_000)  # 15 minutes
        self._ltf_timer.timeout.connect(self._trigger_ltf_scan)
        self._ltf_worker: Optional[object] = None
        self._ltf_worker_started_at: Optional[float] = None

        # ── Scan lock: prevents 1H and 15m scans from overlapping ──
        # Both timers run on the main thread, so this is a simple bool
        # (no threading.Lock needed — Qt signals are dispatched serially
        # on the main event loop).
        self._any_scan_active: bool = False

        # ── Staged candidates enabled flag ────────────────────
        try:
            from config.settings import settings as _sc
            self._staged_enabled = bool(_sc.get("staged_candidates.enabled", True))
        except Exception:
            self._staged_enabled = True

        # ── Candle-alignment pending flags ────────────────────────────────────
        # Set to True between start() and the singleShot firing.
        # The watchdog's timer-heartbeat must NOT restart _timer/_ltf_timer
        # during this window — they are intentionally inactive while waiting
        # for the first aligned candle-close boundary.
        self._htf_alignment_pending: bool = False
        self._ltf_alignment_pending: bool = False

        # ── Watchdog timer ────────────────────────────────────
        # Runs every 30 seconds to detect and recover from stuck ScanWorker
        # threads.  If a worker has been running longer than _max_scan_duration_s,
        # the watchdog force-releases the reference so the next scheduled scan
        # can proceed.  This is the last line of defense after per-call timeouts.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(30_000)  # check every 30s
        self._watchdog.timeout.connect(self._check_worker_health)
        self._watchdog.start()

    def set_timeframe(self, timeframe: str) -> None:
        self._timeframe = timeframe
        interval_s = TF_POLL_SECONDS.get(timeframe, 3600)
        self._timer.setInterval(interval_s * 1000)

    def set_btc_only(self, enabled: bool) -> None:
        """Enable or disable BTC-only mode."""
        self._btc_only = enabled
        if enabled:
            logger.info("AssetScanner: BTC-only mode activated")
        else:
            logger.info("AssetScanner: BTC-only mode deactivated")

    def enable_websocket_feed(self, symbol: str = "BTC/USDT", timeframe: str = "1h") -> None:
        """
        Enable WebSocket candle feed integration.
        When a candle closes, triggers an immediate scan.
        """
        try:
            from core.market_data.websocket_feed import WebSocketCandleFeed

            self._ws_feed = WebSocketCandleFeed(symbol=symbol, timeframe=timeframe)
            self._ws_feed.candle_closed.connect(self._on_candle_closed)
            self._ws_feed.start()
            logger.info("AssetScanner: WebSocket feed enabled for %s/%s", symbol, timeframe)
        except Exception as exc:
            logger.warning("AssetScanner: WebSocket feed unavailable: %s", exc)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._log_production_config()
        logger.info("AssetScanner started (TF=%s, staged=%s)", self._timeframe, self._staged_enabled)

        # Immediate first scan on startup (gives fast initial data regardless of candle timing)
        self._trigger_scan()

        # ── Candle-boundary alignment ─────────────────────────────────────────
        # After the startup scan, align the HTF repeating timer to fire ~30s
        # after each candle close boundary (e.g. 13:00:30, 14:00:30 for 1h TF)
        # rather than repeating every N seconds from startup time.
        # This ensures every scan always sees the freshest fully-closed candle.
        htf_delay_s = _seconds_to_next_candle(self._timeframe)
        _fire_at = (datetime.utcnow().replace(microsecond=0) +
                    __import__("datetime").timedelta(seconds=htf_delay_s))
        logger.info(
            "AssetScanner: HTF timer aligned — first repeating scan in %ds "
            "(at %s UTC, %.1fmin from now)",
            htf_delay_s,
            _fire_at.strftime("%H:%M:%S"),
            htf_delay_s / 60.0,
        )
        self._htf_alignment_pending = True
        QTimer.singleShot(htf_delay_s * 1000, self._fire_aligned_htf_scan)

        # LTF confirmation timer — align to 15m candle boundaries
        if self._staged_enabled:
            ltf_delay_s = _seconds_to_next_candle("15m")
            _ltf_fire_at = (datetime.utcnow().replace(microsecond=0) +
                            __import__("datetime").timedelta(seconds=ltf_delay_s))
            logger.info(
                "AssetScanner: LTF timer aligned — first repeating scan in %ds "
                "(at %s UTC, %.1fmin from now)",
                ltf_delay_s,
                _ltf_fire_at.strftime("%H:%M:%S"),
                ltf_delay_s / 60.0,
            )
            self._ltf_alignment_pending = True
            QTimer.singleShot(ltf_delay_s * 1000, self._fire_aligned_ltf_scan)

    def _log_production_config(self) -> None:
        """Log the frozen production configuration at startup for auditability.

        The banner reflects ACTUAL execution behaviour by reading the
        execution_mode config block.  When backtest_parity is True the
        banner shows parity-mode values (pos_frac sizing, static SL/TP,
        no partial/breakeven).  Otherwise it shows risk-based values.
        """
        from config.settings import settings as _s
        disabled    = _s.get("disabled_models", [])
        threshold   = _s.get("idss.min_confluence_score", 0.20)
        dyn_on      = _s.get("dynamic_confluence.enabled", False)
        time_f      = _s.get("filters.time_of_day.enabled", False)
        mtf_on      = _s.get("multi_tf.confirmation_required", True)
        ae_on       = _s.get("scanner.auto_execute", True)
        mr_enabled  = _s.get("mr_pbl_slc.enabled", False)
        demo_locked = _s.get("demo_mode.locked", False)
        lock_ver    = _s.get("demo_mode.parameter_lock_version", "n/a")
        pbl = _s.get("mr_pbl_slc.pullback_long", {}) or {}
        from config.constants import APP_VERSION as _VER

        # ── Parity-mode detection (must match PaperExecutor._is_parity_mode) ──
        _parity_on     = bool(_s.get("execution_mode.backtest_parity", False))
        _ai_filter     = bool(_s.get("execution_mode.ai_filter_only", True))
        _pos_frac      = float(_s.get("execution_mode.parity_pos_frac", 0.35))
        _max_heat      = float(_s.get("execution_mode.parity_max_heat", 0.80))
        _max_positions = int(_s.get("execution_mode.parity_max_positions", 10))
        _max_per_asset = int(_s.get("execution_mode.parity_max_per_asset", 3))

        # ── Values that change depending on execution mode ──
        if _parity_on:
            _exec_mode_str = "BACKTEST_PARITY_WITH_AI"
            _sizing_str    = "pos_frac (%.0f%% equity)" % (_pos_frac * 100)
            _exit_str      = "static SL/TP (no partial, no breakeven, no trailing)"
            _heat_str      = "%.0f%% max heat" % (_max_heat * 100)
            _ai_str        = "filter-only (block only, never alter)" if _ai_filter else "full confluence"
        else:
            _exit_mode   = _s.get("exit.mode", "partial")
            _partial_pct = _s.get("exit.partial_pct", 0.33)
            risk_pct     = _s.get("risk_engine.risk_pct_per_trade", 0.75)
            heat_pct     = _s.get("risk_engine.portfolio_heat_max_pct", 0.04) * 100
            _exec_mode_str = "STANDARD (risk-based)"
            _sizing_str    = "risk_based (%.2f%% risk/trade)" % risk_pct
            _exit_str      = "%s (partial=%.0f%% @ 1R + SL→BE)" % (_exit_mode, _partial_pct * 100)
            _heat_str      = "%.0f%% max" % heat_pct
            _ai_str        = "full confluence + orchestrator"

        logger.info(
            "═══════════════════════════════════════════════════════════\n"
            "  NEXUS TRADER v%s — PRODUCTION CONFIGURATION (FROZEN)\n"
            "  DEMO MODE       : %s  (lock_version=%s)\n"
            "  Execution mode  : %s\n"
            "  Active models   : PBL+SLC=%s  FundingRate=✓  Sentiment=✓\n"
            "  Primary TF      : %s  |  HTF gate : 4h\n"
            "  Disabled models : %s\n"
            "  PBL params      : sl=%.1f  tp=%.1f  ema_prox=%.2f  rsi_min=%.0f  wick=%.1f\n"
            "  Confluence      : %.2f (dynamic=%s)\n"
            "  Exit mode       : %s\n"
            "  Sizing mode     : %s\n"
            "  Portfolio heat  : %s\n"
            "  AI agents       : %s\n"
            "  Max positions   : %s  |  Max per asset : %s\n"
            "  Time filter     : %s\n"
            "  MTF confirm     : %s\n"
            "  Auto-execute    : %s\n"
            "  Circuit breaker : 10%% drawdown hard stop\n"
            "═══════════════════════════════════════════════════════════",
            _VER,
            "LOCKED ✓" if demo_locked else "UNLOCKED ⚠️", lock_ver,
            _exec_mode_str,
            "✓" if mr_enabled else "✗",
            self._timeframe,
            disabled,
            pbl.get("sl_atr_mult", "?"), pbl.get("tp_atr_mult", "?"),
            pbl.get("ema_prox_atr_mult", "?"), pbl.get("rsi_min", "?"),
            pbl.get("wick_strength", "?"),
            threshold, dyn_on,
            _exit_str,
            _sizing_str,
            _heat_str,
            _ai_str,
            _max_positions if _parity_on else "N/A (risk-based)",
            _max_per_asset if _parity_on else "N/A",
            time_f, mtf_on, ae_on,
        )
        # Run full demo mode validation if locked
        if demo_locked:
            try:
                from core.orchestrator.demo_startup_log import run_demo_startup_validation
                run_demo_startup_validation()
            except Exception as _dsl_exc:
                logger.warning("DemoStartupLog failed (non-fatal): %s", _dsl_exc)

    def stop(self) -> None:
        self._running = False
        self._htf_alignment_pending = False
        self._ltf_alignment_pending = False
        self._timer.stop()
        self._ltf_timer.stop()
        self._watchdog.stop()
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(3000)
        if self._ltf_worker and self._ltf_worker.isRunning():
            self._ltf_worker.quit()
            self._ltf_worker.wait(3000)
        logger.info("AssetScanner stopped")

    # ── Candle-boundary aligned timer fires ──────────────────────────────────

    def _fire_aligned_htf_scan(self) -> None:
        """
        Called once by QTimer.singleShot at the first candle-close boundary.
        Triggers the HTF scan and then starts the repeating interval timer
        so all subsequent scans are also on-boundary.
        """
        self._htf_alignment_pending = False
        if not self._running:
            return
        logger.info("AssetScanner: HTF aligned tick — starting repeating %s timer",
                    self._timeframe)
        self._trigger_scan()
        self._timer.start()   # starts the repeating interval (already set in __init__)

    def _fire_aligned_ltf_scan(self) -> None:
        """
        Called once by QTimer.singleShot at the first 15m candle-close boundary.
        Triggers the LTF scan and then starts the repeating 15m interval timer.
        """
        self._ltf_alignment_pending = False
        if not self._running:
            return
        logger.info("AssetScanner: LTF aligned tick — starting repeating 15m timer")
        self._trigger_ltf_scan()
        self._ltf_timer.start()   # starts the repeating 15m interval (already set in __init__)

    def scan_now(self) -> None:
        """Trigger an immediate scan outside the timer schedule."""
        self._trigger_scan()

    def _on_candle_closed(self, symbol: str, timeframe: str, candle: dict) -> None:
        """
        WebSocket candle closed event handler.
        Triggers an immediate scan when a candle completes.
        """
        logger.debug("WebSocket candle closed: %s/%s — triggering scan", symbol, timeframe)
        self._trigger_scan()

    def _check_worker_health(self) -> None:
        """
        Watchdog: runs every 30s.  Three responsibilities:

        1. **Stuck worker detection** — If a ScanWorker or LTFScanWorker has
           been running longer than _max_scan_duration_s, force-release the
           reference so the next scheduled scan can proceed.

        2. **Scan staleness detection** — If the scanner is running (_running
           is True) but no scan has completed in 1.5× the expected interval,
           force-trigger a new scan.  This handles sleep/wake, system clock
           jumps, and any other scenario where QTimer misses a tick.

        3. **Timer heartbeat** — If the QTimer itself has stopped (isActive()
           is False) but the scanner should be running, restart it.
        """
        import threading as _threading
        now = time.time()

        # ── 0a. Sleep/wake detection via wall-clock gap ──────────────
        # If the gap between consecutive watchdog fires exceeds 3× the 30s
        # interval, the system almost certainly slept.  Trigger a recovery
        # scan immediately (bypassing the 1.5× staleness wait) so trading
        # resumes within seconds of wake rather than up to 90s later.
        _WATCHDOG_INTERVAL_S = 30.0
        if self._watchdog_last_fired_at is not None:
            _gap = now - self._watchdog_last_fired_at
            if _gap > _WATCHDOG_INTERVAL_S * 3:
                logger.warning(
                    "Scanner WATCHDOG: %.0fs gap between health checks — "
                    "system likely woke from sleep/hibernate; triggering recovery scan",
                    _gap,
                )
                if self._running and not self._any_scan_active:
                    self._trigger_scan()
        self._watchdog_last_fired_at = now

        # ── 0b. Thread count monitoring ──────────────────────────────
        thread_count = _threading.active_count()
        if thread_count > 75:
            logger.warning(
                "Scanner WATCHDOG: high thread count: %d active threads", thread_count
            )

        # ── 1. Stuck HTF worker ──────────────────────────────────
        if self._worker is not None and self._worker_started_at is not None:
            elapsed = now - self._worker_started_at
            if elapsed > self._max_scan_duration_s:
                self._watchdog_consecutive_kills += 1
                self._watchdog_last_kill_at = now
                logger.error(
                    "Scanner WATCHDOG: HTF ScanWorker stuck for %.0fs (limit=%.0fs) — "
                    "force-releasing worker (kill #%d) | threads=%d",
                    elapsed, self._max_scan_duration_s,
                    self._watchdog_consecutive_kills, thread_count,
                )
                try:
                    self._worker.quit()
                    self._worker.wait(1000)
                except Exception:
                    pass
                self._worker = None
                self._worker_started_at = None
                self._any_scan_active = False
                self.scan_error.emit("HTF scan timed out after %.0fs" % elapsed)

        # ── 2. Stuck LTF worker ──────────────────────────────────
        if self._ltf_worker is not None and self._ltf_worker_started_at is not None:
            elapsed = now - self._ltf_worker_started_at
            if elapsed > self._max_scan_duration_s:
                logger.error(
                    "Scanner WATCHDOG: LTF ScanWorker stuck for %.0fs (limit=%.0fs) — "
                    "force-releasing worker | threads=%d",
                    elapsed, self._max_scan_duration_s, thread_count,
                )
                try:
                    self._ltf_worker.quit()
                    self._ltf_worker.wait(1000)
                except Exception:
                    pass
                self._ltf_worker = None
                self._ltf_worker_started_at = None
                self._any_scan_active = False
                self.scan_error.emit("LTF scan timed out after %.0fs" % elapsed)

        # ── 3. Scan staleness detection (sleep/wake recovery) ────
        # Trigger a recovery scan if no scan completed within 1.5× expected.
        # BUT: respect cooldown to prevent infinite restart loops.
        if self._running and not self._any_scan_active:
            expected_interval_s = TF_POLL_SECONDS.get(self._timeframe, 3600)
            staleness_limit_s = expected_interval_s * 1.5
            if self._last_scan_at is not None:
                from datetime import datetime, timezone
                last_scan_age_s = (datetime.utcnow() - self._last_scan_at).total_seconds()
                if last_scan_age_s > staleness_limit_s:
                    # ── Cooldown check: prevent infinite restart loops ──
                    # After a watchdog kill, wait _watchdog_recovery_cooldown_s
                    # before allowing another recovery scan. If we've killed
                    # _max_consecutive_kills in a row, extend cooldown 3×.
                    cooldown = self._watchdog_recovery_cooldown_s
                    if self._watchdog_consecutive_kills >= self._watchdog_max_consecutive_kills:
                        cooldown *= 3  # 15 minutes instead of 5
                    if (self._watchdog_last_kill_at is not None
                            and (now - self._watchdog_last_kill_at) < cooldown):
                        logger.debug(
                            "Scanner WATCHDOG: staleness detected (%.0fs) but in cooldown "
                            "(%.0fs remaining, kills=%d) — waiting",
                            last_scan_age_s,
                            cooldown - (now - self._watchdog_last_kill_at),
                            self._watchdog_consecutive_kills,
                        )
                    else:
                        logger.warning(
                            "Scanner WATCHDOG: last scan was %.0fs ago (limit=%.0fs) — "
                            "triggering recovery scan | kills=%d threads=%d",
                            last_scan_age_s, staleness_limit_s,
                            self._watchdog_consecutive_kills, thread_count,
                        )
                        self._trigger_scan()

        # ── 4. Timer heartbeat ───────────────────────────────────
        # Heartbeat: restart timers if they went inactive unexpectedly.
        # Skip if alignment is still pending (timer is intentionally inactive
        # while waiting for the first candle-boundary singleShot to fire).
        if self._running and not self._htf_alignment_pending and not self._timer.isActive():
            logger.warning(
                "Scanner WATCHDOG: HTF timer found inactive while scanner is running — restarting timer"
            )
            self._timer.start()
        if (self._running and self._staged_enabled
                and not self._ltf_alignment_pending and not self._ltf_timer.isActive()):
            logger.warning(
                "Scanner WATCHDOG: LTF timer found inactive while scanner is running — restarting timer"
            )
            self._ltf_timer.start()

    def _trigger_scan(self) -> None:
        if self._any_scan_active:
            logger.debug("Scanner: a scan (HTF or LTF) is already active, skipping HTF trigger")
            return
        if self._worker and self._worker.isRunning():
            logger.debug("Scanner: previous HTF scan still running, skipping")
            return

        symbols = self._watchlist_mgr.get_active_symbols()
        if not symbols:
            logger.debug("Scanner: no active symbols in watchlist")
            return

        # Apply BTC-only filter if enabled
        if self._btc_only:
            symbols = [s for s in symbols if s.startswith("BTC")]
            if not symbols:
                symbols = ["BTC/USDT"]  # fallback guarantee

        from core.market_data.exchange_manager import exchange_manager
        exchange = exchange_manager.get_exchange()
        if exchange is None:
            logger.warning("Scanner: no exchange connected")
            return

        # Portfolio state — pulled from the active executor (paper or live)
        # so RiskGate has accurate position count, capital, and drawdown.
        from core.execution.order_router import order_router as _router
        _executor      = _router.active_executor
        open_positions = _executor.get_open_positions()
        capital        = _executor.available_capital
        drawdown_pct   = _executor.drawdown_pct

        self._any_scan_active = True
        self.scan_started.emit()
        logger.info(
            "AssetScanner: HTF scanning %d symbols on %s | capital=%.2f open=%d dd=%.2f%%",
            len(symbols), self._timeframe, capital, len(open_positions), drawdown_pct,
        )

        self._worker = ScanWorker(
            symbols        = symbols,
            timeframe      = self._timeframe,
            exchange       = exchange,
            open_positions = open_positions,
            capital_usdt   = capital,
            drawdown_pct   = drawdown_pct,
            hmm_models     = self._hmm_models,   # per-symbol HMM persistence
            prev_df_cache  = self._prev_df_cache, # previous-cycle DFs for ATR filter
            sig_gen        = self._sig_gen,       # shared SignalGenerator (preserves RL state)
            # v3: persistent objects for sub-second scan cycles
            ensemble_classifiers   = self._ensemble_classifiers,
            transition_controllers = self._transition_controllers,
            garch_models           = self._garch_models,
            ohlcv_cache            = self._ohlcv_cache,
        )
        self._worker.scan_complete.connect(self._on_scan_complete)
        self._worker.scan_error.connect(self._on_scan_error)
        self._worker.symbol_scanned.connect(self.symbol_progress)
        self._worker.df_cache_updated.connect(self._on_df_cache_updated)
        self._worker.scan_all_results.connect(self.scan_all_results)
        self._worker.scan_metrics_updated.connect(self.scan_metrics_updated)
        self._worker_started_at = time.time()
        self._worker.start()

    @Slot(object)
    def _on_df_cache_updated(self, df_cache: dict) -> None:
        """Store df_cache from current scan cycle for use as ATR filter input next cycle."""
        self._prev_df_cache = df_cache

    # ── Settings hot-apply ─────────────────────────────────
    @staticmethod
    def _make_risk_gate() -> RiskGate:
        """Build a RiskGate from current settings values."""
        from config.settings import settings as _s
        return RiskGate(
            max_concurrent_positions  = int(_s.get("risk.max_concurrent_positions", 3)),
            max_portfolio_drawdown_pct= float(_s.get("risk.max_portfolio_drawdown_pct", 15.0)),
            max_spread_pct            = float(_s.get("risk.max_spread_pct", 0.3)),
            min_risk_reward           = float(_s.get("risk.min_risk_reward", 1.3)),
        )

    def _on_settings_changed(self, event) -> None:
        """
        Rebuild the exposed _risk_gate from the new settings so the
        Risk Management page immediately shows updated limits.
        ScanWorker reads settings fresh on every scan cycle, so no
        additional action is needed for the pipeline itself.
        """
        try:
            self._risk_gate = self._make_risk_gate()
            # Re-check staged_candidates.enabled
            from config.settings import settings as _sc
            self._staged_enabled = bool(_sc.get("staged_candidates.enabled", True))
            logger.info(
                "AssetScanner: settings hot-applied — "
                "max_pos=%d  max_dd=%.1f%%  min_rr=%.2f  max_spread=%.2f%%  "
                "staged=%s",
                self._risk_gate.max_concurrent_positions,
                self._risk_gate.max_portfolio_drawdown_pct,
                self._risk_gate.min_risk_reward,
                self._risk_gate.max_spread_pct,
                self._staged_enabled,
            )
        except Exception as exc:
            logger.warning("AssetScanner: settings hot-apply failed: %s", exc)

    # ── LTF (15m) Confirmation Scan ───────────────────────────────

    def _trigger_ltf_scan(self) -> None:
        """Launch the LTF confirmation worker. Evaluates CREATED candidates."""
        if not self._staged_enabled:
            return
        if self._any_scan_active:
            logger.debug("Scanner: a scan is already active, skipping LTF trigger")
            return
        if self._ltf_worker and self._ltf_worker.isRunning():
            logger.debug("Scanner: previous LTF scan still running, skipping")
            return

        from core.market_data.exchange_manager import exchange_manager
        exchange = exchange_manager.get_exchange()
        if exchange is None:
            logger.warning("Scanner: no exchange connected — skipping LTF scan")
            return

        # Check if there are any CREATED candidates to evaluate.
        # Wrapped in try-except so a candidate-store failure does not escape
        # to the caller and leave _any_scan_active in an inconsistent state.
        try:
            from core.scanning.candidate_store import get_candidate_store
            store = get_candidate_store()
            created = store.get_created()
        except Exception as _store_exc:
            logger.error("AssetScanner: cannot access candidate store — skipping LTF scan: %s", _store_exc)
            return

        if not created:
            logger.debug("Scanner: no CREATED candidates — skipping LTF scan")
            return

        self._any_scan_active = True
        logger.info(
            "AssetScanner: LTF scan starting — %d CREATED candidate(s) to evaluate",
            len(created),
        )

        try:
            from core.scanning.ltf_scan_worker import LTFScanWorker
            from core.scanning.ltf_confirmation import LTFConfirmationConfig

            self._ltf_worker = LTFScanWorker(
                exchange=exchange,
                store=store,
                cfg=LTFConfirmationConfig.from_settings(),
            )
            self._ltf_worker.ltf_complete.connect(self._on_ltf_complete)
            self._ltf_worker.ltf_error.connect(self._on_ltf_error)
            self._ltf_worker_started_at = time.time()
            self._ltf_worker.start()
        except Exception as exc:
            # If worker creation/start fails (import error, config error, etc.),
            # release the lock so HTF scans are not permanently blocked.
            # Attempt a clean thread stop first in case start() raised AFTER
            # spawning the OS thread (extremely rare, but possible with QThread).
            if self._ltf_worker is not None:
                try:
                    self._ltf_worker.quit()
                    self._ltf_worker.wait(1000)
                except Exception:
                    pass
            self._any_scan_active = False
            self._ltf_worker = None
            self._ltf_worker_started_at = None
            logger.error("AssetScanner: LTF worker creation failed: %s", exc)

    @Slot(list)
    def _on_ltf_complete(self, confirmed_candidates: list) -> None:
        """Handle LTF scan completion. Emit confirmed_ready for execution pathway."""
        self._ltf_worker = None
        self._ltf_worker_started_at = None
        self._any_scan_active = False
        n = len(confirmed_candidates)
        logger.info("AssetScanner: LTF scan complete — %d CONFIRMED candidate(s)", n)
        self.ltf_scan_finished.emit(n)
        if confirmed_candidates:
            self.confirmed_ready.emit(confirmed_candidates)

    @Slot(str)
    def _on_ltf_error(self, err: str) -> None:
        """Handle LTF scan error."""
        self._ltf_worker = None
        self._ltf_worker_started_at = None
        self._any_scan_active = False
        logger.error("AssetScanner LTF error: %s", err)
        self.scan_error.emit(f"LTF: {err}")

    @Slot(list)
    def _on_scan_complete(self, candidates: list) -> None:
        # Release the worker reference immediately so the next timer tick is not
        # blocked by `self._worker.isRunning()`.  The ScanWorker thread may still
        # be alive doing post-scan work (CrashDetector / correlation updates), but
        # that work is fire-and-forget and does not affect scan correctness.
        self._worker = None
        self._worker_started_at = None
        self._any_scan_active = False
        self._last_scan_at = datetime.utcnow()
        # Reset watchdog kill counter — scan completed normally
        self._watchdog_consecutive_kills = 0
        self._watchdog_last_kill_at = None
        n = len(candidates)
        logger.info("AssetScanner: HTF scan complete — %d approved candidates", n)
        self.scan_finished.emit(n)
        # Notify event-bus subscribers (e.g. Dashboard status row) that a scan
        # cycle has completed.  SCAN_CYCLE_COMPLETE was defined in Topics but was
        # never published — the Dashboard's _on_scan_cycle_complete() callback
        # never fired, leaving the status row permanently at the startup-probe
        # value ("Stopped").  Published here so every completed cycle refreshes
        # the Dashboard regardless of whether there were any approved candidates.
        try:
            bus.publish(
                Topics.SCAN_CYCLE_COMPLETE,
                data={"candidates": n, "timestamp": self._last_scan_at.isoformat()},
                source="scanner",
            )
        except Exception:
            pass

        # ── Stage candidates into CandidateStore (Phase 4) ────────
        # When staged_candidates is enabled, approved HTF candidates are
        # placed into the CandidateStore as CREATED. They will NOT be
        # executed until the LTF confirmation scan promotes them to CONFIRMED.
        #
        # candidates_ready is ALWAYS emitted (for UI table display).
        # But execution only happens through confirmed_ready (LTF path).
        if candidates:
            self.candidates_ready.emit(candidates)
            bus.publish(
                Topics.SIGNAL_CONFIRMED,
                data={"candidates": candidates, "count": n},
                source="scanner",
            )

            if self._staged_enabled:
                try:
                    from core.scanning.candidate_store import get_candidate_store
                    store = get_candidate_store()
                    staged_count = 0
                    for c in candidates:
                        sc = store.create_or_refresh(c)
                        if sc and sc.is_active:
                            staged_count += 1
                    logger.info(
                        "AssetScanner: staged %d/%d candidates into CandidateStore",
                        staged_count, n,
                    )
                    # Trigger an immediate LTF scan after staging new candidates
                    # so confirmation doesn't wait up to 15 minutes.
                    if staged_count > 0:
                        logger.info("AssetScanner: triggering immediate LTF scan after HTF staging")
                        self._trigger_ltf_scan()
                except Exception as exc:
                    logger.error("AssetScanner: candidate staging failed: %s", exc, exc_info=True)

    @Slot(str)
    def _on_scan_error(self, err: str) -> None:
        # Also release the worker on error so the timer can retry next cycle.
        self._worker = None
        self._worker_started_at = None
        self._any_scan_active = False
        logger.error("AssetScanner HTF error: %s", err)
        self.scan_error.emit(err)


# ── Module-level singleton ────────────────────────────────
# Timeframe is read from config.yaml (data.default_timeframe); fallback 30m.
try:
    from config.settings import settings as _tf_s
    _scanner_tf = _tf_s.get("data.default_timeframe", "30m")
except Exception:
    _scanner_tf = "30m"
scanner = AssetScanner(timeframe=_scanner_tf)

# Apply startup configuration from settings
try:
    from config.settings import settings as _s

    if _s.get("scanner.btc_only_mode", False):
        scanner.set_btc_only(True)
except Exception:
    pass

# Initialize online RL trainer if enabled
try:
    from config.settings import settings as _s
    if _s.get("rl.enabled", False):
        from core.rl.online_trainer import OnlineRLTrainer
        from core.rl.rl_ensemble import RLEnsemble
        _rl_ensemble = RLEnsemble()
        rl_trainer = OnlineRLTrainer(rl_ensemble=_rl_ensemble)
        rl_trainer.start_training()
        logger.info("NexusTrader: Online RL trainer started")
    else:
        rl_trainer = None
except Exception as exc:
    logger.warning("NexusTrader: RL trainer startup failed: %s", exc)
    rl_trainer = None
