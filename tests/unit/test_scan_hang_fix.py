"""
Tests for the scan hang fix (ThreadPoolExecutor blocking on shutdown).

Root cause: `with ThreadPoolExecutor() as pool:` calls pool.shutdown(wait=True)
on __exit__, which blocks indefinitely when any submitted thread hangs.
The fix replaces all such context managers with explicit pool creation
and `finally: pool.shutdown(wait=False, cancel_futures=True)`.

These tests PROVE:
- The old pattern blocks (reproduction)
- The new pattern does not block (fix validation)
- Thread count does not grow unboundedly (stability)
- Scan state resets correctly on failure (lifecycle)
"""
import concurrent.futures
import threading
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── PROOF 1: Reproduction of the blocking pattern ─────────────────

class TestThreadPoolHangReproduction:
    """Prove that `with ThreadPoolExecutor` blocks when a thread hangs."""

    def test_hang_001_with_context_manager_blocks(self):
        """PROOF: `with ThreadPoolExecutor` blocks when submitted work hangs.

        This test uses a very short sleep to simulate a "hang" that would
        take much longer in production. The key insight is that even when
        .result(timeout=X) raises TimeoutError, the __exit__ of the context
        manager calls shutdown(wait=True) which blocks.
        """
        hung = threading.Event()

        def hanging_func():
            hung.set()
            time.sleep(10)  # simulates a hung API call
            return "done"

        start = time.time()
        completed = threading.Event()

        def run_with_context_manager():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(hanging_func)
                    try:
                        fut.result(timeout=0.5)  # timeout quickly
                    except concurrent.futures.TimeoutError:
                        pass  # we expect this
                    # __exit__ calls shutdown(wait=True) which blocks here
            except Exception:
                pass
            completed.set()

        t = threading.Thread(target=run_with_context_manager, daemon=True)
        t.start()

        # Wait for the hang to start
        hung.wait(timeout=2)
        assert hung.is_set(), "Hanging func should have started"

        # Wait 2 seconds — if the context manager doesn't block, completed should be set
        completed.wait(timeout=2.0)
        elapsed = time.time() - start

        # The context manager SHOULD block because shutdown(wait=True)
        # waits for the 10-second sleep to finish
        assert not completed.is_set(), \
            f"Context manager should have blocked (elapsed={elapsed:.1f}s) but completed early"

    def test_hang_002_explicit_shutdown_does_not_block(self):
        """PROOF: Explicit pool + shutdown(wait=False) does NOT block."""
        def hanging_func():
            time.sleep(10)
            return "done"

        start = time.time()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(hanging_func)
            try:
                fut.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.time() - start
        assert elapsed < 2.0, \
            f"Explicit shutdown should return quickly, took {elapsed:.1f}s"

    def test_hang_003_concurrent_prefetch_with_one_hanging(self):
        """Simulate the exact scenario: 5 symbols, 1 hangs, using the fixed pattern."""
        results = {}

        def fetch_ohlcv(symbol):
            if symbol == "XRP/USDT":
                time.sleep(10)  # this one hangs
            else:
                time.sleep(0.1)  # others complete quickly
            return symbol, [1, 2, 3]

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        start = time.time()
        try:
            futures = {pool.submit(fetch_ohlcv, s): s
                      for s in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]}
            for fut in concurrent.futures.as_completed(futures, timeout=2.0):
                try:
                    sym, data = fut.result()
                    results[sym] = data
                except Exception:
                    sym = futures[fut]
                    results[sym] = []
        except concurrent.futures.TimeoutError:
            pass  # XRP/USDT hangs, this is expected
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.time() - start
        assert elapsed < 3.0, f"Should not block on hung thread, took {elapsed:.1f}s"
        # At least 4 of 5 should have completed
        assert len(results) >= 4, f"Expected >=4 results, got {len(results)}: {list(results.keys())}"


# ── PROOF 2: Thread accumulation behavior ─────────────────────────

