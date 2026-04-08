# ============================================================
# NEXUS TRADER — Edge Validity Monitor  (Phase 5B Wave 2)
#
# Per-strategy-class health tracking with state machine.
#
# STATES:
#   ACTIVE     — normal operation, full exposure
#   DEGRADED   — reduced exposure (0.7x multiplier)
#   SUSPENDED  — no new trades for this strategy class
#   PROBE      — recovery testing (0.5x exposure, limited trades)
#
# TRANSITION RULES (with hysteresis):
#   ACTIVE → DEGRADED:   PF < 0.80 AND WR < 40% (lookback 20 trades)
#   DEGRADED → SUSPENDED: PF < 0.60 AND WR < 30% (lookback 20 trades)
#   SUSPENDED → PROBE:    dwell_time >= 2h AND class-local metric recovery
#                          (expectancy trending toward zero or positive)
#   PROBE → ACTIVE:       PF >= 1.20 AND WR >= 50% (probe lookback 5 trades)
#   PROBE → SUSPENDED:    PF < 0.80 OR WR < 35% (probe lookback 5 trades)
#   DEGRADED → ACTIVE:    PF >= 1.10 AND WR >= 48% (recovery thresholds)
#
# CLASS-PURE RECOVERY: SUSPENDED → PROBE uses only class-local evidence.
# The suspended class's own recent outcomes must show improvement (last N
# outcomes have expectancy >= threshold) before probe entry. No cross-class
# coupling. This is the "class recovery signal" policy.
#
# HYSTERESIS: Degrade thresholds are LOWER than recovery thresholds
# to prevent oscillation.
#
# DWELL TIME: Minimum time in each state before transition allowed.
#   ACTIVE: 0 (can degrade immediately)
#   DEGRADED: 30min
#   SUSPENDED: 2h
#   PROBE: 1h (or 5 probe trades)
#
# METRICS:
#   - Profit Factor (PF) = sum(wins) / sum(losses)
#   - Win Rate (WR) = n_wins / n_total
#   - Expectancy = avg(pnl_pct)
#
# DETERMINISM:
#   - Event-sourced, replayable
#   - now_ms injected, no wall-clock
#   - Same events → same state transitions
#
# RUNTIME ONLY: No config.yaml mutation.
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────
_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 86_400_000
_EVENT_LOG_VERSION = 1
_RETENTION_WINDOW_MS = 7 * _MS_PER_DAY


# ── Edge States ─────────────────────────────────────────────

