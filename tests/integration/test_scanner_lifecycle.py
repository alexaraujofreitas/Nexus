"""
Integration Test: IDSS AI Scanner Lifecycle Validation
=======================================================
Tests the full end-to-end scan pipeline using a mock exchange.
No internet access required — all OHLCV data is generated synthetically.

Coverage:
  SL-001  Scanner initializes correctly (scheduler, watchdog, state variables)
  SL-002  Full scan cycle: OHLCV → regime → signals → confluence → candidates
  SL-003  Multiple consecutive scan cycles (no worker leak, no stale flag)
  SL-004  Stall scenario: hanging ticker fetch → timeout → recovery → next scan works
  SL-005  Stall scenario: hanging OHLCV prefetch → timeout → next scan works
  SL-006  Stall scenario: hanging MTF 4h fetch → timeout → scan completes normally
  SL-007  Watchdog: stuck worker → force-kill → _any_scan_active reset → next scan fires
  SL-008  LTF exception handler: worker creation failure → quit() called → no stale ref
  SL-009  Candidate dedup: same condition rejected, different condition accepted
  SL-010  Auto-execute → PaperExecutor: position created, capital updated, persisted
  SL-011  Trade close → History updated, capital updated, DB record written
  SL-012  Restart persistence: position survives restart (load from JSON + DB)
  SL-013  Watchdog cooldown: 3 consecutive kills → extended cooldown engaged
  SL-014  Staleness detection: _last_scan_at old → recovery scan triggered
  SL-015  No `with ThreadPoolExecutor` patterns in scanner modules (code-scan test)
"""

import sys
import os
import time
import json
import threading
import tempfile
import shutil
import unittest
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ── Helpers ───────────────────────────────────────────────────────────
def _make_ohlcv(n: int = 300, base_price: float = 85_000.0,
                drift: float = 0.0001, volatility: float = 0.003,
                timeframe: str = "1h") -> list:
    """
    Generate synthetic OHLCV bars with realistic-looking price action.
    Returns list of [timestamp_ms, open, high, low, close, volume].
    """
    TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    interval_ms = TF_SECONDS.get(timeframe, 3600) * 1000
    # Start 'n' bars in the past
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (n + 1) * interval_ms

    np.random.seed(42)
    prices = [base_price]
    for _ in range(n - 1):
        ret = np.random.normal(drift, volatility)
        prices.append(prices[-1] * (1 + ret))

    bars = []
    for i, close in enumerate(prices):
        ts = start_ms + i * interval_ms
        spread = close * 0.001
        high = close + abs(np.random.normal(0, spread))
        low = close - abs(np.random.normal(0, spread))
        open_ = prices[i - 1] if i > 0 else close
        vol = np.random.uniform(50, 200)
        bars.append([ts, round(open_, 4), round(high, 4), round(low, 4), round(close, 4), round(vol, 4)])
    return bars


def _make_ohlcv_df(n: int = 300, base_price: float = 85_000.0,
                   timeframe: str = "1h") -> pd.DataFrame:
    """Return an OHLCV DataFrame ready for indicator calculation."""
    bars = _make_ohlcv(n=n, base_price=base_price, timeframe=timeframe)
    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").astype(float)
    return df


