# ============================================================
# NEXUS TRADER — Intraday Signal Contracts  (Phase 4)
#
# Typed, immutable schemas for the two-stage signal pipeline:
#   SetupSignal  — Stage A output (structural qualification)
#   TriggerSignal — Stage B output (precise entry condition)
#
# Design invariants:
#   - Both are frozen dataclasses → immutable after creation
#   - Every signal carries full traceability (candle trace IDs,
#     strategy name, regime, lifecycle stage, rejection reason)
#   - A TriggerSignal MUST reference a valid SetupSignal.setup_id
#   - Validation functions enforce schema at pipeline boundaries
#   - ZERO PySide6 imports
# ============================================================
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── Lifecycle Stages ──────────────────────────────────────────

class SetupLifecycle(str, Enum):
    """Lifecycle stages for a setup signal."""
    EVALUATED = "setup_evaluated"
    QUALIFIED = "setup_qualified"
    REJECTED = "setup_rejected"
    EXPIRED = "setup_expired"
    CONSUMED = "setup_consumed"          # A trigger was fired from this setup


class TriggerLifecycle(str, Enum):
    """Lifecycle stages for a trigger signal."""
    EVALUATED = "trigger_evaluated"
    FIRED = "trigger_fired"
    REJECTED = "trigger_rejected"
    EXPIRED = "trigger_expired"          # Signal expiry invalidated
    FORWARDED = "trigger_forwarded"      # Sent to downstream scoring


class StrategyClass(str, Enum):
    """Strategy classification for routing and analytics."""
    MOMENTUM_EXPANSION = "MX"
    VWAP_REVERSION = "VR"
    MICRO_PULLBACK_CONTINUATION = "MPC"
    RANGE_BREAK_RETEST = "RBR"
    LIQUIDITY_SWEEP_REVERSAL = "LSR"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


# ── ID Generation ─────────────────────────────────────────────

def make_setup_id(strategy_name: str, symbol: str, direction: str,
                  setup_candle_ts: int) -> str:
    """
    Deterministic setup ID: SHA-256(strategy|symbol|direction|ts)[:16].
    Same inputs always produce the same ID — supports replay.
    """
    raw = f"{strategy_name}|{symbol}|{direction}|{setup_candle_ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_trigger_id(setup_id: str, trigger_candle_ts: int) -> str:
    """
    Deterministic trigger ID: SHA-256(setup_id|ts)[:16].
    Chained from setup_id for traceability.
    """
    raw = f"{setup_id}|{trigger_candle_ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── SetupSignal ───────────────────────────────────────────────

@dataclass(frozen=True)
class SetupSignal:
    """
    Stage A output: a structural trade setup has been identified.

    Immutable once created. Contains everything needed for audit,
    replay, downstream trigger evaluation, and expiry validation.
    """
    # ── Identity
    setup_id:           str             # Deterministic (make_setup_id)
    strategy_name:      str             # e.g. "momentum_expansion"
    strategy_class:     StrategyClass   # e.g. StrategyClass.MOMENTUM_EXPANSION
    symbol:             str             # e.g. "BTC/USDT"
    direction:          Direction       # LONG or SHORT

    # ── Timeframe context
    setup_timeframe:    str             # TF the setup was evaluated on (e.g. "15m")
    trigger_timeframe:  str             # TF the trigger should be evaluated on (e.g. "1m")

    # ── Prices
    entry_zone_low:     float           # Lower bound of valid entry zone
    entry_zone_high:    float           # Upper bound of valid entry zone
    stop_loss:          float           # Invalidation price
    take_profit:        float           # Target price
    atr_value:          float           # ATR at setup evaluation time

    # ── Regime
    regime:             str             # Regime label at evaluation time
    regime_confidence:  float           # Regime confidence 0.0–1.0

    # ── Traceability
    setup_candle_ts:    int             # Timestamp (ms) of the candle that produced the setup
    candle_trace_ids:   tuple           # Trace IDs of candles used in evaluation (frozen tuple)
    lifecycle:          SetupLifecycle  # Current lifecycle stage

    # ── Rejection / expiry metadata
    rejection_reason:   str = ""        # Non-empty if REJECTED
    rationale:          str = ""        # Human-readable explanation of why setup qualified

    # ── Timing
    created_at_ms:      int = 0         # Wall-clock ms when created (0 = use default)
    max_age_ms:         int = 0         # Per-strategy max age before expiry
    drift_tolerance:    float = 0.0     # Per-strategy max price drift fraction
    base_time_stop_ms:  int = 0         # Per-strategy base time stop (ms)

    def __post_init__(self):
        # Set created_at if not provided
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    @property
    def risk_reward_ratio(self) -> float:
        """Compute R:R from entry zone midpoint."""
        mid = (self.entry_zone_low + self.entry_zone_high) / 2
        if self.direction == Direction.LONG:
            risk = mid - self.stop_loss
            reward = self.take_profit - mid
        else:
            risk = self.stop_loss - mid
            reward = mid - self.take_profit
        return round(reward / risk, 3) if risk > 0 else 0.0

    @property
    def age_ms(self) -> int:
        return int(time.time() * 1000) - self.created_at_ms

    def to_dict(self) -> dict:
        return {
            "setup_id": self.setup_id,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "setup_timeframe": self.setup_timeframe,
            "trigger_timeframe": self.trigger_timeframe,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "atr_value": self.atr_value,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "setup_candle_ts": self.setup_candle_ts,
            "candle_trace_ids": list(self.candle_trace_ids),
            "lifecycle": self.lifecycle.value,
            "rejection_reason": self.rejection_reason,
            "rationale": self.rationale,
            "created_at_ms": self.created_at_ms,
            "max_age_ms": self.max_age_ms,
            "drift_tolerance": self.drift_tolerance,
            "base_time_stop_ms": self.base_time_stop_ms,
            "risk_reward_ratio": self.risk_reward_ratio,
        }


