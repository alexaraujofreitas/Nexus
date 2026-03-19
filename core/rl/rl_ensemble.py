"""
Phase 3e: RL Ensemble combining SAC, CPPO, and Duelling DQN.

Implements a regime-specialized ensemble of three complementary reinforcement
learning agents that operate in parallel. Each agent handles different market
regimes optimally, and their outputs are combined via rolling Sharpe-weighted
voting. Weights adapt dynamically based on recent performance metrics.

Architecture:
- SAC (Soft Actor-Critic): Universal continuous actions, active in all regimes
- CPPO (Constrained PPO): Risk-constrained policy, active in bear/high-vol regimes
- DQN (Duelling DQN): Discrete actions for discrete transitions, active in ranging/accumulation regimes

Weight Updates:
- Tracked over rolling 30-episode windows
- Sharpe ratio = mean return / std of recent returns (min floor: 0.01)
- Weights renormalize every 100 steps among active agents in current regime
"""

from __future__ import annotations

import logging
import numpy as np
from typing import Optional, Dict, List
from collections import deque
from pathlib import Path

# RL Agent imports
try:
    from core.rl.agents.sac_agent import SACAgent
    from core.rl.agents.cppo_agent import CPPOAgent
    from core.rl.agents.duelling_dqn_agent import DuellingDQNAgent
    RL_AGENTS_AVAILABLE = True
except ImportError:
    RL_AGENTS_AVAILABLE = False

logger = logging.getLogger(__name__)


