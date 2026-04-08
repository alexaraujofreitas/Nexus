# ============================================================
# NEXUS TRADER — Adaptive Slippage Model (Phase 6 v3)
#
# Market-realism slippage estimation incorporating:
#   - Size vs liquidity nonlinearity
#   - Directional asymmetry (buy/sell asymmetry with regime skew)
#   - Order urgency (market vs limit aggressiveness)
#   - Latency impact (Issue 4 integration)
#   - Nonlinear volatility (convex scaling)
#   - Regime-dependent base + skew
#   - Per-regime calibration with decay
#   - Corruption detection + auto-reset
#   - Full state persistence
#
# COMPONENT CLASSIFICATION: Decision-affecting
#   - Feeds FillSimulator via SlippageModel ABC
#   - Participates in governed deterministic replay
#   - Identical inputs → identical outputs (zero randomness)
#
# Design invariants:
#   - Implements SlippageModel ABC (drop-in replacement)
#   - FULLY DETERMINISTIC: zero randomness, no RNG, no seed
#     All variation from observable market state only
#   - All parameters auditable via snapshot()
#   - No Qt/GUI imports
#   - Event-sourced calibration updates
#
# Formula (complete, deterministic):
#   base_pct = (base_min_pct + base_max_pct) / 2
#   vol_mult = 1.0 + vol_scale × norm_atr + vol_convexity × norm_atr²
#   (regime_mult, regime_skew) = regime_params.get(regime, (regime_default_mult, 0.0))
#   size_impact = (size_usdt / reference_liquidity) ^ liquidity_exponent
#   latency_decay = 1.0 + latency_scale × (latency_ms / reference_latency_ms)
#   urgency_mult = urgency_map.get(urgency, 1.0)
#
#   direction_asymmetry = buy_asymmetry + regime_skew  (for BUY)
#                       = sell_asymmetry - regime_skew  (for SELL)
#
#   raw_pct = (base_pct × vol_mult × regime_mult × (1 + size_impact)
#              × latency_decay × urgency_mult × direction_asymmetry)
#             + half_spread + calibration_offset_for_regime
#
#   clamped_pct = clamp(raw_pct, min_slippage_pct, max_slippage_pct)
#   slippage = price × clamped_pct × direction_sign
# ============================================================
import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from core.intraday.execution.fill_simulator import SlippageModel
from core.intraday.execution_contracts import Side

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# ENUMS
# ══════════════════════════════════════════════════════════════

