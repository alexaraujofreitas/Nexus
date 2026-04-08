# ============================================================
# NEXUS TRADER — Global Trade Filter  (Phase 5B Wave 1 v2)
#
# Anti-overtrading filter with DETERMINISTIC, REPLAYABLE state.
#
# Design invariants:
#   - FilterState is reconstructable from event history
#   - FilterStateSnapshot is frozen (for pipeline context)
#   - All state mutations go through record_trade() / record_outcome()
#   - No wall-clock defaults: now_ms is ALWAYS required
#   - State can be serialised/deserialised for restart persistence
#   - Thread-safety: NOT thread-safe (single-threaded pipeline)
#
# Filter gates (all must pass, evaluated in order):
#   1. Global daily limit
#   2. Per-class daily limit
#   3. Regime-based throttling (uncertain/choppy)
#   4. Regime TQS floor (uncertain + low TQS)
#   5. Chop detection cooldown
#   6. Loss-streak cooldown
#   7. Per-symbol cooldown
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Milliseconds per day ─────────────────────────────────────
_MS_PER_DAY = 86_400_000

# ── Event log retention ──────────────────────────────────────
# Retention window: 7 days of events are kept in-memory.
# Events older than this are archived (returned by truncate())
# and removed from the active log.
# Replay guarantees: state is fully reconstructable from active
# events + config. Archived events are for audit only.
# Version: event format version=1. Migration path: if version
# changes, from_json() raises FilterStateCorruptError and
# requires explicit migration before loading.
_RETENTION_WINDOW_MS = 7 * _MS_PER_DAY
_EVENT_LOG_VERSION = 1


class FilterStateCorruptError(Exception):
    """
    Raised when persisted filter state cannot be safely restored.

    Fail-closed: the caller MUST handle this explicitly.
    Silent reset to empty state is NEVER acceptable.
    """
    pass


@dataclass(frozen=True)
class GlobalFilterConfig:
    """Configuration for anti-overtrading filter."""
    # Daily trade limits
    max_trades_per_day_global: int = 20
    max_trades_per_day_per_class: int = 8

    # Regime throttling
    uncertain_regime_labels: tuple = ("uncertain", "choppy", "mixed")
    uncertain_regime_max_trades: int = 3
    uncertain_regime_tqs_floor: float = 0.50

    # Chop detection (rolling window)
    chop_lookback_trades: int = 10
    chop_wr_floor: float = 0.30
    chop_cooldown_ms: int = 1_800_000

    # Loss-streak throttling
    loss_streak_warning: int = 3
    loss_streak_cooldown: int = 5
    loss_streak_cooldown_ms: int = 3_600_000

    # Per-symbol cooldown
    symbol_cooldown_ms: int = 300_000


# ── Event log for replay ────────────────────────────────────

@dataclass(frozen=True)
class FilterEvent:
    """
    Immutable record of a state-mutating event.

    Source of truth for replay: the entire FilterState can be
    reconstructed by replaying all events in order.
    """
    event_type: str           # "trade" or "outcome"
    timestamp_ms: int         # Deterministic timestamp (no wall-clock)

    # trade event fields
    strategy_class: str = ""
    symbol: str = ""
    regime: str = ""

    # outcome event fields
    is_win: Optional[bool] = None

    def to_dict(self) -> dict:
        d = {"event_type": self.event_type, "timestamp_ms": self.timestamp_ms}
        if self.event_type == "trade":
            d.update({"strategy_class": self.strategy_class,
                       "symbol": self.symbol, "regime": self.regime})
        elif self.event_type == "outcome":
            d["is_win"] = self.is_win
        return d

    @classmethod
    def from_dict(cls, d: dict) -> FilterEvent:
        return cls(
            event_type=d["event_type"],
            timestamp_ms=d["timestamp_ms"],
            strategy_class=d.get("strategy_class", ""),
            symbol=d.get("symbol", ""),
            regime=d.get("regime", ""),
            is_win=d.get("is_win"),
        )


# ── Frozen state snapshot ───────────────────────────────────

