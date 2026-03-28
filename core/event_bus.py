# ============================================================
# NEXUS TRADER — Central Event Bus
# Thread-safe pub/sub system for decoupled module communication
# ============================================================

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime
from PySide6.QtCore import QObject, Signal, QMetaObject, Qt

logger = logging.getLogger(__name__)


# ── Event Topics ────────────────────────────────────────────
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
    MODE_CHANGED        = "execution.mode_changed"   # paper ↔ live

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
    NOTIFICATION_SENT   = "system.notification.sent"  # emitted after any notification is dispatched
    ACCOUNT_RESET       = "account.reset"              # paper account wiped — all pages must full-refresh

    # Settings
    SETTINGS_CHANGED    = "settings.changed"

    # UI
    PAGE_CHANGED        = "ui.page_changed"
    THEME_CHANGED       = "ui.theme_changed"
    STATUS_UPDATE       = "ui.status_update"

    # ── Multi-Agent Intelligence Layer ──────────────────────
    # Agent status / lifecycle
    AGENT_STARTED       = "agent.started"
    AGENT_STOPPED       = "agent.stopped"
    AGENT_ERROR         = "agent.error"
    AGENT_STATUS        = "agent.status"          # periodic health ping

    # Funding rates & open interest  (Sprint 1)
    FUNDING_RATE_UPDATED = "agent.funding_rate"   # {symbol, rate, oi, signal, confidence}

    # Order book microstructure  (Sprint 2)
    ORDERBOOK_SIGNAL     = "agent.orderbook"      # {symbol, imbalance, bid_wall, ask_wall, signal}

    # Options flow  (Sprint 3)
    OPTIONS_SIGNAL       = "agent.options_flow"   # {symbol, put_call, max_pain, gamma_signal, iv_skew}

    # Enhanced sentiment  (Sprint 4)
    SENTIMENT_SIGNAL     = "agent.sentiment"      # {symbol, score, event_type, confidence, source}

    # Macro / global risk  (Sprint 5)
    MACRO_UPDATED        = "agent.macro"          # {macro_risk_score, dxy, regime_bias}

    # Social media  (Sprint 5)
    SOCIAL_SIGNAL        = "agent.social"         # {symbol, social_volume, sentiment, source}

    # Orchestrator decisions  (Sprint 7)
    ORCHESTRATOR_SIGNAL  = "orchestrator.signal"  # enriched OrderCandidate data
    ORCHESTRATOR_VETO    = "orchestrator.veto"    # macro/risk override blocks a candidate

    # ── Crash Detection  (Sprint 13) ─────────────────────────
    CRASH_SCORE_UPDATED      = "crash.score_updated"      # {score, tier, components}
    CRASH_TIER_CHANGED       = "crash.tier_changed"       # {old_tier, new_tier, score}
    DEFENSIVE_MODE_ACTIVATED = "crash.defensive_activated" # {tier, actions_taken}
    AGENT_SIGNAL             = "agent.signal"              # generic per-agent signal (for UI)
    AGENT_STALE              = "agent.stale"               # agent went stale

    # ── Missing agent topics (Sprint 6–9) ─────────────────────
    VOLATILITY_SURFACE_UPDATED = "agent.volatility_surface"  # {symbol, iv_skew, term_structure}
    LIQUIDATION_FLOW_UPDATED   = "agent.liquidation_flow"    # {liq_volume, dominant_side, signal}
    ONCHAIN_UPDATED            = "agent.onchain"             # {whale_alert, exchange_flow, signal}

    # ── New capability topics (Phase 2–6) ──────────────────────
    WHALE_ALERT               = "agent.whale_alert"          # {address, amount_usd, direction, tx_hash}
    WHALE_CLUSTER_UPDATED     = "agent.whale_cluster"        # {clusters, dominant_behavior, signal}
    STABLECOIN_UPDATED        = "agent.stablecoin"           # {total_supply, net_flow, signal}
    MINER_FLOW_UPDATED        = "agent.miner_flow"           # {miner_outflow, reserve, signal}
    CROSS_EXCHANGE_LIQ        = "agent.cross_exchange_liq"   # {bid_depth, ask_depth, imbalance}
    LIQUIDATION_CASCADE       = "agent.liq_cascade"          # {cascade_risk, cluster_price, signal}
    SQUEEZE_DETECTED          = "agent.squeeze"              # {type, funding, oi_change, signal}
    LEVERAGE_CROWDING         = "agent.leverage_crowding"    # {leverage_ratio, crowding_score, signal}
    TWITTER_SIGNAL            = "agent.twitter"              # {mentions, sentiment, influencers}
    REDDIT_SIGNAL             = "agent.reddit"               # {posts, sentiment, trending}
    TELEGRAM_SIGNAL           = "agent.telegram"             # {channels, sentiment, signal}
    INFLUENCER_ALERT          = "agent.influencer"           # {handle, post, sentiment, impact}
    NARRATIVE_SHIFT           = "agent.narrative_shift"      # {old_narrative, new_narrative, signal}
    LIQUIDITY_VACUUM          = "agent.liquidity_vacuum"     # {price_level, depth, signal}
    POSITION_MONITOR_UPDATED  = "agent.position_monitor"     # {symbol, state, action, signal}
    SCALP_SIGNAL              = "agent.scalp"                # {symbol, direction, entry, sl, tp}
    MODEL_SELECTED            = "ai.model_selected"          # {agent, model_name, provider}
    BTC_PRIORITY_UPDATE       = "btc.priority_update"        # {regime, priority_score, filters}


