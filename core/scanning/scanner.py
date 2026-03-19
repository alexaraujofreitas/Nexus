# ============================================================
# NEXUS TRADER — Asset Scanner
#
# Orchestrates the full IDSS scan cycle:
#   1. Get active symbols from WatchlistManager
#   2. Apply UniverseFilter (liquidity, spread, ATR)
#   3. For each qualifying symbol:
#      a. Fetch recent candles
#      b. Calculate indicators
#      c. Classify regime
#      d. Run SignalGenerator
#      e. Score with ConfluenceScorer
#      f. Validate with RiskGate
#   4. Emit events for approved OrderCandidates
#
# Runs on a QTimer (default: every primary_tf minutes on-close
# approximation). Thread-safe: scan runs in a QThread worker.
# ============================================================
from __future__ import annotations

import concurrent.futures
import logging
import time
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
from core.features.indicator_library import calculate_all
from core.scanning.closed_candle_guard import enforce_closed_candles
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Timeframe → approximate poll interval in seconds
TF_POLL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "1d": 86400,
}


class ScanWorker(QThread):
    """
    Runs one full scan cycle in a background thread.
    Emits results when done.
    """
    scan_complete    = Signal(list)  # list of OrderCandidate dicts
    scan_error       = Signal(str)
    symbol_scanned   = Signal(str, str, float)  # symbol, regime, score (0 if no signal)
    df_cache_updated = Signal(object)            # dict[symbol, DataFrame] — for ATR filter next cycle

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

    def run(self):
        try:
            all_candidates: list[OrderCandidate] = []
            # Track regimes seen this cycle so we can broadcast the majority
            _regime_votes: dict[str, int] = {}
            _regime_confs: dict[str, float] = {}

            # ── Fetch tickers for spread/volume filter ─────────
            # IMPORTANT: Do NOT use `with ThreadPoolExecutor` here.
            # The context manager calls shutdown(wait=True) on exit, which blocks
            # indefinitely if the thread hangs. Use explicit pool + finally shutdown.
            tickers = {}
            _tp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = _tp.submit(self._exchange.fetch_tickers, self._symbols)
                tickers = _fut.result(timeout=15.0) or {}
            except concurrent.futures.TimeoutError:
                logger.warning("Scanner: fetch_tickers timed out after 15s — continuing with empty tickers")
            except Exception as exc:
                logger.warning("Scanner: ticker fetch failed: %s", exc)
            finally:
                _tp.shutdown(wait=False, cancel_futures=True)

            # ── Apply universe filter ──────────────────────────
            # Pass the previous cycle's indicator DataFrames so the ATR range
            # filter can reject excessively volatile or illiquid symbols.
            qualifying = self._univ_filter.apply(
                self._symbols, tickers, feature_dfs=self._prev_df_cache or None
            )
            if not qualifying:
                self.scan_complete.emit([])
                return

            # ── Concurrent OHLCV pre-fetch ─────────────────────
            # Fetch candles for ALL qualifying symbols in parallel to overlap
            # network I/O. Processing (indicators, regime, signals) stays
            # sequential to avoid any thread-safety issues with shared objects.
            from config.settings import settings as _sc
            _ohlcv_limit = int(_sc.get("scanner.ohlcv_bars", 300))
            _ohlcv_cache: dict[str, list] = {}

            def _fetch_one(sym: str) -> tuple[str, list]:
                _t0 = time.time()
                try:
                    raw = self._exchange.fetch_ohlcv(
                        sym, self._timeframe, limit=_ohlcv_limit
                    )
                    raw, _dropped = enforce_closed_candles(
                        raw, self._timeframe, log_symbol=sym,
                    )
                    _elapsed_ms = (time.time() - _t0) * 1000
                    _latest_ts = raw[-1][0] if raw else 0
                    logger.debug(
                        "Scanner: fetch %s OK — %d bars, %.0fms, latest_ts=%s",
                        sym, len(raw), _elapsed_ms,
                        datetime.utcfromtimestamp(_latest_ts / 1000).strftime("%H:%M") if _latest_ts else "N/A",
                    )
                    return sym, raw
                except Exception as _exc:
                    _elapsed_ms = (time.time() - _t0) * 1000
                    logger.warning("Scanner: prefetch FAILED for %s after %.0fms: %s", sym, _elapsed_ms, _exc)
                    return sym, []

            # IMPORTANT: Do NOT use `with ThreadPoolExecutor` here.
            # shutdown(wait=True) in __exit__ blocks forever if any fetch hangs.
            _pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(qualifying), 8)
            )
            try:
                _futures = {_pool.submit(_fetch_one, sym): sym for sym in qualifying}
                for _fut in concurrent.futures.as_completed(_futures, timeout=30):
                    try:
                        sym, raw = _fut.result()
                        _ohlcv_cache[sym] = raw
                    except Exception as _exc:
                        sym = _futures[_fut]
                        logger.warning("Scanner: prefetch result error for %s: %s", sym, _exc)
                        _ohlcv_cache[sym] = []
            except concurrent.futures.TimeoutError:
                logger.warning("Scanner: concurrent OHLCV prefetch timed out after 30s — will fetch per-symbol")
            except Exception as _exc:
                logger.warning("Scanner: concurrent OHLCV prefetch failed: %s — will fetch per-symbol", _exc)
            finally:
                _pool.shutdown(wait=False, cancel_futures=True)

            # ── Prefetch summary ─────────────────────────────────
            _ok = [s for s, d in _ohlcv_cache.items() if d and len(d) >= 30]
            _fail = [s for s in qualifying if s not in _ok]
            logger.info(
                "Scanner: OHLCV prefetch complete — %d/%d symbols OK%s",
                len(_ok), len(qualifying),
                f", failed/short: {_fail}" if _fail else "",
            )

            # ── Scan each qualifying symbol ────────────────────
            # df_cache stores the fully-computed indicator DataFrame for each symbol.
            # This eliminates the duplicate OHLCV fetch that previously occurred
            # when CrashDetector re-fetched data at the end of the scan loop.
            df_cache: dict[str, pd.DataFrame] = {}
            for _sym_idx, symbol in enumerate(qualifying):
                logger.debug("Scanner: === BEGIN symbol %d/%d: %s ===", _sym_idx + 1, len(qualifying), symbol)
                _sym_start = time.time()
                try:
                    candidate, regime, confidence, df = self._scan_symbol_with_regime(
                        symbol, tickers.get(symbol, {}),
                        prefetched_ohlcv=_ohlcv_cache.get(symbol),
                    )
                    logger.debug("Scanner: === END symbol %d/%d: %s (%.1fs) candidate=%s ===",
                                 _sym_idx + 1, len(qualifying), symbol,
                                 time.time() - _sym_start, candidate is not None)
                    if df is not None:
                        df_cache[symbol] = df
                    if regime:
                        _regime_votes[regime] = _regime_votes.get(regime, 0) + 1
                        # Keep the highest-confidence reading for each regime
                        if confidence > _regime_confs.get(regime, 0.0):
                            _regime_confs[regime] = confidence
                    if candidate:
                        all_candidates.append(candidate)
                except Exception as exc:
                    logger.error("Scanner: error scanning %s: %s", symbol, exc, exc_info=True)

            # ── Risk gate (batch) ──────────────────────────────
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
                logger.info(
                    "Scanner: rejected %s — %s", r.symbol, r.rejection_reason
                )

            self.scan_complete.emit([c.to_dict() for c in approved])

            # ── Update CrashDetector with latest scan data ────────────────
            # Use df_cache built during the main scan loop — no duplicate OHLCV fetch.
            try:
                from core.risk.crash_detector import get_crash_detector
                _cd = get_crash_detector()
                _cd.evaluate(tickers, df_cache)
            except Exception as exc:
                logger.debug("Scanner: CrashDetector update failed: %s", exc)

            # ── Update live rolling correlations ──────────────────────────
            # Compute pairwise correlations from recent returns in df_cache and
            # update the CorrelationController so RiskGate uses live data instead
            # of the static pre-computed correlation matrix.
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

            # ── Emit df_cache for next-cycle ATR filtering ────────────────
            # AssetScanner stores this as _prev_df_cache and passes it to the
            # next ScanWorker instance so the UniverseFilter can apply the ATR
            # range filter without waiting for a second OHLCV fetch.
            self.df_cache_updated.emit(df_cache)

            # ── Publish majority regime from this scan cycle ───
            if _regime_votes:
                dominant_regime = max(_regime_votes, key=_regime_votes.__getitem__)
                dom_confidence  = _regime_confs.get(dominant_regime, 0.5)
                # Build flat probs from vote counts
                total_votes = sum(_regime_votes.values())
                regime_probs = {r: v / total_votes for r, v in _regime_votes.items()}
                bus.publish(
                    Topics.REGIME_CHANGED,
                    {
                        "new_regime":   dominant_regime,
                        "confidence":   dom_confidence,
                        "regime_probs": regime_probs,
                        "classifier":   "rule-based (scanner)",
                        "symbol_count": len(_regime_votes),
                    },
                    source="scanner",
                )
                logger.debug(
                    "Scanner: dominant regime=%s (conf=%.2f, votes=%d)",
                    dominant_regime, dom_confidence, _regime_votes[dominant_regime],
                )

        except Exception as exc:
            logger.error("ScanWorker fatal error: %s", exc, exc_info=True)
            self.scan_error.emit(str(exc))

    def _scan_symbol_with_regime(
        self, symbol: str, ticker: dict,
        prefetched_ohlcv: Optional[list] = None,
    ) -> tuple[Optional[OrderCandidate], str, float, Optional[pd.DataFrame]]:
        """
        Run the full pipeline for one symbol.
        Returns (candidate_or_None, regime_label, confidence, df_or_None).
        The DataFrame is returned so the caller can cache it for CrashDetector,
        eliminating the duplicate OHLCV fetch that previously occurred.

        If prefetched_ohlcv is provided (from the concurrent pre-fetch block in
        ScanWorker.run()), the internal fetch_ohlcv call is skipped entirely.
        If it is None or empty, we fall back to a live fetch so standalone usage
        and edge-case symbols still work correctly.
        """
        # Use pre-fetched OHLCV when available; fall back to live fetch otherwise.
        # The fallback is wrapped with a timeout so a hanging exchange call
        # cannot permanently stall the ScanWorker thread.
        if prefetched_ohlcv is not None and len(prefetched_ohlcv) >= 30:
            raw = prefetched_ohlcv
        else:
            from config.settings import settings as _sc
            limit = int(_sc.get("scanner.ohlcv_bars", 300))
            # IMPORTANT: Do NOT use `with ThreadPoolExecutor` here.
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
            return None, "", 0.0, None

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
                return None, "", 0.0, None
        except Exception as _freshness_err:
            logger.debug("Scanner: freshness check failed for %s: %s", symbol, _freshness_err)

        # Calculate indicators
        df = calculate_all(df)

        # Regime classification
        regime, confidence, features = self._regime_clf.classify(df)
        logger.debug("Scanner: %s regime=%s (conf=%.2f)", symbol, regime, confidence)

        # Apply transition controller for hysteresis
        if self._transition_ctrl is not None:
            confirmed_regime, in_transition, blend_weight = self._transition_ctrl.update(
                regime, confidence, features
            )
            if in_transition:
                logger.debug("Scanner: %s regime transition in progress (blend=%.2f)", symbol, blend_weight)
            regime = confirmed_regime

        # MS-GARCH volatility forecast
        try:
            from config.settings import settings as _s
            if _s.get("ms_garch.enabled", True):
                from core.regime.ms_garch_forecaster import ms_garch
                garch_forecast = ms_garch.forecast(df, horizon=3)
                regime = ms_garch.get_regime_adjustment(regime, garch_forecast)
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
                from config.settings import settings as _sc
                from core.regime.hmm_regime_classifier import HMMRegimeClassifier
                if _sc.get("hmm_regime.enabled", True):
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

        # Emit symbol progress
        self.symbol_scanned.emit(symbol, regime, 0.0)

        # Signal generation with regime probabilities
        signals = self._sig_gen.generate(symbol, df, regime, self._timeframe, regime_probs=regime_probs)
        if not signals:
            return None, regime, confidence, df

        # Confluence scoring with regime probabilities
        candidate = self._scorer.score(signals, symbol, regime_probs=regime_probs)
        logger.debug("Scanner: %s — scorer returned candidate=%s", symbol, candidate is not None)
        if not candidate:
            return None, regime, confidence, df

        logger.debug("Scanner: %s — candidate score=%.3f, about to enter MTF block", symbol, candidate.score)

        # ── Multi-timeframe confirmation ──────────────────────────────
        from config.settings import settings as _sc
        logger.debug("Scanner: %s — entering MTF confirmation check (candidate=%s, mtf_enabled=%s)",
                     symbol, candidate is not None, _sc.get("multi_tf.confirmation_required", False))
        if candidate and _sc.get("multi_tf.confirmation_required", False):
            try:
                tf_map = {"1m": "5m", "3m": "15m", "5m": "15m", "15m": "1h",
                          "30m": "1h", "1h": "4h", "2h": "4h", "4h": "1d",
                          "6h": "1d", "12h": "1d", "1d": "1w"}
                higher_tf = tf_map.get(self._timeframe)
                if higher_tf:
                    # Wrap with timeout so a hanging exchange call cannot
                    # permanently stall the ScanWorker thread.
                    # IMPORTANT: Do NOT use `with ThreadPoolExecutor` here.
                    # The context manager calls shutdown(wait=True) on exit,
                    # which blocks forever if the submitted thread is hung on
                    # a network call — even after TimeoutError is raised.
                    # Instead, use shutdown(wait=False, cancel_futures=True)
                    # to abandon the hung thread immediately.
                    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        _fut = _pool.submit(
                            self._exchange.fetch_ohlcv, symbol, higher_tf, limit=50
                        )
                        raw_htf = _fut.result(timeout=10.0)
                    except concurrent.futures.TimeoutError:
                        logger.warning(
                            "Scanner: MTF 4h fetch TIMED OUT for %s (%s) after 10s "
                            "— skipping MTF confirmation for this symbol "
                            "(Bybit Demo 4h endpoint may be unresponsive)",
                            symbol, higher_tf,
                        )
                        raw_htf = None
                    except Exception as _mtf_exc:
                        logger.warning(
                            "Scanner: MTF 4h fetch FAILED for %s (%s): %s "
                            "— skipping MTF confirmation for this symbol",
                            symbol, higher_tf, _mtf_exc,
                        )
                        raw_htf = None
                    finally:
                        _pool.shutdown(wait=False, cancel_futures=True)
                    if raw_htf and len(raw_htf) >= 20:
                        raw_htf, _htf_dropped = enforce_closed_candles(
                            raw_htf, higher_tf, log_symbol=f"{symbol}/MTF",
                        )
                    if raw_htf and len(raw_htf) >= 20:
                        df_htf = pd.DataFrame(raw_htf, columns=["timestamp", "open", "high", "low", "close", "volume"])
                        df_htf["timestamp"] = pd.to_datetime(df_htf["timestamp"], unit="ms", utc=True)
                        df_htf = df_htf.set_index("timestamp").astype(float)
                        df_htf = calculate_all(df_htf)
                        htf_regime, _, _ = self._regime_clf.classify(df_htf)
                        candidate.higher_tf_regime = htf_regime
                        logger.debug("Scanner: %s higher-TF (%s) regime=%s", symbol, higher_tf, htf_regime)
            except Exception as exc:
                logger.debug("Scanner: MTF fetch failed for %s: %s", symbol, exc)

        if candidate:
            logger.debug("Scanner: %s — emitting symbol_scanned signal (score=%.3f)", symbol, candidate.score)
            self.symbol_scanned.emit(symbol, regime, candidate.score)

        logger.debug("Scanner: %s — _scan_symbol_with_regime RETURNING (candidate=%s)", symbol, candidate is not None)
        return candidate, regime, confidence, df


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
    candidates_ready = Signal(list)    # list of OrderCandidate dicts (UI display only)
    confirmed_ready  = Signal(list)    # list of CONFIRMED candidate dicts (execution trigger)
    scan_started     = Signal()
    scan_finished    = Signal(int)     # n candidates found (HTF)
    ltf_scan_finished = Signal(int)    # n confirmed (LTF)
    scan_error       = Signal(str)
    symbol_progress  = Signal(str, str, float)  # symbol, regime, score

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

        # Indicator DataFrames from the most recent scan cycle, passed to the
        # next ScanWorker so the UniverseFilter can apply the ATR range filter.
        self._prev_df_cache: dict = {}

        # Timestamp of the most recently completed scan cycle (UTC).
        # Exposed so the health-check notification can report "Last scan: X min ago".
        self._last_scan_at: Optional[datetime] = None

        # Timestamp when the current scan worker was launched (UTC).
        # Used by the watchdog to detect stuck workers.
        self._worker_started_at: Optional[float] = None

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
        logger.info("AssetScanner started (TF=%s, staged=%s)", self._timeframe, self._staged_enabled)
        self._trigger_scan()   # immediate first scan
        self._timer.start()
        # Start LTF timer if staged candidates are enabled
        if self._staged_enabled:
            self._ltf_timer.start()
            logger.info("AssetScanner: LTF confirmation timer started (15m interval)")

    def stop(self) -> None:
        self._running = False
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

        # ── 0. Thread count monitoring ─────────────────────────────
        thread_count = _threading.active_count()
        if thread_count > 50:
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
        if self._running and not self._timer.isActive():
            logger.warning(
                "Scanner WATCHDOG: HTF timer found inactive while scanner is running — restarting timer"
            )
            self._timer.start()
        if self._running and self._staged_enabled and not self._ltf_timer.isActive():
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
        )
        self._worker.scan_complete.connect(self._on_scan_complete)
        self._worker.scan_error.connect(self._on_scan_error)
        self._worker.symbol_scanned.connect(self.symbol_progress)
        self._worker.df_cache_updated.connect(self._on_df_cache_updated)
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

        # Check if there are any CREATED candidates to evaluate
        from core.scanning.candidate_store import get_candidate_store
        store = get_candidate_store()
        created = store.get_created()
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
            # If worker creation fails (import error, config error, etc.),
            # release the lock so HTF scans are not permanently blocked.
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
scanner = AssetScanner()

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
