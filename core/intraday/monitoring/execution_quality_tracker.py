# ============================================================
# NEXUS TRADER — Execution Quality Tracker (Phase 6 v3)
#
# Records and analyses actual vs expected fill quality:
#   - Per-symbol slippage tracking with control loop stability
#   - Rate-of-change bounded output (max 1 bps per fill)
#   - Hysteresis to prevent jitter
#   - Effective sample size weighting (cold-start smoothing)
#   - Volatile regime detection (use p75 instead of mean)
#   - Feeds dynamic slippage estimate to RiskGate EV gate
#
# COMPONENT CLASSIFICATION: Decision-affecting (bounded, replay-critical)
#   - Feeds RiskGate via get_dynamic_slippage_estimate(symbol)
#   - Output bounded to [0.0, max_slippage_estimate_pct]
#   - Rate-of-change bounded per fill
#   - Deterministic from observation sequence
#   - Symbol-isolated (no cross-contamination)
#
# EQT → RiskGate CONTRACT (Phase 6 v3):
#   Interface: get_dynamic_slippage_estimate(symbol: str) -> float
#   Returns: float in [0.0, max_slippage_estimate_pct]
#   Cold-start: default_slippage_pct (5 bps)
#   Update cadence: per-fill via record_fill()
#   Output delta capped: max_output_delta_per_fill (1 bps)
#   Hysteresis: only updates when raw differs from current by > 0.5 bps
#   Volatile regime: if stddev > threshold, use p75 instead of mean
#   Effective sample size: blend toward default when n < 2*min_observations
#   Deterministic: same observation sequence → same estimates
#   Replay-safe: get_state() / restore_state() methods
#
# Design invariants:
#   - No Qt/GUI imports
#   - Event-sourced: all observations append-only
#   - Thread-safe via RLock (dashboard reads + execution writes)
#   - Symbol-level isolation: each symbol independent deque + estimate
# ============================================================
import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExecutionQualityConfig:
    """Configuration for execution quality tracking.

    All parameters control the slippage estimation pipeline and
    control loop stability guarantees.
    """
    # ── Rolling window for per-symbol observations
    window_size: int = 200

    # ── Minimum observations before producing non-default estimate
    min_observations: int = 10

    # ── Default slippage estimate (used when < min_observations, cold-start)
    default_slippage_pct: float = 0.0005  # 5 bps

    # ── Maximum slippage estimate output (hard cap on bounded influence)
    # EQT can never return a value larger than this
    max_slippage_estimate_pct: float = 0.0020  # 20 bps

    # ── Rate-of-change cap per fill (controls control loop stability)
    max_output_delta_per_fill: float = 0.0001  # 1 bps

    # ── Hysteresis threshold: only update estimate if raw differs from
    # previous by > this amount (prevents jitter)
    hysteresis_threshold: float = 0.00005  # 0.5 bps

    # ── Volatile regime detection: if stddev > this, use p75 instead of mean
    volatile_stddev_threshold: float = 0.0010  # 10 bps

    # ── Quality degradation threshold
    # If mean slippage > degradation_threshold × default, flag as degraded
    degradation_threshold: float = 2.0

    # ── Retention: max observations in global deque before eviction
    max_observations: int = 1000


# ══════════════════════════════════════════════════════════════
# FILL QUALITY OBSERVATION
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FillQualityObservation:
    """Single observation of execution quality.

    Immutable record of a single fill, capturing prices, slippage,
    fees, and latency across all dimensions.
    """
    timestamp_ms: int
    trigger_id: str
    symbol: str
    strategy_class: str
    regime: str
    side: str

    # ── Prices
    expected_price: float     # Price at signal generation
    requested_price: float    # Price sent to exchange/simulator
    filled_price: float       # Actual fill price

    # ── Slippage
    slippage_pct: float       # |filled - requested| / requested, absolute

    # ── Fees
    fee_usdt: float
    fee_rate: float
    is_maker: bool

    # ── Timing
    signal_to_fill_ms: int    # Total pipeline latency

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "timestamp_ms": self.timestamp_ms,
            "trigger_id": self.trigger_id,
            "symbol": self.symbol,
            "strategy_class": self.strategy_class,
            "regime": self.regime,
            "side": self.side,
            "expected_price": self.expected_price,
            "requested_price": self.requested_price,
            "filled_price": self.filled_price,
            "slippage_pct": self.slippage_pct,
            "fee_usdt": self.fee_usdt,
            "fee_rate": self.fee_rate,
            "is_maker": self.is_maker,
            "signal_to_fill_ms": self.signal_to_fill_ms,
        }


