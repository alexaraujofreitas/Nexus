"""
Duelling Deep Q-Network (DQN) with Prioritized Experience Replay (PER).

Implements a discrete action Q-learning agent with duelling architecture and
prioritized experience replay for efficient learning in ranging, accumulation,
and volatility compression market regimes.
"""

from typing import Tuple, Dict, Optional, List
import numpy as np
from dataclasses import dataclass
from collections import deque
import heapq

# Try to import PyTorch; graceful fallback if not available
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None


class SumTree:
    """
    Sum tree data structure for efficient priority sampling in O(log n) time.
    Used by Prioritized Experience Replay.
    """

    def __init__(self, capacity: int):
        """
        Initialize sum tree.

        Args:
            capacity: Maximum number of transitions to store
        """
        self.capacity = capacity
        self.tree = np.zeros(2 * capacity - 1)
        self.data = np.zeros(capacity, dtype=object)
        self.write = 0
        self.n_entries = 0

    def add(self, priority: float, data: object):
        """
        Add new transition with priority.

        Args:
            priority: Priority value (TD error)
            data: Transition data to store
        """
        tree_idx = self.write + self.capacity - 1
        self.data[self.write] = data
        self.update(tree_idx, priority)

        self.write += 1
        if self.write >= self.capacity:
            self.write = 0

        if self.n_entries < self.capacity:
            self.n_entries += 1

    def update(self, tree_idx: int, priority: float):
        """
        Update priority at tree index.

        Args:
            tree_idx: Index in tree array
            priority: New priority value
        """
        delta = priority - self.tree[tree_idx]
        self.tree[tree_idx] = priority

        # Propagate change up tree
        parent_idx = (tree_idx - 1) // 2
        while parent_idx >= 0:
            self.tree[parent_idx] += delta
            parent_idx = (parent_idx - 1) // 2

    def sample(self, z: float) -> Tuple[int, object, float]:
        """
        Sample transition using prioritized sampling.

        Args:
            z: Random value in [0, sum of all priorities)

        Returns:
            data_idx: Index in data array
            data: Transition data
            priority: Priority value
        """
        tree_idx = self._retrieve(0, z)
        data_idx = tree_idx - self.capacity + 1
        return data_idx, self.data[data_idx], self.tree[tree_idx]

    def _retrieve(self, idx: int, z: float) -> int:
        """Recursively retrieve leaf index from priority value."""
        left_child = 2 * idx + 1
        right_child = 2 * idx + 2

        if left_child >= len(self.tree):
            return idx

        if z <= self.tree[left_child]:
            return self._retrieve(left_child, z)
        else:
            return self._retrieve(right_child, z - self.tree[left_child])

    def total_priority(self) -> float:
        """Get total sum of priorities."""
        return self.tree[0]

    def min_priority(self) -> float:
        """Get minimum priority among entries."""
        if self.n_entries == 0:
            return 1.0
        leaf_start = self.capacity - 1
        leaf_end = leaf_start + self.n_entries
        return np.min(self.tree[leaf_start:leaf_end])


