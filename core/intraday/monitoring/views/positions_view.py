"""
Phase 9: Positions View Builder

Builds PositionView projections from PortfolioState snapshots.
STRICT OBSERVER — reads only, never mutates source.

No Qt imports. No execution engine imports. Pure data transformation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .view_contracts import PositionView

logger = logging.getLogger(__name__)


class PositionsViewBuilder:
    """
    Builds read-only position views from portfolio snapshots.

    Input: list of open position dicts (from PositionRecord.to_dict()).
    Output: list of frozen PositionView objects.

    Pure function — no state, no side effects.
    """

    @staticmethod
    def build_from_records(records: List[Dict[str, Any]]) -> List[PositionView]:
        """
        Convert open position records to PositionView list.

        Args:
            records: List of dicts with position data.

        Returns:
            List of PositionView (frozen, immutable).
            Sorted by opened_at_ms descending (most recent first).
        """
        views = []
        for rec in records:
            try:
                view = PositionsViewBuilder._build_single(rec)
                if view is not None:
                    views.append(view)
            except Exception as e:
                logger.warning(f"PositionsViewBuilder: skipping malformed record: {e}")
                continue

        views.sort(key=lambda v: v.opened_at_ms, reverse=True)
        return views

    @staticmethod
    def _build_single(rec: Dict[str, Any]) -> Optional[PositionView]:
        """Build a single PositionView from a record dict."""
        position_id = rec.get("position_id", "")
        symbol = rec.get("symbol", "")
        if not position_id or not symbol:
            return None

        direction = rec.get("direction", "")
        if not direction:
            return None

        return PositionView(
            position_id=position_id,
            symbol=symbol,
            direction=direction,
            strategy_name=str(rec.get("strategy_name", "")),
            entry_price=float(rec.get("entry_price", 0) or 0),
            current_price=float(rec.get("current_price", 0) or 0),
            quantity=float(rec.get("quantity", 0) or 0),
            entry_size_usdt=float(rec.get("entry_size_usdt", 0) or 0),
            current_size_usdt=float(rec.get("current_size_usdt", 0) or 0),
            unrealized_pnl_usdt=float(rec.get("unrealized_pnl_usdt", 0) or 0),
            unrealized_pnl_pct=float(rec.get("unrealized_pnl_pct", 0) or 0),
            stop_loss=float(rec.get("stop_loss", 0) or 0),
            take_profit=float(rec.get("take_profit", 0) or 0),
            regime_at_entry=str(rec.get("regime_at_entry", "")),
            opened_at_ms=int(rec.get("opened_at_ms", 0) or 0),
            bars_held=int(rec.get("bars_held", 0) or 0),
            auto_partial_applied=bool(rec.get("auto_partial_applied", False)),
            breakeven_applied=bool(rec.get("breakeven_applied", False)),
        )

    @staticmethod
    def compute_exposure(positions: List[PositionView]) -> Dict[str, Any]:
        """
        Compute exposure summary from position views.

        Returns dict with: position_count, total_exposure_usdt,
        total_unrealized_pnl, by_symbol, by_direction.
        """
        if not positions:
            return {
                "position_count": 0,
                "total_exposure_usdt": 0.0,
                "total_unrealized_pnl": 0.0,
                "by_symbol": {},
                "by_direction": {"long": 0.0, "short": 0.0},
            }

        total_exposure = sum(p.current_size_usdt for p in positions)
        total_pnl = sum(p.unrealized_pnl_usdt for p in positions)

        by_symbol: Dict[str, float] = {}
        by_direction: Dict[str, float] = {"long": 0.0, "short": 0.0}

        for p in positions:
            by_symbol[p.symbol] = by_symbol.get(p.symbol, 0) + p.current_size_usdt
            if p.direction in by_direction:
                by_direction[p.direction] += p.current_size_usdt

        return {
            "position_count": len(positions),
            "total_exposure_usdt": total_exposure,
            "total_unrealized_pnl": total_pnl,
            "by_symbol": by_symbol,
            "by_direction": by_direction,
        }