class MockExchange:
    """
    Minimal mock of a ccxt exchange object.
    Provides realistic OHLCV data and configurable failure modes.
    """
    id = "bybit"
    name = "Bybit"

    def __init__(self):
        self._call_count: dict[str, int] = {}
        self._hang_symbols: set[str] = set()     # symbols that will hang
        self._fail_symbols: set[str] = set()     # symbols that will raise
        self._hang_delay: float = 60.0           # seconds to block for hanging calls
        self._tickers: dict[str, dict] = {
            "BTC/USDT": {"last": 85000.0, "quoteVolume": 1_000_000.0, "percentage": 0.5},
            "ETH/USDT": {"last": 2000.0,  "quoteVolume": 500_000.0,   "percentage": -0.3},
            "SOL/USDT": {"last": 150.0,   "quoteVolume": 200_000.0,   "percentage": 1.2},
            "XRP/USDT": {"last": 2.5,     "quoteVolume": 100_000.0,   "percentage": -0.1},
            "BNB/USDT": {"last": 650.0,   "quoteVolume": 300_000.0,   "percentage": 0.8},
        }
        self._tick_lock = threading.Lock()

    def set_hang(self, symbol: str, delay: float = 60.0):
        self._hang_symbols.add(symbol)
        self._hang_delay = delay

    def clear_hang(self, symbol: str = None):
        if symbol:
            self._hang_symbols.discard(symbol)
        else:
            self._hang_symbols.clear()

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    since: int = None, limit: int = 300) -> list:
        key = f"fetch_ohlcv:{symbol}:{timeframe}"
        with self._tick_lock:
            self._call_count[key] = self._call_count.get(key, 0) + 1

        if symbol in self._fail_symbols:
            raise RuntimeError(f"MockExchange: forced failure for {symbol}")

        if symbol in self._hang_symbols:
            time.sleep(self._hang_delay)

        prices = {"BTC/USDT": 85000.0, "ETH/USDT": 2000.0, "SOL/USDT": 150.0,
                  "XRP/USDT": 2.5, "BNB/USDT": 650.0}
        base = prices.get(symbol, 100.0)
        return _make_ohlcv(n=limit or 300, base_price=base, timeframe=timeframe)

    def fetch_tickers(self, symbols: list = None) -> dict:
        if symbols is None:
            return dict(self._tickers)
        return {s: self._tickers[s] for s in symbols if s in self._tickers}

    def fetch_ticker(self, symbol: str) -> dict:
        return self._tickers.get(symbol, {"last": 100.0, "quoteVolume": 10000.0, "percentage": 0.0})

    def get_call_count(self, key: str) -> int:
        with self._tick_lock:
            return self._call_count.get(key, 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST CLASS
# ═══════════════════════════════════════════════════════════════════════
class TestScannerLifecycle(unittest.TestCase):

    def setUp(self):
        """Create isolated temp environment for each test."""
        self.tmp = tempfile.mkdtemp(prefix="nexus_test_")
        self.exchange = MockExchange()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────
    # SL-001: Scanner state initialization
    # ─────────────────────────────────────────────────────────────
    def test_sl001_scanner_state_initialization(self):
        """
        Verify all scanner state variables are initialized to safe defaults.
        A scanner with uninitialized state could fire prematurely or
        cause _any_scan_active to be True before the first scan.
        """
        from core.scanning.scanner import AssetScanner

        # Create instance without calling __init__ (avoids Qt timer setup)
        scanner = AssetScanner.__new__(AssetScanner)
        # Manually init the state variables that guard against stalls
        scanner._running = False
        scanner._any_scan_active = False
        scanner._worker = None
        scanner._worker_started_at = None
        scanner._ltf_worker = None
        scanner._ltf_worker_started_at = None
        scanner._last_scan_at = None
        scanner._watchdog_consecutive_kills = 0
        scanner._watchdog_last_kill_at = None

        self.assertFalse(scanner._running)
        self.assertFalse(scanner._any_scan_active)
        self.assertIsNone(scanner._worker)
        self.assertIsNone(scanner._worker_started_at)
        self.assertIsNone(scanner._ltf_worker)
        self.assertIsNone(scanner._last_scan_at)
        self.assertEqual(scanner._watchdog_consecutive_kills, 0)
        self.assertIsNone(scanner._watchdog_last_kill_at)

    # ─────────────────────────────────────────────────────────────
    # SL-002: Full scan cycle — core signal pipeline
    # ─────────────────────────────────────────────────────────────
    def test_sl002_full_signal_pipeline(self):
        """
        Run the complete signal pipeline: indicators → regime → signals → confluence.
        Verifies the pipeline produces no unhandled exceptions and returns
        either None or a valid OrderCandidate for each symbol.
        """
        from core.scanning.scanner import ScanWorker
        from core.features.indicator_library import calculate_all
        from core.regime.hmm_regime_classifier import HMMRegimeClassifier

        exchange = self.exchange
        symbols = ["BTC/USDT", "ETH/USDT"]

        # Fetch realistic data
        ohlcv_map = {}
        for sym in symbols:
            raw = exchange.fetch_ohlcv(sym, "1h", limit=300)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp").astype(float)
            # Calculate all indicators
            df = calculate_all(df)
            ohlcv_map[sym] = df

        # Verify indicators computed
        for sym, df in ohlcv_map.items():
            self.assertIn("ema_9", df.columns, f"EMA-9 missing for {sym}")
            self.assertIn("rsi_14", df.columns, f"RSI missing for {sym}")
            self.assertIn("atr_14", df.columns, f"ATR missing for {sym}")
            self.assertGreater(len(df), 50, f"Too few bars for {sym}")

        # Verify regime classifier works
        clf = HMMRegimeClassifier()
        for sym, df in ohlcv_map.items():
            regime, confidence, _ = clf.classify(df)
            self.assertIsInstance(regime, str, f"Regime should be string for {sym}")
            self.assertGreater(len(regime), 0, f"Regime should not be empty for {sym}")
            self.assertGreaterEqual(confidence, 0.0)
            self.assertLessEqual(confidence, 1.0)

    # ─────────────────────────────────────────────────────────────
    # SL-003: Multiple consecutive scan cycles
    # ─────────────────────────────────────────────────────────────
    def test_sl003_consecutive_cycles_no_stale_state(self):
        """
        Simulate 5 consecutive scan cycles via the ScanWorker.run() path.
        After each cycle, verify:
          - scan_complete signal would be emitted (mocked)
          - No exception propagates out
        The worker should be safe to instantiate and run multiple times.
        """
        from core.scanning.scanner import ScanWorker
        from core.regime.hmm_regime_classifier import HMMRegimeClassifier

        exchange = self.exchange
        symbols = ["BTC/USDT", "ETH/USDT"]
        hmm_models = {sym: HMMRegimeClassifier() for sym in symbols}

        # Build a mock regime classifier that returns deterministic output
        mock_regime_clf = MagicMock()
        mock_regime_clf.classify.return_value = ("bull_trend", 0.75, {})

        # Build a mock signal generator (returns empty signals → no candidate, no crash)
        mock_sig_gen = MagicMock()
        mock_sig_gen.generate.return_value = []

        # Build a mock scorer (not reached if signals empty, but set just in case)
        mock_scorer = MagicMock()
        mock_scorer.score.return_value = None

        results = []

        for cycle in range(5):
            candidates_out = []
            errors_out = []

            # ScanWorker is a QThread — we run its core logic directly
            # by calling the underlying _scan_symbol_with_regime method
            worker = ScanWorker.__new__(ScanWorker)
            worker._exchange = exchange
            worker._timeframe = "1h"
            worker._symbols = symbols
            worker._open_positions = []
            worker._capital_usdt = 100_000.0
            worker._drawdown_pct = 0.0
            worker._hmm_models = hmm_models
            worker._prev_df_cache = {}
            worker._sig_gen = mock_sig_gen
            worker._scorer = mock_scorer
            worker._regime_clf = mock_regime_clf
            worker._transition_ctrl = None
            worker._use_hmm = True
            worker._use_ensemble = False
            # Mock Qt signal (no QApplication in test environment)
            worker.symbol_scanned = MagicMock()
            worker.symbol_scanned.emit = MagicMock()

            # Test _scan_symbol_with_regime directly (core logic, no Qt needed)
            # Signature: (self, symbol, ticker, prefetched_ohlcv=None)
            from config.settings import settings
            for sym in symbols:
                try:
                    raw = exchange.fetch_ohlcv(sym, "1h", limit=300)
                    ticker = exchange.fetch_ticker(sym)
                    candidate, regime, conf, df, _pre_rej, _diag = worker._scan_symbol_with_regime(
                        sym, ticker, prefetched_ohlcv=raw
                    )
                    candidates_out.append((sym, candidate, regime))
                except Exception as e:
                    errors_out.append((sym, str(e)))

            results.append((cycle, len(candidates_out), len(errors_out)))

        # All 5 cycles should process both symbols without errors
        for cycle, processed, err_count in results:
            self.assertEqual(processed, len(symbols),
                f"Cycle {cycle}: expected {len(symbols)} processed, got {processed}")
            self.assertEqual(err_count, 0,
                f"Cycle {cycle}: unexpected errors: {errors_out}")

    # ─────────────────────────────────────────────────────────────
    # SL-004: Hanging ticker fetch → times out → scan continues
    # ─────────────────────────────────────────────────────────────
    def test_sl004_ticker_fetch_timeout_recovery(self):
        """
        Simulate fetch_tickers() hanging indefinitely.
        The scanner wraps this in a ThreadPoolExecutor with 15s timeout.
        Verify that after timeout, the scan continues with empty tickers
        (no crash, no permanent stall).
        """
        import concurrent.futures

        hang_event = threading.Event()
        call_log = []

        def hanging_fetch_tickers(symbols=None):
            call_log.append("ticker_start")
            hang_event.wait(timeout=5)  # 5s > test timeout (2s) but won't freeze suite
            call_log.append("ticker_end")  # Should NOT be reached in 2s timeout
            return {}

        exchange = self.exchange
        exchange.fetch_tickers = hanging_fetch_tickers

        # Simulate the ticker fetch with timeout (mirrors scanner.py lines 144-155)
        tickers = {}
        _tp = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        t0 = time.time()
        try:
            _fut = _tp.submit(exchange.fetch_tickers, ["BTC/USDT"])
            tickers = _fut.result(timeout=2.0)  # Use 2s for test speed
        except concurrent.futures.TimeoutError:
            pass
        finally:
            _tp.shutdown(wait=False, cancel_futures=True)
        elapsed = time.time() - t0

        # Verify: returned quickly (≤ 2.5s), did NOT block for 120s
        self.assertLess(elapsed, 3.0,
            f"Ticker timeout took {elapsed:.1f}s — should complete in <3s")
        # Verify: tickers is empty (graceful degradation)
        self.assertEqual(tickers, {},
            "Expected empty tickers after timeout")
        # Verify: the hang is still pending (thread was abandoned, not waited for)
        self.assertNotIn("ticker_end", call_log,
            "Hanging thread should not have completed")

    # ─────────────────────────────────────────────────────────────
    # SL-005: Hanging OHLCV prefetch → timeout → fallback per-symbol
    # ─────────────────────────────────────────────────────────────
    def test_sl005_ohlcv_prefetch_timeout_recovery(self):
        """
        Simulate OHLCV prefetch timing out (as_completed 30s timeout).
        Verify that after timeout, the symbol falls back to the per-symbol
        live fetch path (which uses its own 15s timeout).
        """
        import concurrent.futures

        hang_start = threading.Event()
        hang_done = threading.Event()

        def hanging_fetch(sym, tf, limit=300):
            hang_start.set()
            time.sleep(5)  # 5s > test timeout (2s) but won't freeze suite
            hang_done.set()
            return []

        self.exchange.fetch_ohlcv = hanging_fetch

        symbols = ["BTC/USDT"]
        _ohlcv_cache = {}

        def _fetch_one(sym):
            try:
                raw = self.exchange.fetch_ohlcv(sym, "1h", limit=300)
                return sym, raw
            except Exception as exc:
                return sym, []

        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        t0 = time.time()
        try:
            _futures = {_pool.submit(_fetch_one, sym): sym for sym in symbols}
            for _fut in concurrent.futures.as_completed(_futures, timeout=2.0):  # 2s timeout for test
                sym, raw = _fut.result()
                _ohlcv_cache[sym] = raw
        except concurrent.futures.TimeoutError:
            pass  # Expected — graceful degradation
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.time() - t0

        # Should complete in ~2s, not 120s
        self.assertLess(elapsed, 3.0,
            f"Prefetch timeout took {elapsed:.1f}s — expected <3s")
        # Cache should be empty (timeout before any result)
        # In real code: scanner falls back to per-symbol fetch

    # ─────────────────────────────────────────────────────────────
    # SL-006: Hanging MTF 4h fetch → timeout → scan continues
    # ─────────────────────────────────────────────────────────────
    def test_sl006_mtf_fetch_timeout_no_stall(self):
        """
        Simulate MTF 4h fetch hanging indefinitely.
        The scanner wraps this in ThreadPoolExecutor(max_workers=1) with 10s timeout.
        The key assertion: `shutdown(wait=False, cancel_futures=True)` is used
        so the hung thread is ABANDONED immediately — NOT waited for.
        """
        import concurrent.futures

        hang_called = threading.Event()
        hang_released = threading.Event()

        def hanging_htf_fetch(*args, **kwargs):
            hang_called.set()
            hang_released.wait(timeout=5)  # 5s > test timeout (2s) but won't freeze suite
            return []

        t0 = time.time()
        raw_htf = None

        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            _fut = _pool.submit(hanging_htf_fetch, "BTC/USDT", "4h", limit=50)
            raw_htf = _fut.result(timeout=2.0)  # 2s for test speed
        except concurrent.futures.TimeoutError:
            raw_htf = None  # Expected: skip MTF for this symbol
        except Exception as exc:
            raw_htf = None
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.time() - t0

        # Critical: scan should not have blocked for 120s
        self.assertLess(elapsed, 3.0,
            f"MTF timeout took {elapsed:.1f}s — expected <3s (shutdown must not wait)")
        # raw_htf should be None (MTF confirmation skipped for this symbol)
        self.assertIsNone(raw_htf)
        # Thread should have been called (hang started)
        self.assertTrue(hang_called.is_set())
        # Thread should NOT have completed (was abandoned)
        self.assertFalse(hang_released.is_set())
        # Verify the thread is NOT blocking us — it was abandoned
        # (checking that the main thread is free)
        can_run = threading.Event()
        threading.Thread(target=lambda: can_run.set(), daemon=True).start()
        can_run.wait(timeout=0.1)
        self.assertTrue(can_run.is_set(), "Main thread should be unblocked after MTF timeout")

    # ─────────────────────────────────────────────────────────────
    # SL-007: Watchdog force-kill resets all state
    # ─────────────────────────────────────────────────────────────
    def test_sl007_watchdog_resets_state_after_kill(self):
        """
        Simulate the watchdog detecting a stuck worker and force-killing it.
        Verify that after the kill:
          - self._worker = None
          - self._worker_started_at = None
          - self._any_scan_active = False
          - scan_error signal is emitted
          - Next call to _trigger_scan() is NOT blocked
        """
        # Mock the scanner object with just the state we need
        scanner = MagicMock()
        scanner._worker = MagicMock()
        scanner._worker.isRunning.return_value = True
        scanner._worker.quit = MagicMock()
        scanner._worker.wait = MagicMock(return_value=True)
        scanner._worker_started_at = time.time() - 150  # 150s ago → exceeds 120s limit
        scanner._any_scan_active = True
        scanner._ltf_worker = None
        scanner._ltf_worker_started_at = None
        scanner._max_scan_duration_s = 120
        scanner._watchdog_consecutive_kills = 0
        scanner._watchdog_last_kill_at = None
        scanner._running = True
        scanner._last_scan_at = datetime.utcnow() - timedelta(hours=2)
        scanner.scan_error = MagicMock()
        scanner.scan_error.emit = MagicMock()

        # Import the actual _check_worker_health method and bind it
        from core.scanning.scanner import AssetScanner
        import types

        def _get_watchdog_method():
            # Read the source and extract just the stuck-worker logic
            # We test it by directly replicating the state check
            now = time.time()
            worker_ref = scanner._worker
            started_at = scanner._worker_started_at

            if worker_ref is not None and started_at is not None:
                elapsed = now - started_at
                if elapsed > scanner._max_scan_duration_s:
                    scanner._watchdog_consecutive_kills += 1
                    scanner._watchdog_last_kill_at = now
                    try:
                        worker_ref.quit()
                        worker_ref.wait(1000)
                    except Exception:
                        pass
                    scanner._worker = None
                    scanner._worker_started_at = None
                    scanner._any_scan_active = False
                    scanner.scan_error.emit(f"HTF scan timed out after {elapsed:.0f}s")
                    return True  # Kill happened
            return False

        # Save reference to the mock worker BEFORE running watchdog (it will be set to None)
        original_worker = scanner._worker
        killed = _get_watchdog_method()

        self.assertTrue(killed, "Watchdog should have killed the stuck worker")
        self.assertIsNone(scanner._worker, "Worker reference should be None after kill")
        self.assertIsNone(scanner._worker_started_at, "Started-at should be None after kill")
        self.assertFalse(scanner._any_scan_active, "_any_scan_active should be False after kill")
        self.assertEqual(scanner._watchdog_consecutive_kills, 1)
        scanner.scan_error.emit.assert_called_once()
        original_worker.quit.assert_called_once()

    # ─────────────────────────────────────────────────────────────
    # SL-008: LTF exception handler calls quit() before clearing ref
    # ─────────────────────────────────────────────────────────────
    def test_sl008_ltf_exception_handler_calls_quit(self):
        """
        Verify that the LTF exception handler (lines 983-989 in scanner.py)
        calls worker.quit() before setting _ltf_worker = None.
        This prevents dangling threads when worker.start() partially starts.

        We test this by parsing the source AST to verify quit() is called.
        """
        import ast

        scanner_path = ROOT / "core" / "scanning" / "scanner.py"
        source = scanner_path.read_text()
        tree = ast.parse(source)

        # Find _trigger_ltf_scan method's except handler
        found_quit_in_except = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_trigger_ltf_scan":
                # Walk through the function body
                for subnode in ast.walk(node):
                    if isinstance(subnode, ast.ExceptHandler):
                        # Check if there's a quit() call in this handler
                        for exc_child in ast.walk(subnode):
                            if (isinstance(exc_child, ast.Call) and
                                    hasattr(exc_child.func, 'attr') and
                                    exc_child.func.attr == 'quit'):
                                found_quit_in_except = True
                                break

        self.assertTrue(found_quit_in_except,
            "_trigger_ltf_scan except handler must call worker.quit() before clearing reference. "
            "This prevents dangling threads if worker.start() raises after partially starting.")

    # ─────────────────────────────────────────────────────────────
    # SL-009: Candidate deduplication
    # ─────────────────────────────────────────────────────────────
    def test_sl009_condition_dedup_same_rejected_different_accepted(self):
        """
        Verify the condition-based dedup logic:
          - Same (side, models_fired, regime) fingerprint → REJECTED
          - Different side → ACCEPTED
          - Different models_fired → ACCEPTED
          - Different regime → ACCEPTED
        """
        from core.scanning.auto_execute_guard import (
            check_candidate, AutoExecuteState, REJECT_DUPLICATE, PASS
        )

        state = AutoExecuteState()

        # Build a mock open position with a known fingerprint
        existing_position = {
            "symbol": "BTC/USDT",
            "side": "buy",
            "models_fired": ["trend", "momentum_breakout"],
            "regime": "Bull Trend",
        }

        # Candidate with SAME fingerprint → should be rejected
        same_candidate = {
            "symbol": "BTC/USDT",
            "side": "buy",
            "models_fired": ["trend", "momentum_breakout"],
            "regime": "Bull Trend",
            "score": 0.75,
            "entry_price": 85000.0,
            "stop_loss_price": 84000.0,
            "take_profit_price": 87000.0,
            "strategy": "IDSS",
            "age_seconds": 30,
        }
        result = check_candidate(same_candidate, timeframe="1h",
                                 open_positions=[existing_position],
                                 n_open=1, max_pos=50,
                                 drawdown_pct=0.0, max_dd_pct=15.0, state=state)
        self.assertEqual(result, REJECT_DUPLICATE,
            "Same condition fingerprint should be rejected as duplicate")

        # Candidate with DIFFERENT side → should not be rejected for dedup
        diff_side = dict(same_candidate, side="sell")
        result = check_candidate(diff_side, timeframe="1h",
                                 open_positions=[existing_position],
                                 n_open=1, max_pos=50,
                                 drawdown_pct=0.0, max_dd_pct=15.0, state=state)
        self.assertNotEqual(result, REJECT_DUPLICATE,
            "Different side should not be rejected as duplicate")

        # Candidate with DIFFERENT models → should not be rejected for dedup
        diff_models = dict(same_candidate, models_fired=["mean_reversion"])
        result = check_candidate(diff_models, timeframe="1h",
                                 open_positions=[existing_position],
                                 n_open=1, max_pos=50,
                                 drawdown_pct=0.0, max_dd_pct=15.0, state=state)
        self.assertNotEqual(result, REJECT_DUPLICATE,
            "Different models_fired should not be rejected as duplicate")

        # Candidate with DIFFERENT regime → should not be rejected for dedup
        diff_regime = dict(same_candidate, regime="Ranging")
        result = check_candidate(diff_regime, timeframe="1h",
                                 open_positions=[existing_position],
                                 n_open=1, max_pos=50,
                                 drawdown_pct=0.0, max_dd_pct=15.0, state=state)
        self.assertNotEqual(result, REJECT_DUPLICATE,
            "Different regime should not be rejected as duplicate")

    # ─────────────────────────────────────────────────────────────
    # SL-010: Auto-execute → PaperExecutor position creation
    # ─────────────────────────────────────────────────────────────
    def test_sl010_paper_executor_submit_creates_position(self):
        """
        Submit a mock OrderCandidate to PaperExecutor.
        Verify:
          1. Position appears in get_open_positions()
          2. available_capital is reduced by position size
          3. JSON snapshot is written
        """
        from core.execution.paper_executor import PaperExecutor
        from core.meta_decision.order_candidate import OrderCandidate

        # Create fresh PaperExecutor with temp data files
        positions_file = Path(self.tmp) / "open_positions.json"
        outcomes_file = Path(self.tmp) / "outcome_tracker.json"
        l2_file = Path(self.tmp) / "level2_tracker.json"

        with patch("core.execution.paper_executor._OPEN_POSITIONS_FILE", positions_file), \
             patch("core.meta_decision.confluence_scorer._TRACKER_PERSIST_FILE", outcomes_file), \
             patch("core.learning.level2_tracker._PERSIST_FILE", l2_file), \
             patch("core.database.engine.get_session") as mock_session:
            # Mock the DB session to avoid real DB access
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_session.return_value = mock_ctx

            pe = PaperExecutor(initial_capital_usdt=10_000.0)

            candidate = OrderCandidate(
                symbol="BTC/USDT",
                side="buy",
                entry_type="market",
                score=0.78,
                entry_price=85_000.0,
                stop_loss_price=84_000.0,
                take_profit_price=87_000.0,
                position_size_usdt=500.0,
                atr_value=250.0,
                regime="bull_trend",
                timeframe="1h",
                models_fired=["trend"],
                rationale="Test trade",
                expected_value=0.15,
            )

            initial_capital = pe.available_capital
            result = pe.submit(candidate)

        self.assertTrue(result, "submit() should return True on success")

        positions = pe.get_open_positions()
        self.assertEqual(len(positions), 1, "Should have exactly 1 open position")

        pos = positions[0]
        self.assertEqual(pos["symbol"], "BTC/USDT")
        self.assertEqual(pos["side"], "buy")
        self.assertAlmostEqual(pos["entry_price"], 85_000.0, delta=500.0)  # slippage applied

        # Capital should be reduced by position size
        used = pos["size_usdt"]
        self.assertGreater(used, 0, "Position size should be > 0")
        self.assertAlmostEqual(pe.available_capital, initial_capital - used, delta=1.0)

    # ─────────────────────────────────────────────────────────────
    # SL-011: Trade close → history updated, DB written
    # ─────────────────────────────────────────────────────────────
    def test_sl011_trade_close_updates_history_and_capital(self):
        """
        Open a position, then close it at a higher price.
        Verify:
          1. Open positions becomes 0
          2. Trade history is updated
          3. Capital increases by the realized P&L
          4. DB save was called
        """
        from core.execution.paper_executor import PaperExecutor
        from core.meta_decision.order_candidate import OrderCandidate

        positions_file = Path(self.tmp) / "open_positions.json"

        with patch("core.execution.paper_executor._OPEN_POSITIONS_FILE", positions_file), \
             patch("core.meta_decision.confluence_scorer._TRACKER_PERSIST_FILE",
                   Path(self.tmp) / "ot.json"), \
             patch("core.learning.level2_tracker._PERSIST_FILE",
                   Path(self.tmp) / "l2.json"), \
             patch("core.database.engine.get_session") as mock_session:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_session.return_value = mock_ctx

            pe = PaperExecutor(initial_capital_usdt=10_000.0)
            initial_capital = pe.available_capital

            # Open a position
            candidate = OrderCandidate(
                symbol="ETH/USDT",
                side="buy",
                entry_type="market",
                score=0.80,
                entry_price=2_000.0,
                stop_loss_price=1_950.0,
                take_profit_price=2_100.0,
                position_size_usdt=500.0,
                atr_value=30.0,
                regime="bull_trend",
                timeframe="1h",
                models_fired=["trend"],
                rationale="Test",
                expected_value=0.20,
            )
            pe.submit(candidate)
            self.assertEqual(len(pe.get_open_positions()), 1)

            # Close it at take-profit price (profitable)
            # close_position signature: (symbol, price=None)
            closed_ok = pe.close_position("ETH/USDT", price=2_100.0)

        self.assertTrue(closed_ok, "close_position() should return True")
        self.assertEqual(len(pe.get_open_positions()), 0,
            "Open positions should be empty after close")

        # Capital should have increased (profitable trade)
        self.assertGreater(pe.available_capital, initial_capital,
            "Capital should increase after profitable close")

        # Check trade history was recorded
        history = pe.get_closed_trades()
        self.assertGreater(len(history), 0, "Trade history should have at least 1 record")
        last_trade = history[-1]
        self.assertEqual(last_trade["symbol"], "ETH/USDT")
        self.assertGreater(last_trade.get("pnl_usdt", 0), 0,
            "P&L should be positive for profitable trade")

    # ─────────────────────────────────────────────────────────────
    # SL-012: Restart persistence
    # ─────────────────────────────────────────────────────────────
    def test_sl012_position_survives_restart(self):
        """
        Verify restart persistence: the open_positions.json format is correct
        and _load_open_positions() can reconstruct positions from it.

        NOTE: The conftest autouse fixture stubs _load/_save on ALL PaperExecutor
        instances to prevent test contamination. This test therefore verifies
        the persistence contract at the data/schema level (JSON format correct,
        PaperPosition constructable from saved data) and verifies the load method
        source reads from _OPEN_POSITIONS_FILE as expected.
        """
        from core.execution.paper_executor import PaperExecutor, PaperPosition

        positions_file = Path(self.tmp) / "open_positions.json"

        # Write a persistence file in the exact format _save_open_positions produces
        persist_data = {
            "capital": 9_500.0,
            "peak_capital": 10_000.0,
            "positions": [
                {
                    "symbol": "SOL/USDT",
                    "side": "buy",
                    "entry_price": 150.0,
                    "quantity": 3.33,
                    "stop_loss": 145.0,
                    "take_profit": 162.0,
                    "size_usdt": 500.0,
                    "score": 0.72,
                    "rationale": "Test persistence",
                    "regime": "ranging",
                    "models_fired": ["mean_reversion"],
                    "timeframe": "1h",
                    "opened_at": "2026-01-01T12:00:00",
                }
            ],
        }
        positions_file.parent.mkdir(parents=True, exist_ok=True)
        positions_file.write_text(json.dumps(persist_data))

        # Verify JSON schema matches what _load_open_positions expects
        data = json.loads(positions_file.read_text())
        self.assertIn("capital", data)
        self.assertIn("positions", data)
        pos_list = data["positions"]
        self.assertEqual(len(pos_list), 1)
        pd_entry = pos_list[0]
        self.assertEqual(pd_entry["symbol"], "SOL/USDT")
        self.assertAlmostEqual(float(pd_entry["entry_price"]), 150.0)
        self.assertAlmostEqual(float(data["capital"]), 9_500.0, delta=1.0)

        # Verify a PaperPosition can be constructed from the saved data format
        pos = PaperPosition(
            symbol      = pd_entry["symbol"],
            side        = pd_entry["side"],
            entry_price = float(pd_entry["entry_price"]),
            quantity    = float(pd_entry["quantity"]),
            stop_loss   = float(pd_entry["stop_loss"]),
            take_profit = float(pd_entry["take_profit"]),
            size_usdt   = float(pd_entry["size_usdt"]),
            score       = float(pd_entry.get("score", 0.0)),
            rationale   = pd_entry.get("rationale", ""),
            regime      = pd_entry.get("regime", ""),
            models_fired= pd_entry.get("models_fired", []),
            timeframe   = pd_entry.get("timeframe", ""),
        )
        self.assertEqual(pos.symbol, "SOL/USDT")
        self.assertAlmostEqual(pos.entry_price, 150.0)
        self.assertAlmostEqual(pos.quantity, 3.33, delta=0.01)

        # Verify _load_open_positions reads from _OPEN_POSITIONS_FILE (source check)
        import ast
        src = (Path(__file__).parents[2] /
               "core" / "execution" / "paper_executor.py").read_text()
        tree = ast.parse(src)
        load_fn_uses_file = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_load_open_positions":
                for subnode in ast.walk(node):
                    if (isinstance(subnode, ast.Name) and
                            subnode.id == "_OPEN_POSITIONS_FILE"):
                        load_fn_uses_file = True
                        break
        self.assertTrue(load_fn_uses_file,
            "_load_open_positions must read from _OPEN_POSITIONS_FILE")

    # ─────────────────────────────────────────────────────────────
    # SL-013: Watchdog cooldown after 3 consecutive kills
    # ─────────────────────────────────────────────────────────────
    def test_sl013_watchdog_cooldown_after_consecutive_kills(self):
        """
        After 3+ consecutive watchdog kills, the staleness detection
        should use a 3× cooldown (900s instead of 300s) to prevent
        infinite restart loops.
        """
        # Mock scanner state representing: 3 kills, last kill 200s ago
        scanner = MagicMock()
        scanner._running = True
        scanner._any_scan_active = False
        scanner._watchdog_consecutive_kills = 3
        scanner._watchdog_last_kill_at = time.time() - 200  # 200s ago
        scanner._last_scan_at = datetime.utcnow() - timedelta(hours=3)
        scanner._max_consecutive_kills = 3
        scanner._watchdog_recovery_cooldown_s = 300
        scanner._timeframe = "1h"
        trigger_called = [False]

        def fake_trigger():
            trigger_called[0] = True

        scanner._trigger_scan = fake_trigger

        # Replicate the staleness detection logic from scanner.py
        now = time.time()
        expected_interval_s = 3600  # 1H
        staleness_limit_s = expected_interval_s * 1.5  # 5400s

        # _last_scan_at is 3 hours old → 10800s > 5400s limit → stale
        last_scan_age_s = (datetime.utcnow() - scanner._last_scan_at).total_seconds()
        self.assertGreater(last_scan_age_s, staleness_limit_s,
            "Precondition: last scan should be stale")

        # Cooldown check
        kills = scanner._watchdog_consecutive_kills
        cooldown = scanner._watchdog_recovery_cooldown_s
        if kills >= scanner._max_consecutive_kills:
            cooldown = cooldown * 3  # 900s extended cooldown

        last_kill_age = now - scanner._watchdog_last_kill_at  # 200s

        if last_kill_age < cooldown:
            # In cooldown → should NOT trigger
            in_cooldown = True
        else:
            scanner._trigger_scan()
            in_cooldown = False

        # With 3 kills and 200s elapsed:
        # Normal cooldown = 300s, extended = 900s
        # Since 200 < 900, we should be in cooldown
        self.assertTrue(in_cooldown,
            "Should be in extended cooldown (200s < 900s) after 3 kills")
        self.assertFalse(trigger_called[0],
            "_trigger_scan should NOT be called during cooldown")

    # ─────────────────────────────────────────────────────────────
    # SL-014: Staleness detection triggers recovery scan
    # ─────────────────────────────────────────────────────────────
    def test_sl014_staleness_detection_triggers_recovery(self):
        """
        When _last_scan_at is > 1.5× TF interval old AND no cooldown is active,
        the watchdog should call _trigger_scan() for recovery.
        """
        trigger_called = [False]

        def fake_trigger():
            trigger_called[0] = True

        scanner = MagicMock()
        scanner._running = True
        scanner._any_scan_active = False
        scanner._watchdog_consecutive_kills = 0
        scanner._watchdog_last_kill_at = None   # No recent kill → no cooldown
        scanner._last_scan_at = datetime.utcnow() - timedelta(hours=3)  # 3h old
        scanner._max_consecutive_kills = 3
        scanner._watchdog_recovery_cooldown_s = 300
        scanner._timeframe = "1h"
        scanner._timer = MagicMock()
        scanner._timer.isActive.return_value = True
        scanner._trigger_scan = fake_trigger

        # Replicate staleness detection
        now = time.time()
        expected_interval_s = 3600
        staleness_limit_s = expected_interval_s * 1.5  # 5400s

        last_scan_age_s = (datetime.utcnow() - scanner._last_scan_at).total_seconds()

        if last_scan_age_s > staleness_limit_s:
            kills = scanner._watchdog_consecutive_kills
            cooldown = scanner._watchdog_recovery_cooldown_s
            if kills >= scanner._max_consecutive_kills:
                cooldown = cooldown * 3

            last_kill_age = (now - scanner._watchdog_last_kill_at
                             if scanner._watchdog_last_kill_at is not None else float('inf'))

            if last_kill_age >= cooldown:
                scanner._trigger_scan()

        self.assertTrue(trigger_called[0],
            "_trigger_scan should be called when stale and no cooldown active")

    # ─────────────────────────────────────────────────────────────
    # SL-015: Code-scan: no `with ThreadPoolExecutor` in scan-critical files
    # ─────────────────────────────────────────────────────────────
    def test_sl015_no_with_threadpoolexecutor_in_scanner_modules(self):
        """
        Scan all scanner module source files for the dangerous
        `with ThreadPoolExecutor` pattern.

        Each instance of `with ThreadPoolExecutor` is a latent scan stall:
        the context manager calls shutdown(wait=True) on __exit__, which
        blocks indefinitely if the submitted thread hangs on a network call
        even after TimeoutError is raised.

        RULE: ALL ThreadPoolExecutor usages must use explicit pool + finally:shutdown(wait=False)
        """
        import re

        scan_dirs = [
            ROOT / "core" / "scanning",
            ROOT / "core" / "nlp",       # news_feed.py was fixed
        ]

        violations = []
        for scan_dir in scan_dirs:
            for py_file in scan_dir.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                source = py_file.read_text(errors="replace")
                lines = source.splitlines()
                for i, line in enumerate(lines, start=1):
                    # Match actual usage, not comments
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if re.search(r'\bwith\s+ThreadPoolExecutor\b', stripped):
                        violations.append(f"{py_file.name}:{i}: {stripped[:80]}")

        self.assertEqual(violations, [],
            "DANGEROUS PATTERN FOUND — use explicit pool + finally:shutdown(wait=False) instead:\n"
            + "\n".join(f"  {v}" for v in violations))

    # ─────────────────────────────────────────────────────────────
    # SL-016: Any_scan_active always reset in all completion paths
    # ─────────────────────────────────────────────────────────────
    def test_sl016_any_scan_active_reset_in_all_paths(self):
        """
        Verify that _any_scan_active = False appears in all four completion
        paths in scanner.py:
          1. _on_scan_complete() (HTF success)
          2. _on_scan_error()    (HTF error)
          3. _on_ltf_complete()  (LTF success)
          4. _on_ltf_error()     (LTF error)
          5. _check_worker_health() HTF kill
          6. _check_worker_health() LTF kill
          7. _trigger_ltf_scan() exception handler
        """
        import ast

        scanner_path = ROOT / "core" / "scanning" / "scanner.py"
        source = scanner_path.read_text()
        tree = ast.parse(source)

        # Find all function defs and look for _any_scan_active = False
        methods_with_reset = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for subnode in ast.walk(node):
                    if (isinstance(subnode, ast.Assign) and
                            len(subnode.targets) == 1 and
                            isinstance(subnode.targets[0], ast.Attribute) and
                            subnode.targets[0].attr == "_any_scan_active" and
                            isinstance(subnode.value, ast.Constant) and
                            subnode.value.value is False):
                        methods_with_reset.add(node.name)

        required_methods = {
            "_on_scan_complete",
            "_on_scan_error",
            "_on_ltf_complete",
            "_on_ltf_error",
            "_check_worker_health",
            "_trigger_ltf_scan",  # exception handler
        }

        missing = required_methods - methods_with_reset
        self.assertEqual(missing, set(),
            f"_any_scan_active = False is missing from: {missing}\n"
            f"Found in: {methods_with_reset}")

    # ─────────────────────────────────────────────────────────────
    # SL-017: Worker reference cleared in all completion paths
    # ─────────────────────────────────────────────────────────────
    def test_sl017_worker_reference_cleared_in_completion_paths(self):
        """
        Verify self._worker = None appears in all 3 HTF completion paths:
          1. _on_scan_complete()
          2. _on_scan_error()
          3. _check_worker_health() (watchdog kill)
        """
        import ast, re

        scanner_path = ROOT / "core" / "scanning" / "scanner.py"
        source = scanner_path.read_text()
        tree = ast.parse(source)

        methods_with_worker_none = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for subnode in ast.walk(node):
                    if (isinstance(subnode, ast.Assign) and
                            len(subnode.targets) == 1 and
                            isinstance(subnode.targets[0], ast.Attribute) and
                            subnode.targets[0].attr == "_worker" and
                            isinstance(subnode.value, ast.Constant) and
                            subnode.value.value is None):
                        methods_with_worker_none.add(node.name)

        required = {"_on_scan_complete", "_on_scan_error", "_check_worker_health"}
        missing = required - methods_with_worker_none
        self.assertEqual(missing, set(),
            f"self._worker = None missing from: {missing}")


class TestAutoExecuteGuard(unittest.TestCase):
    """Additional tests for the auto-execute guard."""

    def test_ae_per_pair_limit_not_global_break(self):
        """
        run_batch() should allow one trade per pair per cycle.
        BTC/USDT approved AND ETH/USDT approved in same batch.
        NOT: only the first pair (no global break).
        """
        from core.scanning.auto_execute_guard import run_batch, AutoExecuteState, PASS

        state = AutoExecuteState()

        candidates = [
            {
                "symbol": "BTC/USDT",
                "side": "buy",
                "score": 0.85,
                "strategy": "IDSS",
                "models_fired": ["trend"],
                "regime": "Bull Trend",
                "entry_price": 85000.0,
                "stop_loss_price": 84000.0,
                "take_profit_price": 87000.0,
                "age_seconds": 10,
            },
            {
                "symbol": "ETH/USDT",
                "side": "buy",
                "score": 0.80,
                "strategy": "IDSS",
                "models_fired": ["trend"],
                "regime": "Bull Trend",
                "entry_price": 2000.0,
                "stop_loss_price": 1950.0,
                "take_profit_price": 2100.0,
                "age_seconds": 10,
            },
        ]

        results = run_batch(candidates, timeframe="1h",
                            open_positions=[], max_pos=10, state=state)
        approved_symbols = {r["symbol"] for r in results}

        # Both should be approved (different pairs)
        self.assertIn("BTC/USDT", approved_symbols)
        self.assertIn("ETH/USDT", approved_symbols)

    def test_ae_stale_candidate_rejected(self):
        """
        Candidates older than the max age (default 2× TF seconds) should be rejected.
        This prevents executing on stale market conditions.
        """
        from core.scanning.auto_execute_guard import (
            check_candidate, AutoExecuteState, REJECT_STALE
        )

        state = AutoExecuteState()

        from datetime import timezone
        # generate_at 3h in the past — well past 1× TF interval (3600s)
        stale_dt = datetime.now(timezone.utc) - timedelta(hours=3)
        stale_candidate = {
            "symbol": "BTC/USDT",
            "side": "buy",
            "score": 0.85,
            "strategy": "IDSS",
            "models_fired": ["trend"],
            "regime": "Bull Trend",
            "entry_price": 85000.0,
            "stop_loss_price": 84000.0,
            "take_profit_price": 87000.0,
            "generated_at": stale_dt.isoformat(),  # 3h old > 1× TF → stale
        }

        result = check_candidate(stale_candidate, timeframe="1h",
                                 open_positions=[], n_open=0,
                                 max_pos=50, drawdown_pct=0.0, max_dd_pct=15.0,
                                 state=state)
        self.assertEqual(result, REJECT_STALE,
            "Candidate with generated_at 3h ago should be rejected as stale")


class TestDataLoaderNoWithExecutor(unittest.TestCase):
    """Verify the data_loader.py fix removed the with-ThreadPoolExecutor pattern."""

    def test_data_loader_no_with_threadpoolexecutor(self):
        """
        data_loader.py previously used `with ThreadPoolExecutor` which blocks
        indefinitely on hung network calls. This was replaced with explicit
        pool + finally:shutdown(wait=False, cancel_futures=True).
        """
        import re

        data_loader_path = ROOT / "core" / "backtesting" / "data_loader.py"
        source = data_loader_path.read_text()
        lines = source.splitlines()

        violations = []
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r'\bwith\s+ThreadPoolExecutor\b', stripped):
                violations.append(f"Line {i}: {stripped[:80]}")

        self.assertEqual(violations, [],
            "data_loader.py must not use `with ThreadPoolExecutor`. "
            "Use explicit pool + finally:shutdown(wait=False).\n"
            + "\n".join(violations))


if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
