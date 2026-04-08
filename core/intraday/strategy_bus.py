# ============================================================
# NEXUS TRADER — StrategyBus  (Phase 4)
#
# Orchestrator of the two-stage intraday signal pipeline.
# Subscribes to DATA-layer candle topics, routes timeframe events
# to the correct stage (setup or trigger), manages pending setups,
# expires invalid setups/triggers, and publishes validated
# STRATEGY-layer events.
#
# Design invariants:
#   - Consumes ONLY DATA-layer topics (market.candle.*)
#   - Publishes ONLY STRATEGY-layer topics
#   - Does NOT call execution directly
#   - Does NOT bypass contracts
#   - Does NOT embed risk logic
#   - No PySide6 imports
#   - Thread-safe (all state under lock)
#   - Deterministic when given deterministic inputs
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from core.event_bus import Event, Topics, bus
from core.contracts import SignalLayer, check_topic_boundary
from core.intraday.base_strategy import BaseIntradayStrategy, RegimeInfo
from core.intraday.signal_contracts import (
    ContractViolation,
    SetupLifecycle,
    SetupSignal,
    TriggerLifecycle,
    TriggerSignal,
    validate_setup_signal,
    validate_trigger_signal,
)
from core.intraday.signal_expiry import (
    ExpiryReason,
    validate_signal_expiry,
)
from core.intraday.strategy_trace import (
    DecisionStage,
    strategy_trace_registry,
)

logger = logging.getLogger(__name__)


# ── Topic → timeframe mapping ────────────────────────────────
_TOPIC_TF_MAP = {
    Topics.CANDLE_1M: "1m",
    Topics.CANDLE_3M: "3m",
    Topics.CANDLE_5M: "5m",
    Topics.CANDLE_15M: "15m",
    Topics.CANDLE_1H: "1h",
}

# ── Metrics ───────────────────────────────────────────────────

@dataclass
class StrategyBusMetrics:
    """Runtime metrics for the StrategyBus."""
    candles_received: int = 0
    setups_evaluated: int = 0
    setups_qualified: int = 0
    setups_rejected: int = 0
    setups_expired: int = 0
    triggers_evaluated: int = 0
    triggers_fired: int = 0
    triggers_rejected: int = 0
    signals_expired: int = 0
    signals_forwarded: int = 0
    processing_latency_ms: float = 0.0
    _latency_samples: list = field(default_factory=list)

    def record_latency(self, ms: float) -> None:
        self._latency_samples.append(ms)
        if len(self._latency_samples) > 100:
            self._latency_samples = self._latency_samples[-100:]
        self.processing_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    def snapshot(self) -> dict:
        return {
            "candles_received": self.candles_received,
            "setups_evaluated": self.setups_evaluated,
            "setups_qualified": self.setups_qualified,
            "setups_rejected": self.setups_rejected,
            "setups_expired": self.setups_expired,
            "triggers_evaluated": self.triggers_evaluated,
            "triggers_fired": self.triggers_fired,
            "triggers_rejected": self.triggers_rejected,
            "signals_expired": self.signals_expired,
            "signals_forwarded": self.signals_forwarded,
            "avg_processing_latency_ms": round(self.processing_latency_ms, 2),
        }


