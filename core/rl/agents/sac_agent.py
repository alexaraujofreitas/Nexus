"""
Soft Actor-Critic (SAC) Agent for Continuous Action Trading.

Implements a state-of-the-art reinforcement learning agent using the Soft Actor-Critic
algorithm for continuous action spaces. Compatible with PyTorch; graceful fallback if
PyTorch unavailable.
"""

import numpy as np
from typing import Tuple, Dict, Optional, List
from collections import deque
import warnings

# Attempt PyTorch import
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    warnings.warn("PyTorch not available. SAC agent will use fallback mode (flat actions).")


class ReplayBuffer:
    """
    Experience replay buffer for storing and sampling transitions.

    Stores (state, action, reward, next_state, done) tuples and provides
    efficient sampling for training.
    """

    def __init__(self, capacity: int = 100000) -> None:
        """
        Initialize replay buffer.

        Args:
            capacity: Maximum number of transitions to store
        """
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)

    def add(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ) -> None:
        """
        Add a transition to the buffer.

        Args:
            state: Current state
            action: Action taken
            reward: Reward received
            next_state: Next state
            done: Whether episode terminated
        """
        self.buffer.append((state, action, reward, next_state, done))

    def sample(self, batch_size: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Sample a batch of transitions.

        Args:
            batch_size: Number of transitions to sample

        Returns:
            Tuple of (states, actions, rewards, next_states, dones) as PyTorch tensors
        """
        if not TORCH_AVAILABLE:
            raise RuntimeError("PyTorch required for sampling from replay buffer")

        indices = np.random.randint(0, len(self.buffer), size=batch_size)
        batch = [self.buffer[i] for i in indices]

        states, actions, rewards, next_states, dones = zip(*batch)

        states = torch.FloatTensor(np.array(states))
        actions = torch.FloatTensor(np.array(actions))
        rewards = torch.FloatTensor(np.array(rewards)).unsqueeze(1)
        next_states = torch.FloatTensor(np.array(next_states))
        dones = torch.FloatTensor(np.array(dones)).unsqueeze(1)

        return states, actions, rewards, next_states, dones

    def __len__(self) -> int:
        """Return current buffer size."""
        return len(self.buffer)


class ActorNetwork(nn.Module):
    """
    Actor network for policy approximation.

    Outputs mean and log standard deviation for Gaussian policy
    with tanh squashing for continuous action space.
    """

    def __init__(
        self,
        state_dim: int,
        action_dim: int,
        hidden_dims: Tuple[int, int, int] = (256, 256, 128),
    ) -> None:
        """
        Initialize actor network.

        Args:
            state_dim: Dimension of state space
            action_dim: Dimension of action space
            hidden_dims: Tuple of hidden layer dimensions
        """
        super(ActorNetwork, self).__init__()

        self.state_dim = state_dim
        self.action_dim = action_dim

        # MLP: state -> hidden -> hidden -> hidden -> (mean, log_std)
        self.fc1 = nn.Linear(state_dim, hidden_dims[0])
        self.fc2 = nn.Linear(hidden_dims[0], hidden_dims[1])
        self.fc3 = nn.Linear(hidden_dims[1], hidden_dims[2])

        self.mean = nn.Linear(hidden_dims[2], action_dim)
        self.log_std = nn.Linear(hidden_dims[2], action_dim)

    def forward(self, state: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass through actor.

        Args:
            state: State tensor

        Returns:
            Tuple of (mean, log_std)
        """
        x = F.relu(self.fc1(state))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))

        mean = self.mean(x)
        log_std = self.log_std(x)

        # Clip log_std to reasonable range
        log_std = torch.clamp(log_std, min=-20, max=2)

        return mean, log_std

    def sample_action(self, state: torch.Tensor, deterministic: bool = False) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Sample action using reparameterization trick.

        Args:
            state: State tensor
            deterministic: If True, use mean without noise

        Returns:
            Tuple of (action, log_prob, tanh_mean)
        """
        mean, log_std = self.forward(state)
        std = torch.exp(log_std)

        if deterministic:
            action = torch.tanh(mean)
            return action, torch.zeros(1), action

        # Reparameterization trick
        normal = torch.distributions.Normal(mean, std)
        z = normal.rsample()

        action = torch.tanh(z)
        log_prob = normal.log_prob(z) - torch.log(1 - action.pow(2) + 1e-6)
        log_prob = log_prob.sum(dim=1, keepdim=True)

        return action, log_prob, z


class CriticNetwork(nn.Module):
    """
    Critic network for Q-value approximation.

    Takes state and action as input and outputs Q-value.
    Used for both Q1 and Q2 networks in twin critic architecture.
    """

    def __init__(
        self,
        state_dim: int,
        action_dim: int,
        hidden_dims: Tuple[int, int, int] = (256, 256, 128),
    ) -> None:
        """
        Initialize critic network.

        Args:
            state_dim: Dimension of state space
            action_dim: Dimension of action space
            hidden_dims: Tuple of hidden layer dimensions
        """
        super(CriticNetwork, self).__init__()

        # MLP: (state, action) -> hidden -> hidden -> hidden -> Q-value
        self.fc1 = nn.Linear(state_dim + action_dim, hidden_dims[0])
        self.fc2 = nn.Linear(hidden_dims[0], hidden_dims[1])
        self.fc3 = nn.Linear(hidden_dims[1], hidden_dims[2])
        self.q_value = nn.Linear(hidden_dims[2], 1)

    def forward(self, state: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
        """
        Forward pass through critic.

        Args:
            state: State tensor
            action: Action tensor

        Returns:
            Q-value tensor
        """
        x = torch.cat([state, action], dim=1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        q = self.q_value(x)

        return q


class SACAgent:
    """
    Soft Actor-Critic (SAC) agent for continuous trading actions.

    Implements SAC algorithm with automatic entropy tuning, twin critics,
    and target networks. Works across all market regimes with adaptive
    action scaling based on regime risk.

    Architecture:
    - Actor: 3-layer MLP (256, 256, 128) with tanh output
    - Critic1, Critic2: Twin Q-networks 3-layer MLP (256, 256, 128)
    - Target networks for both critics
    - Entropy tuning: automatic temperature parameter alpha
    """

    def __init__(
        self,
        state_dim: int = 50,
        action_dim: int = 1,
        hidden_dims: Tuple[int, int, int] = (256, 256, 128),
        lr_actor: float = 3e-4,
        lr_critic: float = 3e-4,
        lr_alpha: float = 3e-4,
        gamma: float = 0.99,
        tau: float = 0.005,
        alpha_init: float = 0.2,
        buffer_size: int = 100000,
        batch_size: int = 256,
    ) -> None:
        """
        Initialize SAC agent.

        Args:
            state_dim: Dimension of state space (default: 50)
            action_dim: Dimension of action space (default: 1 for continuous position)
            hidden_dims: Tuple of hidden layer dimensions for networks
            lr_actor: Learning rate for actor
            lr_critic: Learning rate for critics
            lr_alpha: Learning rate for temperature parameter
            gamma: Discount factor
            tau: Target network update rate (soft update)
            alpha_init: Initial temperature parameter
            buffer_size: Maximum size of replay buffer
            batch_size: Batch size for training
        """
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.gamma = gamma
        self.tau = tau
        self.batch_size = batch_size
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if TORCH_AVAILABLE else None

        if not TORCH_AVAILABLE:
            self.is_torch_available = False
            self.buffer = None
            self.actor = None
            self.critic1 = None
            self.critic2 = None
            self.target_critic1 = None
            self.target_critic2 = None
            return

        self.is_torch_available = True

        # Experience replay buffer
        self.buffer = ReplayBuffer(capacity=buffer_size)

        # Actor network
        self.actor = ActorNetwork(state_dim, action_dim, hidden_dims).to(self.device)
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=lr_actor)

        # Critic networks (twin critics)
        self.critic1 = CriticNetwork(state_dim, action_dim, hidden_dims).to(self.device)
        self.critic2 = CriticNetwork(state_dim, action_dim, hidden_dims).to(self.device)
        self.critic1_optimizer = optim.Adam(self.critic1.parameters(), lr=lr_critic)
        self.critic2_optimizer = optim.Adam(self.critic2.parameters(), lr=lr_critic)

        # Target networks
        self.target_critic1 = CriticNetwork(state_dim, action_dim, hidden_dims).to(self.device)
        self.target_critic2 = CriticNetwork(state_dim, action_dim, hidden_dims).to(self.device)
        self._hard_update_targets()

        # Entropy tuning
        self.log_alpha = torch.tensor(np.log(alpha_init), dtype=torch.float32, requires_grad=True, device=self.device)
        self.alpha_optimizer = optim.Adam([self.log_alpha], lr=lr_alpha)
        self.target_entropy = -action_dim

        # Training step counter
        self.total_steps = 0

    def select_action(self, state: np.ndarray, explore: bool = True) -> np.ndarray:
        """
        Select action given state.

        Args:
            state: Current state (numpy array)
            explore: If True, sample action; if False, use mean action

        Returns:
            Action as numpy array
        """
        if not self.is_torch_available:
            # Fallback: return zero action
            return np.array([0.0], dtype=np.float32)

        state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)

        with torch.no_grad():
            action, _, _ = self.actor.sample_action(state_tensor, deterministic=not explore)
            action = action.cpu().numpy()[0]

        return action.astype(np.float32)

    def train_step(self) -> Dict[str, float]:
        """
        Execute one training step.

        Performs updates on actor, critics, and temperature parameter
        using samples from replay buffer.

        Returns:
            Dictionary with loss metrics: {"critic_loss", "actor_loss", "alpha_loss", "alpha"}
        """
        if not self.is_torch_available:
            return {"critic_loss": 0.0, "actor_loss": 0.0, "alpha_loss": 0.0, "alpha": 0.0}

        if len(self.buffer) < self.batch_size * 2:
            return {"critic_loss": 0.0, "actor_loss": 0.0, "alpha_loss": 0.0, "alpha": 0.0}

        # Sample batch
        states, actions, rewards, next_states, dones = self.buffer.sample(self.batch_size)
        states = states.to(self.device)
        actions = actions.to(self.device)
        rewards = rewards.to(self.device)
        next_states = next_states.to(self.device)
        dones = dones.to(self.device)

        # ===== Critic Update =====
        alpha = torch.exp(self.log_alpha).detach()

        with torch.no_grad():
            # Next action and log prob from current policy
            next_actions, next_log_probs, _ = self.actor.sample_action(next_states, deterministic=False)

            # Target Q-values
            target_q1 = self.target_critic1(next_states, next_actions)
            target_q2 = self.target_critic2(next_states, next_actions)
            target_q = torch.min(target_q1, target_q2) - alpha * next_log_probs

            # Bellman backup
            target_q = rewards + (1 - dones) * self.gamma * target_q

        # Compute critic losses
        q1 = self.critic1(states, actions)
        q2 = self.critic2(states, actions)
        critic1_loss = F.mse_loss(q1, target_q)
        critic2_loss = F.mse_loss(q2, target_q)
        critic_loss = critic1_loss + critic2_loss

        # Update critics
        self.critic1_optimizer.zero_grad()
        self.critic2_optimizer.zero_grad()
        critic_loss.backward()
        self.critic1_optimizer.step()
        self.critic2_optimizer.step()

        # ===== Actor Update =====
        new_actions, log_probs, _ = self.actor.sample_action(states, deterministic=False)
        q1_new = self.critic1(states, new_actions)
        q2_new = self.critic2(states, new_actions)
        q_new = torch.min(q1_new, q2_new)

        # Actor loss: minimize negative expected Q-value minus entropy
        actor_loss = (alpha * log_probs - q_new).mean()

        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        # ===== Alpha (Temperature) Update =====
        alpha_loss = -(self.log_alpha * (log_probs.detach() + self.target_entropy).detach()).mean()

        self.alpha_optimizer.zero_grad()
        alpha_loss.backward()
        self.alpha_optimizer.step()

        # Update target networks
        self._soft_update_targets()

        self.total_steps += 1

        return {
            "critic_loss": critic_loss.item(),
            "actor_loss": actor_loss.item(),
            "alpha_loss": alpha_loss.item(),
            "alpha": alpha.item(),
        }

    def _soft_update_targets(self) -> None:
        """Soft update of target networks using tau parameter."""
        for target_param, param in zip(self.target_critic1.parameters(), self.critic1.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

        for target_param, param in zip(self.target_critic2.parameters(), self.critic2.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def _hard_update_targets(self) -> None:
        """Hard update of target networks (copy weights)."""
        for target_param, param in zip(self.target_critic1.parameters(), self.critic1.parameters()):
            target_param.data.copy_(param.data)

        for target_param, param in zip(self.target_critic2.parameters(), self.critic2.parameters()):
            target_param.data.copy_(param.data)

    def add_experience(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ) -> None:
        """
        Add experience to replay buffer.

        Args:
            state: Current state
            action: Action taken
            reward: Reward received
            next_state: Next state
            done: Whether episode terminated
        """
        if not self.is_torch_available:
            return

        self.buffer.add(state, action, reward, next_state, done)

    @property
    def is_ready(self) -> bool:
        """
        Check if agent has sufficient experience to start training.

        Returns:
            True if buffer has enough samples, False otherwise
        """
        if not self.is_torch_available or self.buffer is None:
            return False

        return len(self.buffer) >= self.batch_size * 2

    def save(self, path: str) -> None:
        """
        Save agent checkpoint.

        Args:
            path: Path to save checkpoint
        """
        if not self.is_torch_available:
            warnings.warn("Cannot save agent without PyTorch")
            return

        checkpoint = {
            "actor": self.actor.state_dict(),
            "critic1": self.critic1.state_dict(),
            "critic2": self.critic2.state_dict(),
            "target_critic1": self.target_critic1.state_dict(),
            "target_critic2": self.target_critic2.state_dict(),
            "actor_optimizer": self.actor_optimizer.state_dict(),
            "critic1_optimizer": self.critic1_optimizer.state_dict(),
            "critic2_optimizer": self.critic2_optimizer.state_dict(),
            "alpha_optimizer": self.alpha_optimizer.state_dict(),
            "log_alpha": self.log_alpha.detach(),
            "total_steps": self.total_steps,
        }

        torch.save(checkpoint, path)

    def load(self, path: str) -> None:
        """
        Load agent checkpoint.

        Args:
            path: Path to load checkpoint from
        """
        if not self.is_torch_available:
            warnings.warn("Cannot load agent without PyTorch")
            return

        checkpoint = torch.load(path, map_location=self.device)

        self.actor.load_state_dict(checkpoint["actor"])
        self.critic1.load_state_dict(checkpoint["critic1"])
        self.critic2.load_state_dict(checkpoint["critic2"])
        self.target_critic1.load_state_dict(checkpoint["target_critic1"])
        self.target_critic2.load_state_dict(checkpoint["target_critic2"])
        self.actor_optimizer.load_state_dict(checkpoint["actor_optimizer"])
        self.critic1_optimizer.load_state_dict(checkpoint["critic1_optimizer"])
        self.critic2_optimizer.load_state_dict(checkpoint["critic2_optimizer"])
        self.alpha_optimizer.load_state_dict(checkpoint["alpha_optimizer"])
        self.log_alpha = checkpoint["log_alpha"].requires_grad_(True)
        self.total_steps = checkpoint["total_steps"]

    @staticmethod
    def get_regime_multiplier(regime: str) -> float:
        """
        Get action scale modifier based on market regime.

        Args:
            regime: Market regime name

        Returns:
            Action scale multiplier (0.0 to 1.0)
        """
        regime_multipliers = {
            "crisis": 0.0,
            "liquidation_cascade": 0.0,
            "flash_crash": 0.2,
            "heavy_volatility": 0.4,
            "shock_wave": 0.5,
            "consolidation": 0.8,
            "trending": 1.0,
            "breakout": 1.0,
            "mean_reversion": 0.9,
            "quiet_accumulation": 0.7,
            "distribution": 0.6,
            "normal": 1.0,
        }

        return regime_multipliers.get(regime, 1.0)
