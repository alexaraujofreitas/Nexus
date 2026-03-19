# ============================================================
# NEXUS TRADER — Level-2 Performance Tracker  (v2)
#
# Tracks model performance across multiple contextual
# dimensions that Level-1 (global win rate) cannot capture:
#
#   • (model × regime) — does TrendModel work in bear markets?
#   • (model × asset)  — does MR work better on BTC vs SOL?
#   • score calibration — do 0.8-score trades actually win more?
#   • exit efficiency   — TP hit rate vs SL hit rate per model
#
# ALL adjustments are bounded, guarded by minimum sample
# counts, and use rolling windows to prevent stale data
# from dominating.  This tracker is DIAGNOSTIC + ADAPTIVE
# (it adjusts weights) but NEVER changes model code or
# signal thresholds automatically.
#
# ── v2 improvements ────────────────────────────────────────
#  • Partial activation: cells with MIN_SAMPLES_PARTIAL–
#    MIN_SAMPLES_CELL trades apply a confidence-scaled blend
#    instead of hard-neutral, providing signal earlier without
#    exposing the system to under-sampled noise.
#
#  • Hierarchical fallback: when a (model, regime) or
#    (model, asset) cell has < MIN_SAMPLES_PARTIAL trades, the
#    adjustment falls back to the model-wide average across
#    all active cells for that dimension (at half strength),
#    rather than returning a neutral 1.0.  This means a model
#    that consistently performs well in all observed regimes
#    will carry a mild positive prior into unseen regimes.
#
#  • Richer exit attribution: record() now accepts realized_r
#    and expected_rr.  Rolling float windows per (model, exit)
#    track average realized R by exit reason, enabling the
#    "target capture rate" diagnostic.
#
#  • Score calibration quality: get_score_calibration_quality()
#    measures monotonicity — whether higher score buckets
#    consistently produce higher win rates.  Purely diagnostic;
#    does not adjust any weights.
#
# ── Safeguards against overfitting ────────────────────────
#  1. MIN_SAMPLES_CELL    — full activation threshold (10)
#  2. MIN_SAMPLES_PARTIAL — partial activation floor   (5)
#  3. WINDOW              — rolling window per cell    (50)
#  4. MAX_ADJ_*           — hard magnitude caps
#  5. Fallback at ×0.5 strength — conservative prior
#  6. No single update moves more than MAX_ADJ per call
# ============================================================
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PERSIST_FILE   = Path(__file__).parent.parent.parent / "data" / "level2_tracker.json"

# ── Hyperparameters ──────────────────────────────────────────────────────────
MIN_SAMPLES_CELL    = 10    # full activation threshold
MIN_SAMPLES_PARTIAL = 5     # partial (confidence-scaled) activation floor
WINDOW              = 50    # max rolling window per cell (bool and float)
MAX_ADJ_REGIME      = 0.10  # max ±10% from regime×model adjustment
MAX_ADJ_ASSET       = 0.08  # max ±8% from asset×model adjustment
FALLBACK_STRENGTH   = 0.5   # model-level fallback is applied at 50% strength

# Score calibration bins: [0.3,0.4), [0.4,0.5), ... [0.9,1.0]
SCORE_BINS    = [(0.3 + i * 0.1, 0.4 + i * 0.1) for i in range(7)]
MIN_SCORE_BIN = 5           # min trades per score bin for calibration


# ── Rolling window helpers ────────────────────────────────────────────────────