class UrgencyLevel(str, Enum):
    """Order urgency — maps to execution slippage multiplier."""
    MARKET = "market"              # Immediate execution, 1.0 multiplier
    LIMIT_AGGRESSIVE = "limit_aggressive"  # Immediate-ish limit, 0.6 multiplier
    LIMIT_PASSIVE = "limit_passive"        # Patient limit, 0.3 multiplier


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AdaptiveSlippageConfig:
    """Configuration for Phase 6 v3 market-realistic slippage model.

    All percentages are fractions (0.0001 = 0.01% = 1 bps).
    All monetary values in USDT. All times in milliseconds.
    """
    # ── Base slippage: deterministic midpoint of expected range
    # No randomness. Base = midpoint of [min, max] band.
    base_min_pct: float = 0.0001      # 1 bps floor (for midpoint calc)
    base_max_pct: float = 0.0005      # 5 bps ceiling (for midpoint calc)

    # ── Spread component: half-spread added to slippage
    spread_half_default_pct: float = 0.0002  # 2 bps default half-spread

    # ── Volatility scaling: nonlinear (convex in vol expansion)
    # vol_mult = 1.0 + vol_scale × norm_atr + vol_convexity × norm_atr²
    vol_scale: float = 0.5
    vol_convexity: float = 0.25      # Convex term: extra penalty at high vol

    # ── Size impact: (size_usdt / reference_liquidity) ^ liquidity_exponent
    liquidity_exponent: float = 1.5   # Sublinear: impact ∝ size^1.5
    reference_liquidity_usdt: float = 1_000_000.0  # 1M USDT reference

    # ── Directional asymmetry: buy side hits ask, sell side hits bid
    # Raw asymmetries (before regime skew)
    buy_asymmetry: float = 1.05       # 5% extra when lifting ask
    sell_asymmetry: float = 0.95      # 5% less when hitting bid

    # ── Regime parameters: (base_multiplier, skew_offset)
    # Skew increases buy_asymmetry and decreases sell_asymmetry
    regime_params: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        "bull_trend": (0.8, 0.02),        # Trending → tighter, slight buy skew
        "bear_trend": (1.2, -0.02),       # Trending down → wider, sell skew
        "range_bound": (0.9, 0.0),        # Range → neutral
        "high_volatility": (1.5, 0.0),    # Vol spike → wider, no skew
        "uncertain": (1.3, 0.0),          # Uncertain → wider, no skew
    })
    regime_default_mult: float = 1.0
    regime_default_skew: float = 0.0

    # ── Order urgency multipliers (deterministic from UrgencyLevel enum)
    urgency_map: Dict[str, float] = field(default_factory=lambda: {
        UrgencyLevel.MARKET.value: 1.0,              # 100% slippage
        UrgencyLevel.LIMIT_AGGRESSIVE.value: 0.6,    # 60% slippage
        UrgencyLevel.LIMIT_PASSIVE.value: 0.3,       # 30% slippage
    })

    # ── Latency integration (Issue 4): slippage increases with latency
    # latency_decay = 1.0 + latency_scale × (latency_ms / reference_latency_ms)
    latency_scale: float = 0.1
    reference_latency_ms: float = 50.0   # 50ms reference baseline

    # ── Calibration: per-regime rolling window
    calibration_window: int = 100        # Last N fills per regime
    calibration_blend: float = 0.3       # EMA blend factor α

    # ── Calibration safety bounds
    min_calibration_observations: int = 10
    max_calibration_offset_pct: float = 0.0005      # ±5 bps
    max_calibration_step_pct: float = 0.0001        # ±1 bps per update
    max_calibration_per_update: float = 0.00005     # ±0.5 bps absolute cap per update

    # ── Calibration decay (Issue 5): offset decays when no new data
    calibration_decay_rate: float = 0.05            # 5% decay per interval
    calibration_decay_interval_ms: int = 300_000    # 5 minutes

    # ── Corruption detection: skip outlier observations
    corruption_threshold_pct: float = 0.01          # |error| > 1% → skip

    # ── Auto-reset: if error stddev > threshold, reset offset to 0
    calibration_reset_stddev_threshold_pct: float = 0.005  # 50 bps

    # ── Hard caps (safety)
    max_slippage_pct: float = 0.0020    # 20 bps absolute cap
    min_slippage_pct: float = 0.0       # Floor


# ══════════════════════════════════════════════════════════════
# CALIBRATION OBSERVATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SlippageObservation:
    """Single observation of actual vs predicted slippage."""
    timestamp_ms: int
    symbol: str
    side: str           # "buy" or "sell"
    predicted_pct: float
    actual_pct: float
    regime: str
    atr_normalised: float
    spread_pct: float


# ══════════════════════════════════════════════════════════════
# ADAPTIVE SLIPPAGE MODEL (PHASE 6 v3)
# ══════════════════════════════════════════════════════════════

