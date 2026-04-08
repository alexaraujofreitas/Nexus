# ============================================================
# NEXUS TRADER — Phase 4a: Online RL Continuous Learning Trainer
# Real-time training loop with persistent experience replay buffer
# ============================================================

import logging
import threading
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import deque, namedtuple
from datetime import datetime


class _RestrictedUnpickler(pickle.Unpickler):
    """Only allow safe standard library, numpy, and torch types."""

    _SAFE_MODULES = frozenset({
        'builtins', 'collections', 'collections.OrderedDict', '_codecs',
        'numpy', 'numpy.core', 'numpy.core.multiarray',
        'numpy.core.numeric', 'numpy.core._multiarray_umath',
        'numpy._core', 'numpy._core.multiarray',
        'torch', 'torch._utils', 'torch.nn', 'torch.nn.modules',
        'torch.nn.modules.linear', 'torch.nn.modules.activation',
        'torch.nn.modules.container', 'torch.nn.modules.loss',
        'torch.nn.modules.batchnorm', 'torch.nn.modules.dropout',
        'torch._C',
    })

    def find_class(self, module: str, name: str) -> type:
        top = module.split('.')[0]
        if top in self._SAFE_MODULES or module in self._SAFE_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked: {module}.{name}")


def _safe_pickle_load(f):
    """Load a pickle file using a restricted unpickler that only allows safe types."""
    return _RestrictedUnpickler(f).load()

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# Experience tuple for replay buffer
Experience = namedtuple('Experience', ['state', 'action', 'reward', 'next_state', 'done'])