class _RollingWindow:
    """Thread-safe fixed-size rolling list of booleans (won/lost)."""

    def __init__(self, maxlen: int = WINDOW):
        self._d    = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, won: bool) -> None:
        with self._lock:
            self._d.append(bool(won))

    def win_rate(self) -> Optional[float]:
        """Full win rate — returns None if count < MIN_SAMPLES_CELL."""
        with self._lock:
            n = len(self._d)
            if n < MIN_SAMPLES_CELL:
                return None
            return sum(self._d) / n

    def raw_win_rate(self) -> Optional[float]:
        """Win rate with no minimum sample guard (for partial activation)."""
        with self._lock:
            n = len(self._d)
            if n == 0:
                return None
            return sum(self._d) / n

    def count(self) -> int:
        with self._lock:
            return len(self._d)

    def to_list(self) -> list[bool]:
        with self._lock:
            return list(self._d)

    @classmethod
    def from_list(cls, data: list, maxlen: int = WINDOW) -> "_RollingWindow":
        w = cls(maxlen=maxlen)
        for v in data[-maxlen:]:
            w._d.append(bool(v))
        return w


class _RollingFloatWindow:
    """Thread-safe fixed-size rolling list of float values (for R-multiples, etc.)."""

    def __init__(self, maxlen: int = WINDOW):
        self._d    = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, v: float) -> None:
        with self._lock:
            self._d.append(float(v))

    def mean(self) -> Optional[float]:
        with self._lock:
            if not self._d:
                return None
            return round(sum(self._d) / len(self._d), 4)

    def count(self) -> int:
        with self._lock:
            return len(self._d)

    def to_list(self) -> list[float]:
        with self._lock:
            return list(self._d)

    @classmethod
    def from_list(cls, data: list, maxlen: int = WINDOW) -> "_RollingFloatWindow":
        w = cls(maxlen=maxlen)
        for v in data[-maxlen:]:
            w._d.append(float(v))
        return w


# ── Adjustment formula ────────────────────────────────────────────────────────

def _win_rate_to_adj(win_rate: Optional[float], max_adj: float) -> float:
    """
    Convert win rate to a weight multiplier.

    Formula: 1.0 + (WR - 0.50) * (max_adj / 0.20)
    This means:
      WR = 0.70 → +max_adj
      WR = 0.50 → 0.0  (neutral)
      WR = 0.30 → -max_adj
    Hard clamped to [1 - max_adj, 1 + max_adj].
    None input → 1.0 (neutral).
    """
    if win_rate is None:
        return 1.0
    deviation  = win_rate - 0.50
    adjustment = deviation * (max_adj / 0.20)
    adjustment = max(-max_adj, min(max_adj, adjustment))
    return round(1.0 + adjustment, 6)


# ── Main tracker ──────────────────────────────────────────────────────────────

