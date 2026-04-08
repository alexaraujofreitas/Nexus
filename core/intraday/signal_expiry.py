# ============================================================
# NEXUS TRADER — Signal Expiry Validator  (Phase 4)
#
# Boundary control that validates whether a TriggerSignal is
# still actionable. NOT a loose utility — enforced at the
# pipeline boundary before any signal reaches scoring/execution.
#
# Validates:
#   1. Age vs per-strategy max age
#   2. Price drift vs per-strategy tolerance
#   3. R:R still valid after drift adjustment
#   4. Stop-loss not already invalidated by current price
#
# Produces a typed ExpiryResult with reason, publishes expiry
# events, and preserves full traceability.
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.intraday.signal_contracts import Direction, TriggerSignal
from core.intraday.strategy_trace import (
    DecisionStage,
    strategy_trace_registry,
)

logger = logging.getLogger(__name__)


class ExpiryReason(str, Enum):
    """Why a signal was expired."""
    VALID = "valid"
    AGE_EXCEEDED = "age_exceeded"
    PRICE_DRIFT = "price_drift_exceeded"
    RR_INVALIDATED = "rr_invalidated_by_drift"
    SL_BREACHED = "stop_loss_already_breached"
    SETUP_EXPIRED = "parent_setup_expired"


@dataclass(frozen=True)
class ExpiryResult:
    """Result of signal expiry validation."""
    is_valid: bool
    reason: ExpiryReason
    detail: str                 # Human-readable explanation
    trigger_id: str
    age_ms: int                 # Actual age of the trigger signal
    drift_pct: float            # Actual price drift as fraction
    adjusted_rr: float          # R:R after drift adjustment


_MIN_RR_AFTER_DRIFT = 1.0      # Minimum R:R after drift adjustment


def validate_signal_expiry(
    trigger: TriggerSignal,
    current_price: float,
    now_ms: Optional[int] = None,
    min_rr: float = _MIN_RR_AFTER_DRIFT,
) -> ExpiryResult:
    """
    Validate whether a TriggerSignal is still actionable.

    Parameters
    ----------
    trigger : TriggerSignal
        The signal to validate
    current_price : float
        Current market price for the symbol
    now_ms : int, optional
        Current time in ms (defaults to wall clock; pass explicit for replay)
    min_rr : float
        Minimum R:R after drift adjustment (default 1.0)

    Returns
    -------
    ExpiryResult with is_valid flag and detailed reason.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    age = now_ms - trigger.created_at_ms

    # ── Check 1: Age ─────────────────────────────────────────
    if trigger.max_age_ms > 0 and age > trigger.max_age_ms:
        result = ExpiryResult(
            is_valid=False,
            reason=ExpiryReason.AGE_EXCEEDED,
            detail=f"Signal age {age}ms exceeds max {trigger.max_age_ms}ms",
            trigger_id=trigger.trigger_id,
            age_ms=age,
            drift_pct=0.0,
            adjusted_rr=0.0,
        )
        _record_expiry(trigger, result)
        return result

    # ── Check 2: Stop-loss already breached ──────────────────
    if trigger.direction == Direction.LONG:
        if current_price <= trigger.stop_loss:
            result = ExpiryResult(
                is_valid=False,
                reason=ExpiryReason.SL_BREACHED,
                detail=f"Current price {current_price:.4f} <= stop_loss {trigger.stop_loss:.4f}",
                trigger_id=trigger.trigger_id,
                age_ms=age,
                drift_pct=0.0,
                adjusted_rr=0.0,
            )
            _record_expiry(trigger, result)
            return result
    else:  # SHORT
        if current_price >= trigger.stop_loss:
            result = ExpiryResult(
                is_valid=False,
                reason=ExpiryReason.SL_BREACHED,
                detail=f"Current price {current_price:.4f} >= stop_loss {trigger.stop_loss:.4f}",
                trigger_id=trigger.trigger_id,
                age_ms=age,
                drift_pct=0.0,
                adjusted_rr=0.0,
            )
            _record_expiry(trigger, result)
            return result

    # ── Check 3: Price drift ─────────────────────────────────
    drift = abs(current_price - trigger.entry_price) / trigger.entry_price
    if trigger.drift_tolerance > 0 and drift > trigger.drift_tolerance:
        result = ExpiryResult(
            is_valid=False,
            reason=ExpiryReason.PRICE_DRIFT,
            detail=(
                f"Price drift {drift:.4f} ({drift*100:.2f}%) exceeds "
                f"tolerance {trigger.drift_tolerance:.4f} ({trigger.drift_tolerance*100:.2f}%)"
            ),
            trigger_id=trigger.trigger_id,
            age_ms=age,
            drift_pct=drift,
            adjusted_rr=0.0,
        )
        _record_expiry(trigger, result)
        return result

    # ── Check 4: R:R still valid after drift ─────────────────
    # Adjust entry to current price and recompute R:R
    if trigger.direction == Direction.LONG:
        risk = current_price - trigger.stop_loss
        reward = trigger.take_profit - current_price
    else:
        risk = trigger.stop_loss - current_price
        reward = current_price - trigger.take_profit

    adjusted_rr = round(reward / risk, 3) if risk > 0 else 0.0

    if adjusted_rr < min_rr:
        result = ExpiryResult(
            is_valid=False,
            reason=ExpiryReason.RR_INVALIDATED,
            detail=(
                f"Adjusted R:R {adjusted_rr:.3f} below minimum {min_rr:.1f} "
                f"(drift moved entry from {trigger.entry_price:.4f} to {current_price:.4f})"
            ),
            trigger_id=trigger.trigger_id,
            age_ms=age,
            drift_pct=drift,
            adjusted_rr=adjusted_rr,
        )
        _record_expiry(trigger, result)
        return result

    # ── All checks passed ────────────────────────────────────
    return ExpiryResult(
        is_valid=True,
        reason=ExpiryReason.VALID,
        detail="Signal is valid",
        trigger_id=trigger.trigger_id,
        age_ms=age,
        drift_pct=drift,
        adjusted_rr=adjusted_rr,
    )


def _record_expiry(trigger: TriggerSignal, result: ExpiryResult) -> None:
    """Record expiry in the strategy trace registry."""
    trace = strategy_trace_registry.get(trigger.trigger_id)
    if trace:
        trace.record_stage(DecisionStage.SIGNAL_EXPIRED, result.detail)
    logger.info(
        "SIGNAL EXPIRED: %s %s %s | reason=%s | %s",
        trigger.strategy_name, trigger.symbol, trigger.trigger_id,
        result.reason.value, result.detail,
    )
