# ============================================================
# NEXUS TRADER — Proposal ROI Tracker  (Wave 2 Sub-wave 2.3)
#
# Measures the realized performance impact of applied TuningProposals.
#
# Design constraints:
#   - Proposals remain MANUAL-ONLY (operator applies; tracker measures)
#   - Min 30 trades post-application before verdict is computed
#   - Pre-metrics must be supplied at application time (from live performance
#     data); they are NOT computed retroactively
#   - Thread-safe: RLock protects in-memory post-trade buffers
#   - DB writes on record_application() and _measure() only — no per-trade writes
#
# Lifecycle:
#   record_application()  →  status = EVALUATING
#   on_trade_closed() × N →  post_trades accumulates in memory
#   when post_trades >= min_trades_threshold
#                         →  _measure() called automatically
#                         →  status = MEASURED, verdict stored in DB
#
# Verdict logic:
#   IMPROVED  — delta_pf > +0.10 AND delta_win_rate > +2.0 pp
#   DEGRADED  — delta_pf < -0.10 OR  delta_win_rate < -2.0 pp
#   NEUTRAL   — otherwise
#
# Usage:
#   from core.analysis.proposal_roi_tracker import proposal_roi_tracker
#   proposal_roi_tracker.record_application("P-042", pre_metrics={...})
#   # ... trades close ...
#   proposal_roi_tracker.on_trade_closed(trade_dict)
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Minimum post-application trades before a verdict can be rendered
MIN_TRADES_DEFAULT = 30

# Verdict thresholds
_IMPROVE_DELTA_PF  =  0.10   # profit factor must improve by this much
_IMPROVE_DELTA_WR  =  2.0    # win rate (pp) must improve by this much
_DEGRADE_DELTA_PF  = -0.10   # profit factor degraded by this → DEGRADED
_DEGRADE_DELTA_WR  = -2.0    # win rate (pp) worsened by this → DEGRADED


