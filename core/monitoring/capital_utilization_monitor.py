# ============================================================
# NEXUS TRADER — Capital Utilization Monitor
#
# Lightweight observability facade over PaperExecutor state.
# Answers the question: "how much of our capital is actually
# deployed right now, and why is it what it is?"
#
# Design constraints:
#   - Read-only: zero mutations to executor or risk state
#   - No DB queries: reads in-memory executor state only
#   - Stateless: call get_snapshot() any time; no init needed
#   - Safe from any thread: PaperExecutor properties are safe to read
#
# Key metrics exposed:
#   utilization_pct        float  — locked capital / total capital × 100
#   locked_usdt            float  — sum of open position.size_usdt
#   available_usdt         float  — capital not locked in positions
#   open_positions         int    — total open position count
#   open_symbols           list   — unique symbols with open positions
#   portfolio_heat_pct     float  — committed risk as % of capital (mirrors RiskGate calc)
#   daily_loss_limit_hit   bool   — kill switch active
#   daily_loss_limit_pct   float  — configured threshold
#   today_realized_pnl     float  — realized P&L today (UTC)
#   today_pnl_pct          float  — today P&L / initial capital × 100
#   limiting_factor        str    — primary reason utilization is below 100%
# ============================================================
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.execution.paper_executor import PaperExecutor

logger = logging.getLogger(__name__)

# Factors that cap capital deployment — ordered by priority for display
_FACTORS = [
    "daily_loss_limit",      # kill switch active today
    "drawdown_circuit",      # drawdown >= 10%
    "portfolio_heat",        # heat cap reached
    "max_concurrent",        # position count cap reached
    "no_signals",            # nothing approved by IDSS this cycle
    "none",                  # utilization is healthy / no constraint active
]


