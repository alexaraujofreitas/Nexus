# ============================================================
# NEXUS TRADER — Scale Manager
#
# Tracks the current risk-scaling phase and evaluates whether
# advancement criteria are met.  NEVER auto-applies parameter
# changes — always RECOMMENDS only.  The operator must manually
# update risk_per_trade in Settings after reviewing.
#
# Phases:
#   Phase 1 — risk_per_trade = 0.5%  (entry, ≥ 50 trades)
#   Phase 2 — risk_per_trade = 0.75% (GREEN after Phase 1)
#   Phase 3 — risk_per_trade = 1.0%  (GREEN after 100+ trades)
#
# Phase advancement requires:
#   • Minimum trade count for the phase
#   • ALL three portfolio metrics GREEN (WR, PF, avg R)
#   • No active pause condition (should_pause = False)
#
# DOES NOT MODIFY STRATEGY OR PARAMETERS.
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_FILE = Path("data/scale_manager.json")
_STATE_LOCK = threading.Lock()


# ── Phase definition ──────────────────────────────────────────────────────────

@dataclass
class PhaseDefinition:
    """Static definition of a scaling phase."""
    phase:           int
    risk_pct:        float   # risk_per_trade as fraction (0.005 = 0.5%)
    min_trades:      int     # minimum trades before advancement is possible
    description:     str


PHASES: dict[int, PhaseDefinition] = {
    1: PhaseDefinition(phase=1, risk_pct=0.005,  min_trades=50,  description="Phase 1 — 0.5% risk (entry)"),
    2: PhaseDefinition(phase=2, risk_pct=0.0075, min_trades=50,  description="Phase 2 — 0.75% risk"),
    3: PhaseDefinition(phase=3, risk_pct=0.010,  min_trades=100, description="Phase 3 — 1.0% risk (target)"),
}

_MAX_PHASE = 3


# ── Advancement evaluation result ─────────────────────────────────────────────

@dataclass
class AdvancementEvaluation:
    """Result of checking whether advancement to the next phase is appropriate."""
    current_phase:      int
    next_phase:         Optional[int]
    can_advance:        bool
    recommendation:     str          # human-readable recommendation
    blocking_reasons:   list[str]    # why we cannot advance (if any)
    trade_count:        int
    min_trades_needed:  int
    all_metrics_green:  bool
    should_pause:       bool
    evaluated_at:       str          # ISO timestamp

    def to_dict(self) -> dict:
        return {
            "current_phase":     self.current_phase,
            "next_phase":        self.next_phase,
            "can_advance":       self.can_advance,
            "recommendation":    self.recommendation,
            "blocking_reasons":  self.blocking_reasons,
            "trade_count":       self.trade_count,
            "min_trades_needed": self.min_trades_needed,
            "all_metrics_green": self.all_metrics_green,
            "should_pause":      self.should_pause,
            "evaluated_at":      self.evaluated_at,
        }


# ── Persisted state ───────────────────────────────────────────────────────────

@dataclass
class ScaleState:
    """Mutable state persisted to disk."""
    current_phase:      int   = 1
    phase_started_at:   str   = ""   # ISO timestamp when current phase began
    trades_at_start:    int   = 0    # total trades when current phase began
    last_evaluated_at:  str   = ""
    advancement_log:    list  = field(default_factory=list)  # history of evaluations


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Scale Manager ─────────────────────────────────────────────────────────────

