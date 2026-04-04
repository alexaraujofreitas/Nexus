# core/monitoring/coverage_guarantee.py
"""
CoverageGuarantee — detects prolonged inactivity and triggers graduated fallback.

Integration: called in scanner._on_scan_complete() after regime diagnostics.

Config gate: coverage_guarantee.enabled (default False)

Safety constraints (from approved architecture):
- Fallback trades capped at 0.3-0.5x normal position size
- Maximum 2 fallback trades per 6-hour window (prevents unlimited enrichment-only trading)
- Auto-retracts when primary model fires (idle_cycles resets to 0)
- Never activates in crisis, liquidation_cascade, or vol_compression
"""
from __future__ import annotations
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Regimes where fallback is NEVER allowed
_EXCLUDED_REGIMES = frozenset({"crisis", "liquidation_cascade", "volatility_compression", "squeeze"})


class CoverageGuarantee:
    """
    Singleton-ish per AssetScanner. Tracks consecutive idle scan cycles
    and triggers graduated fallback responses.

    Thread-safe: only called from _on_scan_complete() on a single thread.
    """

    # Graduated response thresholds (idle cycles → action)
    LEVEL_INFO = 3        # 45 min at 15m LTF
    LEVEL_EXPAND = 6      # 1.5h
    LEVEL_ENRICHMENT = 12 # 3h
    LEVEL_NOTIFY = 24     # 6h

    # Safety caps
    MAX_FALLBACK_TRADES_PER_WINDOW = 2
    FALLBACK_WINDOW_SECONDS = 6 * 3600  # 6 hours

    def __init__(self):
        self._idle_cycles: int = 0
        self._current_level: int = 0  # 0=normal, 1=info, 2=expand, 3=enrichment, 4=notify
        self._fallback_trade_times: list[float] = []  # timestamps of fallback trades
        self._last_primary_signal_time: float = time.time()
        self._notified: bool = False  # only notify operator once per prolonged gap

    def on_scan_complete(
        self,
        approved_count: int,
        regime_distribution: dict,
        dominant_regime: str,
    ) -> dict:
        """
        Called after every scan cycle completion.

        Parameters
        ----------
        approved_count : int — number of approved candidates this cycle
        regime_distribution : dict[str, int] — regime counts across scanned symbols
        dominant_regime : str — most frequent regime this cycle

        Returns
        -------
        dict with keys:
            level: int (0-4)
            idle_cycles: int
            action: str ("none", "info", "expand_models", "allow_enrichment", "notify_operator")
            expand_regimes: list[str] — regimes where MB should be activated (level 2)
            allow_enrichment_standalone: bool — whether enrichment-only candidates are allowed (level 3)
            fallback_size_multiplier: float — position size cap for fallback trades
            fallback_trades_remaining: int — trades left in the safety window
        """
        # Reset on primary signal
        if approved_count > 0:
            self._idle_cycles = 0
            self._current_level = 0
            self._notified = False
            self._last_primary_signal_time = time.time()
            return self._result(0, "none", dominant_regime)

        self._idle_cycles += 1

        # Determine level
        if self._idle_cycles >= self.LEVEL_NOTIFY:
            level = 4
        elif self._idle_cycles >= self.LEVEL_ENRICHMENT:
            level = 3
        elif self._idle_cycles >= self.LEVEL_EXPAND:
            level = 2
        elif self._idle_cycles >= self.LEVEL_INFO:
            level = 1
        else:
            level = 0

        # Only escalate, never de-escalate within a gap
        self._current_level = max(self._current_level, level)

        # Check if dominant regime is excluded from fallback
        if dominant_regime in _EXCLUDED_REGIMES:
            logger.debug(
                "CoverageGuarantee: idle=%d but regime=%s is excluded from fallback",
                self._idle_cycles, dominant_regime,
            )
            return self._result(self._current_level, "none_excluded", dominant_regime)

        # Graduated response
        if self._current_level == 0:
            return self._result(0, "none", dominant_regime)

        if self._current_level == 1:
            logger.info(
                "CoverageGuarantee: INFO — %d idle cycles (%.0f min). "
                "Regime: %s. No primary model coverage.",
                self._idle_cycles, self._idle_cycles * 15, dominant_regime,
            )
            return self._result(1, "info", dominant_regime)

        if self._current_level == 2:
            logger.warning(
                "CoverageGuarantee: EXPAND — %d idle cycles (%.0f min). "
                "Activating MomentumBreakout in %s at 0.5x size.",
                self._idle_cycles, self._idle_cycles * 15, dominant_regime,
            )
            return self._result(2, "expand_models", dominant_regime,
                                expand_regimes=[dominant_regime],
                                fallback_size_mult=0.50)

        if self._current_level == 3:
            remaining = self._fallback_trades_remaining()
            if remaining <= 0:
                logger.warning(
                    "CoverageGuarantee: ENRICHMENT level reached but fallback trade cap exhausted (%d/%d in window). Waiting.",
                    self.MAX_FALLBACK_TRADES_PER_WINDOW, self.MAX_FALLBACK_TRADES_PER_WINDOW,
                )
                return self._result(3, "enrichment_capped", dominant_regime,
                                    allow_enrichment=False, fallback_size_mult=0.30)

            logger.warning(
                "CoverageGuarantee: ENRICHMENT — %d idle cycles (%.0f min). "
                "Allowing enrichment-standalone candidates at 0.3x size. "
                "Trades remaining in window: %d/%d.",
                self._idle_cycles, self._idle_cycles * 15,
                remaining, self.MAX_FALLBACK_TRADES_PER_WINDOW,
            )
            return self._result(3, "allow_enrichment", dominant_regime,
                                expand_regimes=[dominant_regime],
                                allow_enrichment=True,
                                fallback_size_mult=0.30)

        if self._current_level >= 4 and not self._notified:
            self._notified = True
            logger.critical(
                "CoverageGuarantee: NOTIFY OPERATOR — System inactive %d cycles (%.0f min) "
                "in %s regime. Manual review recommended.",
                self._idle_cycles, self._idle_cycles * 15, dominant_regime,
            )
            return self._result(4, "notify_operator", dominant_regime,
                                expand_regimes=[dominant_regime],
                                allow_enrichment=True,
                                fallback_size_mult=0.30)

        # Level 4 already notified
        return self._result(4, "notified_waiting", dominant_regime,
                            expand_regimes=[dominant_regime],
                            allow_enrichment=True,
                            fallback_size_mult=0.30)

    def record_fallback_trade(self):
        """Call when a fallback trade is actually executed."""
        self._fallback_trade_times.append(time.time())

    def _fallback_trades_remaining(self) -> int:
        """Count remaining fallback trades in the safety window."""
        now = time.time()
        cutoff = now - self.FALLBACK_WINDOW_SECONDS
        # Prune old entries
        self._fallback_trade_times = [t for t in self._fallback_trade_times if t > cutoff]
        return max(0, self.MAX_FALLBACK_TRADES_PER_WINDOW - len(self._fallback_trade_times))

    def _result(self, level, action, regime, expand_regimes=None, allow_enrichment=False, fallback_size_mult=1.0):
        return {
            "level": level,
            "idle_cycles": self._idle_cycles,
            "action": action,
            "dominant_regime": regime,
            "expand_regimes": expand_regimes or [],
            "allow_enrichment_standalone": allow_enrichment,
            "fallback_size_multiplier": fallback_size_mult,
            "fallback_trades_remaining": self._fallback_trades_remaining(),
        }

    def get_state(self) -> dict:
        """Diagnostic state for dashboard."""
        return {
            "idle_cycles": self._idle_cycles,
            "current_level": self._current_level,
            "notified": self._notified,
            "fallback_trades_remaining": self._fallback_trades_remaining(),
            "minutes_since_primary": round((time.time() - self._last_primary_signal_time) / 60, 1),
        }