class EdgeState(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    PROBE = "probe"


# ── Configuration ───────────────────────────────────────────

@dataclass(frozen=True)
class EdgeValidityConfig:
    """Configuration for edge validity monitor."""
    # ── Lookback windows ──
    lookback_trades: int = 20          # Trades for degrade/recover evaluation
    probe_lookback_trades: int = 5     # Trades for probe evaluation

    # ── ACTIVE → DEGRADED thresholds ──
    degrade_pf: float = 0.80           # PF below this → DEGRADED
    degrade_wr: float = 0.40           # WR below this → DEGRADED

    # ── DEGRADED → SUSPENDED thresholds ──
    suspend_pf: float = 0.60           # PF below this → SUSPENDED
    suspend_wr: float = 0.30           # WR below this → SUSPENDED

    # ── DEGRADED → ACTIVE recovery thresholds (hysteresis) ──
    recover_pf: float = 1.10           # PF above this → ACTIVE
    recover_wr: float = 0.48           # WR above this → ACTIVE

    # ── PROBE → ACTIVE thresholds ──
    probe_recover_pf: float = 1.20     # PF in probe → ACTIVE
    probe_recover_wr: float = 0.50     # WR in probe → ACTIVE

    # ── PROBE → SUSPENDED thresholds ──
    probe_fail_pf: float = 0.80        # PF in probe → back to SUSPENDED
    probe_fail_wr: float = 0.35        # WR in probe → back to SUSPENDED

    # ── Dwell times (minimum time in state) ──
    dwell_degraded_ms: int = 30 * 60 * 1000     # 30 min
    dwell_suspended_ms: int = 2 * _MS_PER_HOUR   # 2 hours
    dwell_probe_ms: int = 1 * _MS_PER_HOUR        # 1 hour

    # ── SUSPENDED → PROBE: class-local recovery signal ──
    # Uses the SAME class's recent outcomes to judge readiness.
    # Expectancy of last N outcomes must be >= threshold.
    probe_entry_lookback: int = 5          # Last N outcomes for this class
    probe_entry_expectancy: float = -0.10  # Expectancy floor for probe entry
    #   (≥ -0.10 means "bleeding has slowed enough to test recovery")

    # ── Exposure multipliers ──
    degraded_multiplier: float = 0.70
    probe_multiplier: float = 0.50

    # ── Probe trade limit (per probe period) ──
    probe_max_trades: int = 5


# ── Event Log ───────────────────────────────────────────────

@dataclass(frozen=True)
class EdgeEvent:
    """Immutable event for edge validity replay."""
    event_type: str        # "trade_outcome" or "state_transition"
    timestamp_ms: int
    strategy_class: str = ""

    # trade_outcome fields
    is_win: Optional[bool] = None
    pnl_pct: float = 0.0

    # state_transition fields
    from_state: str = ""
    to_state: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        d = {"event_type": self.event_type, "timestamp_ms": self.timestamp_ms,
             "strategy_class": self.strategy_class}
        if self.event_type == "trade_outcome":
            d.update({"is_win": self.is_win, "pnl_pct": self.pnl_pct})
        elif self.event_type == "state_transition":
            d.update({"from_state": self.from_state, "to_state": self.to_state,
                       "reason": self.reason})
        return d

    @classmethod
    def from_dict(cls, d: dict) -> EdgeEvent:
        return cls(
            event_type=d["event_type"], timestamp_ms=d["timestamp_ms"],
            strategy_class=d.get("strategy_class", ""),
            is_win=d.get("is_win"), pnl_pct=d.get("pnl_pct", 0.0),
            from_state=d.get("from_state", ""), to_state=d.get("to_state", ""),
            reason=d.get("reason", ""),
        )


# ── Per-Strategy-Class State ────────────────────────────────

class _StrategyState:
    """Mutable per-strategy-class state. Internal only."""
    __slots__ = ("state", "entered_at_ms", "outcomes", "probe_outcomes",
                 "probe_trade_count")

    def __init__(self):
        self.state: EdgeState = EdgeState.ACTIVE
        self.entered_at_ms: int = 0
        self.outcomes: List[dict] = []    # {is_win, pnl_pct, ts}
        self.probe_outcomes: List[dict] = []
        self.probe_trade_count: int = 0


# ── Result (for PipelineContext) ────────────────────────────

@dataclass(frozen=True)
class EdgeValidityResult:
    """Immutable result for pipeline context."""
    passed: bool                   # True if strategy class is not SUSPENDED
    strategy_class: str
    state: str                     # EdgeState value
    exposure_multiplier: float     # 1.0, 0.7, 0.5, or 0.0
    pf: float                      # Current profit factor
    wr: float                      # Current win rate
    expectancy: float              # Current expectancy (avg pnl_pct)
    trade_count: int               # Trades for this strategy class
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "edge_passed": self.passed,
            "edge_strategy_class": self.strategy_class,
            "edge_state": self.state,
            "edge_exposure_multiplier": self.exposure_multiplier,
            "edge_pf": round(self.pf, 4),
            "edge_wr": round(self.wr, 4),
            "edge_expectancy": round(self.expectancy, 6),
            "edge_trade_count": self.trade_count,
            "edge_reason": self.reason,
        }


# ── Snapshot ────────────────────────────────────────────────

@dataclass(frozen=True)
class EdgeValiditySnapshot:
    """Frozen snapshot of all strategy class states."""
    class_states: tuple    # tuple of (strategy_class, state, trade_count)
    event_count: int

    def to_dict(self) -> dict:
        return {
            "class_states": [
                {"strategy_class": sc, "state": st, "trade_count": tc}
                for sc, st, tc in self.class_states
            ],
            "event_count": self.event_count,
        }


# ── Corrupt State Error ─────────────────────────────────────

class EdgeValidityStateCorruptError(Exception):
    """Fail-closed: persisted state cannot be safely restored."""
    pass


# ── Main Class ──────────────────────────────────────────────