class Level2PerformanceTracker:
    """
    Tracks contextual model performance across four dimensions:
      1. (model, regime) — regime-specific model quality
      2. (model, asset)  — asset-specific model quality
      3. score bucket    — is the confluence score well-calibrated?
      4. exit efficiency — TP vs SL hit rates, realized R per exit type

    New in v2:
      • Partial activation (MIN_SAMPLES_PARTIAL ≤ count < MIN_SAMPLES_CELL)
      • Hierarchical fallback (model-wide average when cell is empty)
      • Realized R tracking per exit reason per model
      • Score calibration quality metric (monotonicity score)

    Persistence: data/level2_tracker.json (loaded on init, saved
    after every record() call).  Thread-safe throughout.
    """

    def __init__(self):
        self._lock       = threading.Lock()
        # Keyed: (model_name, regime) → _RollingWindow
        self._regime:    dict[tuple, _RollingWindow]      = {}
        # Keyed: (model_name, symbol) → _RollingWindow
        self._asset:     dict[tuple, _RollingWindow]      = {}
        # Keyed: score_bin_label → _RollingWindow
        self._score_cal: dict[str, _RollingWindow]        = {}
        # Keyed: model_name → {"tp": int, "sl": int, "other": int}
        self._exit_eff:  dict[str, dict]                  = {}
        # Keyed: (model_name, exit_bucket) → _RollingFloatWindow of realized R values
        # exit_bucket ∈ {"tp", "sl", "other"}
        self._exit_r:    dict[tuple, _RollingFloatWindow] = {}
        # Keyed: model_name → expected RR at entry (for target capture rate)
        self._entry_rr:  dict[str, _RollingFloatWindow]   = {}
        self._load()

    # ── Public record API ─────────────────────────────────────────────────

    def record(
        self,
        models:      list[str],
        won:         bool,
        regime:      str,
        symbol:      str,
        score:       float,
        exit_reason: str           = "",
        realized_r:  Optional[float] = None,
        expected_rr: Optional[float] = None,
        source:      str = "live",
    ) -> None:
        """
        Record one trade outcome across all tracking dimensions.

        New in v2:
            realized_r  — realized R-multiple for this trade (P&L / initial risk)
            expected_rr — expected R:R configured at entry
            source      — origin tag — "live" for real trades, "test"/"synthetic" are rejected

        Safe to call from any thread.
        """
        if source in ("test", "synthetic"):
            logger.debug("Level2PerformanceTracker: rejecting %s-sourced outcome (source tagging)", source)
            return
        regime  = (regime or "unknown").lower()
        symbol  = (symbol or "?")
        ex_type = (
            "tp"    if "take_profit" in exit_reason else
            "sl"    if "stop_loss"   in exit_reason else
            "other"
        )

        with self._lock:
            for model in models:
                # ── (model, regime) cell ──────────────────────────────
                key_r = (model, regime)
                if key_r not in self._regime:
                    self._regime[key_r] = _RollingWindow()
                self._regime[key_r].append(won)

                # ── (model, asset) cell ───────────────────────────────
                key_a = (model, symbol)
                if key_a not in self._asset:
                    self._asset[key_a] = _RollingWindow()
                self._asset[key_a].append(won)

                # ── exit efficiency counts ────────────────────────────
                if model not in self._exit_eff:
                    self._exit_eff[model] = {"tp": 0, "sl": 0, "other": 0}
                self._exit_eff[model][ex_type] += 1

                # ── exit realized R rolling windows ───────────────────
                if realized_r is not None:
                    key_er = (model, ex_type)
                    if key_er not in self._exit_r:
                        self._exit_r[key_er] = _RollingFloatWindow()
                    self._exit_r[key_er].append(realized_r)

                # ── expected RR per model (for target capture rate) ───
                if expected_rr is not None and expected_rr > 0:
                    if model not in self._entry_rr:
                        self._entry_rr[model] = _RollingFloatWindow()
                    self._entry_rr[model].append(expected_rr)

            # ── score calibration (model-agnostic) ────────────────────
            bin_label = self._score_bin(score)
            if bin_label:
                if bin_label not in self._score_cal:
                    self._score_cal[bin_label] = _RollingWindow()
                self._score_cal[bin_label].append(won)

        self._save()

    # ── Adjustment getters ────────────────────────────────────────────────

    def get_regime_adjustment(self, model: str, regime: str) -> float:
        """
        Weight multiplier for (model, regime).

        Activation tiers (v2):
          count ≥ MIN_SAMPLES_CELL   → full adjustment
          count ≥ MIN_SAMPLES_PARTIAL → confidence-scaled partial adj
          count <  MIN_SAMPLES_PARTIAL → hierarchical fallback
                                         (model-wide avg at FALLBACK_STRENGTH)

        Range: [1 - MAX_ADJ_REGIME, 1 + MAX_ADJ_REGIME]
        """
        regime_k = (regime or "unknown").lower()
        key = (model, regime_k)
        with self._lock:
            window = self._regime.get(key)
            count  = window.count() if window is not None else 0

        if count == 0:
            # No data at all — hierarchical fallback to model-wide average
            return self._get_model_fallback_adj("regime", model, MAX_ADJ_REGIME)
        elif count >= MIN_SAMPLES_CELL:
            # Full activation
            return _win_rate_to_adj(window.win_rate(), MAX_ADJ_REGIME)
        elif count >= MIN_SAMPLES_PARTIAL:
            # Partial activation: confidence-scaled blend toward neutral
            confidence = count / MIN_SAMPLES_CELL
            wr = window.raw_win_rate()
            full_adj = _win_rate_to_adj(wr, MAX_ADJ_REGIME)
            return round(1.0 + confidence * (full_adj - 1.0), 6)
        else:
            # Too few samples — hierarchical fallback
            return self._get_model_fallback_adj("regime", model, MAX_ADJ_REGIME)

    def get_asset_adjustment(self, model: str, symbol: str) -> float:
        """
        Weight multiplier for (model, asset).

        Same three-tier activation as get_regime_adjustment().
        Range: [1 - MAX_ADJ_ASSET, 1 + MAX_ADJ_ASSET]
        """
        sym_k = (symbol or "?")
        key   = (model, sym_k)
        with self._lock:
            window = self._asset.get(key)
            count  = window.count() if window is not None else 0

        if count == 0:
            return self._get_model_fallback_adj("asset", model, MAX_ADJ_ASSET)
        elif count >= MIN_SAMPLES_CELL:
            return _win_rate_to_adj(window.win_rate(), MAX_ADJ_ASSET)
        elif count >= MIN_SAMPLES_PARTIAL:
            confidence = count / MIN_SAMPLES_CELL
            wr = window.raw_win_rate()
            full_adj = _win_rate_to_adj(wr, MAX_ADJ_ASSET)
            return round(1.0 + confidence * (full_adj - 1.0), 6)
        else:
            return self._get_model_fallback_adj("asset", model, MAX_ADJ_ASSET)

    def get_score_calibration(self) -> dict[str, dict]:
        """
        Returns win-rate and sample count per score bucket.
        Used for dashboard visualization; does not directly adjust weights.

        Format: {"0.3-0.4": {"win_rate": 0.42, "count": 18}, ...}
        """
        result = {}
        with self._lock:
            for label, window in self._score_cal.items():
                wr = window.win_rate()
                result[label] = {
                    "win_rate": round(wr, 4) if wr is not None else None,
                    "count":    window.count(),
                }
        return result

    def get_score_calibration_quality(self) -> dict:
        """
        Measures calibration quality by testing whether higher score buckets
        consistently produce higher win rates (monotonicity).

        Returns:
            {
                "monotonicity_score": float (0.0–1.0),
                "active_bins":        int,
                "description":        str,
                "bin_win_rates":      dict[str, Optional[float]],
            }

        monotonicity_score = fraction of adjacent active bin pairs where
        the higher bin has a higher win rate.  1.0 = perfectly calibrated.
        0.0 = higher scores systematically under-perform.
        None if fewer than 2 active bins.
        """
        with self._lock:
            bins_ordered = sorted(
                [(label, w.win_rate()) for label, w in self._score_cal.items()
                 if w.win_rate() is not None],
                key=lambda x: x[0],
            )

        active = len(bins_ordered)
        bin_wr = {label: wr for label, wr in bins_ordered}

        if active < 2:
            return {
                "monotonicity_score": None,
                "active_bins":        active,
                "description":        f"Insufficient data ({active} active bin(s)); need ≥2.",
                "bin_win_rates":      bin_wr,
            }

        pairs      = list(zip(bins_ordered, bins_ordered[1:]))
        monotone   = sum(1 for (_, lo_wr), (_, hi_wr) in pairs if hi_wr >= lo_wr)
        mono_score = round(monotone / len(pairs), 4)

        if mono_score >= 0.75:
            desc = "Good calibration — higher scores generally predict better outcomes."
        elif mono_score >= 0.50:
            desc = "Moderate calibration — weak positive trend between score and outcome."
        else:
            desc = "Poor calibration — score is not reliably predicting trade success."

        return {
            "monotonicity_score": mono_score,
            "active_bins":        active,
            "description":        desc,
            "bin_win_rates":      bin_wr,
        }

    def get_exit_efficiency(self) -> dict[str, dict]:
        """
        Returns TP/SL/other counts, TP hit rate, and avg realized R per exit
        type per model.

        Format (v2):
            {
                "trend": {
                    "tp": 12, "sl": 8, "other": 2,
                    "tp_rate": 0.545,
                    "total":   22,
                    "avg_tp_r":    2.01,   # avg realized R on TP exits
                    "avg_sl_r":   -1.05,   # avg realized R on SL exits
                    "avg_r":       0.74,   # avg realized R across all exits
                    "avg_exp_rr":  1.98,   # avg expected R:R at entry
                    "target_capture_pct": 101.5,  # avg_tp_r / avg_exp_rr * 100
                },
                ...
            }
        """
        result = {}
        with self._lock:
            for model, counts in self._exit_eff.items():
                total = counts["tp"] + counts["sl"] + counts["other"]
                avg_tp_r = self._exit_r.get((model, "tp"))
                avg_sl_r = self._exit_r.get((model, "sl"))
                avg_all  = None
                # Compute blended average across all exit windows
                tp_r_mean  = avg_tp_r.mean() if avg_tp_r else None
                sl_r_mean  = avg_sl_r.mean() if avg_sl_r else None
                exp_rr_win = self._entry_rr.get(model)
                exp_rr_mean = exp_rr_win.mean() if exp_rr_win else None
                target_cap = None
                if tp_r_mean is not None and exp_rr_mean and exp_rr_mean > 0:
                    target_cap = round(tp_r_mean / exp_rr_mean * 100, 1)
                result[model] = {
                    "tp":                  counts["tp"],
                    "sl":                  counts["sl"],
                    "other":               counts["other"],
                    "tp_rate":             round(counts["tp"] / total, 4) if total > 0 else None,
                    "total":               total,
                    "avg_tp_r":            tp_r_mean,
                    "avg_sl_r":            sl_r_mean,
                    "avg_exp_rr":          exp_rr_mean,
                    "target_capture_pct":  target_cap,
                }
        return result

    def get_exit_diagnostics(self) -> dict:
        """
        Comprehensive exit quality analysis for dashboard and strategy review.

        Returns:
            {
                "by_model": dict  — per-model exit efficiency (same as get_exit_efficiency()),
                "overall": {
                    "tp_rate_pct":   float — overall TP rate across all models,
                    "sl_rate_pct":   float — overall SL rate,
                    "avg_tp_r":      float — avg realized R on TP exits,
                    "avg_sl_r":      float — avg realized R on SL exits,
                    "stop_tightness_flag": bool — True if SL rate > 60%
                                                  (suggests stops may be too tight),
                }
            }
        """
        by_model = self.get_exit_efficiency()
        total_tp = sum(v["tp"]    for v in by_model.values())
        total_sl = sum(v["sl"]    for v in by_model.values())
        total_ot = sum(v["other"] for v in by_model.values())
        grand    = total_tp + total_sl + total_ot

        # Aggregate realized R means (weighted by count)
        tp_r_vals = [v["avg_tp_r"] for v in by_model.values() if v["avg_tp_r"] is not None]
        sl_r_vals = [v["avg_sl_r"] for v in by_model.values() if v["avg_sl_r"] is not None]

        overall = {
            "tp_rate_pct":          round(total_tp / grand * 100, 1) if grand > 0 else None,
            "sl_rate_pct":          round(total_sl / grand * 100, 1) if grand > 0 else None,
            "other_rate_pct":       round(total_ot / grand * 100, 1) if grand > 0 else None,
            "avg_tp_r":             round(sum(tp_r_vals) / len(tp_r_vals), 3) if tp_r_vals else None,
            "avg_sl_r":             round(sum(sl_r_vals) / len(sl_r_vals), 3) if sl_r_vals else None,
            "stop_tightness_flag":  (total_sl / grand > 0.60) if grand > 0 else False,
        }
        return {"by_model": by_model, "overall": overall}

    def get_summary(self) -> dict:
        """
        High-level summary for dashboard.
        Returns active cells, partial cells, warming cells, and overall status.
        """
        with self._lock:
            def _classify(windows: dict) -> tuple[int, int, int]:
                """Returns (active, partial, warming) counts."""
                active = warming = partial = 0
                for w in windows.values():
                    c = w.count()
                    if c >= MIN_SAMPLES_CELL:
                        active  += 1
                    elif c >= MIN_SAMPLES_PARTIAL:
                        partial += 1
                    elif c > 0:
                        warming += 1
                return active, partial, warming

            r_act, r_part, r_warm = _classify(self._regime)
            a_act, a_part, a_warm = _classify(self._asset)
            score_bins_act = sum(1 for w in self._score_cal.values() if w.win_rate() is not None)

            return {
                "regime_cells_active":   r_act,
                "regime_cells_partial":  r_part,
                "regime_cells_warming":  r_warm,
                "asset_cells_active":    a_act,
                "asset_cells_partial":   a_part,
                "asset_cells_warming":   a_warm,
                "score_bins_active":     score_bins_act,
                "total_cells":           len(self._regime) + len(self._asset),
            }

    def get_regime_table(self) -> list[dict]:
        """
        Returns per-(model, regime) win-rate rows for the dashboard table.
        v2: includes activation_tier ("active", "partial", "warming", "empty").
        """
        rows = []
        with self._lock:
            for (model, regime), window in sorted(self._regime.items()):
                c  = window.count()
                wr = window.win_rate()
                if c >= MIN_SAMPLES_CELL:
                    tier, adj = "active",  _win_rate_to_adj(wr, MAX_ADJ_REGIME)
                elif c >= MIN_SAMPLES_PARTIAL:
                    tier = "partial"
                    raw_wr = window.raw_win_rate()
                    confidence = c / MIN_SAMPLES_CELL
                    full_adj = _win_rate_to_adj(raw_wr, MAX_ADJ_REGIME)
                    adj = round(1.0 + confidence * (full_adj - 1.0), 6)
                else:
                    tier, adj = "warming", 1.0
                rows.append({
                    "model":           model,
                    "regime":          regime,
                    "trades":          c,
                    "win_rate":        round(wr, 4) if wr is not None else
                                       round(window.raw_win_rate(), 4) if c > 0 else None,
                    "adj":             adj,
                    "active":          c >= MIN_SAMPLES_CELL,
                    "activation_tier": tier,
                })
        return rows

    def get_asset_table(self) -> list[dict]:
        """
        Returns per-(model, asset) win-rate rows for the dashboard table.
        v2: includes activation_tier.
        """
        rows = []
        with self._lock:
            for (model, symbol), window in sorted(self._asset.items()):
                c  = window.count()
                wr = window.win_rate()
                if c >= MIN_SAMPLES_CELL:
                    tier, adj = "active",  _win_rate_to_adj(wr, MAX_ADJ_ASSET)
                elif c >= MIN_SAMPLES_PARTIAL:
                    tier = "partial"
                    raw_wr = window.raw_win_rate()
                    confidence = c / MIN_SAMPLES_CELL
                    full_adj = _win_rate_to_adj(raw_wr, MAX_ADJ_ASSET)
                    adj = round(1.0 + confidence * (full_adj - 1.0), 6)
                else:
                    tier, adj = "warming", 1.0
                rows.append({
                    "model":           model,
                    "symbol":          symbol,
                    "trades":          c,
                    "win_rate":        round(wr, 4) if wr is not None else
                                       round(window.raw_win_rate(), 4) if c > 0 else None,
                    "adj":             adj,
                    "active":          c >= MIN_SAMPLES_CELL,
                    "activation_tier": tier,
                })
        return rows

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_model_fallback_adj(
        self, dimension: str, model: str, max_adj: float
    ) -> float:
        """
        Hierarchical fallback: when a specific (model, context) cell has no
        data, compute the model-wide average win rate across ALL active cells
        for that dimension and apply it at FALLBACK_STRENGTH (50%).

        This means a model that consistently wins in all observed regimes
        will carry a mild positive prior into unseen regimes, while a
        consistently poor model carries a mild negative prior.

        Returns 1.0 if the model has no active cells in this dimension.
        """
        storage = self._regime if dimension == "regime" else self._asset
        with self._lock:
            rates = [
                w.win_rate()
                for (m, _), w in storage.items()
                if m == model and w.win_rate() is not None
            ]
        if not rates:
            return 1.0
        avg_wr  = sum(rates) / len(rates)
        full_adj = _win_rate_to_adj(avg_wr, max_adj)
        # Apply at half strength to remain conservative for unseen contexts
        return round(1.0 + FALLBACK_STRENGTH * (full_adj - 1.0), 6)

    # ── Persistence ───────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            _PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "regime": {
                        f"{m}|{r}": w.to_list()
                        for (m, r), w in self._regime.items()
                    },
                    "asset": {
                        f"{m}|{s}": w.to_list()
                        for (m, s), w in self._asset.items()
                    },
                    "score_cal": {
                        label: w.to_list()
                        for label, w in self._score_cal.items()
                    },
                    "exit_eff":  dict(self._exit_eff),
                    "exit_r": {
                        f"{m}|{ex}": w.to_list()
                        for (m, ex), w in self._exit_r.items()
                    },
                    "entry_rr": {
                        m: w.to_list()
                        for m, w in self._entry_rr.items()
                    },
                }
            with open(_PERSIST_FILE, "w") as fh:
                json.dump(data, fh)
        except Exception as exc:
            logger.debug("Level2Tracker: save failed (non-fatal): %s", exc)

    def _load(self) -> None:
        try:
            if not _PERSIST_FILE.exists():
                return
            with open(_PERSIST_FILE, "r") as fh:
                data = json.load(fh)
            with self._lock:
                for key_str, outcomes in data.get("regime", {}).items():
                    parts = key_str.split("|", 1)
                    if len(parts) == 2:
                        self._regime[(parts[0], parts[1])] = _RollingWindow.from_list(outcomes)
                for key_str, outcomes in data.get("asset", {}).items():
                    parts = key_str.split("|", 1)
                    if len(parts) == 2:
                        self._asset[(parts[0], parts[1])] = _RollingWindow.from_list(outcomes)
                for label, outcomes in data.get("score_cal", {}).items():
                    self._score_cal[label] = _RollingWindow.from_list(outcomes, maxlen=200)
                for model, counts in data.get("exit_eff", {}).items():
                    self._exit_eff[model] = dict(counts)
                for key_str, rvals in data.get("exit_r", {}).items():
                    parts = key_str.split("|", 1)
                    if len(parts) == 2:
                        self._exit_r[(parts[0], parts[1])] = _RollingFloatWindow.from_list(rvals)
                for model, rvals in data.get("entry_rr", {}).items():
                    self._entry_rr[model] = _RollingFloatWindow.from_list(rvals)
            logger.debug(
                "Level2Tracker: loaded %d regime cells, %d asset cells from disk",
                len(self._regime), len(self._asset),
            )
        except Exception as exc:
            logger.debug("Level2Tracker: load failed (starting fresh): %s", exc)

    # ── Score bin helper ───────────────────────────────────────────────────

    @staticmethod
    def _score_bin(score: float) -> Optional[str]:
        """Map a score to its bin label, e.g. 0.72 → '0.7-0.8'."""
        for lo, hi in SCORE_BINS:
            if lo <= score < hi:
                return f"{lo:.1f}-{hi:.1f}"
        if score >= 0.9:
            return "0.9-1.0"
        return None

    def reset_for_testing(self) -> None:
        """Clear all state — for unit tests only."""
        with self._lock:
            self._regime.clear()
            self._asset.clear()
            self._score_cal.clear()
            self._exit_eff.clear()
            self._exit_r.clear()
            self._entry_rr.clear()


# ── Module singleton ─────────────────────────────────────────────────────────
_level2_tracker = Level2PerformanceTracker()


def get_level2_tracker() -> Level2PerformanceTracker:
    """Return the module-level Level-2 tracker singleton."""
    return _level2_tracker