class PrioritizedReplayBuffer:
    """
    Prioritized Experience Replay Buffer using Sum Tree.

    Samples transitions with probability proportional to their TD error
    (priority), enabling more efficient learning on high-error experiences.
    """

    def __init__(
        self,
        capacity: int = 100000,
        alpha: float = 0.6,
        beta: float = 0.4,
    ):
        """
        Initialize PER buffer.

        Args:
            capacity: Maximum buffer size
            alpha: How much prioritization is used (0=no priority, 1=full priority)
            beta: Importance sampling exponent for bias correction
        """
        self.capacity = capacity
        self.alpha = alpha
        self.beta = beta
        self.beta_increment_per_sampling = 0.001  # For annealing beta toward 1.0

        self.tree = SumTree(capacity)
        self.max_priority = 1.0

    def add(self, state: np.ndarray, action: int, reward: float,
            next_state: np.ndarray, done: bool, priority: Optional[float] = None):
        """
        Add transition to buffer.

        Args:
            state: Current state
            action: Action taken
            reward: Reward received
            next_state: Next state
            done: Whether episode terminated
            priority: Priority (TD error). If None, uses max_priority.
        """
        transition = (state, action, reward, next_state, done)

        if priority is None:
            priority = self.max_priority

        priority = np.power(priority + 1e-6, self.alpha)
        self.tree.add(priority, transition)
        self.max_priority = max(self.max_priority, priority)

    def sample(
        self,
        batch_size: int,
    ) -> Tuple[List, List[int], np.ndarray]:
        """
        Sample minibatch with prioritized sampling.

        Args:
            batch_size: Size of minibatch

        Returns:
            transitions: List of (state, action, reward, next_state, done) tuples
            indices: Tree indices for priority updates
            weights: Importance sampling weights
        """
        transitions = []
        indices = []
        weights = []

        total_priority = self.tree.total_priority()
        priority_segment = total_priority / batch_size
        min_priority = self.tree.min_priority()
        max_weight = np.power(self.capacity * min_priority / total_priority, -self.beta)

        for i in range(batch_size):
            a = priority_segment * i
            b = priority_segment * (i + 1)
            z = np.random.uniform(a, b)

            idx, transition, priority = self.tree.sample(z)
            transitions.append(transition)
            indices.append(idx)

            # Importance sampling weight
            priority_prob = priority / total_priority
            weight = np.power(self.capacity * priority_prob, -self.beta)
            weights.append(weight / max_weight)

        # Increase beta toward 1.0 (reduce importance sampling correction)
        self.beta = min(1.0, self.beta + self.beta_increment_per_sampling)

        return transitions, indices, np.array(weights, dtype=np.float32)

    def update_priorities(self, indices: List[int], td_errors: np.ndarray):
        """
        Update priorities based on TD errors.

        Args:
            indices: Tree indices to update
            td_errors: TD error magnitudes
        """
        for idx, td_error in zip(indices, td_errors):
            priority = np.power(np.abs(td_error) + 1e-6, self.alpha)
            self.tree.update(self.capacity - 1 + idx, priority)
            self.max_priority = max(self.max_priority, priority)