@dataclass(frozen=True)
class FilterStateSnapshot:
    """
    Immutable snapshot of filter state at a point in time.

    Used in PipelineContext for audit trail and replay validation.
    """
    trades_today_global: int
    trades_per_class: tuple         # tuple of (class_name, count) pairs
    trades_per_regime: tuple        # tuple of (regime, count) pairs
    consecutive_losses: int
    recent_outcomes: tuple          # tuple of bool
    chop_cooldown_until_ms: int
    loss_cooldown_until_ms: int
    last_reset_day: int
    event_count: int                # total events replayed

    def to_dict(self) -> dict:
        return {
            "trades_today_global": self.trades_today_global,
            "trades_per_class": dict(self.trades_per_class),
            "trades_per_regime": dict(self.trades_per_regime),
            "consecutive_losses": self.consecutive_losses,
            "recent_outcomes": list(self.recent_outcomes),
            "chop_cooldown_until_ms": self.chop_cooldown_until_ms,
            "loss_cooldown_until_ms": self.loss_cooldown_until_ms,
            "last_reset_day": self.last_reset_day,
            "event_count": self.event_count,
        }


# ── Mutable internal state ──────────────────────────────────

class _FilterState:
    """
    Mutable filter state. Internal to GlobalTradeFilter.

    Canonical source: reconstructable from event_log.
    """
    __slots__ = (
        "trades_today_global", "trades_today_per_class",
        "trades_today_per_regime", "consecutive_losses",
        "recent_outcomes", "chop_cooldown_until_ms",
        "loss_cooldown_until_ms", "symbol_last_trade_ms",
        "last_reset_day",
    )

    def __init__(self):
        self.trades_today_global: int = 0
        self.trades_today_per_class: Dict[str, int] = defaultdict(int)
        self.trades_today_per_regime: Dict[str, int] = defaultdict(int)
        self.consecutive_losses: int = 0
        self.recent_outcomes: List[bool] = []
        self.chop_cooldown_until_ms: int = 0
        self.loss_cooldown_until_ms: int = 0
        self.symbol_last_trade_ms: Dict[str, int] = defaultdict(int)
        self.last_reset_day: int = 0

    def snapshot(self, event_count: int) -> FilterStateSnapshot:
        """Create frozen snapshot for pipeline context."""
        return FilterStateSnapshot(
            trades_today_global=self.trades_today_global,
            trades_per_class=tuple(sorted(self.trades_today_per_class.items())),
            trades_per_regime=tuple(sorted(self.trades_today_per_regime.items())),
            consecutive_losses=self.consecutive_losses,
            recent_outcomes=tuple(self.recent_outcomes),
            chop_cooldown_until_ms=self.chop_cooldown_until_ms,
            loss_cooldown_until_ms=self.loss_cooldown_until_ms,
            last_reset_day=self.last_reset_day,
            event_count=event_count,
        )


@dataclass(frozen=True)
class FilterResult:
    """Immutable result of global filter evaluation."""
    passed: bool
    reason: str = ""
    gate: str = ""

    def to_dict(self) -> dict:
        return {
            "filter_passed": self.passed,
            "filter_reason": self.reason,
            "filter_gate": self.gate,
        }


