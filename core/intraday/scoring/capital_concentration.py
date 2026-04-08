# ============================================================
# NEXUS TRADER — Capital Concentration Engine  (Phase 5B Wave 1)
#
# Dynamic risk multiplier (0.40x–1.50x) that adjusts position
# sizing based on trade quality, asset score, and execution score.
#
# HARD CONSTRAINT: The multiplied size MUST NOT exceed
# max_capital_pct (4%) hard cap. This is enforced at the
# PositionSizer level, but concentration also caps its output
# to ensure no component can request above-cap sizing.
#
# Integration: ProcessingEngine._apply_concentration() modifies
# the APPROVED ExecutionDecision's sizing fields by applying
# the concentration multiplier.
#
# Pure function: no state, no side effects, fully deterministic.
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConcentrationConfig:
    """Configuration for Capital Concentration Engine."""
    # Multiplier bounds
    min_multiplier: float = 0.40    # Worst-case reduction
    max_multiplier: float = 1.50    # Best-case amplification
    neutral_multiplier: float = 1.0 # Baseline

    # Component weights for multiplier calculation
    w_tqs: float = 0.50            # TQS contributes 50% of multiplier decision
    w_asset_score: float = 0.25    # Asset-level score contributes 25%
    w_execution_score: float = 0.25 # Execution conditions contribute 25%

    # TQS mapping to multiplier range
    tqs_low: float = 0.25          # TQS at or below → min_multiplier
    tqs_high: float = 0.80         # TQS at or above → max_multiplier

    # Hard cap enforcement (matches PositionSizer)
    max_capital_pct: float = 0.04  # 4% — NEVER exceeded


@dataclass(frozen=True)
class ConcentrationResult:
    """Immutable result of concentration evaluation."""
    multiplier: float               # Final multiplier (0.40–1.50)
    tqs_component: float            # TQS contribution (0.0–1.0)
    asset_component: float          # Asset score contribution (0.0–1.0)
    execution_component: float      # Execution score contribution (0.0–1.0)
    adjusted_size_usdt: float       # Post-multiplier size
    adjusted_quantity: float        # Post-multiplier quantity
    capped: bool                    # True if hard cap was applied
    reason: str = ""                # Explanation

    def to_dict(self) -> dict:
        return {
            "concentration_multiplier": round(self.multiplier, 4),
            "tqs_component": round(self.tqs_component, 4),
            "asset_component": round(self.asset_component, 4),
            "execution_component": round(self.execution_component, 4),
            "adjusted_size_usdt": round(self.adjusted_size_usdt, 2),
            "adjusted_quantity": round(self.adjusted_quantity, 8),
            "capped": self.capped,
        }


class CapitalConcentrationEngine:
    """
    Pure-function Capital Concentration Engine.

    Computes a dynamic multiplier (0.40x–1.50x) from trade quality
    and context signals. Adjusts sizing while strictly respecting
    the 4% max_capital_pct hard cap.

    Stateless: safe to call from any thread, fully deterministic.
    """

    def __init__(self, config: ConcentrationConfig = None):
        self.config = config or ConcentrationConfig()
        logger.info(
            "CapitalConcentrationEngine initialized: range=[%.2fx, %.2fx], "
            "max_capital_pct=%.2f%%",
            self.config.min_multiplier,
            self.config.max_multiplier,
            self.config.max_capital_pct * 100,
        )

    def calculate(
        self,
        tqs_score: float,
        asset_score: float,
        execution_score: float,
        base_size_usdt: float,
        base_quantity: float,
        total_capital: float,
        entry_price: float,
    ) -> ConcentrationResult:
        """
        Calculate concentration multiplier and adjusted sizing.

        Parameters
        ----------
        tqs_score : float
            Trade Quality Score (0.0–1.0)
        asset_score : float
            Asset-level quality score (0.0–1.0).
            Phase 5B: derived from execution_context sub-score.
            Wave 2 will provide richer asset-level metrics.
        execution_score : float
            Execution condition score (0.0–1.0).
            From TQS execution_context component.
        base_size_usdt : float
            Base position size from PositionSizer
        base_quantity : float
            Base quantity from PositionSizer
        total_capital : float
            Total account capital (for hard cap)
        entry_price : float
            Entry price (for quantity recalculation)

        Returns
        -------
        ConcentrationResult with multiplier and adjusted sizing
        """
        cfg = self.config

        # ── Map TQS to 0.0–1.0 range within [tqs_low, tqs_high] ──
        tqs_norm = _normalize(tqs_score, cfg.tqs_low, cfg.tqs_high)

        # ── Clamp input scores ───────────────────────────────────
        asset_norm = _clamp(asset_score)
        exec_norm = _clamp(execution_score)

        # ── Weighted composite signal (0.0–1.0) ─────────────────
        composite = (
            cfg.w_tqs * tqs_norm
            + cfg.w_asset_score * asset_norm
            + cfg.w_execution_score * exec_norm
        )
        composite = _clamp(composite)

        # ── Map composite to multiplier range ────────────────────
        multiplier = (
            cfg.min_multiplier
            + composite * (cfg.max_multiplier - cfg.min_multiplier)
        )
        multiplier = _clamp(multiplier, cfg.min_multiplier, cfg.max_multiplier)

        # ── Apply multiplier to sizing ───────────────────────────
        adjusted_size = base_size_usdt * multiplier
        adjusted_qty = base_quantity * multiplier

        # ── Hard cap enforcement ─────────────────────────────────
        hard_cap = cfg.max_capital_pct * total_capital
        capped = False
        if adjusted_size > hard_cap:
            capped = True
            adjusted_size = hard_cap
            if entry_price > 0:
                adjusted_qty = adjusted_size / entry_price

        reason = (
            f"concentration={multiplier:.3f}x "
            f"(tqs={tqs_norm:.2f} asset={asset_norm:.2f} exec={exec_norm:.2f})"
        )
        if capped:
            reason += f" CAPPED at {cfg.max_capital_pct:.0%}"

        logger.debug(
            "Concentration: multiplier=%.3fx size=%.2f→%.2f capped=%s",
            multiplier, base_size_usdt, adjusted_size, capped,
        )

        return ConcentrationResult(
            multiplier=multiplier,
            tqs_component=tqs_norm,
            asset_component=asset_norm,
            execution_component=exec_norm,
            adjusted_size_usdt=adjusted_size,
            adjusted_quantity=adjusted_qty,
            capped=capped,
            reason=reason,
        )


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    """Normalize v from [lo, hi] to [0, 1], clamped."""
    if hi <= lo:
        return 0.5
    return _clamp((v - lo) / (hi - lo))