class EdgeValidityMonitor:
    """
    Per-strategy-class health monitor with state machine.

    Tracks PF, WR, and expectancy per strategy class. Applies
    graduated state transitions with hysteresis and dwell times.

    State management:
      - Event-sourced: all state from event log
      - record_trade_outcome() appends event + updates state + evaluates transitions
      - evaluate() is read-only
      - Replay via replay(events)

    Concurrency: NOT thread-safe. Single-threaded pipeline.
    """

    def __init__(self, config: EdgeValidityConfig = None):
        self.config = config or EdgeValidityConfig()
        self._event_log: List[EdgeEvent] = []
        self._states: Dict[str, _StrategyState] = defaultdict(_StrategyState)
        logger.info(
            "EdgeValidityMonitor initialized: lookback=%d degrade_pf=%.2f recover_pf=%.2f",
            self.config.lookback_trades, self.config.degrade_pf, self.config.recover_pf,
        )

    @property
    def event_log(self) -> List[EdgeEvent]:
        return list(self._event_log)

    @property
    def event_count(self) -> int:
        return len(self._event_log)

    # ── Evaluation (read-only) ──────────────────────────────

    def evaluate(self, strategy_class: str, now_ms: int) -> EdgeValidityResult:
        """
        Evaluate whether a strategy class should trade.

        READ-ONLY: does not mutate state.
        """
        ss = self._states[strategy_class]
        n = len(ss.outcomes)
        pf, wr, exp = self._compute_metrics(ss.outcomes, self.config.lookback_trades)

        if ss.state == EdgeState.SUSPENDED:
            return EdgeValidityResult(
                passed=False, strategy_class=strategy_class,
                state=ss.state.value, exposure_multiplier=0.0,
                pf=pf, wr=wr, expectancy=exp, trade_count=n,
                reason=f"Strategy class {strategy_class} SUSPENDED (PF={pf:.2f} WR={wr:.1%})",
            )

        if ss.state == EdgeState.PROBE:
            if ss.probe_trade_count >= self.config.probe_max_trades:
                return EdgeValidityResult(
                    passed=False, strategy_class=strategy_class,
                    state=ss.state.value, exposure_multiplier=0.0,
                    pf=pf, wr=wr, expectancy=exp, trade_count=n,
                    reason=f"Probe trade limit reached ({ss.probe_trade_count}/{self.config.probe_max_trades})",
                )
            return EdgeValidityResult(
                passed=True, strategy_class=strategy_class,
                state=ss.state.value,
                exposure_multiplier=self.config.probe_multiplier,
                pf=pf, wr=wr, expectancy=exp, trade_count=n,
                reason=f"PROBE mode: {self.config.probe_multiplier:.0%} exposure",
            )

        if ss.state == EdgeState.DEGRADED:
            return EdgeValidityResult(
                passed=True, strategy_class=strategy_class,
                state=ss.state.value,
                exposure_multiplier=self.config.degraded_multiplier,
                pf=pf, wr=wr, expectancy=exp, trade_count=n,
                reason=f"DEGRADED: {self.config.degraded_multiplier:.0%} exposure (PF={pf:.2f} WR={wr:.1%})",
            )

        # ACTIVE
        return EdgeValidityResult(
            passed=True, strategy_class=strategy_class,
            state=EdgeState.ACTIVE.value, exposure_multiplier=1.0,
            pf=pf, wr=wr, expectancy=exp, trade_count=n,
        )

    # ── State getter ─────────────────────────────────────────

    def get_state(self, strategy_class: str) -> str:
        """Return current EdgeState value for a strategy class."""
        return self._states[strategy_class].state.value

    # ── State Mutation ──────────────────────────────────────

    def record_trade_outcome(
        self, strategy_class: str, is_win: bool, pnl_pct: float, now_ms: int,
    ) -> None:
        """Record trade outcome and evaluate transitions."""
        event = EdgeEvent(
            event_type="trade_outcome", timestamp_ms=now_ms,
            strategy_class=strategy_class, is_win=is_win, pnl_pct=pnl_pct,
        )
        self._event_log.append(event)
        self._apply_trade_outcome(event)
        self._evaluate_transitions(strategy_class, now_ms)

    # record_global_win REMOVED (v2.1): probe entry is now class-pure.
    # SUSPENDED → PROBE uses only class-local evidence (expectancy of
    # recent outcomes for the suspended class itself).

    # ── Internal ────────────────────────────────────────────

    def _apply_trade_outcome(self, event: EdgeEvent) -> None:
        ss = self._states[event.strategy_class]
        outcome = {"is_win": event.is_win, "pnl_pct": event.pnl_pct,
                    "ts": event.timestamp_ms}
        ss.outcomes.append(outcome)
        if ss.state == EdgeState.PROBE:
            ss.probe_outcomes.append(outcome)
            ss.probe_trade_count += 1

    def _evaluate_transitions(self, strategy_class: str, now_ms: int) -> None:
        """Evaluate and apply state transitions for a strategy class."""
        cfg = self.config
        ss = self._states[strategy_class]
        elapsed = now_ms - ss.entered_at_ms

        pf, wr, _ = self._compute_metrics(ss.outcomes, cfg.lookback_trades)

        if ss.state == EdgeState.ACTIVE:
            # ACTIVE → DEGRADED
            if len(ss.outcomes) >= cfg.lookback_trades:
                if pf < cfg.degrade_pf and wr < cfg.degrade_wr:
                    self._transition(strategy_class, EdgeState.DEGRADED, now_ms,
                                     f"PF={pf:.2f}<{cfg.degrade_pf} AND WR={wr:.1%}<{cfg.degrade_wr:.0%}")

        elif ss.state == EdgeState.DEGRADED:
            if elapsed < cfg.dwell_degraded_ms:
                return  # Dwell time not met

            # DEGRADED → ACTIVE (recovery, hysteresis)
            if pf >= cfg.recover_pf and wr >= cfg.recover_wr:
                self._transition(strategy_class, EdgeState.ACTIVE, now_ms,
                                 f"Recovery: PF={pf:.2f}>={cfg.recover_pf} AND WR={wr:.1%}>={cfg.recover_wr:.0%}")
                return

            # DEGRADED → SUSPENDED
            if pf < cfg.suspend_pf and wr < cfg.suspend_wr:
                self._transition(strategy_class, EdgeState.SUSPENDED, now_ms,
                                 f"PF={pf:.2f}<{cfg.suspend_pf} AND WR={wr:.1%}<{cfg.suspend_wr:.0%}")

        elif ss.state == EdgeState.SUSPENDED:
            if elapsed < cfg.dwell_suspended_ms:
                return  # Dwell time not met

            # SUSPENDED → PROBE: class-local recovery signal.
            # Requires enough recent outcomes AND expectancy above floor.
            if len(ss.outcomes) >= cfg.probe_entry_lookback:
                recent = ss.outcomes[-cfg.probe_entry_lookback:]
                recent_exp = sum(o["pnl_pct"] for o in recent) / len(recent)
                if recent_exp >= cfg.probe_entry_expectancy:
                    self._transition(strategy_class, EdgeState.PROBE, now_ms,
                                     f"Probe entry: class-local expectancy={recent_exp:.4f}>={cfg.probe_entry_expectancy}")

        elif ss.state == EdgeState.PROBE:
            if len(ss.probe_outcomes) < cfg.probe_lookback_trades:
                return  # Not enough probe data

            probe_pf, probe_wr, _ = self._compute_metrics(
                ss.probe_outcomes, cfg.probe_lookback_trades,
            )

            # PROBE → ACTIVE
            if probe_pf >= cfg.probe_recover_pf and probe_wr >= cfg.probe_recover_wr:
                self._transition(strategy_class, EdgeState.ACTIVE, now_ms,
                                 f"Probe success: PF={probe_pf:.2f} WR={probe_wr:.1%}")
                return

            # PROBE → SUSPENDED (probe failed)
            if probe_pf < cfg.probe_fail_pf or probe_wr < cfg.probe_fail_wr:
                self._transition(strategy_class, EdgeState.SUSPENDED, now_ms,
                                 f"Probe failed: PF={probe_pf:.2f} WR={probe_wr:.1%}")

    def _transition(self, strategy_class: str, to_state: EdgeState,
                    now_ms: int, reason: str) -> None:
        ss = self._states[strategy_class]
        from_state = ss.state

        event = EdgeEvent(
            event_type="state_transition", timestamp_ms=now_ms,
            strategy_class=strategy_class,
            from_state=from_state.value, to_state=to_state.value,
            reason=reason,
        )
        self._event_log.append(event)

        logger.info(
            "EdgeValidityMonitor: %s %s → %s: %s",
            strategy_class, from_state.value, to_state.value, reason,
        )

        ss.state = to_state
        ss.entered_at_ms = now_ms

        # Reset probe state on entering PROBE
        if to_state == EdgeState.PROBE:
            ss.probe_outcomes = []
            ss.probe_trade_count = 0

    @staticmethod
    def _compute_metrics(
        outcomes: List[dict], lookback: int,
    ) -> Tuple[float, float, float]:
        """Compute PF, WR, expectancy from recent outcomes."""
        if not outcomes:
            return 1.0, 0.5, 0.0  # Neutral defaults (no data)

        recent = outcomes[-lookback:]
        n = len(recent)
        wins = sum(1 for o in recent if o["is_win"])
        wr = wins / n

        gross_profit = sum(o["pnl_pct"] for o in recent if o["pnl_pct"] > 0)
        gross_loss = abs(sum(o["pnl_pct"] for o in recent if o["pnl_pct"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else (10.0 if gross_profit > 0 else 1.0)

        expectancy = sum(o["pnl_pct"] for o in recent) / n

        return pf, wr, expectancy

    # ── Snapshot ─────────────────────────────────────────────

    def state_snapshot(self) -> EdgeValiditySnapshot:
        states = []
        for sc, ss in sorted(self._states.items()):
            states.append((sc, ss.state.value, len(ss.outcomes)))
        return EdgeValiditySnapshot(
            class_states=tuple(states),
            event_count=len(self._event_log),
        )

    # ── Replay / Persistence ─────────────────────────────────

    def replay(self, events: List[EdgeEvent]) -> None:
        """Reconstruct state from event history."""
        self._event_log = []
        self._states = defaultdict(_StrategyState)
        for event in events:
            self._event_log.append(event)
            if event.event_type == "trade_outcome":
                self._apply_trade_outcome(event)
                self._evaluate_transitions(event.strategy_class, event.timestamp_ms)
            elif event.event_type == "state_transition":
                # State transitions are already applied via _evaluate_transitions
                # during trade_outcome replay. Direct transition events in the log
                # are for audit — we skip re-applying them to avoid double transitions.
                pass

    def truncate(self, now_ms: int) -> List[EdgeEvent]:
        """Remove events older than retention window."""
        cutoff_ms = now_ms - _RETENTION_WINDOW_MS
        archived = [e for e in self._event_log if e.timestamp_ms < cutoff_ms]
        kept = [e for e in self._event_log if e.timestamp_ms >= cutoff_ms]
        if archived:
            logger.info(
                "EdgeValidityMonitor.truncate: archiving %d events, keeping %d",
                len(archived), len(kept),
            )
            self.replay(kept)
        return archived

    def to_json(self) -> str:
        return json.dumps(
            {"version": _EVENT_LOG_VERSION,
             "events": [e.to_dict() for e in self._event_log]},
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, data: str, config: EdgeValidityConfig = None) -> EdgeValidityMonitor:
        """Restore from JSON. Fail-closed on corruption."""
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            raise EdgeValidityStateCorruptError(f"JSON corrupt: {e}") from e
        if not isinstance(parsed, dict):
            raise EdgeValidityStateCorruptError("Root must be object")
        if "version" not in parsed:
            raise EdgeValidityStateCorruptError("Missing 'version'")
        if parsed["version"] != _EVENT_LOG_VERSION:
            raise EdgeValidityStateCorruptError(f"Unknown version: {parsed['version']}")
        if "events" not in parsed or not isinstance(parsed["events"], list):
            raise EdgeValidityStateCorruptError("Missing or invalid 'events'")
        try:
            events = [EdgeEvent.from_dict(e) for e in parsed["events"]]
        except (KeyError, TypeError, ValueError) as e:
            raise EdgeValidityStateCorruptError(f"Invalid event: {e}") from e
        m = cls(config=config)
        m.replay(events)
        return m
