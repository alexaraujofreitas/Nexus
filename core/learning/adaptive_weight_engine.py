# ============================================================
# NEXUS TRADER — Adaptive Weight Engine
#
# Single query point that combines Level-1 and Level-2
# adjustments into one final weight multiplier.
#
# Level-1  (TradeOutcomeTracker)  — global per-model win rate
#          adjustment, ±15%.  Already built in confluence_scorer.
#
# Level-2  (Level2PerformanceTracker) — contextual adjustments:
#          • regime × model  ±10%
#          • asset  × model  ±8%
#
# Combined formula:
#   multiplier = L1 × L2_regime × L2_asset
#   Hard-clamped to [MIN_COMBINED, MAX_COMBINED] = [0.70, 1.30]
#
# Design principles:
#   • This module is READ-ONLY for callers — it never mutates
#     tracker state.  Recording trade outcomes is done by
#     PaperExecutor after each close.
#   • Falls back gracefully (returns 1.0) when either tracker
#     has insufficient data for a cell.
#   • Completely transparent — get_detail() exposes every
#     component so dashboards can show the breakdown.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional

from .trade_outcome_store import get_outcome_store          # noqa: F401  (imported for side-effect singleton init)
from .level2_tracker import get_level2_tracker

logger = logging.getLogger(__name__)

# ── Bounds ───────────────────────────────────────────────────────────────────
MIN_COMBINED = 0.70   # absolute floor for any combined multiplier
MAX_COMBINED = 1.30   # absolute ceiling

# ── Lazy import of L1 tracker (avoids circular import) ──────────────────────
def _get_l1_tracker():
    """Return the TradeOutcomeTracker singleton from confluence_scorer."""
    from core.meta_decision.confluence_scorer import get_outcome_tracker
    return get_outcome_tracker()


class AdaptiveWeightEngine:
    """
    Combines Level-1 (global win-rate) and Level-2 (contextual) adjustments.

    Usage in ConfluenceScorer::

        from core.learning import get_adaptive_weight_engine
        engine = get_adaptive_weight_engine()
        multiplier = engine.get_multiplier(model_name, regime, symbol)
        adjusted_weight = base_weight * activation * multiplier

    Thread-safe: relies on the thread-safety of the underlying trackers.
    """

    def get_multiplier(
        self,
        model:  str,
        regime: str,
        asset:  str,
    ) -> float:
        """
        Return the combined L1 × L2 weight multiplier for a (model, regime, asset) triple.

        All three components fall back to 1.0 when data is insufficient.
        Result is hard-clamped to [MIN_COMBINED, MAX_COMBINED].

        Args:
            model:  Sub-model name (e.g. "trend", "mean_reversion")
            regime: Current market regime label (e.g. "bull_trend")
            asset:  Trading pair symbol (e.g. "BTC/USDT")

        Returns:
            float in [0.70, 1.30]
        """
        l1        = self._l1(model)
        l2_regime = self._l2_regime(model, regime)
        l2_asset  = self._l2_asset(model, asset)

        combined = l1 * l2_regime * l2_asset
        clamped  = max(MIN_COMBINED, min(MAX_COMBINED, combined))

        logger.debug(
            "AdaptiveWeightEngine: %s | %s | %s → "
            "L1=%.4f  L2r=%.4f  L2a=%.4f  combined=%.4f  clamped=%.4f",
            model, regime, asset, l1, l2_regime, l2_asset, combined, clamped,
        )
        return round(clamped, 6)

    def get_detail(
        self,
        model:  str,
        regime: str,
        asset:  str,
    ) -> dict:
        """
        Return a breakdown of every component for dashboard display.

        Returns:
            {
                "l1":          float,   # Level-1 adjustment
                "l2_regime":   float,   # Level-2 regime adjustment
                "l2_asset":    float,   # Level-2 asset adjustment
                "combined":    float,   # product before clamping
                "multiplier":  float,   # final clamped value
                "clamped":     bool,    # True if combined was outside bounds
            }
        """
        l1        = self._l1(model)
        l2_regime = self._l2_regime(model, regime)
        l2_asset  = self._l2_asset(model, asset)
        combined  = l1 * l2_regime * l2_asset
        clamped_v = max(MIN_COMBINED, min(MAX_COMBINED, combined))

        return {
            "l1":         round(l1, 6),
            "l2_regime":  round(l2_regime, 6),
            "l2_asset":   round(l2_asset, 6),
            "combined":   round(combined, 6),
            "multiplier": round(clamped_v, 6),
            "clamped":    combined != clamped_v,
        }

    def get_all_model_details(
        self,
        models: list[str],
        regime: str,
        asset:  str,
    ) -> dict[str, dict]:
        """
        Return get_detail() for each model in the list.
        Convenience method for dashboard batch queries.
        """
        return {m: self.get_detail(m, regime, asset) for m in models}

    # ── Private component accessors ───────────────────────────────────────

    def _l1(self, model: str) -> float:
        """Level-1: global win-rate adjustment from TradeOutcomeTracker."""
        try:
            return _get_l1_tracker().get_weight_adjustment(model)
        except Exception:
            return 1.0

    def _l2_regime(self, model: str, regime: str) -> float:
        """Level-2: regime × model adjustment from Level2PerformanceTracker."""
        try:
            return get_level2_tracker().get_regime_adjustment(model, regime)
        except Exception:
            return 1.0

    def _l2_asset(self, model: str, asset: str) -> float:
        """Level-2: asset × model adjustment from Level2PerformanceTracker."""
        try:
            return get_level2_tracker().get_asset_adjustment(model, asset)
        except Exception:
            return 1.0


# ── Module singleton ──────────────────────────────────────────────────────────
_engine = AdaptiveWeightEngine()


def get_adaptive_weight_engine() -> AdaptiveWeightEngine:
    """Return the module-level AdaptiveWeightEngine singleton."""
    return _engine
