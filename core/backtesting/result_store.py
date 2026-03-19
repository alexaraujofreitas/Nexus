# ============================================================
# NEXUS TRADER — Backtest Result Storage & Comparison
#
# Persistence system for saving, loading, and comparing
# backtest results with full configuration snapshots.
# ============================================================

from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import uuid
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

from core.backtesting.kpi_engine import BacktestKPIs, kpis_to_dict, dict_to_kpis
from core.backtesting.data_loader import DataSourceInfo


class _SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles pandas Timestamps, numpy types, and datetimes."""

    def default(self, obj):
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if pd.isna(obj):
            return None
        return super().default(obj)


@dataclass
class BacktestResult:
    """Complete saved backtest result for comparison."""

    # Identity
    result_id: str  # UUID
    timestamp: str  # ISO format when test was run
    name: str  # user-provided or auto-generated

    # Configuration
    strategy_name: str  # "IDSS" or specific model name
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    initial_capital: float
    fee_pct: float
    slippage_pct: float

    # Data source
    data_source: str  # "bybit", "binance", etc.
    data_source_info: dict  # DataSourceInfo as dict

    # Strategy parameters
    model_weights: dict  # current model_weights
    disabled_models: list  # current disabled_models
    regime_affinity: dict  # current regime_affinity
    global_params: dict  # confluence threshold, EV gate, etc.

    # Results
    kpis: dict  # BacktestKPIs as dict
    trades: list  # list of trade dicts
    equity_curve: list  # list of equity values
    equity_timestamps: list  # list of ISO timestamps

    # Metadata
    total_bars: int
    warmup_bars: int
    notes: str = ""


class BacktestResultStore:
    """Manages persistence and comparison of backtest results."""

    def __init__(self, results_dir: Optional[str] = None):
        """
        Initialize result store.

        Args:
            results_dir: Directory to store results (default: data/backtest_results)
        """
        if results_dir is None:
            # Default to project root / data / backtest_results
            from config.constants import ROOT_DIR

            results_dir = ROOT_DIR / "data" / "backtest_results"

        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def save(self, result: BacktestResult) -> str:
        """
        Save a backtest result to disk.

        Args:
            result: BacktestResult object to save

        Returns:
            The result_id of the saved result
        """
        # Generate UUID if not provided
        if not result.result_id or result.result_id == "":
            result.result_id = str(uuid.uuid4())

        # Auto-generate name if not provided
        if not result.name or result.name == "":
            result.name = f"{result.symbol}_{result.timeframe}_{result.timestamp.split('T')[0]}"

        # Convert to dict for JSON serialization
        result_dict = {
            "result_id": result.result_id,
            "timestamp": result.timestamp,
            "name": result.name,
            "strategy_name": result.strategy_name,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "fee_pct": result.fee_pct,
            "slippage_pct": result.slippage_pct,
            "data_source": result.data_source,
            "data_source_info": result.data_source_info,
            "model_weights": result.model_weights,
            "disabled_models": result.disabled_models,
            "regime_affinity": result.regime_affinity,
            "global_params": result.global_params,
            "kpis": result.kpis,
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "equity_timestamps": result.equity_timestamps,
            "total_bars": result.total_bars,
            "warmup_bars": result.warmup_bars,
            "notes": result.notes,
        }

        # Save to JSON file (use safe encoder for Timestamps, numpy types, etc.)
        filepath = self.results_dir / f"{result.result_id}.json"
        with open(filepath, "w") as f:
            json.dump(result_dict, f, indent=2, cls=_SafeJSONEncoder)

        return result.result_id

    def load(self, result_id: str) -> Optional[BacktestResult]:
        """
        Load a backtest result from disk.

        Args:
            result_id: The result_id to load

        Returns:
            BacktestResult object or None if not found
        """
        filepath = self.results_dir / f"{result_id}.json"

        if not filepath.exists():
            return None

        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            result = BacktestResult(
                result_id=data.get("result_id", ""),
                timestamp=data.get("timestamp", ""),
                name=data.get("name", ""),
                strategy_name=data.get("strategy_name", ""),
                symbol=data.get("symbol", ""),
                timeframe=data.get("timeframe", ""),
                start_date=data.get("start_date", ""),
                end_date=data.get("end_date", ""),
                initial_capital=data.get("initial_capital", 0.0),
                fee_pct=data.get("fee_pct", 0.0),
                slippage_pct=data.get("slippage_pct", 0.0),
                data_source=data.get("data_source", ""),
                data_source_info=data.get("data_source_info", {}),
                model_weights=data.get("model_weights", {}),
                disabled_models=data.get("disabled_models", []),
                regime_affinity=data.get("regime_affinity", {}),
                global_params=data.get("global_params", {}),
                kpis=data.get("kpis", {}),
                trades=data.get("trades", []),
                equity_curve=data.get("equity_curve", []),
                equity_timestamps=data.get("equity_timestamps", []),
                total_bars=data.get("total_bars", 0),
                warmup_bars=data.get("warmup_bars", 0),
                notes=data.get("notes", ""),
            )

            return result
        except Exception as e:
            print(f"Error loading backtest result {result_id}: {e}")
            return None

    def list_results(self) -> list[dict]:
        """
        List all saved backtest results.

        Returns:
            List of dicts with: id, name, timestamp, symbol, timeframe,
            total_return_pct, win_rate, profit_factor, max_drawdown_pct
            Sorted by timestamp (descending)
        """
        results = []

        for filepath in sorted(self.results_dir.glob("*.json"), reverse=True):
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)

                kpis = data.get("kpis", {})

                summary = {
                    "result_id": data.get("result_id", ""),
                    "name": data.get("name", ""),
                    "timestamp": data.get("timestamp", ""),
                    "symbol": data.get("symbol", ""),
                    "timeframe": data.get("timeframe", ""),
                    "strategy_name": data.get("strategy_name", ""),
                    "total_return_pct": kpis.get("total_return_pct", 0.0),
                    "win_rate": kpis.get("win_rate", 0.0),
                    "profit_factor": kpis.get("profit_factor", 0.0),
                    "max_drawdown_pct": kpis.get("max_drawdown_pct", 0.0),
                    "total_trades": kpis.get("total_trades", 0),
                    "sharpe_ratio": kpis.get("sharpe_ratio", 0.0),
                }
                results.append(summary)
            except Exception as e:
                print(f"Error reading result file {filepath}: {e}")
                continue

        return results

    def delete(self, result_id: str) -> bool:
        """
        Delete a saved backtest result.

        Args:
            result_id: The result_id to delete

        Returns:
            True if deleted, False if not found
        """
        filepath = self.results_dir / f"{result_id}.json"

        if not filepath.exists():
            return False

        try:
            filepath.unlink()
            return True
        except Exception as e:
            print(f"Error deleting result {result_id}: {e}")
            return False

    def compare(self, result_ids: list[str]) -> pd.DataFrame:
        """
        Load multiple results and create a comparison DataFrame.

        Args:
            result_ids: List of result IDs to compare

        Returns:
            DataFrame with results as rows and KPI metrics as columns
        """
        rows = []

        for result_id in result_ids:
            result = self.load(result_id)
            if result is None:
                continue

            kpis = result.kpis
            row = {
                "name": result.name,
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "total_return_pct": kpis.get("total_return_pct", 0.0),
                "net_profit_usdt": kpis.get("net_profit_usdt", 0.0),
                "total_trades": kpis.get("total_trades", 0),
                "win_rate": kpis.get("win_rate", 0.0),
                "profit_factor": kpis.get("profit_factor", 0.0),
                "expectancy_r": kpis.get("expectancy_r", 0.0),
                "max_drawdown_pct": kpis.get("max_drawdown_pct", 0.0),
                "sharpe_ratio": kpis.get("sharpe_ratio", 0.0),
                "sortino_ratio": kpis.get("sortino_ratio", 0.0),
                "calmar_ratio": kpis.get("calmar_ratio", 0.0),
                "avg_trade_duration_hours": kpis.get("avg_trade_duration_hours", 0.0),
                "exposure_pct": kpis.get("exposure_pct", 0.0),
                "long_win_rate": kpis.get("long_win_rate", 0.0),
                "short_win_rate": kpis.get("short_win_rate", 0.0),
                "long_pnl_usdt": kpis.get("long_pnl_usdt", 0.0),
                "short_pnl_usdt": kpis.get("short_pnl_usdt", 0.0),
            }
            rows.append(row)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        return df

    def export_csv(self, result_id: str, path: str) -> bool:
        """
        Export a backtest result's trades and KPIs to CSV.

        Args:
            result_id: The result_id to export
            path: Output path for CSV file

        Returns:
            True if successful, False otherwise
        """
        result = self.load(result_id)
        if result is None:
            return False

        try:
            # Create DataFrame from trades
            trades_df = pd.DataFrame(result.trades)

            # Add summary row with KPIs
            kpis = result.kpis
            summary_row = {
                "entry_time": "SUMMARY",
                "exit_time": "",
                "entry_price": "",
                "exit_price": "",
                "pnl": kpis.get("net_profit_usdt", 0.0),
                "pnl_pct": kpis.get("total_return_pct", 0.0),
                "side": "",
                "duration_bars": "",
                "regime": f"WR: {kpis.get('win_rate', 0.0):.1%}",
                "models_fired": f"PF: {kpis.get('profit_factor', 0.0):.2f}",
                "score": f"DD: {kpis.get('max_drawdown_pct', 0.0):.1f}%",
            }

            # Append summary row
            summary_df = pd.DataFrame([summary_row])
            result_df = pd.concat([trades_df, summary_df], ignore_index=True)

            # Export to CSV
            result_df.to_csv(path, index=False)
            return True

        except Exception as e:
            print(f"Error exporting result {result_id} to CSV: {e}")
            return False

    def get_stats_summary(self, result_id: str) -> Optional[dict]:
        """
        Get a quick summary of stats for a result.

        Args:
            result_id: The result_id to summarize

        Returns:
            Dict with key stats or None if not found
        """
        result = self.load(result_id)
        if result is None:
            return None

        kpis = result.kpis
        return {
            "name": result.name,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "period": f"{result.start_date} to {result.end_date}",
            "total_return_pct": kpis.get("total_return_pct", 0.0),
            "net_profit_usdt": kpis.get("net_profit_usdt", 0.0),
            "total_trades": kpis.get("total_trades", 0),
            "win_rate": kpis.get("win_rate", 0.0),
            "profit_factor": kpis.get("profit_factor", 0.0),
            "expectancy_r": kpis.get("expectancy_r", 0.0),
            "max_drawdown_pct": kpis.get("max_drawdown_pct", 0.0),
            "sharpe_ratio": kpis.get("sharpe_ratio", 0.0),
            "calmar_ratio": kpis.get("calmar_ratio", 0.0),
        }


# ────────────────────────────────────────────────────────
# Singleton accessor
# ────────────────────────────────────────────────────────

_result_store: Optional[BacktestResultStore] = None


def get_result_store() -> BacktestResultStore:
    """Get or create the global BacktestResultStore singleton."""
    global _result_store
    if _result_store is None:
        _result_store = BacktestResultStore()
    return _result_store
