# ============================================================
# NEXUS TRADER — Risk Gate
#
# Validates OrderCandidates against portfolio risk limits
# before allowing them into the execution pipeline.
#
# Checks (in order):
#  1. Already in position for this symbol?
#  2. Max concurrent open positions reached?
#  3. Portfolio drawdown limit breached?
#  4. Position size within capital allocation limits?
#  5. Spread acceptable? (requires live ticker)
#  6. Risk:reward ratio acceptable?
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Optional
from datetime import datetime

from core.meta_decision.order_candidate import OrderCandidate
from core.portfolio.correlation_controller import get_correlation_controller
from config.settings import settings as _s

logger = logging.getLogger(__name__)

# Defaults — overridable via settings
DEFAULT_MAX_POSITIONS       = 5
DEFAULT_MAX_DRAWDOWN_PCT    = 15.0
DEFAULT_MAX_POSITION_PCT    = 0.25   # max 25% of capital in one position
DEFAULT_MAX_SPREAD_PCT      = 0.30   # 0.30% bid-ask spread max
DEFAULT_MIN_RISK_REWARD     = 1.3    # minimum R:R ratio
DEFAULT_MAX_CAPITAL_USDT    = 0.0    # 0 = unlimited


class RiskGate:
    """
    Stateless risk validator for OrderCandidates.

    Requires caller to supply current portfolio state
    (open positions, available capital, drawdown) so this
    class has no database dependency and is easily testable.
    """

    def __init__(
        self,
        max_concurrent_positions: int   = DEFAULT_MAX_POSITIONS,
        max_portfolio_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
        max_position_capital_pct: float = DEFAULT_MAX_POSITION_PCT,
        max_spread_pct: float           = DEFAULT_MAX_SPREAD_PCT,
        min_risk_reward: float          = DEFAULT_MIN_RISK_REWARD,
        max_capital_usdt: float         = DEFAULT_MAX_CAPITAL_USDT,
    ):
        self.max_concurrent_positions   = max_concurrent_positions
        self.max_portfolio_drawdown_pct = max_portfolio_drawdown_pct
        self.max_position_capital_pct   = max_position_capital_pct
        self.max_spread_pct             = max_spread_pct
        self.min_risk_reward            = min_risk_reward
        self.max_capital_usdt           = max_capital_usdt

    def validate(
        self,
        candidate: OrderCandidate,
        open_positions: list[dict],       # list of {symbol, side, ...}
        available_capital_usdt: float,
        portfolio_drawdown_pct: float,
        spread_pct: Optional[float] = None,
    ) -> OrderCandidate:
        """
        Validate a candidate and set candidate.approved = True/False.
        Sets candidate.rejection_reason on failure.
        Returns the same candidate (mutated in-place for convenience).
        """
        # ── Expiry check ──────────────────────────────────────
        if candidate.expiry and datetime.utcnow() > candidate.expiry:
            candidate.rejection_reason = "Candidate expired before risk check"
            candidate.approved = False
            return candidate

        # ── Already in position (multiple per symbol allowed, up to max) ───────────────────────────────
        # Count positions per symbol; reject only if symbol has reached the max
        symbol_count = sum(1 for p in open_positions if p.get("symbol") == candidate.symbol)
        max_per_symbol = int(_s.get("risk_engine.max_positions_per_symbol", 10))
        if symbol_count >= max_per_symbol:
            candidate.rejection_reason = f"Max positions per symbol reached for {candidate.symbol} ({symbol_count}/{max_per_symbol})"
            candidate.approved = False
            return candidate

        # ── Correlation cap ───────────────────────────────────
        try:
            cc = get_correlation_controller()
            allowed, corr_reason = cc.check_correlation(candidate.symbol, open_positions)
            if not allowed:
                candidate.rejection_reason = corr_reason
                candidate.approved = False
                return candidate

            # Directional exposure check
            allowed, dir_reason = cc.check_directional_exposure(
                candidate.side,
                candidate.position_size_usdt,
                open_positions,
                available_capital_usdt,
            )
            if not allowed:
                candidate.rejection_reason = dir_reason
                candidate.approved = False
                return candidate
        except Exception as _cc_exc:
            logger.debug("RiskGate: correlation check skipped — %s", _cc_exc)

        # ── Max concurrent positions ──────────────────────────
        if len(open_positions) >= self.max_concurrent_positions:
            candidate.rejection_reason = (
                f"Max concurrent positions reached ({self.max_concurrent_positions})"
            )
            candidate.approved = False
            return candidate

        # ── Portfolio drawdown ────────────────────────────────
        if portfolio_drawdown_pct >= self.max_portfolio_drawdown_pct:
            candidate.rejection_reason = (
                f"Portfolio drawdown {portfolio_drawdown_pct:.1f}% ≥ "
                f"limit {self.max_portfolio_drawdown_pct:.1f}%"
            )
            candidate.approved = False
            return candidate

        # ── Capital allocation ────────────────────────────────
        if available_capital_usdt <= 0:
            candidate.rejection_reason = "Insufficient capital"
            candidate.approved = False
            return candidate

        max_size = available_capital_usdt * self.max_position_capital_pct
        if candidate.position_size_usdt > max_size:
            # Reduce size rather than reject outright
            original_size = candidate.position_size_usdt
            candidate.position_size_usdt = round(max_size, 2)
            logger.info(
                "RiskGate: reduced %s size from %.2f to %.2f USDT (capital limit)",
                candidate.symbol, original_size, candidate.position_size_usdt,
            )

        # ── Max capital gate ──────────────────────────────────
        if self.max_capital_usdt > 0 and candidate.position_size_usdt > self.max_capital_usdt:
            original_size = candidate.position_size_usdt
            candidate.position_size_usdt = round(min(original_size, self.max_capital_usdt), 2)
            logger.info(
                "RiskGate: capped %s size from %.2f to %.2f USDT (max_capital_usdt limit)",
                candidate.symbol, original_size, candidate.position_size_usdt,
            )

        # ── Portfolio heat (total stop-fire exposure) ─────────────────
        max_heat_pct = float(_s.get("risk_engine.portfolio_heat_max_pct", 0.06))
        if max_heat_pct > 0 and available_capital_usdt > 0:
            existing_heat = sum(
                (p.get("position_size_usdt", 0) or 0) *
                (p.get("stop_distance_pct", 0.02) or 0.02)
                for p in open_positions
            )
            new_heat = candidate.position_size_usdt * (
                abs(candidate.entry_price - candidate.stop_loss_price) / candidate.entry_price
                if candidate.entry_price and candidate.entry_price > 0 else 0.02
            )
            total_heat_pct = (existing_heat + new_heat) / available_capital_usdt
            if total_heat_pct > max_heat_pct:
                candidate.rejection_reason = (
                    f"Portfolio heat {total_heat_pct:.1%} would exceed limit {max_heat_pct:.1%}"
                )
                candidate.approved = False
                return candidate

        # ── Spread filter ─────────────────────────────────────
        if spread_pct is not None and spread_pct > self.max_spread_pct:
            candidate.rejection_reason = (
                f"Spread {spread_pct:.3f}% > max {self.max_spread_pct:.3f}%"
            )
            candidate.approved = False
            return candidate

        # ── Expected Value gate ───────────────────────────────────────
        ev_enabled = _s.get("expected_value.enabled", True)
        ev_threshold = float(_s.get("expected_value.ev_threshold", 0.05))
        min_rr_floor = float(_s.get("expected_value.min_rr_floor", 1.0))

        # Always apply R:R floor
        if candidate.risk_reward_ratio < min_rr_floor:
            candidate.rejection_reason = (
                f"R:R ratio {candidate.risk_reward_ratio:.2f} < floor {min_rr_floor:.2f}"
            )
            candidate.approved = False
            return candidate

        if ev_enabled:
            # Use confluence score as win probability (sigmoid-calibrated)
            score = candidate.score
            k = float(_s.get("expected_value.sigmoid_steepness", 8.0))
            midpoint = float(_s.get("expected_value.score_midpoint", 0.55))
            win_prob = 1.0 / (1.0 + math.exp(-k * (score - midpoint)))

            # Penalty for uncertain regime or near-crash conditions
            regime = candidate.regime.lower() if candidate.regime else ""
            if regime == "uncertain":
                win_prob *= (1.0 - float(_s.get("expected_value.regime_uncertainty_penalty", 0.15)))

            if candidate.entry_price and candidate.entry_price > 0:
                reward = abs(candidate.take_profit_price - candidate.entry_price)
                risk   = abs(candidate.entry_price - candidate.stop_loss_price)

                # Slippage adjustment: reduce reward and increase risk by estimated
                # round-trip slippage (default 0.05% = 0.0005 as a fraction).
                # This makes the EV gate aware that market fills are not perfect.
                slippage_pct = float(_s.get("backtesting.default_slippage_pct", 0.05)) / 100.0
                slippage_usdt = candidate.entry_price * slippage_pct
                effective_reward = max(0.0, reward - slippage_usdt)
                effective_risk   = risk + slippage_usdt

                ev = win_prob * effective_reward - (1.0 - win_prob) * effective_risk
                ev_normalised = ev / effective_risk if effective_risk > 0 else 0.0

                # Store EV on candidate
                candidate.expected_value = round(ev_normalised, 4)

                if ev_normalised < ev_threshold:
                    candidate.rejection_reason = (
                        f"EV {ev_normalised:.3f} < threshold {ev_threshold:.3f} "
                        f"(win_prob={win_prob:.2f}, R:R={candidate.risk_reward_ratio:.2f}, "
                        f"slippage={slippage_pct*100:.3f}%)"
                    )
                    candidate.approved = False
                    return candidate

        # ── Multi-timeframe confirmation ──────────────────────────────
        require_mtf = _s.get("multi_tf.confirmation_required", False)
        if require_mtf and candidate.higher_tf_regime:
            higher_regime = candidate.higher_tf_regime.lower()
            side = candidate.side.lower()
            mtf_conflict = (
                (side in ("buy", "long") and "bear" in higher_regime) or
                (side in ("sell", "short") and "bull" in higher_regime)
            )
            if mtf_conflict:
                candidate.rejection_reason = (
                    f"MTF conflict: {side} signal vs higher-TF regime '{candidate.higher_tf_regime}'"
                )
                candidate.approved = False
                return candidate

        # ── All checks passed ─────────────────────────────────
        candidate.approved = True
        logger.info(
            "RiskGate: APPROVED %s %s | score=%.2f | size=%.2f USDT | R:R=%.2f",
            candidate.symbol, candidate.side,
            candidate.score, candidate.position_size_usdt, candidate.risk_reward_ratio,
        )
        # Note: validate_batch() persists all candidates; single validate() calls
        # persist here for direct usage outside of batch context.
        self._persist_signal_log(candidate)
        return candidate

    def validate_batch(
        self,
        candidates: list[OrderCandidate],
        open_positions: list[dict],
        available_capital_usdt: float,
        portfolio_drawdown_pct: float,
        spread_map: Optional[dict[str, float]] = None,
    ) -> tuple[list[OrderCandidate], list[OrderCandidate]]:
        """
        Validate a list of candidates. Returns (approved, rejected).
        Candidates are processed in descending score order so higher-quality
        signals consume capacity first.
        """
        spread_map = spread_map or {}
        sorted_candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
        approved:  list[OrderCandidate] = []
        rejected:  list[OrderCandidate] = []

        # Track positions consumed during this batch (simulate allocation)
        simulated_positions = list(open_positions)

        for cand in sorted_candidates:
            spread = spread_map.get(cand.symbol)
            result = self.validate(
                cand,
                simulated_positions,
                available_capital_usdt,
                portfolio_drawdown_pct,
                spread,
            )
            if result.approved:
                approved.append(result)
                # Add to simulated so next candidate sees updated position count
                simulated_positions.append({"symbol": cand.symbol, "side": cand.side})
            else:
                rejected.append(result)

        return approved, rejected

    # ── DB persistence ────────────────────────────────────────

    @staticmethod
    def _persist_signal_log(candidate: OrderCandidate) -> None:
        """
        Write an OrderCandidate to the SignalLog table (best-effort).
        Called after every approve/reject decision so Signal Explorer has data.
        """
        try:
            from datetime import timezone
            from core.database.engine import get_session
            from core.database.models import SignalLog

            direction = "long" if candidate.side in ("buy", "long") else "short"
            row = SignalLog(
                timestamp      = datetime.now(timezone.utc),
                symbol         = candidate.symbol,
                strategy_name  = ", ".join(candidate.models_fired) if candidate.models_fired else "idss",
                direction      = direction,
                strength       = round(candidate.score, 4),
                entry_price    = candidate.entry_price or 0.0,
                stop_loss      = candidate.stop_loss_price,
                take_profit    = candidate.take_profit_price,
                regime         = candidate.regime,
                timeframe      = candidate.timeframe,
                rationale      = candidate.rationale,
                models_fired   = candidate.models_fired,
                approved       = candidate.approved,
                rejection_reason = candidate.rejection_reason,
            )
            with get_session() as session:
                session.add(row)
                session.commit()
        except Exception as exc:
            logger.debug("RiskGate: SignalLog persist failed (non-fatal): %s", exc)