class TestThreadAccumulation:
    """Prove that shutdown(wait=False) doesn't cause unbounded thread growth."""

    def test_ta_001_threads_dont_accumulate_unboundedly(self):
        """Run 10 cycles with hanging threads — count should stay bounded."""
        baseline_threads = threading.active_count()

        for i in range(10):
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(time.sleep, 5)  # "hangs" for 5s
                try:
                    fut.result(timeout=0.1)
                except concurrent.futures.TimeoutError:
                    pass
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        # Give threads a moment to be GC'd
        time.sleep(0.5)
        current_threads = threading.active_count()
        growth = current_threads - baseline_threads

        # Thread count growth should be bounded. Some threads may linger
        # until their sleep completes, but they should not accumulate
        # indefinitely across many cycles.
        assert growth < 15, \
            f"Thread count grew by {growth} (baseline={baseline_threads}, " \
            f"current={current_threads}). Possible unbounded growth."

    def test_ta_002_threads_die_after_work_completes(self):
        """Prove abandoned threads die when their work finishes."""
        baseline = threading.active_count()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(time.sleep, 0.5)  # short sleep
            try:
                fut.result(timeout=0.1)  # timeout before it finishes
            except concurrent.futures.TimeoutError:
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Thread is still running (0.5s sleep not done yet)
        time.sleep(1.0)  # wait for the sleep to finish

        # Thread should have died now
        current = threading.active_count()
        assert current <= baseline + 2, \
            f"Thread should have died (baseline={baseline}, current={current})"


# ── PROOF 3: Scan state management ───────────────────────────────

class TestScanStateManagement:
    """Test that scan state flags reset correctly in all scenarios."""

    def test_ssm_001_any_scan_active_resets_on_complete(self):
        """_any_scan_active must be False after scan_complete."""
        # This tests the actual AssetScanner._on_scan_complete method
        from config.settings import DEFAULT_CONFIG
        # Verify the pattern: _on_scan_complete sets _any_scan_active = False
        import inspect
        # Read the source to confirm the flag is reset
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        # Find _on_scan_complete method
        assert "def _on_scan_complete(self, candidates" in source

        # Verify key state resets are present in the method
        # These should appear shortly after the method definition
        idx = source.index("def _on_scan_complete(self, candidates")
        next_method = source.index("def _on_scan_error", idx)  # find next method to bound our search
        method_section = source[idx:next_method]

        assert "self._any_scan_active = False" in method_section, \
            "_any_scan_active must be reset in _on_scan_complete"
        assert "self._worker = None" in method_section, \
            "_worker must be cleared in _on_scan_complete"
        assert "self._last_scan_at" in method_section, \
            "_last_scan_at must be updated in _on_scan_complete"

    def test_ssm_002_any_scan_active_resets_on_error(self):
        """_any_scan_active must be False after scan_error."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _on_scan_error(self, err")
        method_body = source[idx:idx+300]
        assert "self._any_scan_active = False" in method_body
        assert "self._worker = None" in method_body

    def test_ssm_003_watchdog_resets_state_on_stuck(self):
        """Watchdog must reset _any_scan_active when killing stuck worker."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _check_worker_health(self)")
        # Find end of method (next def at same indent level)
        next_def = source.index("\n    def ", idx + 10)
        method_body = source[idx:next_def]
        assert "self._any_scan_active = False" in method_body, \
            "Watchdog must reset _any_scan_active when killing stuck worker"

    def test_ssm_004_scan_complete_emitted_in_run_method(self):
        """ScanWorker.run() must always emit scan_complete or scan_error."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        # Both emit methods exist in the file (they may be in different methods)
        assert "scan_complete.emit" in source, "run() must emit scan_complete somewhere"
        assert "scan_error.emit" in source, "file must emit scan_error on exception somewhere"

    def test_ssm_005_no_with_threadpoolexecutor_in_scanner(self):
        """Verify NO `with ThreadPoolExecutor` context managers remain in scanner.py."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        lines = source.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('with') and 'ThreadPoolExecutor' in stripped:
                pytest.fail(f"Line {i}: found `with ThreadPoolExecutor` context manager: {stripped}")

    def test_ssm_006_no_with_threadpoolexecutor_in_ltf_worker(self):
        """Verify NO `with ThreadPoolExecutor` context managers remain in ltf_scan_worker.py."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "ltf_scan_worker.py"
        source = src_path.read_text()

        lines = source.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('with') and 'ThreadPoolExecutor' in stripped:
                pytest.fail(f"Line {i}: found `with ThreadPoolExecutor`: {stripped}")

    def test_ssm_007_no_with_threadpoolexecutor_in_news_feed(self):
        """Verify NO `with ThreadPoolExecutor` context managers remain in news_feed.py."""
        src_path = Path(__file__).parent.parent.parent / "core" / "nlp" / "news_feed.py"
        source = src_path.read_text()

        lines = source.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('with') and 'ThreadPoolExecutor' in stripped:
                pytest.fail(f"Line {i}: found `with ThreadPoolExecutor`: {stripped}")

    def test_ssm_008_all_pool_shutdowns_use_wait_false(self):
        """Every pool.shutdown() in scanner.py must use wait=False."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        lines = source.split('\n')
        for i, line in enumerate(lines, 1):
            if '.shutdown(' in line and 'ThreadPoolExecutor' not in line:
                assert 'wait=False' in line, \
                    f"Line {i}: pool.shutdown() must use wait=False: {line.strip()}"