# ── TriggerSignal ─────────────────────────────────────────────

@dataclass(frozen=True)
class TriggerSignal:
    """
    Stage B output: a precise entry trigger has been identified.

    MUST reference a valid, non-expired SetupSignal.
    Immutable once created. Decision-ready: contains all prices
    and metadata needed by downstream scoring and execution.
    """
    # ── Identity
    trigger_id:         str             # Deterministic (make_trigger_id)
    setup_id:           str             # MUST reference a live SetupSignal
    strategy_name:      str
    strategy_class:     StrategyClass
    symbol:             str
    direction:          Direction

    # ── Prices (refined from setup)
    entry_price:        float           # Precise entry price
    stop_loss:          float           # May be tightened from setup SL
    take_profit:        float           # Inherited or refined from setup TP
    atr_value:          float           # ATR at trigger evaluation time

    # ── Strength / confidence
    strength:           float           # 0.0–1.0 trigger confidence
    trigger_quality:    float           # 0.0–1.0 quality score (volume, momentum, etc.)

    # ── Timeframe context
    setup_timeframe:    str
    trigger_timeframe:  str

    # ── Regime
    regime:             str
    regime_confidence:  float

    # ── Traceability
    trigger_candle_ts:  int             # Timestamp (ms) of the candle that produced the trigger
    setup_candle_ts:    int             # Original setup candle ts (for full chain)
    candle_trace_ids:   tuple           # Trace IDs of candles used in trigger evaluation
    setup_trace_ids:    tuple           # Trace IDs from the parent setup (inherited)
    lifecycle:          TriggerLifecycle

    # ── Rejection / expiry metadata
    rejection_reason:   str = ""
    rationale:          str = ""

    # ── Timing
    created_at_ms:      int = 0
    max_age_ms:         int = 0         # Per-strategy max trigger age
    drift_tolerance:    float = 0.0

    def __post_init__(self):
        if self.created_at_ms == 0:
            object.__setattr__(self, "created_at_ms", int(time.time() * 1000))

    @property
    def risk_reward_ratio(self) -> float:
        if self.direction == Direction.LONG:
            risk = self.entry_price - self.stop_loss
            reward = self.take_profit - self.entry_price
        else:
            risk = self.stop_loss - self.entry_price
            reward = self.entry_price - self.take_profit
        return round(reward / risk, 3) if risk > 0 else 0.0

    @property
    def age_ms(self) -> int:
        return int(time.time() * 1000) - self.created_at_ms

    def to_dict(self) -> dict:
        return {
            "trigger_id": self.trigger_id,
            "setup_id": self.setup_id,
            "strategy_name": self.strategy_name,
            "strategy_class": self.strategy_class.value,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "atr_value": self.atr_value,
            "strength": self.strength,
            "trigger_quality": self.trigger_quality,
            "setup_timeframe": self.setup_timeframe,
            "trigger_timeframe": self.trigger_timeframe,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "trigger_candle_ts": self.trigger_candle_ts,
            "setup_candle_ts": self.setup_candle_ts,
            "candle_trace_ids": list(self.candle_trace_ids),
            "setup_trace_ids": list(self.setup_trace_ids),
            "lifecycle": self.lifecycle.value,
            "rejection_reason": self.rejection_reason,
            "rationale": self.rationale,
            "created_at_ms": self.created_at_ms,
            "max_age_ms": self.max_age_ms,
            "drift_tolerance": self.drift_tolerance,
            "risk_reward_ratio": self.risk_reward_ratio,
        }


# ── Validation ────────────────────────────────────────────────

_VALID_TIMEFRAMES = frozenset({"1m", "3m", "5m", "15m", "30m", "1h"})

