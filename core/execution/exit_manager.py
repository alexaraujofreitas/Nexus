# ============================================================
# NEXUS TRADER — Exit Manager (Shared Exit Logic)
#
# Single source of truth for ALL exit logic:
#   • Static SL/TP
#   • Trailing stop
#   • Breakeven move at +1R
#   • Auto-partial at +1R (33% close)
#   • Time-based exit (max hold bars)
#
# Used by BOTH PaperExecutor and LiveExecutor to guarantee
# identical exit behavior.  LiveExecutor always runs full exit
# management.  PaperExecutor runs parity mode (static only) or
# full mode depending on config.
#
# The class is stateless — all state lives on the position dict.
# This keeps serialisation simple and restart-safe.
#
# Position dict contract (keys read/written by ExitManager):
#   READ:  side, entry_price, stop_loss, take_profit,
#          highest_price, lowest_price, bars_held,
#          _initial_risk, _breakeven_applied, _auto_partial_applied,
#          trailing_stop_pct, max_hold_bars
#   WRITE: stop_loss, highest_price, lowest_price, bars_held,
#          unrealized_pnl, _breakeven_applied
#
# Return value:
#   ExitAction dataclass with action type and metadata, or None.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExitAction:
    """Describes an exit action to be taken by the executor."""
    action: str       # "stop_loss" | "take_profit" | "trailing_stop" | "time_exit"
                      # | "auto_partial" | "breakeven_move"
    reason: str       # human-readable explanation
    # For auto_partial:
    reduce_pct: float = 0.0       # fraction to close (0.33 = 33%)
    new_stop_loss: float = 0.0    # SL to set after partial (breakeven)


