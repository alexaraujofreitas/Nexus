"""
Phase 9: Health View Builder

Builds HealthView projection from system protection components.
STRICT OBSERVER — reads get_status()/get_state() only.

No Qt imports. No execution engine imports. Pure data transformation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .view_contracts import HealthView

logger = logging.getLogger(__name__)


class HealthViewBuilder:
    """
    Builds read-only health view from protection component states.

    Input: dicts from get_status() / get_state() calls on:
    - KillSwitch
    - CircuitBreaker
    - FailureModeProtection
    - RestartRecoveryManager
    - EdgeValidityMonitor

    Output: frozen HealthView.

    Pure function — no state, no side effects.
    """

    @staticmethod
    def build(
        kill_switch_status: Optional[Dict[str, Any]] = None,
        circuit_breaker_status: Optional[Dict[str, Any]] = None,
        failure_mode_state: Optional[Dict[str, Any]] = None,
        recovery_state: Optional[Dict[str, Any]] = None,
        edge_states: Optional[Dict[str, str]] = None,
    ) -> HealthView:
        """
        Build HealthView from component states.

        All inputs are optional — missing components default to healthy.

        Args:
            kill_switch_status: KillSwitch.get_status() dict.
            circuit_breaker_status: CircuitBreaker.get_status() dict.
            failure_mode_state: FailureModeProtection state dict.
            recovery_state: RestartRecoveryManager.get_state() dict.
            edge_states: {strategy_class → "normal"/"warning"/"degraded"/"suspended"}.

        Returns:
            Frozen HealthView.
        """
        ks = kill_switch_status or {}
        cb = circuit_breaker_status or {}
        fm = failure_mode_state or {}
        rc = recovery_state or {}

        # Kill switch
        ks_state = str(ks.get("state", "armed")).lower()
        ks_reason = str(ks.get("disarm_reason", "") or ks.get("reason", "") or "")
        ks_at = int(ks.get("disarmed_at_ms", 0) or 0)

        # Circuit breaker
        cb_state = str(cb.get("state", "normal")).lower()
        cb_tripped = int(cb.get("tripped_at_ms", 0) or 0)

        # Failure mode
        fm_tier = str(fm.get("tier", "normal")).lower()
        fm_detectors = int(fm.get("active_detectors", 0) or 0)

        # Recovery (Phase 8)
        recovery_complete = bool(rc.get("recovery_complete", True))
        trading_allowed = bool(rc.get("trading_allowed", True))
        last_report = rc.get("last_report") or {}
        recon = last_report.get("reconciliation") or {}
        recon_clean = bool(recon.get("is_clean", True) if recon else True)
        recon_mismatches = int(recon.get("mismatch_count", 0) or 0)

        return HealthView(
            kill_switch_state=ks_state,
            kill_switch_reason=ks_reason,
            kill_switch_at_ms=ks_at,
            circuit_breaker_state=cb_state,
            circuit_breaker_tripped_at_ms=cb_tripped,
            failure_mode_tier=fm_tier,
            failure_mode_detectors=fm_detectors,
            recovery_complete=recovery_complete,
            recovery_trading_allowed=trading_allowed,
            last_reconciliation_clean=recon_clean,
            last_reconciliation_mismatches=recon_mismatches,
            edge_states=dict(edge_states or {}),
        )
