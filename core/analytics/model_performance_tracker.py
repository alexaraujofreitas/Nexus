"""
Model Performance Tracker — Phase 2.
Tracks per-model trade outcomes and provides auto-disable logic.

v1 (Session 21): Simple WR < 40% after 20 trades → disable.
v2 (Session 22): Multi-criteria framework — a model is auto-disabled only when ALL
  applicable criteria agree it has negative expected value. Requires minimum
  sample sizes before each criterion activates.

v2 Criteria (all must be met for auto-disable):
  1. MIN_TRADES_FOR_EVAL (default 20)  — absolute gate, below this nothing disables
  2. Win rate < wr_threshold (default 40%) over rolling ROLLING_WINDOW trades
  3. Expectancy (avg R-multiple) < expectancy_threshold (default -0.10)
     — a model can have WR < 40% but positive expectancy via large winners
  4. Profit factor < pf_threshold (default 0.85) once MIN_TRADES_PF trades seen
     — gross_win_r / gross_loss_r; < 1.0 means more R lost than gained
  5. Regime isolation: if the model has positive expectancy in ≥1 regime with
     ≥MIN_REGIME_TRADES trades, do NOT disable globally — disable only in
     losing regimes via the 'regime_blacklist' recommendation

A model is recommended for GLOBAL disable only when it fails all 4 criteria above.
When only regime-specific failures exist, the tracker returns 'regime_blacklist'
with the failing (model, regime) pairs.

Note: The criteria thresholds are deliberately conservative. False positives
(disabling a good model) are more costly than false negatives (keeping a bad one)
because disabling reduces signal diversity. Disable only when evidence is clear.
"""
from __future__ import annotations
import json
import logging
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional
from config.settings import settings as _s

logger = logging.getLogger(__name__)

_TRACKER_PATH = Path(__file__).parent.parent.parent / "data" / "model_perf_tracker.json"
_lock = threading.Lock()

ROLLING_WINDOW       = 50   # trades used for rolling WR / expectancy
MIN_TRADES_FOR_EVAL  = 20   # absolute minimum before any criterion activates
MIN_TRADES_PF        = 30   # minimum trades before profit-factor criterion fires
MIN_REGIME_TRADES    = 10   # minimum trades in a regime before regime isolation applies


