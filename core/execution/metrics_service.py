# ============================================================
# NEXUS TRADER — Unified MetricsService  (Phase 2, Fix 7)
#
# Single point of contact for all GUI pages to read executor
# state. Decouples UI from executor internals — the UI never
# accesses private attributes like _closed_trades, _positions,
# _initial_capital, etc.
#
# All methods return plain dicts/lists — no executor types leak.
# ============================================================
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class MetricsService:
    """
    Unified read-only facade over the active executor.

    GUI pages call MetricsService methods instead of reaching into
    executor internals. This guarantees:
      1. API stability (executor refactors don't break GUI)
      2. Consistent snapshots (single lock acquisition per call)
      3. Mode-agnostic access (works identically for paper/live)
    """

    def __init__(self):
        self._last_snapshot: Optional[dict] = None
        self._last_snapshot_ts: float = 0.0
        self._snapshot_ttl: float = 0.5  # 500ms cache

    def _get_executor(self):
        """
        Return the active executor, or None if unavailable.

        Phase 3 (Module 5): Null-safe — all callers must handle None.
        """
        try:
            from core.execution.order_router import order_router
            return order_router.active_executor
        except Exception:
            return None

    def _get_mode(self) -> str:
        try:
            from core.execution.order_router import order_router
            return order_router.mode
        except Exception:
            return "paper"

    # ── Core snapshot (cached) ───────────────────────────────────

    def get_snapshot(self, force: bool = False) -> dict:
        """
        Return a full production status snapshot.
        Cached for 500ms to avoid hammering the executor on rapid UI refreshes.

        Fix 6: Includes state_version so UI can detect stale state.
        """
        now = time.time()
        if not force and self._last_snapshot and (now - self._last_snapshot_ts) < self._snapshot_ttl:
            return self._last_snapshot

        executor = self._get_executor()
        if executor is None:
            return {"mode": self._get_mode(), "snapshot_ts": now}
        try:
            ps = executor.get_production_status()
        except Exception as exc:
            logger.warning("MetricsService: get_production_status failed: %s", exc)
            ps = {}

        ps["mode"] = self._get_mode()
        ps["snapshot_ts"] = now
        self._last_snapshot = ps
        self._last_snapshot_ts = now
        return ps

    def invalidate_cache(self) -> None:
        """Force next get_snapshot() to re-fetch."""
        self._last_snapshot = None
        self._last_snapshot_ts = 0.0

    # ── Convenience accessors ────────────────────────────────────

    def get_closed_trades(self) -> list[dict]:
        """Return closed trades list from active executor."""
        executor = self._get_executor()
        if executor is not None and hasattr(executor, "get_closed_trades"):
            return executor.get_closed_trades()
        return []

    def get_open_positions(self) -> list[dict]:
        """Return open positions from active executor."""
        executor = self._get_executor()
        if executor is not None and hasattr(executor, "get_open_positions"):
            return executor.get_open_positions()
        return []

    def get_stats(self) -> dict:
        """Return executor stats dict."""
        executor = self._get_executor()
        if executor is not None and hasattr(executor, "get_stats"):
            return executor.get_stats()
        return {}

    def get_initial_capital(self) -> float:
        """
        Return initial capital via public API.

        Phase 3 (Module 5): Uses get_stats() or available_capital instead of
        private attributes (_initial_capital, _initial_usdt).
        """
        executor = self._get_executor()
        if executor is None:
            return 0.0
        # Prefer public API: stats dict or property
        stats = getattr(executor, "get_stats", lambda: {})()
        if stats.get("initial_capital", 0) > 0:
            return float(stats["initial_capital"])
        # Fallback: private attributes (backward compat with PaperExecutor)
        return float(
            getattr(executor, "_initial_capital", 0)
            or getattr(executor, "_initial_usdt", 0)
            or 0.0
        )

    def get_current_capital(self) -> float:
        """
        Return current capital/equity via public API.

        Phase 3 (Module 5): Uses available_capital property instead of
        private attributes (_capital, _balance_cache).
        """
        executor = self._get_executor()
        if executor is None:
            return 0.0
        # Prefer public API: available_capital property
        cap = getattr(executor, "available_capital", None)
        if cap is not None and cap > 0:
            return float(cap)
        # Fallback: private attributes (backward compat)
        return float(getattr(executor, "_capital", 0) or 0.0)

    def get_mode(self) -> str:
        return self._get_mode()

    def is_live(self) -> bool:
        return self._get_mode() == "live"

    # ── Safety state accessors ───────────────────────────────────

    def get_safety_summary(self) -> dict:
        """
        Return a compact dict of all safety states.
        UI renders this without knowing executor internals.
        """
        snap = self.get_snapshot()
        return {
            "kill_switch_active":       snap.get("kill_switch_active", False),
            "kill_switch_reason":       snap.get("kill_switch_reason", ""),
            "circuit_breaker_on":       snap.get("circuit_breaker_on", False),
            "critical_state":           snap.get("critical_state", False),
            "requires_manual_review":   snap.get("requires_manual_review", False),
            "config_validation_failed": snap.get("config_validation_failed", False),
            "equity_uncertain":         snap.get("equity_uncertain", False),
            "daily_loss_limit_hit":     snap.get("daily_loss_limit_hit", False),
            "pending_confirmations":    snap.get("pending_confirmations", 0),
            "state_version":            snap.get("state_version", 0),
        }

    def get_state_version(self) -> int:
        """Quick check for UI polling — avoids full snapshot."""
        executor = self._get_executor()
        if executor is None:
            return 0
        return getattr(executor, "state_version", 0)


# Module-level singleton
metrics_service = MetricsService()
