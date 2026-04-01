"""
FeatureTransitionDetector — HMM-independent event detection for regime phase shifts.

Phase 2c: Detects 3 transition EVENTS using raw indicator features only.
  1. Compression → Expansion (BB/ATR contraction then release)
  2. Range → Breakout (horizontal consolidation then boundary break)
  3. Pullback → Continuation (trend pullback exhaustion then resumption)

Architecture: 4h (trend gate) → 1h (transition detection) → 30m (execution entry)

ZERO dependency on HMM, RegimeClassifier, or regime_probs.
Only consumes DataFrames with calculate_scan_mode() columns.

Config gate: transition_detector_v2.enabled (default False)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Output dataclass
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TransitionEvent:
    """Immutable output from FeatureTransitionDetector."""
    event_type: str          # compression_expansion, range_breakout, pullback_continuation
    direction: str           # "long" or "short"
    confidence: float        # 0.35–0.85
    detection_tf: str        # "1h" always
    bar_idx: int             # Index into the 1h DataFrame
    bar_timestamp: object    # Timestamp of the detection bar
    features_snapshot: dict  # Frozen feature values at detection
    expires_bars: int = 3    # 1h bars until expiry


# ════════════════════════════════════════════════════════════════════
# Default parameters (overridable via config)
# ════════════════════════════════════════════════════════════════════

_DEFAULTS = {
    # --- Compression → Expansion ---
    "compression_expansion.bb_ratio_threshold": 0.75,      # Adjusted per review (was 0.70)
    "compression_expansion.atr_ratio_threshold": 0.75,
    "compression_expansion.compression_min_bars": 3,
    "compression_expansion.expansion_bb_ratio": 0.85,
    "compression_expansion.volume_mult_min": 1.3,          # Adjusted per review (was 1.5)
    "compression_expansion.cooldown_bars": 8,
    "compression_expansion.bb_rolling_window": 50,

    # --- Range → Breakout ---
    "range_breakout.range_lookback": 20,
    "range_breakout.range_max_pct": 6.0,                   # Tightened per review (was 8.0)
    "range_breakout.range_min_bars": 10,
    "range_breakout.adx_max_ranging": 25,
    "range_breakout.volume_mult_min": 1.5,
    "range_breakout.require_confirmation_bar": True,
    "range_breakout.cooldown_bars": 10,

    # --- Pullback → Continuation ---
    "pullback_continuation.ema_prox_atr": 1.0,
    "pullback_continuation.rsi_pullback_bull": 45,
    "pullback_continuation.rsi_pullback_bear": 55,
    "pullback_continuation.swing_bars": 5,
    "pullback_continuation.volume_mult_min": 1.2,
    "pullback_continuation.htf_adx_min": 20,
    "pullback_continuation.cooldown_bars": 6,
}


def _get(params: dict, key: str):
    """Read param with fallback to _DEFAULTS."""
    return params.get(key, _DEFAULTS.get(key))


# ════════════════════════════════════════════════════════════════════
# Detector class
# ════════════════════════════════════════════════════════════════════

class FeatureTransitionDetector:
    """
    Per-symbol event detector. One instance per symbol.
    Thread-safe: each caller should create its own instance.

    Usage (bar-by-bar):
        det = FeatureTransitionDetector(params={...})
        for i in range(warmup, len(df_1h)):
            events = det.detect(df_1h, i, df_4h=df_4h, idx_4h=j)
    """

    def __init__(self, params: dict | None = None):
        self._p = params or {}

        # --- Compression state ---
        self._compression_count: int = 0
        self._min_bb_ratio_during_compression: float = 1.0

        # --- Range state ---
        self._range_bar_count: int = 0
        self._pending_breakout: Optional[dict] = None  # awaiting confirmation bar

        # --- Pullback state ---
        self._in_pullback: bool = False
        self._pullback_direction: str = ""  # "bull" or "bear"

        # --- Cooldowns ---
        self._cooldowns: dict[str, int] = {}

        # --- Active events ---
        self._active_events: list[TransitionEvent] = []

    # ────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────

    def detect(
        self,
        df_1h: pd.DataFrame,
        loc: int,
        df_4h: pd.DataFrame | None = None,
        idx_4h: int | None = None,
    ) -> list[TransitionEvent]:
        """
        Called once per 1h bar per symbol.

        Parameters
        ----------
        df_1h : DataFrame with scan-mode columns (1h)
        loc   : Current bar index into df_1h
        df_4h : Optional 4h DataFrame (for pullback trend gate)
        idx_4h: Current bar index into df_4h (if provided)

        Returns list of newly fired TransitionEvents (0, 1, or 2 max).
        """
        # Decrement cooldowns
        for k in list(self._cooldowns):
            self._cooldowns[k] -= 1
            if self._cooldowns[k] <= 0:
                del self._cooldowns[k]

        # Expire old events
        self._active_events = [e for e in self._active_events if e.expires_bars > 0]
        self._active_events = [
            TransitionEvent(
                event_type=e.event_type, direction=e.direction, confidence=e.confidence,
                detection_tf=e.detection_tf, bar_idx=e.bar_idx, bar_timestamp=e.bar_timestamp,
                features_snapshot=e.features_snapshot, expires_bars=e.expires_bars - 1,
            )
            for e in self._active_events
        ]

        new_events = []

        # Run detectors
        ev = self._check_compression_expansion(df_1h, loc)
        if ev is not None:
            new_events.append(ev)

        ev = self._check_range_breakout(df_1h, loc)
        if ev is not None:
            new_events.append(ev)

        ev = self._check_pullback_continuation(df_1h, loc, df_4h, idx_4h)
        if ev is not None:
            new_events.append(ev)

        # Direction conflict: if two events fire with opposite directions, discard both
        if len(new_events) == 2:
            if new_events[0].direction != new_events[1].direction:
                logger.debug("TransitionDetectorV2: direction conflict, discarding both")
                new_events = []

        # Max 2 active events
        for ev in new_events:
            self._active_events.append(ev)
        if len(self._active_events) > 2:
            self._active_events.sort(key=lambda e: e.confidence)
            self._active_events = self._active_events[-2:]

        return new_events

    def get_active_events(self) -> list[TransitionEvent]:
        return list(self._active_events)

    def reset(self):
        self._compression_count = 0
        self._min_bb_ratio_during_compression = 1.0
        self._range_bar_count = 0
        self._pending_breakout = None
        self._in_pullback = False
        self._pullback_direction = ""
        self._cooldowns.clear()
        self._active_events.clear()

    # ────────────────────────────────────────────────────────────────
    # Helper: safe column read
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _col(df, loc, name, default=np.nan):
        if name not in df.columns:
            return default
        v = df.iloc[loc][name]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)

    @staticmethod
    def _rolling_mean(df, loc, col, window, min_periods=None):
        """Compute rolling mean ending at loc (inclusive)."""
        if col not in df.columns:
            return np.nan
        if min_periods is None:
            min_periods = window
        start = max(0, loc - window + 1)
        vals = df[col].iloc[start:loc + 1].values.astype(float)
        vals = vals[~np.isnan(vals)]
        if len(vals) < min_periods:
            return np.nan
        return float(np.mean(vals))

    # ────────────────────────────────────────────────────────────────
    # Type 1: Compression → Expansion
    # ────────────────────────────────────────────────────────────────

    def _check_compression_expansion(self, df, loc) -> Optional[TransitionEvent]:
        if "compression_expansion" in self._cooldowns:
            return None
        if loc < 60:
            return None

        bb_threshold = _get(self._p, "compression_expansion.bb_ratio_threshold")
        atr_threshold = _get(self._p, "compression_expansion.atr_ratio_threshold")
        min_bars = _get(self._p, "compression_expansion.compression_min_bars")
        exp_bb = _get(self._p, "compression_expansion.expansion_bb_ratio")
        vol_min = _get(self._p, "compression_expansion.volume_mult_min")
        cooldown = _get(self._p, "compression_expansion.cooldown_bars")
        bb_window = _get(self._p, "compression_expansion.bb_rolling_window")

        # Current values
        bb_w = self._col(df, loc, "bb_width")
        atr_now = self._col(df, loc, "atr_14")
        if np.isnan(bb_w) or np.isnan(atr_now):
            return None

        # Rolling means
        bb_mean = self._rolling_mean(df, loc, "bb_width", bb_window)
        atr_mean = self._rolling_mean(df, loc, "atr_14", bb_window)
        if np.isnan(bb_mean) or np.isnan(atr_mean) or bb_mean == 0 or atr_mean == 0:
            return None

        bb_ratio = bb_w / bb_mean
        atr_ratio = atr_now / atr_mean

        # Phase A: compression tracking
        if bb_ratio <= bb_threshold and atr_ratio <= atr_threshold:
            self._compression_count += 1
            self._min_bb_ratio_during_compression = min(
                self._min_bb_ratio_during_compression, bb_ratio
            )
            return None  # still compressing, no event yet

        # Phase B: expansion trigger (only if was compressed)
        if self._compression_count >= min_bars:
            # Check expansion conditions
            bb_prev = self._col(df, loc - 1, "bb_width") if loc > 0 else np.nan
            atr_prev = self._col(df, loc - 1, "atr_14") if loc > 0 else np.nan

            bb_expanding = bb_ratio >= exp_bb
            bb_delta_positive = not np.isnan(bb_prev) and bb_w > bb_prev
            atr_rising = not np.isnan(atr_prev) and atr_now > atr_prev

            vol_now = self._col(df, loc, "volume")
            vol_mean = self._rolling_mean(df, loc, "volume", 20)
            vol_ratio = vol_now / vol_mean if not np.isnan(vol_mean) and vol_mean > 0 else 0
            vol_ok = vol_ratio >= vol_min

            if bb_expanding and bb_delta_positive and atr_rising and vol_ok:
                # Phase C: direction
                close = self._col(df, loc, "close")
                ema_20 = self._col(df, loc, "ema_20")
                ema_9 = self._col(df, loc, "ema_9")
                ema_21 = self._col(df, loc, "ema_21")

                if np.isnan(close) or np.isnan(ema_20):
                    self._compression_count = 0
                    self._min_bb_ratio_during_compression = 1.0
                    return None

                if close > ema_20 and (np.isnan(ema_9) or np.isnan(ema_21) or ema_9 > ema_21):
                    direction = "long"
                elif close < ema_20 and (np.isnan(ema_9) or np.isnan(ema_21) or ema_9 < ema_21):
                    direction = "short"
                else:
                    # Neutral — discard
                    self._compression_count = 0
                    self._min_bb_ratio_during_compression = 1.0
                    return None

                # Confidence
                depth_bonus = min(0.15, (1.0 - self._min_bb_ratio_during_compression) * 0.5)
                vol_bonus = min(0.15, (vol_ratio - vol_min) * 0.15 / 0.5) if vol_ratio > vol_min else 0
                adx = self._col(df, loc, "adx_14", 0)
                adx_bonus = 0.10 if adx > 20 else 0
                dir_bonus = 0.10  # always present if direction was assigned
                confidence = min(0.85, 0.40 + depth_bonus + vol_bonus + adx_bonus + dir_bonus)

                snapshot = {
                    "bb_ratio": round(bb_ratio, 4), "atr_ratio": round(atr_ratio, 4),
                    "compression_bars": self._compression_count,
                    "min_bb_ratio": round(self._min_bb_ratio_during_compression, 4),
                    "vol_ratio": round(vol_ratio, 2), "adx": round(adx, 1),
                }

                self._cooldowns["compression_expansion"] = cooldown
                self._compression_count = 0
                self._min_bb_ratio_during_compression = 1.0

                return TransitionEvent(
                    event_type="compression_expansion",
                    direction=direction,
                    confidence=round(confidence, 3),
                    detection_tf="1h",
                    bar_idx=loc,
                    bar_timestamp=df.index[loc] if loc < len(df) else None,
                    features_snapshot=snapshot,
                )

        # Not expanding or conditions not met — reset compression state
        self._compression_count = 0
        self._min_bb_ratio_during_compression = 1.0
        return None

    # ────────────────────────────────────────────────────────────────
    # Type 2: Range → Breakout
    # ────────────────────────────────────────────────────────────────

    def _check_range_breakout(self, df, loc) -> Optional[TransitionEvent]:
        if "range_breakout" in self._cooldowns:
            return None
        if loc < 30:
            return None

        lookback = int(_get(self._p, "range_breakout.range_lookback"))
        max_pct = _get(self._p, "range_breakout.range_max_pct")
        min_bars = int(_get(self._p, "range_breakout.range_min_bars"))
        adx_max = _get(self._p, "range_breakout.adx_max_ranging")
        vol_min = _get(self._p, "range_breakout.volume_mult_min")
        confirm = _get(self._p, "range_breakout.require_confirmation_bar")
        cooldown = int(_get(self._p, "range_breakout.cooldown_bars"))

        close = self._col(df, loc, "close")
        adx = self._col(df, loc, "adx_14", 50)  # default high to skip if missing
        if np.isnan(close):
            return None

        # Compute range high/low over lookback ending at PREVIOUS bar
        if loc < lookback + 1:
            return None
        range_start = loc - lookback
        range_end = loc  # exclusive — range computed over bars [start, loc-1]
        highs = df["high"].iloc[range_start:range_end].values.astype(float)
        lows = df["low"].iloc[range_start:range_end].values.astype(float)
        highs = highs[~np.isnan(highs)]
        lows = lows[~np.isnan(lows)]
        if len(highs) == 0 or len(lows) == 0:
            return None

        range_high = float(np.max(highs))
        range_low = float(np.min(lows))
        mid = (range_high + range_low) / 2
        if mid <= 0:
            return None
        range_pct = (range_high - range_low) / mid * 100

        # Check if pending breakout needs confirmation
        if self._pending_breakout is not None:
            pb = self._pending_breakout
            self._pending_breakout = None

            if pb["direction"] == "long" and close > pb["range_high"]:
                # Confirmed
                return self._emit_breakout(df, loc, pb)
            elif pb["direction"] == "short" and close < pb["range_low"]:
                return self._emit_breakout(df, loc, pb)
            else:
                # Failed confirmation — false breakout
                return None

        # Phase A: range identification
        if range_pct <= max_pct and adx < adx_max:
            self._range_bar_count += 1
        else:
            self._range_bar_count = 0
            return None

        if self._range_bar_count < min_bars:
            return None

        # Phase B: breakout trigger
        vol_now = self._col(df, loc, "volume")
        vol_mean = self._rolling_mean(df, loc, "volume", 20)
        vol_ratio = vol_now / vol_mean if not np.isnan(vol_mean) and vol_mean > 0 else 0

        if vol_ratio < vol_min:
            return None

        direction = None
        if close > range_high:
            direction = "long"
        elif close < range_low:
            direction = "short"

        if direction is None:
            return None

        # Momentum confirmation (at least 1 of 3)
        rsi = self._col(df, loc, "rsi_14", 50)
        macd = self._col(df, loc, "macd", 0)
        macd_sig = self._col(df, loc, "macd_signal", 0)
        adx_prev = self._col(df, loc - 1, "adx_14", 0) if loc > 0 else 0

        mom_count = 0
        if direction == "long":
            if rsi > 55:
                mom_count += 1
            if macd > macd_sig and macd > 0:
                mom_count += 1
        else:
            if rsi < 45:
                mom_count += 1
            if macd < macd_sig and macd < 0:
                mom_count += 1
        if adx > 18 and adx > adx_prev:
            mom_count += 1

        if mom_count < 1:
            return None

        # Package breakout data
        breakout_data = {
            "direction": direction,
            "range_high": range_high,
            "range_low": range_low,
            "range_pct": range_pct,
            "range_bars": self._range_bar_count,
            "vol_ratio": vol_ratio,
            "mom_count": mom_count,
            "rsi": rsi,
            "adx": adx,
        }

        if confirm:
            # Defer to next bar for confirmation
            self._pending_breakout = breakout_data
            return None
        else:
            return self._emit_breakout(df, loc, breakout_data)

    def _emit_breakout(self, df, loc, data) -> TransitionEvent:
        cooldown = int(_get(self._p, "range_breakout.cooldown_bars"))

        # Confidence
        dur_bonus = min(0.15, (data["range_bars"] - 10) / 40 * 0.15)
        vol_bonus = min(0.15, (data["vol_ratio"] - 1.5) * 0.15 / 0.5) if data["vol_ratio"] > 1.5 else 0
        mom_bonus = min(0.20, data["mom_count"] * 0.10)
        confidence = min(0.85, 0.35 + dur_bonus + vol_bonus + mom_bonus)

        self._cooldowns["range_breakout"] = cooldown
        self._range_bar_count = 0

        return TransitionEvent(
            event_type="range_breakout",
            direction=data["direction"],
            confidence=round(confidence, 3),
            detection_tf="1h",
            bar_idx=loc,
            bar_timestamp=df.index[loc] if loc < len(df) else None,
            features_snapshot={
                "range_high": round(data["range_high"], 2),
                "range_low": round(data["range_low"], 2),
                "range_pct": round(data["range_pct"], 2),
                "range_bars": data["range_bars"],
                "vol_ratio": round(data["vol_ratio"], 2),
                "mom_count": data["mom_count"],
            },
        )

    # ────────────────────────────────────────────────────────────────
    # Type 3: Pullback → Continuation
    # ────────────────────────────────────────────────────────────────

    def _check_pullback_continuation(self, df_1h, loc, df_4h, idx_4h) -> Optional[TransitionEvent]:
        if "pullback_continuation" in self._cooldowns:
            return None
        if loc < 20:
            return None

        ema_prox_atr = _get(self._p, "pullback_continuation.ema_prox_atr")
        rsi_pb_bull = _get(self._p, "pullback_continuation.rsi_pullback_bull")
        rsi_pb_bear = _get(self._p, "pullback_continuation.rsi_pullback_bear")
        swing = int(_get(self._p, "pullback_continuation.swing_bars"))
        vol_min = _get(self._p, "pullback_continuation.volume_mult_min")
        htf_adx_min = _get(self._p, "pullback_continuation.htf_adx_min")
        cooldown = int(_get(self._p, "pullback_continuation.cooldown_bars"))

        # Phase A: 4h trend gate
        htf_direction = self._get_htf_direction(df_4h, idx_4h, htf_adx_min)
        if htf_direction is None:
            self._in_pullback = False
            return None

        # 1h features
        close = self._col(df_1h, loc, "close")
        ema_20 = self._col(df_1h, loc, "ema_20")
        ema_50 = self._col(df_1h, loc, "ema_50")
        atr = self._col(df_1h, loc, "atr_14")
        rsi = self._col(df_1h, loc, "rsi_14", 50)
        adx = self._col(df_1h, loc, "adx_14", 0)

        if any(np.isnan(v) for v in [close, ema_20, ema_50, atr]) or atr == 0:
            return None

        # EMA zone: use EMA20–EMA50 range (adjusted per review)
        ema_zone_top = max(ema_20, ema_50)
        ema_zone_bottom = min(ema_20, ema_50)
        ema_zone_width = ema_zone_top - ema_zone_bottom

        # Phase B: Pullback detection
        if not self._in_pullback:
            if htf_direction == "bull":
                # Price entering or within EMA20–EMA50 zone from above
                in_zone = close <= ema_zone_top + ema_prox_atr * atr and close >= ema_zone_bottom - 0.5 * atr
                rsi_retrace = rsi < rsi_pb_bull
                # Higher low check
                if loc >= swing and "low" in df_1h.columns:
                    current_low = self._col(df_1h, loc, "low")
                    prev_low = float(df_1h["low"].iloc[loc - swing:loc].min())
                    hl_ok = current_low > prev_low
                else:
                    hl_ok = False

                if in_zone and rsi_retrace and hl_ok:
                    self._in_pullback = True
                    self._pullback_direction = "bull"
                    return None

            elif htf_direction == "bear":
                in_zone = close >= ema_zone_bottom - ema_prox_atr * atr and close <= ema_zone_top + 0.5 * atr
                rsi_retrace = rsi > rsi_pb_bear
                if loc >= swing and "high" in df_1h.columns:
                    current_high = self._col(df_1h, loc, "high")
                    prev_high = float(df_1h["high"].iloc[loc - swing:loc].max())
                    lh_ok = current_high < prev_high
                else:
                    lh_ok = False

                if in_zone and rsi_retrace and lh_ok:
                    self._in_pullback = True
                    self._pullback_direction = "bear"
                    return None

            return None

        # Phase C: Continuation trigger (in_pullback == True)
        # Must match the 4h direction
        if self._pullback_direction != htf_direction:
            self._in_pullback = False
            return None

        # Mandatory: EMA recross
        if self._pullback_direction == "bull":
            ema_recross = close > ema_20
        else:
            ema_recross = close < ema_20

        if not ema_recross:
            return None  # still in pullback

        # Additional confirmations (at least 1 required)
        confirm_count = 0

        # RSI recovery
        if self._pullback_direction == "bull" and rsi > 50:
            confirm_count += 1
        elif self._pullback_direction == "bear" and rsi < 50:
            confirm_count += 1

        # Volume uptick
        vol_now = self._col(df_1h, loc, "volume")
        vol_mean = self._rolling_mean(df_1h, loc, "volume", 20)
        vol_ratio = vol_now / vol_mean if not np.isnan(vol_mean) and vol_mean > 0 else 0
        if vol_ratio >= vol_min:
            confirm_count += 1

        # ADX inflection
        adx_prev = self._col(df_1h, loc - 1, "adx_14", 0) if loc > 0 else 0
        if adx > adx_prev:
            confirm_count += 1

        if confirm_count < 1:
            return None

        # Confidence
        dist_to_zone = abs(close - (ema_zone_top + ema_zone_bottom) / 2)
        prox_bonus = min(0.15, (1.0 - dist_to_zone / (atr + 1e-9)) * 0.15) if atr > 0 else 0
        prox_bonus = max(0, prox_bonus)
        struct_bonus = 0.10  # HL/LH was confirmed in Phase B
        confirm_bonus = min(0.15, confirm_count * 0.05)
        confidence = min(0.85, 0.35 + prox_bonus + struct_bonus + confirm_bonus)

        direction = "long" if self._pullback_direction == "bull" else "short"

        snapshot = {
            "ema_20": round(ema_20, 2), "ema_50": round(ema_50, 2),
            "rsi": round(rsi, 1), "adx": round(adx, 1),
            "vol_ratio": round(vol_ratio, 2),
            "confirm_count": confirm_count,
            "htf_direction": htf_direction,
        }

        self._cooldowns["pullback_continuation"] = cooldown
        self._in_pullback = False
        self._pullback_direction = ""

        return TransitionEvent(
            event_type="pullback_continuation",
            direction=direction,
            confidence=round(confidence, 3),
            detection_tf="1h",
            bar_idx=loc,
            bar_timestamp=df_1h.index[loc] if loc < len(df_1h) else None,
            features_snapshot=snapshot,
        )

    def _get_htf_direction(self, df_4h, idx_4h, adx_min) -> Optional[str]:
        """Check 4h trend gate. Returns 'bull', 'bear', or None."""
        if df_4h is None or idx_4h is None or idx_4h < 0:
            return None

        ema_20 = self._col(df_4h, idx_4h, "ema_20")
        ema_50 = self._col(df_4h, idx_4h, "ema_50")
        adx = self._col(df_4h, idx_4h, "adx_14", 0)

        if np.isnan(ema_20) or np.isnan(ema_50):
            return None
        if adx < adx_min:
            return None

        if ema_20 > ema_50:
            return "bull"
        elif ema_20 < ema_50:
            return "bear"
        return None
