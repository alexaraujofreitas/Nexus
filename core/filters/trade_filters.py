"""
Trade-level pre-filters applied before signal generation.
Each filter is stateless and returns (passed: bool, reason: str).

Instrumentation:
  Every pass/fail result is recorded in FilterStatsTracker (data/filter_stats.json).
  After 50+ trades, query get_filter_stats_tracker().get_all_summaries() to review:
    - Block rate per filter
    - Top blocked symbols / regimes
    - avg_accepted_confluence_score vs avg_blocked_confluence_score
    - avg_accepted_realized_r (populated as trades close)

Ablation:
  To disable a specific filter without touching code:
    config.yaml:  filters.time_of_day.enabled: false
    config.yaml:  filters.volatility.enabled: false
  The stats accumulation continues even when a filter is disabled — it records
  all candidates as "accepted" (with reason="") so baseline rates are preserved.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
from config.settings import settings as _s

logger = logging.getLogger(__name__)


def check_time_of_day(dt: Optional[datetime] = None) -> tuple[bool, str]:
    """
    Only allow trades during high-liquidity UTC hours.
    Default: 12:00-21:00 UTC (EU/US overlap + US session).
    Returns (passed, reason).

    Evidence basis: crypto volume and spread data show EU/US overlap
    (12:00-21:00 UTC) has 2-3× the daily average order flow. This filter
    is a hypothesis — we do not yet have empirical evidence it improves
    expectancy. FilterStatsTracker will provide that evidence over time.
    """
    if not _s.get("filters.time_of_day.enabled", True):
        return True, ""
    now = dt or datetime.now(timezone.utc)
    hour = now.hour
    start = int(_s.get("filters.time_of_day.start_hour_utc", 12))
    end   = int(_s.get("filters.time_of_day.end_hour_utc", 21))
    if start <= hour < end:
        return True, ""
    return False, f"Time filter: UTC {hour:02d}:xx outside window {start:02d}:00-{end:02d}:00"


def check_volatility(df: pd.DataFrame, timeframe: str = "1h") -> tuple[bool, str]:
    """
    Reject trades when current ATR is below a fraction of its 20-period average.
    Low-volatility periods produce more false breakouts and tighter P&L.
    Returns (passed, reason).

    The 0.5 ATR ratio threshold is NOT validated. It is a starting hypothesis.
    FilterStatsTracker will measure whether blocked trades (ratio < 0.5) have
    lower realized_r than accepted trades. If they are similar, lower the
    threshold or disable this filter.
    """
    if not _s.get("filters.volatility.enabled", True):
        return True, ""
    if df is None or len(df) < 25:
        return True, ""  # Not enough data — pass through
    try:
        atr_col = "atr" if "atr" in df.columns else None
        if atr_col is None:
            # Compute ATR from scratch if not in df
            high  = df["high"].astype(float)
            low   = df["low"].astype(float)
            close = df["close"].astype(float)
            tr = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low  - close.shift(1)).abs()
            ], axis=1).max(axis=1)
            atr_series = tr.rolling(14).mean()
        else:
            atr_series = df[atr_col].astype(float)
        current_atr = float(atr_series.iloc[-1])
        avg_atr     = float(atr_series.iloc[-20:].mean())
        if avg_atr <= 0:
            return True, ""
        ratio = current_atr / avg_atr
        min_ratio = float(_s.get("filters.volatility.min_atr_ratio", 0.5))
        if ratio >= min_ratio:
            return True, f"atr_ratio={ratio:.2f}"
        return False, f"Volatility filter: ATR ratio {ratio:.2f} < min {min_ratio:.2f} (low-vol rejection)"
    except Exception as exc:
        logger.debug("VolatilityFilter: error computing ATR — %s", exc)
        return True, ""  # Pass on error to avoid blocking


def apply_pre_scan_filters(
    symbol: str,
    df: Optional[pd.DataFrame] = None,
    timeframe: str = "1h",
    regime: str = "",
) -> tuple[bool, str]:
    """
    Apply all pre-scan filters. Returns (all_passed, first_rejection_reason).
    Called before signal generation in scanner.

    Records every result in FilterStatsTracker for offline analysis.
    """
    # Time-of-day
    tod_passed, tod_reason = check_time_of_day()
    _record_filter("time_of_day", symbol, regime, tod_passed, tod_reason)
    if not tod_passed:
        logger.debug("PreFilter REJECTED %s: %s", symbol, tod_reason)
        return False, tod_reason

    # Volatility (requires df)
    if df is not None:
        vol_passed, vol_reason = check_volatility(df, timeframe)
        _record_filter("volatility", symbol, regime, vol_passed, vol_reason)
        if not vol_passed:
            logger.debug("PreFilter REJECTED %s: %s", symbol, vol_reason)
            return False, vol_reason

    return True, ""


def _record_filter(
    filter_name: str,
    symbol: str,
    regime: str,
    passed: bool,
    reason: str,
) -> None:
    """Non-fatal stats recording — never blocks the scan."""
    try:
        from core.analytics.filter_stats import get_filter_stats_tracker
        get_filter_stats_tracker().record_filter_result(
            filter_name=filter_name,
            symbol=symbol,
            regime=regime,
            passed=passed,
            reason=reason,
        )
    except Exception as exc:
        logger.debug("FilterStatsTracker: record error: %s", exc)