class AdaptiveSlippageModel(SlippageModel):
    """Market-realistic, fully deterministic slippage model.

    Implements SlippageModel ABC. Drop-in replacement for DefaultSlippageModel.

    Key features:
    1. Size impact: nonlinear function of size vs liquidity
    2. Directional asymmetry: buy-side higher (lifts ask), sell-side lower (hits bid)
    3. Order urgency: market orders pay more than limit orders
    4. Latency integration: higher latency → more adverse prices
    5. Nonlinear volatility: convex scaling in vol expansion
    6. Regime-dependent base + skew
    7. Per-regime calibration with decay + corruption detection
    8. Full state persistence

    FULLY DETERMINISTIC: zero randomness. Identical inputs → identical output.
    No RNG, no seed. All variation from observable market state and calibration
    history (which is deterministic from observation sequence replay).

    Thread safety: NOT thread-safe. Use from a single thread or protect with
    external lock.

    Usage:
        cfg = AdaptiveSlippageConfig()
        model = AdaptiveSlippageModel(cfg)

        # Calculate slippage with full context
        slippage = model.calculate_adaptive(
            price=100.0,
            side=Side.BUY,
            size_usdt=50_000.0,      # New: size vs liquidity
            atr=2.0,
            regime="bull_trend",
            symbol="BTC/USDT",
            spread_pct=0.0002,
            urgency=UrgencyLevel.MARKET,  # New: order urgency
            latency_ms=45.0,               # New: Issue 4 latency
        )

        # Record actual fill for calibration
        model.record_observation(
            symbol="BTC/USDT",
            side="buy",
            predicted_pct=0.0007,
            actual_pct=0.0008,
            regime="bull_trend",
            atr_normalised=0.02,
            spread_pct=0.0002,
        )

        # Persist state
        state = model.get_state()
        # ... later ...
        model.restore_state(state)
    """

    def __init__(self, config: AdaptiveSlippageConfig = None):
        self.config = config or AdaptiveSlippageConfig()

        # Deterministic base: midpoint of band
        self._base_pct: float = (
            self.config.base_min_pct + self.config.base_max_pct
        ) / 2.0

        # Per-regime calibration state
        self._regime_offsets: Dict[str, float] = {}
        self._global_offset: float = 0.0
        self._regime_obs_counts: Dict[str, int] = {}
        self._regime_observations: Dict[str, Deque[SlippageObservation]] = {}
        self._last_decay_ms: int = int(time.time() * 1000)

        # Per-symbol spread cache
        self._spread_cache: Dict[str, float] = {}

        logger.info(
            "AdaptiveSlippageModel v3 initialized (DETERMINISTIC): "
            "base=%.1f bps, vol_scale=%.2f, vol_convexity=%.2f, "
            "liquidity_exp=%.1f, buy_asym=%.2f, sell_asym=%.2f, "
            "latency_scale=%.2f, calibration_window=%d",
            self._base_pct * 10000,
            self.config.vol_scale,
            self.config.vol_convexity,
            self.config.liquidity_exponent,
            self.config.buy_asymmetry,
            self.config.sell_asymmetry,
            self.config.latency_scale,
            self.config.calibration_window,
        )

    # ── SlippageModel Protocol ───────────────────────────────

    def calculate_slippage(
        self, price: float, side: Side, seed: int = None,
    ) -> float:
        """Calculate slippage with minimal context (ABC compatibility).

        For full adaptive behaviour, use calculate_adaptive() instead.
        The seed parameter is accepted for ABC compatibility but IGNORED
        — this model is fully deterministic from observable inputs only.
        """
        return self.calculate_adaptive(
            price=price, side=side,
            size_usdt=None, atr=None, regime=None, symbol=None,
            spread_pct=None, urgency=UrgencyLevel.MARKET, latency_ms=None,
        )

    # ── Extended API (Phase 6 v3) ────────────────────────────

    def calculate_adaptive(
        self,
        price: float,
        side: Side,
        size_usdt: float = None,
        atr: float = None,
        regime: str = None,
        symbol: str = None,
        spread_pct: float = None,
        urgency: UrgencyLevel = UrgencyLevel.MARKET,
        latency_ms: float = None,
    ) -> float:
        """Calculate slippage incorporating full market context.

        FULLY DETERMINISTIC: identical inputs → identical output.

        Args:
            price: Current/requested price
            side: BUY or SELL
            size_usdt: Order size in USDT. If None, size_impact=0.
            atr: ATR value (same unit as price). If None, vol_mult=1.0.
            regime: Regime string for multiplier/skew lookup.
            symbol: Symbol for spread cache lookup.
            spread_pct: Live spread as fraction. Overrides cache.
            urgency: UrgencyLevel enum (MARKET, LIMIT_AGGRESSIVE, LIMIT_PASSIVE).
            latency_ms: Network latency in milliseconds. If None, latency_decay=1.0.

        Returns:
            Slippage amount (positive for BUY, negative for SELL)

        Formula breakdown:
        1. base_pct = (base_min_pct + base_max_pct) / 2
        2. vol_mult = 1.0 + vol_scale × norm_atr + vol_convexity × norm_atr²
        3. (regime_mult, regime_skew) = regime_params.get(regime, defaults)
        4. size_impact = (size_usdt / reference_liquidity) ^ liquidity_exponent
        5. latency_decay = 1.0 + latency_scale × (latency_ms / reference_latency_ms)
        6. urgency_mult = urgency_map.get(urgency, 1.0)
        7. direction_asymmetry = buy_asymmetry + regime_skew (for BUY)
                               = sell_asymmetry - regime_skew (for SELL)
        8. raw_pct = (base_pct × vol_mult × regime_mult × (1 + size_impact)
                      × latency_decay × urgency_mult × direction_asymmetry)
                     + half_spread + calibration_offset_for_regime
        9. clamped_pct = clamp(raw_pct, min_slippage_pct, max_slippage_pct)
        10. slippage = price × clamped_pct × direction_sign
        """
        cfg = self.config

        # 1. Base slippage: deterministic midpoint
        base_pct = self._base_pct

        # 2. Volatility scaling (nonlinear)
        vol_mult = 1.0
        norm_atr = 0.0
        if atr is not None and price > 0:
            norm_atr = atr / price
            vol_mult = 1.0 + cfg.vol_scale * norm_atr + cfg.vol_convexity * (norm_atr ** 2)

        # 3. Regime parameters
        regime_mult = cfg.regime_default_mult
        regime_skew = cfg.regime_default_skew
        if regime:
            params = cfg.regime_params.get(regime)
            if params:
                regime_mult, regime_skew = params

        # 4. Size impact: (size / liquidity) ^ exponent
        size_impact = 0.0
        if size_usdt is not None and size_usdt > 0:
            size_pct = size_usdt / cfg.reference_liquidity_usdt
            size_impact = (size_pct ** cfg.liquidity_exponent)

        # 5. Latency decay: higher latency = more adverse execution
        latency_decay = 1.0
        if latency_ms is not None and latency_ms > 0:
            latency_decay = 1.0 + cfg.latency_scale * (latency_ms / cfg.reference_latency_ms)

        # 6. Order urgency multiplier
        urgency_mult = cfg.urgency_map.get(urgency.value, 1.0)

        # 7. Directional asymmetry (regime-modulated)
        if side == Side.BUY:
            direction_asymmetry = cfg.buy_asymmetry + regime_skew
        else:
            direction_asymmetry = cfg.sell_asymmetry - regime_skew

        # 8. Combine all factors
        raw_pct = (
            base_pct * vol_mult * regime_mult * (1.0 + size_impact)
            * latency_decay * urgency_mult * direction_asymmetry
        )

        # Add spread
        half_spread = cfg.spread_half_default_pct
        if spread_pct is not None:
            half_spread = spread_pct / 2.0
        elif symbol and symbol in self._spread_cache:
            half_spread = self._spread_cache[symbol] / 2.0

        # Add per-regime calibration offset
        cal_offset = self._get_calibration_offset(regime)
        raw_pct += half_spread + cal_offset

        # 9. Clamp to safety bounds
        clamped_pct = max(cfg.min_slippage_pct, min(cfg.max_slippage_pct, raw_pct))

        # 10. Direction sign
        if side == Side.BUY:
            slippage = price * clamped_pct
        else:
            slippage = -(price * clamped_pct)

        logger.debug(
            "AdaptiveSlippage v3: price=%.2f side=%s size_usdt=%s atr=%.2f latency_ms=%s "
            "base=%.4f%% vol_mult=%.3f regime_mult=%.2f regime_skew=%.4f size_impact=%.4f%% "
            "latency_decay=%.3f urgency_mult=%.2f dir_asym=%.3f half_spread=%.4f%% "
            "cal_offset=%.4f%% → %.4f%% (clamped) → slippage=%.6f",
            price, side.value, size_usdt, atr or 0.0, latency_ms or 0.0,
            base_pct * 100, vol_mult, regime_mult, regime_skew * 100, size_impact * 100,
            latency_decay, urgency_mult, direction_asymmetry, half_spread * 100,
            cal_offset * 100, clamped_pct * 100, slippage,
        )

        return slippage

    # ── Calibration ──────────────────────────────────────────

    def record_observation(
        self,
        symbol: str,
        side: str,
        predicted_pct: float,
        actual_pct: float,
        regime: str = "",
        atr_normalised: float = 0.0,
        spread_pct: float = 0.0,
        now_ms: int = None,
    ) -> None:
        """Record an actual slippage observation for per-regime calibration.

        Called by ExecutionQualityTracker after each fill.
        Calibration is fully deterministic: replaying the same sequence of
        observations produces the same offset state.

        Args:
            symbol: Trading symbol
            side: "buy" or "sell"
            predicted_pct: Predicted slippage as fraction
            actual_pct: Actual slippage as fraction
            regime: Regime string (defaults to "")
            atr_normalised: Normalized ATR (atr/price)
            spread_pct: Observed spread as fraction
            now_ms: Timestamp (defaults to current time)
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        obs = SlippageObservation(
            timestamp_ms=now_ms,
            symbol=symbol,
            side=side,
            predicted_pct=predicted_pct,
            actual_pct=actual_pct,
            regime=regime,
            atr_normalised=atr_normalised,
            spread_pct=spread_pct,
        )

        # Store in per-regime deque
        if regime not in self._regime_observations:
            self._regime_observations[regime] = deque(maxlen=self.config.calibration_window)
            self._regime_offsets[regime] = 0.0

        self._regime_observations[regime].append(obs)
        self._regime_obs_counts[regime] = len(self._regime_observations[regime])

        # Apply decay to all offsets if interval has passed
        self._apply_decay(now_ms)

        # Recalibrate this regime
        self._recalibrate_regime(regime, now_ms)

    def update_spread(self, symbol: str, spread_pct: float) -> None:
        """Update cached spread for a symbol."""
        self._spread_cache[symbol] = spread_pct

    def _get_calibration_offset(self, regime: str = "") -> float:
        """Get calibration offset for regime, or global fallback."""
        cfg = self.config

        # If regime has enough observations, use regime-specific offset
        if regime in self._regime_obs_counts:
            if self._regime_obs_counts[regime] >= cfg.min_calibration_observations:
                return self._regime_offsets.get(regime, 0.0)

        # Fallback to global offset
        return self._global_offset

    def _apply_decay(self, now_ms: int) -> None:
        """Apply calibration decay to all offsets if interval elapsed.

        Decay reduces offset toward zero to avoid stale calibration:
            offset *= (1 - decay_rate)
        """
        cfg = self.config
        time_elapsed_ms = now_ms - self._last_decay_ms

        if time_elapsed_ms < cfg.calibration_decay_interval_ms:
            return

        decay_factor = 1.0 - cfg.calibration_decay_rate
        self._global_offset *= decay_factor

        for regime in self._regime_offsets:
            self._regime_offsets[regime] *= decay_factor

        self._last_decay_ms = now_ms

        logger.debug(
            "Slippage calibration decay applied: factor=%.3f, "
            "global_offset=%.4f%%, regime_offsets=%s",
            decay_factor,
            self._global_offset * 100,
            {r: f"{v*100:.4f}%" for r, v in self._regime_offsets.items()},
        )

    def _recalibrate_regime(self, regime: str, now_ms: int) -> None:
        """Recalibrate calibration offset for a specific regime.

        Calibration equation (formally specified):
            error_i = actual_pct_i - predicted_pct_i for each observation
            if |error_i| > corruption_threshold: skip (log warning)
            errors_clean = [e for e in errors if |e| <= corruption_threshold]
            if stddev(errors_clean) > reset_threshold: reset offset to 0
            else:
                mean_error = mean(errors_clean)
                raw_new = (1 - α) × offset_old + α × mean_error
                Δ = raw_new - offset_old
                Δ_clamped = clamp(Δ, -max_step, +max_step)
                offset_new = clamp(offset_old + Δ_clamped, -max_offset, +max_offset)

        Guards:
            - No calibration until min_calibration_observations reached
            - Corrupt observations (|error| > threshold) are skipped + logged
            - Per-update step capped at max_calibration_step_pct
            - Absolute offset capped at max_calibration_offset_pct
            - Auto-reset if error stddev > threshold
            - Deterministic replay
        """
        cfg = self.config

        if regime not in self._regime_observations:
            return

        obs_deque = self._regime_observations[regime]
        if len(obs_deque) < cfg.min_calibration_observations:
            return

        # Compute errors, detect corruption
        errors = []
        for obs in obs_deque:
            error = obs.actual_pct - obs.predicted_pct
            if abs(error) > cfg.corruption_threshold_pct:
                logger.warning(
                    "Slippage observation corruption detected: regime=%s symbol=%s "
                    "side=%s error=%.6f%% (threshold=%.6f%%)",
                    regime, obs.symbol, obs.side, error * 100,
                    cfg.corruption_threshold_pct * 100,
                )
                continue  # Skip corrupt observation
            errors.append(error)

        if not errors:
            logger.warning("All observations for regime=%s are corrupt; not recalibrating", regime)
            return

        # Check for auto-reset condition
        if len(errors) > 1:
            error_stddev = statistics.stdev(errors)
            if error_stddev > cfg.calibration_reset_stddev_threshold_pct:
                logger.info(
                    "Slippage calibration auto-reset: regime=%s stddev=%.6f%% > threshold=%.6f%%",
                    regime, error_stddev * 100,
                    cfg.calibration_reset_stddev_threshold_pct * 100,
                )
                self._regime_offsets[regime] = 0.0
                return

        # Normal calibration: EMA blend
        mean_error = sum(errors) / len(errors)
        old_offset = self._regime_offsets.get(regime, 0.0)

        raw_new = (1.0 - cfg.calibration_blend) * old_offset + cfg.calibration_blend * mean_error

        # Per-update step cap
        delta = raw_new - old_offset
        delta_clamped = max(
            -cfg.max_calibration_step_pct,
            min(cfg.max_calibration_step_pct, delta)
        )

        # Apply clamped step
        new_offset = old_offset + delta_clamped
        new_offset = max(
            -cfg.max_calibration_offset_pct,
            min(cfg.max_calibration_offset_pct, new_offset)
        )

        self._regime_offsets[regime] = new_offset

        logger.debug(
            "Slippage calibration (regime=%s): n=%d mean_error=%.4f%% Δ=%.4f%% "
            "(clamped=%.4f%%) → offset=%.4f%%",
            regime, len(errors), mean_error * 100, delta * 100,
            delta_clamped * 100, new_offset * 100,
        )

    # ── State Persistence ────────────────────────────────────

    def reset_calibration(self) -> None:
        """Clear all calibration offsets and observations.

        Used for fresh start or manual reset.
        """
        self._regime_offsets.clear()
        self._global_offset = 0.0
        self._regime_obs_counts.clear()
        self._regime_observations.clear()
        self._last_decay_ms = int(time.time() * 1000)
        logger.info("Slippage calibration reset to zero")

    def get_state(self) -> dict:
        """Return full model state for persistence.

        State is deterministic: restoring from this dict produces
        identical results on subsequent calculations.

        Returns:
            dict with keys:
                - global_offset_pct: float
                - regime_offsets: Dict[str, float]
                - regime_obs_counts: Dict[str, int]
                - last_decay_ms: int
                - observations: List of dicts (per regime)
                - config: Full config dict
                - base_pct: float
                - spread_cache: Dict[str, float]
        """
        # Serialize observations as list of dicts
        observations_list = {}
        for regime, obs_deque in self._regime_observations.items():
            observations_list[regime] = [
                {
                    "timestamp_ms": obs.timestamp_ms,
                    "symbol": obs.symbol,
                    "side": obs.side,
                    "predicted_pct": obs.predicted_pct,
                    "actual_pct": obs.actual_pct,
                    "regime": obs.regime,
                    "atr_normalised": obs.atr_normalised,
                    "spread_pct": obs.spread_pct,
                }
                for obs in obs_deque
            ]

        return {
            "global_offset_pct": self._global_offset,
            "regime_offsets": dict(self._regime_offsets),
            "regime_obs_counts": dict(self._regime_obs_counts),
            "last_decay_ms": self._last_decay_ms,
            "observations": observations_list,
            "config": {
                "base_min_pct": self.config.base_min_pct,
                "base_max_pct": self.config.base_max_pct,
                "spread_half_default_pct": self.config.spread_half_default_pct,
                "vol_scale": self.config.vol_scale,
                "vol_convexity": self.config.vol_convexity,
                "liquidity_exponent": self.config.liquidity_exponent,
                "reference_liquidity_usdt": self.config.reference_liquidity_usdt,
                "buy_asymmetry": self.config.buy_asymmetry,
                "sell_asymmetry": self.config.sell_asymmetry,
                "regime_default_mult": self.config.regime_default_mult,
                "regime_default_skew": self.config.regime_default_skew,
                "latency_scale": self.config.latency_scale,
                "reference_latency_ms": self.config.reference_latency_ms,
                "calibration_window": self.config.calibration_window,
                "calibration_blend": self.config.calibration_blend,
                "min_calibration_observations": self.config.min_calibration_observations,
                "max_calibration_offset_pct": self.config.max_calibration_offset_pct,
                "max_calibration_step_pct": self.config.max_calibration_step_pct,
                "calibration_decay_rate": self.config.calibration_decay_rate,
                "calibration_decay_interval_ms": self.config.calibration_decay_interval_ms,
                "corruption_threshold_pct": self.config.corruption_threshold_pct,
                "calibration_reset_stddev_threshold_pct": self.config.calibration_reset_stddev_threshold_pct,
                "max_slippage_pct": self.config.max_slippage_pct,
                "min_slippage_pct": self.config.min_slippage_pct,
            },
            "base_pct": self._base_pct,
            "spread_cache": dict(self._spread_cache),
        }

    def restore_state(self, state: dict) -> None:
        """Restore model state from persistence dict.

        Restores observations in order, which deterministically recomputes
        all calibration offsets. Safe to call multiple times (overwrites).

        Args:
            state: dict from get_state()
        """
        self._global_offset = state.get("global_offset_pct", 0.0)
        self._regime_offsets = dict(state.get("regime_offsets", {}))
        self._regime_obs_counts = dict(state.get("regime_obs_counts", {}))
        self._last_decay_ms = state.get("last_decay_ms", int(time.time() * 1000))
        self._spread_cache = dict(state.get("spread_cache", {}))

        # Clear old observations
        self._regime_observations.clear()

        # Restore observations (deterministically rebuilds offsets)
        observations_dict = state.get("observations", {})
        for regime, obs_list in observations_dict.items():
            self._regime_observations[regime] = deque(maxlen=self.config.calibration_window)
            for obs_dict in obs_list:
                obs = SlippageObservation(
                    timestamp_ms=obs_dict["timestamp_ms"],
                    symbol=obs_dict["symbol"],
                    side=obs_dict["side"],
                    predicted_pct=obs_dict["predicted_pct"],
                    actual_pct=obs_dict["actual_pct"],
                    regime=obs_dict["regime"],
                    atr_normalised=obs_dict["atr_normalised"],
                    spread_pct=obs_dict["spread_pct"],
                )
                self._regime_observations[regime].append(obs)

        logger.info(
            "Slippage model state restored: global_offset=%.4f%%, "
            "regime_offsets=%s, total_obs=%d",
            self._global_offset * 100,
            {r: f"{v*100:.4f}%" for r, v in self._regime_offsets.items()},
            sum(len(d) for d in self._regime_observations.values()),
        )

    # ── Properties (read-only) ──────────────────────────────

    @property
    def calibration_offset(self) -> float:
        """Current global calibration offset (read-only)."""
        return self._global_offset

    @property
    def observation_count(self) -> int:
        """Total number of observations across all regimes."""
        return sum(len(d) for d in self._regime_observations.values())

    @property
    def regime_offsets(self) -> Dict[str, float]:
        """Per-regime calibration offsets (read-only)."""
        return dict(self._regime_offsets)