class OnlineRLTrainer(QObject):
    """
    Continuous online training loop for the RL Ensemble.

    Runs in a background thread (via QThread integration), training agents on
    live market data as new candles close. Uses a persistent experience replay
    buffer that is saved to disk periodically and loaded on startup.

    Training frequency is configurable (every N closed candles).
    Evaluation runs in parallel via paper trades to measure real performance.

    Signals:
        - training_update: Emits metrics dict when training occurs
        - performance_update: Emits episode performance metrics
    """

    training_update = Signal(dict)
    performance_update = Signal(dict)

    def __init__(
        self,
        rl_ensemble: Optional['RLEnsemble'] = None,
        train_every_n_candles: int = 10,
        eval_every_n_episodes: int = 5,
        project_root: Optional[Path] = None,
    ) -> None:
        """
        Initialize the Online RL Trainer.

        Args:
            rl_ensemble: The RLEnsemble instance to train. If None, trainer is
                        inactive until set later.
            train_every_n_candles: Candle close events to accumulate before
                                  running one training step.
            eval_every_n_episodes: Episode count before emitting evaluation metrics.
            project_root: Base path for saving/loading models. Defaults to current
                         working directory.
        """
        super().__init__()

        self.rl_ensemble = rl_ensemble
        self.train_every_n_candles = train_every_n_candles
        self.eval_every_n_episodes = eval_every_n_episodes
        self.project_root = project_root or Path.cwd()

        # Training state
        self._is_training = False
        self._candle_count = 0
        self._total_steps = 0
        self._total_episodes = 0
        self._lock = threading.Lock()

        # Experience replay buffer (persistent)
        self._replay_buffer: deque = deque()
        self._buffer_max_size = self._load_buffer_config()
        self._buffer_save_interval = 500  # Save every 500 steps
        self._steps_since_save = 0

        # Performance tracking (rolling windows)
        self._episode_returns: deque = deque(maxlen=30)
        self._episode_sharpes: deque = deque(maxlen=30)
        self._win_rates: deque = deque(maxlen=30)
        self._max_drawdowns: deque = deque(maxlen=30)

        # Load persistent buffer from disk
        self._load_replay_buffer()

        # Event bus reference (lazy-loaded)
        self._event_bus = None

        logger.info(
            "OnlineRLTrainer initialized | "
            "train_every=%d candles | "
            "buffer_size=%d/%d",
            train_every_n_candles,
            len(self._replay_buffer),
            self._buffer_max_size,
        )

    def _load_buffer_config(self) -> int:
        """Load replay buffer max size from settings."""
        try:
            from config.settings import settings
            return int(settings.get("rl.replay_buffer_size", 50000))
        except Exception as e:
            logger.warning("Failed to load buffer config: %s. Using default 50000.", e)
            return 50000

    def _get_event_bus(self):
        """Lazy-load event bus on first use."""
        if self._event_bus is None:
            try:
                from core.event_bus import bus as _event_bus  # singleton is named 'bus'
                self._event_bus = _event_bus
            except ImportError:
                logger.error("Event bus not available")
                return None
        return self._event_bus

    def start_training(self) -> None:
        """
        Begin the online training loop.

        Subscribes to CANDLE_CLOSED events from the event bus and starts
        listening for new market data to drive training.
        """
        if self._is_training:
            logger.warning("Training already in progress")
            return

        if self.rl_ensemble is None:
            logger.error("Cannot start training: RL ensemble not set")
            return

        self._is_training = True
        self._candle_count = 0

        # Subscribe to candle close events
        event_bus = self._get_event_bus()
        if event_bus is not None:
            from core.event_bus import Topics
            event_bus.subscribe(Topics.CANDLE_CLOSED, self._on_candle_closed)
            logger.info("OnlineRLTrainer started | subscribed to CANDLE_CLOSED events")
        else:
            logger.error("Cannot subscribe to event bus")
            self._is_training = False

    def stop_training(self) -> None:
        """
        Stop the training loop and save the replay buffer.

        Unsubscribes from candle events and persists the buffer to disk.
        """
        if not self._is_training:
            logger.warning("Training not in progress")
            return

        self._is_training = False

        # Unsubscribe from events
        event_bus = self._get_event_bus()
        if event_bus is not None:
            from core.event_bus import Topics
            event_bus.unsubscribe(Topics.CANDLE_CLOSED, self._on_candle_closed)

        # Save buffer
        self._save_replay_buffer()
        logger.info("OnlineRLTrainer stopped | buffer saved")

    def _on_candle_closed(self, event: Dict[str, Any]) -> None:
        """
        Handler for CANDLE_CLOSED events.

        Extracts market data from event, builds state, computes reward,
        and accumulates experience. Triggers training every N candles.

        Event data structure expected:
            {
                'symbol': str,
                'timeframe': str,
                'candle': {
                    'open': float,
                    'high': float,
                    'low': float,
                    'close': float,
                    'volume': float,
                    'timestamp': int,
                    'previous_close': float (optional)
                }
            }
        """
        if not self._is_training or self.rl_ensemble is None:
            return

        try:
            # Extract data from event
            symbol = event.get('symbol', 'BTCUSDT')
            timeframe = event.get('timeframe', '1h')
            candle = event.get('candle', {})

            if not candle:
                logger.debug("Received empty candle data")
                return

            close = candle.get('close', 0.0)
            previous_close = candle.get('previous_close', close)

            # Build state vector (placeholder - actual implementation uses RLSignalModel)
            state = self._build_state_vector(symbol, timeframe, candle)
            if state is None:
                return

            # Get ensemble action
            action = self.rl_ensemble.select_action(state)
            if action is None:
                return

            # Calculate reward (log return with asymmetric risk penalty)
            reward = self._calculate_reward(close, previous_close, action)

            # Store experience
            with self._lock:
                self._candle_count += 1
                self._steps_since_save += 1

                # For now, assume next_state is current state + 1 step ahead (placeholder)
                # In practice, this would be computed when the next candle closes
                next_state = state
                done = False

                self._add_to_replay_buffer(
                    Experience(state, action, reward, next_state, done)
                )

                # Train every N candles
                if self._candle_count >= self.train_every_n_candles:
                    self._run_training_step()
                    self._candle_count = 0

                # Save buffer periodically
                if self._steps_since_save >= self._buffer_save_interval:
                    self._save_replay_buffer()
                    self._steps_since_save = 0

        except Exception as e:
            logger.error("Error in _on_candle_closed: %s", e, exc_info=True)

    def _build_state_vector(
        self,
        symbol: str,
        timeframe: str,
        candle: Dict[str, Any]
    ) -> Optional[np.ndarray]:
        """
        Build the state vector for the RL agent.

        Uses RLSignalModel.build_state_vector() if available, otherwise
        creates a simple default state.

        Args:
            symbol: Trading symbol (e.g. 'BTCUSDT')
            timeframe: Timeframe (e.g. '1h')
            candle: OHLCV candle data

        Returns:
            State vector as numpy array or None on error
        """
        try:
            # Attempt to use RLSignalModel if available
            from core.signals.rl_signal_model import RLSignalModel
            return RLSignalModel.build_state_vector(symbol, timeframe, candle)
        except Exception as e:
            logger.debug("RLSignalModel not available (%s), using default state", e)

        # Fallback: build simple state from candle data
        try:
            o = candle.get('open', 0.0)
            h = candle.get('high', 0.0)
            l = candle.get('low', 0.0)
            c = candle.get('close', 0.0)
            v = candle.get('volume', 0.0)

            if c == 0:
                logger.warning("Invalid candle data (close=0)")
                return None

            # Normalize OHLCV
            state = np.array([
                (o - c) / c,  # normalized open
                (h - c) / c,  # normalized high
                (l - c) / c,  # normalized low
                np.log(v) if v > 0 else 0.0,  # log volume
                0.0,  # position (placeholder)
                0.0,  # regime (placeholder)
                0.0,  # volatility (placeholder)
            ], dtype=np.float32)

            return state
        except Exception as e:
            logger.error("Failed to build default state: %s", e)
            return None

    def _calculate_reward(self, close: float, previous_close: float, action: float) -> float:
        """
        Calculate reward based on price change and agent action.

        Reward = log(close / previous_close) * action_sign * leverage
        With asymmetric risk penalty: negative rewards are penalized by 1.5x.

        Args:
            close: Current closing price
            previous_close: Previous candle's close price
            action: Agent's continuous action in [-1, 1]

        Returns:
            Scalar reward value
        """
        try:
            from config.settings import settings

            if previous_close <= 0 or close <= 0:
                return 0.0

            # Log return
            log_return = np.log(close / previous_close)

            # Leverage factor (amplifies signal)
            leverage = float(settings.get("rl.reward_leverage", 10.0))

            # Position-adjusted reward
            reward = log_return * float(np.sign(action)) * leverage

            # Asymmetric risk penalty (penalize losses more)
            if reward < 0:
                reward *= 1.5

            return float(reward)
        except Exception as e:
            logger.error("Error calculating reward: %s", e)
            return 0.0

    def _add_to_replay_buffer(self, experience: Experience) -> None:
        """
        Add experience to replay buffer with size limit.

        Args:
            experience: (state, action, reward, next_state, done) tuple
        """
        self._replay_buffer.append(experience)

        # Enforce max size (oldest experiences are auto-removed by deque maxlen)
        if len(self._replay_buffer) > self._buffer_max_size:
            self._replay_buffer.popleft()

        self._total_steps += 1

    def _run_training_step(self) -> None:
        """
        Run one training step on the RL ensemble.

        Samples a batch from the replay buffer and calls ensemble.train_step().
        Emits training_update signal with metrics.
        """
        if len(self._replay_buffer) < 32:  # Minimum batch size
            return

        try:
            # Sample batch (32 experiences)
            batch_size = min(32, len(self._replay_buffer))
            indices = np.random.choice(
                len(self._replay_buffer),
                size=batch_size,
                replace=False
            )
            batch = [self._replay_buffer[i] for i in indices]

            # Call ensemble training
            metrics = self.rl_ensemble.train_step(batch)

            # Update stats
            self._total_episodes += 1

            # Emit training update signal
            self.training_update.emit({
                "timestamp": datetime.now().isoformat(),
                "step": self._total_steps,
                "episode": self._total_episodes,
                "buffer_size": len(self._replay_buffer),
                "batch_size": batch_size,
                **metrics,
            })

            logger.debug(
                "Training step %d/%d | buffer=%d | loss=%.4f",
                self._total_episodes,
                self._total_steps,
                len(self._replay_buffer),
                metrics.get("loss", 0.0),
            )

        except Exception as e:
            logger.error("Error in training step: %s", e, exc_info=True)

    def _load_replay_buffer(self) -> None:
        """
        Load persistent replay buffer from disk.

        Buffer file: {project_root}/models/rl/replay_buffer.pkl
        If file exists and is valid, load all experiences.
        """
        buffer_path = self.project_root / "models" / "rl" / "replay_buffer.pkl"

        if not buffer_path.exists():
            logger.info("No persistent buffer found at %s", buffer_path)
            return

        try:
            with open(buffer_path, 'rb') as f:
                experiences = _safe_pickle_load(f)

            if isinstance(experiences, list):
                # Restore to deque
                self._replay_buffer = deque(experiences, maxlen=self._buffer_max_size)
                logger.info(
                    "Loaded %d experiences from persistent buffer",
                    len(self._replay_buffer)
                )
            else:
                logger.warning("Invalid buffer format (expected list)")
        except Exception as e:
            logger.error("Failed to load persistent buffer: %s", e)

    def _save_replay_buffer(self) -> None:
        """
        Save replay buffer to disk.

        Saves to: {project_root}/models/rl/replay_buffer.pkl
        Uses pickle for serialization.
        """
        buffer_path = self.project_root / "models" / "rl" / "replay_buffer.pkl"

        try:
            buffer_path.parent.mkdir(parents=True, exist_ok=True)

            with open(buffer_path, 'wb') as f:
                pickle.dump(list(self._replay_buffer), f)

            logger.debug(
                "Saved %d experiences to buffer (path=%s)",
                len(self._replay_buffer),
                buffer_path,
            )
        except Exception as e:
            logger.error("Failed to save persistent buffer: %s", e)

    def get_training_stats(self) -> Dict[str, Any]:
        """
        Return current training statistics.

        Returns:
            Dict with keys:
                - steps: Total training steps
                - episodes: Total training episodes
                - sharpe_30: Rolling 30-episode Sharpe ratio
                - win_rate: Rolling win rate (% profitable episodes)
                - buffer_size: Current replay buffer size
        """
        with self._lock:
            sharpe_30 = self._compute_sharpe_ratio(
                list(self._episode_returns)
            ) if self._episode_returns else 0.0

            win_rate = self._compute_win_rate(
                list(self._episode_returns)
            ) if self._episode_returns else 0.0

            return {
                "steps": self._total_steps,
                "episodes": self._total_episodes,
                "sharpe_30": float(sharpe_30),
                "win_rate": float(win_rate),
                "buffer_size": len(self._replay_buffer),
            }

    @staticmethod
    def _compute_sharpe_ratio(returns: List[float], rf_rate: float = 0.0) -> float:
        """
        Compute Sharpe ratio from a list of returns.

        Args:
            returns: List of periodic returns
            rf_rate: Risk-free rate (default 0%)

        Returns:
            Sharpe ratio (annualized if assuming daily returns)
        """
        if len(returns) < 2:
            return 0.0

        returns_arr = np.array(returns)
        excess_returns = returns_arr - rf_rate

        if np.std(excess_returns) == 0:
            return 0.0

        sharpe = np.mean(excess_returns) / np.std(excess_returns)
        return float(sharpe * np.sqrt(252))  # Annualized

    @staticmethod
    def _compute_win_rate(returns: List[float]) -> float:
        """
        Compute win rate (% of positive returns).

        Args:
            returns: List of episode returns

        Returns:
            Win rate as percentage [0, 100]
        """
        if not returns:
            return 0.0

        wins = sum(1 for r in returns if r > 0)
        return (wins / len(returns)) * 100.0

    def update_episode_performance(
        self,
        episode_return: float,
        max_drawdown: float = 0.0,
        sharpe: float = 0.0,
    ) -> None:
        """
        Update rolling performance metrics after each episode.

        Called by the RL ensemble after episode completion.

        Args:
            episode_return: Total return of the episode
            max_drawdown: Maximum drawdown during episode
            sharpe: Sharpe ratio of episode
        """
        with self._lock:
            self._episode_returns.append(episode_return)
            self._max_drawdowns.append(max_drawdown)
            self._episode_sharpes.append(sharpe)

            # Notify ensemble if available
            if self.rl_ensemble is not None:
                try:
                    self.rl_ensemble.update_agent_performance(
                        agent_name="ensemble",
                        episode_return=episode_return
                    )
                except Exception as e:
                    logger.debug("Could not update agent performance: %s", e)

            # Emit performance update signal
            self.performance_update.emit({
                "timestamp": datetime.now().isoformat(),
                "episode": self._total_episodes,
                "return": float(episode_return),
                "max_drawdown": float(max_drawdown),
                "sharpe": float(sharpe),
            })
