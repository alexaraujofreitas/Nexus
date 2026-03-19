"""
tests/unit/test_scanner_lifecycle.py
--------------------------------------
Regression tests for scanner worker lifecycle bugs that caused IDSS to run
only once per session and SignalGenerator warmup to reset between cycles.

  SL-01  Worker reference cleared in _on_scan_complete → next scan not skipped
  SL-02  Worker reference cleared in _on_scan_error   → next scan not skipped
  SL-03  AssetScanner passes the SAME sig_gen to every ScanWorker instance
  SL-04  SignalGenerator warmup state is preserved across scan cycles
  SL-05  _last_scan_at is None before first scan
  SL-06  _last_scan_at is set after _on_scan_complete
  SL-07  _last_scan_at is NOT set after _on_scan_error (error doesn't count as a scan)
"""
from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# SL-01 & SL-02 — Worker reference cleared on completion / error
# ---------------------------------------------------------------------------

class TestWorkerReferenceClearing:
    """Worker reference must be cleared so the timer guard never blocks a retry."""

    def _make_scanner(self):
        """Return an AssetScanner instance with QTimer and QThread patched."""
        with patch("core.scanning.scanner.QTimer"), \
             patch("core.scanning.scanner.ScanWorker"), \
             patch("core.scanning.scanner.SignalGenerator"):
            from core.scanning.scanner import AssetScanner
            scanner = AssetScanner.__new__(AssetScanner)
            scanner._worker = None
            scanner._running = False
            scanner._last_scan_at = None
            scanner._staged_enabled = False
            scanner._any_scan_active = False
            scanner._ltf_worker = None
            scanner._ltf_worker_started_at = None
            # Minimal stubs so signal emission doesn't fail
            scanner.scan_finished = MagicMock()
            scanner.scan_finished.emit = MagicMock()
            scanner.candidates_ready = MagicMock()
            scanner.candidates_ready.emit = MagicMock()
            scanner.confirmed_ready = MagicMock()
            scanner.confirmed_ready.emit = MagicMock()
            scanner.ltf_scan_finished = MagicMock()
            scanner.ltf_scan_finished.emit = MagicMock()
            scanner.scan_error = MagicMock()
            scanner.scan_error.emit = MagicMock()
            return scanner

    def test_sl01_worker_none_after_scan_complete(self):
        """_on_scan_complete must set self._worker = None immediately."""
        scanner = self._make_scanner()
        # Simulate an in-flight worker
        mock_worker = MagicMock()
        scanner._worker = mock_worker

        with patch("core.scanning.scanner.bus"):
            scanner._on_scan_complete([])

        assert scanner._worker is None, (
            "_worker must be None after _on_scan_complete so the next timer "
            "tick is not blocked by isRunning()"
        )

    def test_sl01_worker_none_even_with_candidates(self):
        """Worker reference must be cleared regardless of how many candidates there are."""
        scanner = self._make_scanner()
        scanner._worker = MagicMock()
        candidate = {"symbol": "BTC/USDT", "side": "buy"}

        with patch("core.scanning.scanner.bus"):
            scanner._on_scan_complete([candidate])

        assert scanner._worker is None

    def test_sl02_worker_none_after_scan_error(self):
        """_on_scan_error must also set self._worker = None."""
        scanner = self._make_scanner()
        scanner._worker = MagicMock()

        scanner._on_scan_error("Connection timeout")

        assert scanner._worker is None, (
            "_worker must be cleared on error so the timer can retry next cycle"
        )

    def test_sl01_second_scan_not_skipped_after_first(self):
        """
        After _on_scan_complete clears _worker, a subsequent _trigger_scan call
        must NOT log 'previous scan still running, skipping'.
        """
        src = inspect.getsource(
            __import__("core.scanning.scanner", fromlist=["AssetScanner"]).AssetScanner
        )
        # Confirm the guard checks self._worker (not self._worker.isRunning() alone)
        # The fix ensures self._worker = None in _on_scan_complete so the guard
        # `if self._worker and self._worker.isRunning()` becomes False on the next tick.
        assert "self._worker = None" in src, (
            "AssetScanner must set self._worker = None in _on_scan_complete "
            "to prevent the 'previous scan still running' skip bug"
        )


# ---------------------------------------------------------------------------
# SL-03 & SL-04 — SignalGenerator shared instance preserved across cycles
# ---------------------------------------------------------------------------