class ModelPerformanceTracker:
    """
    Tracks per-model statistics from trade outcomes and implements
    multi-criteria auto-disable logic.
    """

    # v1 backward-compat class attributes
    MIN_TRADES_FOR_EVAL = MIN_TRADES_FOR_EVAL
    AUTO_DISABLE_WR_THRESHOLD = 0.40

    def __init__(self):
        self._stats: dict = {}        # model_name -> {wins, losses, r_sum, trades, r_history}
        self._regime_stats: dict = {} # (model, regime) -> {wins, losses, r_sum}
        self._load()

    def _load(self) -> None:
        if not _TRACKER_PATH.exists():
            return
        try:
            data = json.loads(_TRACKER_PATH.read_text())
            self._stats = data.get("stats", {})
            # Restore r_history as deque
            for m, s in self._stats.items():
                history = s.get("r_history", [])
                s["r_history"] = deque(history, maxlen=ROLLING_WINDOW)
            self._regime_stats = {
                tuple(k.split("|")): v
                for k, v in data.get("regime_stats", {}).items()
            }
        except Exception as exc:
            logger.warning("ModelPerfTracker: load failed: %s", exc)

    def _save(self) -> None:
        try:
            _TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Serialise deques as lists
            serialisable_stats = {}
            for m, s in self._stats.items():
                entry = dict(s)
                entry["r_history"] = list(s.get("r_history", []))
                serialisable_stats[m] = entry
            data = {
                "stats": serialisable_stats,
                "regime_stats": {"|".join(k): v for k, v in self._regime_stats.items()},
            }
            _TRACKER_PATH.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("ModelPerfTracker: save failed: %s", exc)

    def record(
        self,
        models_fired: list[str],
        won: bool,
        realized_r: Optional[float],
        regime: str = "",
    ) -> None:
        """Record a trade outcome for all models that fired."""
        with _lock:
            for model in (models_fired or []):
                if model not in self._stats:
                    self._stats[model] = {
                        "wins": 0, "losses": 0, "r_sum": 0.0, "trades": 0,
                        "gross_win_r": 0.0, "gross_loss_r": 0.0,
                        "r_history": deque(maxlen=ROLLING_WINDOW),
                    }
                s = self._stats[model]
                s["trades"] += 1
                if won:
                    s["wins"] += 1
                else:
                    s["losses"] += 1
                if realized_r is not None:
                    r = float(realized_r)
                    s["r_sum"] = round(s["r_sum"] + r, 4)
                    s["r_history"].append(r)
                    if r > 0:
                        s["gross_win_r"] = round(s.get("gross_win_r", 0.0) + r, 4)
                    else:
                        s["gross_loss_r"] = round(s.get("gross_loss_r", 0.0) + abs(r), 4)

                # Regime stats
                if regime:
                    key = (model, regime)
                    if key not in self._regime_stats:
                        self._regime_stats[key] = {"wins": 0, "losses": 0, "r_sum": 0.0}
                    rs = self._regime_stats[key]
                    if won:
                        rs["wins"] += 1
                    else:
                        rs["losses"] += 1
                    if realized_r is not None:
                        rs["r_sum"] = round(rs["r_sum"] + float(realized_r), 4)
            self._save()

    # ── Read accessors ────────────────────────────────────────────────────────

    def get_win_rate(self, model: str) -> Optional[float]:
        s = self._stats.get(model)
        if not s or s["trades"] < 1:
            return None
        return round(s["wins"] / s["trades"], 4)

    def get_expectancy(self, model: str) -> Optional[float]:
        """Avg R-multiple per trade (rolling ROLLING_WINDOW)."""
        s = self._stats.get(model)
        if not s or not s.get("r_history"):
            return None
        hist = list(s["r_history"])
        return round(sum(hist) / len(hist), 4)

    def get_profit_factor(self, model: str) -> Optional[float]:
        s = self._stats.get(model)
        if not s or s["trades"] < MIN_TRADES_PF:
            return None
        gross_win  = s.get("gross_win_r", 0.0)
        gross_loss = s.get("gross_loss_r", 0.0)
        if gross_loss <= 0:
            return None  # all wins — not enough data to penalise
        return round(gross_win / gross_loss, 4)

    def get_rolling_win_rate(self, model: str) -> Optional[float]:
        """Win rate computed on the last ROLLING_WINDOW trades only."""
        s = self._stats.get(model)
        if not s or not s.get("r_history"):
            return None
        hist = list(s["r_history"])
        if len(hist) < 5:
            return None
        wins = sum(1 for r in hist if r > 0)
        return round(wins / len(hist), 4)

    def get_stats(self, model: str) -> dict:
        raw = dict(self._stats.get(model, {
            "wins": 0, "losses": 0, "r_sum": 0.0, "trades": 0,
            "gross_win_r": 0.0, "gross_loss_r": 0.0,
        }))
        raw.pop("r_history", None)
        return raw

    def get_all_stats(self) -> dict[str, dict]:
        """Return all model stats with computed WR and expectancy."""
        result = {}
        for model, s in self._stats.items():
            t = s["trades"]
            result[model] = {
                "trades":       t,
                "wins":         s["wins"],
                "losses":       s["losses"],
                "win_rate":     round(s["wins"] / t, 4) if t > 0 else None,
                "expectancy_r": self.get_expectancy(model),
                "profit_factor": self.get_profit_factor(model),
                "rolling_wr":   self.get_rolling_win_rate(model),
            }
        return result

    def get_regime_win_rate(self, model: str, regime: str) -> Optional[float]:
        rs = self._regime_stats.get((model, regime))
        if not rs:
            return None
        total = rs["wins"] + rs["losses"]
        return round(rs["wins"] / total, 4) if total > 0 else None

    def get_regime_expectancy(self, model: str, regime: str) -> Optional[float]:
        rs = self._regime_stats.get((model, regime))
        if not rs:
            return None
        total = rs["wins"] + rs["losses"]
        if total < 1:
            return None
        return round(rs.get("r_sum", 0.0) / total, 4)

    # ── Auto-disable v2 ───────────────────────────────────────────────────────

    def should_auto_disable(self, model: str) -> tuple[bool, str]:
        """
        v2 multi-criteria auto-disable.
        Returns (should_disable_globally, reason_string).

        A model is globally disabled only when ALL of the following are true:
          1. trades >= min_trades
          2. Rolling WR < wr_threshold
          3. Expectancy < expectancy_threshold (neg expected value)
          4. Profit factor < pf_threshold (once enough trades available)

        If the model fails criteria 2/3 only in specific regimes but has positive
        expectancy in others, should_auto_disable returns False — use
        get_regime_blacklist() to see which (model, regime) pairs to avoid.
        """
        if not _s.get("filters.model_auto_disable.enabled", False):
            return False, ""

        s = self._stats.get(model)
        if not s:
            return False, ""

        trades = s["trades"]
        min_trades = int(_s.get("filters.model_auto_disable.min_trades", MIN_TRADES_FOR_EVAL))
        if trades < min_trades:
            return False, f"insufficient data ({trades} < {min_trades} trades)"

        # ── Criterion 1: rolling win rate ────────────────────────────────────
        wr_threshold = float(_s.get("filters.model_auto_disable.wr_threshold", 0.40))
        rolling_wr = self.get_rolling_win_rate(model)
        if rolling_wr is None or rolling_wr >= wr_threshold:
            return False, ""  # WR acceptable — no action

        # ── Criterion 2: expectancy ──────────────────────────────────────────
        exp_threshold = float(_s.get("filters.model_auto_disable.expectancy_threshold", -0.10))
        expectancy = self.get_expectancy(model)
        if expectancy is None or expectancy >= exp_threshold:
            # WR is low but expectancy is neutral/positive — large winners are saving it
            return (
                False,
                f"WR {rolling_wr:.1%} below threshold but expectancy {expectancy:.3f}R "
                f"is acceptable — model kept (size your winners)",
            )

        # ── Criterion 3: profit factor (only once enough trades available) ───
        pf_threshold = float(_s.get("filters.model_auto_disable.pf_threshold", 0.85))
        pf = self.get_profit_factor(model)
        if pf is not None and pf >= pf_threshold:
            return (
                False,
                f"WR {rolling_wr:.1%} low but PF {pf:.2f} ≥ {pf_threshold} — model kept",
            )

        # ── Criterion 4: regime isolation ────────────────────────────────────
        # If ≥1 regime has positive expectancy (≥MIN_REGIME_TRADES), don't globally disable
        positive_regimes = []
        for (m, regime), rs in self._regime_stats.items():
            if m != model:
                continue
            rtotal = rs["wins"] + rs["losses"]
            if rtotal < MIN_REGIME_TRADES:
                continue
            regime_exp = rs.get("r_sum", 0.0) / rtotal
            if regime_exp > 0:
                positive_regimes.append(f"{regime}(+{regime_exp:.3f}R)")

        if positive_regimes:
            return (
                False,
                f"WR {rolling_wr:.1%} / E {expectancy:.3f}R fail globally but "
                f"positive in: {', '.join(positive_regimes)} — use regime blacklist",
            )

        # All criteria failed — recommend global disable
        pf_str = f" PF={pf:.2f}" if pf is not None else ""
        reason = (
            f"WR {rolling_wr:.1%} < {wr_threshold:.0%}, "
            f"E {expectancy:.3f}R < {exp_threshold:.2f}R"
            f"{pf_str}"
            f" over {min(trades, ROLLING_WINDOW)} trades"
        )
        logger.warning(
            "ModelPerfTracker: %s RECOMMEND DISABLE | %s", model, reason
        )
        return True, reason

    def get_regime_blacklist(self, model: str) -> list[tuple[str, str]]:
        """
        Return list of (model, regime) pairs where the model has
        ≥MIN_REGIME_TRADES and negative expectancy. Used as soft guidance
        (e.g. reduce affinity weight) before considering global disable.
        """
        blacklist = []
        for (m, regime), rs in self._regime_stats.items():
            if m != model:
                continue
            rtotal = rs["wins"] + rs["losses"]
            if rtotal < MIN_REGIME_TRADES:
                continue
            regime_exp = rs.get("r_sum", 0.0) / rtotal
            if regime_exp < -0.10:
                blacklist.append((model, regime))
        return blacklist

    def get_models_to_disable(self) -> list[tuple[str, str]]:
        """Return list of (model_name, reason) that should be globally auto-disabled."""
        result = []
        for model in list(self._stats.keys()):
            should, reason = self.should_auto_disable(model)
            if should:
                result.append((model, reason))
        return result

    def reset(self, model: Optional[str] = None) -> None:
        with _lock:
            if model:
                self._stats.pop(model, None)
                self._regime_stats = {k: v for k, v in self._regime_stats.items() if k[0] != model}
            else:
                self._stats.clear()
                self._regime_stats.clear()
            self._save()


_tracker_instance: Optional[ModelPerformanceTracker] = None


def get_model_performance_tracker() -> ModelPerformanceTracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = ModelPerformanceTracker()
    return _tracker_instance