# ══════════════════════════════════════════════════════════════
# QUALITY STATISTICS
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class QualityStats:
    """Computed statistics for a dimension slice.

    All metrics computed from fill observations, including
    degradation flag for monitoring alerts.
    """
    count: int
    mean_slippage_pct: float
    median_slippage_pct: float
    p95_slippage_pct: float
    max_slippage_pct: float
    std_slippage_pct: float
    mean_fee_rate: float
    maker_ratio: float          # Fraction of fills that were maker
    mean_signal_to_fill_ms: float
    is_degraded: bool           # True if mean_slippage > threshold


# ══════════════════════════════════════════════════════════════
# EXECUTION QUALITY TRACKER
# ══════════════════════════════════════════════════════════════

class ExecutionQualityTracker:
    """Tracks and analyses execution quality with bounded, replay-safe output.

    Provides:
      - Per-symbol slippage tracking with rolling deques
      - Dynamic slippage estimate with control loop stability (rate-of-change cap)
      - Hysteresis (jitter prevention)
      - Effective sample size weighting (cold-start smoothing)
      - Volatile regime detection (p75 instead of mean when stddev > threshold)
      - Quality degradation detection
      - Full replay capability (get_state/restore_state)

    EQT → RISKGATE CONTRACT (Phase 6 v3):
      Method: get_dynamic_slippage_estimate(symbol: str) -> float
      Returns: float in [0.0, max_slippage_estimate_pct]
      Cold-start: default_slippage_pct (5 bps)
      Symbol-scoped ONLY (no strategy/regime params)
      Update cadence: per-fill via record_fill()
      Output delta capped: max 1 bps per fill
      Deterministic: same observation sequence → same estimates
      Replay: get_state() / restore_state() methods preserve state

    Thread-safety: All access via RLock. Symbol isolation: each symbol
    has independent deque + estimate with no cross-contamination.
    """

    def __init__(self, config: ExecutionQualityConfig = None):
        """Initialize tracker.

        Args:
            config: ExecutionQualityConfig instance (defaults created if None)
        """
        self.config = config or ExecutionQualityConfig()
        self._lock = threading.RLock()

        # Global rolling window of all observations (audit trail)
        self._observations: Deque[FillQualityObservation] = deque(
            maxlen=self.config.max_observations
        )

        # Per-symbol rolling windows: deque of slippage_pct values
        # Symbol isolation: each symbol has its own independent deque
        self._by_symbol: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.config.window_size)
        )

        # Previous estimate for each symbol (used for rate-of-change cap)
        self._prev_estimate: Dict[str, float] = {}

        # Per-strategy and per-regime windows (for backward compat / analytics)
        self._by_strategy: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.config.window_size)
        )
        self._by_regime: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.config.window_size)
        )

        logger.info(
            "ExecutionQualityTracker initialized: window=%d, min_obs=%d, "
            "default_slippage=%.2f bps, max_estimate=%.2f bps, "
            "max_output_delta=%.2f bps, hysteresis=%.2f bps, "
            "volatile_stddev_threshold=%.2f bps",
            self.config.window_size, self.config.min_observations,
            self.config.default_slippage_pct * 10000,
            self.config.max_slippage_estimate_pct * 10000,
            self.config.max_output_delta_per_fill * 10000,
            self.config.hysteresis_threshold * 10000,
            self.config.volatile_stddev_threshold * 10000,
        )

    def record_fill(
        self,
        trigger_id: str,
        symbol: str,
        strategy_class: str,
        regime: str,
        side: str,
        expected_price: float,
        requested_price: float,
        filled_price: float,
        fee_usdt: float,
        fee_rate: float,
        is_maker: bool,
        signal_to_fill_ms: int,
        now_ms: int = None,
    ) -> None:
        """Record a fill quality observation and update estimate.

        This method:
          1. Creates FillQualityObservation from inputs
          2. Appends to global and per-symbol deques (oldest auto-evicted)
          3. Computes new slippage estimate for symbol with control loop logic:
             - Raw estimate from symbol's deque
             - Apply hysteresis (only update if diff > threshold)
             - Apply rate-of-change cap (delta clamped to +/- max_output_delta)
             - Apply absolute bounds [0.0, max_slippage_estimate_pct]
             - Store as _prev_estimate[symbol]

        Args:
            trigger_id: Unique trigger identifier
            symbol: Trading symbol (e.g. "BTCUSDT")
            strategy_class: Strategy class name
            regime: Market regime at entry (e.g. "BULL_TREND")
            side: "buy" or "sell"
            expected_price: Price at signal generation
            requested_price: Price sent to exchange
            filled_price: Actual fill price
            fee_usdt: Fee in USDT
            fee_rate: Fee rate applied (e.g. 0.0004 for 0.04%)
            is_maker: Whether fill was maker
            signal_to_fill_ms: Total pipeline latency (ms)
            now_ms: Timestamp override (default: current time in ms)

        Returns:
            None (side effect: updates deques and estimates)
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # Compute slippage: always absolute (direction-agnostic)
        if requested_price > 0:
            raw_slip = (filled_price - requested_price) / requested_price
            slippage_pct = abs(raw_slip)
        else:
            slippage_pct = 0.0

        obs = FillQualityObservation(
            timestamp_ms=now_ms,
            trigger_id=trigger_id,
            symbol=symbol,
            strategy_class=strategy_class,
            regime=regime,
            side=side,
            expected_price=expected_price,
            requested_price=requested_price,
            filled_price=filled_price,
            slippage_pct=slippage_pct,
            fee_usdt=fee_usdt,
            fee_rate=fee_rate,
            is_maker=is_maker,
            signal_to_fill_ms=signal_to_fill_ms,
        )

        with self._lock:
            # Append to global audit trail
            self._observations.append(obs)

            # Append to per-dimension deques (oldest auto-evicted by maxlen)
            self._by_symbol[symbol].append(slippage_pct)
            self._by_strategy[strategy_class].append(slippage_pct)
            if regime:
                self._by_regime[regime].append(slippage_pct)

            # Update estimate for this symbol with control loop logic
            self._update_symbol_estimate(symbol)

        logger.debug(
            "EQT: recorded fill %s %s slip=%.4f%% fee=%.4f%% latency=%dms",
            trigger_id, symbol, slippage_pct * 100, fee_rate * 100,
            signal_to_fill_ms,
        )

    def _update_symbol_estimate(self, symbol: str) -> None:
        """Update dynamic slippage estimate for a symbol with control loop logic.

        Called within _lock. Implements:
          1. Compute raw estimate from symbol's deque
          2. If n < min_observations: return default
          3. If stddev > volatile_threshold: use p75 instead of mean
          4. If n < 2*min_observations: blend toward default (effective sample size)
          5. Apply hysteresis: only update if raw differs from prev by > threshold
          6. Apply rate-of-change cap: delta clamped to +/- max_output_delta_per_fill
          7. Apply absolute bounds [0.0, max_slippage_estimate_pct]
          8. Store as _prev_estimate[symbol]

        Args:
            symbol: Trading symbol (e.g. "BTCUSDT")

        Returns:
            None (side effect: updates _prev_estimate[symbol])
        """
        cfg = self.config
        values = list(self._by_symbol[symbol])
        n = len(values)

        # Cold-start: insufficient data
        if n < cfg.min_observations:
            self._prev_estimate[symbol] = cfg.default_slippage_pct
            return

        # Compute raw estimate with volatile regime detection
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        stddev = math.sqrt(variance)

        # Volatile regime: use p75 instead of mean
        if stddev > cfg.volatile_stddev_threshold:
            sorted_vals = sorted(values)
            raw_estimate = sorted_vals[int(n * 0.75)]
        else:
            raw_estimate = mean

        # Effective sample size weighting: blend toward default when sparse
        if n < cfg.min_observations * 2:
            weight = n / (cfg.min_observations * 2)
            raw_estimate = (
                cfg.default_slippage_pct * (1.0 - weight) +
                raw_estimate * weight
            )

        # Hysteresis: only update if raw differs from previous by > threshold
        prev = self._prev_estimate.get(symbol, cfg.default_slippage_pct)
        if abs(raw_estimate - prev) < cfg.hysteresis_threshold:
            return

        # Rate-of-change cap: clamp delta to max_output_delta_per_fill
        delta = raw_estimate - prev
        delta_clamped = self._clamp(
            delta,
            -cfg.max_output_delta_per_fill,
            cfg.max_output_delta_per_fill
        )
        new_estimate = prev + delta_clamped

        # Absolute bounds
        new_estimate = self._clamp(new_estimate, 0.0, cfg.max_slippage_estimate_pct)

        self._prev_estimate[symbol] = new_estimate

    # ── Queries ──────────────────────────────────────────────

    def get_dynamic_slippage_estimate(self, symbol: str) -> float:
        """Get current slippage estimate for RiskGate EV gate.

        CONTRACT (Phase 6 v3):
          - Input: symbol (string, e.g. "BTCUSDT")
          - Returns: float in [0.0, max_slippage_estimate_pct]
          - Cold-start: returns default_slippage_pct when < min_observations
          - Bounded: output always clamped to max_slippage_estimate_pct
          - Deterministic: same observation sequence → same output
          - Symbol-scoped: NO strategy/regime params (those are internal)
          - Rate-of-change bounded per fill
          - Replay-safe: persisted via get_state/restore_state

        Args:
            symbol: Trading symbol (e.g. "BTCUSDT")

        Returns:
            float in [0.0, max_slippage_estimate_pct]
        """
        cfg = self.config

        with self._lock:
            if symbol not in self._prev_estimate:
                return cfg.default_slippage_pct

            estimate = self._prev_estimate[symbol]
            # Double-check absolute bounds (safety net)
            return self._clamp(estimate, 0.0, cfg.max_slippage_estimate_pct)

    def get_quality_stats(
        self, dimension: str = "global", key: str = None,
    ) -> Optional[QualityStats]:
        """Get quality statistics for a dimension slice.

        Computes mean, median, p95, max, std, maker ratio, latency,
        and degradation flag from observations.

        Args:
            dimension: "global", "symbol", "strategy", or "regime"
            key: Key within dimension (e.g. "BTCUSDT" for symbol)

        Returns:
            QualityStats or None if insufficient data
        """
        with self._lock:
            if dimension == "symbol" and key:
                values = list(self._by_symbol.get(key, []))
            elif dimension == "strategy" and key:
                values = list(self._by_strategy.get(key, []))
            elif dimension == "regime" and key:
                values = list(self._by_regime.get(key, []))
            else:
                values = [o.slippage_pct for o in self._observations]

            if len(values) < self.config.min_observations:
                return None

            return self._compute_stats(values, dimension, key)

    def get_all_stats(self) -> dict:
        """Get statistics across all dimensions.

        Returns:
            dict with keys "global", "by_symbol", "by_strategy", "by_regime"
            Each containing QualityStats for available keys
        """
        with self._lock:
            result = {}

            # Global
            global_vals = [o.slippage_pct for o in self._observations]
            if len(global_vals) >= self.config.min_observations:
                result["global"] = self._compute_stats(global_vals, "global", None)

            # Per symbol
            result["by_symbol"] = {}
            for sym, vals in self._by_symbol.items():
                v = list(vals)
                if len(v) >= self.config.min_observations:
                    result["by_symbol"][sym] = self._compute_stats(v, "symbol", sym)

            # Per strategy
            result["by_strategy"] = {}
            for sc, vals in self._by_strategy.items():
                v = list(vals)
                if len(v) >= self.config.min_observations:
                    result["by_strategy"][sc] = self._compute_stats(v, "strategy", sc)

            # Per regime
            result["by_regime"] = {}
            for reg, vals in self._by_regime.items():
                v = list(vals)
                if len(v) >= self.config.min_observations:
                    result["by_regime"][reg] = self._compute_stats(v, "regime", reg)

            return result

    def snapshot(self) -> dict:
        """Full audit snapshot for logging/debugging.

        Returns:
            dict with observation counts, tracked dimensions, and contract info
        """
        with self._lock:
            return {
                "total_observations": len(self._observations),
                "symbols_tracked": list(self._by_symbol.keys()),
                "strategies_tracked": list(self._by_strategy.keys()),
                "regimes_tracked": list(self._by_regime.keys()),
                "symbol_estimates": dict(self._prev_estimate),
                "contract": {
                    "output_range": [0.0, self.config.max_slippage_estimate_pct],
                    "cold_start_value": self.config.default_slippage_pct,
                    "min_observations": self.config.min_observations,
                    "max_output_delta_per_fill": self.config.max_output_delta_per_fill,
                    "hysteresis_threshold": self.config.hysteresis_threshold,
                },
            }

    def get_state(self) -> dict:
        """Get full state for replay persistence.

        Returns all observations and per-symbol estimates in a serializable dict.
        Can be replayed via restore_state() to recover exact same state.

        Returns:
            dict with "observations" (list) and "prev_estimates" (dict)
        """
        with self._lock:
            return {
                "observations": [o.to_dict() for o in self._observations],
                "prev_estimates": dict(self._prev_estimate),
            }

    def restore_state(self, state: dict) -> None:
        """Restore state from dict (e.g., after replay or checkpoint load).

        Clears current deques and re-populates from observations,
        then restores per-symbol estimates.

        Args:
            state: dict from get_state()

        Returns:
            None (side effect: clears and repopulates deques)
        """
        with self._lock:
            # Clear all deques
            self._observations.clear()
            self._by_symbol.clear()
            self._by_strategy.clear()
            self._by_regime.clear()
            self._prev_estimate.clear()

            # Restore observations by replaying record_fill logic
            for obs_dict in state.get("observations", []):
                # Reconstruct FillQualityObservation
                obs = FillQualityObservation(
                    timestamp_ms=obs_dict["timestamp_ms"],
                    trigger_id=obs_dict["trigger_id"],
                    symbol=obs_dict["symbol"],
                    strategy_class=obs_dict["strategy_class"],
                    regime=obs_dict["regime"],
                    side=obs_dict["side"],
                    expected_price=obs_dict["expected_price"],
                    requested_price=obs_dict["requested_price"],
                    filled_price=obs_dict["filled_price"],
                    slippage_pct=obs_dict["slippage_pct"],
                    fee_usdt=obs_dict["fee_usdt"],
                    fee_rate=obs_dict["fee_rate"],
                    is_maker=obs_dict["is_maker"],
                    signal_to_fill_ms=obs_dict["signal_to_fill_ms"],
                )

                # Append to deques (no estimate update yet)
                self._observations.append(obs)
                self._by_symbol[obs.symbol].append(obs.slippage_pct)
                self._by_strategy[obs.strategy_class].append(obs.slippage_pct)
                if obs.regime:
                    self._by_regime[obs.regime].append(obs.slippage_pct)

            # Restore per-symbol estimates
            self._prev_estimate = dict(state.get("prev_estimates", {}))

    # ── Internal ─────────────────────────────────────────────

    def _compute_stats(
        self, values: List[float], dimension: str, key: str,
    ) -> QualityStats:
        """Compute statistics from a list of slippage values.

        Args:
            values: list of slippage_pct floats
            dimension: "global", "symbol", "strategy", or "regime"
            key: dimension key (symbol name, etc.)

        Returns:
            QualityStats dataclass
        """
        n = len(values)
        sorted_v = sorted(values)
        mean = sum(sorted_v) / n
        median = sorted_v[n // 2]

        # Standard deviation
        variance = sum((v - mean) ** 2 for v in sorted_v) / n
        std = math.sqrt(variance)

        # Percentiles
        p95 = sorted_v[int(n * 0.95)] if n >= 20 else sorted_v[-1]
        max_v = sorted_v[-1]

        # Maker ratio and fee stats from full observations (matched by dimension/key)
        maker_count = 0
        fee_rates = []
        latencies = []
        with self._lock:
            for obs in self._observations:
                match = False
                if dimension == "symbol" and obs.symbol == key:
                    match = True
                elif dimension == "strategy" and obs.strategy_class == key:
                    match = True
                elif dimension == "regime" and obs.regime == key:
                    match = True
                elif dimension == "global":
                    match = True
                if match:
                    if obs.is_maker:
                        maker_count += 1
                    fee_rates.append(obs.fee_rate)
                    latencies.append(obs.signal_to_fill_ms)

        obs_count = max(len(fee_rates), 1)
        is_degraded = mean > (
            self.config.default_slippage_pct * self.config.degradation_threshold
        )

        return QualityStats(
            count=n,
            mean_slippage_pct=mean,
            median_slippage_pct=median,
            p95_slippage_pct=p95,
            max_slippage_pct=max_v,
            std_slippage_pct=std,
            mean_fee_rate=sum(fee_rates) / obs_count if fee_rates else 0.0,
            maker_ratio=maker_count / obs_count,
            mean_signal_to_fill_ms=(
                sum(latencies) / len(latencies) if latencies else 0.0
            ),
            is_degraded=is_degraded,
        )

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        """Clamp a value to [min_val, max_val] range.

        Args:
            value: value to clamp
            min_val: minimum allowed value
            max_val: maximum allowed value

        Returns:
            clamped value
        """
        return max(min_val, min(max_val, value))
