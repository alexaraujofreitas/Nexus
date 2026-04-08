"""
Phase 9: Trades View Builder

Builds TradeView projections from PositionRecord data.
STRICT OBSERVER — reads only, never mutates source.

No Qt imports. No execution engine imports. Pure data transformation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .view_contracts import TradeView

logger = logging.getLogger(__name__)


class TradesViewBuilder:
    """
    Builds read-only trade views from position records.

    Input: list of closed position dicts (from PositionRecord.to_dict()
           or TradeRecord or raw DB rows).
    Output: list of frozen TradeView objects.

    Pure function — no state, no side effects.
    """

    @staticmethod
    def build_from_records(records: List[Dict[str, Any]]) -> List[TradeView]:
        """
        Convert closed position/trade records to TradeView list.

        Args:
            records: List of dicts with position/trade data.
                     Each dict should have keys matching PositionRecord.to_dict()
                     or TradeRecord fields.

        Returns:
            List of TradeView (frozen, immutable).
            Sorted by closed_at_ms descending (most recent first).
        """
        views = []
        for rec in records:
            try:
                view = TradesViewBuilder._build_single(rec)
                if view is not None:
                    views.append(view)
            except Exception as e:
                logger.warning(f"TradesViewBuilder: skipping malformed record: {e}")
                continue

        # Sort by closed time, most recent first
        views.sort(key=lambda v: v.closed_at_ms, reverse=True)
        return views

    @staticmethod
    def _build_single(rec: Dict[str, Any]) -> Optional[TradeView]:
        """Build a single TradeView from a record dict."""
        # Required fields — skip if missing
        position_id = rec.get("position_id", "")
        symbol = rec.get("symbol", "")
        if not position_id or not symbol:
            return None

        direction = rec.get("direction", "")
        if not direction:
            return None

        opened_at_ms = int(rec.get("opened_at_ms", 0) or 0)
        closed_at_ms = int(rec.get("closed_at_ms", 0) or 0)
        if closed_at_ms == 0:
            return None  # Not a closed trade

        duration_ms = closed_at_ms - opened_at_ms if opened_at_ms > 0 else 0

        return TradeView(
            position_id=position_id,
            symbol=symbol,
            direction=direction,
            strategy_name=str(rec.get("strategy_name", "")),
            entry_price=float(rec.get("entry_price", 0) or 0),
            exit_price=float(rec.get("close_price", 0) or rec.get("exit_price", 0) or 0),
            quantity=float(rec.get("quantity", 0) or 0),
            entry_size_usdt=float(rec.get("entry_size_usdt", 0) or 0),
            realized_pnl_usdt=float(rec.get("realized_pnl_usdt", 0) or 0),
            fee_total_usdt=float(rec.get("fee_total_usdt", 0) or 0),
            r_multiple=float(rec.get("r_multiple", 0) or 0),
            close_reason=str(rec.get("close_reason", "")),
            regime_at_entry=str(rec.get("regime_at_entry", "")),
            opened_at_ms=opened_at_ms,
            closed_at_ms=closed_at_ms,
            duration_ms=duration_ms,
            bars_held=int(rec.get("bars_held", 0) or 0),
            slippage_pct=float(rec.get("slippage_pct", 0) or 0),
        )

    @staticmethod
    def compute_summary(trades: List[TradeView]) -> Dict[str, Any]:
        """
        Compute summary statistics from trade views.

        Returns dict with: trade_count, win_count, loss_count, win_rate,
        total_pnl, avg_pnl, avg_r, avg_duration_ms, by_reason, by_strategy.
        """
        if not trades:
            return {
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_r": 0.0,
                "avg_duration_ms": 0,
                "by_reason": {},
                "by_strategy": {},
            }

        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]
        total_pnl = sum(t.net_pnl_usdt for t in trades)
        avg_r = sum(t.r_multiple for t in trades) / len(trades)
        avg_duration = sum(t.duration_ms for t in trades) / len(trades)

        # By reason
        by_reason: Dict[str, int] = {}
        for t in trades:
            by_reason[t.close_reason] = by_reason.get(t.close_reason, 0) + 1

        # By strategy
        by_strategy: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            if t.strategy_name not in by_strategy:
                by_strategy[t.strategy_name] = {"count": 0, "wins": 0, "pnl": 0.0}
            by_strategy[t.strategy_name]["count"] += 1
            if t.is_winner:
                by_strategy[t.strategy_name]["wins"] += 1
            by_strategy[t.strategy_name]["pnl"] += t.net_pnl_usdt

        return {
            "trade_count": len(trades),
            "win_count": len(winners),
            "loss_count": len(losers),
            "win_rate": len(winners) / len(trades) if trades else 0.0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / len(trades),
            "avg_r": avg_r,
            "avg_duration_ms": int(avg_duration),
            "by_reason": by_reason,
            "by_strategy": by_strategy,
        }
