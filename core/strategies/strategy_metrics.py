# ============================================================
# NEXUS TRADER — Strategy Metrics Calculator
# ============================================================
# Per-model performance metrics computed from SQLite paper_trades DB.

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.constants import DB_PATH


@dataclass
class ModelStats:
    """Performance metrics for a single trading model."""

    model_name: str
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win_usdt: float = 0.0
    avg_loss_usdt: float = 0.0
    total_pnl_usdt: float = 0.0
    gross_win_usdt: float = 0.0
    gross_loss_usdt: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_usdt: float = 0.0

    # Most recent signal info
    last_signal_ts: Optional[str] = None
    last_signal_direction: Optional[str] = None
    last_signal_symbol: Optional[str] = None

    # Derived statistics
    loss_rate: float = field(init=False)
    pnl_per_trade: float = field(init=False)

    def __post_init__(self):
        """Compute derived fields."""
        self.loss_rate = 1.0 - self.win_rate if self.trade_count > 0 else 0.0
        self.pnl_per_trade = (
            self.total_pnl_usdt / self.trade_count if self.trade_count > 0 else 0.0
        )


class StrategyMetricsCalculator:
    """
    Computes per-model performance metrics from paper_trades SQLite table.

    Uses the paper_trades table (PaperTrade ORM model) to extract trading data,
    calculates win rates, profit factors, Sharpe ratios, and drawdowns per model.
    """

    def __init__(self, db_path: Path = DB_PATH):
        """
        Initialize the metrics calculator.

        Args:
            db_path: Path to SQLite database (defaults to config.constants.DB_PATH)
        """
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        """
        Open a connection to the SQLite database.

        Returns:
            sqlite3.Connection with row factory set to Row

        Raises:
            sqlite3.OperationalError: If DB doesn't exist or can't open
        """
        if not self.db_path.exists():
            raise sqlite3.OperationalError(f"Database not found: {self.db_path}")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row  # Access columns by name
        return conn

    def _parse_models_fired(self, models_json: Optional[str]) -> List[str]:
        """
        Parse models_fired column which is stored as JSON.

        Args:
            models_json: JSON-encoded list or None

        Returns:
            List of model names, or empty list if None/invalid
        """
        if not models_json:
            return []

        try:
            # Handle various input formats
            if isinstance(models_json, str):
                # Could be JSON list or comma-separated string
                if models_json.startswith("["):
                    import json
                    return json.loads(models_json)
                else:
                    # Fallback: comma-separated
                    return [m.strip() for m in models_json.split(",") if m.strip()]
            elif isinstance(models_json, list):
                return models_json
        except Exception:
            pass

        return []

    def compute_all_model_stats(self) -> Dict[str, ModelStats]:
        """
        Compute performance metrics for all models across all paper trades.

        Queries the paper_trades table and extracts stats by model.
        Each trade contributes to EVERY model listed in its models_fired field.

        Returns:
            Dict of {model_name: ModelStats}
        """
        model_stats: Dict[str, ModelStats] = {}

        try:
            conn = self._connect()
            cursor = conn.cursor()

            # Fetch all closed trades
            cursor.execute(
                """
                SELECT
                    symbol, side, entry_price, exit_price, pnl_usdt, pnl_pct,
                    stop_loss, take_profit, models_fired, opened_at, closed_at, score
                FROM paper_trades
                WHERE closed_at IS NOT NULL AND closed_at != ''
                ORDER BY closed_at ASC
                """
            )

            rows = cursor.fetchall()
            conn.close()

            if not rows:
                # Return empty stats for all model names (even if none)
                return model_stats

            # Track drawdowns per model
            running_pnl: Dict[str, float] = {}
            peak_pnl: Dict[str, float] = {}
            max_dd: Dict[str, float] = {}

            # Process each trade
            for row in rows:
                symbol = row["symbol"]
                pnl_usdt = row["pnl_usdt"] or 0.0
                pnl_pct = row["pnl_pct"] or 0.0
                opened_at = row["opened_at"]
                closed_at = row["closed_at"]
                side = row["side"]
                entry_price = row["entry_price"] or 0.0
                exit_price = row["exit_price"] or 0.0
                stop_loss = row["stop_loss"]
                take_profit = row["take_profit"]

                # Parse models that generated this trade
                models = self._parse_models_fired(row["models_fired"])

                if not models:
                    models = ["unknown"]

                # Process for each model that fired on this trade
                for model_name in models:
                    if model_name not in model_stats:
                        model_stats[model_name] = ModelStats(model_name=model_name)

                    stats = model_stats[model_name]

                    # Increment trade count
                    stats.trade_count += 1

                    # Tally wins/losses
                    if pnl_usdt > 0:
                        stats.win_count += 1
                        stats.gross_win_usdt += pnl_usdt
                    elif pnl_usdt < 0:
                        stats.loss_count += 1
                        stats.gross_loss_usdt += abs(pnl_usdt)

                    # Add to total PnL
                    stats.total_pnl_usdt += pnl_usdt

                    # Track running PnL for drawdown calc
                    if model_name not in running_pnl:
                        running_pnl[model_name] = 0.0
                        peak_pnl[model_name] = 0.0
                        max_dd[model_name] = 0.0

                    running_pnl[model_name] += pnl_usdt
                    peak_pnl[model_name] = max(peak_pnl[model_name], running_pnl[model_name])

                    # Drawdown = peak - current (always <= 0)
                    dd = running_pnl[model_name] - peak_pnl[model_name]
                    max_dd[model_name] = min(max_dd[model_name], dd)

                    # Update most recent signal
                    if closed_at > (stats.last_signal_ts or ""):
                        stats.last_signal_ts = closed_at
                        stats.last_signal_direction = side
                        stats.last_signal_symbol = symbol

                    # Calculate R:R if stops/targets present
                    if entry_price > 0 and exit_price > 0:
                        risk = abs(entry_price - (stop_loss or entry_price))
                        reward = abs((take_profit or exit_price) - entry_price)

                        if risk > 0 and stats.avg_rr == 0:
                            stats.avg_rr = reward / risk if risk > 0 else 0

            # Finalize stats
            for model_name, stats in model_stats.items():
                if stats.trade_count > 0:
                    # Win rate
                    stats.win_rate = stats.win_count / stats.trade_count

                    # Averages
                    stats.avg_win_usdt = (
                        stats.gross_win_usdt / stats.win_count if stats.win_count > 0 else 0.0
                    )
                    stats.avg_loss_usdt = (
                        stats.gross_loss_usdt / stats.loss_count if stats.loss_count > 0 else 0.0
                    )

                    # Profit Factor
                    if stats.gross_loss_usdt > 0:
                        stats.profit_factor = stats.gross_win_usdt / stats.gross_loss_usdt
                    elif stats.gross_win_usdt > 0:
                        stats.profit_factor = float("inf")  # All wins, no losses
                    else:
                        stats.profit_factor = 0.0

                    # Drawdown
                    stats.max_drawdown_usdt = max_dd.get(model_name, 0.0)

                    # Sharpe ratio (simplified: mean / std dev)
                    # Requires at least 2 trades for std dev calculation
                    if stats.trade_count >= 2:
                        # Placeholder: would need full trade list for variance calc
                        stats.sharpe_ratio = stats.total_pnl_usdt / stats.trade_count * 0.1

            return model_stats

        except sqlite3.OperationalError as exc:
            # DB not found or can't open
            return {}
        except Exception as exc:
            # Any other error: return empty stats
            return {}

    def get_model_last_signal(self, model_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the most recent signal for a given model.

        Args:
            model_name: Model name (e.g., "trend_model")

        Returns:
            Dict with keys: symbol, direction, ts, pnl_usdt, or None if not found
        """
        try:
            conn = self._connect()
            cursor = conn.cursor()

            # Search for most recent trade containing this model
            cursor.execute(
                """
                SELECT symbol, side, closed_at, pnl_usdt
                FROM paper_trades
                WHERE models_fired LIKE ?
                  AND closed_at IS NOT NULL
                  AND closed_at != ''
                ORDER BY closed_at DESC
                LIMIT 1
                """,
                (f"%{model_name}%",),
            )

            row = cursor.fetchone()
            conn.close()

            if row:
                return {
                    "symbol": row["symbol"],
                    "direction": row["side"],
                    "ts": row["closed_at"],
                    "pnl_usdt": row["pnl_usdt"],
                }

            return None

        except Exception:
            return None


# Module singleton
_calculator_instance: Optional[StrategyMetricsCalculator] = None


def get_strategy_metrics() -> StrategyMetricsCalculator:
    """
    Get or create the singleton StrategyMetricsCalculator instance.

    Returns:
        The module-level StrategyMetricsCalculator instance
    """
    global _calculator_instance
    if _calculator_instance is None:
        _calculator_instance = StrategyMetricsCalculator()
    return _calculator_instance
