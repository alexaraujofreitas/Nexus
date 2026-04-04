"""
test_session37_dashboard_scanner_status.py
===========================================
Regression tests for the Dashboard "IDSS Scanner: Stopped" bug.

Root cause (Session 37, 2026-03-28):
  1. Topics.SCAN_CYCLE_COMPLETE was defined in event_bus.py but NEVER published
     by the scanner.  The Dashboard subscribed to it in _on_scan_cycle_complete()
     but the callback never fired, so the status row stayed permanently at the
     startup-probe value ("Stopped").
  2. Dashboard's startup probe (QTimer 3.5s after exchange connect) fired before
     the scanner's 8-second GPU/RL init delay completed, always seeing _running=False.

Fix:
  1. AssetScanner._on_scan_complete() now calls bus.publish(SCAN_CYCLE_COMPLETE, ...)
     after every HTF scan cycle.
  2. Dashboard startup probe delay increased from 3.5s to 10s (> 8s scanner init).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call
from datetime import datetime


# ── Test 1: SCAN_CYCLE_COMPLETE IS published after a scan cycle ───────────────

class TestScanCycleCompletePublished(unittest.TestCase):
    """Verify that AssetScanner._on_scan_complete publishes SCAN_CYCLE_COMPLETE."""

    def test_scan_cycle_complete_published_with_candidates(self):
        """When candidates exist, SCAN_CYCLE_COMPLETE must still fire."""
        from core.event_bus import Topics
        with patch("core.scanning.scanner.bus") as mock_bus, \
             patch("core.scanning.scanner.scanner") as _:
            from core.scanning.scanner import AssetScanner
            sc = AssetScanner.__new__(AssetScanner)
            sc._worker = MagicMock()
            sc._worker_started_at = None
            sc._any_scan_active = True
            sc._last_scan_at = None
            sc._watchdog_consecutive_kills = 1
            sc._watchdog_last_kill_at = datetime.utcnow()
            sc.scan_finished = MagicMock()
            sc.candidates_ready = MagicMock()
            sc._staged_enabled = False
            sc._any_ltf_active = False
            sc._ltf_worker = None

            candidate = MagicMock()
            candidate.to_dict.return_value = {}

            sc._on_scan_complete([candidate])

            # Must publish SCAN_CYCLE_COMPLETE
            publish_calls = [str(c) for c in mock_bus.publish.call_args_list]
            cycle_complete_calls = [
                c for c in mock_bus.publish.call_args_list
                if c.args and c.args[0] == Topics.SCAN_CYCLE_COMPLETE
            ]
            self.assertTrue(
                len(cycle_complete_calls) >= 1,
                f"SCAN_CYCLE_COMPLETE not published. publish calls: {publish_calls}"
            )

    def test_scan_cycle_complete_published_with_zero_candidates(self):
        """Even with zero candidates, SCAN_CYCLE_COMPLETE must fire."""
        from core.event_bus import Topics
        with patch("core.scanning.scanner.bus") as mock_bus, \
             patch("core.scanning.scanner.scanner") as _:
            from core.scanning.scanner import AssetScanner
            sc = AssetScanner.__new__(AssetScanner)
            sc._worker = MagicMock()
            sc._worker_started_at = None
            sc._any_scan_active = True
            sc._last_scan_at = None
            sc._watchdog_consecutive_kills = 0
            sc._watchdog_last_kill_at = None
            sc.scan_finished = MagicMock()
            sc.candidates_ready = MagicMock()
            sc._staged_enabled = False
            sc._any_ltf_active = False
            sc._ltf_worker = None

            sc._on_scan_complete([])  # zero candidates

            cycle_complete_calls = [
                c for c in mock_bus.publish.call_args_list
                if c.args and c.args[0] == Topics.SCAN_CYCLE_COMPLETE
            ]
            self.assertTrue(
                len(cycle_complete_calls) >= 1,
                "SCAN_CYCLE_COMPLETE must be published even when no candidates found"
            )

    def test_scan_cycle_complete_has_correct_source(self):
        """SCAN_CYCLE_COMPLETE must be published with source='scanner'."""
        from core.event_bus import Topics
        with patch("core.scanning.scanner.bus") as mock_bus, \
             patch("core.scanning.scanner.scanner") as _:
            from core.scanning.scanner import AssetScanner
            sc = AssetScanner.__new__(AssetScanner)
            sc._worker = MagicMock()
            sc._worker_started_at = None
            sc._any_scan_active = True
            sc._last_scan_at = None
            sc._watchdog_consecutive_kills = 0
            sc._watchdog_last_kill_at = None
            sc.scan_finished = MagicMock()
            sc.candidates_ready = MagicMock()
            sc._staged_enabled = False
            sc._any_ltf_active = False
            sc._ltf_worker = None

            sc._on_scan_complete([])

            cycle_calls = [
                c for c in mock_bus.publish.call_args_list
                if c.args and c.args[0] == Topics.SCAN_CYCLE_COMPLETE
            ]
            self.assertTrue(cycle_calls, "SCAN_CYCLE_COMPLETE not published")
            # Check source kwarg
            kw = cycle_calls[0].kwargs
            self.assertEqual(kw.get("source"), "scanner",
                             f"Expected source='scanner', got: {kw}")

    def test_scan_cycle_complete_data_has_candidates_count(self):
        """Published data must include 'candidates' count and 'timestamp'."""
        from core.event_bus import Topics
        with patch("core.scanning.scanner.bus") as mock_bus, \
             patch("core.scanning.scanner.scanner") as _:
            from core.scanning.scanner import AssetScanner
            sc = AssetScanner.__new__(AssetScanner)
            sc._worker = MagicMock()
            sc._worker_started_at = None
            sc._any_scan_active = True
            sc._last_scan_at = None
            sc._watchdog_consecutive_kills = 0
            sc._watchdog_last_kill_at = None
            sc.scan_finished = MagicMock()
            sc.candidates_ready = MagicMock()
            sc._staged_enabled = False
            sc._any_ltf_active = False
            sc._ltf_worker = None

            mock_c1 = MagicMock()
            mock_c2 = MagicMock()
            sc._on_scan_complete([mock_c1, mock_c2])

            cycle_calls = [
                c for c in mock_bus.publish.call_args_list
                if c.args and c.args[0] == Topics.SCAN_CYCLE_COMPLETE
            ]
            self.assertTrue(cycle_calls)
            data = cycle_calls[0].kwargs.get("data", {})
            self.assertIn("candidates", data,
                          "Published data must include 'candidates' key")
            self.assertEqual(data["candidates"], 2,
                             "candidates count must equal len(candidates)")
            self.assertIn("timestamp", data,
                          "Published data must include 'timestamp' key")


# ── Test 2: SCAN_CYCLE_COMPLETE topic is defined in Topics ────────────────────

class TestTopicsDefined(unittest.TestCase):
    """Sanity-check that the event bus Topic exists."""

    def test_scan_cycle_complete_in_topics(self):
        from core.event_bus import Topics
        self.assertTrue(
            hasattr(Topics, "SCAN_CYCLE_COMPLETE"),
            "Topics.SCAN_CYCLE_COMPLETE must exist"
        )
        self.assertEqual(Topics.SCAN_CYCLE_COMPLETE, "scanner.cycle_complete")

    def test_scan_cycle_start_in_topics(self):
        from core.event_bus import Topics
        self.assertTrue(hasattr(Topics, "SCAN_CYCLE_START"))


# ── Test 3: Dashboard startup probe delay check (code inspection) ─────────────

class TestDashboardProbeDelay(unittest.TestCase):
    """Verify the dashboard probe delay is > 8000ms (scanner's GPU init window)."""

    def test_dashboard_probe_fires_after_scanner_init_window(self):
        """
        The scanner defers start() by 8000ms (GPU/RL init).
        The dashboard startup probe must fire AFTER that window to see _running=True.
        """
        import ast, pathlib
        src = pathlib.Path(
            "gui/pages/dashboard/dashboard_page.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)

        # Find all singleShot calls with _update_scanner_row_startup
        delays_for_scanner_probe = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "singleShot":
                    # Check if last arg is _update_scanner_row_startup
                    if node.args and isinstance(node.args[-1], ast.Attribute):
                        if node.args[-1].attr == "_update_scanner_row_startup":
                            if isinstance(node.args[0], ast.Constant):
                                delays_for_scanner_probe.append(node.args[0].value)

        self.assertTrue(
            delays_for_scanner_probe,
            "No QTimer.singleShot(..., _update_scanner_row_startup) found in dashboard"
        )
        # All probes that fire after exchange connect must be > 8000ms
        # (the initial t+2s probe is before exchange connect so it may be less)
        max_delay = max(delays_for_scanner_probe)
        self.assertGreater(
            max_delay, 8000,
            f"Largest scanner probe delay is {max_delay}ms — must be >8000ms "
            f"to fire after the scanner's 8s GPU/RL init window. "
            f"All delays found: {delays_for_scanner_probe}"
        )


if __name__ == "__main__":
    unittest.main()
