# ============================================================
# NEXUS TRADER — Central Event Bus (Web/Headless Edition)
#
# Drop-in replacement for core.event_bus that removes all
# PySide6/Qt dependencies.  Same API:
#   bus.subscribe(topic, callback)
#   bus.unsubscribe(topic, callback)
#   bus.publish(topic, data, source)
#   bus.get_history(topic, limit)
#   bus.clear_subscribers(topic)
#
# Additionally supports an optional Redis bridge for
# cross-process event distribution.
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Event Topics ────────────────────────────────────────────
# Identical to the original — no changes.
class Topics:
    # Market Data
    TICK_UPDATE         = "market.tick"
    OHLCV_UPDATE        = "market.ohlcv"
    ORDERBOOK_UPDATE    = "market.orderbook"
    TRADE_STREAM        = "market.trades"

    # Strategy
    SIGNAL_GENERATED    = "strategy.signal"
    SIGNAL_CONFIRMED    = "strategy.signal.confirmed"
    SIGNAL_REJECTED     = "strategy.signal.rejected"
    STRATEGY_PROMOTED   = "strategy.promoted"
    STRATEGY_DISABLED   = "strategy.disabled"

    # Orders & Trades
    ORDER_PLACED        = "order.placed"
    ORDER_FILLED        = "order.filled"
    ORDER_CANCELLED     = "order.cancelled"
    TRADE_OPENED        = "trade.opened"
    TRADE_CLOSED        = "trade.closed"
    AUTO_EXECUTE_TRIGGERED = "order.auto_execute_triggered"
    SIGNAL_PENDING_CONFIRMATION = "order.signal_pending_confirmation"
    CONFIRMATION_REQUIRED = "order.confirmation_required"

    # Portfolio
    PORTFOLIO_UPDATED   = "portfolio.updated"
    POSITION_UPDATED    = "position.updated"

    # Risk
    DRAWDOWN_ALERT      = "risk.drawdown_alert"
    RISK_LIMIT_HIT      = "risk.limit_hit"
    EMERGENCY_STOP      = "risk.emergency_stop"
    LIQUIDITY_ALERT     = "risk.liquidity_alert"
    CASCADE_ALERT       = "risk.cascade_alert"
    SQUEEZE_ALERT       = "risk.squeeze_alert"

    # Position Sizing
    POSITION_SIZING_UPDATED = "position.sizing_updated"

    # Execution mode
    MODE_CHANGED        = "execution.mode_changed"

    # Regime & Intelligence
    REGIME_CHANGED      = "intelligence.regime_changed"
    REGIME_TRANSITION   = "intelligence.regime_transition"
    CANDLE_CLOSE        = "market.candle_close"
    CANDLE_CLOSED       = "market.candle_closed"
    SCAN_CYCLE_START    = "scanner.cycle_start"
    SCAN_CYCLE_COMPLETE = "scanner.cycle_complete"
    CANDIDATE_APPROVED  = "scanner.candidate_approved"
    CANDIDATE_REJECTED  = "scanner.candidate_rejected"
    SENTIMENT_UPDATED   = "intelligence.sentiment"
    PREDICTION_READY    = "intelligence.prediction"
    FINBERT_SIGNAL      = "intelligence.finbert_signal"
    RL_SIGNAL           = "intelligence.rl_signal"

    # System
    EXCHANGE_CONNECTED      = "system.exchange.connected"
    EXCHANGE_DISCONNECTED   = "system.exchange.disconnected"
    EXCHANGE_ERROR          = "system.exchange.error"
    FEED_STATUS             = "system.feed.status"
    SYSTEM_ALERT        = "system.alert"
    LOG_ENTRY           = "system.log"
    NOTIFICATION_SENT   = "system.notification.sent"
    ACCOUNT_RESET       = "account.reset"

    # Settings
    SETTINGS_CHANGED    = "settings.changed"

    # UI (kept for compatibility; web version may or may not use these)
    PAGE_CHANGED        = "ui.page_changed"
    THEME_CHANGED       = "ui.theme_changed"
    STATUS_UPDATE       = "ui.status_update"

    # Multi-Agent Intelligence Layer
    AGENT_STARTED       = "agent.started"
    AGENT_STOPPED       = "agent.stopped"
    AGENT_ERROR         = "agent.error"
    AGENT_STATUS        = "agent.status"
    FUNDING_RATE_UPDATED = "agent.funding_rate"
    ORDERBOOK_SIGNAL     = "agent.orderbook"
    OPTIONS_SIGNAL       = "agent.options_flow"
    SENTIMENT_SIGNAL     = "agent.sentiment"
    MACRO_UPDATED        = "agent.macro"
    SOCIAL_SIGNAL        = "agent.social"
    ORCHESTRATOR_SIGNAL  = "orchestrator.signal"
    ORCHESTRATOR_VETO    = "orchestrator.veto"

    # Crash Detection
    CRASH_SCORE_UPDATED      = "crash.score_updated"
    CRASH_TIER_CHANGED       = "crash.tier_changed"
    DEFENSIVE_MODE_ACTIVATED = "crash.defensive_activated"
    AGENT_SIGNAL             = "agent.signal"
    AGENT_STALE              = "agent.stale"

    # Agent topics (Sprint 6–9)
    VOLATILITY_SURFACE_UPDATED = "agent.volatility_surface"
    LIQUIDATION_FLOW_UPDATED   = "agent.liquidation_flow"
    ONCHAIN_UPDATED            = "agent.onchain"

    # Phase 2–6 topics
    WHALE_ALERT               = "agent.whale_alert"
    WHALE_CLUSTER_UPDATED     = "agent.whale_cluster"
    STABLECOIN_UPDATED        = "agent.stablecoin"
    MINER_FLOW_UPDATED        = "agent.miner_flow"
    CROSS_EXCHANGE_LIQ        = "agent.cross_exchange_liq"
    LIQUIDATION_CASCADE       = "agent.liq_cascade"
    SQUEEZE_DETECTED          = "agent.squeeze"
    LEVERAGE_CROWDING         = "agent.leverage_crowding"
    TWITTER_SIGNAL            = "agent.twitter"
    REDDIT_SIGNAL             = "agent.reddit"
    TELEGRAM_SIGNAL           = "agent.telegram"
    INFLUENCER_ALERT          = "agent.influencer"
    NARRATIVE_SHIFT           = "agent.narrative_shift"
    LIQUIDITY_VACUUM          = "agent.liquidity_vacuum"
    POSITION_MONITOR_UPDATED  = "agent.position_monitor"
    SCALP_SIGNAL              = "agent.scalp"
    MODEL_SELECTED            = "ai.model_selected"
    BTC_PRIORITY_UPDATE       = "btc.priority_update"


