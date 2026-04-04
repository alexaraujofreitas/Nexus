# ============================================================
# NEXUS TRADER — MIL Phase 4A: Enhanced Open Interest
#
# OI delta tracking (1h/4h/24h), OI/volume ratio, and
# liquidation proximity scoring.
#
# Architecture:
#   1. record_oi() / record_volume() called from CoinglassAgent
#      (QThread context — blocking HTTP is the established pattern).
#   2. All history stored per-symbol with deque (maxlen capped).
#   3. enhance_oi_result() reads ONLY from in-memory history.
#   4. All MIL signals carry timestamps + staleness enforcement.
#   5. MIL influence is capped at MIL_INFLUENCE_CAP (default 0.30).
#
# Backtest isolation: These signals enter via OrchestratorEngine
# only. ConfluenceScorer.score(technical_only=True) blocks them.
# ============================================================
from __future__ import annotations

import logging
import time
import threading
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── History Constants ────────────────────────────────────────
# We keep enough samples for 24h delta at 5-min cadence = 288 entries
_OI_HISTORY_MAX = 300
_VOLUME_HISTORY_MAX = 50  # ~4h of 5-min samples

# ── Liquidation Proximity ────────────────────────────────────
_LIQ_PROXIMITY_DEFAULT_PCT = 2.0  # 2% of price

# ── Staleness Constants ──────────────────────────────────────
_MAX_STALENESS_S = 300.0    # Discard MIL OI data older than 5 minutes
                            # (CoinglassAgent polls every 300s with 5-min TTL)

# ── Influence Cap ────────────────────────────────────────────
# Maximum influence MIL OI metadata can have on the effective
# signal when used downstream. The cap scales MIL-derived adjustments
# so they never exceed this fraction of total signal magnitude.
MIL_OI_INFLUENCE_CAP = 0.30  # 30% maximum contribution


