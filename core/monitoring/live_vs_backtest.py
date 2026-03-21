# ============================================================
# NEXUS TRADER — Live vs Backtest Comparison Tracker
#
# Records every closed paper trade and computes rolling metrics
# (WR, PF, avg_R, slippage) broken down by model.
# Compares live demo results against Study 4 backtest baselines.
#
# Study 4 baselines (13-month synthetic backtest, 1,870 trades):
#   TrendModel         : 50.3% WR, PF 1.47, +$13,532
#   MomentumBreakout   : 63.5% WR, PF 4.17, +$10,307
#   MeanReversion      : 32.2% WR, PF 0.21  (DISABLED — pruned)
#   LiquiditySweep     : 19.3% WR, PF 0.28  (DISABLED — pruned)
#   Portfolio          : PF 1.47, +$25,540 (after pruning)
#
# Persists to data/live_vs_backtest.json after every trade.
# Module singleton: get_live_vs_backtest_tracker()
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).parent.parent.parent / "data" / "live_vs_backtest.json"
_WINDOW = 50   # rolling window for metric computation

# ── Study 4 baselines ────────────────────────────────────────────────────────
# Based on 13-month synthetic backtest (March 2024 – March 2025, 1,870 trades).
# These are the TARGETS for Bybit Demo trading.  All values are per-model metrics.
STUDY4_BASELINES: dict[str, dict] = {
    "trend": {
        "model_name":    "TrendModel",
        "trades":        987,
        "win_rate":      0.503,
        "profit_factor": 1.47,
        "avg_r":         0.22,       # estimated from total PnL / trades / avg risk
    },
    "momentum_breakout": {
        "model_name":    "MomentumBreakoutModel",
        "trades":        137,
        "win_rate":      0.635,
        "profit_factor": 4.17,
        "avg_r":         1.21,
    },
    "sentiment": {
        "model_name":    "SentimentModel",
        "trades":        None,       # not individually tracked in Study 4
        "win_rate":      None,
        "profit_factor": None,
        "avg_r":         None,
    },
    "funding_rate": {
        "model_name":    "FundingRateModel",
        "trades":        None,
        "win_rate":      None,
        "profit_factor": None,
        "avg_r":         None,
    },
    "order_book": {
        "model_name":    "OrderBookModel",
        "trades":        None,
        "win_rate":      None,
        "profit_factor": None,
        "avg_r":         None,
    },
    # Portfolio-level baseline (enabled models only)
    "_portfolio": {
        "model_name":    "Portfolio (pruned)",
        "trades":        1124,       # trend + momentum_breakout combined
        "win_rate":      0.514,
        "profit_factor": 1.47,
        "avg_r":         0.31,
    },
}


@dataclass
class ModelMetrics:
    """Rolling metrics for a single model."""
    model:        str
    trades:       int            = 0
    wins:         int            = 0
    total_win_r:  float          = 0.0
    total_loss_r: float          = 0.0   # sum of |realized_r| on losses
    total_pnl:    float          = 0.0
    total_slippage_pct: float    = 0.0
    _r_window:    list           = field(default_factory=list)   # rolling realized_r values

    # ── Derived ────────────────────────────────────────────────
    @property
    def win_rate(self) -> Optional[float]:
        return round(self.wins / self.trades, 4) if self.trades >= 5 else None

    @property
    def profit_factor(self) -> Optional[float]:
        if self.total_loss_r <= 0:
            return None if self.total_win_r <= 0 else 999.0
        return round(self.total_win_r / self.total_loss_r, 3)

    @property
    def avg_r(self) -> Optional[float]:
        r_vals = [v for v in self._r_window if v is not None]
        return round(sum(r_vals) / len(r_vals), 4) if r_vals else None

    @property
    def avg_slippage_pct(self) -> Optional[float]:
        return round(self.total_slippage_pct / self.trades, 4) if self.trades > 0 else None

    def to_dict(self) -> dict:
        return {
            "model":         self.model,
            "trades":        self.trades,
            "wins":          self.wins,
            "win_rate":      self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_r":         self.avg_r,
            "avg_slippage_pct": self.avg_slippage_pct,
            "total_pnl":     round(self.total_pnl, 2),
        }


