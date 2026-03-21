"""
Enhanced trade log — appends a JSONL record with rich context for every trade close.
Used to build the training dataset for the Phase 3 probability calibrator.
"""
from __future__ import annotations
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).parent.parent.parent / "data" / "trade_log.jsonl"
_lock = threading.Lock()


def log_trade(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    stop_loss: float,
    take_profit: float,
    size_usdt: float,
    regime: str,
    regime_confidence: float,
    confluence_score: float,
    models_fired: list[str],
    direction: str,
    timeframe: str,
    pnl_pct: float,
    pnl_usdt: float,
    exit_reason: str,
    realized_r: Optional[float],
    rsi_at_entry: Optional[float] = None,
    adx_at_entry: Optional[float] = None,
    atr_ratio: Optional[float] = None,
    funding_rate: Optional[float] = None,
    utc_hour_at_entry: Optional[int] = None,
    opened_at: Optional[str] = None,
    closed_at: Optional[str] = None,
    **extra,
) -> None:
    """Append one trade record to the JSONL trade log."""
    won = pnl_pct > 0
    record = {
        "ts":                   closed_at or datetime.now(timezone.utc).isoformat(),
        "symbol":               symbol,
        "side":                 side,
        "direction":            direction,
        "timeframe":            timeframe,
        "regime":               regime,
        "regime_confidence":    round(regime_confidence, 4) if regime_confidence else None,
        "confluence_score":     round(confluence_score, 4) if confluence_score else None,
        "models_fired":         models_fired,
        "primary_model":        models_fired[0] if models_fired else None,
        "entry_price":          entry_price,
        "exit_price":           exit_price,
        "stop_loss":            stop_loss,
        "take_profit":          take_profit,
        "size_usdt":            size_usdt,
        "pnl_pct":              pnl_pct,
        "pnl_usdt":             pnl_usdt,
        "realized_r":           realized_r,
        "exit_reason":          exit_reason,
        "won":                  won,
        "rsi_at_entry":         rsi_at_entry,
        "adx_at_entry":         adx_at_entry,
        "atr_ratio":            atr_ratio,
        "funding_rate":         funding_rate,
        "utc_hour_at_entry":    utc_hour_at_entry,
        "opened_at":            opened_at,
    }
    record.update(extra)
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("TradeLog: failed to write record: %s", exc)


def read_trades(min_count: int = 0) -> list[dict]:
    """Read all trades from the log. Returns empty list if file missing."""
    if not _LOG_PATH.exists():
        return []
    trades = []
    try:
        with _LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning("TradeLog: failed to read log: %s", exc)
    return trades


def count_trades() -> int:
    return len(read_trades())
