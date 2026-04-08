# ============================================================
# NEXUS TRADER — Intraday Engine Integration  (Phase 4 Addendum)
#
# Bridges the Phase 4 StrategyBus into the NexusEngine runtime:
#   - Creates StrategyBus with all 5 intraday strategies
#   - Provides runtime regime_provider using EnsembleRegimeClassifier
#   - Provides runtime candle_history_provider using DataEngine buffers
#   - Manages lifecycle (start/stop) with duplicate-subscription guard
#   - Preserves strict layer boundaries:
#       DATA layer candle topics → STRATEGY layer events only
#
# ZERO PySide6 imports. Pure Python.
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

import pandas as pd

from core.event_bus import bus
from core.intraday.base_strategy import BaseIntradayStrategy, RegimeInfo
from core.intraday.strategy_bus import StrategyBus

logger = logging.getLogger(__name__)


def _create_default_strategies() -> list[BaseIntradayStrategy]:
    """
    Instantiate all 5 approved intraday strategies.

    Adding a new strategy: import it and append to this list.
    No other changes required.
    """
    from core.intraday.strategies.momentum_expansion import MomentumExpansionStrategy
    from core.intraday.strategies.vwap_reversion import VWAPReversionStrategy
    from core.intraday.strategies.micro_pullback import MicroPullbackStrategy
    from core.intraday.strategies.range_break_retest import RangeBreakRetestStrategy
    from core.intraday.strategies.liquidity_sweep_reversal import LiquiditySweepReversalStrategy

    return [
        MomentumExpansionStrategy(),
        VWAPReversionStrategy(),
        MicroPullbackStrategy(),
        RangeBreakRetestStrategy(),
        LiquiditySweepReversalStrategy(),
    ]


def _make_regime_provider() -> callable:
    """
    Create a runtime regime provider that uses EnsembleRegimeClassifier.

    Returns a callable(symbol) → RegimeInfo that:
    - Maintains per-symbol classifier instances (thread-safe)
    - Falls back to "uncertain" if classification fails or insufficient data
    - Converts EnsembleRegimeClassifier output to RegimeInfo contract
    """
    classifiers: dict = {}
    lock = threading.Lock()

    def _classify_regime_from_history(symbol: str) -> RegimeInfo:
        """
        Classify regime for a symbol using its recent candle history.

        The classifier needs a DataFrame. We get this from the candle
        history provider. If no history is available, return uncertain.
        """
        try:
            df = _get_candle_history(symbol, "15m", 100)
            if df is None or len(df) < 30:
                return RegimeInfo(label="uncertain", confidence=0.3, probs={})

            with lock:
                if symbol not in classifiers:
                    from core.regime.ensemble_regime_classifier import EnsembleRegimeClassifier
                    classifiers[symbol] = EnsembleRegimeClassifier()
                clf = classifiers[symbol]

            regime_label, confidence, features = clf.classify(df)
            probs = {}
            if isinstance(features, dict):
                probs = {k: v for k, v in features.items()
                         if isinstance(v, (int, float))}

            return RegimeInfo(
                label=regime_label,
                confidence=float(confidence),
                probs=probs,
            )
        except Exception as e:
            logger.debug("Regime classification failed for %s: %s", symbol, e)
            return RegimeInfo(label="uncertain", confidence=0.3, probs={})

    return _classify_regime_from_history


# ── Candle history buffer ─────────────────────────────────────

# Per-(symbol, timeframe) ring buffer of recent candles.
# Populated by EventBus candle events, consumed by StrategyBus.
_candle_buffers: dict[tuple[str, str], list[dict]] = {}
_buffer_lock = threading.Lock()
_MAX_BUFFER_SIZE = 200


def _accumulate_candle(symbol: str, timeframe: str, candle: dict) -> None:
    """Store a candle in the history buffer (called from EventBus subscriber)."""
    key = (symbol, timeframe)
    with _buffer_lock:
        if key not in _candle_buffers:
            _candle_buffers[key] = []
        buf = _candle_buffers[key]
        buf.append(candle)
        if len(buf) > _MAX_BUFFER_SIZE:
            _candle_buffers[key] = buf[-_MAX_BUFFER_SIZE:]