class ProposalROITracker:
    """
    Tracks the realized performance impact of applied TuningProposals.

    One row per applied proposal in the ``tuning_proposal_outcomes`` table.
    Post-application trades are accumulated in-memory for efficiency;
    DB is updated only when a proposal is first recorded and when
    the verdict is measured (threshold reached).

    Parameters
    ----------
    min_trades : int
        Global default minimum post-application trades before verdict.
        Can be overridden per proposal in ``record_application()``.
    """

    def __init__(self, min_trades: int = MIN_TRADES_DEFAULT):
        self._min_trades   = min_trades
        self._lock         = threading.RLock()
        # In-memory buffers: proposal_id → list of (pnl, is_win, avg_r) tuples
        self._post_trades: Dict[str, List[dict]] = {}

    # ── Public API ────────────────────────────────────────────

    def record_application(
        self,
        proposal_id:   str,
        pre_metrics:   dict,
        min_trades:    Optional[int]  = None,
        notes:         Optional[str]  = None,
    ) -> bool:
        """
        Record that a TuningProposal has been manually applied.

        Must be called by the operator immediately after applying a proposal.
        Pre-application metrics are supplied here; they are NOT inferred.

        Parameters
        ----------
        proposal_id : str
            Matches StrategyTuningProposal.proposal_id (e.g. "P-042").
        pre_metrics : dict
            Must contain at least one of:
              "win_rate"      — float, percentage (e.g. 50.3)
              "profit_factor" — float (e.g. 1.47)
              "avg_r"         — float, average R per trade (e.g. 0.22)
            "trade_count" — int, number of trades in pre-window
        min_trades : int, optional
            Override the global threshold for this proposal.
        notes : str, optional
            Free-text annotation (e.g. which parameter was changed).

        Returns
        -------
        bool
            True if successfully recorded; False if proposal_id already exists
            or a DB error occurred.
        """
        threshold = min_trades if min_trades is not None else self._min_trades

        try:
            from core.database.engine  import Session
            from core.database.models  import TuningProposalOutcome

            with Session() as sess:
                existing = sess.query(TuningProposalOutcome).filter_by(
                    proposal_id=proposal_id
                ).first()
                if existing is not None:
                    logger.warning(
                        "ProposalROITracker: proposal %s already recorded — "
                        "call record_application() only once per proposal.",
                        proposal_id,
                    )
                    return False

                row = TuningProposalOutcome(
                    proposal_id          = proposal_id,
                    status               = "EVALUATING",
                    min_trades_threshold = threshold,
                    pre_trades           = int(pre_metrics.get("trade_count", 0)),
                    pre_win_rate         = pre_metrics.get("win_rate"),
                    pre_profit_factor    = pre_metrics.get("profit_factor"),
                    pre_avg_r            = pre_metrics.get("avg_r"),
                    notes                = notes,
                    applied_at           = datetime.now(timezone.utc),
                )
                sess.add(row)
                sess.commit()

            with self._lock:
                self._post_trades[proposal_id] = []

            logger.info(
                "ProposalROITracker: tracking %s — need %d post-application trades.",
                proposal_id, threshold,
            )
            return True

        except Exception as exc:
            logger.error("ProposalROITracker.record_application failed: %s", exc)
            return False

    def on_trade_closed(self, trade: dict) -> None:
        """
        Notify the tracker that a trade has closed.

        Attributes the trade to ALL proposals currently in EVALUATING status.
        Call this from paper_executor._close_position() after the trade record
        is persisted (wiring is deferred — see CLAUDE.md pending actions).

        Parameters
        ----------
        trade : dict
            Must contain: "pnl" (float), "pnl_pct" (float, optional).
            Optionally "avg_r" or "r_multiple" (float).
        """
        with self._lock:
            active = list(self._post_trades.keys())

        if not active:
            return  # fast path — nothing being tracked

        pnl     = float(trade.get("pnl", 0.0))
        pnl_pct = float(trade.get("pnl_pct", 0.0))
        avg_r   = float(trade.get("avg_r") or trade.get("r_multiple") or 0.0)
        is_win  = pnl > 0.0

        record = {"pnl": pnl, "pnl_pct": pnl_pct, "avg_r": avg_r, "is_win": is_win}

        for pid in active:
            with self._lock:
                if pid in self._post_trades:
                    self._post_trades[pid].append(record)
                    count = len(self._post_trades[pid])

            # Check threshold outside the lock (minor TOCTOU acceptable here)
            threshold = self._get_threshold(pid)
            if threshold and count >= threshold:
                self._measure(pid)

    def get_status(self, proposal_id: str) -> dict:
        """
        Return the current tracking state for a proposal.

        Returns
        -------
        dict with keys:
            proposal_id, status, post_trades (in-memory), threshold,
            pre_metrics, post_metrics (None if not yet measured), verdict.
        Returns {"error": "not_found"} if proposal_id is unknown.
        """
        try:
            from core.database.engine import Session
            from core.database.models import TuningProposalOutcome

            with Session() as sess:
                row = sess.query(TuningProposalOutcome).filter_by(
                    proposal_id=proposal_id
                ).first()
                if row is None:
                    return {"error": "not_found"}

                with self._lock:
                    buffered = len(self._post_trades.get(proposal_id, []))

                return {
                    "proposal_id":    proposal_id,
                    "status":         row.status,
                    "threshold":      row.min_trades_threshold,
                    "post_trades_db": row.post_trades,
                    "post_trades_mem":buffered,
                    "pre_metrics": {
                        "trade_count":    row.pre_trades,
                        "win_rate":       row.pre_win_rate,
                        "profit_factor":  row.pre_profit_factor,
                        "avg_r":          row.pre_avg_r,
                    },
                    "post_metrics": {
                        "win_rate":       row.post_win_rate,
                        "profit_factor":  row.post_profit_factor,
                        "avg_r":          row.post_avg_r,
                    } if row.status == "MEASURED" else None,
                    "delta_win_rate": row.delta_win_rate,
                    "delta_pf":       row.delta_pf,
                    "verdict":        row.verdict,
                    "applied_at":     str(row.applied_at) if row.applied_at else None,
                    "measured_at":    str(row.measured_at) if row.measured_at else None,
                }
        except Exception as exc:
            logger.error("ProposalROITracker.get_status error: %s", exc)
            return {"error": str(exc)}

    def get_all_outcomes(self) -> list[dict]:
        """
        Return summary dicts for all tracked proposals, newest first.
        """
        try:
            from core.database.engine import Session
            from core.database.models import TuningProposalOutcome

            with Session() as sess:
                rows = sess.query(TuningProposalOutcome).order_by(
                    TuningProposalOutcome.applied_at.desc()
                ).all()

                return [
                    {
                        "proposal_id":    r.proposal_id,
                        "status":         r.status,
                        "post_trades":    r.post_trades,
                        "threshold":      r.min_trades_threshold,
                        "verdict":        r.verdict,
                        "delta_pf":       r.delta_pf,
                        "delta_win_rate": r.delta_win_rate,
                        "applied_at":     str(r.applied_at) if r.applied_at else None,
                        "measured_at":    str(r.measured_at) if r.measured_at else None,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.error("ProposalROITracker.get_all_outcomes error: %s", exc)
            return []

    # ── Private helpers ───────────────────────────────────────

    def _get_threshold(self, proposal_id: str) -> Optional[int]:
        """Read min_trades_threshold from DB (cached per proposal would be nice
        but DB round-trip is acceptable — this path runs infrequently)."""
        try:
            from core.database.engine import Session
            from core.database.models import TuningProposalOutcome

            with Session() as sess:
                row = sess.query(TuningProposalOutcome).filter_by(
                    proposal_id=proposal_id
                ).first()
                return row.min_trades_threshold if row else None
        except Exception:
            return None

    def _measure(self, proposal_id: str) -> None:
        """
        Compute post-application metrics and verdict, persist to DB.
        Clears the in-memory buffer for this proposal after measurement.
        """
        with self._lock:
            trades = list(self._post_trades.get(proposal_id, []))

        if not trades:
            return

        # Compute post-metrics from buffered trades
        wins           = [t for t in trades if t["is_win"]]
        losses         = [t for t in trades if not t["is_win"]]
        n              = len(trades)
        post_win_rate  = len(wins) / n * 100.0
        gross_profit   = sum(t["pnl"] for t in wins)
        gross_loss     = abs(sum(t["pnl"] for t in losses))
        post_pf        = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
        avg_rs         = [t["avg_r"] for t in trades if t["avg_r"] != 0.0]
        post_avg_r     = sum(avg_rs) / len(avg_rs) if avg_rs else None

        try:
            from core.database.engine import Session
            from core.database.models import TuningProposalOutcome

            with Session() as sess:
                row = sess.query(TuningProposalOutcome).filter_by(
                    proposal_id=proposal_id
                ).first()
                if row is None:
                    return

                # Deltas (None if pre-metric was not provided)
                delta_wr = (
                    round(post_win_rate - row.pre_win_rate, 4)
                    if row.pre_win_rate is not None else None
                )
                delta_pf = (
                    round(post_pf - row.pre_profit_factor, 4)
                    if row.pre_profit_factor is not None else None
                )

                # Verdict
                verdict = _compute_verdict(delta_pf, delta_wr)

                row.status            = "MEASURED"
                row.post_trades       = n
                row.post_win_rate     = round(post_win_rate, 4)
                row.post_profit_factor= round(post_pf, 4)
                row.post_avg_r        = round(post_avg_r, 4) if post_avg_r is not None else None
                row.delta_win_rate    = delta_wr
                row.delta_pf          = delta_pf
                row.verdict           = verdict
                row.measured_at       = datetime.now(timezone.utc)
                sess.commit()

            # Clear in-memory buffer
            with self._lock:
                self._post_trades.pop(proposal_id, None)

            logger.info(
                "ProposalROITracker: %s MEASURED — %d trades, verdict=%s, "
                "delta_pf=%s, delta_wr=%s",
                proposal_id, n, verdict, delta_pf, delta_wr,
            )

        except Exception as exc:
            logger.error("ProposalROITracker._measure failed for %s: %s", proposal_id, exc)


def _compute_verdict(
    delta_pf: Optional[float],
    delta_wr: Optional[float],
) -> str:
    """
    Derive IMPROVED / NEUTRAL / DEGRADED from deltas.

    IMPROVED  : delta_pf > +0.10 AND delta_wr > +2.0 pp
    DEGRADED  : delta_pf < -0.10 OR  delta_wr < -2.0 pp
    NEUTRAL   : otherwise (including when both deltas are None)
    """
    if delta_pf is None and delta_wr is None:
        return "NEUTRAL"

    pf_ok  = delta_pf is not None and delta_pf > _IMPROVE_DELTA_PF
    wr_ok  = delta_wr is not None and delta_wr > _IMPROVE_DELTA_WR
    pf_bad = delta_pf is not None and delta_pf < _DEGRADE_DELTA_PF
    wr_bad = delta_wr is not None and delta_wr < _DEGRADE_DELTA_WR

    if pf_ok and wr_ok:
        return "IMPROVED"
    if pf_bad or wr_bad:
        return "DEGRADED"
    return "NEUTRAL"


# Module-level singleton — import and use directly
proposal_roi_tracker = ProposalROITracker()