class LiveVsBacktestTracker:
    """
    Records closed paper trades and maintains per-model rolling metrics.
    On each record() call, computes comparison vs Study 4 baselines.

    Thread-safe.  Persists to data/live_vs_backtest.json.
    """

    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._metrics: dict[str, ModelMetrics] = {}
        self._portfolio = ModelMetrics(model="_portfolio")
        self._trade_count = 0
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def record(self, trade: dict) -> None:
        """
        Record a closed trade dict (from paper_executor._close_position()).
        Updates per-model and portfolio-level rolling metrics.

        Expected keys: symbol, side, models_fired, pnl_usdt, pnl_pct,
                       realized_r, risk_amount_usdt, entry_price, exit_price,
                       stop_loss, take_profit, size_usdt, regime, score,
                       symbol_weight, adjusted_score, expected_rr.
        """
        try:
            self._record_internal(trade)
        except Exception as exc:
            logger.debug("LiveVsBacktestTracker.record error (non-fatal): %s", exc)

    def get_comparison(self) -> dict:
        """
        Return a dict comparing live rolling metrics against Study 4 baselines.
        Keys: portfolio, per_model (dict by model name), baselines, trade_count.
        """
        with self._lock:
            per_model = {k: v.to_dict() for k, v in self._metrics.items()}
            port = self._portfolio.to_dict()
            tc   = self._trade_count

        comparisons = {}
        for model_key, live in per_model.items():
            base = STUDY4_BASELINES.get(model_key)
            if not base:
                comparisons[model_key] = {"live": live, "baseline": None, "delta": None}
                continue
            delta = {}
            if live.get("win_rate") is not None and base.get("win_rate"):
                delta["win_rate"] = round(live["win_rate"] - base["win_rate"], 4)
            if live.get("profit_factor") is not None and base.get("profit_factor"):
                delta["profit_factor"] = round(live["profit_factor"] - base["profit_factor"], 3)
            if live.get("avg_r") is not None and base.get("avg_r"):
                delta["avg_r"] = round(live["avg_r"] - base["avg_r"], 4)
            comparisons[model_key] = {"live": live, "baseline": base, "delta": delta}

        # Portfolio-level comparison
        port_base = STUDY4_BASELINES.get("_portfolio", {})
        port_delta = {}
        if port.get("win_rate") and port_base.get("win_rate"):
            port_delta["win_rate"] = round(port["win_rate"] - port_base["win_rate"], 4)
        if port.get("profit_factor") and port_base.get("profit_factor"):
            port_delta["profit_factor"] = round(port["profit_factor"] - port_base["profit_factor"], 3)
        if port.get("avg_r") and port_base.get("avg_r"):
            port_delta["avg_r"] = round(port["avg_r"] - port_base["avg_r"], 4)

        return {
            "trade_count": tc,
            "portfolio": {
                "live":     port,
                "baseline": port_base,
                "delta":    port_delta,
            },
            "per_model":  comparisons,
            "baselines":  STUDY4_BASELINES,
        }

    def get_portfolio_metrics(self) -> dict:
        """Return just the portfolio-level live metrics dict."""
        with self._lock:
            return self._portfolio.to_dict()

    def get_model_metrics(self, model: str) -> Optional[dict]:
        with self._lock:
            m = self._metrics.get(model)
            return m.to_dict() if m else None

    def all_model_metrics(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._metrics.items()}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _record_internal(self, trade: dict) -> None:
        pnl_usdt   = float(trade.get("pnl_usdt", 0.0) or 0.0)
        pnl_pct    = float(trade.get("pnl_pct",  0.0) or 0.0)
        realized_r = trade.get("realized_r")
        won        = pnl_usdt > 0

        # Slippage = |fill_price - entry_price| / entry_price × 100
        entry_p  = float(trade.get("entry_price", 0.0) or 0.0)
        entry_ex = float(trade.get("entry_expected", entry_p) or entry_p)
        slippage_pct = abs(entry_p - entry_ex) / entry_ex * 100 if entry_ex > 0 else 0.0

        models = list(trade.get("models_fired") or [])
        if not models:
            models = ["unknown"]

        with self._lock:
            for model in models:
                if model not in self._metrics:
                    self._metrics[model] = ModelMetrics(model=model)
                m = self._metrics[model]
                m.trades += 1
                m.total_pnl += pnl_usdt
                m.total_slippage_pct += slippage_pct
                if won:
                    m.wins += 1
                    if realized_r is not None:
                        m.total_win_r += realized_r
                else:
                    if realized_r is not None:
                        m.total_loss_r += abs(realized_r)
                # Rolling R window (capped at _WINDOW per model)
                if realized_r is not None:
                    m._r_window.append(realized_r)
                    if len(m._r_window) > _WINDOW:
                        m._r_window.pop(0)

            # Portfolio level
            p = self._portfolio
            p.trades += 1
            p.total_pnl += pnl_usdt
            p.total_slippage_pct += slippage_pct
            if won:
                p.wins += 1
                if realized_r is not None:
                    p.total_win_r += realized_r
            else:
                if realized_r is not None:
                    p.total_loss_r += abs(realized_r)
            if realized_r is not None:
                p._r_window.append(realized_r)
                if len(p._r_window) > _WINDOW:
                    p._r_window.pop(0)

            self._trade_count += 1

        self._save()

    def _load(self) -> None:
        if not _DATA_FILE.exists():
            return
        try:
            raw = json.loads(_DATA_FILE.read_text())
            self._trade_count = raw.get("trade_count", 0)

            for model_key, md in raw.get("metrics", {}).items():
                mm = ModelMetrics(model=model_key)
                mm.trades             = md.get("trades", 0)
                mm.wins               = md.get("wins", 0)
                mm.total_win_r        = md.get("total_win_r", 0.0)
                mm.total_loss_r       = md.get("total_loss_r", 0.0)
                mm.total_pnl          = md.get("total_pnl", 0.0)
                mm.total_slippage_pct = md.get("total_slippage_pct", 0.0)
                mm._r_window          = md.get("r_window", [])
                self._metrics[model_key] = mm

            pd = raw.get("portfolio", {})
            if pd:
                self._portfolio.trades             = pd.get("trades", 0)
                self._portfolio.wins               = pd.get("wins", 0)
                self._portfolio.total_win_r        = pd.get("total_win_r", 0.0)
                self._portfolio.total_loss_r       = pd.get("total_loss_r", 0.0)
                self._portfolio.total_pnl          = pd.get("total_pnl", 0.0)
                self._portfolio.total_slippage_pct = pd.get("total_slippage_pct", 0.0)
                self._portfolio._r_window          = pd.get("r_window", [])

            logger.debug(
                "LiveVsBacktestTracker: loaded %d trades, %d models",
                self._trade_count, len(self._metrics),
            )
        except Exception as exc:
            logger.warning("LiveVsBacktestTracker: load failed (starting fresh): %s", exc)

    def _save(self) -> None:
        try:
            payload = {
                "trade_count": self._trade_count,
                "saved_at":    datetime.utcnow().isoformat(),
                "metrics": {
                    k: {
                        "trades":             v.trades,
                        "wins":               v.wins,
                        "total_win_r":        v.total_win_r,
                        "total_loss_r":       v.total_loss_r,
                        "total_pnl":          v.total_pnl,
                        "total_slippage_pct": v.total_slippage_pct,
                        "r_window":           v._r_window[-_WINDOW:],
                    }
                    for k, v in self._metrics.items()
                },
                "portfolio": {
                    "trades":             self._portfolio.trades,
                    "wins":               self._portfolio.wins,
                    "total_win_r":        self._portfolio.total_win_r,
                    "total_loss_r":       self._portfolio.total_loss_r,
                    "total_pnl":          self._portfolio.total_pnl,
                    "total_slippage_pct": self._portfolio.total_slippage_pct,
                    "r_window":           self._portfolio._r_window[-_WINDOW:],
                },
            }
            _DATA_FILE.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.debug("LiveVsBacktestTracker: save failed (non-fatal): %s", exc)


# ── Module singleton ─────────────────────────────────────────────────────────
_tracker: Optional[LiveVsBacktestTracker] = None
_tracker_lock = threading.Lock()


def get_live_vs_backtest_tracker() -> LiveVsBacktestTracker:
    """Return the global LiveVsBacktestTracker singleton."""
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = LiveVsBacktestTracker()
    return _tracker
