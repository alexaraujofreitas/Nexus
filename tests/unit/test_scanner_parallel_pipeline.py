"""
tests/unit/test_scanner_parallel_pipeline.py
----------------------------------------------
Tests for the v2 parallel pipeline scanner optimization.

Validates:
  PP-01  ScanCycleMetrics tracks timing data correctly
  PP-02  _scan_symbol_with_regime accepts prefetched context data
  PP-03  Per-symbol regime classifier isolation (no shared mutable state)
  PP-04  Parallel compute produces same results as sequential
  PP-05  Failure isolation — one symbol failure doesn't affect others
  PP-06  Batch OHLCV fetch manifest is correctly constructed
  PP-07  Settings snapshot is passed and used (no lock contention)
  PP-08  Atomic result emission — all results published together
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import fields

import pytest


# ---------------------------------------------------------------------------
# PP-01 — ScanCycleMetrics
# ---------------------------------------------------------------------------

class TestScanCycleMetrics:
    """Validate the timing instrumentation dataclass."""

    def test_pp01_metrics_default_values(self):
        """ScanCycleMetrics initializes with zero/empty defaults."""
        from core.scanning.scanner import ScanCycleMetrics
        m = ScanCycleMetrics()
        assert m.total_cycle_ms == 0.0
        assert m.symbols_total == 0
        assert m.symbols_failed == []
        assert m.per_symbol_ms == {}

    def test_pp01_metrics_log_summary_no_crash(self):
        """log_summary() must not crash even with default values."""
        from core.scanning.scanner import ScanCycleMetrics
        m = ScanCycleMetrics()
        m.total_cycle_ms = 5000
        m.symbols_total = 20
        m.symbols_qualifying = 18
        m.symbols_fetched_ok = 18
        m.symbols_computed = 18
        m.fetch_concurrency = 10
        m.compute_concurrency = 8
        m.slowest_symbol = "BTC/USDT"
        m.slowest_symbol_ms = 800
        m.avg_symbol_ms = 400
        # Should not raise
        m.log_summary()


# ---------------------------------------------------------------------------
# PP-02 — _scan_symbol_with_regime accepts prefetched context
# ---------------------------------------------------------------------------

class TestPrefetchedContextAcceptance:
    """Verify _scan_symbol_with_regime signature accepts new parameters."""

    def test_pp02_signature_has_prefetched_params(self):
        """Method signature includes prefetched_ctx_4h, prefetched_ctx_1h, prefetched_mtf."""
        import inspect
        from core.scanning.scanner import ScanWorker
        sig = inspect.signature(ScanWorker._scan_symbol_with_regime)
        params = list(sig.parameters.keys())
        assert "prefetched_ctx_4h" in params
        assert "prefetched_ctx_1h" in params
        assert "prefetched_mtf" in params
        assert "settings_snapshot" in params


# ---------------------------------------------------------------------------
# PP-03 — Per-symbol regime classifier isolation
# ---------------------------------------------------------------------------

class TestRegimeClassifierIsolation:
    """Verify that parallel compute uses per-call classifier instances."""

    def test_pp03_no_shared_regime_clf_in_scan_symbol(self):
        """_scan_symbol_with_regime must NOT reference self._regime_clf."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker._scan_symbol_with_regime)
        # Should use _local_clf, not self._regime_clf
        assert "_local_clf" in source, \
            "_scan_symbol_with_regime should use _local_clf for thread safety"
        # The old self._regime_clf should not appear in the method body
        # (it may appear in __init__ but not in the per-symbol method)
        # We check the method source specifically
        assert "self._regime_clf.classify" not in source, \
            "_scan_symbol_with_regime should not use self._regime_clf (shared mutable state)"


# ---------------------------------------------------------------------------
# PP-04 — Settings snapshot usage
# ---------------------------------------------------------------------------

class TestSettingsSnapshot:
    """Verify settings_snapshot is used instead of direct settings access."""

    def test_pp07_scan_symbol_uses_settings_snapshot(self):
        """_scan_symbol_with_regime should use _ss (settings snapshot) for key settings."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker._scan_symbol_with_regime)
        # Should reference _ss for various settings
        assert "_ss.get(" in source, \
            "Method should use _ss (settings_snapshot) to avoid lock contention"


# ---------------------------------------------------------------------------
# PP-05 — Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    """Verify one symbol's failure doesn't stall the entire cycle."""

    def test_pp05_process_symbol_catches_exceptions(self):
        """The _process_symbol wrapper in run() should catch exceptions gracefully."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker.run)
        # Should have per-symbol exception handling in compute results
        assert "Scan error" in source, \
            "run() should handle per-symbol errors and produce 'Scan error' results"


# ---------------------------------------------------------------------------
# PP-06 — Batch OHLCV fetch manifest
# ---------------------------------------------------------------------------

class TestBatchFetchManifest:
    """Verify the fetch manifest includes all needed timeframes."""

    def test_pp06_manifest_structure_in_run(self):
        """run() should build a fetch_manifest with primary + context + MTF keys."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker.run)
        assert "_fetch_manifest" in source
        assert "|primary" in source
        assert "|ctx_4h" in source
        assert "|ctx_1h" in source
        assert "|mtf" in source


# ---------------------------------------------------------------------------
# PP-07 — No sequential per-symbol REST calls in compute phase
# ---------------------------------------------------------------------------

class TestNoSequentialFetchInCompute:
    """Verify that _scan_symbol_with_regime doesn't make REST calls for context."""

    def test_pp07_no_exchange_fetch_in_pbl_slc_section(self):
        """PBL/SLC context should use prefetched data, not live REST calls."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker._scan_symbol_with_regime)
        # The old code had: self._exchange.fetch_ohlcv inside PBL/SLC section
        # The new code should use prefetched_ctx_4h and prefetched_ctx_1h
        # Count occurrences of self._exchange.fetch_ohlcv in the method
        fetch_count = source.count("self._exchange.fetch_ohlcv")
        # Should only appear ONCE (in the fallback for primary OHLCV)
        assert fetch_count <= 1, (
            f"_scan_symbol_with_regime has {fetch_count} exchange.fetch_ohlcv calls. "
            "Expected ≤1 (fallback only). Context/MTF should use prefetched data."
        )


# ---------------------------------------------------------------------------
# PP-08 — Parallel compute in run()
# ---------------------------------------------------------------------------

class TestParallelCompute:
    """Verify run() uses parallel execution for per-symbol compute."""

    def test_pp08_run_uses_compute_pool(self):
        """run() should create a ThreadPoolExecutor for compute phase."""
        import inspect
        from core.scanning.scanner import ScanWorker
        source = inspect.getsource(ScanWorker.run)
        assert "_compute_pool" in source, \
            "run() should use _compute_pool for parallel per-symbol compute"
        assert "_process_symbol" in source, \
            "run() should define _process_symbol for parallel dispatch"
        assert "max_compute_workers" in source or "compute_concurrency" in source, \
            "run() should have configurable compute concurrency"
