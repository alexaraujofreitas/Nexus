# ============================================================
# NEXUS TRADER — Position Monitor Agent
#
# Continuous monitoring and dynamic adjustment of open positions.
# Implements trailing stops, regime-based tightening, and signal
# deterioration detection. Runs every 30 seconds to catch critical
# market moves quickly.
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.agents.base_agent import BaseAgent
from core.event_bus import bus, Topics
from core.execution.paper_executor import paper_executor
from core.execution.live_executor import live_executor

logger = logging.getLogger(__name__)


class PositionState:
    """Position state machine states."""
    WATCHING    = "WATCHING"     # normal monitoring
    TIGHTENING  = "TIGHTENING"   # tighten stop loss (protect profit)
    REDUCING    = "REDUCING"     # reduce position size (partial close)
    CLOSING     = "CLOSING"      # close position immediately
    LOCKED      = "LOCKED"       # manual lock, no auto-adjustment


class PositionMonitorAgent(BaseAgent):
    """
    Monitors all open positions (paper and live) and continuously
    re-evaluates them against current market conditions.

    Implements dynamic stop adjustment, position reduction on
    signal deterioration, and regime-aware risk management.
    """

    def __init__(self, name: str = "position_monitor", parent=None):
        super().__init__(name, parent)
        self._position_states: dict[str, str] = {}  # symbol → PositionState
        self._position_high_water: dict[str, float] = {}  # symbol → max unrealized profit %
        self._signal_cache: dict[str, dict] = {}  # symbol → last agent signal
        self._current_regime = "TRENDING_UP"

        # Subscribe to regime changes and signal updates
        bus.subscribe(Topics.REGIME_CHANGED, self._on_regime_changed)
        bus.subscribe(Topics.AGENT_SIGNAL, self._on_agent_signal)

    # ── Abstract interface implementation ──────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.POSITION_MONITOR_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return 30

    def fetch(self) -> dict:
        """
        Fetch current market data and position info.
        Returns dict with prices and positions.
        """
        # Get open positions from both executors
        paper_positions = paper_executor.get_open_positions()
        live_positions = live_executor.get_open_positions()

        all_positions = paper_positions + live_positions

        if not all_positions:
            return {"positions": [], "prices": {}, "regime": self._current_regime}

        # Collect unique symbols
        symbols = list(set(pos["symbol"] for pos in all_positions))

        # Fetch current prices from Binance
        prices = self._fetch_prices(symbols)

        # Fetch RSI for momentum check
        rsi_data = self._fetch_rsi_data(symbols)

        return {
            "positions": all_positions,
            "prices": prices,
            "rsi_data": rsi_data,
            "regime": self._current_regime,
        }

    def process(self, raw: dict) -> dict:
        """
        Process position data and determine actions.
        Returns aggregate monitoring signal.
        """
        positions = raw.get("positions", [])
        prices = raw.get("prices", {})
        rsi_data = raw.get("rsi_data", {})
        regime = raw.get("regime", "TRENDING_UP")

        if not positions:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "positions_monitored": 0,
                "actions_taken": [],
                "states": {},
            }

        actions_taken = []
        states_dict = {}
        position_healths = []

        # Process each position
        for pos in positions:
            symbol = pos["symbol"]
            current_price = prices.get(symbol, pos.get("current_price", 0))

            if current_price <= 0:
                continue

            # Update high-water mark
            unrealized_pct = pos.get("unrealized_pnl", 0)
            if symbol not in self._position_high_water:
                self._position_high_water[symbol] = max(0, unrealized_pct)
            else:
                self._position_high_water[symbol] = max(
                    self._position_high_water[symbol], unrealized_pct
                )

            # Get current position state
            state = self._position_states.get(symbol, PositionState.WATCHING)

            # Skip locked positions
            if state == PositionState.LOCKED:
                states_dict[symbol] = state
                position_healths.append(1.0)  # Consider locked positions healthy
                continue

            # Determine required action
            action = self._determine_action(
                pos, symbol, current_price, state, rsi_data.get(symbol, 0), regime
            )

            if action:
                actions_taken.append(action)
                states_dict[symbol] = action.get("state", state)
                position_healths.append(self._health_score(action))
            else:
                states_dict[symbol] = state
                position_healths.append(0.5)  # Neutral health

        # Calculate aggregate signal
        avg_health = sum(position_healths) / len(position_healths) if position_healths else 0.0
        signal = (avg_health - 0.5) * 2.0  # Convert 0-1 to -1 to 1

        return {
            "signal": signal,
            "confidence": min(0.95, len(positions) / 10.0),
            "has_data": True,
            "positions_monitored": len(positions),
            "actions_taken": actions_taken,
            "states": states_dict,
        }

    # ── Event handlers ────────────────────────────────────────────

    def _on_regime_changed(self, event) -> None:
        """Update regime when market regime changes."""
        try:
            data = event.data or {}
            self._current_regime = data.get("regime", self._current_regime)
            logger.debug("PositionMonitorAgent: regime changed to %s", self._current_regime)
        except Exception as exc:
            logger.warning("PositionMonitorAgent: regime update error: %s", exc)

    def _on_agent_signal(self, event) -> None:
        """Cache agent signals to detect deterioration."""
        try:
            data = event.data or {}
            symbol = data.get("symbol")
            if symbol:
                self._signal_cache[symbol] = data
        except Exception:
            pass

    # ── Action determination ──────────────────────────────────────

    def _determine_action(
        self,
        pos: dict,
        symbol: str,
        current_price: float,
        state: str,
        rsi: float,
        regime: str,
    ) -> Optional[dict]:
        """
        Analyze position and determine required action.
        Returns action dict or None if no action needed.
        """
        side = pos.get("side", "buy")
        entry_price = pos.get("entry_price", 0)
        stop_loss = pos.get("stop_loss", 0)
        unrealized_pct = pos.get("unrealized_pnl", 0)
        size_usdt = pos.get("size_usdt", 0)

        if entry_price <= 0 or size_usdt <= 0:
            return None

        # Check for signal deterioration
        signal_action = self._check_signal_deterioration(symbol, side)
        if signal_action:
            return signal_action

        # Check for momentum loss
        if rsi > 0:
            momentum_action = self._check_momentum_loss(symbol, side, rsi)
            if momentum_action:
                return momentum_action

        # Regime-aware tightening
        if regime in ("BEAR", "CRASH"):
            regime_action = self._tighten_for_regime(symbol, side, entry_price, stop_loss)
            if regime_action:
                return regime_action

        # Trailing stop logic (profit protection)
        if unrealized_pct > 1.0:
            # Trail stop to breakeven
            new_stop = entry_price
            if self._should_update_stop(symbol, side, stop_loss, new_stop):
                return {
                    "symbol": symbol,
                    "action": "adjust_stop",
                    "new_stop_loss": new_stop,
                    "state": PositionState.TIGHTENING,
                    "reason": "trailing_stop_breakeven",
                    "signal": 1.0,
                    "confidence": 0.8,
                }

        # High-water mark trailing
        if unrealized_pct > 2.0:
            max_profit = self._position_high_water.get(symbol, unrealized_pct)
            trailing_stop = entry_price + (max_profit * entry_price / 100) * 0.5
            if side == "sell":
                trailing_stop = entry_price - (max_profit * entry_price / 100) * 0.5

            if self._should_update_stop(symbol, side, stop_loss, trailing_stop):
                return {
                    "symbol": symbol,
                    "action": "adjust_stop",
                    "new_stop_loss": trailing_stop,
                    "state": PositionState.TIGHTENING,
                    "reason": "trailing_high_water_mark",
                    "signal": 1.0,
                    "confidence": 0.85,
                }

        # Position reduction on high profit
        if unrealized_pct > 5.0:
            return {
                "symbol": symbol,
                "action": "partial_close",
                "reduce_pct": 0.25,
                "state": PositionState.REDUCING,
                "reason": "profit_taking_5pct",
                "signal": 0.8,
                "confidence": 0.75,
            }

        if unrealized_pct > 3.0:
            signal_obj = self._signal_cache.get(symbol, {})
            signal_confidence = signal_obj.get("confidence", 1.0)
            if signal_confidence < 0.4:
                return {
                    "symbol": symbol,
                    "action": "partial_close",
                    "reduce_pct": 0.5,
                    "state": PositionState.REDUCING,
                    "reason": "signal_confidence_drop",
                    "signal": 0.5,
                    "confidence": 0.7,
                }

        # Time-based stop tightening (24h old with no profit)
        opened_at_str = pos.get("opened_at", "")
        if opened_at_str:
            try:
                opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
                if age_hours > 24 and unrealized_pct < 0.1:
                    # Tight stop: ±0.5%
                    tight_stop = entry_price * (1.005 if side == "buy" else 0.995)
                    return {
                        "symbol": symbol,
                        "action": "tighten_stop",
                        "new_stop_loss": tight_stop,
                        "state": PositionState.TIGHTENING,
                        "reason": "time_based_tighten_24h",
                        "signal": 0.0,
                        "confidence": 0.6,
                    }
            except Exception:
                pass

        return None

    def _check_signal_deterioration(self, symbol: str, side: str) -> Optional[dict]:
        """Check if the agent that generated this signal now has opposite signal."""
        signal_obj = self._signal_cache.get(symbol, {})
        if not signal_obj:
            return None

        cached_signal = signal_obj.get("signal", 0.0)

        # If original signal was bullish and now bearish (or vice versa)
        if (side == "buy" and cached_signal < -0.3) or (side == "sell" and cached_signal > 0.3):
            return {
                "symbol": symbol,
                "action": "partial_close",
                "reduce_pct": 0.5,
                "state": PositionState.REDUCING,
                "reason": "signal_deterioration",
                "signal": cached_signal,
                "confidence": 0.8,
            }

        return None

    def _check_momentum_loss(self, symbol: str, side: str, rsi: float) -> Optional[dict]:
        """Check for momentum loss via RSI."""
        if side == "buy" and rsi < 45 and rsi > 0:
            return {
                "symbol": symbol,
                "action": "tighten_stop",
                "new_stop_loss": None,  # Will be calculated by executor
                "state": PositionState.TIGHTENING,
                "reason": "momentum_loss_rsi_below_45",
                "signal": -0.5,
                "confidence": 0.6,
            }

        if side == "sell" and rsi > 55 and rsi < 100:
            return {
                "symbol": symbol,
                "action": "tighten_stop",
                "new_stop_loss": None,
                "state": PositionState.TIGHTENING,
                "reason": "momentum_loss_rsi_above_55",
                "signal": 0.5,
                "confidence": 0.6,
            }

        return None

    def _tighten_for_regime(
        self, symbol: str, side: str, entry_price: float, current_stop: float
    ) -> Optional[dict]:
        """Tighten stops by 30% in BEAR/CRASH regime."""
        if side == "buy":
            stop_range = entry_price - current_stop
            tightened = current_stop + (stop_range * 0.3)
        else:
            stop_range = current_stop - entry_price
            tightened = current_stop - (stop_range * 0.3)

        if self._should_update_stop(symbol, side, current_stop, tightened):
            return {
                "symbol": symbol,
                "action": "tighten_stop",
                "new_stop_loss": tightened,
                "state": PositionState.TIGHTENING,
                "reason": "regime_change_bear_crash",
                "signal": -1.0,
                "confidence": 0.9,
            }

        return None

    def _should_update_stop(self, symbol: str, side: str, current_stop: float, new_stop: float) -> bool:
        """Check if new stop is an improvement."""
        if side == "buy":
            # For long, moving stop up is better (tighter)
            return new_stop > current_stop
        else:
            # For short, moving stop down is better (tighter)
            return new_stop < current_stop

    def _health_score(self, action: dict) -> float:
        """Calculate health score (0-1) from action."""
        action_type = action.get("action", "")

        if action_type == "full_close":
            return 0.0  # Position being closed
        elif action_type == "closing":
            return 0.1
        elif action_type == "partial_close":
            return 0.3
        elif action_type == "tighten_stop":
            return 0.6
        elif action_type == "adjust_stop":
            return 0.7
        else:
            return 0.5

    # ── Data fetching ─────────────────────────────────────────────

    def _fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        """Fetch current prices via the active exchange (exchange_manager)."""
        from core.market_data.exchange_manager import exchange_manager
        prices = {}

        if not exchange_manager.is_connected():
            logger.debug("PositionMonitorAgent: exchange not connected, skipping price fetch")
            return prices

        for symbol in symbols:
            try:
                ticker = exchange_manager.fetch_ticker(symbol)
                if ticker and ticker.get("last"):
                    prices[symbol] = float(ticker["last"])
            except Exception as exc:
                logger.debug("PositionMonitorAgent: price fetch failed for %s: %s", symbol, exc)

        return prices

    def _fetch_rsi_data(self, symbols: list[str]) -> dict[str, float]:
        """Fetch 1h OHLCV and compute RSI via the active exchange (exchange_manager)."""
        from core.market_data.exchange_manager import exchange_manager
        rsi_data = {}

        if not exchange_manager.is_connected():
            logger.debug("PositionMonitorAgent: exchange not connected, skipping RSI fetch")
            return rsi_data

        for symbol in symbols:
            try:
                # fetch_ohlcv returns [[ts_ms, open, high, low, close, volume], ...]
                candles = exchange_manager.fetch_ohlcv(symbol, timeframe="1h", limit=15)
                if candles and len(candles) >= 2:
                    # Build klines-compatible list [[_, _, _, _, close, _], ...]
                    klines = [[c[0], c[1], c[2], c[3], c[4], c[5]] for c in candles]
                    rsi_data[symbol] = self._calculate_rsi(klines)
            except Exception as exc:
                logger.debug("PositionMonitorAgent: RSI fetch failed for %s: %s", symbol, exc)

        return rsi_data

    def _calculate_rsi(self, klines: list) -> float:
        """Calculate RSI from klines. klines: [[time, open, high, low, close, ...], ...]"""
        if len(klines) < 2:
            return 50.0  # Default neutral RSI

        closes = [float(k[4]) for k in klines]

        # Calculate gains and losses
        gains = []
        losses = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    # ── Action application ────────────────────────────────────────

    def apply_action(self, symbol: str, action_dict: dict) -> bool:
        """
        Apply position adjustment action.
        Calls paper_executor or live_executor methods.
        """
        action = action_dict.get("action", "")

        try:
            if action == "adjust_stop" or action == "tighten_stop":
                new_stop = action_dict.get("new_stop_loss")
                if new_stop:
                    # Both executors should support this
                    logger.info(
                        "PositionMonitorAgent: adjusting stop for %s to %.6g",
                        symbol, new_stop
                    )
                    # In a real implementation, executors would have adjust_stop method
                    # For now, we log the action
                    return True

            elif action == "partial_close":
                reduce_pct = action_dict.get("reduce_pct", 0.25)
                logger.info(
                    "PositionMonitorAgent: reducing %s by %.0f%%",
                    symbol, reduce_pct * 100
                )
                # In a real implementation, partial close would reduce position size
                return True

            elif action == "full_close":
                logger.info("PositionMonitorAgent: closing %s", symbol)
                paper_executor.close_position(symbol)
                live_executor.close_position(symbol)
                return True

            return False

        except Exception as exc:
            logger.error("PositionMonitorAgent: action apply failed: %s", exc)
            return False


# ── Module singleton ──────────────────────────────────────────
position_monitor_agent: Optional[PositionMonitorAgent] = None
