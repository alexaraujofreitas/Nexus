"""
Open Interest (OI) Signal Modifier — Phase 4 / Session 23.
Reads OI data from exchange and applies as a confluence score modifier.
Not a standalone model — modifies the score of confirmed signals.

Edge hypothesis:
- OI rising + price rising → new money entering, trend continuation (+bonus)
- OI falling + price rising → short covering, weak move (-penalty)
- OI spike (>30% in 1h) → unstable market structure, suppress trade (-penalty)
- Large liquidation clusters in trade direction → amplifies expected move (+bonus)

Ablation toggles (independent):
- oi_signal.oi_modifier_enabled   — controls OI trend-confirm / weak-trend modifier
- oi_signal.liq_modifier_enabled  — controls liquidation cluster bonus
- oi_signal.enabled               — master switch; disables both when false

OI Data Quality (Session 23 safety layer):
  assess_oi_data_quality() returns (score: int, reason: str) where:
    0 — No coinglass agent (initialisation failure)
    1 — Agent present but returned no data for this symbol
    2 — Data present but unusable: stale (age > OI_STALE_MINUTES) OR extreme spike
    3 — Data present, fresh, and within normal range — safe to apply modifiers

  The OI modifier is suppressed (returns 0.0) when quality < OI_MIN_QUALITY_THRESHOLD.
  Default threshold = 2 (stale or no-data → suppress; only fresh data applied).

  This prevents: applying OI modifiers based on 6-hour-old data, on cached zeros
  from an agent that failed its last fetch, or during a violent OI cascade that
  makes the modifier sign unpredictable.

Validation:
  To test whether OI modifier improves expectancy, set oi_signal.oi_modifier_enabled=false
  and compare avg_r / profit_factor of approved trades vs baseline. Run for ≥75 trades each.
  Similarly for liq_modifier_enabled. See data/filter_stats.json for per-fire counts.
"""
from __future__ import annotations
import logging
import time
from collections import deque
from typing import Optional
from config.settings import settings as _s

logger = logging.getLogger(__name__)

# ── Data quality constants ──────────────────────────────────────────────────
OI_STALE_MINUTES         = 10    # OI data older than this is considered stale
OI_MIN_QUALITY_THRESHOLD = 2     # Suppress modifier below this quality score
# Rolling stability: track last N oi_change_1h readings per symbol
_OI_HISTORY_WINDOW       = 5
_oi_history: dict[str, deque] = {}  # symbol → deque of recent oi_change_1h values


def assess_oi_data_quality(symbol: str) -> tuple[int, str]:
    """
    Return (quality_score: int, reason: str) for OI data availability.

    Scores:
      0 — No coinglass agent
      1 — Agent present, no data for symbol
      2 — Data present but stale or during extreme spike (unusable)
      3 — Fresh, within normal range (safe to apply modifier)

    Call this before applying any OI modifier to ensure signal integrity.
    A quality < OI_MIN_QUALITY_THRESHOLD means suppress the modifier.
    """
    try:
        from core.agents.coinglass_agent import coinglass_agent
        if coinglass_agent is None:
            return 0, "no_agent"
    except Exception:
        return 0, "agent_import_error"

    oi_data = coinglass_agent.get_oi_data(symbol)
    if not oi_data:
        return 1, "no_data"

    # Freshness check — some agents cache indefinitely on fetch failure
    data_age_s = float(oi_data.get("age_seconds", 0.0) or 0.0)
    stale_threshold_s = OI_STALE_MINUTES * 60
    if data_age_s > stale_threshold_s:
        return 2, f"stale_{int(data_age_s/60)}min"

    # Extreme spike check — unreliable data during cascade
    oi_change = float(oi_data.get("oi_change_1h_pct", 0.0) or 0.0)
    spike_threshold = float(_s.get("oi_signal.spike_threshold_pct", 30.0))
    if abs(oi_change) > spike_threshold:
        return 2, f"extreme_spike_{oi_change:+.1f}pct"

    return 3, "fresh"


def get_oi_stability_cv(symbol: str, new_value: Optional[float] = None) -> Optional[float]:
    """
    Compute the coefficient of variation (CV = std/mean) of the last N OI readings.
    A high CV (> 1.0) means OI is erratic — don't trust the modifier signal.

    new_value: if provided, adds it to the rolling history.
    Returns None if fewer than 3 readings are available.
    """
    if symbol not in _oi_history:
        _oi_history[symbol] = deque(maxlen=_OI_HISTORY_WINDOW)
    if new_value is not None:
        _oi_history[symbol].append(float(new_value))

    hist = list(_oi_history[symbol])
    if len(hist) < 3:
        return None
    mean = sum(hist) / len(hist)
    if mean == 0:
        return None
    variance = sum((x - mean) ** 2 for x in hist) / len(hist)
    std = variance ** 0.5
    return round(std / abs(mean), 3)


