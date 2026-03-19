"""
Constrained Proximal Policy Optimization (CPPO) Agent with CVaR Risk Management.

Implements a PPO-based agent with Conditional Value at Risk (CVaR) constraints
for trading in bear markets, distribution shifts, and uncertain environments.
Uses Lagrangian multiplier adaptation for constraint enforcement.
"""

from typing import Tuple, List, Dict, Optional, Iterator
import numpy as np
from dataclasses import dataclass
from collections import deque

# Try to import PyTorch; graceful fallback if not available
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.distributions import Normal
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None


@dataclass
class RolloutBuffer:
    """Buffer for storing PPO rollout data."""
    states: List[np.ndarray]
    actions: List[np.ndarray]
    rewards: List[float]
    values: List[float]
    log_probs: List[float]
    dones: List[bool]

    def compute_advantages(
        self,
        gamma: float = 0.99,
        gae_lambda: float = 0.95
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute Generalized Advantage Estimation (GAE).

        Args:
            gamma: Discount factor
            gae_lambda: GAE lambda parameter (0-1)

        Returns:
            advantages: numpy array of computed advantages
            returns: numpy array of discounted returns
        """
        advantages = []
        advantage = 0.0
        returns = []

        # Compute advantages with GAE backward pass
        for t in reversed(range(len(self.rewards))):
            if t == len(self.rewards) - 1:
                next_value = 0.0
            else:
                next_value = self.values[t + 1]

            delta = self.rewards[t] + gamma * next_value * (1 - self.dones[t]) - self.values[t]
            advantage = delta + gamma * gae_lambda * (1 - self.dones[t]) * advantage

            advantages.insert(0, advantage)
            returns.insert(0, advantage + self.values[t])

        advantages_array = np.array(advantages, dtype=np.float32)
        returns_array = np.array(returns, dtype=np.float32)

        # Normalize advantages
        if len(advantages_array) > 1:
            advantages_array = (advantages_array - np.mean(advantages_array)) / (
                np.std(advantages_array) + 1e-8
            )

        return advantages_array, returns_array

    def get_minibatches(self, batch_size: int) -> Iterator[Tuple]:
        """
        Yield minibatches for training.

        Args:
            batch_size: Size of each minibatch

        Yields:
            Tuples of (states, actions, log_probs, advantages, returns, values)
        """
        dataset_size = len(self.states)
        indices = np.arange(dataset_size)
        np.random.shuffle(indices)

        for start_idx in range(0, dataset_size, batch_size):
            batch_indices = indices[start_idx : start_idx + batch_size]

            yield (
                np.array([self.states[i] for i in batch_indices], dtype=np.float32),
                np.array([self.actions[i] for i in batch_indices], dtype=np.float32),
                np.array([self.log_probs[i] for i in batch_indices], dtype=np.float32),
                np.array([self.advantages[i] for i in batch_indices], dtype=np.float32),
                np.array([self.returns[i] for i in batch_indices], dtype=np.float32),
                np.array([self.values[i] for i in batch_indices], dtype=np.float32),
            )

    def finalize(self, gamma: float = 0.99, gae_lambda: float = 0.95):
        """Compute advantages and returns in-place."""
        self.advantages, self.returns = self.compute_advantages(gamma, gae_lambda)


class SharedFeatureExtractor(nn.Module if TORCH_AVAILABLE else object):
    """Shared feature extraction network for actor and critic."""

    def __init__(self, state_dim: int, hidden_dim: int = 256, hidden_dim2: int = 128):
        """
        Initialize feature extractor.

        Args:
            state_dim: Dimension of state space
            hidden_dim: First hidden layer dimension
            hidden_dim2: Second hidden layer dimension
        """
        if TORCH_AVAILABLE:
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(state_dim, hidden_dim),
                nn.LayerNorm(hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim2),
                nn.ReLU(),
            )
        else:
            raise RuntimeError("PyTorch is required for CPPOAgent")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass through feature extractor."""
        return self.net(x)


class ActorHead(nn.Module if TORCH_AVAILABLE else object):
    """Actor network head for policy."""

    def __init__(self, hidden_dim: int = 128, action_dim: int = 1):
        """
        Initialize actor head.

        Args:
            hidden_dim: Input dimension from feature extractor
            action_dim: Dimension of action space
        """
        if TORCH_AVAILABLE:
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(hidden_dim, 64),
                nn.ReLU(),
            )
            self.mean_head = nn.Linear(64, action_dim)
            self.log_std_head = nn.Linear(64, action_dim)

            # Initialize log_std to reasonable value
            nn.init.constant_(self.log_std_head.weight, 0)
            nn.init.constant_(self.log_std_head.bias, -0.5)
        else:
            raise RuntimeError("PyTorch is required for CPPOAgent")

    def forward(self, features: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass through actor head.

        Returns:
            mean: Action mean
            log_std: Log standard deviation (clamped for stability)
        """
        hidden = self.net(features)
        mean = self.mean_head(hidden)
        log_std = torch.clamp(self.log_std_head(hidden), -2, 0.5)
        return mean, log_std


class CriticHead(nn.Module if TORCH_AVAILABLE else object):
    """Critic network head for value function."""

    def __init__(self, hidden_dim: int = 128):
        """
        Initialize critic head.

        Args:
            hidden_dim: Input dimension from feature extractor
        """
        if TORCH_AVAILABLE:
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(hidden_dim, 64),
                nn.ReLU(),
                nn.Linear(64, 1),
            )
        else:
            raise RuntimeError("PyTorch is required for CPPOAgent")

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        """Forward pass through critic head."""
        return self.net(features)


class RiskHead(nn.Module if TORCH_AVAILABLE else object):
    """Risk assessment head for CVaR estimation."""

    def __init__(self, hidden_dim: int = 128, action_dim: int = 1):
        """
        Initialize risk head.

        Args:
            hidden_dim: Input dimension from feature extractor
            action_dim: Dimension of action space
        """
        if TORCH_AVAILABLE:
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(hidden_dim + action_dim, 64),
                nn.ReLU(),
                nn.Linear(64, 1),
            )
        else:
            raise RuntimeError("PyTorch is required for CPPOAgent")

    def forward(self, features: torch.Tensor, actions: torch.Tensor) -> torch.Tensor:
        """Forward pass through risk head."""
        combined = torch.cat([features, actions], dim=-1)
        return self.net(combined)


class CPPOAgent:
    """
    Constrained Proximal Policy Optimization (CPPO) Agent.

    Optimizes expected return while constraining Conditional Value at Risk (CVaR)
    at the 95th percentile to remain below a risk budget. Uses Lagrangian
    multiplier adaptation for constraint enforcement.

    Specialized for bear markets, distribution shifts, high volatility, and crises.
    """

    # Active regimes for this agent
    ACTIVE_REGIMES = {
        "trend_bear",
        "distribution",
        "high_volatility",
        "crisis",
        "volatility_compression",
    }

    # Regime-specific multipliers for constraint adaptation
    REGIME_MULTIPLIERS = {
        "trend_bear": 1.0,
        "distribution": 1.0,
        "high_volatility": 1.0,
        "crisis": 0.3,
        "volatility_compression": 0.8,
        "liquidation_cascade": 0.0,
    }

    def __init__(
        self,
        state_dim: int = 50,
        action_dim: int = 1,
        hidden_dims: Tuple[int, int] = (256, 128),
        lr_actor: float = 3e-4,
        lr_critic: float = 1e-3,
        lr_lagrange: float = 1e-3,
        cvar_alpha: float = 0.95,
        cvar_budget: float = 0.05,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_epsilon: float = 0.2,
        n_epochs: int = 10,
        batch_size: int = 64,
        rollout_length: int = 2048,
    ):
        """
        Initialize CPPO Agent.

        Args:
            state_dim: Dimension of state space
            action_dim: Dimension of action space
            hidden_dims: Tuple of (first_hidden, second_hidden) dimensions
            lr_actor: Learning rate for actor
            lr_critic: Learning rate for critic
            lr_lagrange: Learning rate for Lagrangian multiplier
            cvar_alpha: CVaR percentile (0.95 = 95th percentile)
            cvar_budget: Maximum allowed CVaR value
            gamma: Discount factor
            gae_lambda: GAE lambda parameter
            clip_epsilon: PPO clipping epsilon
            n_epochs: Number of training epochs per rollout
            batch_size: Minibatch size for training
            rollout_length: Number of steps to collect before training
        """
        if not TORCH_AVAILABLE:
            raise RuntimeError("PyTorch is required for CPPOAgent")

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.hidden_dims = hidden_dims
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.clip_epsilon = clip_epsilon
        self.n_epochs = n_epochs
        self.batch_size = batch_size
        self.rollout_length = rollout_length

        # CVaR parameters
        self.cvar_alpha = cvar_alpha
        self.cvar_budget = cvar_budget
        self.cvar_alpha_percentile = int((1 - cvar_alpha) * 100)

        # Lagrangian multiplier for constraint
        self.lagrange_multiplier = 0.1
        self.lr_lagrange = lr_lagrange

        # Build networks
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device

        self.feature_extractor = SharedFeatureExtractor(
            state_dim, hidden_dims[0], hidden_dims[1]
        ).to(device)
        self.actor_head = ActorHead(hidden_dims[1], action_dim).to(device)
        self.critic_head = CriticHead(hidden_dims[1]).to(device)
        self.risk_head = RiskHead(hidden_dims[1], action_dim).to(device)

        # Optimizers
        self.optimizer_actor = optim.Adam(
            list(self.feature_extractor.parameters()) +
            list(self.actor_head.parameters()),
            lr=lr_actor
        )
        self.optimizer_critic = optim.Adam(
            list(self.feature_extractor.parameters()) +
            list(self.critic_head.parameters()),
            lr=lr_critic
        )
        self.optimizer_risk = optim.Adam(self.risk_head.parameters(), lr=lr_critic)

        # Tracking
        self.episode_returns = deque(maxlen=100)
        self.episode_cvars = deque(maxlen=100)

    def is_active_in_regime(self, regime: str) -> bool:
        """
        Check if this agent should be active in the given regime.

        Args:
            regime: Market regime identifier

        Returns:
            True if agent is active in this regime
        """
        return regime in self.ACTIVE_REGIMES

    def get_regime_multiplier(self, regime: str) -> float:
        """
        Get regime-specific multiplier for constraint adaptation.

        Args:
            regime: Market regime identifier

        Returns:
            Multiplier for CVaR constraint (0.0 to 1.0)
        """
        return self.REGIME_MULTIPLIERS.get(regime, 0.5)

    def select_action(
        self,
        state: np.ndarray,
        explore: bool = True,
    ) -> Tuple[np.ndarray, float, float]:
        """
        Select action for given state.

        Args:
            state: Current state (numpy array)
            explore: Whether to sample from distribution (True) or use mean (False)

        Returns:
            action: Selected action
            log_prob: Log probability of action under current policy
            value: Estimated value of state
        """
        state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)

        with torch.no_grad():
            features = self.feature_extractor(state_tensor)
            mean, log_std = self.actor_head(features)
            value = self.critic_head(features)

            # Create action distribution
            std = torch.exp(log_std)
            dist = Normal(mean, std)

            if explore:
                action_tensor = dist.rsample()
            else:
                action_tensor = mean

            log_prob = dist.log_prob(action_tensor).sum(dim=-1)

        action = action_tensor.cpu().numpy().flatten()
        log_prob_value = log_prob.cpu().numpy().item()
        value_value = value.cpu().numpy().item()

        return action, log_prob_value, value_value

    @staticmethod
    def compute_cvar(returns: List[float], alpha: float = 0.95) -> float:
        """
        Compute Conditional Value at Risk (CVaR).

        CVaR at confidence level α is the mean of the worst (1-α) fraction
        of returns (worst returns, i.e., lowest values).

        Args:
            returns: List of returns/losses
            alpha: Confidence level (default 0.95)

        Returns:
            CVaR value (negative for losses)
        """
        if len(returns) == 0:
            return 0.0

        returns_sorted = np.sort(returns)
        worst_count = max(1, int(np.ceil(len(returns) * (1 - alpha))))
        cvar = np.mean(returns_sorted[:worst_count])

        return float(cvar)

    def collect_rollout(
        self,
        env,
        n_steps: int = 2048,
    ) -> Tuple[RolloutBuffer, List[float]]:
        """
        Collect rollout data from environment.

        Args:
            env: Trading environment with step() and reset() methods
            n_steps: Number of steps to collect

        Returns:
            RolloutBuffer with collected data
            List of episode returns for CVaR computation
        """
        buffer = RolloutBuffer(
            states=[],
            actions=[],
            rewards=[],
            values=[],
            log_probs=[],
            dones=[],
        )

        episode_returns = []
        episode_return = 0.0

        state = env.reset()

        for _ in range(n_steps):
            action, log_prob, value = self.select_action(state, explore=True)
            next_state, reward, done, _ = env.step(action)

            buffer.states.append(state.copy())
            buffer.actions.append(action.copy())
            buffer.rewards.append(float(reward))
            buffer.values.append(float(value))
            buffer.log_probs.append(float(log_prob))
            buffer.dones.append(bool(done))

            episode_return += reward

            if done:
                episode_returns.append(episode_return)
                episode_return = 0.0
                state = env.reset()
            else:
                state = next_state

        buffer.finalize(self.gamma, self.gae_lambda)
        return buffer, episode_returns

    def train_on_rollout(self, rollout: RolloutBuffer) -> Dict[str, float]:
        """
        Train on collected rollout data.

        Performs multiple epochs of PPO training with:
        - Actor loss: PPO clipped objective + Lagrangian CVaR constraint
        - Critic loss: MSE value prediction
        - CVaR constraint enforcement via Lagrangian multiplier

        Args:
            rollout: RolloutBuffer with collected experience

        Returns:
            Dictionary with training metrics
        """
        states = torch.FloatTensor(np.array(rollout.states)).to(self.device)
        actions = torch.FloatTensor(np.array(rollout.actions)).to(self.device)
        old_log_probs = torch.FloatTensor(rollout.log_probs).to(self.device)
        advantages = torch.FloatTensor(rollout.advantages).to(self.device)
        returns = torch.FloatTensor(rollout.returns).to(self.device)

        # Compute CVaR from episode returns
        cvar_value = self.compute_cvar(
            rollout.episode_returns if hasattr(rollout, 'episode_returns')
            else rollout.rewards,
            alpha=self.cvar_alpha
        )
        self.episode_cvars.append(cvar_value)

        policy_loss_sum = 0.0
        value_loss_sum = 0.0
        cvar_loss_sum = 0.0
        n_batches = 0

        for epoch in range(self.n_epochs):
            for batch_data in rollout.get_minibatches(self.batch_size):
                batch_states, batch_actions, batch_old_lp, batch_advantages, batch_returns, batch_values = batch_data

                batch_states_t = torch.FloatTensor(batch_states).to(self.device)
                batch_actions_t = torch.FloatTensor(batch_actions).to(self.device)
                batch_old_lp_t = torch.FloatTensor(batch_old_lp).to(self.device)
                batch_advantages_t = torch.FloatTensor(batch_advantages).to(self.device)
                batch_returns_t = torch.FloatTensor(batch_returns).to(self.device)

                # Forward pass
                features = self.feature_extractor(batch_states_t)
                mean, log_std = self.actor_head(features)
                value = self.critic_head(features).squeeze(-1)

                # Actor loss with PPO clipping
                std = torch.exp(log_std)
                dist = Normal(mean, std)
                new_log_probs = dist.log_prob(batch_actions_t).sum(dim=-1)

                ratio = torch.exp(new_log_probs - batch_old_lp_t)
                surr1 = ratio * batch_advantages_t
                surr2 = torch.clamp(ratio, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * batch_advantages_t
                ppo_loss = -torch.min(surr1, surr2).mean()

                # CVaR constraint loss
                risk_pred = self.risk_head(features, batch_actions_t).squeeze(-1)
                cvar_loss = torch.clamp(cvar_value - self.cvar_budget, min=0.0)
                lagrange_loss = self.lagrange_multiplier * cvar_loss

                actor_loss = ppo_loss + lagrange_loss

                # Critic loss with value clipping
                value_loss = nn.functional.mse_loss(value, batch_returns_t)

                # Update actor
                self.optimizer_actor.zero_grad()
                actor_loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    list(self.feature_extractor.parameters()) +
                    list(self.actor_head.parameters()),
                    max_norm=0.5
                )
                self.optimizer_actor.step()

                # Update critic
                self.optimizer_critic.zero_grad()
                value_loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    list(self.feature_extractor.parameters()) +
                    list(self.critic_head.parameters()),
                    max_norm=0.5
                )
                self.optimizer_critic.step()

                # Update risk head
                self.optimizer_risk.zero_grad()
                risk_loss = torch.mean(risk_pred ** 2)  # Predict risk magnitude
                risk_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.risk_head.parameters(), max_norm=0.5)
                self.optimizer_risk.step()

                policy_loss_sum += ppo_loss.item()
                value_loss_sum += value_loss.item()
                cvar_loss_sum += cvar_loss.item()
                n_batches += 1

        # Update Lagrange multiplier
        self.lagrange_multiplier = max(
            0.0,
            self.lagrange_multiplier + self.lr_lagrange * (cvar_value - self.cvar_budget)
        )

        return {
            "policy_loss": policy_loss_sum / n_batches if n_batches > 0 else 0.0,
            "value_loss": value_loss_sum / n_batches if n_batches > 0 else 0.0,
            "cvar_loss": cvar_loss_sum / n_batches if n_batches > 0 else 0.0,
            "lagrange_multiplier": self.lagrange_multiplier,
            "cvar_value": cvar_value,
        }

    def save(self, path: str):
        """
        Save agent state to file.

        Args:
            path: File path for saving
        """
        checkpoint = {
            "feature_extractor": self.feature_extractor.state_dict(),
            "actor_head": self.actor_head.state_dict(),
            "critic_head": self.critic_head.state_dict(),
            "risk_head": self.risk_head.state_dict(),
            "lagrange_multiplier": self.lagrange_multiplier,
            "hyperparams": {
                "state_dim": self.state_dim,
                "action_dim": self.action_dim,
                "hidden_dims": self.hidden_dims,
                "gamma": self.gamma,
                "gae_lambda": self.gae_lambda,
                "clip_epsilon": self.clip_epsilon,
                "cvar_alpha": self.cvar_alpha,
                "cvar_budget": self.cvar_budget,
            }
        }
        torch.save(checkpoint, path)

    def load(self, path: str):
        """
        Load agent state from file.

        Args:
            path: File path for loading
        """
        checkpoint = torch.load(path, map_location=self.device)
        self.feature_extractor.load_state_dict(checkpoint["feature_extractor"])
        self.actor_head.load_state_dict(checkpoint["actor_head"])
        self.critic_head.load_state_dict(checkpoint["critic_head"])
        self.risk_head.load_state_dict(checkpoint["risk_head"])
        self.lagrange_multiplier = checkpoint["lagrange_multiplier"]