class ScaleManager:
    """
    Tracks current scaling phase and recommends advancement.

    Usage:
        sm = get_scale_manager()
        eval_result = sm.evaluate_advancement()
        print(eval_result.recommendation)   # human-readable guidance
        print(sm.current_phase_def.risk_pct)  # current risk setting
    """

    def __init__(self) -> None:
        self._state = ScaleState()
        self._load()
        if not self._state.phase_started_at:
            self._state.phase_started_at = _now_iso()
            self._save()

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def current_phase(self) -> int:
        return self._state.current_phase

    @property
    def current_phase_def(self) -> PhaseDefinition:
        return PHASES[self._state.current_phase]

    def evaluate_advancement(self) -> AdvancementEvaluation:
        """
        Check current live metrics and determine whether phase advancement
        is appropriate.  Does NOT modify any parameters — returns recommendation only.
        """
        from core.monitoring.performance_thresholds import (
            get_threshold_evaluator, RAGStatus,
        )
        from core.monitoring.live_vs_backtest import get_live_vs_backtest_tracker

        current = self._state.current_phase
        next_ph = current + 1 if current < _MAX_PHASE else None

        # Portfolio trade count
        try:
            comp        = get_live_vs_backtest_tracker().get_comparison()
            trade_count = int(comp.get("trade_count") or 0)
        except Exception as exc:
            logger.debug("ScaleManager: cannot get trade count: %s", exc)
            trade_count = 0

        # Performance assessment
        try:
            assessment      = get_threshold_evaluator().evaluate()
            should_pause    = assessment.should_pause
            port_overall    = assessment.overall
            from core.monitoring.performance_thresholds import RAGStatus
            all_green       = (port_overall == RAGStatus.GREEN)
        except Exception as exc:
            logger.debug("ScaleManager: cannot get assessment: %s", exc)
            assessment   = None
            should_pause = False
            all_green    = False

        # Phase min-trades requirement (for current phase)
        current_def   = PHASES[current]
        trades_in_phase = max(0, trade_count - self._state.trades_at_start)
        min_needed    = current_def.min_trades
        enough_trades = trades_in_phase >= min_needed

        blocking: list[str] = []
        if current >= _MAX_PHASE:
            blocking.append(f"Already at maximum phase ({_MAX_PHASE})")
        else:
            if not enough_trades:
                blocking.append(
                    f"Insufficient trades in current phase: "
                    f"{trades_in_phase}/{min_needed}"
                )
            if not all_green:
                blocking.append(
                    "Portfolio metrics not all GREEN — "
                    "do not advance until WR, PF, and avg R are GREEN"
                )
            if should_pause:
                blocking.append(
                    f"Active pause condition — resolve before scaling: "
                    f"{getattr(assessment, 'pause_reason', 'unknown')}"
                )

        can_advance = len(blocking) == 0 and next_ph is not None

        if can_advance:
            next_def     = PHASES[next_ph]
            recommendation = (
                f"✅ READY TO ADVANCE to Phase {next_ph} "
                f"({next_def.risk_pct*100:.2g}% risk per trade). "
                f"All criteria met: {trades_in_phase} trades in phase, "
                f"portfolio GREEN, no pause condition. "
                f"Update risk_per_trade in Settings to {next_def.risk_pct*100:.2g}% manually."
            )
        elif current >= _MAX_PHASE:
            recommendation = (
                f"📊 Phase {_MAX_PHASE} (maximum) — maintain {current_def.risk_pct*100:.2g}% "
                f"risk per trade and monitor performance."
            )
        else:
            recommendation = (
                f"⏳ Remain in Phase {current} ({current_def.risk_pct*100:.2g}% risk). "
                f"Blocking: {'; '.join(blocking)}"
            )

        now = _now_iso()
        result = AdvancementEvaluation(
            current_phase     = current,
            next_phase        = next_ph,
            can_advance       = can_advance,
            recommendation    = recommendation,
            blocking_reasons  = blocking,
            trade_count       = trade_count,
            min_trades_needed = min_needed,
            all_metrics_green = all_green,
            should_pause      = should_pause,
            evaluated_at      = now,
        )

        # Log the evaluation
        with _STATE_LOCK:
            self._state.last_evaluated_at = now
            self._state.advancement_log.append({
                "ts":          now,
                "phase":       current,
                "can_advance": can_advance,
                "trades":      trade_count,
                "green":       all_green,
            })
            # Keep log to last 100 entries
            if len(self._state.advancement_log) > 100:
                self._state.advancement_log = self._state.advancement_log[-100:]
            self._save()

        return result

    def record_phase_advance(self, new_phase: int, trade_count: int) -> None:
        """
        Call this AFTER the operator has manually updated the risk setting
        to record the phase transition.

        Args:
            new_phase:   The phase the system has moved to (2 or 3)
            trade_count: Current total trade count at transition time
        """
        if new_phase not in PHASES:
            logger.warning("ScaleManager: unknown phase %s — ignoring", new_phase)
            return
        with _STATE_LOCK:
            old_phase = self._state.current_phase
            self._state.current_phase   = new_phase
            self._state.phase_started_at = _now_iso()
            self._state.trades_at_start  = trade_count
            self._state.advancement_log.append({
                "ts":         _now_iso(),
                "event":      "phase_advanced",
                "from_phase": old_phase,
                "to_phase":   new_phase,
                "trade_count": trade_count,
            })
            self._save()
        logger.info(
            "ScaleManager: phase advanced %s → %s at %s trades",
            old_phase, new_phase, trade_count,
        )

    def get_phase_summary(self) -> dict:
        """Return a dict suitable for dashboard display."""
        phdef = self.current_phase_def
        return {
            "current_phase":     self._state.current_phase,
            "risk_pct":          phdef.risk_pct,
            "risk_pct_str":      f"{phdef.risk_pct*100:.2g}%",
            "description":       phdef.description,
            "phase_started_at":  self._state.phase_started_at,
            "trades_at_start":   self._state.trades_at_start,
            "last_evaluated_at": self._state.last_evaluated_at,
            "max_phase":         _MAX_PHASE,
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            _DATA_FILE.write_text(
                json.dumps(asdict(self._state), indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("ScaleManager: save failed (non-fatal): %s", exc)

    def _load(self) -> None:
        try:
            if _DATA_FILE.exists():
                raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
                self._state.current_phase     = int(raw.get("current_phase", 1))
                self._state.phase_started_at  = raw.get("phase_started_at", "")
                self._state.trades_at_start   = int(raw.get("trades_at_start", 0))
                self._state.last_evaluated_at = raw.get("last_evaluated_at", "")
                self._state.advancement_log   = raw.get("advancement_log", [])
                # Guard against invalid phase values
                if self._state.current_phase not in PHASES:
                    logger.warning(
                        "ScaleManager: invalid phase %s in saved state — resetting to 1",
                        self._state.current_phase,
                    )
                    self._state.current_phase = 1
        except Exception as exc:
            logger.debug("ScaleManager: load failed — starting fresh: %s", exc)


# ── Module singleton ──────────────────────────────────────────────────────────
_manager: Optional[ScaleManager] = None
_mgr_lock = threading.Lock()


def get_scale_manager() -> ScaleManager:
    global _manager
    if _manager is None:
        with _mgr_lock:
            if _manager is None:
                _manager = ScaleManager()
    return _manager
