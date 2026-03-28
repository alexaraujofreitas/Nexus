# ============================================================
# NEXUS TRADER — Agent Coordinator  (Sprint 6)
#
# Manages the full lifecycle of all intelligence agents:
#   - Starts agents after the exchange connects
#   - Stops agents on shutdown
#   - Injects module-level singletons so sub-models can access them
#   - Provides a unified status API for the UI
#   - Handles agent restart on crash (via consecutive_errors backoff
#     already built into BaseAgent — coordinator just monitors)
#
# Usage:
#   coordinator = AgentCoordinator()
#   coordinator.start_all()   # call after exchange connected
#   coordinator.stop_all()    # call on app shutdown
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.event_bus import bus, Topics
from core.agents.onchain_agent import OnChainAgent
from core.agents.volatility_surface_agent import VolatilitySurfaceAgent
from core.agents.liquidation_flow_agent import LiquidationFlowAgent
from core.agents.crash_detection_agent import CrashDetectionAgent

logger = logging.getLogger(__name__)


class AgentCoordinator(QObject):
    """
    Lifecycle manager for all NexusTrader intelligence agents.

    Maintains references to every agent singleton, starts them after
    the exchange is connected, and tears them down cleanly on exit.
    """

    # Emitted whenever any agent produces a new signal
    agents_status_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._agents: list = []
        self._running = False

        # Subscribe to exchange connect/disconnect events
        bus.subscribe(Topics.EXCHANGE_CONNECTED,    self._on_exchange_connected)
        bus.subscribe(Topics.EXCHANGE_DISCONNECTED, self._on_exchange_disconnected)

        # Status tracking for UI
        self._status: dict[str, dict] = {}
        bus.subscribe(Topics.AGENT_STARTED, self._on_agent_event)
        bus.subscribe(Topics.AGENT_STOPPED, self._on_agent_event)

    # ── Public API ────────────────────────────────────────────

    def start_all(self) -> None:
        """
        Instantiate and start all agents.
        Injects module-level singletons so sub-models can import them.
        Safe to call multiple times — guards against double-start.
        """
        if self._running:
            logger.debug("AgentCoordinator: already running — ignoring start_all()")
            return

        logger.info("AgentCoordinator: starting all intelligence agents")
        self._running = True
        self._agents.clear()

        # Initialize BTC-first priority filter
        try:
            from core.scanning.btc_priority import get_btc_priority_filter
            get_btc_priority_filter()  # initialize singleton
            logger.info("AgentCoordinator: BTC-first priority filter initialized")
        except Exception as exc:
            logger.warning("AgentCoordinator: BTC priority filter init failed: %s", exc)

        # Initialize model registry
        try:
            from core.ai.model_registry import get_model_registry
            get_model_registry()  # initialize singleton
            logger.info("AgentCoordinator: AI model registry initialized")
        except Exception as exc:
            logger.warning("AgentCoordinator: model registry init failed: %s", exc)

        try:
            self._start_funding_rate_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: FundingRateAgent start failed — %s", exc)

        try:
            self._start_order_book_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: OrderBookAgent start failed — %s", exc)

        try:
            self._start_options_flow_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: OptionsFlowAgent start failed — %s", exc)

        try:
            self._start_macro_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: MacroAgent start failed — %s", exc)

        try:
            self._start_social_sentiment_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: SocialSentimentAgent start failed — %s", exc)

        try:
            self._start_news_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: NewsAgent start failed — %s", exc)

        try:
            self._start_geopolitical_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: GeopoliticalAgent start failed — %s", exc)

        try:
            self._start_sector_rotation_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: SectorRotationAgent start failed — %s", exc)

        try:
            self._start_onchain_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: OnChainAgent start failed — %s", exc)

        try:
            self._start_volatility_surface_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: VolatilitySurfaceAgent start failed — %s", exc)

        try:
            self._start_liquidation_flow_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: LiquidationFlowAgent start failed — %s", exc)

        try:
            self._start_crash_detection_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: CrashDetectionAgent start failed — %s", exc)

        try:
            self._start_whale_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: WhaleTrackingAgent start failed — %s", exc)

        try:
            self._start_stablecoin_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: StablecoinLiquidityAgent start failed — %s", exc)

        try:
            self._start_miner_flow_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: MinerFlowAgent start failed — %s", exc)

        try:
            self._start_squeeze_detection_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: SqueezeDetectionAgent start failed — %s", exc)

        try:
            self._start_narrative_shift_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: NarrativeShiftAgent start failed — %s", exc)

        try:
            self._start_liquidity_vacuum_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: LiquidityVacuumAgent start failed — %s", exc)

        try:
            self._start_twitter_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: TwitterSentimentAgent start failed — %s", exc)

        try:
            self._start_reddit_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: RedditSentimentAgent start failed — %s", exc)

        try:
            self._start_telegram_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: TelegramSentimentAgent start failed — %s", exc)

        try:
            self._start_position_monitor_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: PositionMonitorAgent start failed — %s", exc)

        try:
            self._start_scalp_agent()
        except Exception as exc:
            logger.error("AgentCoordinator: ScalpingAgent start failed — %s", exc)

        logger.info(
            "AgentCoordinator: %d agents started", len(self._agents)
        )

    def stop_all(self) -> None:
        """Stop all running agents gracefully."""
        if not self._running:
            return
        logger.info("AgentCoordinator: stopping %d agents", len(self._agents))
        for agent in self._agents:
            try:
                agent.stop()
                agent.wait(3000)  # wait up to 3s for thread to exit
            except Exception as exc:
                logger.warning("AgentCoordinator: error stopping agent — %s", exc)
        self._agents.clear()
        self._running = False
        logger.info("AgentCoordinator: all agents stopped")

    def get_status(self) -> dict[str, dict]:
        """Return status snapshot for all agents (for UI display)."""
        status: dict[str, dict] = {}
        for agent in self._agents:
            try:
                sig = agent.last_signal or {}
                status[agent._name] = {
                    "running":      agent.isRunning(),
                    "stale":        agent.is_stale,
                    "signal":       round(sig.get("signal", 0.0), 4),
                    "confidence":   round(sig.get("confidence", 0.0), 4),
                    "updated_at":   sig.get("updated_at", "never"),
                    "errors":       agent._consecutive_errors,
                }
            except Exception:
                pass
        return status

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _is_agent_enabled(config_key: str, default: bool = True) -> bool:
        """
        Read a boolean agent-enable flag from config.
        config_key uses dot notation, e.g. 'agents.reddit_enabled'.
        Returns `default` when the key is absent or config is unavailable.
        """
        try:
            from config.settings import settings as _s
            val = _s.get(config_key, default)
            return bool(val)
        except Exception:
            return default

    # ── Agent starters ────────────────────────────────────────

    def _start_funding_rate_agent(self) -> None:
        import core.agents.funding_rate_agent as _mod
        from core.agents.funding_rate_agent import FundingRateAgent
        agent = FundingRateAgent()
        _mod.funding_rate_agent = agent   # inject singleton
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: FundingRateAgent started")

    def _start_order_book_agent(self) -> None:
        if not self._is_agent_enabled("agents.orderbook_enabled", default=False):
            logger.info("AgentCoordinator: OrderBookAgent DISABLED (agents.orderbook_enabled=false)")
            return
        import core.agents.order_book_agent as _mod
        from core.agents.order_book_agent import OrderBookAgent
        agent = OrderBookAgent()
        _mod.order_book_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: OrderBookAgent started")

    def _start_options_flow_agent(self) -> None:
        if not self._is_agent_enabled("agents.options_enabled", default=False):
            logger.info("AgentCoordinator: OptionsFlowAgent DISABLED (agents.options_enabled=false)")
            return
        import core.agents.options_flow_agent as _mod
        from core.agents.options_flow_agent import OptionsFlowAgent
        agent = OptionsFlowAgent()
        _mod.options_flow_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: OptionsFlowAgent started")

    def _start_macro_agent(self) -> None:
        import core.agents.macro_agent as _mod
        from core.agents.macro_agent import MacroAgent
        agent = MacroAgent()
        _mod.macro_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: MacroAgent started")

    def _start_social_sentiment_agent(self) -> None:
        if not self._is_agent_enabled("agents.social_sentiment_enabled", default=False):
            logger.info("AgentCoordinator: SocialSentimentAgent DISABLED (agents.social_sentiment_enabled=false)")
            return
        import core.agents.social_sentiment_agent as _mod
        from core.agents.social_sentiment_agent import SocialSentimentAgent
        agent = SocialSentimentAgent()
        _mod.social_sentiment_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: SocialSentimentAgent started")

    def _start_news_agent(self) -> None:
        import core.agents.news_agent as _mod
        from core.agents.news_agent import NewsAgent
        agent = NewsAgent()
        _mod.news_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: NewsAgent started")

    def _start_geopolitical_agent(self) -> None:
        import core.agents.geopolitical_agent as _mod
        from core.agents.geopolitical_agent import GeopoliticalAgent
        agent = GeopoliticalAgent()
        _mod.geopolitical_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: GeopoliticalAgent started")

    def _start_sector_rotation_agent(self) -> None:
        if not self._is_agent_enabled("agents.sector_rotation_enabled", default=False):
            logger.info("AgentCoordinator: SectorRotationAgent DISABLED (agents.sector_rotation_enabled=false)")
            return
        import core.agents.sector_rotation_agent as _mod
        from core.agents.sector_rotation_agent import SectorRotationAgent
        agent = SectorRotationAgent()
        _mod.sector_rotation_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: SectorRotationAgent started")

    def _start_onchain_agent(self) -> None:
        import core.agents.onchain_agent as _mod
        agent = OnChainAgent()
        _mod.onchain_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: OnChainAgent started")

    def _start_volatility_surface_agent(self) -> None:
        if not self._is_agent_enabled("agents.volatility_surface.enabled", default=False):
            logger.info("AgentCoordinator: VolatilitySurfaceAgent DISABLED (agents.volatility_surface.enabled=false)")
            return
        import core.agents.volatility_surface_agent as _mod
        agent = VolatilitySurfaceAgent()
        _mod.volatility_surface_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: VolatilitySurfaceAgent started")

    def _start_liquidation_flow_agent(self) -> None:
        import core.agents.liquidation_flow_agent as _mod
        agent = LiquidationFlowAgent()
        _mod.liquidation_flow_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: LiquidationFlowAgent started")

    def _start_crash_detection_agent(self) -> None:
        import core.agents.crash_detection_agent as _mod
        agent = CrashDetectionAgent()
        _mod.crash_detection_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: CrashDetectionAgent started")

    def _start_whale_agent(self) -> None:
        import core.agents.whale_agent as _mod
        from core.agents.whale_agent import WhaleTrackingAgent
        agent = WhaleTrackingAgent()
        _mod.whale_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: WhaleTrackingAgent started")

    def _start_stablecoin_agent(self) -> None:
        import core.agents.stablecoin_agent as _mod
        from core.agents.stablecoin_agent import StablecoinLiquidityAgent
        agent = StablecoinLiquidityAgent()
        _mod.stablecoin_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: StablecoinLiquidityAgent started")

    def _start_miner_flow_agent(self) -> None:
        if not self._is_agent_enabled("agents.miner_flow_enabled", default=False):
            logger.info("AgentCoordinator: MinerFlowAgent DISABLED (agents.miner_flow_enabled=false)")
            return
        import core.agents.miner_flow_agent as _mod
        from core.agents.miner_flow_agent import MinerFlowAgent
        agent = MinerFlowAgent()
        _mod.miner_flow_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: MinerFlowAgent started")

    def _start_squeeze_detection_agent(self) -> None:
        import core.agents.squeeze_detection_agent as _mod
        from core.agents.squeeze_detection_agent import SqueezeDetectionAgent
        agent = SqueezeDetectionAgent()
        _mod.squeeze_detection_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: SqueezeDetectionAgent started")

    def _start_narrative_shift_agent(self) -> None:
        if not self._is_agent_enabled("agents.narrative_enabled", default=False):
            logger.info("AgentCoordinator: NarrativeShiftAgent DISABLED (agents.narrative_enabled=false)")
            return
        import core.agents.narrative_agent as _mod
        from core.agents.narrative_agent import NarrativeShiftAgent
        agent = NarrativeShiftAgent()
        _mod.narrative_shift_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: NarrativeShiftAgent started")

    def _start_liquidity_vacuum_agent(self) -> None:
        if not self._is_agent_enabled("agents.liquidity_vacuum_enabled", default=False):
            logger.info("AgentCoordinator: LiquidityVacuumAgent DISABLED (agents.liquidity_vacuum_enabled=false)")
            return
        import core.agents.liquidity_vacuum_agent as _mod
        from core.agents.liquidity_vacuum_agent import LiquidityVacuumAgent
        agent = LiquidityVacuumAgent()
        _mod.liquidity_vacuum_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: LiquidityVacuumAgent started")

    def _start_twitter_agent(self) -> None:
        if not self._is_agent_enabled("agents.twitter_enabled", default=False):
            logger.info("AgentCoordinator: TwitterSentimentAgent DISABLED (agents.twitter_enabled=false)")
            return
        import core.agents.twitter_agent as _mod
        from core.agents.twitter_agent import TwitterSentimentAgent
        agent = TwitterSentimentAgent()
        _mod.twitter_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: TwitterSentimentAgent started")

    def _start_reddit_agent(self) -> None:
        if not self._is_agent_enabled("agents.reddit_enabled", default=False):
            logger.info("AgentCoordinator: RedditSentimentAgent DISABLED (agents.reddit_enabled=false)")
            return
        import core.agents.reddit_agent as _mod
        from core.agents.reddit_agent import RedditSentimentAgent
        agent = RedditSentimentAgent()
        _mod.reddit_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: RedditSentimentAgent started")

    def _start_telegram_agent(self) -> None:
        import core.agents.telegram_agent as _mod
        from core.agents.telegram_agent import TelegramSentimentAgent
        agent = TelegramSentimentAgent()
        _mod.telegram_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: TelegramSentimentAgent started")

    def _start_position_monitor_agent(self) -> None:
        import core.agents.position_monitor_agent as _mod
        from core.agents.position_monitor_agent import PositionMonitorAgent
        agent = PositionMonitorAgent()
        _mod.position_monitor_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: PositionMonitorAgent started")

    def _start_scalp_agent(self) -> None:
        if not self._is_agent_enabled("agents.scalp_enabled", default=False):
            logger.info("AgentCoordinator: ScalpingAgent DISABLED (agents.scalp_enabled=false)")
            return
        import core.agents.scalp_agent as _mod
        from core.agents.scalp_agent import ScalpingAgent
        agent = ScalpingAgent()
        _mod.scalp_agent = agent
        agent.start()
        self._agents.append(agent)
        logger.info("AgentCoordinator: ScalpingAgent started")

    # ── Event handlers ────────────────────────────────────────

    def _on_exchange_connected(self, event: dict) -> None:
        """Auto-start agents when the exchange connects."""
        logger.info("AgentCoordinator: exchange connected — starting agents")
        self.start_all()

    def _on_exchange_disconnected(self, event: dict) -> None:
        """Stop agents when the exchange disconnects."""
        logger.info("AgentCoordinator: exchange disconnected — stopping agents")
        self.stop_all()

    def _on_agent_event(self, event) -> None:
        # EventBus delivers Event objects — extract .data dict
        data = event.data if (hasattr(event, "data") and isinstance(event.data, dict)) else (event if isinstance(event, dict) else {})
        agent_name = data.get("agent", "unknown")
        self._status[agent_name] = data
        try:
            self.agents_status_changed.emit(self.get_status())
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────
_coordinator: Optional[AgentCoordinator] = None


def get_coordinator() -> AgentCoordinator:
    """Return the global AgentCoordinator, creating it if needed."""
    global _coordinator
    if _coordinator is None:
        _coordinator = AgentCoordinator()
    return _coordinator