class StrategyBus:
    """
    Orchestrates the two-stage intraday signal pipeline.

    Lifecycle:
        1. Subscribes to DATA-layer candle topics
        2. On each candle event:
           a. Routes to setup evaluation if TF matches any strategy's setup_timeframe
           b. Routes to trigger evaluation if TF matches any strategy's trigger_timeframe
              AND there are pending setups for that symbol
        3. Manages pending setup lifecycle (creation, expiry, consumption)
        4. Validates all signals via contracts before publishing
        5. Publishes STRATEGY-layer events
    """

    def __init__(
        self,
        strategies: Optional[list[BaseIntradayStrategy]] = None,
        regime_provider: Optional[Callable[[str], RegimeInfo]] = None,
        candle_history_provider: Optional[Callable[[str, str, int], pd.DataFrame]] = None,
        event_bus=None,
        now_ms_fn: Optional[Callable[[], int]] = None,
    ):
        """
        Parameters
        ----------
        strategies : list of BaseIntradayStrategy instances
        regime_provider : callable(symbol) → RegimeInfo
        candle_history_provider : callable(symbol, timeframe, n_bars) → DataFrame
        event_bus : EventBus instance (defaults to global bus)
        now_ms_fn : callable() → int ms (defaults to wall clock; pass explicit for replay)
        """
        self._strategies = strategies or []
        self._regime_provider = regime_provider or self._default_regime
        self._candle_history = candle_history_provider or self._default_history
        self._bus = event_bus or bus
        self._now_ms = now_ms_fn or (lambda: int(time.time() * 1000))

        # Pending setups: {symbol: {setup_id: SetupSignal}}
        self._pending_setups: dict[str, dict[str, SetupSignal]] = defaultdict(dict)
        self._lock = threading.Lock()
        self._running = False
        self._metrics = StrategyBusMetrics()

        # Build routing tables
        self._setup_strategies: dict[str, list[BaseIntradayStrategy]] = defaultdict(list)
        self._trigger_strategies: dict[str, list[BaseIntradayStrategy]] = defaultdict(list)
        for strat in self._strategies:
            self._setup_strategies[strat.SETUP_TIMEFRAME].append(strat)
            self._trigger_strategies[strat.TRIGGER_TIMEFRAME].append(strat)

    @property
    def metrics(self) -> StrategyBusMetrics:
        return self._metrics

    @property
    def pending_setups(self) -> dict:
        """Read-only snapshot of pending setups."""
        with self._lock:
            return {
                sym: dict(setups) for sym, setups in self._pending_setups.items()
            }

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to DATA-layer candle topics."""
        if self._running:
            return
        self._running = True
        for topic in _TOPIC_TF_MAP:
            self._bus.subscribe(topic, self._on_candle)
        logger.info(
            "StrategyBus started with %d strategies: %s",
            len(self._strategies),
            [s.NAME for s in self._strategies],
        )

    def stop(self) -> None:
        """Unsubscribe and clear state."""
        self._running = False
        for topic in _TOPIC_TF_MAP:
            self._bus.unsubscribe(topic, self._on_candle)
        with self._lock:
            self._pending_setups.clear()
        logger.info("StrategyBus stopped")

    # ── Core event handler ────────────────────────────────────

    def _on_candle(self, event: Event) -> None:
        """Handle incoming candle event from DATA layer."""
        if not self._running:
            return

        t0 = time.time()
        self._metrics.candles_received += 1

        tf = _TOPIC_TF_MAP.get(event.topic)
        if not tf:
            return

        data = event.data or {}
        symbol = data.get("symbol", "")
        if not symbol:
            return

        # Get regime
        regime_info = self._regime_provider(symbol)

        # Expire stale setups before processing
        self._expire_stale_setups(symbol)

        # Stage A: Setup evaluation
        if tf in self._setup_strategies:
            self._evaluate_setups(symbol, tf, regime_info)

        # Stage B: Trigger evaluation
        if tf in self._trigger_strategies:
            self._evaluate_triggers(symbol, tf, regime_info)

        elapsed = (time.time() - t0) * 1000
        self._metrics.record_latency(elapsed)

    # ── Stage A: Setup Evaluation ─────────────────────────────

    def _evaluate_setups(self, symbol: str, tf: str,
                         regime_info: RegimeInfo) -> None:
        """Evaluate all setup-eligible strategies for this symbol/timeframe."""
        strategies = self._setup_strategies.get(tf, [])
        if not strategies:
            return

        df = self._candle_history(symbol, tf, 100)
        if df is None or len(df) < 20:
            return

        for strat in strategies:
            self._metrics.setups_evaluated += 1
            setup = strat.run_setup(symbol, df, regime_info)

            if setup is None:
                self._metrics.setups_rejected += 1
                continue

            if setup.lifecycle != SetupLifecycle.QUALIFIED:
                self._metrics.setups_rejected += 1
                continue

            # Store as pending
            with self._lock:
                existing = self._pending_setups[symbol]
                # Deduplicate: same strategy + direction replaces old setup
                for sid, old in list(existing.items()):
                    if (old.strategy_name == setup.strategy_name
                            and old.direction == setup.direction):
                        del existing[sid]
                existing[setup.setup_id] = setup

            self._metrics.setups_qualified += 1

            # Publish SETUP_QUALIFIED (STRATEGY layer)
            boundary_err = check_topic_boundary(
                Topics.SETUP_QUALIFIED, SignalLayer.STRATEGY
            )
            if boundary_err:
                logger.error("Topic boundary violation: %s", boundary_err)
                continue

            self._bus.publish(
                Topics.SETUP_QUALIFIED,
                data=setup.to_dict(),
                source="strategy_bus",
            )

    # ── Stage B: Trigger Evaluation ───────────────────────────

    def _evaluate_triggers(self, symbol: str, tf: str,
                           regime_info: RegimeInfo) -> None:
        """Evaluate triggers for all pending setups matching this timeframe."""
        with self._lock:
            setups = list(self._pending_setups.get(symbol, {}).values())

        if not setups:
            return

        strategies_for_tf = self._trigger_strategies.get(tf, [])
        if not strategies_for_tf:
            return

        df = self._candle_history(symbol, tf, 50)
        if df is None or len(df) < 5:
            return

        # Get current price for expiry checks
        current_price = float(df["close"].iloc[-1])

        for setup in setups:
            # Find the strategy that produced this setup
            strat = self._find_strategy(setup.strategy_name)
            if strat is None:
                continue
            if strat.TRIGGER_TIMEFRAME != tf:
                continue

            self._metrics.triggers_evaluated += 1
            trigger = strat.run_trigger(symbol, df, setup, regime_info)

            if trigger is None:
                self._metrics.triggers_rejected += 1
                continue

            if trigger.lifecycle != TriggerLifecycle.FIRED:
                self._metrics.triggers_rejected += 1
                continue

            # Validate signal expiry before forwarding
            expiry = validate_signal_expiry(
                trigger, current_price,
                now_ms=self._now_ms(),
            )
            if not expiry.is_valid:
                self._metrics.signals_expired += 1
                self._bus.publish(
                    Topics.SIGNAL_EXPIRED,
                    data={
                        "trigger_id": trigger.trigger_id,
                        "setup_id": setup.setup_id,
                        "symbol": symbol,
                        "strategy": trigger.strategy_name,
                        "reason": expiry.reason.value,
                        "detail": expiry.detail,
                        "age_ms": expiry.age_ms,
                        "drift_pct": expiry.drift_pct,
                    },
                    source="strategy_bus",
                )
                continue

            # Mark setup as consumed
            with self._lock:
                sym_setups = self._pending_setups.get(symbol, {})
                if setup.setup_id in sym_setups:
                    del sym_setups[setup.setup_id]
                    trace = strategy_trace_registry.get(setup.setup_id)
                    if trace:
                        trace.record_stage(
                            DecisionStage.SETUP_CONSUMED,
                            f"consumed by trigger {trigger.trigger_id}"
                        )

            self._metrics.triggers_fired += 1

            # Publish TRIGGER_FIRED (STRATEGY layer)
            boundary_err = check_topic_boundary(
                Topics.TRIGGER_FIRED, SignalLayer.STRATEGY
            )
            if boundary_err:
                logger.error("Topic boundary violation: %s", boundary_err)
                continue

            self._bus.publish(
                Topics.TRIGGER_FIRED,
                data=trigger.to_dict(),
                source="strategy_bus",
            )

            # Record forwarding in trace
            trace = strategy_trace_registry.get(trigger.trigger_id)
            if trace:
                trace.record_stage(DecisionStage.SIGNAL_FORWARDED)

            self._metrics.signals_forwarded += 1

    # ── Setup Expiry ──────────────────────────────────────────

    def _expire_stale_setups(self, symbol: str) -> None:
        """Remove setups that have exceeded their max_age_ms."""
        now = self._now_ms()
        with self._lock:
            sym_setups = self._pending_setups.get(symbol, {})
            expired_ids = []
            for sid, setup in sym_setups.items():
                age = now - setup.created_at_ms
                if setup.max_age_ms > 0 and age > setup.max_age_ms:
                    expired_ids.append(sid)

            for sid in expired_ids:
                setup = sym_setups.pop(sid)
                self._metrics.setups_expired += 1
                trace = strategy_trace_registry.get(sid)
                if trace:
                    trace.record_stage(
                        DecisionStage.SETUP_EXPIRED,
                        f"age {age}ms > max {setup.max_age_ms}ms",
                    )
                logger.info(
                    "SETUP EXPIRED: %s %s %s (age=%dms, max=%dms)",
                    setup.strategy_name, symbol, sid,
                    now - setup.created_at_ms, setup.max_age_ms,
                )

    # ── Helpers ───────────────────────────────────────────────

    def _find_strategy(self, name: str) -> Optional[BaseIntradayStrategy]:
        for strat in self._strategies:
            if strat.NAME == name:
                return strat
        return None

    @staticmethod
    def _default_regime(symbol: str) -> RegimeInfo:
        return RegimeInfo(label="uncertain", confidence=0.5, probs={})

    @staticmethod
    def _default_history(symbol: str, tf: str, n: int) -> Optional[pd.DataFrame]:
        return None

    # ── Programmatic API (for replay/testing) ─────────────────

    def inject_candle(self, symbol: str, timeframe: str,
                      candle: dict) -> None:
        """
        Inject a candle directly (bypassing EventBus).
        Used for deterministic replay and testing.
        """
        topic = None
        for t, tf in _TOPIC_TF_MAP.items():
            if tf == timeframe:
                topic = t
                break
        if not topic:
            return

        event = Event(
            topic=topic,
            data={"symbol": symbol, "candle": candle},
            source="replay",
        )
        self._on_candle(event)