class CapitalUtilizationMonitor:
    """
    Stateless observer for capital deployment efficiency.

    Usage::
        monitor = CapitalUtilizationMonitor()
        snapshot = monitor.get_snapshot(paper_executor)

    All fields in the snapshot dict are safe to display in dashboards,
    push to notifications, or log for observability.
    """

    def get_snapshot(self, executor: "PaperExecutor") -> dict:
        """
        Compute a full capital utilization snapshot.

        Parameters
        ----------
        executor : PaperExecutor
            The live paper executor instance.

        Returns
        -------
        dict with keys described in module docstring.
        """
        try:
            return self._compute(executor)
        except Exception as exc:
            logger.warning("CapitalUtilizationMonitor: snapshot failed — %s", exc)
            return self._empty_snapshot()

    def _compute(self, executor: "PaperExecutor") -> dict:
        capital        = executor._capital
        initial        = executor._initial_capital or capital or 1.0
        peak           = executor._peak_capital

        # ── Position inventory ────────────────────────────────────
        all_positions  = [
            p
            for pos_list in executor._positions.values()
            for p in pos_list
        ]
        locked_usdt    = sum(p.size_usdt for p in all_positions)
        available_usdt = max(0.0, capital - locked_usdt)
        open_count     = len(all_positions)
        open_symbols   = sorted({p.symbol for p in all_positions})

        utilization_pct = (locked_usdt / capital * 100.0) if capital > 0 else 0.0

        # ── Portfolio heat ────────────────────────────────────────
        # Mirrors the heat calculation in RiskGate.validate() so both
        # systems report the same number.
        heat_usdt = 0.0
        for p in all_positions:
            if p.stop_loss and p.stop_loss > 0 and p.entry_price > 0:
                stop_dist = abs(p.entry_price - p.stop_loss)
                qty = p.size_usdt / p.entry_price
                heat_usdt += stop_dist * qty
        portfolio_heat_pct = (heat_usdt / capital * 100.0) if capital > 0 else 0.0

        # ── Daily loss limit ──────────────────────────────────────
        daily_limit_hit  = executor.is_daily_limit_hit
        daily_limit_pct  = executor.daily_loss_limit_pct
        today_pnl        = executor.today_realized_pnl
        today_pnl_pct    = (today_pnl / initial * 100.0) if initial > 0 else 0.0

        # ── Drawdown ──────────────────────────────────────────────
        drawdown_pct     = executor.drawdown_pct
        dd_breaker_pct   = getattr(executor, "_dd_circuit_breaker_pct", 10.0)
        dd_circuit_on    = drawdown_pct >= dd_breaker_pct

        # ── Portfolio heat cap (from live config) ─────────────────
        try:
            from config.settings import settings as _s
            max_heat = float(_s.get("risk_engine.portfolio_heat_max_pct", 0.04)) * 100.0
        except Exception:
            max_heat = 4.0
        heat_capped = portfolio_heat_pct >= max_heat

        # ── Max concurrent positions cap ──────────────────────────
        try:
            from config.settings import settings as _s
            max_concurrent = int(_s.get("risk.max_concurrent_positions", 10))
        except Exception:
            max_concurrent = 10
        at_max_concurrent = open_count >= max_concurrent

        # ── Determine primary limiting factor ─────────────────────
        if daily_limit_hit:
            limiting_factor = "daily_loss_limit"
        elif dd_circuit_on:
            limiting_factor = "drawdown_circuit"
        elif heat_capped:
            limiting_factor = "portfolio_heat"
        elif at_max_concurrent:
            limiting_factor = "max_concurrent"
        elif open_count == 0 and utilization_pct < 1.0:
            limiting_factor = "no_signals"
        else:
            limiting_factor = "none"

        # ── Headroom — max additional USDT deployable ─────────────
        # Bounded by both available capital and heat headroom
        heat_headroom_usdt = max(
            0.0, ((max_heat / 100.0) * capital) - heat_usdt
        ) if capital > 0 else 0.0
        deployable_usdt = min(available_usdt, heat_headroom_usdt)

        return {
            # Core utilization
            "capital_usdt":          round(capital, 2),
            "initial_capital_usdt":  round(initial, 2),
            "peak_capital_usdt":     round(peak, 2),
            "locked_usdt":           round(locked_usdt, 2),
            "available_usdt":        round(available_usdt, 2),
            "utilization_pct":       round(utilization_pct, 2),
            "deployable_usdt":       round(deployable_usdt, 2),
            # Position inventory
            "open_positions":        open_count,
            "max_concurrent":        max_concurrent,
            "open_symbols":          open_symbols,
            # Risk exposure
            "portfolio_heat_pct":    round(portfolio_heat_pct, 4),
            "max_heat_pct":          round(max_heat, 2),
            "drawdown_pct":          round(drawdown_pct, 4),
            "drawdown_circuit_on":   dd_circuit_on,
            # Daily loss limit
            "daily_loss_limit_hit":  daily_limit_hit,
            "daily_loss_limit_pct":  daily_limit_pct,
            "today_realized_pnl":    round(today_pnl, 2),
            "today_pnl_pct":         round(today_pnl_pct, 4),
            # Diagnostic
            "limiting_factor":       limiting_factor,
        }

    @staticmethod
    def _empty_snapshot() -> dict:
        """Returned when executor state cannot be read."""
        return {
            "capital_usdt":          0.0,
            "initial_capital_usdt":  0.0,
            "peak_capital_usdt":     0.0,
            "locked_usdt":           0.0,
            "available_usdt":        0.0,
            "utilization_pct":       0.0,
            "deployable_usdt":       0.0,
            "open_positions":        0,
            "max_concurrent":        10,
            "open_symbols":          [],
            "portfolio_heat_pct":    0.0,
            "max_heat_pct":          4.0,
            "drawdown_pct":          0.0,
            "drawdown_circuit_on":   False,
            "daily_loss_limit_hit":  False,
            "daily_loss_limit_pct":  2.0,
            "today_realized_pnl":    0.0,
            "today_pnl_pct":         0.0,
            "limiting_factor":       "no_signals",
        }


# Module-level singleton — import and use directly
capital_utilization_monitor = CapitalUtilizationMonitor()