class ExitManager:
    """
    Stateless exit logic engine.

    All state is stored on the position dict passed to check_exits().
    ExitManager reads and mutates the dict in place (high-water marks,
    breakeven flag, bars_held counter) — identical to the old
    PaperPosition.update() contract.

    Parameters
    ----------
    exit_config : dict
        Runtime exit configuration read from config.yaml:
          exit.mode            : "partial" | "full"
          exit.partial_pct     : float (0.33)
          exit.partial_r_trigger : float (1.0)
          trailing_stop.enabled : bool
          trailing_stop.pct     : float (distance from HWM)
          max_hold_bars         : int (0 = disabled)
    """

    def __init__(self, exit_config: Optional[dict] = None):
        cfg = exit_config or {}
        self.exit_mode       = cfg.get("exit.mode", "partial")
        self.partial_pct     = float(cfg.get("exit.partial_pct", 0.33))
        self.partial_r_trigger = float(cfg.get("exit.partial_r_trigger", 1.0))
        self.trailing_enabled = bool(cfg.get("trailing_stop.enabled", False))
        self.trailing_pct    = float(cfg.get("trailing_stop.pct", 0.0))
        self.default_max_hold = int(cfg.get("max_hold_bars", 0))

    def check_exits(
        self,
        pos: dict,
        current_price: float,
        *,
        parity_mode: bool = False,
    ) -> Optional[ExitAction]:
        """
        Evaluate all exit conditions for *pos* at *current_price*.

        Mutates pos in place:
          - bars_held incremented
          - highest_price / lowest_price updated
          - stop_loss updated (trailing, breakeven)
          - unrealized_pnl updated
          - _breakeven_applied set when triggered

        Parameters
        ----------
        pos : dict
            Mutable position dict with the keys listed in the module docstring.
        current_price : float
            Latest market price for the position's symbol.
        parity_mode : bool
            When True, only check static SL/TP (no trailing/breakeven/partial/time).
            Used by PaperExecutor in BACKTEST_PARITY_WITH_AI mode.

        Returns
        -------
        ExitAction or None
            If an exit should occur, returns the action. None means hold.
        """
        side = pos["side"]
        entry = pos["entry_price"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]

        # ── Increment counters ──────────────────────────────────
        pos["bars_held"] = pos.get("bars_held", 0) + 1

        # ── Update high/low water marks ─────────────────────────
        if side == "buy":
            pos["highest_price"] = max(pos.get("highest_price", entry), current_price)
        else:
            pos["lowest_price"] = min(pos.get("lowest_price", entry), current_price)

        # ── Unrealized P&L ──────────────────────────────────────
        if side == "buy":
            unrealized = (current_price - entry) / entry * 100.0 if entry > 0 else 0.0
        else:
            unrealized = (entry - current_price) / entry * 100.0 if entry > 0 else 0.0
        pos["unrealized_pnl"] = round(unrealized, 4)

        # ══════════════════════════════════════════════════════════
        # PARITY MODE — static SL/TP only (match backtest exactly)
        # ══════════════════════════════════════════════════════════
        if parity_mode:
            return self._check_static_sl_tp(pos, current_price)

        # ══════════════════════════════════════════════════════════
        # FULL EXIT MANAGEMENT
        # ══════════════════════════════════════════════════════════

        # 1. Time-based exit
        max_bars = pos.get("max_hold_bars", self.default_max_hold)
        if max_bars > 0 and pos["bars_held"] >= max_bars:
            return ExitAction(
                action="time_exit",
                reason=f"Max hold bars reached ({pos['bars_held']} >= {max_bars})",
            )

        # 2. Trailing stop update (mutates pos["stop_loss"])
        trail_pct = pos.get("trailing_stop_pct", self.trailing_pct)
        if trail_pct > 0:
            if side == "buy":
                trail_sl = pos["highest_price"] * (1.0 - trail_pct)
                if trail_sl > pos["stop_loss"]:
                    pos["stop_loss"] = trail_sl
            else:
                trail_sl = pos["lowest_price"] * (1.0 + trail_pct)
                if trail_sl < pos["stop_loss"]:
                    pos["stop_loss"] = trail_sl

        # 3. Breakeven move at +1R
        initial_risk = pos.get("_initial_risk", 0.0)
        if not pos.get("_breakeven_applied", False) and initial_risk > 0:
            if side == "buy":
                unrealized_r = (current_price - entry) / initial_risk
                if unrealized_r >= 1.0 and entry > pos["stop_loss"]:
                    pos["stop_loss"] = entry
                    pos["_breakeven_applied"] = True
            else:
                unrealized_r = (entry - current_price) / initial_risk
                if unrealized_r >= 1.0 and entry < pos["stop_loss"]:
                    pos["stop_loss"] = entry
                    pos["_breakeven_applied"] = True

        # 4. Auto-partial at +1R (before SL/TP check so partial fires first)
        if (
            self.exit_mode == "partial"
            and not pos.get("_auto_partial_applied", False)
            and initial_risk > 0
        ):
            if side == "buy":
                ur = (current_price - entry) / initial_risk
            else:
                ur = (entry - current_price) / initial_risk
            if ur >= self.partial_r_trigger:
                return ExitAction(
                    action="auto_partial",
                    reason=f"Auto-partial at +{ur:.2f}R (trigger {self.partial_r_trigger}R)",
                    reduce_pct=self.partial_pct,
                    new_stop_loss=entry,  # move to breakeven
                )

        # 5. Static SL/TP check (with potentially updated SL from trailing/breakeven)
        return self._check_static_sl_tp(pos, current_price)

    # ── Helpers ─────────────────────────────────────────────────

    def _check_static_sl_tp(self, pos: dict, price: float) -> Optional[ExitAction]:
        """
        Check static stop-loss and take-profit.

        SL=0 or TP=0 means "unset" — the check is skipped for that level.
        This is critical for orphaned positions created during reconciliation
        (which have SL=0, TP=0 and require manual intervention to set levels).
        """
        side = pos["side"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]

        if side == "buy":
            if sl > 0 and price <= sl:
                return ExitAction(action="stop_loss", reason=f"SL hit at {price:.6g} <= {sl:.6g}")
            if tp > 0 and price >= tp:
                return ExitAction(action="take_profit", reason=f"TP hit at {price:.6g} >= {tp:.6g}")
        else:
            if sl > 0 and price >= sl:
                return ExitAction(action="stop_loss", reason=f"SL hit at {price:.6g} >= {sl:.6g}")
            if tp > 0 and price <= tp:
                return ExitAction(action="take_profit", reason=f"TP hit at {price:.6g} <= {tp:.6g}")
        return None
