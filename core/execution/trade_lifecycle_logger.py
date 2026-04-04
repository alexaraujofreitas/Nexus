"""
NexusTrader — Trade Lifecycle Logger
=====================================
Captures every event in the order lifecycle for testnet validation:
  - Signal generation → candidate creation
  - Pre-trade checks (all gates)
  - Order placement → exchange response
  - Fill confirmation + latency
  - Position management (on_tick, SL/TP adjustments)
  - Partial closes
  - Full closes + PnL
  - Reconnection events + reconciliation
  - Errors and anomalies

All events are written to a JSONL file (data/trade_lifecycle.jsonl) and
can be aggregated into a validation report via generate_report().
"""

import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LOG_PATH = _ROOT / "data" / "trade_lifecycle.jsonl"
_MAX_LINES = 50_000  # rotate after this many lines


class TradeLifecycleLogger:
    """Singleton lifecycle event logger for testnet validation."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, path: Optional[Path] = None):
        if self._initialized:
            return
        self._path = path or _DEFAULT_LOG_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._event_counter = 0

        # Metrics accumulators
        self._trade_count = 0
        self._order_latencies: list[float] = []
        self._errors: list[dict] = []
        self._partial_fills: list[dict] = []
        self._reconnects: list[dict] = []
        self._initialized = True

        logger.info("TradeLifecycleLogger initialized: %s (session=%s)", self._path, self._session_id)

    # ── Core event recording ─────────────────────────────────
    def _record(self, event_type: str, data: dict) -> None:
        """Write a single event to the JSONL log with flush."""
        self._event_counter += 1
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "epoch_ms": int(time.time() * 1000),
            "session": self._session_id,
            "seq": self._event_counter,
            "event": event_type,
            **data,
        }
        try:
            with self._write_lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
                    f.flush()
        except Exception as e:
            logger.warning("TradeLifecycleLogger write failed: %s", e)

    # ── Signal & Candidate Events ─────────────────────────────
    def signal_generated(self, symbol: str, model: str, direction: str,
                         strength: float, regime: str, **extra) -> None:
        self._record("signal_generated", {
            "symbol": symbol, "model": model, "direction": direction,
            "strength": strength, "regime": regime, **extra,
        })

    def candidate_created(self, symbol: str, side: str, score: float,
                          size_usdt: float, sl: float, tp: float, entry: float,
                          models: list, **extra) -> None:
        self._record("candidate_created", {
            "symbol": symbol, "side": side, "score": score,
            "size_usdt": size_usdt, "sl": sl, "tp": tp, "entry": entry,
            "models": models, **extra,
        })

    # ── Pre-Trade Gate Events ─────────────────────────────────
    def pre_trade_check(self, label: str, result: Optional[str],
                        symbol: str = "", **extra) -> None:
        """Record pre-trade gate outcome. result=None means passed."""
        self._record("pre_trade_check", {
            "label": label, "passed": result is None,
            "block_reason": result or "", "symbol": symbol, **extra,
        })

    # ── Order Events ──────────────────────────────────────────
    def order_placed(self, symbol: str, side: str, amount: float,
                     size_usdt: float, **extra) -> None:
        self._record("order_placed", {
            "symbol": symbol, "side": side, "amount": amount,
            "size_usdt": size_usdt, **extra,
        })

    def order_filled(self, symbol: str, side: str, fill_price: float,
                     amount: float, order_id: str, latency_ms: float,
                     **extra) -> None:
        self._trade_count += 1
        self._order_latencies.append(latency_ms)
        self._record("order_filled", {
            "symbol": symbol, "side": side, "fill_price": fill_price,
            "amount": amount, "order_id": order_id,
            "latency_ms": round(latency_ms, 1),
            "trade_number": self._trade_count, **extra,
        })

    def order_failed(self, symbol: str, side: str, error: str, **extra) -> None:
        self._errors.append({"symbol": symbol, "side": side, "error": error,
                             "ts": datetime.now(timezone.utc).isoformat()})
        self._record("order_failed", {
            "symbol": symbol, "side": side, "error": error, **extra,
        })

    def order_timeout(self, symbol: str, side: str, timeout_s: float, **extra) -> None:
        self._errors.append({"symbol": symbol, "side": side, "error": f"timeout_{timeout_s}s"})
        self._record("order_timeout", {
            "symbol": symbol, "side": side, "timeout_s": timeout_s, **extra,
        })

    # ── Position Events ───────────────────────────────────────
    def position_opened(self, symbol: str, side: str, entry_price: float,
                        quantity: float, size_usdt: float, **extra) -> None:
        self._record("position_opened", {
            "symbol": symbol, "side": side, "entry_price": entry_price,
            "quantity": quantity, "size_usdt": size_usdt, **extra,
        })

    def stop_loss_adjusted(self, symbol: str, old_sl: float, new_sl: float,
                           reason: str = "", **extra) -> None:
        self._record("sl_adjusted", {
            "symbol": symbol, "old_sl": old_sl, "new_sl": new_sl,
            "reason": reason, **extra,
        })

    def partial_close(self, symbol: str, reduce_pct: float, close_qty: float,
                      remaining_qty: float, pnl_usdt: float, **extra) -> None:
        self._partial_fills.append({
            "symbol": symbol, "reduce_pct": reduce_pct,
            "pnl_usdt": pnl_usdt,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        self._record("partial_close", {
            "symbol": symbol, "reduce_pct": reduce_pct,
            "close_qty": close_qty, "remaining_qty": remaining_qty,
            "pnl_usdt": round(pnl_usdt, 4), **extra,
        })

    def position_closed(self, symbol: str, side: str, entry_price: float,
                        exit_price: float, pnl_usdt: float, duration_s: float,
                        exit_reason: str, **extra) -> None:
        self._record("position_closed", {
            "symbol": symbol, "side": side, "entry_price": entry_price,
            "exit_price": exit_price, "pnl_usdt": round(pnl_usdt, 4),
            "duration_s": round(duration_s, 1), "exit_reason": exit_reason,
            **extra,
        })

    # ── Exchange Connectivity Events ──────────────────────────
    def exchange_connected(self, mode: str, markets: int, **extra) -> None:
        self._record("exchange_connected", {
            "mode": mode, "markets": markets, **extra,
        })

    def exchange_disconnected(self, reason: str, **extra) -> None:
        self._reconnects.append({
            "event": "disconnected", "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        self._record("exchange_disconnected", {"reason": reason, **extra})

    def exchange_reconnected(self, reconciliation_result: str, **extra) -> None:
        self._reconnects.append({
            "event": "reconnected", "result": reconciliation_result,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        self._record("exchange_reconnected", {
            "reconciliation": reconciliation_result, **extra,
        })

    # ── Reconciliation Events ─────────────────────────────────
    def reconciliation_started(self, stale_count: int, **extra) -> None:
        self._record("reconciliation_started", {
            "stale_positions": stale_count, **extra,
        })

    def reconciliation_result(self, symbol: str, prior_status: str,
                              new_status: str, action: str, **extra) -> None:
        self._record("reconciliation_result", {
            "symbol": symbol, "prior_status": prior_status,
            "new_status": new_status, "action": action, **extra,
        })

    # ── Error / Anomaly Events ────────────────────────────────
    def anomaly(self, category: str, description: str, **extra) -> None:
        self._errors.append({"category": category, "description": description,
                             "ts": datetime.now(timezone.utc).isoformat()})
        self._record("anomaly", {
            "category": category, "description": description, **extra,
        })

    # ── Metrics ───────────────────────────────────────────────
    @property
    def trade_count(self) -> int:
        return self._trade_count

    def get_metrics(self) -> dict:
        """Return accumulated metrics for report generation."""
        latencies = self._order_latencies
        return {
            "session_id": self._session_id,
            "total_events": self._event_counter,
            "trades_filled": self._trade_count,
            "order_latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "order_latency_p50_ms": round(sorted(latencies)[len(latencies) // 2], 1) if latencies else 0,
            "order_latency_p99_ms": round(sorted(latencies)[int(len(latencies) * 0.99)], 1) if latencies else 0,
            "order_latency_max_ms": round(max(latencies), 1) if latencies else 0,
            "errors": len(self._errors),
            "partial_fills": len(self._partial_fills),
            "reconnects": len(self._reconnects),
        }

    # ── Report Generation ─────────────────────────────────────
    def load_events(self, event_type: Optional[str] = None) -> list[dict]:
        """Load all events (or filtered by type) from the JSONL file."""
        events = []
        if not self._path.exists():
            return events
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if event_type is None or entry.get("event") == event_type:
                            events.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning("Failed to load lifecycle events: %s", e)
        return events

    def generate_summary(self) -> dict:
        """Generate a summary dict from all recorded events for the report."""
        events = self.load_events()
        if not events:
            return {"status": "no_events"}

        fills = [e for e in events if e["event"] == "order_filled"]
        closes = [e for e in events if e["event"] == "position_closed"]
        errors = [e for e in events if e["event"] in ("order_failed", "order_timeout", "anomaly")]
        partials = [e for e in events if e["event"] == "partial_close"]
        reconnects = [e for e in events if e["event"] in ("exchange_disconnected", "exchange_reconnected")]

        # Win/loss breakdown
        wins = [c for c in closes if c.get("pnl_usdt", 0) > 0]
        losses = [c for c in closes if c.get("pnl_usdt", 0) <= 0]
        total_pnl = sum(c.get("pnl_usdt", 0) for c in closes)
        gross_profit = sum(c.get("pnl_usdt", 0) for c in wins)
        gross_loss = abs(sum(c.get("pnl_usdt", 0) for c in losses))

        # Latency stats
        latencies = [f.get("latency_ms", 0) for f in fills if f.get("latency_ms")]
        lat_sorted = sorted(latencies) if latencies else [0]

        # Per-symbol breakdown
        symbol_stats = {}
        for c in closes:
            sym = c.get("symbol", "?")
            if sym not in symbol_stats:
                symbol_stats[sym] = {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
            symbol_stats[sym]["count"] += 1
            symbol_stats[sym]["pnl"] += c.get("pnl_usdt", 0)
            if c.get("pnl_usdt", 0) > 0:
                symbol_stats[sym]["wins"] += 1
            else:
                symbol_stats[sym]["losses"] += 1

        # Exit reason breakdown
        exit_reasons = {}
        for c in closes:
            reason = c.get("exit_reason", "unknown")
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        return {
            "session_id": self._session_id,
            "total_events": len(events),
            "orders_filled": len(fills),
            "positions_closed": len(closes),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closes) * 100, 1) if closes else 0,
            "total_pnl_usdt": round(total_pnl, 2),
            "gross_profit_usdt": round(gross_profit, 2),
            "gross_loss_usdt": round(gross_loss, 2),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 999.0,
            "latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "latency_p50_ms": round(lat_sorted[len(lat_sorted) // 2], 1) if latencies else 0,
            "latency_p99_ms": round(lat_sorted[int(len(lat_sorted) * 0.99)], 1) if latencies else 0,
            "latency_max_ms": round(max(latencies), 1) if latencies else 0,
            "errors": len(errors),
            "error_details": errors[:20],  # cap at 20 for report
            "partial_fills": len(partials),
            "reconnects": len(reconnects),
            "reconnect_details": reconnects[:10],
            "symbol_breakdown": symbol_stats,
            "exit_reasons": exit_reasons,
        }


# Singleton accessor
_lifecycle_logger: Optional[TradeLifecycleLogger] = None


def get_lifecycle_logger() -> TradeLifecycleLogger:
    """Get or create the singleton TradeLifecycleLogger."""
    global _lifecycle_logger
    if _lifecycle_logger is None:
        _lifecycle_logger = TradeLifecycleLogger()
    return _lifecycle_logger
