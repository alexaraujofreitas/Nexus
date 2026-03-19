"""
BTC Trading Environment for Reinforcement Learning.

OpenAI Gym-compatible trading environment with realistic market microstructure,
fees, slippage, and risk-adjusted reward functions for Bitcoin trading agents.
"""

import numpy as np
import pandas as pd
from typing import Tuple, Dict, Any, Optional
from collections import deque


class BTCTradingEnv:
    """
    OpenAI Gym-compatible BTC trading environment.

    State space: [normalized OHLCV + technical indicators + position state]
    Action space: continuous [-1, +1] (negative = short, positive = long, near 0 = flat)
    Reward: risk-adjusted returns with realistic cost model

    The environment provides a 50-dimensional observation vector including price
    features, technical indicators, regime encoding, and position state information.
    Agents take continuous actions in [-1, 1] to control position sizing.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        episode_length: int = 500,
        initial_capital: float = 10000.0,
        max_position_pct: float = 0.1,
        fee_taker: float = 0.0004,
    ) -> None:
        """
        Initialize the BTC trading environment.

        Args:
            df: DataFrame with columns ['open', 'high', 'low', 'close', 'volume']
                and technical indicator columns
            episode_length: Number of bars per episode
            initial_capital: Starting portfolio value in USDT
            max_position_pct: Maximum position size as % of capital
            fee_taker: Taker fee rate (0.0004 = 0.04%)
        """
        self.df = df.reset_index(drop=True)
        self.episode_length = episode_length
        self.initial_capital = initial_capital
        self.max_position_pct = max_position_pct
        self.fee_taker = fee_taker
        self.fee_maker = -0.0001  # -0.01% rebate for limit orders

        # Episode state
        self.current_step = 0
        self.start_idx = 0
        self.portfolio_value = initial_capital
        self.cash = initial_capital
        self.position = 0.0  # BTC position
        self.position_entry_price = 0.0
        self.bars_in_position = 0
        self.stop_loss_pct = 0.05  # 5% stop loss
        self.take_profit_pct = 0.10  # 10% take profit

        # Trade tracking
        self.trade_history = []
        self.returns_history = deque(maxlen=500)
        self.pnl_history = []
        self.n_trades = 0

        # Feature normalization state (rolling z-score, 200-bar window)
        self.norm_window = 200
        self.feature_means = {}
        self.feature_stds = {}

        # Regime info (12 regimes for one-hot encoding)
        self.n_regimes = 12
        self.current_regime = 0

        # Step tracking
        self.done = False
        self.truncated = False

    def reset(self) -> np.ndarray:
        """
        Reset the environment and return initial observation.

        Returns:
            Initial observation vector of shape (50,)
        """
        # Randomly sample a start index with enough data for episode
        max_start = len(self.df) - self.episode_length - self.norm_window
        if max_start <= 0:
            max_start = len(self.df) - self.episode_length - 1
        self.start_idx = np.random.randint(0, max(1, max_start))

        self.current_step = 0
        self.portfolio_value = self.initial_capital
        self.cash = self.initial_capital
        self.position = 0.0
        self.position_entry_price = 0.0
        self.bars_in_position = 0
        self.trade_history = []
        self.returns_history.clear()
        self.pnl_history = []
        self.n_trades = 0
        self.done = False
        self.truncated = False

        # Initialize normalization stats from historical data
        self._compute_normalization_stats()

        return self._get_observation()

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        """
        Execute one step in the environment.

        Args:
            action: Continuous action in [-1, 1]
                   > 0.3: go long
                   < -0.3: go short
                   [-0.3, 0.3]: close/stay flat

        Returns:
            observation: Next state (50-dim vector)
            reward: Risk-adjusted reward
            done: Episode finished naturally
            truncated: Episode truncated by step limit
            info: Dictionary with metrics
        """
        action = float(action[0]) if isinstance(action, np.ndarray) else float(action)
        action = np.clip(action, -1.0, 1.0)

        if self.current_step >= self.episode_length:
            self.truncated = True
            self.done = True

        # Get current bar
        idx = self.start_idx + self.current_step
        if idx >= len(self.df):
            self.done = True
            return self._get_observation(), 0.0, self.done, self.truncated, self._get_info()

        current_row = self.df.iloc[idx]
        current_price = float(current_row['close'])

        # Process position changes
        pnl_before = self._compute_pnl(current_price)
        old_position = self.position

        # Execute action
        self._execute_action(action, current_price)

        # Compute PnL
        pnl_after = self._compute_pnl(current_price)
        pnl_change = pnl_after - pnl_before
        pnl_pct = pnl_change / self.portfolio_value if self.portfolio_value > 0 else 0.0

        # Check stop loss / take profit
        sl_hit, tp_hit = self._check_stops(current_price)
        if sl_hit:
            reward = -0.5
            self.position = 0.0
            self.bars_in_position = 0
        elif tp_hit:
            reward = pnl_pct + 0.1
            self.position = 0.0
            self.bars_in_position = 0
        else:
            # Risk-adjusted reward (asymmetric penalty for losses)
            lambda_risk = 1.5
            reward = pnl_pct - lambda_risk * max(0.0, -pnl_pct)

        # Update portfolio
        self.portfolio_value += pnl_change
        self.returns_history.append(pnl_pct)
        self.pnl_history.append(pnl_change)

        # Update position tracking
        if self.position != 0:
            self.bars_in_position += 1
        else:
            self.bars_in_position = 0

        # Terminal reward on episode end
        if self.truncated or self.done:
            if len(self.returns_history) > 1:
                sharpe = self.compute_sharpe(list(self.returns_history))
                reward += sharpe * 0.1

        self.current_step += 1

        return (
            self._get_observation(),
            float(reward),
            self.done,
            self.truncated,
            self._get_info(),
        )

    def _execute_action(self, action: float, current_price: float) -> None:
        """
        Execute trading action with realistic fee/slippage modeling.

        Args:
            action: Continuous action value
            current_price: Current BTC price
        """
        target_position = 0.0

        if action > 0.3:
            # Go long
            target_position = action * self.max_position_pct * (self.portfolio_value / current_price)
        elif action < -0.3:
            # Go short
            target_position = action * self.max_position_pct * (self.portfolio_value / current_price)
        else:
            # Close position
            target_position = 0.0

        position_change = target_position - self.position

        if abs(position_change) > 1e-8:
            # Entry/exit cost
            entry_cost = abs(position_change) * current_price
            fee = entry_cost * self.fee_taker

            # Slippage model: higher fee for large positions
            if entry_cost > 1e6:
                slippage = entry_cost * 0.0002  # 0.02%
            else:
                slippage = entry_cost * 0.0005  # 0.05%

            total_cost = fee + slippage
            self.cash -= total_cost
            self.portfolio_value -= total_cost

            # Funding rate penalty for held positions (0.01% per 8h)
            if self.position != 0 and self.bars_in_position > 0:
                funding_penalty = abs(self.position) * current_price * 0.0001
                self.cash -= funding_penalty
                self.portfolio_value -= funding_penalty

            self.position = target_position
            if self.position != 0:
                self.position_entry_price = current_price

            self.n_trades += 1

    def _check_stops(self, current_price: float) -> Tuple[bool, bool]:
        """
        Check if stop loss or take profit is hit.

        Args:
            current_price: Current BTC price

        Returns:
            Tuple of (stop_loss_hit, take_profit_hit)
        """
        if self.position == 0:
            return False, False

        pnl_pct = (current_price - self.position_entry_price) / self.position_entry_price

        if pnl_pct <= -self.stop_loss_pct:
            return True, False

        if pnl_pct >= self.take_profit_pct:
            return False, True

        return False, False

    def _compute_pnl(self, current_price: float) -> float:
        """
        Compute unrealized P&L.

        Args:
            current_price: Current BTC price

        Returns:
            Unrealized P&L in USDT
        """
        if self.position == 0:
            return 0.0

        return self.position * (current_price - self.position_entry_price)

    def _compute_normalization_stats(self) -> None:
        """Compute z-score normalization statistics for current episode window."""
        start = max(0, self.start_idx - self.norm_window)
        end = self.start_idx + self.episode_length
        window_df = self.df.iloc[start:end]

        # Compute means and stds for key features
        key_features = ['close', 'volume', 'RSI_14', 'MACD_hist', 'ATR_14_pct']
        for feat in key_features:
            if feat in window_df.columns:
                self.feature_means[feat] = window_df[feat].mean()
                std = window_df[feat].std()
                self.feature_stds[feat] = std if std > 0 else 1.0

    def _get_observation(self) -> np.ndarray:
        """
        Construct the 50-dimensional observation vector.

        Returns:
            Observation array of shape (50,)
        """
        idx = self.start_idx + self.current_step
        if idx >= len(self.df):
            return np.zeros(50, dtype=np.float32)

        row = self.df.iloc[idx]
        obs = []

        # Price features (5)
        close = float(row['close'])
        open_price = float(row.get('open', close))
        high = float(row.get('high', close))
        low = float(row.get('low', close))
        volume = float(row.get('volume', 0.0))

        # Normalize price features
        close_norm = (close - self.feature_means.get('close', close)) / (self.feature_stds.get('close', 1.0) + 1e-8)
        obs.append(close_norm)
        obs.append(open_price / close if close > 0 else 1.0)
        obs.append(high / close if close > 0 else 1.0)
        obs.append(low / close if close > 0 else 1.0)

        # Volume ratio (current / 20-bar average)
        vol_20_avg = self.df.iloc[max(0, idx-20):idx]['volume'].mean()
        obs.append(volume / vol_20_avg if vol_20_avg > 0 else 1.0)

        # Technical indicators (20)
        indicator_names = [
            'RSI_14', 'RSI_7', 'MACD_hist', 'BB_upper_pct', 'BB_lower_pct',
            'ATR_14_pct', 'EMA20_pct', 'EMA50_pct', 'SMA200_pct', 'ADX_14',
            'DI_plus', 'DI_minus', 'VWAP_pct', 'OBV_norm', 'CMF_14',
            'ROC_10', 'ROC_20', 'Stoch_K', 'Stoch_D', 'MFI_14'
        ]
        for ind_name in indicator_names:
            if ind_name in row:
                val = float(row[ind_name])
                # Normalize if stats available
                if ind_name in self.feature_stds:
                    val = (val - self.feature_means.get(ind_name, 0.0)) / (self.feature_stds.get(ind_name, 1.0) + 1e-8)
                obs.append(np.clip(val, -5.0, 5.0))  # Clip outliers
            else:
                obs.append(0.0)

        # Regime encoding (12 one-hot)
        regime_one_hot = np.zeros(self.n_regimes)
        regime_one_hot[self.current_regime] = 1.0
        obs.extend(regime_one_hot.tolist())

        # Position state (5)
        current_price = close
        unrealized_pnl_pct = 0.0
        distance_to_stop = 0.0
        distance_to_target = 0.0

        if self.position != 0 and self.position_entry_price > 0:
            unrealized_pnl_pct = (current_price - self.position_entry_price) / self.position_entry_price
            distance_to_stop = abs(unrealized_pnl_pct + self.stop_loss_pct)
            distance_to_target = abs(self.take_profit_pct - unrealized_pnl_pct)

        obs.append(self.position / self.initial_capital if self.initial_capital > 0 else 0.0)
        obs.append(unrealized_pnl_pct)
        obs.append(self.bars_in_position / self.episode_length if self.episode_length > 0 else 0.0)
        obs.append(distance_to_stop)
        obs.append(distance_to_target)

        # Time features (8)
        # Hour encoding (sin/cos)
        hour = (self.current_step % 24) / 24.0
        obs.append(np.sin(2 * np.pi * hour))
        obs.append(np.cos(2 * np.pi * hour))

        # Day encoding (sin/cos)
        day = (self.current_step % 7) / 7.0
        obs.append(np.sin(2 * np.pi * day))
        obs.append(np.cos(2 * np.pi * day))

        # Week in month
        obs.append((self.current_step % 30) / 30.0)

        # Is weekend
        obs.append(1.0 if (self.current_step % 7) >= 5 else 0.0)

        # Session overlap (simplified: 0 to 1)
        obs.append(0.5)

        # Volatility session
        recent_returns = self.returns_history[-20:] if len(self.returns_history) >= 20 else list(self.returns_history)
        volatility = np.std(recent_returns) if recent_returns else 0.0
        obs.append(volatility)

        return np.array(obs, dtype=np.float32)

    def _get_info(self) -> Dict[str, Any]:
        """
        Construct info dictionary with episode metrics.

        Returns:
            Dictionary with performance metrics
        """
        pnl_pct = (self.portfolio_value - self.initial_capital) / self.initial_capital

        returns_list = list(self.returns_history)
        sharpe = self.compute_sharpe(returns_list) if returns_list else 0.0
        max_dd = self._compute_max_drawdown() if self.pnl_history else 0.0

        return {
            "pnl_pct": pnl_pct,
            "portfolio_value": self.portfolio_value,
            "position": self.position,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "n_trades": self.n_trades,
        }

    def _compute_max_drawdown(self) -> float:
        """
        Compute maximum drawdown from portfolio history.

        Returns:
            Maximum drawdown as percentage
        """
        if not self.pnl_history:
            return 0.0

        cumulative = self.initial_capital
        running_max = cumulative
        max_dd = 0.0

        for pnl in self.pnl_history:
            cumulative += pnl
            running_max = max(running_max, cumulative)
            dd = (running_max - cumulative) / running_max if running_max > 0 else 0.0
            max_dd = max(max_dd, dd)

        return max_dd

    @staticmethod
    def compute_sharpe(returns_list: list, risk_free_rate: float = 0.0) -> float:
        """
        Compute Sharpe ratio from returns list.

        Args:
            returns_list: List of returns
            risk_free_rate: Risk-free rate (default 0.0)

        Returns:
            Sharpe ratio
        """
        if not returns_list or len(returns_list) < 2:
            return 0.0

        returns_array = np.array(returns_list)
        mean_return = np.mean(returns_array)
        std_return = np.std(returns_array)

        if std_return == 0:
            return 0.0

        sharpe = (mean_return - risk_free_rate) / std_return
        return float(sharpe)

    def render(self, mode: str = 'human') -> None:
        """
        Render environment state (human-readable output).

        Args:
            mode: Render mode ('human' or 'rgb_array')
        """
        idx = self.start_idx + self.current_step
        if idx < len(self.df):
            row = self.df.iloc[idx]
            print(f"Step: {self.current_step} | Price: {row['close']:.2f} | "
                  f"Position: {self.position:.4f} | PV: {self.portfolio_value:.2f} | "
                  f"Trades: {self.n_trades}")

    def close(self) -> None:
        """Clean up environment resources."""
        pass
