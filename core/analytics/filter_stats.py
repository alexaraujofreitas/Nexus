"""
FilterStatsTracker — instrumentation for pre-scan filters.

Tracks, persists, and exposes:
  - How many candidates each filter blocked vs accepted
  - Breakdown by symbol and by regime
  - Quality proxy: avg confluence_score of blocked vs accepted trades
    (requires post-hoc enrichment via record_trade_outcome())

Intent: after 50+ trades, read data/filter_stats.json to determine whether:
  1. The filters are actually blocking meaningful volume
  2. Blocked trades had lower quality than accepted trades
  3. Specific symbols or regimes are disproportionately blocked

Ablation discipline:
  To test a filter's impact:
    1. Note current filter_stats baseline
    2. Set filters.time_of_day.enabled=false (or volatility.enabled=false)
    3. Run 50 trades; compare avg_r_accepted vs avg_r_hypothetical_blocked
    4. If blocked group's avg_r is similar to accepted, the filter has no edge
    5. If blocked group's avg_r is lower, the filter is filtering correctly

Usage:
    from core.analytics.filter_stats import get_filter_stats_tracker
    tracker = get_filter_stats_tracker()
    tracker.record_filter_result(
        filter_name="time_of_day",
        symbol="BTC/USDT",
        regime="bull_trend",
        passed=False,
        reason="UTC 23:xx outside 12-21 window",
        confluence_score=0.52,  # optional — pre-computed score when available
    )
"""
from __future__ import annotations
import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATS_PATH = Path(__file__).parent.parent.parent / "data" / "filter_stats.json"
_lock = threading.Lock()


class FilterStatsTracker:
    """Thread-safe counter for pre-scan filter outcomes."""

    FILTER_NAMES = ("time_of_day", "volatility")

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _empty_filter(self) -> dict:
        return {
            "blocked": 0,
            "accepted": 0,
            "blocked_by_symbol": {},
            "blocked_by_regime": {},
            "accepted_score_sum": 0.0,
            "accepted_score_count": 0,
            "blocked_score_sum": 0.0,
            "blocked_score_count": 0,
            # Outcome enrichment — added via record_trade_outcome()
            "accepted_r_sum": 0.0,
            "accepted_r_count": 0,
        }

    def _load(self) -> None:
        if not _STATS_PATH.exists():
            return
        try:
            self._data = json.loads(_STATS_PATH.read_text())
        except Exception as exc:
            logger.warning("FilterStatsTracker: load failed: %s", exc)

    def _save(self) -> None:
        try:
            _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATS_PATH.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            logger.warning("FilterStatsTracker: save failed: %s", exc)

    def record_filter_result(
        self,
        filter_name: str,
        symbol: str,
        regime: str,
        passed: bool,
        reason: str = "",
        confluence_score: Optional[float] = None,
    ) -> None:
        """Record a single filter pass/fail result."""
        with _lock:
            if filter_name not in self._data:
                self._data[filter_name] = self._empty_filter()
            f = self._data[filter_name]

            if passed:
                f["accepted"] += 1
                if confluence_score is not None:
                    f["accepted_score_sum"] += float(confluence_score)
                    f["accepted_score_count"] += 1
            else:
                f["blocked"] += 1
                # By symbol
                if symbol not in f["blocked_by_symbol"]:
                    f["blocked_by_symbol"][symbol] = 0
                f["blocked_by_symbol"][symbol] += 1
                # By regime
                regime_key = (regime or "unknown").lower()
                if regime_key not in f["blocked_by_regime"]:
                    f["blocked_by_regime"][regime_key] = 0
                f["blocked_by_regime"][regime_key] += 1

                if confluence_score is not None:
                    f["blocked_score_sum"] += float(confluence_score)
                    f["blocked_score_count"] += 1

            self._save()

    def record_trade_outcome(self, filter_name: str, realized_r: float) -> None:
        """
        Enrich accepted trades with realized R to build a quality proxy.
        Called from paper_executor when a trade closes (for trades that
        passed the given filter). This provides the evidence path for
        'accepted trades produce positive R'.
        """
        with _lock:
            if filter_name not in self._data:
                return
            f = self._data[filter_name]
            f["accepted_r_sum"] += float(realized_r)
            f["accepted_r_count"] += 1
            self._save()

    def get_summary(self, filter_name: str) -> dict:
        """Return a human-readable summary dict for the given filter."""
        f = self._data.get(filter_name)
        if not f:
            return {"filter": filter_name, "no_data": True}

        blocked = f["blocked"]
        accepted = f["accepted"]
        total = blocked + accepted
        block_rate = round(blocked / total, 3) if total > 0 else None

        avg_accepted_score = (
            round(f["accepted_score_sum"] / f["accepted_score_count"], 3)
            if f["accepted_score_count"] > 0 else None
        )
        avg_blocked_score = (
            round(f["blocked_score_sum"] / f["blocked_score_count"], 3)
            if f["blocked_score_count"] > 0 else None
        )
        avg_accepted_r = (
            round(f["accepted_r_sum"] / f["accepted_r_count"], 3)
            if f["accepted_r_count"] > 0 else None
        )

        return {
            "filter": filter_name,
            "blocked": blocked,
            "accepted": accepted,
            "total_seen": total,
            "block_rate_pct": round(block_rate * 100, 1) if block_rate else None,
            "avg_accepted_confluence_score": avg_accepted_score,
            "avg_blocked_confluence_score": avg_blocked_score,
            "score_delta_accepted_minus_blocked": (
                round(avg_accepted_score - avg_blocked_score, 3)
                if avg_accepted_score and avg_blocked_score else None
            ),
            "avg_accepted_realized_r": avg_accepted_r,
            "top_blocked_symbols": sorted(
                f["blocked_by_symbol"].items(), key=lambda x: -x[1]
            )[:5],
            "top_blocked_regimes": sorted(
                f["blocked_by_regime"].items(), key=lambda x: -x[1]
            )[:5],
        }

    def get_all_summaries(self) -> list[dict]:
        return [self.get_summary(name) for name in self._data]

    def reset(self, filter_name: Optional[str] = None) -> None:
        with _lock:
            if filter_name:
                self._data.pop(filter_name, None)
            else:
                self._data.clear()
            self._save()


_instance: Optional[FilterStatsTracker] = None


def get_filter_stats_tracker() -> FilterStatsTracker:
    global _instance
    if _instance is None:
        _instance = FilterStatsTracker()
    return _instance