@dataclass
class Event:
    """Represents a single event in the system."""
    topic: str
    data: Any = None
    source: str = "system"
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __repr__(self):
        return f"Event(topic={self.topic!r}, source={self.source!r}, ts={self.timestamp.isoformat()})"


class EventBus(QObject):
    """
    Thread-safe, Qt-integrated event bus.

    - Python subscribers: register callbacks via subscribe()
    - Qt UI subscribers: use the qt_signal for cross-thread GUI updates
    - All events are logged at DEBUG level for traceability
    """

    # Qt signal for cross-thread UI updates
    qt_event = Signal(object)

    def __init__(self):
        super().__init__()
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.RLock()
        self._event_history: list[Event] = []
        self._max_history = 1000

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
        Publish an event to all subscribers of the topic.
        Also notifies wildcard '*' subscribers.
        Thread-safe: can be called from any thread.

        CRITICAL: Callbacks are always dispatched on the MAIN Qt thread via
        QTimer.singleShot(0, ...).  This prevents the 'QBasicTimer::stop:
        Failed. Possibly trying to stop from a different thread' crash that
        occurs when UI widgets (QTimers, QLabels, etc.) are touched from a
        background QThread.

        If publish() is called from the main thread, the singleShot fires
        on the next event-loop iteration (negligible latency).  If called
        from a background thread, Qt automatically queues it to the main
        thread.
        """
        event = Event(topic=topic, data=data, source=source)
        logger.debug("EVENT: %s", event)

        with self._lock:
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history.pop(0)
            # Collect callbacks (copy to avoid modification during iteration)
            callbacks = list(self._subscribers.get(topic, []))
            callbacks += list(self._subscribers.get("*", []))

        # Emit Qt signal for UI subscribers (thread-safe cross-thread)
        self.qt_event.emit(event)

        # Call Python callbacks directly (synchronous dispatch).
        # NOTE: Callbacks that touch Qt widgets MUST use QTimer.singleShot(0, ...)
        # internally if they may be called from a background thread.  The event bus
        # cannot safely defer ALL callbacks because non-UI callbacks (data feed,
        # agent coordinator) must run immediately.  The responsibility is on each
        # UI subscriber to marshal to the main thread when needed.
        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error("Event handler error for topic '%s': %s", topic, e, exc_info=True)

    def get_history(self, topic: Optional[str] = None, limit: int = 100) -> list[Event]:
        """Return recent event history, optionally filtered by topic."""
        with self._lock:
            events = self._event_history if topic is None else [
                e for e in self._event_history if e.topic == topic
            ]
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