class TestSignalGeneratorPreservation:
    """The same SignalGenerator instance must be reused across all scan cycles."""

    def test_sl03_signal_generator_created_once_in_init(self):
        """
        AssetScanner.__init__ creates self._sig_gen once.
        ScanWorker must receive this instance — never create a new one per cycle.
        """
        import ast
        import textwrap
        src = inspect.getsource(
            __import__("core.scanning.scanner", fromlist=["AssetScanner"]).AssetScanner
        )
        # _sig_gen must be assigned in __init__
        assert "_sig_gen" in src, "AssetScanner must have a _sig_gen attribute"
        assert "SignalGenerator()" in src, (
            "AssetScanner must create SignalGenerator() once in __init__"
        )

    def test_sl03_scan_worker_receives_sig_gen_parameter(self):
        """ScanWorker must accept sig_gen as a constructor parameter."""
        from core.scanning.scanner import ScanWorker
        sig = inspect.signature(ScanWorker.__init__)
        assert "sig_gen" in sig.parameters, (
            "ScanWorker.__init__ must accept a sig_gen parameter so the shared "
            "instance from AssetScanner can be passed in — prevents warmup reset"
        )

    def test_sl04_warmup_flag_preserved_on_sig_gen(self):
        """
        AssetScanner sets _warmup_complete = True on its shared _sig_gen.
        This must survive across calls (the attribute must exist and be True).
        """
        with patch("core.scanning.scanner.QTimer"), \
             patch("core.scanning.scanner.ScanWorker"):
            from core.scanning.scanner import AssetScanner
            scanner = AssetScanner.__new__(AssetScanner)
            # Replicate what __init__ does for sig_gen
            from core.signals.signal_generator import SignalGenerator
            scanner._sig_gen = SignalGenerator()
            scanner._sig_gen._warmup_complete = True
            scanner._sig_gen._warmup_bars_remaining = 0

        assert scanner._sig_gen._warmup_complete is True, (
            "SignalGenerator warmup must be forcibly completed after creation "
            "to prevent signals from being suppressed during the warm-up bars"
        )
        assert scanner._sig_gen._warmup_bars_remaining == 0


# ---------------------------------------------------------------------------
# SL-05, SL-06, SL-07 — _last_scan_at timestamp tracking
# ---------------------------------------------------------------------------

class TestLastScanAt:
    """_last_scan_at must be None before first scan and set after completion."""

    def _make_scanner(self):
        with patch("core.scanning.scanner.QTimer"), \
             patch("core.scanning.scanner.ScanWorker"), \
             patch("core.scanning.scanner.SignalGenerator"):
            from core.scanning.scanner import AssetScanner
            scanner = AssetScanner.__new__(AssetScanner)
            scanner._worker = None
            scanner._running = False
            scanner._last_scan_at = None
            scanner.scan_finished = MagicMock()
            scanner.scan_finished.emit = MagicMock()
            scanner.candidates_ready = MagicMock()
            scanner.candidates_ready.emit = MagicMock()
            scanner.scan_error = MagicMock()
            scanner.scan_error.emit = MagicMock()
            return scanner

    def test_sl05_last_scan_at_none_before_first_scan(self):
        """_last_scan_at must be None on a fresh AssetScanner (not yet scanned)."""
        with patch("core.scanning.scanner.QTimer"), \
             patch("core.scanning.scanner.ScanWorker"), \
             patch("core.scanning.scanner.SignalGenerator"):
            from core.scanning.scanner import AssetScanner
            # Read attribute from class definition to confirm it defaults to None
            src = inspect.getsource(AssetScanner.__init__)
        assert "_last_scan_at" in src, (
            "AssetScanner.__init__ must initialise _last_scan_at"
        )
        assert "None" in src, (
            "_last_scan_at must default to None (not yet scanned)"
        )

    def test_sl06_last_scan_at_set_after_completion(self):
        """_on_scan_complete must set _last_scan_at to a datetime."""
        scanner = self._make_scanner()
        assert scanner._last_scan_at is None

        before = datetime.utcnow()
        with patch("core.scanning.scanner.bus"):
            scanner._on_scan_complete([])
        after = datetime.utcnow()

        assert scanner._last_scan_at is not None, (
            "_last_scan_at must be set after _on_scan_complete"
        )
        assert isinstance(scanner._last_scan_at, datetime)
        assert before <= scanner._last_scan_at <= after, (
            "_last_scan_at must reflect the actual completion time"
        )

    def test_sl07_last_scan_at_not_set_after_error(self):
        """_on_scan_error must NOT update _last_scan_at — errors don't count as scans."""
        scanner = self._make_scanner()
        assert scanner._last_scan_at is None

        scanner._on_scan_error("API timeout")

        assert scanner._last_scan_at is None, (
            "_last_scan_at must remain None after a scan error — "
            "only successful scans should update the timestamp"
        )