def get_oi_modifier(
    symbol: str,
    direction: str,
    price_change_pct: Optional[float] = None,
) -> tuple[float, str]:
    """
    Returns (score_modifier, reason_string).
    modifier > 0 = bonus, modifier < 0 = penalty, 0 = no signal/disabled.

    Activation conditions:
      Bonus  (+trend_confirm_bonus, default +0.05):
        direction=long AND price_change_pct > 0 AND oi_change_1h > +2.0%
        direction=short AND price_change_pct < 0 AND oi_change_1h > +2.0%
      Penalty  (-weak_trend_penalty, default -0.03):
        direction=long AND price_change_pct > 0 AND oi_change_1h < -5.0%
        direction=short AND price_change_pct < 0 AND oi_change_1h < -5.0%
      Suppression  (-0.10, fixed):
        abs(oi_change_1h) > spike_threshold_pct (default 30%)
    """
    if not _s.get("oi_signal.enabled", True):
        return 0.0, ""
    if not _s.get("oi_signal.oi_modifier_enabled", True):
        return 0.0, "oi_modifier_disabled"

    try:
        # ── Data quality gate (Session 23 safety layer) ───────────────────
        quality, quality_reason = assess_oi_data_quality(symbol)
        min_quality = int(_s.get("oi_signal.min_data_quality", OI_MIN_QUALITY_THRESHOLD))
        if quality < min_quality:
            logger.debug(
                "OISignal %s: suppressed — data quality %d < %d (%s)",
                symbol, quality, min_quality, quality_reason,
            )
            return 0.0, f"quality_{quality}_{quality_reason}"

        from core.agents.coinglass_agent import coinglass_agent
        oi_data = coinglass_agent.get_oi_data(symbol)

        oi_change_1h = float(oi_data.get("oi_change_1h_pct", 0.0) or 0.0)

        # Update rolling stability history
        get_oi_stability_cv(symbol, new_value=oi_change_1h)

        # Spike suppression — already caught by data quality gate (quality=2) but
        # keep explicit check here for safety and logging clarity.
        spike_threshold = float(_s.get("oi_signal.spike_threshold_pct", 30.0))
        if abs(oi_change_1h) > spike_threshold:
            reason = f"OI spike {oi_change_1h:+.1f}% in 1h — suppress"
            logger.info(
                "OISignal %s: PENALTY -0.10 | %s",
                symbol, reason,
            )
            return -0.10, reason

        bonus = float(_s.get("oi_signal.trend_confirm_bonus", 0.05))
        penalty = float(_s.get("oi_signal.weak_trend_penalty", 0.03))
        is_long = direction.lower() in ("buy", "long")
        price_up = (price_change_pct or 0.0) > 0
        oi_rising = oi_change_1h > 2.0

        # Trend confirmation: new money entering in the right direction
        if is_long and price_up and oi_rising:
            reason = f"OI +{oi_change_1h:.1f}% confirms long"
            logger.info("OISignal %s: BONUS +%.3f | %s", symbol, bonus, reason)
            return bonus, reason
        if not is_long and not price_up and oi_rising:
            reason = f"OI +{oi_change_1h:.1f}% confirms short"
            logger.info("OISignal %s: BONUS +%.3f | %s", symbol, bonus, reason)
            return bonus, reason

        # Weak-trend: liquidation/short-covering, not genuine pressure
        if is_long and price_up and oi_change_1h < -5.0:
            reason = f"OI {oi_change_1h:.1f}% declining — short covering, not fresh longs"
            logger.info("OISignal %s: PENALTY -%.3f | %s", symbol, penalty, reason)
            return -penalty, reason
        if not is_long and not price_up and oi_change_1h < -5.0:
            reason = f"OI {oi_change_1h:.1f}% declining — long exits, not real selling"
            logger.info("OISignal %s: PENALTY -%.3f | %s", symbol, penalty, reason)
            return -penalty, reason

        return 0.0, "oi_neutral"
    except Exception as exc:
        logger.debug("OISignal: error for %s: %s", symbol, exc)
        return 0.0, "error"


def get_liquidation_modifier(
    symbol: str,
    direction: str,
) -> tuple[float, str]:
    """
    Returns (score_modifier, reason) based on liquidation cluster proximity.
    Large clusters in the trade direction amplify expected move.

    Activation conditions:
      Bonus  (+liq_cluster_bonus, default +0.04):
        direction=long AND liq_density_long > liq_density_threshold (default 0.70)
        direction=short AND liq_density_short > liq_density_threshold
    """
    if not _s.get("oi_signal.enabled", True):
        return 0.0, ""
    if not _s.get("oi_signal.liq_modifier_enabled", True):
        return 0.0, "liq_modifier_disabled"

    try:
        from core.agents.liquidation_intelligence_agent import get_liquidation_intelligence_agent
        agent = get_liquidation_intelligence_agent()
        if agent is None:
            return 0.0, "no_agent"

        liq_data = agent.get_symbol_data(symbol)
        if not liq_data:
            return 0.0, "no_data"

        is_long = direction.lower() in ("buy", "long")
        liq_above = float(liq_data.get("liq_density_long", 0.0) or 0.0)
        liq_below = float(liq_data.get("liq_density_short", 0.0) or 0.0)
        threshold = float(_s.get("oi_signal.liq_density_threshold", 0.70))
        bonus = float(_s.get("oi_signal.liq_cluster_bonus", 0.04))

        if is_long and liq_above > threshold:
            reason = f"Liq cluster above: density={liq_above:.2f} — long target supported"
            logger.info("LiqSignal %s: BONUS +%.3f | %s", symbol, bonus, reason)
            return bonus, reason
        if not is_long and liq_below > threshold:
            reason = f"Liq cluster below: density={liq_below:.2f} — short target supported"
            logger.info("LiqSignal %s: BONUS +%.3f | %s", symbol, bonus, reason)
            return bonus, reason
        return 0.0, "liq_neutral"
    except Exception as exc:
        logger.debug("LiqSignal: error for %s: %s", symbol, exc)
        return 0.0, "error"
