# ============================================================
# NEXUS TRADER — Base Intraday Strategy  (Phase 4)
#
# Abstract base class for all intraday two-stage strategies.
# Each strategy MUST implement:
#   evaluate_setup(symbol, df_setup, regime_info) → SetupSignal | None
#   evaluate_trigger(symbol, df_trigger, setup, regime_info) → TriggerSignal | None
#
# Each strategy MUST declare:
#   NAME, STRATEGY_CLASS, SETUP_TIMEFRAME, TRIGGER_TIMEFRAME,
#   MAX_SETUP_AGE_MS, MAX_TRIGGER_AGE_MS, DRIFT_TOLERANCE,
#   BASE_TIME_STOP_MS, REGIME_AFFINITY
#
# Design: pure computation, no EventBus, no PySide6, no execution.
# Strategies consume validated candle DataFrames and regime info,
# produce typed signal contracts, nothing else.
# ============================================================
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from core.intraday.signal_contracts import (
    ContractViolation,
    Direction,
    SetupLifecycle,
    SetupSignal,
    StrategyClass,
    TriggerLifecycle,
    TriggerSignal,
    make_setup_id,
    make_trigger_id,
    validate_setup_signal,
    validate_trigger_signal,
)
from core.intraday.strategy_trace import (
    DecisionStage,
    StrategyTrace,
    strategy_trace_registry,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegimeInfo:
    """Regime context passed to strategy evaluation."""
    label: str              # e.g. "bull_trend", "ranging", "high_volatility"
    confidence: float       # 0.0–1.0
    probs: dict             # {regime_name: probability}


class BaseIntradayStrategy(ABC):
    """
    Abstract base for all Phase 4 intraday strategies.

    Subclasses must define class-level constants and implement
    evaluate_setup() and evaluate_trigger().
    """

    # ── Required class constants (MUST override in subclass) ──

    NAME: str = ""                                  # Unique strategy name
    STRATEGY_CLASS: StrategyClass = None             # Strategy classification
    SETUP_TIMEFRAME: str = ""                        # e.g. "15m"
    TRIGGER_TIMEFRAME: str = ""                      # e.g. "1m"
    MAX_SETUP_AGE_MS: int = 0                        # Max setup validity (ms)
    MAX_TRIGGER_AGE_MS: int = 0                      # Max trigger validity (ms)
    DRIFT_TOLERANCE: float = 0.0                     # Max price drift fraction
    BASE_TIME_STOP_MS: int = 0                       # Base time stop (ms)

    # Regime affinity: {regime_label: weight 0.0–1.0}
    # 0.0 = never activate, 1.0 = full activation
    REGIME_AFFINITY: dict = {}

    # ── Helpers ───────────────────────────────────────────────

    def is_active_in_regime(self, regime: str) -> bool:
        """Check if strategy should be active in the given regime."""
        return self.REGIME_AFFINITY.get(regime, 0.0) > 0.0

    def get_regime_weight(self, regime: str) -> float:
        """Get activation weight for the given regime."""
        return self.REGIME_AFFINITY.get(regime, 0.0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        """Extract ATR from the last row, computing if needed."""
        if f"atr_{period}" in df.columns:
            val = df[f"atr_{period}"].iloc[-1]
            if pd.notna(val) and val > 0:
                return float(val)
        # Compute on the fly
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.rolling(period).mean()
        val = atr_series.iloc[-1]
        return float(val) if pd.notna(val) and val > 0 else 0.0

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        """Compute EMA of a series."""
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """Compute RSI of a close series."""
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _col(df: pd.DataFrame, name: str, default=None):
        """Safe column extraction from last row."""
        if name in df.columns:
            val = df[name].iloc[-1]
            if pd.notna(val):
                return val
        return default

    @staticmethod
    def _vwap(df: pd.DataFrame) -> pd.Series:
        """Compute cumulative VWAP."""
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum()
        cum_tp_vol = (typical * df["volume"]).cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    def _candle_trace_ids(self, df: pd.DataFrame, n: int = 5) -> tuple:
        """Extract trace IDs from the last n candles in the DataFrame."""
        ids = []
        for i in range(max(0, len(df) - n), len(df)):
            row = df.iloc[i]
            tid = row.get("trace_id") if hasattr(row, "get") else None
            if tid:
                ids.append(str(tid))
        return tuple(ids) if ids else ("no_trace",)

    # ── Core evaluation interface ─────────────────────────────

    @abstractmethod
    def evaluate_setup(
        self,
        symbol: str,
        df_setup: pd.DataFrame,
        regime_info: RegimeInfo,
    ) -> Optional[SetupSignal]:
        """
        Stage A: Evaluate structural setup conditions.

        Parameters
        ----------
        symbol : str
            Trading pair
        df_setup : pd.DataFrame
            Validated candle data at SETUP_TIMEFRAME
        regime_info : RegimeInfo
            Current regime context

        Returns
        -------
        SetupSignal if qualified, None if rejected.
        """
        ...

    @abstractmethod
    def evaluate_trigger(
        self,
        symbol: str,
        df_trigger: pd.DataFrame,
        setup: SetupSignal,
        regime_info: RegimeInfo,
    ) -> Optional[TriggerSignal]:
        """
        Stage B: Evaluate precise entry trigger within a valid setup.

        Parameters
        ----------
        symbol : str
            Trading pair
        df_trigger : pd.DataFrame
            Validated candle data at TRIGGER_TIMEFRAME
        setup : SetupSignal
            The live setup this trigger derives from
        regime_info : RegimeInfo
            Current regime context

        Returns
        -------
        TriggerSignal if fired, None if rejected.
        """
        ...

    # ── Tracing wrappers ──────────────────────────────────────

    def run_setup(
        self,
        symbol: str,
        df_setup: pd.DataFrame,
        regime_info: RegimeInfo,
    ) -> Optional[SetupSignal]:
        """
        Wrapper around evaluate_setup() that handles tracing,
        validation, and lifecycle management.
        """
        if not self.is_active_in_regime(regime_info.label):
            return None

        if len(df_setup) < 20:
            return None

        # Create trace
        trace = StrategyTrace(
            trace_id="",  # Will be set after evaluation
            trace_type="setup",
            strategy_name=self.NAME,
            symbol=symbol,
            direction="",
            parent_candle_traces=self._candle_trace_ids(df_setup),
        )
        trace.record_stage(DecisionStage.SETUP_EVALUATED)

        try:
            setup = self.evaluate_setup(symbol, df_setup, regime_info)
        except Exception as e:
            logger.error(
                "Strategy %s setup evaluation failed for %s: %s",
                self.NAME, symbol, e, exc_info=True,
            )
            return None

        if setup is None:
            # Rejected — no setup found (not an error, just no signal)
            return None

        # Validate contract
        violations = validate_setup_signal(setup)
        if violations:
            logger.warning(
                "Strategy %s produced invalid SetupSignal for %s: %s",
                self.NAME, symbol, violations,
            )
            return None

        # Record tracing
        trace.trace_id = setup.setup_id
        trace.direction = setup.direction.value
        if setup.lifecycle == SetupLifecycle.QUALIFIED:
            trace.record_stage(DecisionStage.SETUP_QUALIFIED, setup.rationale)
        elif setup.lifecycle == SetupLifecycle.REJECTED:
            trace.record_stage(DecisionStage.SETUP_REJECTED, setup.rejection_reason)

        strategy_trace_registry.register(trace)

        if setup.lifecycle == SetupLifecycle.QUALIFIED:
            logger.info(
                "SETUP QUALIFIED: %s %s %s %s | R:R=%.2f | %s",
                self.NAME, symbol, setup.direction.value,
                setup.setup_timeframe, setup.risk_reward_ratio,
                setup.rationale,
            )
            return setup

        return None

    def run_trigger(
        self,
        symbol: str,
        df_trigger: pd.DataFrame,
        setup: SetupSignal,
        regime_info: RegimeInfo,
    ) -> Optional[TriggerSignal]:
        """
        Wrapper around evaluate_trigger() that handles tracing,
        validation, and lifecycle management.
        """
        if len(df_trigger) < 5:
            return None

        trace = StrategyTrace(
            trace_id="",
            trace_type="trigger",
            strategy_name=self.NAME,
            symbol=symbol,
            direction=setup.direction.value,
            parent_candle_traces=self._candle_trace_ids(df_trigger),
            parent_setup_id=setup.setup_id,
        )
        trace.record_stage(DecisionStage.TRIGGER_EVALUATED)

        try:
            trigger = self.evaluate_trigger(symbol, df_trigger, setup, regime_info)
        except Exception as e:
            logger.error(
                "Strategy %s trigger evaluation failed for %s: %s",
                self.NAME, symbol, e, exc_info=True,
            )
            return None

        if trigger is None:
            return None

        # Validate contract
        violations = validate_trigger_signal(trigger)
        if violations:
            logger.warning(
                "Strategy %s produced invalid TriggerSignal for %s: %s",
                self.NAME, symbol, violations,
            )
            return None

        # Record tracing
        trace.trace_id = trigger.trigger_id
        if trigger.lifecycle == TriggerLifecycle.FIRED:
            trace.record_stage(DecisionStage.TRIGGER_FIRED, trigger.rationale)
        elif trigger.lifecycle == TriggerLifecycle.REJECTED:
            trace.record_stage(DecisionStage.TRIGGER_REJECTED, trigger.rejection_reason)

        strategy_trace_registry.register(trace)

        if trigger.lifecycle == TriggerLifecycle.FIRED:
            logger.info(
                "TRIGGER FIRED: %s %s %s @ %.2f | SL=%.2f TP=%.2f R:R=%.2f | quality=%.2f | %s",
                self.NAME, symbol, trigger.direction.value,
                trigger.entry_price, trigger.stop_loss, trigger.take_profit,
                trigger.risk_reward_ratio, trigger.trigger_quality,
                trigger.rationale,
            )
            return trigger

        return None