class GlobalTradeFilter:
    """
    Deterministic, replayable anti-overtrading filter.

    State management:
      - All state mutations go through record_trade() / record_outcome()
      - Every mutation appends to event_log (source of truth)
      - State can be reconstructed from event_log via replay()
      - State can be persisted/restored for restart via to_json()/from_json()

    Concurrency: NOT thread-safe. Accessed only from the single-threaded
    ProcessingEngine.process() path.
    """

    def __init__(self, config: GlobalFilterConfig = None):
        self.config = config or GlobalFilterConfig()
        self._state = _FilterState()
        self._event_log: List[FilterEvent] = []
        logger.info(
            "GlobalTradeFilter initialized: max_daily=%d, max_per_class=%d, "
            "chop_wr_floor=%.2f, loss_streak_cooldown=%d",
            self.config.max_trades_per_day_global,
            self.config.max_trades_per_day_per_class,
            self.config.chop_wr_floor,
            self.config.loss_streak_cooldown,
        )

    @property
    def event_log(self) -> List[FilterEvent]:
        """Read-only access to event log for replay validation."""
        return list(self._event_log)

    @property
    def event_count(self) -> int:
        return len(self._event_log)

    def state_snapshot(self) -> FilterStateSnapshot:
        """Return frozen snapshot of current state."""
        return self._state.snapshot(len(self._event_log))

    # ── Evaluation (read-only, no state mutation) ────────────

    def evaluate(
        self,
        strategy_class: str,
        symbol: str,
        regime: str,
        tqs_score: Optional[float],
        now_ms: int,
    ) -> FilterResult:
        """
        Evaluate whether a trade should be allowed.

        This is a READ-ONLY operation — it does NOT mutate state.
        State mutation happens only via record_trade()/record_outcome().

        Parameters
        ----------
        strategy_class : str
            Strategy class value
        symbol : str
            Trading symbol
        regime : str
            Current market regime label
        tqs_score : float or None
            TQS score (0.0–1.0), or None if TQS scorer is absent.
            When None, TQS-dependent gates (regime_tqs_floor) are
            explicitly skipped — no NaN, no silent defaults.
        now_ms : int
            Current time in ms. REQUIRED — no wall-clock fallback.

        Returns
        -------
        FilterResult
        """
        cfg = self.config

        # Daily reset check (idempotent, safe in read path)
        self._maybe_reset_daily(now_ms)

        # Gate 1: Global daily limit
        if self._state.trades_today_global >= cfg.max_trades_per_day_global:
            return FilterResult(
                passed=False,
                reason=f"Global daily limit reached: {self._state.trades_today_global}/{cfg.max_trades_per_day_global}",
                gate="daily_limit_global",
            )

        # Gate 2: Per-class daily limit
        class_count = self._state.trades_today_per_class[strategy_class]
        if class_count >= cfg.max_trades_per_day_per_class:
            return FilterResult(
                passed=False,
                reason=f"Class {strategy_class} daily limit: {class_count}/{cfg.max_trades_per_day_per_class}",
                gate="daily_limit_class",
            )

        # Gate 3+4: Regime-based throttling
        regime_lower = regime.lower()
        if regime_lower in cfg.uncertain_regime_labels:
            regime_count = self._state.trades_today_per_regime[regime_lower]
            if regime_count >= cfg.uncertain_regime_max_trades:
                return FilterResult(
                    passed=False,
                    reason=f"Uncertain regime '{regime}' trade limit: {regime_count}/{cfg.uncertain_regime_max_trades}",
                    gate="regime_throttle",
                )
            # Gate 4: TQS floor — only when TQS score is available.
            # When tqs_score is None (TQS scorer absent), this gate
            # is explicitly disabled. No NaN control flow.
            if tqs_score is not None and tqs_score < cfg.uncertain_regime_tqs_floor:
                return FilterResult(
                    passed=False,
                    reason=f"TQS {tqs_score:.3f} below uncertain-regime floor {cfg.uncertain_regime_tqs_floor:.2f}",
                    gate="regime_tqs_floor",
                )

        # Gate 5: Chop detection cooldown
        if now_ms < self._state.chop_cooldown_until_ms:
            remaining = (self._state.chop_cooldown_until_ms - now_ms) / 1000
            return FilterResult(
                passed=False,
                reason=f"Chop cooldown active ({remaining:.0f}s remaining)",
                gate="chop_cooldown",
            )

        # Gate 6: Loss-streak cooldown
        if now_ms < self._state.loss_cooldown_until_ms:
            remaining = (self._state.loss_cooldown_until_ms - now_ms) / 1000
            return FilterResult(
                passed=False,
                reason=f"Loss-streak cooldown active ({remaining:.0f}s remaining)",
                gate="loss_cooldown",
            )

        # Gate 7: Per-symbol cooldown
        last_trade = self._state.symbol_last_trade_ms.get(symbol, 0)
        if now_ms - last_trade < cfg.symbol_cooldown_ms:
            remaining = (cfg.symbol_cooldown_ms - (now_ms - last_trade)) / 1000
            return FilterResult(
                passed=False,
                reason=f"Symbol {symbol} cooldown ({remaining:.0f}s remaining)",
                gate="symbol_cooldown",
            )

        logger.debug(
            "GlobalTradeFilter: PASSED class=%s symbol=%s regime=%s "
            "daily_global=%d daily_class=%d",
            strategy_class, symbol, regime,
            self._state.trades_today_global, class_count,
        )
        return FilterResult(passed=True)

    # ── State mutations (append to event log) ────────────────

    def record_trade(
        self,
        strategy_class: str,
        symbol: str,
        regime: str,
        now_ms: int,
    ) -> None:
        """
        Record that a trade was executed.

        Appends to event log and updates state. now_ms is REQUIRED.
        """
        event = FilterEvent(
            event_type="trade",
            timestamp_ms=now_ms,
            strategy_class=strategy_class,
            symbol=symbol,
            regime=regime,
        )
        self._event_log.append(event)
        self._apply_trade_event(event)

        logger.debug(
            "GlobalTradeFilter.record_trade: class=%s symbol=%s daily_global=%d",
            strategy_class, symbol, self._state.trades_today_global,
        )

    def record_outcome(
        self,
        is_win: bool,
        now_ms: int,
    ) -> None:
        """
        Record trade outcome for chop/loss-streak detection.

        Appends to event log and updates state. now_ms is REQUIRED.
        """
        event = FilterEvent(
            event_type="outcome",
            timestamp_ms=now_ms,
            is_win=is_win,
        )
        self._event_log.append(event)
        self._apply_outcome_event(event)

    # ── Internal state application ───────────────────────────

    def _apply_trade_event(self, event: FilterEvent) -> None:
        """Apply a trade event to mutable state."""
        self._maybe_reset_daily(event.timestamp_ms)
        self._state.trades_today_global += 1
        self._state.trades_today_per_class[event.strategy_class] += 1
        self._state.trades_today_per_regime[event.regime.lower()] += 1
        self._state.symbol_last_trade_ms[event.symbol] = event.timestamp_ms

    def _apply_outcome_event(self, event: FilterEvent) -> None:
        """Apply an outcome event to mutable state."""
        cfg = self.config

        self._state.recent_outcomes.append(event.is_win)
        if len(self._state.recent_outcomes) > cfg.chop_lookback_trades:
            self._state.recent_outcomes = self._state.recent_outcomes[-cfg.chop_lookback_trades:]

        if event.is_win:
            self._state.consecutive_losses = 0
        else:
            self._state.consecutive_losses += 1

        # Chop detection
        if len(self._state.recent_outcomes) >= cfg.chop_lookback_trades:
            wins = sum(1 for x in self._state.recent_outcomes if x)
            wr = wins / len(self._state.recent_outcomes)
            if wr < cfg.chop_wr_floor:
                self._state.chop_cooldown_until_ms = (
                    event.timestamp_ms + cfg.chop_cooldown_ms
                )
                logger.warning(
                    "GlobalTradeFilter: chop detected WR=%.1f%% < floor=%.1f%%",
                    wr * 100, cfg.chop_wr_floor * 100,
                )

        # Loss-streak cooldown
        if self._state.consecutive_losses >= cfg.loss_streak_cooldown:
            self._state.loss_cooldown_until_ms = (
                event.timestamp_ms + cfg.loss_streak_cooldown_ms
            )
            logger.warning(
                "GlobalTradeFilter: loss streak %d >= %d",
                self._state.consecutive_losses, cfg.loss_streak_cooldown,
            )

    def _maybe_reset_daily(self, now_ms: int) -> None:
        """Reset daily counters if UTC day changed."""
        current_day = now_ms // _MS_PER_DAY
        if current_day > self._state.last_reset_day:
            logger.info(
                "GlobalTradeFilter: daily reset (day %d -> %d), prior trades=%d",
                self._state.last_reset_day, current_day,
                self._state.trades_today_global,
            )
            self._state.trades_today_global = 0
            self._state.trades_today_per_class.clear()
            self._state.trades_today_per_regime.clear()
            self._state.last_reset_day = current_day

    # ── Replay / Persistence ─────────────────────────────────

    def replay(self, events: List[FilterEvent]) -> None:
        """
        Reconstruct state from an ordered event history.

        Replaces current state entirely. Used for:
          - Restart recovery (deserialise event log, replay)
          - Replay validation (prove determinism)

        Parameters
        ----------
        events : list of FilterEvent
            Ordered event history. Must be in chronological order.
        """
        self._state = _FilterState()
        self._event_log = []
        for event in events:
            self._event_log.append(event)
            if event.event_type == "trade":
                self._apply_trade_event(event)
            elif event.event_type == "outcome":
                self._apply_outcome_event(event)

    def truncate(self, now_ms: int) -> List[FilterEvent]:
        """
        Remove events older than the retention window and return them.

        Retention policy:
          - Events within _RETENTION_WINDOW_MS (7 days) are kept
          - Older events are removed from the active log and returned
            as an archived batch for the caller to persist if desired
          - State is reconstructed from remaining events via replay()
          - This is safe because all stateful fields (cooldowns, streaks)
            are bounded by shorter windows than 7 days

        Returns
        -------
        List[FilterEvent] : archived (removed) events, chronologically ordered
        """
        cutoff_ms = now_ms - _RETENTION_WINDOW_MS
        archived = [e for e in self._event_log if e.timestamp_ms < cutoff_ms]
        kept = [e for e in self._event_log if e.timestamp_ms >= cutoff_ms]

        if archived:
            logger.info(
                "GlobalTradeFilter.truncate: archiving %d events older than %d ms, "
                "keeping %d events",
                len(archived), cutoff_ms, len(kept),
            )
            # Replay from kept events to rebuild state
            self.replay(kept)

        return archived

    def to_json(self) -> str:
        """
        Serialise event log to JSON for restart persistence.

        The event log is the source of truth — state is derived.
        """
        return json.dumps(
            {"version": _EVENT_LOG_VERSION, "events": [e.to_dict() for e in self._event_log]},
            sort_keys=True,
        )

    @classmethod
    def from_json(
        cls, data: str, config: GlobalFilterConfig = None,
    ) -> GlobalTradeFilter:
        """
        Restore filter from persisted JSON event log.

        FAIL-CLOSED RECOVERY:
          - Corrupt/unparseable JSON → raises FilterStateCorruptError
          - Missing "version" key → raises FilterStateCorruptError
          - Unknown version → raises FilterStateCorruptError
          - Missing "events" key → raises FilterStateCorruptError
          - Invalid event data → raises FilterStateCorruptError

        The caller (OrchestratorEngine or startup code) MUST handle
        FilterStateCorruptError and decide whether to:
          (a) Start fresh (losing filter history), or
          (b) Enter DEGRADED mode (block all trades until operator ack)

        There is NO silent reset to empty state.
        """
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            raise FilterStateCorruptError(
                f"Filter state JSON is corrupt/unparseable: {e}"
            ) from e

        if not isinstance(parsed, dict):
            raise FilterStateCorruptError(
                f"Filter state JSON root must be object, got {type(parsed).__name__}"
            )

        if "version" not in parsed:
            raise FilterStateCorruptError(
                "Filter state JSON missing required 'version' key"
            )

        version = parsed["version"]
        if version != 1:
            raise FilterStateCorruptError(
                f"Unknown filter state version: {version}. "
                f"Expected version 1. Cannot safely restore."
            )

        if "events" not in parsed:
            raise FilterStateCorruptError(
                "Filter state JSON missing required 'events' key"
            )

        if not isinstance(parsed["events"], list):
            raise FilterStateCorruptError(
                f"Filter state 'events' must be list, got {type(parsed['events']).__name__}"
            )

        try:
            events = [FilterEvent.from_dict(e) for e in parsed["events"]]
        except (KeyError, TypeError, ValueError) as e:
            raise FilterStateCorruptError(
                f"Filter state event data is invalid: {e}"
            ) from e

        f = cls(config=config)
        f.replay(events)
        return f