@dataclass
class Event:
    """Represents a single event in the system."""
    topic: str
    data: Any = None
    source: str = "system"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"Event(topic={self.topic!r}, source={self.source!r}, ts={self.timestamp.isoformat()})"

    def to_dict(self) -> dict:
        """Serialise for Redis/JSON transport."""
        return {
            "topic": self.topic,
            "data": self.data,
            "source": self.source,
            "ts": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        ts = d.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        elif ts is None:
            ts = datetime.now(timezone.utc)
        return cls(
            topic=d["topic"],
            data=d.get("data"),
            source=d.get("source", "system"),
            timestamp=ts,
        )


class EventBus:
    """
    Thread-safe event bus — pure Python, no Qt dependency.

    Drop-in compatible with the original EventBus API.
    Optionally bridges events to/from Redis for cross-process IPC.
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.RLock()
        self._event_history: list[Event] = []
        self._max_history = 1000
        self._redis_bridge: Optional["RedisBridge"] = None

    # ── Redis bridge ────────────────────────────────────────
    def attach_redis_bridge(self, bridge: "RedisBridge") -> None:
        """Attach a Redis bridge for cross-process event distribution."""
        self._redis_bridge = bridge
        logger.info("Redis bridge attached to EventBus")

    # ── Core API (unchanged from original) ──────────────────
    def subscribe(self, topic: str, callback: Callable[[Event], None]) -> None:
        """Register a callback for a topic. Supports wildcard '*'."""
        with self._lock:
            if callback not in self._subscribers[topic]:
                self._subscribers[topic].append(callback)
                logger.debug("Subscribed %s to topic '%s'", callback.__qualname__, topic)

    def unsubscribe(self, topic: str, callback: Callable) -> None:
        """Remove a callback from a topic."""
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic] = [
                    cb for cb in self._subscribers[topic] if cb != callback
                ]

    def publish(self, topic: str, data: Any = None, source: str = "system") -> None:
        """
        Publish an event to all local subscribers of the topic.
        Also notifies wildcard '*' subscribers.
        Thread-safe: can be called from any thread.

        If a Redis bridge is attached, the event is also published
        to the appropriate Redis channel for cross-process delivery.
        """
        event = Event(topic=topic, data=data, source=source)
        logger.debug("EVENT: %s", event)

        with self._lock:
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history.pop(0)
            callbacks = list(self._subscribers.get(topic, []))
            callbacks += list(self._subscribers.get("*", []))

        # Dispatch to local Python callbacks
        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(
                    "Event handler error for topic '%s': %s", topic, e, exc_info=True
                )

        # Publish to Redis bridge if attached
        if self._redis_bridge is not None:
            try:
                self._redis_bridge.publish_event(event)
            except Exception as e:
                logger.warning("Redis bridge publish failed: %s", e)

    def get_history(
        self, topic: Optional[str] = None, limit: int = 100
    ) -> list[Event]:
        """Return recent event history, optionally filtered by topic."""
        with self._lock:
            events = (
                self._event_history
                if topic is None
                else [e for e in self._event_history if e.topic == topic]
            )
            return list(reversed(events[-limit:]))

    def clear_subscribers(self, topic: Optional[str] = None):
        """Clear all subscribers for a topic, or all topics if None."""
        with self._lock:
            if topic:
                self._subscribers.pop(topic, None)
            else:
                self._subscribers.clear()


# ── Global Singleton ─────────────────────────────────────────
bus = EventBus()