class RLEnsemble:
    """
    Ensemble of SAC, CPPO, and Duelling DQN agents.

    Each agent is regime-specialized. The ensemble combines their outputs using
    rolling Sharpe-weighted voting. Weights update every 100 steps based on each
    agent's recent 30-episode Sharpe ratio. If an agent is inactive in the
    current regime, its weight is zeroed and remaining weights renormalize.

    Attributes:
        sac_agent: Soft Actor-Critic agent (universal)
        cppo_agent: Constrained PPO agent (bear/high-vol regimes)
        dqn_agent: Duelling DQN agent (ranging/accumulation regimes)
        _weights: Current agent weights (renormalized to sum=1.0)
        _agent_returns: Deques tracking recent episode returns per agent
        _steps: Step counter for periodic weight updates
    """

    # Regime specialization
    # Includes both legacy names (trend_bull/trend_bear) and HMMRegimeClassifier
    # names (bull_trend/bear_trend) so the ensemble activates regardless of
    # which classifier produced the regime label.
    SAC_ACTIVE_REGIMES = [
        # Legacy names (kept for backward compat)
        "trend_bull", "trend_bear", "distribution", "high_volatility",
        "crisis", "volatility_compression", "ranging", "accumulation",
        "squeeze", "recovery", "consolidation", "quiet_accumulation",
        # HMMRegimeClassifier names
        "bull_trend", "bear_trend", "uncertain", "volatility_expansion",
        "liquidation_cascade",
    ]

    CPPO_ACTIVE_REGIMES = [
        # Legacy
        "trend_bear", "distribution", "high_volatility", "crisis",
        "volatility_compression",
        # HMM
        "bear_trend", "volatility_expansion",
    ]

    DQN_ACTIVE_REGIMES = [
        # Legacy + HMM (identical where names match)
        "ranging", "accumulation", "volatility_compression", "squeeze",
        "recovery",
    ]

    # Initial base weights
    INITIAL_WEIGHTS = {
        "sac": 0.40,
        "cppo": 0.35,
        "dqn": 0.25,
    }

    def __init__(self, state_dim: int = 50, model_dir: Optional[str] = None) -> None:
        """
        Initialize RL ensemble with all three agents.

        Instantiates SAC, CPPO, and Duelling DQN agents, each with the given
        state dimension. Attempts to load existing checkpoints from model_dir
        if provided and files exist.

        Args:
            state_dim: Dimension of state space (default: 50, matches BTCTradingEnv)
            model_dir: Directory for loading/saving agent checkpoints.
                      Default: {project_root}/models/rl/
                      If directory doesn't exist, will be created on save.

        Raises:
            RuntimeError: If RL agents are not available (PyTorch not installed)
        """
        if not RL_AGENTS_AVAILABLE:
            logger.warning("RL agents not available — PyTorch installation required")
            self.sac_agent = None
            self.cppo_agent = None
            self.dqn_agent = None
            self._weights = {}
            self._agent_returns = {}
            self._steps = 0
            return

        self.state_dim = state_dim
        self.model_dir = model_dir or "./models/rl/"

        # Instantiate agents
        try:
            self.sac_agent = SACAgent(state_dim=state_dim, action_dim=1)
            logger.info("SAC agent initialized (state_dim=%d)", state_dim)
        except Exception as exc:
            logger.error("Failed to initialize SAC agent: %s", exc)
            self.sac_agent = None

        try:
            self.cppo_agent = CPPOAgent(state_dim=state_dim, action_dim=1)
            logger.info("CPPO agent initialized (state_dim=%d)", state_dim)
        except Exception as exc:
            logger.error("Failed to initialize CPPO agent: %s", exc)
            self.cppo_agent = None

        try:
            self.dqn_agent = DuellingDQNAgent(state_dim=state_dim, n_actions=5)
            logger.info("Duelling DQN agent initialized (state_dim=%d)", state_dim)
        except Exception as exc:
            logger.error("Failed to initialize Duelling DQN agent: %s", exc)
            self.dqn_agent = None

        # Initialize weights
        self._weights = dict(self.INITIAL_WEIGHTS)

        # Performance tracking per agent (30-episode rolling window)
        self._agent_returns: Dict[str, deque] = {
            "sac": deque(maxlen=30),
            "cppo": deque(maxlen=30),
            "dqn": deque(maxlen=30),
        }

        # Step counter for periodic weight updates
        self._steps = 0

        # Attempt to load existing checkpoints
        self._load_all(self.model_dir)

    def select_action(
        self,
        state: np.ndarray,
        regime: str,
    ) -> Dict:
        """
        Select action from ensemble using Sharpe-weighted voting.

        Gets actions from all active agents in the current regime, normalizes
        their weights among active agents, and computes weighted average of
        actions. Continuous actions (SAC, CPPO) are averaged directly in
        [-1, 1] space. DQN discrete actions are first mapped to continuous
        equivalents, then averaged.

        Confidence is the weighted average of normalized actions (i.e., how
        strongly the ensemble is aligned).

        Args:
            state: Current state (50-dim numpy array)
            regime: Current market regime string

        Returns:
            Dictionary with keys:
            - "action": float, ensemble action in [-1.0, 1.0]
            - "confidence": float, 0.0-1.0 (alignment strength)
            - "active_agents": list of active agent names
            - "weights": dict of renormalized weights used
            - "agent_actions": dict mapping agent names to their raw actions
        """
        self._steps += 1

        # Determine which agents are active in this regime
        active_agents = []
        agent_actions_raw: Dict[str, float] = {}

        # Try SAC
        if self.sac_agent is not None and regime in self.SAC_ACTIVE_REGIMES:
            try:
                sac_action = self.sac_agent.select_action(state, explore=True)
                # SAC returns array, extract scalar
                sac_action_scalar = float(sac_action[0]) if isinstance(sac_action, np.ndarray) else float(sac_action)
                agent_actions_raw["sac"] = np.clip(sac_action_scalar, -1.0, 1.0)
                active_agents.append("sac")
            except Exception as exc:
                logger.debug("SAC action selection failed: %s", exc)

        # Try CPPO
        if self.cppo_agent is not None and regime in self.CPPO_ACTIVE_REGIMES:
            try:
                cppo_action, _, _ = self.cppo_agent.select_action(state, explore=True)
                # CPPO returns array, extract scalar
                cppo_action_scalar = float(cppo_action[0]) if isinstance(cppo_action, np.ndarray) else float(cppo_action)
                agent_actions_raw["cppo"] = np.clip(cppo_action_scalar, -1.0, 1.0)
                active_agents.append("cppo")
            except Exception as exc:
                logger.debug("CPPO action selection failed: %s", exc)

        # Try DQN
        if self.dqn_agent is not None and regime in self.DQN_ACTIVE_REGIMES:
            try:
                dqn_action_idx, dqn_position_size = self.dqn_agent.select_action(state, explore=True)
                # DQN discrete action already mapped to continuous [-1, 1]
                agent_actions_raw["dqn"] = float(dqn_position_size)
                active_agents.append("dqn")
            except Exception as exc:
                logger.debug("DQN action selection failed: %s", exc)

        # Fallback if no agents are active or all failed
        if not active_agents:
            logger.warning("No active agents in regime %s, using SAC fallback", regime)
            try:
                if self.sac_agent is not None:
                    sac_action = self.sac_agent.select_action(state, explore=False)
                    sac_action_scalar = float(sac_action[0]) if isinstance(sac_action, np.ndarray) else float(sac_action)
                    return {
                        "action": np.clip(sac_action_scalar, -1.0, 1.0),
                        "confidence": 0.0,
                        "active_agents": ["sac_fallback"],
                        "weights": {"sac": 1.0},
                        "agent_actions": {"sac": sac_action_scalar},
                    }
            except Exception as exc:
                logger.error("Even SAC fallback failed: %s", exc)

            # Ultimate fallback: zero action
            return {
                "action": 0.0,
                "confidence": 0.0,
                "active_agents": [],
                "weights": {},
                "agent_actions": {},
            }

        # Compute renormalized weights for active agents
        active_weights = {agent: self._weights.get(agent, 0.1) for agent in active_agents}
        total_weight = sum(active_weights.values())
        if total_weight > 0:
            active_weights = {agent: w / total_weight for agent, w in active_weights.items()}

        # Compute weighted average action
        ensemble_action = sum(
            active_weights[agent] * agent_actions_raw[agent]
            for agent in active_agents
        )
        ensemble_action = np.clip(ensemble_action, -1.0, 1.0)

        # Confidence = abs of weighted average (how strongly aligned the ensemble is)
        confidence = abs(ensemble_action)

        # Periodic weight update (every 100 steps)
        if self._steps % 100 == 0:
            self._update_weights()

        return {
            "action": ensemble_action,
            "confidence": confidence,
            "active_agents": active_agents,
            "weights": active_weights,
            "agent_actions": agent_actions_raw,
        }

    def record_transition(
        self,
        state: np.ndarray,
        action: float,
        reward: float,
        next_state: np.ndarray,
        done: bool,
        agent_name: str,
    ) -> None:
        """
        Record experience transition to specified agent's memory.

        Routes the transition to the appropriate agent's replay buffer or
        rollout buffer. For CPPO, appends to its rollout buffer; for SAC/DQN,
        calls their experience storage methods.

        Args:
            state: Current state
            action: Action taken (float for SAC/CPPO, int index for DQN)
            reward: Reward received
            next_state: Next state
            done: Whether episode terminated
            agent_name: One of "sac", "cppo", "dqn"
        """
        if agent_name == "sac" and self.sac_agent is not None:
            # SAC expects continuous action as array or scalar
            action_array = np.array([action], dtype=np.float32) if np.isscalar(action) else action
            self.sac_agent.add_experience(state, action_array, reward, next_state, done)

        elif agent_name == "cppo" and self.cppo_agent is not None:
            # CPPO has rollout buffer; record will be handled by collect_rollout
            # This is a placeholder — CPPO typically collects full rollouts
            pass

        elif agent_name == "dqn" and self.dqn_agent is not None:
            # DQN expects discrete action index
            action_idx = int(action) if np.isscalar(action) else int(action[0])
            self.dqn_agent.remember(state, action_idx, reward, next_state, done)

    def update_agent_performance(self, agent_name: str, episode_return: float) -> None:
        """
        Update rolling performance metrics for an agent.

        Records the total return of a completed episode into the agent's
        30-episode rolling window. Used later to compute Sharpe ratios for
        weight adaptation.

        Args:
            agent_name: One of "sac", "cppo", "dqn"
            episode_return: Total return accumulated in the episode
        """
        if agent_name in self._agent_returns:
            self._agent_returns[agent_name].append(float(episode_return))
            logger.debug(
                "Agent %s: episode return=%.4f (window size=%d)",
                agent_name, episode_return, len(self._agent_returns[agent_name])
            )

    def _update_weights(self) -> None:
        """
        Update ensemble weights based on recent Sharpe ratios.

        Computes Sharpe ratio for each agent over its recent 30-episode window.
        Sharpe = mean(returns) / std(returns). Floors Sharpe at 0.01 to prevent
        zero-division. Renormalizes all weights to sum to 1.0.

        Called every 100 steps from select_action.
        """
        sharpe_ratios = {}

        for agent_name in ["sac", "cppo", "dqn"]:
            returns_window = list(self._agent_returns.get(agent_name, []))

            if len(returns_window) < 2:
                sharpe_ratios[agent_name] = 0.01
            else:
                mean_return = np.mean(returns_window)
                std_return = np.std(returns_window)
                sharpe = mean_return / (std_return + 1e-8) if std_return > 0 else mean_return / 1e-8
                sharpe_ratios[agent_name] = max(0.01, float(sharpe))

        # Normalize Sharpe ratios to weights
        total_sharpe = sum(sharpe_ratios.values())
        if total_sharpe > 0:
            self._weights = {
                agent: sharpe / total_sharpe
                for agent, sharpe in sharpe_ratios.items()
            }
        else:
            self._weights = dict(self.INITIAL_WEIGHTS)

        logger.info(
            "Weights updated — Sharpe: %s → Weights: %s",
            {k: f"{v:.4f}" for k, v in sharpe_ratios.items()},
            {k: f"{v:.4f}" for k, v in self._weights.items()},
        )

    def train_step(self, regime: str) -> Dict:
        """
        Execute training step on agents with sufficient experience.

        Calls train_step() on SAC and DQN if they have minimum buffer samples.
        CPPO training is typically done via collect_rollout/train_on_rollout
        in the main training loop and is not called here.

        Returns aggregated training metrics across agents.

        Args:
            regime: Current market regime (used for logging/future regime-based adaptation)

        Returns:
            Dictionary with keys:
            - "sac_loss": SAC critic loss (0.0 if not trained)
            - "sac_actor_loss": SAC actor loss (0.0 if not trained)
            - "dqn_loss": DQN loss (0.0 if not trained)
            - "cppo_loss": Placeholder for CPPO (0.0)
            - "active_weights": Current weights dict
        """
        metrics = {
            "sac_loss": 0.0,
            "sac_actor_loss": 0.0,
            "dqn_loss": 0.0,
            "cppo_loss": 0.0,
            "active_weights": dict(self._weights),
        }

        # SAC training
        if self.sac_agent is not None and self.sac_agent.is_ready:
            try:
                sac_metrics = self.sac_agent.train_step()
                metrics["sac_loss"] = sac_metrics.get("critic_loss", 0.0)
                metrics["sac_actor_loss"] = sac_metrics.get("actor_loss", 0.0)
                logger.debug("SAC trained: critic_loss=%.4f", metrics["sac_loss"])
            except Exception as exc:
                logger.debug("SAC training step failed: %s", exc)

        # DQN training
        if self.dqn_agent is not None:
            try:
                dqn_metrics = self.dqn_agent.train_step()
                if dqn_metrics:
                    metrics["dqn_loss"] = dqn_metrics.get("loss", 0.0)
                    logger.debug("DQN trained: loss=%.4f", metrics["dqn_loss"])
            except Exception as exc:
                logger.debug("DQN training step failed: %s", exc)

        # CPPO training is done via env.collect_rollout/train_on_rollout
        # Not called here; placeholder metric only
        metrics["cppo_loss"] = 0.0

        return metrics

    def save_all(self, model_dir: Optional[str] = None) -> None:
        """
        Save checkpoints for all agents.

        Saves SAC, CPPO, and DQN agent states to model_dir. Creates directory
        if it doesn't exist.

        Args:
            model_dir: Directory to save checkpoints. If None, uses self.model_dir.
        """
        model_dir = model_dir or self.model_dir
        model_path = Path(model_dir)
        model_path.mkdir(parents=True, exist_ok=True)

        if self.sac_agent is not None:
            try:
                sac_path = str(model_path / "sac_agent.pt")
                self.sac_agent.save(sac_path)
                logger.info("SAC agent saved to %s", sac_path)
            except Exception as exc:
                logger.error("Failed to save SAC agent: %s", exc)

        if self.cppo_agent is not None:
            try:
                cppo_path = str(model_path / "cppo_agent.pt")
                self.cppo_agent.save(cppo_path)
                logger.info("CPPO agent saved to %s", cppo_path)
            except Exception as exc:
                logger.error("Failed to save CPPO agent: %s", exc)

        if self.dqn_agent is not None:
            try:
                dqn_path = str(model_path / "dqn_agent.pt")
                self.dqn_agent.save(dqn_path)
                logger.info("Duelling DQN agent saved to %s", dqn_path)
            except Exception as exc:
                logger.error("Failed to save Duelling DQN agent: %s", exc)

    def _load_all(self, model_dir: str) -> None:
        """
        Load checkpoints for all agents (internal helper).

        Attempts to load agent checkpoints from model_dir. If files don't
        exist, silently skips (agents start from scratch).

        Args:
            model_dir: Directory containing agent checkpoints
        """
        model_path = Path(model_dir)

        if not model_path.exists():
            logger.debug("Model directory %s does not exist; starting fresh", model_dir)
            return

        # SAC
        sac_path = model_path / "sac_agent.pt"
        if sac_path.exists() and self.sac_agent is not None:
            try:
                self.sac_agent.load(str(sac_path))
                logger.info("SAC agent loaded from %s", sac_path)
            except Exception as exc:
                logger.warning("Failed to load SAC agent: %s", exc)

        # CPPO
        cppo_path = model_path / "cppo_agent.pt"
        if cppo_path.exists() and self.cppo_agent is not None:
            try:
                self.cppo_agent.load(str(cppo_path))
                logger.info("CPPO agent loaded from %s", cppo_path)
            except Exception as exc:
                logger.warning("Failed to load CPPO agent: %s", exc)

        # DQN
        dqn_path = model_path / "dqn_agent.pt"
        if dqn_path.exists() and self.dqn_agent is not None:
            try:
                self.dqn_agent.load(str(dqn_path))
                logger.info("Duelling DQN agent loaded from %s", dqn_path)
            except Exception as exc:
                logger.warning("Failed to load Duelling DQN agent: %s", exc)