class DuellingNetwork(nn.Module if TORCH_AVAILABLE else object):
    """
    Duelling architecture separates value and advantage streams.

    Q(s,a) = V(s) + A(s,a) - mean(A(s,:))
    """

    def __init__(
        self,
        state_dim: int,
        n_actions: int,
        hidden_dim: int = 256,
    ):
        """
        Initialize duelling network.

        Args:
            state_dim: State space dimension
            n_actions: Number of discrete actions
            hidden_dim: Hidden layer dimension
        """
        if TORCH_AVAILABLE:
            super().__init__()
            self.feature_layer = nn.Sequential(
                nn.Linear(state_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
            )

            self.value_stream = nn.Sequential(
                nn.Linear(hidden_dim, 128),
                nn.ReLU(),
                nn.Linear(128, 1),
            )

            self.advantage_stream = nn.Sequential(
                nn.Linear(hidden_dim, 128),
                nn.ReLU(),
                nn.Linear(128, n_actions),
            )
        else:
            raise RuntimeError("PyTorch is required for DuellingDQNAgent")

    def forward(self, state: torch.Tensor) -> torch.Tensor:
        """
        Forward pass returning Q values.

        Args:
            state: Input state

        Returns:
            Q values for all actions
        """
        features = self.feature_layer(state)
        value = self.value_stream(features)
        advantages = self.advantage_stream(features)

        # Duelling aggregation: Q = V + A - mean(A)
        q_values = value + advantages - advantages.mean(dim=1, keepdim=True)
        return q_values


class DuellingDQNAgent:
    """
    Duelling Deep Q-Network with Prioritized Experience Replay.

    Implements discrete action Q-learning with duelling network architecture
    for efficient value function approximation. Uses prioritized experience
    replay to focus learning on high-error experiences.

    Discrete actions map to position sizes:
    - 0: STRONG_LONG (1.0)
    - 1: LONG (0.5)
    - 2: FLAT (0.0)
    - 3: SHORT (-0.5)
    - 4: STRONG_SHORT (-1.0)

    Specialized for ranging, accumulation, and volatility compression regimes.
    """

    # Discrete action mapping to position sizes
    ACTION_SIZES = {
        0: 1.0,      # STRONG_LONG
        1: 0.5,      # LONG
        2: 0.0,      # FLAT
        3: -0.5,     # SHORT
        4: -1.0,     # STRONG_SHORT
    }

    # Active regimes for this agent
    ACTIVE_REGIMES = {
        "ranging",
        "accumulation",
        "volatility_compression",
        "squeeze",
        "recovery",
    }

    # Regime-specific multipliers
    REGIME_MULTIPLIERS = {
        "ranging": 1.0,
        "accumulation": 1.0,
        "volatility_compression": 1.0,
        "squeeze": 0.8,
        "recovery": 0.7,
        "crisis": 0.0,
        "liquidation_cascade": 0.0,
    }

    def __init__(
        self,
        state_dim: int = 50,
        n_actions: int = 5,
        hidden_dim: int = 256,
        lr: float = 1e-3,
        gamma: float = 0.99,
        epsilon_start: float = 1.0,
        epsilon_end: float = 0.05,
        epsilon_decay: int = 50000,
        target_update_freq: int = 1000,
        buffer_size: int = 100000,
        batch_size: int = 256,
        per_alpha: float = 0.6,
        per_beta: float = 0.4,
    ):
        """
        Initialize Duelling DQN Agent.

        Args:
            state_dim: Dimension of state space
            n_actions: Number of discrete actions (default 5)
            hidden_dim: Hidden layer dimension for networks
            lr: Learning rate for Q-network
            gamma: Discount factor
            epsilon_start: Starting exploration rate
            epsilon_end: Minimum exploration rate
            epsilon_decay: Steps to decay epsilon from start to end
            target_update_freq: Update target network every N steps
            buffer_size: Capacity of experience replay buffer
            batch_size: Minibatch size for training
            per_alpha: PER alpha (prioritization exponent)
            per_beta: PER beta (importance sampling exponent)
        """
        if not TORCH_AVAILABLE:
            raise RuntimeError("PyTorch is required for DuellingDQNAgent")

        self.state_dim = state_dim
        self.n_actions = n_actions
        self.hidden_dim = hidden_dim
        self.gamma = gamma
        self.batch_size = batch_size
        self.target_update_freq = target_update_freq

        # Exploration schedule
        self.epsilon_start = epsilon_start
        self.epsilon_end = epsilon_end
        self.epsilon_decay = epsilon_decay
        self._steps = 0

        # Device
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Networks
        self.online_network = DuellingNetwork(state_dim, n_actions, hidden_dim).to(
            self.device
        )
        self.target_network = DuellingNetwork(state_dim, n_actions, hidden_dim).to(
            self.device
        )
        self.target_network.load_state_dict(self.online_network.state_dict())
        self.target_network.eval()

        # Optimizer
        self.optimizer = optim.Adam(self.online_network.parameters(), lr=lr)

        # Experience replay buffer
        self.replay_buffer = PrioritizedReplayBuffer(
            capacity=buffer_size,
            alpha=per_alpha,
            beta=per_beta,
        )

        # Tracking
        self.episode_returns = deque(maxlen=100)

    @property
    def epsilon(self) -> float:
        """Get current exploration rate with linear decay."""
        progress = min(self._steps / self.epsilon_decay, 1.0)
        return self.epsilon_start + (self.epsilon_end - self.epsilon_start) * progress

    @property
    def steps(self) -> int:
        """Get total training steps."""
        return self._steps

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
        Get regime-specific multiplier for learning rate adaptation.

        Args:
            regime: Market regime identifier

        Returns:
            Multiplier for learning rate (0.0 to 1.0)
        """
        return self.REGIME_MULTIPLIERS.get(regime, 0.5)

    def select_action(
        self,
        state: np.ndarray,
        explore: bool = True,
    ) -> Tuple[int, float]:
        """
        Select discrete action using epsilon-greedy policy.

        Args:
            state: Current state (numpy array)
            explore: Whether to use exploration (epsilon-greedy)

        Returns:
            action_idx: Selected action index (0-4)
            position_size: Corresponding position size
        """
        if explore and np.random.rand() < self.epsilon:
            # Exploration: random action
            action_idx = np.random.randint(self.n_actions)
        else:
            # Exploitation: greedy action
            state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)
            with torch.no_grad():
                q_values = self.online_network(state_tensor)
                action_idx = q_values.argmax(dim=1).item()

        position_size = self.ACTION_SIZES[action_idx]
        return action_idx, position_size

    def remember(
        self,
        state: np.ndarray,
        action: int,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ):
        """
        Store transition in experience replay buffer.

        Args:
            state: Current state
            action: Action taken
            reward: Reward received
            next_state: Next state
            done: Whether episode terminated
        """
        # Compute initial priority (1-step TD error estimate)
        state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)
        next_state_tensor = torch.FloatTensor(next_state).unsqueeze(0).to(self.device)

        with torch.no_grad():
            q_current = self.online_network(state_tensor)[0, action]
            q_next_max = self.target_network(next_state_tensor).max(dim=1)[0]
            td_target = reward + (1 - done) * self.gamma * q_next_max
            td_error = torch.abs(q_current - td_target).item()

        priority = td_error
        self.replay_buffer.add(state, action, reward, next_state, done, priority)

    def train_step(self) -> Dict[str, float]:
        """
        Perform one training step on minibatch from experience replay.

        Uses Double DQN (separate networks for action selection and evaluation)
        and Huber loss for stability. Applies importance sampling weights from PER.

        Returns:
            Dictionary with training metrics
        """
        if len(self.replay_buffer.tree.data) < self.batch_size:
            return {}

        # Sample from replay buffer
        transitions, indices, weights = self.replay_buffer.sample(self.batch_size)

        # Unpack transitions
        states = np.array([t[0] for t in transitions])
        actions = np.array([t[1] for t in transitions])
        rewards = np.array([t[2] for t in transitions])
        next_states = np.array([t[3] for t in transitions])
        dones = np.array([t[4] for t in transitions])

        # Convert to tensors
        states_t = torch.FloatTensor(states).to(self.device)
        actions_t = torch.LongTensor(actions).to(self.device)
        rewards_t = torch.FloatTensor(rewards).to(self.device)
        next_states_t = torch.FloatTensor(next_states).to(self.device)
        dones_t = torch.BoolTensor(dones).to(self.device)
        weights_t = torch.FloatTensor(weights).to(self.device)

        # Current Q values (online network)
        q_values = self.online_network(states_t)
        q_current = q_values.gather(1, actions_t.unsqueeze(1)).squeeze(1)

        # Double DQN: use online network to select actions, target network to evaluate
        with torch.no_grad():
            next_q_online = self.online_network(next_states_t)
            next_actions = next_q_online.argmax(dim=1)
            next_q_target = self.target_network(next_states_t)
            next_q_values = next_q_target.gather(1, next_actions.unsqueeze(1)).squeeze(1)

            # Bellman target
            q_target = rewards_t + (1 - dones_t.float()) * self.gamma * next_q_values

        # Huber loss with importance sampling weights
        loss = F.smooth_l1_loss(q_current, q_target, reduction='none')
        weighted_loss = (loss * weights_t).mean()

        # Backward pass
        self.optimizer.zero_grad()
        weighted_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.online_network.parameters(), max_norm=10)
        self.optimizer.step()

        # Update priorities in replay buffer based on new TD errors
        td_errors = (q_current.detach() - q_target).cpu().numpy()
        self.replay_buffer.update_priorities(indices, td_errors)

        # Update target network
        if self._steps % self.target_update_freq == 0:
            self.update_target_network()

        self._steps += 1

        return {
            "loss": weighted_loss.item(),
            "epsilon": self.epsilon,
            "mean_q": q_current.detach().mean().item(),
            "max_priority": self.replay_buffer.max_priority,
        }

    def update_target_network(self):
        """Hard copy online network weights to target network."""
        self.target_network.load_state_dict(self.online_network.state_dict())

    def save(self, path: str):
        """
        Save agent state to file.

        Args:
            path: File path for saving
        """
        checkpoint = {
            "online_network": self.online_network.state_dict(),
            "target_network": self.target_network.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "steps": self._steps,
            "replay_buffer_alpha": self.replay_buffer.alpha,
            "replay_buffer_beta": self.replay_buffer.beta,
            "hyperparams": {
                "state_dim": self.state_dim,
                "n_actions": self.n_actions,
                "hidden_dim": self.hidden_dim,
                "gamma": self.gamma,
                "epsilon_start": self.epsilon_start,
                "epsilon_end": self.epsilon_end,
                "epsilon_decay": self.epsilon_decay,
                "target_update_freq": self.target_update_freq,
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
        self.online_network.load_state_dict(checkpoint["online_network"])
        self.target_network.load_state_dict(checkpoint["target_network"])
        self.optimizer.load_state_dict(checkpoint["optimizer"])
        self._steps = checkpoint["steps"]
        self.replay_buffer.alpha = checkpoint["replay_buffer_alpha"]
        self.replay_buffer.beta = checkpoint["replay_buffer_beta"]