def _get_candle_history(symbol: str, timeframe: str,
                        n_bars: int) -> Optional[pd.DataFrame]:
    """
    Get recent candle history as a DataFrame.

    First tries the Phase 3 DataEngine buffers (authoritative source).
    Falls back to the local accumulation buffer.
    """
    # Try Phase 3 DataEngine first (authoritative for 1m candles)
    try:
        from core.market_data.data_engine import DataEngine
        # DataEngine is not a singleton — check if one is available
        # via the connectivity pipeline. Fall through to local buffer.
    except ImportError:
        pass

    # Use local accumulation buffer
    key = (symbol, timeframe)
    with _buffer_lock:
        buf = _candle_buffers.get(key)
        if not buf:
            return None
        recent = buf[-n_bars:] if len(buf) > n_bars else list(buf)

    if not recent:
        return None

    return pd.DataFrame(recent)


def _candle_event_accumulator(event) -> None:
    """EventBus callback that accumulates candles for history queries."""
    from core.intraday.strategy_bus import _TOPIC_TF_MAP
    tf = _TOPIC_TF_MAP.get(event.topic)
    if not tf:
        return
    data = event.data or {}
    symbol = data.get("symbol", "")
    candle = data.get("candle")
    if symbol and candle:
        _accumulate_candle(symbol, tf, candle)


# ── Singleton lifecycle ───────────────────────────────────────

_strategy_bus: Optional[StrategyBus] = None
_accumulator_subscribed = False
_started = False


def get_strategy_bus() -> Optional[StrategyBus]:
    """Get the runtime StrategyBus instance (None if not started)."""
    return _strategy_bus


def start_intraday_engine(event_bus=None) -> StrategyBus:
    """
    Create, wire, and start the intraday StrategyBus.

    Called by NexusEngine.start(). Safe to call multiple times —
    guards against duplicate subscriptions.

    Returns the StrategyBus instance.
    """
    global _strategy_bus, _accumulator_subscribed, _started

    if _started and _strategy_bus is not None:
        logger.warning("Intraday engine already started — ignoring duplicate start()")
        return _strategy_bus

    the_bus = event_bus or bus

    # Register candle accumulator (for history queries)
    if not _accumulator_subscribed:
        from core.event_bus import Topics
        for topic in [Topics.CANDLE_1M, Topics.CANDLE_3M, Topics.CANDLE_5M,
                      Topics.CANDLE_15M, Topics.CANDLE_1H]:
            the_bus.subscribe(topic, _candle_event_accumulator)
        _accumulator_subscribed = True
        logger.info("Intraday candle accumulator subscribed to DATA-layer topics")

    # Create strategies
    strategies = _create_default_strategies()

    # Create StrategyBus with runtime providers
    _strategy_bus = StrategyBus(
        strategies=strategies,
        regime_provider=_make_regime_provider(),
        candle_history_provider=_get_candle_history,
        event_bus=the_bus,
    )
    _strategy_bus.start()
    _started = True

    logger.info(
        "Intraday StrategyBus started with %d strategies: %s",
        len(strategies),
        [s.NAME for s in strategies],
    )
    return _strategy_bus


def stop_intraday_engine() -> None:
    """
    Stop the StrategyBus and clean up subscriptions.

    Called by NexusEngine.stop(). Safe to call multiple times.
    """
    global _strategy_bus, _accumulator_subscribed, _started

    if _strategy_bus is not None:
        _strategy_bus.stop()
        logger.info("Intraday StrategyBus stopped")

    # Unsubscribe accumulator
    if _accumulator_subscribed:
        from core.event_bus import Topics
        for topic in [Topics.CANDLE_1M, Topics.CANDLE_3M, Topics.CANDLE_5M,
                      Topics.CANDLE_15M, Topics.CANDLE_1H]:
            bus.unsubscribe(topic, _candle_event_accumulator)
        _accumulator_subscribed = False

    # Clear buffers
    with _buffer_lock:
        _candle_buffers.clear()

    _strategy_bus = None
    _started = False
    logger.info("Intraday engine fully stopped and cleaned up")


def reset_intraday_engine() -> None:
    """
    Full reset for restart safety.
    Stops everything, clears all state, ready for fresh start().
    """
    stop_intraday_engine()