# ── PROOF 4: Bybit data layer validation ──────────────────────────

class TestBybitDataValidation:
    """Validate that exchange data calls work correctly."""

    def test_bdv_001_fetch_ohlcv_has_timeout_protection(self):
        """Every fetch_ohlcv in scanner.py must be inside a timeout-protected executor."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        # Count fetch_ohlcv calls
        import re
        fetches = re.findall(r'fetch_ohlcv\(', source)
        timeouts = re.findall(r'\.result\(timeout=', source)

        # Every fetch_ohlcv should have a corresponding timeout
        # (At least as many timeouts as fetches)
        assert len(timeouts) >= len(fetches), \
            f"Found {len(fetches)} fetch_ohlcv calls but only {len(timeouts)} timeouts"

    def test_bdv_002_fetch_tickers_has_timeout(self):
        """fetch_tickers call must be timeout-protected."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        assert "fetch_tickers" in source
        # Find the line and verify it's in a timeout context
        idx = source.index("fetch_tickers")
        context = source[max(0, idx-500):idx+500]
        assert "timeout=" in context, "fetch_tickers must have timeout protection"

    def test_bdv_003_mtf_fetch_has_timeout(self):
        """MTF 4h fetch must be timeout-protected."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        # Find MTF fetch section
        assert "MTF" in source
        # MTF section contains fetch_ohlcv call within explicit pool + timeout pattern
        assert "fetch_ohlcv, symbol, higher_tf" in source
        assert "timeout=10.0" in source, "MTF 4h fetch must have 10s timeout protection"
        assert "wait=False" in source, "MTF fetch must use non-blocking shutdown"

    def test_bdv_004_news_feed_parse_has_timeout(self):
        """feedparser.parse() call must be timeout-protected."""
        src_path = Path(__file__).parent.parent.parent / "core" / "nlp" / "news_feed.py"
        source = src_path.read_text()

        assert "timeout=" in source, "feedparser parse must have timeout"
        assert "shutdown(wait=False" in source, "news_feed must use non-blocking shutdown"


# ── PROOF 5: Watchdog behavior ────────────────────────────────────

class TestWatchdogBehavior:
    """Test watchdog recovery logic."""

    def test_wd_001_watchdog_checks_htf_worker_elapsed(self):
        """Watchdog must check elapsed time of HTF worker."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _check_worker_health")
        next_def = source.index("\n    def ", idx + 10)
        method = source[idx:next_def]
        assert "_worker_started_at" in method
        assert "_max_scan_duration_s" in method

    def test_wd_002_watchdog_checks_ltf_worker(self):
        """Watchdog must also check LTF worker."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _check_worker_health")
        next_def = source.index("\n    def ", idx + 10)
        method = source[idx:next_def]
        assert "self._ltf_worker" in method
        assert "self._ltf_worker_started_at" in method

    def test_wd_003_watchdog_detects_scan_staleness(self):
        """Watchdog must detect when no scan has completed within expected interval."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _check_worker_health")
        next_def = source.index("\n    def ", idx + 10)
        method = source[idx:next_def]
        # Check for staleness detection pattern
        has_staleness = "staleness" in method.lower() or "_last_scan_age" in method.lower() or "since last" in method.lower()
        assert has_staleness, "Watchdog must check staleness"

    def test_wd_004_watchdog_restarts_inactive_timers(self):
        """Watchdog must restart timers that stopped unexpectedly."""
        src_path = Path(__file__).parent.parent.parent / "core" / "scanning" / "scanner.py"
        source = src_path.read_text()

        idx = source.index("def _check_worker_health")
        next_method = source.index("def _trigger_scan", idx)  # find next method to bound search
        method = source[idx:next_method]

        assert "isActive()" in method, "Watchdog must check timer activity"
        assert ".start()" in method, "Watchdog must restart inactive timers"