def validate_setup_signal(setup: SetupSignal) -> list[str]:
    """Validate a SetupSignal. Returns list of violations (empty = valid)."""
    v = []
    if not setup.setup_id:
        v.append("setup_id is empty")
    if not setup.strategy_name:
        v.append("strategy_name is empty")
    if not isinstance(setup.strategy_class, StrategyClass):
        v.append(f"strategy_class invalid: {setup.strategy_class}")
    if not setup.symbol:
        v.append("symbol is empty")
    if not isinstance(setup.direction, Direction):
        v.append(f"direction invalid: {setup.direction}")
    if setup.setup_timeframe not in _VALID_TIMEFRAMES:
        v.append(f"setup_timeframe invalid: {setup.setup_timeframe}")
    if setup.trigger_timeframe not in _VALID_TIMEFRAMES:
        v.append(f"trigger_timeframe invalid: {setup.trigger_timeframe}")
    if setup.entry_zone_low <= 0 or setup.entry_zone_high <= 0:
        v.append("entry_zone prices must be positive")
    if setup.entry_zone_low > setup.entry_zone_high:
        v.append("entry_zone_low > entry_zone_high")
    if setup.stop_loss <= 0:
        v.append("stop_loss must be positive")
    if setup.take_profit <= 0:
        v.append("take_profit must be positive")
    if setup.atr_value <= 0:
        v.append("atr_value must be positive")
    if setup.direction == Direction.LONG:
        if setup.stop_loss >= setup.entry_zone_low:
            v.append("LONG stop_loss must be below entry_zone_low")
        if setup.take_profit <= setup.entry_zone_high:
            v.append("LONG take_profit must be above entry_zone_high")
    elif setup.direction == Direction.SHORT:
        if setup.stop_loss <= setup.entry_zone_high:
            v.append("SHORT stop_loss must be above entry_zone_high")
        if setup.take_profit >= setup.entry_zone_low:
            v.append("SHORT take_profit must be below entry_zone_low")
    if setup.setup_candle_ts <= 0:
        v.append("setup_candle_ts must be positive")
    if not setup.candle_trace_ids:
        v.append("candle_trace_ids is empty")
    if setup.max_age_ms <= 0:
        v.append("max_age_ms must be positive")
    return v


def validate_setup_signal_strict(setup: SetupSignal) -> None:
    """Validate and raise ContractViolation on failure."""
    violations = validate_setup_signal(setup)
    if violations:
        raise ContractViolation(
            f"SetupSignal validation failed: {'; '.join(violations)}"
        )


def validate_trigger_signal(trigger: TriggerSignal) -> list[str]:
    """Validate a TriggerSignal. Returns list of violations (empty = valid)."""
    v = []
    if not trigger.trigger_id:
        v.append("trigger_id is empty")
    if not trigger.setup_id:
        v.append("setup_id is empty — trigger MUST reference a valid setup")
    if not trigger.strategy_name:
        v.append("strategy_name is empty")
    if not isinstance(trigger.strategy_class, StrategyClass):
        v.append(f"strategy_class invalid: {trigger.strategy_class}")
    if not trigger.symbol:
        v.append("symbol is empty")
    if not isinstance(trigger.direction, Direction):
        v.append(f"direction invalid: {trigger.direction}")
    if trigger.entry_price <= 0:
        v.append("entry_price must be positive")
    if trigger.stop_loss <= 0:
        v.append("stop_loss must be positive")
    if trigger.take_profit <= 0:
        v.append("take_profit must be positive")
    if trigger.atr_value <= 0:
        v.append("atr_value must be positive")
    if not (0.0 <= trigger.strength <= 1.0):
        v.append(f"strength must be 0.0–1.0, got {trigger.strength}")
    if not (0.0 <= trigger.trigger_quality <= 1.0):
        v.append(f"trigger_quality must be 0.0–1.0, got {trigger.trigger_quality}")
    if trigger.direction == Direction.LONG:
        if trigger.stop_loss >= trigger.entry_price:
            v.append("LONG stop_loss must be below entry_price")
        if trigger.take_profit <= trigger.entry_price:
            v.append("LONG take_profit must be above entry_price")
    elif trigger.direction == Direction.SHORT:
        if trigger.stop_loss <= trigger.entry_price:
            v.append("SHORT stop_loss must be above entry_price")
        if trigger.take_profit >= trigger.entry_price:
            v.append("SHORT take_profit must be below entry_price")
    if trigger.trigger_candle_ts <= 0:
        v.append("trigger_candle_ts must be positive")
    if not trigger.candle_trace_ids:
        v.append("candle_trace_ids is empty")
    if trigger.max_age_ms <= 0:
        v.append("max_age_ms must be positive")
    return v


def validate_trigger_signal_strict(trigger: TriggerSignal) -> None:
    """Validate and raise ContractViolation on failure."""
    violations = validate_trigger_signal(trigger)
    if violations:
        raise ContractViolation(
            f"TriggerSignal validation failed: {'; '.join(violations)}"
        )


class ContractViolation(Exception):
    """Raised when a signal contract is violated."""
    pass
