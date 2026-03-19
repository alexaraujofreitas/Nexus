# ============================================================
# NEXUS TRADER — Phase 4b: RL with Model Feedback (RLMF)
# LLM-based reward shaping for RL agent decisions
# ============================================================

import logging
import json
import re
import threading
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TradeContext:
    """Context information for a single trade."""
    entry_price: float
    exit_price: float
    pnl: float
    regime: str
    action: float  # [-1, 1]
    rationale: str
    timestamp: str


class RLModelFeedback:
    """
    RL with Model Feedback (RLMF) — uses LLM analysis to shape RL rewards.

    Periodically queries an LLM to analyze recent trade decisions and generate
    qualitative reward adjustments. The LLM evaluates:
        1. Whether RL actions aligned with market regime
        2. Whether risk management was appropriate
        3. Timing quality relative to technical levels

    Returns reward shaping bonuses/penalties in the [-0.2, +0.2] range.

    The shaping weight decays over time (more training → more reliance on RL
    signal, less on LLM guidance). LLM is only queried periodically to save
    API cost.
    """

    def __init__(self, feedback_interval_episodes: int = 50) -> None:
        """
        Initialize RLMF module.

        Args:
            feedback_interval_episodes: Only query LLM every N episodes.
                                       Cached feedback is used in between.
        """
        self.feedback_interval_episodes = feedback_interval_episodes

        # Cache for LLM feedback (used between queries)
        self._cached_feedback: Dict[str, Any] = {
            "bonus": 0.0,
            "penalty": 0.0,
            "reasoning": "No feedback yet"
        }

        self._lock = threading.Lock()
        self._last_query_episode = -feedback_interval_episodes  # Force first query

        logger.info(
            "RLModelFeedback initialized | "
            "feedback_interval=%d episodes",
            feedback_interval_episodes
        )

    def shape_reward(
        self,
        base_reward: float,
        trade_context: Dict[str, Any],
        episode_num: int
    ) -> float:
        """
        Shape the base reward using LLM feedback.

        Returns: shaped_reward = base_reward + llm_bonus * shaping_weight

        The shaping weight decays linearly from 0.1 to 0.01 over the first 1000
        episodes, then stays at 0.01. This allows the LLM to guide early training
        while RL becomes increasingly autonomous.

        Args:
            base_reward: Original RL reward signal
            trade_context: Dict with trade information (entry, exit, pnl, etc.)
            episode_num: Current episode number (for decay schedule)

        Returns:
            Shaped reward combining RL signal with LLM guidance
        """
        try:
            # Get shaping weight (decays with training progress)
            weight = self.get_shaping_weight(episode_num)

            # Query LLM periodically, use cache otherwise
            with self._lock:
                if self._should_query_llm(episode_num):
                    self._cached_feedback = self.request_feedback(
                        trade_history=[trade_context],
                        regime=trade_context.get("regime", "unknown"),
                        performance_stats={}
                    )
                    self._last_query_episode = episode_num

                feedback = self._cached_feedback

            # Compute shaping adjustment
            llm_adjustment = feedback.get("bonus", 0.0) - feedback.get("penalty", 0.0)

            # Apply weight-adjusted shaping
            shaped = base_reward + (llm_adjustment * weight)

            logger.debug(
                "Episode %d | base=%.4f | adjustment=%.4f | weight=%.3f | shaped=%.4f",
                episode_num,
                base_reward,
                llm_adjustment,
                weight,
                shaped,
            )

            return shaped

        except Exception as e:
            logger.error("Error in shape_reward: %s", e)
            return base_reward

    def _should_query_llm(self, episode_num: int) -> bool:
        """
        Check if LLM should be queried at this episode.

        Returns True if enough episodes have passed since last query.
        """
        return (episode_num - self._last_query_episode) >= self.feedback_interval_episodes

    def request_feedback(
        self,
        trade_history: List[Dict[str, Any]],
        regime: str,
        performance_stats: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Request reward adjustment feedback from the LLM.

        The LLM is asked to evaluate trade quality and provide reward adjustments
        in the range [-0.2, +0.2].

        Args:
            trade_history: List of recent trades (up to 20), each with:
                           {entry, exit, pnl, regime, action, rationale, timestamp}
            regime: Current market regime (e.g., "bull", "bear", "chop")
            performance_stats: Dict with performance metrics (sharpe, win_rate, etc.)

        Returns:
            Dict with keys:
                - bonus: Positive reward adjustment [0, 0.2]
                - penalty: Negative adjustment [0, 0.2]
                - reasoning: Text explanation of the assessment
        """
        try:
            from core.ai.llm_provider import get_provider, LLMMessage

            provider = get_provider()
            if provider is None:
                logger.warning("LLM provider unavailable, returning null feedback")
                return {
                    "bonus": 0.0,
                    "penalty": 0.0,
                    "reasoning": "LLM unavailable"
                }

            # Truncate trade history to last 20 trades
            recent_trades = trade_history[-20:]

            # Build LLM prompt
            system_prompt = self._build_system_prompt()
            user_message = self._build_user_message(recent_trades, regime, performance_stats)

            # Stream LLM response
            response_text = ""
            try:
                for chunk in provider.stream_chat(
                    messages=[LLMMessage(role="user", content=user_message)],
                    system_prompt=system_prompt,
                    max_tokens=500
                ):
                    response_text += chunk
            except Exception as e:
                logger.warning("LLM stream error: %s", e)

            # Parse response
            feedback = self._parse_llm_response(response_text)

            logger.info(
                "LLM feedback received | bonus=%.3f | penalty=%.3f | reasoning=%s",
                feedback.get("bonus", 0.0),
                feedback.get("penalty", 0.0),
                feedback.get("reasoning", "")[:100],
            )

            return feedback

        except Exception as e:
            logger.error("Error requesting LLM feedback: %s", e, exc_info=True)
            return {
                "bonus": 0.0,
                "penalty": 0.0,
                "reasoning": f"Error: {str(e)}"
            }

    @staticmethod
    def _build_system_prompt() -> str:
        """Build system prompt for LLM."""
        return (
            "You are a professional cryptocurrency trading evaluator with 10+ years "
            "of experience. Your role is to assess the quality of RL agent decisions "
            "and provide constructive feedback on trading strategy execution.\n\n"
            "Focus on:\n"
            "1. Regime alignment: Did the agent's actions match the market regime?\n"
            "2. Risk management: Were position sizes and stop losses appropriate?\n"
            "3. Timing quality: Were entries/exits near key technical levels?\n"
            "4. Consistency: Did the agent follow a coherent logic?\n\n"
            "Respond with ONLY valid JSON (no markdown, no explanation)."
        )

    @staticmethod
    def _build_user_message(
        trade_history: List[Dict[str, Any]],
        regime: str,
        performance_stats: Dict[str, Any]
    ) -> str:
        """Build user message for LLM."""
        # Serialize trades to JSON
        trades_json = json.dumps(
            [
                {
                    k: (float(v) if isinstance(v, (int, float)) else v)
                    for k, v in trade.items()
                }
                for trade in trade_history
            ],
            default=str,
            indent=2
        )

        stats_json = json.dumps(
            {k: float(v) if isinstance(v, (int, float)) else v
             for k, v in performance_stats.items()},
            default=str,
            indent=2
        )

        return (
            f"Evaluate the following RL trading decisions:\n\n"
            f"Trade History (last 20):\n{trades_json}\n\n"
            f"Market Regime: {regime}\n\n"
            f"Performance Stats:\n{stats_json}\n\n"
            f"Provide a JSON assessment with this exact structure:\n"
            f'{{"assessment": "good|mixed|poor", '
            f'"reward_adjustment": <float between -0.2 and 0.2>, '
            f'"reasoning": "<explanation>"}}'
        )

    @staticmethod
    def _parse_llm_response(response_text: str) -> Dict[str, Any]:
        """
        Parse LLM JSON response.

        Tries JSON parsing first, then regex fallback if that fails.

        Args:
            response_text: Raw LLM response text

        Returns:
            Dict with bonus, penalty, and reasoning keys
        """
        if not response_text.strip():
            return {
                "bonus": 0.0,
                "penalty": 0.0,
                "reasoning": "Empty response"
            }

        # Try direct JSON parsing
        try:
            data = json.loads(response_text)
            adjustment = float(data.get("reward_adjustment", 0.0))
            assessment = data.get("assessment", "mixed").lower()
            reasoning = str(data.get("reasoning", ""))

            # Clamp adjustment to [-0.2, 0.2]
            adjustment = max(-0.2, min(0.2, adjustment))

            # Split into bonus/penalty
            if adjustment >= 0:
                return {
                    "bonus": adjustment,
                    "penalty": 0.0,
                    "reasoning": f"[{assessment}] {reasoning}"
                }
            else:
                return {
                    "bonus": 0.0,
                    "penalty": -adjustment,
                    "reasoning": f"[{assessment}] {reasoning}"
                }

        except (json.JSONDecodeError, ValueError, TypeError):
            logger.debug("JSON parse failed, trying regex fallback")

        # Fallback: regex extraction
        try:
            # Extract reward_adjustment number
            adjustment_match = re.search(
                r'"reward_adjustment"\s*:\s*(-?\d+\.?\d*)',
                response_text
            )
            adjustment = float(adjustment_match.group(1)) if adjustment_match else 0.0
            adjustment = max(-0.2, min(0.2, adjustment))

            # Extract assessment
            assessment_match = re.search(
                r'"assessment"\s*:\s*"(\w+)"',
                response_text
            )
            assessment = assessment_match.group(1).lower() if assessment_match else "mixed"

            # Extract reasoning
            reasoning_match = re.search(
                r'"reasoning"\s*:\s*"([^"]+)"',
                response_text
            )
            reasoning = reasoning_match.group(1) if reasoning_match else ""

            if adjustment >= 0:
                return {
                    "bonus": adjustment,
                    "penalty": 0.0,
                    "reasoning": f"[{assessment}] {reasoning}"
                }
            else:
                return {
                    "bonus": 0.0,
                    "penalty": -adjustment,
                    "reasoning": f"[{assessment}] {reasoning}"
                }

        except Exception as e:
            logger.warning("Regex parse also failed: %s", e)

        # Last resort: zero adjustment
        return {
            "bonus": 0.0,
            "penalty": 0.0,
            "reasoning": "Parse error, using zero adjustment"
        }

    @staticmethod
    def get_shaping_weight(episode_num: int) -> float:
        """
        Compute shaping weight for the current episode.

        Linear decay: 0.1 → 0.01 over first 1000 episodes, stays at 0.01 after.

        This ensures the LLM provides strong guidance early but RL becomes
        increasingly autonomous as it learns.

        Args:
            episode_num: Current episode number (0-indexed)

        Returns:
            Shaping weight in range [0.01, 0.1]
        """
        if episode_num >= 1000:
            return 0.01

        # Linear decay: 0.1 at episode 0, 0.01 at episode 1000
        weight = 0.1 - (episode_num / 1000) * (0.1 - 0.01)
        return max(0.01, min(0.1, weight))

    def reset_feedback(self) -> None:
        """
        Clear cached feedback and reset query tracking.

        Used when starting a new training session.
        """
        with self._lock:
            self._cached_feedback = {
                "bonus": 0.0,
                "penalty": 0.0,
                "reasoning": "Feedback reset"
            }
            self._last_query_episode = -self.feedback_interval_episodes

        logger.info("RLMF feedback cache cleared")
