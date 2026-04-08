# ============================================================
# NEXUS TRADER — Strategy Replay  (Phase 4)
#
# Deterministic replay of candle streams through the strategy
# pipeline. Extends Phase 3 candle replay to cover setups and
# triggers.
#
# Guarantees:
#   - Same candle stream → same setup outcomes
#   - Same candle stream → same trigger outcomes
#   - Publication sequence is deterministic
#   - No wall-clock dependence (explicit now_ms_fn)
#   - No hidden mutable global state
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from core.intraday.base_strategy import BaseIntradayStrategy, RegimeInfo
from core.intraday.signal_contracts import SetupSignal, TriggerSignal
from core.intraday.strategy_bus import StrategyBus
from core.intraday.strategy_trace import strategy_trace_registry

logger = logging.getLogger(__name__)


@dataclass
class StrategyReplayResult:
    """Captures the outcome of replaying a candle stream through the strategy pipeline."""
    setups_qualified: list = field(default_factory=list)     # [SetupSignal.to_dict()]
    triggers_fired: list = field(default_factory=list)        # [TriggerSignal.to_dict()]
    signals_expired: list = field(default_factory=list)       # [expiry event dicts]
    event_sequence: list = field(default_factory=list)        # [(event_type, symbol, id, ts)]
    candles_processed: int = 0

    def matches(self, other: StrategyReplayResult) -> bool:
        """Binary equality check for determinism verification."""
        if self.candles_processed != other.candles_processed:
            return False
        if len(self.setups_qualified) != len(other.setups_qualified):
            return False
        if len(self.triggers_fired) != len(other.triggers_fired):
            return False
        # Compare setup IDs in order
        for a, b in zip(self.setups_qualified, other.setups_qualified):
            if a.get("setup_id") != b.get("setup_id"):
                return False
        # Compare trigger IDs in order
        for a, b in zip(self.triggers_fired, other.triggers_fired):
            if a.get("trigger_id") != b.get("trigger_id"):
                return False
        return True

    def diff(self, other: StrategyReplayResult) -> list[str]:
        """Diagnostic differences between two replay results."""
        diffs = []
        if self.candles_processed != other.candles_processed:
            diffs.append(
                f"candles_processed: {self.candles_processed} vs {other.candles_processed}"
            )
        if len(self.setups_qualified) != len(other.setups_qualified):
            diffs.append(
                f"setups_qualified count: {len(self.setups_qualified)} vs {len(other.setups_qualified)}"
            )
        if len(self.triggers_fired) != len(other.triggers_fired):
            diffs.append(
                f"triggers_fired count: {len(self.triggers_fired)} vs {len(other.triggers_fired)}"
            )
        for i, (a, b) in enumerate(zip(self.setups_qualified, other.setups_qualified)):
            if a.get("setup_id") != b.get("setup_id"):
                diffs.append(f"setup[{i}] id mismatch: {a.get('setup_id')} vs {b.get('setup_id')}")
        for i, (a, b) in enumerate(zip(self.triggers_fired, other.triggers_fired)):
            if a.get("trigger_id") != b.get("trigger_id"):
                diffs.append(f"trigger[{i}] id mismatch: {a.get('trigger_id')} vs {b.get('trigger_id')}")
        return diffs


@dataclass
class ReplayFixture:
    """
    A fixture of candle data for deterministic replay.

    Each entry is a dict with:
      - symbol: str
      - timeframe: str
      - candles: list of candle dicts (with timestamp, open, high, low, close, volume)
    """
    entries: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def total_candles(self) -> int:
        return sum(len(e.get("candles", [])) for e in self.entries)


def replay_strategies(
    fixture: ReplayFixture,
    strategies: list[BaseIntradayStrategy],
    regime_info: Optional[RegimeInfo] = None,
    start_ms: int = 1_000_000_000_000,
) -> StrategyReplayResult:
    """
    Replay a candle fixture through the strategy pipeline.

    Creates a fresh StrategyBus with no wall-clock dependence,
    feeds candles in timestamp order, captures all outputs.

    Parameters
    ----------
    fixture : ReplayFixture
        Candle data to replay
    strategies : list of strategy instances
    regime_info : RegimeInfo (constant for replay)
    start_ms : int
        Simulated start time (ms)

    Returns
    -------
    StrategyReplayResult with all setups, triggers, and events.
    """
    if regime_info is None:
        regime_info = RegimeInfo(label="uncertain", confidence=0.5, probs={})

    result = StrategyReplayResult()

    # Build candle history from fixture entries
    # Group by (symbol, timeframe) for history provider
    history_store: dict[tuple[str, str], pd.DataFrame] = {}
    all_events: list[tuple[int, str, str, dict]] = []  # (ts, symbol, tf, candle)

    for entry in fixture.entries:
        symbol = entry["symbol"]
        tf = entry["timeframe"]
        candles = entry.get("candles", [])
        key = (symbol, tf)

        rows = []
        for c in candles:
            rows.append(c)
            all_events.append((c["timestamp"], symbol, tf, c))

        if rows:
            df = pd.DataFrame(rows)
            history_store[key] = df

    # Sort all events by timestamp for deterministic ordering
    all_events.sort(key=lambda x: (x[0], x[1], x[2]))

    # Simulated clock
    sim_clock = [start_ms]

    def now_ms_fn() -> int:
        return sim_clock[0]

    def regime_provider(symbol: str) -> RegimeInfo:
        return regime_info

    # History provider returns accumulated candles up to current sim time
    candle_accumulators: dict[tuple[str, str], list[dict]] = {}

    def history_provider(symbol: str, tf: str, n: int) -> Optional[pd.DataFrame]:
        key = (symbol, tf)
        acc = candle_accumulators.get(key)
        if not acc:
            return None
        recent = acc[-n:] if len(acc) > n else acc
        return pd.DataFrame(recent)

    # Create isolated StrategyBus (no EventBus subscription)
    from core.event_bus import EventBus
    replay_bus = EventBus()

    # Capture outputs
    from core.event_bus import Topics

    def on_setup(event):
        result.setups_qualified.append(event.data)
        result.event_sequence.append(
            ("setup_qualified", event.data.get("symbol"), event.data.get("setup_id"), sim_clock[0])
        )

    def on_trigger(event):
        result.triggers_fired.append(event.data)
        result.event_sequence.append(
            ("trigger_fired", event.data.get("symbol"), event.data.get("trigger_id"), sim_clock[0])
        )

    def on_expiry(event):
        result.signals_expired.append(event.data)
        result.event_sequence.append(
            ("signal_expired", event.data.get("symbol"), event.data.get("trigger_id"), sim_clock[0])
        )

    replay_bus.subscribe(Topics.SETUP_QUALIFIED, on_setup)
    replay_bus.subscribe(Topics.TRIGGER_FIRED, on_trigger)
    replay_bus.subscribe(Topics.SIGNAL_EXPIRED, on_expiry)

    strat_bus = StrategyBus(
        strategies=strategies,
        regime_provider=regime_provider,
        candle_history_provider=history_provider,
        event_bus=replay_bus,
        now_ms_fn=now_ms_fn,
    )
    strat_bus.start()

    # Clear trace registry for clean replay
    strategy_trace_registry.clear()

    # Feed events in order
    for ts, symbol, tf, candle in all_events:
        sim_clock[0] = ts

        # Accumulate candle in history
        key = (symbol, tf)
        if key not in candle_accumulators:
            candle_accumulators[key] = []
        candle_accumulators[key].append(candle)

        # Inject into StrategyBus
        strat_bus.inject_candle(symbol, tf, candle)
        result.candles_processed += 1

    strat_bus.stop()
    return result
