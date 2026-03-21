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

# Default buffer (seconds) added after candle close before scanning.
# 30s gives the exchange time to finalize and serve the closed bar.
_CANDLE_CLOSE_BUFFER_S: int = 30


def _seconds_to_next_candle(timeframe: str, buffer_s: int = _CANDLE_CLOSE_BUFFER_S) -> int:
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
        try:
            all_candidates: list[OrderCandidate] = []
            # Track regimes seen this cycle so we can broadcast the majority
            _regime_votes: dict[str, int] = {}
            _regime_confs: dict[str, float] = {}
            # Per-symbol results for scan_all_results signal (all symbols, not just approved)
            _all_sym_results: dict[str, dict] = {}

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
            # Seed results for symbols that didn't pass the universe filter
            for _sym in self._symbols:
                if _sym not in qualifying:
                    _all_sym_results[_sym] = self._empty_sym_result(_sym, "Filtered")
            if not qualifying:
                self.scan_complete.emit([])
                self.scan_all_results.emit(list(_all_sym_results.values()))
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
                    candidate, regime, confidence, df, pre_rejection, sym_diag = self._scan_symbol_with_regime(
                        symbol, tickers.get(symbol, {}),
                        prefetched_ohlcv=_ohlcv_cache.get(symbol),
                    )
                    logger.debug("Scanner: === END symbol %d/%d: %s (%.1fs) candidate=%s pre_rejection=%r ===",
                                 _sym_idx + 1, len(qualifying), symbol,
                                 time.time() - _sym_start, candidate is not None, pre_rejection)
                    if df is not None:
                        df_cache[symbol] = df
                    if regime:
                        _regime_votes[regime] = _regime_votes.get(regime, 0) + 1
                        # Keep the highest-confidence reading for each regime
                        if confidence > _regime_confs.get(regime, 0.0):
                            _regime_confs[regime] = confidence
                    if candidate:
                        all_candidates.append(candidate)
                        # Pre-populate result; status will be finalized after risk gate
                        _all_sym_results[symbol] = {
                            **candidate.to_dict(),
                            "status":      "pending",
                            "is_approved": False,
                            "diagnostics": sym_diag,
                        }
                    else:
                        _r = self._empty_sym_result(symbol, pre_rejection or "No signal", regime)
                        _r["diagnostics"] = sym_diag
                        _all_sym_results[symbol] = _r
                except Exception as exc:
                    logger.error("Scanner: error scanning %s: %s", symbol, exc, exc_info=True)
                    _r = self._empty_sym_result(symbol, "Scan error")
                    _r["diagnostics"] = {}
                    _all_sym_results[symbol] = _r

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
                if r.symbol in _all_sym_results:
                    _all_sym_results[r.symbol]["status"]           = r.rejection_reason or "Rejected"
                    _all_sym_results[r.symbol]["is_approved"]      = False
                    _all_sym_results[r.symbol]["rejection_reason"] = r.rejection_reason or "Rejected"

            _scan_ts = datetime.utcnow().isoformat()
            for c in approved:
                if c.symbol in _all_sym_results:
                    _all_sym_results[c.symbol]["status"]      = "approved"
                    _all_sym_results[c.symbol]["is_approved"] = True
                    # Stamp generated_at so the Age column ticks for approved rows
                    if not _all_sym_results[c.symbol].get("generated_at"):
                        _all_sym_results[c.symbol]["generated_at"] = _scan_ts

            # Stamp scan time for all remaining rows so Age column works everywhere
            for _sym, _row in _all_sym_results.items():
                if not _row.get("generated_at"):
                    _row["generated_at"] = _scan_ts

            self.scan_complete.emit([c.to_dict() for c in approved])
            self.scan_all_results.emit(list(_all_sym_results.values()))

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
    ) -> tuple[Optional[OrderCandidate], str, float, Optional[pd.DataFrame], str, dict]:
        """
        Run the full pipeline for one symbol.
        Returns (candidate_or_None, regime_label, confidence, df_or_None, pre_rejection, diagnostics).

        pre_rejection is a human-readable string explaining why no candidate was produced:
          ""                — candidate produced (may still be rejected by the risk gate)
          "No data"         — insufficient or stale OHLCV bars
          "No signal"       — no sub-model fired for this symbol
          "Below threshold" — signals fired but confluence score < threshold

        diagnostics is a dict with pipeline transparency data for the rationale panel:
          regime_confidence, regime_probs, candle_age_s, candle_count, candle_ts_str,
          models_fired, models_disabled, models_no_signal, all_model_names,
          raw_score, effective_threshold, per_model, direction_split, dominant_side, etc.
        """
        # Accumulated diagnostics for the rationale panel — populated progressively
        # as the pipeline runs and returned at every exit point.
        _sym_diag: dict = {}

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

        # Calculate indicators
        df = calculate_all(df)

        # ── Phase 1 pre-scan filters (time-of-day, volatility) ─────────
        try:
            from core.filters.trade_filters import apply_pre_scan_filters
            _pf_ok, _pf_reason = apply_pre_scan_filters(symbol, df, self._timeframe)
            if not _pf_ok:
                logger.debug("Scanner: %s pre-filter REJECTED — %s", symbol, _pf_reason)
                return symbol, None, None, None, _pf_reason, {}
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

        # ── Regime diagnostics for rationale panel ────────────────────
        _sym_diag["regime_confidence"] = round(confidence, 3)
        _sym_diag["regime_probs"]      = regime_probs

        # Signal generation with regime probabilities
        signals = self._sig_gen.generate(symbol, df, regime, self._timeframe, regime_probs=regime_probs)

        # ── Model-level diagnostics for rationale panel ───────────────
        try:
            from config.settings import settings as _sc_d
            _disabled_names = list(_sc_d.get("disabled_models", []))
        except Exception:
            _disabled_names = []
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
        candidate = self._scorer.score(signals, symbol, regime_probs=regime_probs)
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
        """Log the frozen production configuration at startup for auditability."""
        from config.settings import settings as _s
        disabled  = _s.get("disabled_models", [])
        threshold = _s.get("idss.min_confluence_score", 0.20)
        dyn_on    = _s.get("dynamic_confluence.enabled", False)
        time_f    = _s.get("filters.time_of_day.enabled", False)
        heat      = _s.get("risk_engine.portfolio_heat_max_pct", 0.04) * 100
        sz_mode   = _s.get("risk_engine.sizing_mode", "risk_based")
        risk_pct  = _s.get("risk_engine.risk_pct_per_trade", 0.75)
        mtf_on    = _s.get("multi_tf.confirmation_required", True)
        ae_on     = _s.get("scanner.auto_execute", True)
        logger.info(
            "═══════════════════════════════════════════════════════════\n"
            "  NEXUS TRADER — PRODUCTION CONFIGURATION (FROZEN)\n"
            "  Strategy        : MomentumBreakout (TrendModel gate)\n"
            "  Disabled models : %s\n"
            "  Confluence      : %.2f (dynamic=%s)\n"
            "  Time filter     : %s\n"
            "  Portfolio heat  : %.0f%% max\n"
            "  Sizing mode     : %s (%.2f%% risk/trade)\n"
            "  MTF confirm     : %s\n"
            "  Auto-execute    : %s\n"
            "  Circuit breaker : 10%% drawdown hard stop\n"
            "═══════════════════════════════════════════════════════════",
            disabled, threshold, dyn_on, time_f, heat,
            sz_mode, risk_pct, mtf_on, ae_on,
        )

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
        )
        self._worker.scan_complete.connect(self._on_scan_complete)
        self._worker.scan_error.connect(self._on_scan_error)
        self._worker.symbol_scanned.connect(self.symbol_progress)
        self._worker.df_cache_updated.connect(self._on_df_cache_updated)
        self._worker.scan_all_results.connect(self.scan_all_results)
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