class OIEnhancer:
    """
    Stateful OI enhancer for CoinglassAgent.

    Maintains:
    - Per-symbol OI history deques for multi-window delta calculation
    - Volume estimates for OI/volume ratio
    - Liquidation zone tracking (simplified: uses OI acceleration as proxy)
    - Timestamp + staleness on every signal

    Thread-safe: all state accessed under _lock (threading.Lock).
    The Lock (not RLock) is used because no method calls another
    lock-holding method — simpler and avoids accidental reentrancy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # symbol -> deque of (timestamp, oi_usd) for delta calculations
        self._oi_history: dict[str, deque] = {}
        # symbol -> deque of (timestamp, volume_usd) for OI/volume ratio
        self._volume_history: dict[str, deque] = {}
        # symbol -> float (wall-clock ts of last enhance call)
        self._last_enhance_ts: dict[str, float] = {}

    def is_enabled(self) -> bool:
        """Check if MIL OI enhancement is enabled."""
        try:
            from config.settings import settings
            global_enabled = settings.get("mil.global_enabled", False)
            oi_enabled = settings.get("agents.oi_enhanced", False)
            return bool(global_enabled and oi_enabled)
        except Exception:
            return False

    def record_oi(self, symbol: str, oi_usd: float, ts: Optional[float] = None) -> None:
        """Record an OI observation into the history deque."""
        now = ts or time.time()
        with self._lock:
            hist = self._oi_history.setdefault(
                symbol, deque(maxlen=_OI_HISTORY_MAX)
            )
            hist.append((now, oi_usd))

    def record_volume(self, symbol: str, volume_usd: float, ts: Optional[float] = None) -> None:
        """Record a volume observation for OI/volume ratio."""
        now = ts or time.time()
        with self._lock:
            hist = self._volume_history.setdefault(
                symbol, deque(maxlen=_VOLUME_HISTORY_MAX)
            )
            hist.append((now, volume_usd))

    def compute_oi_deltas(self, symbol: str, current_oi: float) -> dict:
        """
        Compute OI percent change over 1h, 4h, and 24h windows.

        Returns:
            oi_delta_1h: float (% change)
            oi_delta_4h: float (% change)
            oi_delta_24h: float (% change)
            oi_acceleration: float (delta_1h - delta_4h, positive = accelerating)
        """
        now = time.time()
        windows = {
            "1h": 3600.0,
            "4h": 14400.0,
            "24h": 86400.0,
        }
        deltas = {}

        with self._lock:
            hist = self._oi_history.get(symbol, deque())
            if not hist:
                return {
                    "oi_delta_1h": 0.0,
                    "oi_delta_4h": 0.0,
                    "oi_delta_24h": 0.0,
                    "oi_acceleration": 0.0,
                }

            for window_name, window_s in windows.items():
                target_ts = now - window_s
                # Find closest entry to target timestamp
                candidates = [(abs(t - target_ts), oi) for t, oi in hist if t <= now]
                if candidates:
                    _, old_oi = min(candidates, key=lambda x: x[0])
                    if old_oi and old_oi != 0:
                        delta = ((current_oi - old_oi) / abs(old_oi)) * 100.0
                    else:
                        delta = 0.0
                else:
                    delta = 0.0
                deltas[f"oi_delta_{window_name}"] = round(delta, 4)

        # Acceleration: 1h delta vs 4h delta (positive = short-term accelerating)
        d1h = deltas.get("oi_delta_1h", 0.0)
        d4h = deltas.get("oi_delta_4h", 0.0)
        deltas["oi_acceleration"] = round(d1h - d4h, 4)

        return deltas

    def compute_oi_volume_ratio(self, symbol: str, current_oi: float) -> float:
        """
        Compute OI/Volume ratio as a positioning indicator.

        High OI/Volume = crowded positioning (potential for liquidations).
        Low OI/Volume = fresh interest (trend confirming).

        Returns ratio or 0.0 if insufficient volume data.
        """
        with self._lock:
            vol_hist = self._volume_history.get(symbol, deque())
            if not vol_hist or len(vol_hist) < 2:
                return 0.0

            # Average recent volume
            avg_volume = sum(v for _, v in vol_hist) / len(vol_hist)
            if avg_volume <= 0:
                return 0.0

            return round(current_oi / avg_volume, 4)

    def compute_liquidation_proximity(
        self,
        symbol: str,
        current_oi: float,
    ) -> dict:
        """
        Estimate liquidation proximity using OI acceleration as a proxy.

        When OI is accelerating rapidly (high 1h delta), it suggests
        leveraged positions are building and liquidation risk is elevated.

        Full liquidation heatmap integration is Phase 4C scope.
        This Phase 4A implementation uses OI dynamics as a proxy signal.

        Returns:
            liquidation_proximity_score: float [0.0, 1.0]
            liquidation_risk_level: str (low/medium/high)
        """
        deltas = self.compute_oi_deltas(symbol, current_oi)
        accel = abs(deltas.get("oi_acceleration", 0.0))
        delta_1h = abs(deltas.get("oi_delta_1h", 0.0))

        # Scoring: combine acceleration and 1h delta magnitude
        # Normalized to [0, 1] with reasonable crypto thresholds
        accel_score = min(1.0, accel / 10.0)    # 10% acceleration = max
        delta_score = min(1.0, delta_1h / 15.0)  # 15% 1h delta = max
        score = round(0.6 * accel_score + 0.4 * delta_score, 4)

        if score >= 0.7:
            level = "high"
        elif score >= 0.35:
            level = "medium"
        else:
            level = "low"

        return {
            "liquidation_proximity_score": score,
            "liquidation_risk_level": level,
        }

    def compute_influence_factor(
        self,
        deltas: dict,
        liq_score: float,
    ) -> float:
        """
        Compute MIL OI influence factor, capped at MIL_OI_INFLUENCE_CAP.

        Higher influence when:
        - OI acceleration is extreme (rapid position building)
        - Liquidation proximity is high
        Lower influence when data is sparse.

        Returns: float [0.0, MIL_OI_INFLUENCE_CAP]
        """
        accel = abs(deltas.get("oi_acceleration", 0.0))
        # Acceleration extremity [0, 1]
        accel_extremity = min(1.0, accel / 10.0)
        # Combine with liquidation score
        raw_influence = 0.5 * accel_extremity + 0.5 * liq_score
        return round(min(raw_influence * MIL_OI_INFLUENCE_CAP, MIL_OI_INFLUENCE_CAP), 4)

    def enhance_oi_result(
        self,
        symbol: str,
        base_result: dict,
    ) -> dict:
        """
        Enrich a CoinglassAgent result dict with MIL OI metadata.

        Called by CoinglassAgent._build_result() when enhancement is enabled.
        Returns the base_result with additional mil_* keys.

        Staleness enforcement: if last enhancement is older than
        _MAX_STALENESS_S, signal is marked stale.

        Influence cap: mil_oi_influence_factor is clamped to
        [0, MIL_OI_INFLUENCE_CAP] so downstream consumers can bound
        MIL's contribution.

        Fail-open: if any computation fails, original data is returned
        unchanged with mil_enhanced=False.
        """
        try:
            oi_usd = base_result.get("raw_oi_usd", 0.0)
            if oi_usd <= 0:
                base_result["mil_enhanced"] = False
                return base_result

            # Record this observation
            self.record_oi(symbol, oi_usd)

            # Compute deltas
            deltas = self.compute_oi_deltas(symbol, oi_usd)

            # OI/volume ratio
            oi_vol_ratio = self.compute_oi_volume_ratio(symbol, oi_usd)

            # Liquidation proximity
            liq = self.compute_liquidation_proximity(symbol, oi_usd)

            # Influence factor (capped)
            influence = self.compute_influence_factor(
                deltas, liq["liquidation_proximity_score"]
            )

            # Timestamp for staleness tracking
            signal_ts = time.time()
            with self._lock:
                prev_ts = self._last_enhance_ts.get(symbol, signal_ts)
                self._last_enhance_ts[symbol] = signal_ts
            data_age_s = signal_ts - prev_ts

            base_result["mil_enhanced"] = True
            base_result["mil_oi_delta_1h"] = deltas["oi_delta_1h"]
            base_result["mil_oi_delta_4h"] = deltas["oi_delta_4h"]
            base_result["mil_oi_delta_24h"] = deltas["oi_delta_24h"]
            base_result["mil_oi_acceleration"] = deltas["oi_acceleration"]
            base_result["mil_oi_volume_ratio"] = oi_vol_ratio
            base_result["mil_liquidation_proximity"] = liq["liquidation_proximity_score"]
            base_result["mil_liquidation_risk"] = liq["liquidation_risk_level"]
            base_result["mil_oi_influence_factor"] = influence
            base_result["mil_oi_influence_cap"] = MIL_OI_INFLUENCE_CAP
            base_result["mil_signal_ts"] = signal_ts
            base_result["mil_data_age_s"] = round(data_age_s, 1)
            base_result["mil_stale"] = data_age_s > _MAX_STALENESS_S

            return base_result

        except Exception as exc:
            logger.debug(
                "MIL OIEnhancer: enhance failed for %s (fail-open): %s",
                symbol, exc,
            )
            base_result["mil_enhanced"] = False
            return base_result

    def get_diagnostics(self) -> dict:
        """Return MIL OI diagnostics for pipeline dashboard."""
        with self._lock:
            return {
                "symbols_tracked": len(self._oi_history),
                "history_sizes": {
                    sym: len(hist)
                    for sym, hist in self._oi_history.items()
                },
                "max_staleness_s": _MAX_STALENESS_S,
                "influence_cap": MIL_OI_INFLUENCE_CAP,
            }


# ── Module-level singleton ─────────────────────────────────
_enhancer: Optional[OIEnhancer] = None
_init_lock = threading.Lock()


def get_oi_enhancer() -> OIEnhancer:
    """Return (or create) the OIEnhancer singleton."""
    global _enhancer
    if _enhancer is None:
        with _init_lock:
            if _enhancer is None:
                _enhancer = OIEnhancer()
    return _enhancer
