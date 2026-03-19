# ============================================================
# NEXUS TRADER — Trade Performance Monitor
#
# Tracks real-time trading metrics for the 75-trade checkpoint:
#   1. Score-bucket histogram (win rate by 0.1 score bucket)
#   2. Model-level performance (win rate, avg R, trade count)
#   3. Stop-loss hit rate by model
#   4. Rolling 20-trade expectancy and profit factor
#   5. RL shadow performance tracking
#
# Thread-safe, persisted to data/trade_monitor.json.
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MONITOR_FILE = Path(__file__).parent.parent.parent / "data" / "trade_monitor.json"


class TradeMonitor:
    """Tracks trade metrics for the 75-trade evaluation checkpoint."""

    def __init__(self, path: Path = _MONITOR_FILE):
        self._path = path
        self._lock = threading.Lock()

        # Score bucket histogram: bucket_key -> {"wins": int, "losses": int}
        self._score_buckets: dict[str, dict[str, int]] = {}

        # Model-level performance: model -> {"wins": int, "losses": int, "r_multiples": list[float]}
        self._model_perf: dict[str, dict] = {}

        # Stop-loss hit rate: model -> {"sl_count": int, "tp_count": int, "other_count": int}
        self._exit_by_model: dict[str, dict[str, int]] = {}

        # Rolling trades for expectancy/PF calculation
        self._recent_trades: list[dict] = []  # last 20

        # RL shadow: list of {"regime": str, "rl_action": float, "actual_direction": str, "score": float, "won": bool}
        self._rl_shadow: list[dict] = []

        self._load()

    # ── Recording ──────────────────────────────────────────────────

    def record_trade(
        self,
        score: float,
        models_fired: list[str],
        won: bool,
        exit_reason: str,
        realized_r: Optional[float] = None,
        pnl_usdt: float = 0.0,
        regime: str = "",
        symbol: str = "",
    ) -> None:
        """Record a completed trade for monitoring."""
        with self._lock:
            # 1. Score bucket
            bucket = self._score_to_bucket(score)
            if bucket not in self._score_buckets:
                self._score_buckets[bucket] = {"wins": 0, "losses": 0}
            if won:
                self._score_buckets[bucket]["wins"] += 1
            else:
                self._score_buckets[bucket]["losses"] += 1

            # 2. Model-level performance
            for model in models_fired:
                if model not in self._model_perf:
                    self._model_perf[model] = {"wins": 0, "losses": 0, "r_multiples": []}
                if won:
                    self._model_perf[model]["wins"] += 1
                else:
                    self._model_perf[model]["losses"] += 1
                if realized_r is not None:
                    self._model_perf[model]["r_multiples"].append(round(realized_r, 4))
                    # Keep last 100 R-multiples per model
                    if len(self._model_perf[model]["r_multiples"]) > 100:
                        self._model_perf[model]["r_multiples"] = self._model_perf[model]["r_multiples"][-100:]

            # 3. Exit reason by model
            for model in models_fired:
                if model not in self._exit_by_model:
                    self._exit_by_model[model] = {"sl_count": 0, "tp_count": 0, "other_count": 0}
                if exit_reason == "stop_loss":
                    self._exit_by_model[model]["sl_count"] += 1
                elif exit_reason == "take_profit":
                    self._exit_by_model[model]["tp_count"] += 1
                else:
                    self._exit_by_model[model]["other_count"] += 1

            # 4. Rolling window
            self._recent_trades.append({
                "won": won,
                "realized_r": realized_r,
                "pnl_usdt": pnl_usdt,
            })
            if len(self._recent_trades) > 20:
                self._recent_trades = self._recent_trades[-20:]

        self._save()

    def record_rl_shadow(
        self,
        regime: str,
        rl_action: float,
        rl_confidence: float,
        actual_direction: str,
        score: float,
        won: bool,
    ) -> None:
        """Record what RL would have recommended (shadow mode)."""
        with self._lock:
            self._rl_shadow.append({
                "regime": regime,
                "rl_action": round(rl_action, 4),
                "rl_confidence": round(rl_confidence, 4),
                "actual_direction": actual_direction,
                "score": score,
                "won": won,
            })
            # Keep last 200 shadow entries
            if len(self._rl_shadow) > 200:
                self._rl_shadow = self._rl_shadow[-200:]
        self._save()

    # ── Queries ────────────────────────────────────────────────────

    def get_score_histogram(self) -> dict[str, dict]:
        """Returns score bucket -> {"wins": int, "losses": int, "win_rate": float}"""
        with self._lock:
            result = {}
            for bucket, data in sorted(self._score_buckets.items()):
                total = data["wins"] + data["losses"]
                wr = data["wins"] / total if total > 0 else 0.0
                result[bucket] = {**data, "total": total, "win_rate": round(wr, 4)}
            return result

    def get_model_performance(self) -> dict[str, dict]:
        """Returns model -> {"wins", "losses", "win_rate", "avg_r", "trade_count"}"""
        with self._lock:
            result = {}
            for model, data in self._model_perf.items():
                total = data["wins"] + data["losses"]
                wr = data["wins"] / total if total > 0 else 0.0
                r_list = data["r_multiples"]
                avg_r = sum(r_list) / len(r_list) if r_list else 0.0
                result[model] = {
                    "wins": data["wins"],
                    "losses": data["losses"],
                    "win_rate": round(wr, 4),
                    "avg_r": round(avg_r, 4),
                    "trade_count": total,
                }
            return result

    def get_sl_rates(self) -> dict[str, dict]:
        """Returns model -> {"sl_rate", "tp_rate", "other_rate", "total"}"""
        with self._lock:
            result = {}
            for model, data in self._exit_by_model.items():
                total = data["sl_count"] + data["tp_count"] + data["other_count"]
                if total == 0:
                    continue
                result[model] = {
                    "sl_rate": round(data["sl_count"] / total, 4),
                    "tp_rate": round(data["tp_count"] / total, 4),
                    "other_rate": round(data["other_count"] / total, 4),
                    "total": total,
                }
            return result

    def get_rolling_expectancy(self) -> dict:
        """Returns rolling 20-trade expectancy and profit factor."""
        with self._lock:
            trades = self._recent_trades
            if not trades:
                return {"expectancy_r": 0.0, "profit_factor": 0.0, "trade_count": 0}

            r_values = [t["realized_r"] for t in trades if t.get("realized_r") is not None]
            if not r_values:
                return {"expectancy_r": 0.0, "profit_factor": 0.0, "trade_count": len(trades)}

            wins = [r for r in r_values if r > 0]
            losses = [r for r in r_values if r < 0]

            expectancy = sum(r_values) / len(r_values) if r_values else 0.0
            gross_win = sum(wins) if wins else 0.0
            gross_loss = abs(sum(losses)) if losses else 0.0
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

            return {
                "expectancy_r": round(expectancy, 4),
                "profit_factor": round(pf, 4) if pf != float("inf") else 999.0,
                "trade_count": len(trades),
                "win_count": len(wins),
                "loss_count": len(losses),
            }

    def get_rl_shadow_stats(self) -> dict:
        """Returns RL shadow mode performance statistics."""
        with self._lock:
            if not self._rl_shadow:
                return {"total": 0, "rl_aligned_wins": 0, "rl_aligned_losses": 0}

            aligned_wins = 0
            aligned_losses = 0
            misaligned_wins = 0
            misaligned_losses = 0

            for entry in self._rl_shadow:
                rl_dir = "long" if entry["rl_action"] > 0 else "short"
                actual = entry["actual_direction"]
                aligned = (rl_dir == actual)
                if aligned and entry["won"]:
                    aligned_wins += 1
                elif aligned and not entry["won"]:
                    aligned_losses += 1
                elif not aligned and entry["won"]:
                    misaligned_wins += 1
                else:
                    misaligned_losses += 1

            total_aligned = aligned_wins + aligned_losses
            return {
                "total": len(self._rl_shadow),
                "rl_aligned_total": total_aligned,
                "rl_aligned_win_rate": round(aligned_wins / total_aligned, 4) if total_aligned > 0 else 0.0,
                "rl_aligned_wins": aligned_wins,
                "rl_aligned_losses": aligned_losses,
                "rl_misaligned_wins": misaligned_wins,
                "rl_misaligned_losses": misaligned_losses,
            }

    def get_total_trades(self) -> int:
        """Total trades recorded across all buckets."""
        with self._lock:
            return sum(d["wins"] + d["losses"] for d in self._score_buckets.values())

    # ── Persistence ────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "score_buckets": self._score_buckets,
                "model_perf": self._model_perf,
                "exit_by_model": self._exit_by_model,
                "recent_trades": self._recent_trades,
                "rl_shadow": self._rl_shadow,
            }
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.debug("TradeMonitor: save failed (non-fatal): %s", exc)

    def _load(self) -> None:
        try:
            if self._path.exists():
                with open(self._path, "r") as f:
                    data = json.load(f)
                self._score_buckets = data.get("score_buckets", {})
                self._model_perf = data.get("model_perf", {})
                self._exit_by_model = data.get("exit_by_model", {})
                self._recent_trades = data.get("recent_trades", [])
                self._rl_shadow = data.get("rl_shadow", [])
                logger.debug("TradeMonitor: loaded %d trade records from disk", self.get_total_trades())
        except Exception as exc:
            logger.debug("TradeMonitor: load failed (starting fresh): %s", exc)

    @staticmethod
    def _score_to_bucket(score: float) -> str:
        """Convert score to bucket key like '0.40-0.50'."""
        low = int(score * 10) / 10.0
        high = low + 0.1
        if high > 1.0:
            high = 1.0
        return f"{low:.2f}-{high:.2f}"


# ── Module singleton ──────────────────────────────────────────────
_monitor: Optional[TradeMonitor] = None


def get_trade_monitor() -> TradeMonitor:
    global _monitor
    if _monitor is None:
        _monitor = TradeMonitor()
    return _monitor
