# ============================================================
# NEXUS TRADER — Paper Executor (Phase 2)
#
# Simulates order execution against live ticker prices.
# Tracks paper positions in-memory and in the database.
# Called by the order router when mode=paper.
#
# Enhanced features:
# - Dynamic stop adjustment via adjust_stop()
# - Partial close via partial_close()
# - Subscribes to POSITION_MONITOR_UPDATED events
# - Full trade execution verification logging (risk_amount_usdt, expected_rr,
#   symbol_weight, adjusted_score, realized_r) — Session 26
# NOTE: BTC-first size multiplier removed in Session 25.
#       SymbolAllocator is the sole per-symbol allocation mechanism.
# ============================================================
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.meta_decision.order_candidate import OrderCandidate
from core.event_bus import bus, Topics

# File used to persist open positions across restarts
_OPEN_POSITIONS_FILE = Path(__file__).parent.parent.parent / "data" / "open_positions.json"

# CDA observability log — append-only JSONL; one record per opened trade.
# Each record: ts, symbol, side, base_position_size_usdt, cda_multiplier,
#              adjusted_position_size_usdt, cda_tier, regime, score.
# This file is read by the CPS validation script and the v0.4 report generator.
_CDA_OBS_FILE = Path(__file__).parent.parent.parent / "data" / "cda_observations.jsonl"

logger = logging.getLogger(__name__)


class PaperPosition:
    """An open paper trading position."""

    def __init__(
        self,
        symbol:       str,
        side:         str,
        entry_price:  float,
        quantity:     float,
        stop_loss:    float,
        take_profit:  float,
        size_usdt:    float,
        score:        float,
        rationale:    str,
        regime:       str = "",
        models_fired: Optional[list] = None,
        timeframe:    str = "",
        opened_at:    Optional[datetime] = None,
    ):
        self.symbol       = symbol
        self.side         = side
        self.entry_price  = entry_price
        self.quantity     = quantity
        self.stop_loss    = stop_loss
        self.take_profit  = take_profit
        self.size_usdt    = size_usdt
        # entry_size_usdt is the ORIGINAL position size at open — it never changes,
        # even if partial_close() later reduces size_usdt.  Stored so that trade
        # records can always show the true capital that was deployed at entry.
        self.entry_size_usdt = size_usdt
        self.score        = score
        self.rationale    = rationale
        self.regime       = regime
        self.models_fired = models_fired or []
        self.timeframe    = timeframe
        self.opened_at    = opened_at or datetime.utcnow()
        self.current_price  = entry_price
        self.unrealized_pnl = 0.0
        # Exit logic attributes
        self.trailing_stop_pct: float = 0.0   # 0 = disabled
        self.max_hold_bars: int = 0            # 0 = no limit
        self.bars_held: int = 0
        self.highest_price: float = entry_price  # for trailing stop tracking
        self.lowest_price: float = entry_price
        self._breakeven_applied: bool = False  # True once SL moves to entry on +1R
        self._initial_risk: float = abs(entry_price - stop_loss)  # original risk for R calc

    def update(self, current_price: float) -> Optional[str]:
        """
        Update position with new price. Returns exit reason if
        stop/take-profit was hit, else None.
        """
        self.current_price = current_price
        self.bars_held += 1

        # Track high/low water marks for trailing stops
        if self.side == "buy":
            self.highest_price = max(self.highest_price, current_price)
        else:
            self.lowest_price = min(self.lowest_price, current_price)

        # Time-based exit
        if self.max_hold_bars > 0 and self.bars_held >= self.max_hold_bars:
            return "time_exit"

        # Trailing stop update
        if self.trailing_stop_pct > 0:
            if self.side == "buy":
                trail_sl = self.highest_price * (1.0 - self.trailing_stop_pct)
                if trail_sl > self.stop_loss:
                    self.stop_loss = trail_sl
            else:
                trail_sl = self.lowest_price * (1.0 + self.trailing_stop_pct)
                if trail_sl < self.stop_loss:
                    self.stop_loss = trail_sl

        # ── 1R breakeven move ──────────────────────────────────────
        # When unrealized profit reaches +1R (based on initial risk),
        # move stop-loss to entry once. Only moves SL in the protective
        # direction — never overwrites a trailing stop that's already tighter.
        if not self._breakeven_applied and self._initial_risk > 0:
            if self.side == "buy":
                unrealized_r = (current_price - self.entry_price) / self._initial_risk
                if unrealized_r >= 1.0 and self.entry_price > self.stop_loss:
                    self.stop_loss = self.entry_price
                    self._breakeven_applied = True
            else:
                unrealized_r = (self.entry_price - current_price) / self._initial_risk
                if unrealized_r >= 1.0 and self.entry_price < self.stop_loss:
                    self.stop_loss = self.entry_price
                    self._breakeven_applied = True

        if self.side == "buy":
            self.unrealized_pnl = (current_price - self.entry_price) / self.entry_price * 100
            if current_price <= self.stop_loss:
                return "stop_loss"
            if current_price >= self.take_profit:
                return "take_profit"
        else:
            self.unrealized_pnl = (self.entry_price - current_price) / self.entry_price * 100
            if current_price >= self.stop_loss:
                return "stop_loss"
            if current_price <= self.take_profit:
                return "take_profit"
        return None

    def to_dict(self) -> dict:
        return {
            "symbol":           self.symbol,
            "side":             self.side,
            "entry_price":      self.entry_price,
            "current_price":    self.current_price,
            "quantity":         self.quantity,
            "stop_loss":        self.stop_loss,
            "take_profit":      self.take_profit,
            "size_usdt":        self.size_usdt,
            "entry_size_usdt":  self.entry_size_usdt,
            "unrealized_pnl":   round(self.unrealized_pnl, 4),
            "score":            self.score,
            "rationale":        self.rationale,
            "regime":           self.regime,
            "models_fired":     self.models_fired,
            "timeframe":        self.timeframe,
            "opened_at":        self.opened_at.isoformat(),
        }


class PaperExecutor:
    """
    Simulates paper trading execution.
    - Fills limit orders at the specified price when reached
    - Tracks positions in-memory
    - Monitors stops and take-profits on every tick
    - Supports dynamic stop adjustment and partial close
    - Listens to POSITION_MONITOR_UPDATED for external position actions
    - Applies BTC-first size multiplier to order sizing
    """

    _SLIPPAGE_MIN = 0.0001   # 0.01% minimum market slippage
    _SLIPPAGE_MAX = 0.0005   # 0.05% maximum market slippage
    _SPREAD_HALF  = 0.0002   # 0.02% half-spread (4bps total spread)

    def __init__(self, initial_capital_usdt: float = 100_000.0):
        self._initial_capital = initial_capital_usdt
        self._capital         = initial_capital_usdt
        self._positions:      dict[str, list[PaperPosition]] = {}  # symbol → list of positions
        self._max_positions_per_symbol: int = 10
        self._closed_trades:  list[dict] = []
        self._peak_capital    = initial_capital_usdt
        # ── Production safeguards ────────────────────────────────────
        self._dd_circuit_breaker_pct: float = 10.0   # Hard block at 10% drawdown
        # Restore any open positions that survived a restart (reads capital from JSON)
        self._load_open_positions()
        # Restore closed-trade history from SQLite — MUST run after _load_open_positions()
        # so the SQLite-replayed equity (authoritative) overwrites the JSON capital.
        # This prevents the stale JSON capital from masking real closed-trade P&L.
        self._load_history()
        # Subscribe to position monitoring events
        bus.subscribe(Topics.POSITION_MONITOR_UPDATED, self._on_position_monitor)

    # ── Open-position persistence ───────────────────────────────

    def _save_open_positions(self) -> None:
        """Write current open positions to JSON so they survive restarts."""
        try:
            _OPEN_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "capital":      self._capital,
                "peak_capital": self._peak_capital,
                "positions":    [p.to_dict() for pos_list in self._positions.values() for p in pos_list],
            }
            _OPEN_POSITIONS_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")
        except Exception as exc:
            logger.warning("PaperExecutor: could not save open positions: %s", exc)

    def _load_open_positions(self) -> None:
        """Restore open positions from JSON file on startup."""
        if not _OPEN_POSITIONS_FILE.exists():
            return
        try:
            data = json.loads(_OPEN_POSITIONS_FILE.read_text(encoding="utf-8"))
            restored = 0
            for pd in data.get("positions", []):
                symbol = pd.get("symbol")
                if not symbol:
                    continue
                # Allow multiple positions per symbol (up to _max_positions_per_symbol)
                existing = self._positions.get(symbol, [])
                if len(existing) >= self._max_positions_per_symbol:
                    logger.warning("PaperExecutor: max positions (%d) reached for %s during restore",
                                   self._max_positions_per_symbol, symbol)
                    continue
                opened_at_raw = pd.get("opened_at", "")
                try:
                    opened_at_dt = datetime.fromisoformat(opened_at_raw) if opened_at_raw else datetime.utcnow()
                except (ValueError, TypeError):
                    opened_at_dt = datetime.utcnow()
                pos = PaperPosition(
                    symbol       = symbol,
                    side         = pd.get("side", "buy"),
                    entry_price  = float(pd.get("entry_price", 0)),
                    quantity     = float(pd.get("quantity", 0)),
                    stop_loss    = float(pd.get("stop_loss", 0)),
                    take_profit  = float(pd.get("take_profit", 0)),
                    size_usdt    = float(pd.get("size_usdt", 0)),
                    score        = float(pd.get("score", 0)),
                    rationale    = pd.get("rationale", ""),
                    regime       = pd.get("regime", ""),
                    models_fired = pd.get("models_fired", []),
                    timeframe    = pd.get("timeframe", ""),
                    opened_at    = opened_at_dt,
                )
                pos.unrealized_pnl   = float(pd.get("unrealized_pnl", 0))
                # Restore entry_size_usdt — fall back to size_usdt for positions
                # saved before this field was added (backward compatible).
                pos.entry_size_usdt  = float(
                    pd.get("entry_size_usdt") or pd.get("size_usdt", pos.size_usdt)
                )
                self._positions.setdefault(symbol, []).append(pos)
                restored += 1
            # Always restore capital from the JSON file — it is the authoritative
            # record of the account balance at last shutdown (regardless of whether
            # there are open positions to restore).
            if "capital" in data:
                self._capital      = float(data["capital"])
                self._peak_capital = float(data.get("peak_capital", data["capital"]))
            if restored:
                logger.info("PaperExecutor: restored %d open position(s) from disk", restored)
        except Exception as exc:
            logger.warning("PaperExecutor: could not restore open positions: %s", exc)

    # ── Event handlers ─────────────────────────────────────────

    def _on_position_monitor(self, event):
        """
        Handle POSITION_MONITOR_UPDATED events from external monitoring systems.
        Supports adjust_stop, partial_close, full_close, and tighten_stop actions.
        """
        try:
            data = (event.data or {}) if hasattr(event, "data") else event
            action = data.get("action")
            symbol = data.get("symbol")

            if not symbol:
                return

            if action == "adjust_stop":
                new_stop = data.get("new_stop_loss")
                if new_stop:
                    self.adjust_stop(symbol, new_stop)

            elif action == "partial_close":
                reduce_pct = data.get("reduce_pct", 0.5)
                self.partial_close(symbol, reduce_pct)

            elif action in ("full_close", "close_position"):
                self.close_position(symbol)

            elif action == "tighten_stop":
                new_stop = data.get("new_stop_loss")
                if new_stop:
                    self.adjust_stop(symbol, new_stop)

        except Exception as exc:
            logger.warning("PaperExecutor: position monitor handler error: %s", exc)

    @property
    def available_capital(self) -> float:
        used = sum(p.size_usdt for pos_list in self._positions.values() for p in pos_list)
        return max(0.0, self._capital - used)

    @property
    def drawdown_pct(self) -> float:
        # Total equity = free capital + current mark-to-market value of open positions.
        # _capital is the TOTAL float (never deducted on position open; P&L added on close).
        # available_capital = _capital - sum(size_usdt) removes the locked portion so we
        # don't double-count position value when adding current_position_value below.
        open_positions_flat = [
            p for pos_list in self._positions.values() for p in pos_list
        ]
        locked_capital = sum(p.size_usdt for p in open_positions_flat)
        current_pos_value = sum(
            p.size_usdt * (1 + p.unrealized_pnl / 100) for p in open_positions_flat
        )
        total = (self._capital - locked_capital) + current_pos_value
        if self._peak_capital > 0:
            dd = (self._peak_capital - total) / self._peak_capital * 100
            return max(0.0, dd)
        return 0.0

    def get_open_positions(self) -> list[dict]:
        return [p.to_dict() for pos_list in self._positions.values() for p in pos_list]

    @staticmethod
    def _condition_fingerprint(side: str, models_fired: list, regime: str) -> tuple:
        """
        Build a hashable fingerprint for a trade condition.
        Two positions share the "same condition" if their fingerprint matches:
          (side, frozenset(models_fired), regime)
        """
        return (side, frozenset(models_fired or []), (regime or "").lower())

    def has_duplicate_condition(self, symbol: str, side: str,
                                models_fired: list, regime: str) -> bool:
        """
        Return True if *symbol* already has an open position with the
        exact same condition (side + models_fired set + regime).
        """
        fp = self._condition_fingerprint(side, models_fired, regime)
        for pos in self._positions.get(symbol, []):
            existing_fp = self._condition_fingerprint(
                pos.side, pos.models_fired, pos.regime
            )
            if fp == existing_fp:
                return True
        return False

    def submit(self, candidate: OrderCandidate) -> bool:
        """
        Submit an approved OrderCandidate for paper execution.
        Applies BTC-first size multiplier.
        Returns True if position opened.
        """
        # ── Drawdown circuit breaker ─────────────────────────────────
        # Hard safeguard: block ALL new trades when drawdown >= 10%.
        dd = self.drawdown_pct
        _dd_limit = float(getattr(self, "_dd_circuit_breaker_pct", 10.0))
        if dd >= _dd_limit:
            logger.warning(
                "PaperExecutor: CIRCUIT BREAKER ACTIVE — drawdown %.2f%% >= %.1f%% limit. "
                "All new trade entries blocked until drawdown recovers.",
                dd, _dd_limit,
            )
            bus.publish(
                Topics.SYSTEM_ALERT,
                data={"type": "drawdown_circuit_breaker", "drawdown_pct": round(dd, 2),
                      "limit_pct": _dd_limit, "symbol": candidate.symbol},
                source="paper_executor",
            )
            return False

        # ── Performance-based pause (non-fatal advisory check) ────────
        # Soft safeguard: log a strong WARNING when RAG assessment says
        # should_pause=True, but does NOT hard-block (operator may override).
        # Hard-blocks only when portfolio PF < 1.0 AND WR < 40% (clear
        # negative-expectancy territory) over ≥ 30 trades.
        try:
            from core.monitoring.performance_thresholds import (
                get_threshold_evaluator, RAGStatus,
            )
            _rag = get_threshold_evaluator().evaluate()
            if _rag.should_pause:
                logger.warning(
                    "PaperExecutor: PERFORMANCE PAUSE RECOMMENDED — %s. "
                    "Trade will proceed (operator must manually pause). "
                    "Review Demo Monitor → RAG Status.",
                    _rag.pause_reason,
                )
                bus.publish(
                    Topics.SYSTEM_ALERT,
                    data={"type":         "performance_pause_recommended",
                          "pause_reason": _rag.pause_reason,
                          "symbol":       candidate.symbol},
                    source="paper_executor",
                )
            _port = _rag.portfolio
            _port_trades = _port.trades if _port else 0
            if _port_trades >= 30:
                from core.monitoring.performance_thresholds import RAGStatus
                _pf_val = _port.pf.value or 999
                _wr_val = _port.wr.value or 999

                # ── Intermediate hard stop: PF<1.2 AND WR<45% ───────────────
                # Fires before the final safeguard to catch deteriorating
                # performance early enough to protect capital.
                _pf_inter = _pf_val < 1.2
                _wr_inter = _wr_val < 0.45
                if _pf_inter and _wr_inter:
                    logger.critical(
                        "PaperExecutor: INTERMEDIATE HARD BLOCK — "
                        "portfolio PF=%.3f < 1.2 AND WR=%.1f%% < 45%% "
                        "over %d trades. Blocking new entry for %s. "
                        "Investigate performance before resuming.",
                        _pf_val,
                        _wr_val * 100,
                        _port_trades,
                        candidate.symbol,
                    )
                    bus.publish(
                        Topics.SYSTEM_ALERT,
                        data={"type":    "performance_intermediate_block",
                              "pf":      _pf_val,
                              "wr":      _wr_val,
                              "trades":  _port_trades,
                              "symbol":  candidate.symbol},
                        source="paper_executor",
                    )
                    return False

                # ── Final hard block: PF<1.0 AND WR<40% ────────────────────
                # Last-resort safeguard for confirmed negative-expectancy.
                _pf_red = (_port.pf.status == RAGStatus.RED and _pf_val < 1.0)
                _wr_red = (_port.wr.status == RAGStatus.RED and _wr_val < 0.40)
                if _pf_red and _wr_red:
                    logger.warning(
                        "PaperExecutor: HARD PERFORMANCE BLOCK — "
                        "portfolio PF=%.3f < 1.0 AND WR=%.1f%% < 40%% "
                        "over %d trades. Blocking new entry for %s.",
                        _pf_val,
                        _wr_val * 100,
                        _port_trades,
                        candidate.symbol,
                    )
                    bus.publish(
                        Topics.SYSTEM_ALERT,
                        data={"type":    "performance_hard_block",
                              "pf":      _pf_val,
                              "wr":      _wr_val,
                              "trades":  _port_trades,
                              "symbol":  candidate.symbol},
                        source="paper_executor",
                    )
                    return False
        except Exception as _perf_exc:
            logger.debug(
                "PaperExecutor: performance pause check failed (non-fatal): %s",
                _perf_exc,
            )

        existing = self._positions.get(candidate.symbol, [])
        if len(existing) >= self._max_positions_per_symbol:
            logger.debug("PaperExecutor: max positions (%d) reached for %s",
                         self._max_positions_per_symbol, candidate.symbol)
            return False

        # ── Condition deduplication ──────────────────────────────────
        # Reject if an open position for this symbol already shares the
        # exact same condition (side + models_fired set + regime).
        cand_models = list(getattr(candidate, "models_fired", []))
        cand_regime = getattr(candidate, "regime", "")
        if self.has_duplicate_condition(candidate.symbol, candidate.side,
                                        cand_models, cand_regime):
            logger.info(
                "PaperExecutor: REJECTED duplicate condition for %s %s "
                "(models=%s, regime=%s) — existing position with same condition",
                candidate.side, candidate.symbol, cand_models, cand_regime,
            )
            return False

        entry_price = candidate.entry_price or 0.0
        if entry_price <= 0:
            logger.warning("PaperExecutor: invalid entry price for %s", candidate.symbol)
            return False

        # PositionSizer output (position_size_usdt) is final — SymbolAllocator
        # is the single allocation mechanism.  No per-symbol overrides applied here.
        size_usdt = candidate.position_size_usdt

        fill_price = self._apply_slippage(entry_price, candidate.side) if entry_price > 0 else entry_price
        slippage_cost = abs(fill_price - entry_price)
        logger.debug("PaperExecutor: slippage %.6f → fill %.6f (cost=%.4f USDT/unit)",
                     entry_price, fill_price, slippage_cost)

        # ── Zero-stop-distance guard ─────────────────────────────────────────
        # Root cause of Trade #2 bug: market price moved to the model's stop
        # level between the HTF scan and LTF execution. _do_auto_execute_one()
        # uses live ticker["last"] as fill price, which can equal stop_loss_price
        # when the market has already breached the stop level. Without this guard
        # the trade opens and closes in the same tick (pnl ≈ $0 loss from spread),
        # and position sizing produces infinite qty (risk_usdt / 0).
        _MIN_STOP_PCT = 0.001  # 0.1% — minimum meaningful stop distance
        _sl_guard = candidate.stop_loss_price
        if _sl_guard and fill_price > 0:
            _stop_dist_pct = abs(fill_price - _sl_guard) / fill_price
            if _stop_dist_pct < _MIN_STOP_PCT:
                logger.warning(
                    "PaperExecutor: REJECTED %s %s — stop distance %.6f%% < minimum %.3f%% "
                    "(fill=%.6f, stop=%.6f). Market moved to stop level since signal scan. "
                    "Candidate discarded — next scan will re-evaluate.",
                    candidate.side, candidate.symbol,
                    _stop_dist_pct * 100, _MIN_STOP_PCT * 100,
                    fill_price, _sl_guard,
                )
                bus.publish(
                    Topics.SYSTEM_ALERT,
                    data={
                        "type":           "zero_stop_rejected",
                        "symbol":         candidate.symbol,
                        "side":           candidate.side,
                        "fill_price":     fill_price,
                        "stop_price":     _sl_guard,
                        "stop_dist_pct":  round(_stop_dist_pct * 100, 6),
                    },
                    source="paper_executor",
                )
                return False

        quantity = size_usdt / fill_price

        pos = PaperPosition(
            symbol        = candidate.symbol,
            side          = candidate.side,
            entry_price   = fill_price,
            quantity      = round(quantity, 8),
            stop_loss     = candidate.stop_loss_price,
            take_profit   = candidate.take_profit_price,
            size_usdt     = size_usdt,
            score         = candidate.score,
            rationale     = candidate.rationale,
            regime        = getattr(candidate, "regime", ""),
            models_fired  = list(getattr(candidate, "models_fired", [])),
            timeframe     = getattr(candidate, "timeframe", ""),
        )
        # Store enriched fields for Level-2 learning on close
        pos.entry_expected  = entry_price           # pre-slippage price
        pos.expected_value  = getattr(candidate, "expected_value", None)

        # ── Trade execution verification fields (Session 26) ────────────────
        # symbol_weight and adjusted_score are stamped by SymbolAllocator onto
        # the candidate dict and then forwarded as attributes in scanner_page.py.
        pos.symbol_weight   = float(getattr(candidate, "symbol_weight",  1.0) or 1.0)
        pos.adjusted_score  = float(getattr(candidate, "adjusted_score", candidate.score) or candidate.score)
        # Compute risk_amount_usdt: how many USDT at risk based on fill price and SL
        _sl_p = candidate.stop_loss_price or 0.0
        _tp_p = candidate.take_profit_price or 0.0
        pos.risk_amount_usdt = 0.0
        pos.expected_rr      = 0.0
        if fill_price > 0 and _sl_p > 0 and size_usdt > 0:
            pos.risk_amount_usdt = round(abs(fill_price - _sl_p) / fill_price * size_usdt, 4)
        if fill_price > 0 and _sl_p > 0 and _tp_p > 0:
            _risk   = abs(fill_price - _sl_p)
            _reward = abs(_tp_p - fill_price)
            if _risk > 0:
                pos.expected_rr = round(_reward / _risk, 4)
        # ────────────────────────────────────────────────────────────────────

        self._positions.setdefault(candidate.symbol, []).append(pos)
        logger.info(
            "PaperExecutor: OPENED %s %s @ %.4f (fill) | SL=%.4f TP=%.4f | "
            "size=%.2f USDT | risk=%.2f USDT | expRR=%.2f | "
            "score=%.3f wt=%.2f adjScore=%.3f | models=%s",
            candidate.side, candidate.symbol, fill_price,
            candidate.stop_loss_price, candidate.take_profit_price,
            size_usdt, pos.risk_amount_usdt, pos.expected_rr,
            candidate.score, pos.symbol_weight, pos.adjusted_score,
            list(getattr(candidate, "models_fired", [])),
        )
        bus.publish(Topics.TRADE_OPENED, data=pos.to_dict(), source="paper_executor")

        # ── Phase 1B observability: log CDA metadata per trade ───────────────
        # Reads fields stamped by RiskGate — zero logic impact.
        # Written to cda_observations.jsonl for offline CPS validation.
        try:
            _cda_mult  = float(getattr(candidate, "cda_multiplier", 1.0) or 1.0)
            _cda_tier  = str(getattr(candidate, "cda_tier",  "NORMAL") or "NORMAL")
            _base_sz   = float(getattr(candidate, "base_position_size_usdt", size_usdt) or size_usdt)
            if _cda_mult < 1.0:
                logger.info(
                    "PaperExecutor[CDA]: %s %s | base=%.2f USDT → adjusted=%.2f USDT "
                    "(mult=%.2f, tier=%s)",
                    candidate.side, candidate.symbol,
                    _base_sz, size_usdt, _cda_mult, _cda_tier,
                )
            _obs = {
                "ts":                          datetime.utcnow().isoformat(),
                "symbol":                      candidate.symbol,
                "side":                        candidate.side,
                "base_position_size_usdt":     round(_base_sz, 4),
                "cda_multiplier":              round(_cda_mult, 4),
                "adjusted_position_size_usdt": round(size_usdt, 4),
                "cda_tier":                    _cda_tier,
                "regime":                      getattr(candidate, "regime", ""),
                "score":                       round(float(candidate.score or 0), 4),
                "entry_price":                 round(float(fill_price or 0), 4),
            }
            _CDA_OBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CDA_OBS_FILE, "a") as _fh:
                _fh.write(json.dumps(_obs) + "\n")
        except Exception as _obs_exc:
            logger.debug("PaperExecutor: CDA obs write failed (non-fatal): %s", _obs_exc)

        self._save_open_positions()
        return True

    def on_tick(self, symbol: str, price: float) -> None:
        """Update position mark-to-market and check stops."""
        if symbol not in self._positions:
            return
        # Iterate over a copy — positions may be closed during iteration
        for pos in list(self._positions.get(symbol, [])):
            exit_reason = pos.update(price)
            if exit_reason:
                self._close_position(symbol, price, exit_reason, pos)
            else:
                bus.publish(Topics.POSITION_UPDATED, data=pos.to_dict(), source="paper_executor")

    def close_position(self, symbol: str, price: Optional[float] = None) -> bool:
        """
        Manually close the oldest position for a symbol.
        Uses the position's last known price if *price* is not given.
        Returns True if the position existed and was closed.
        """
        pos_list = self._positions.get(symbol, [])
        if not pos_list:
            return False
        pos = pos_list[0]  # Close oldest
        exit_price = price if price else pos.current_price
        self._close_position(symbol, exit_price, "manual_close", pos)
        return True

    def close_all(self) -> int:
        """
        Manually close every open position at its last known mark price.
        Returns the number of positions closed.
        """
        count = 0
        for symbol in list(self._positions.keys()):
            for pos in list(self._positions.get(symbol, [])):
                self._close_position(symbol, pos.current_price, "manual_close", pos)
                count += 1
        return count

    # ── Dynamic stop adjustment ────────────────────────────────

    def adjust_stop(self, symbol: str, new_stop_loss: float) -> bool:
        """
        Adjust the stop loss of the first (oldest) position for a symbol.
        Only allows tightening (moving closer to entry price), not loosening.
        Returns True on success, False otherwise.
        """
        pos_list = self._positions.get(symbol, [])
        pos = pos_list[0] if pos_list else None
        if pos is None:
            logger.warning("PaperExecutor: adjust_stop — position not found for %s", symbol)
            return False

        entry = pos.entry_price
        current_sl = pos.stop_loss
        side = pos.side

        # Validate: new stop must be tighter (closer to entry)
        if side == "buy":
            # For buy: current SL is below entry, new SL must be >= current SL and < entry
            if new_stop_loss < current_sl or new_stop_loss >= entry:
                logger.warning(
                    "PaperExecutor: adjust_stop — invalid SL for %s (buy): "
                    "new=%.6g, current=%.6g, entry=%.6g",
                    symbol, new_stop_loss, current_sl, entry
                )
                return False
        else:
            # For sell: current SL is above entry, new SL must be <= current SL and > entry
            if new_stop_loss > current_sl or new_stop_loss <= entry:
                logger.warning(
                    "PaperExecutor: adjust_stop — invalid SL for %s (sell): "
                    "new=%.6g, current=%.6g, entry=%.6g",
                    symbol, new_stop_loss, current_sl, entry
                )
                return False

        pos.stop_loss = new_stop_loss

        logger.info(
            "PaperExecutor: adjusted stop for %s | %.6g → %.6g",
            symbol, current_sl, new_stop_loss
        )
        bus.publish(
            Topics.POSITION_UPDATED,
            data=pos.to_dict(),
            source="paper_executor",
        )
        self._save_open_positions()
        return True

    def adjust_target(self, symbol: str, new_take_profit: float) -> bool:
        """
        Adjust the take-profit of the first (oldest) position for a symbol.
        For a buy: new TP must be below the current TP (tightening toward entry allowed).
        For a sell: new TP must be above the current TP (tightening toward entry allowed).
        In practice this method allows setting any valid TP for testing purposes.
        Returns True on success, False otherwise.
        """
        pos_list = self._positions.get(symbol, [])
        pos = pos_list[0] if pos_list else None
        if pos is None:
            logger.warning("PaperExecutor: adjust_target — position not found for %s", symbol)
            return False

        current_tp = pos.take_profit
        pos.take_profit = new_take_profit
        logger.info(
            "PaperExecutor: adjusted take-profit for %s | %.6g → %.6g",
            symbol, current_tp, new_take_profit,
        )
        bus.publish(
            Topics.POSITION_UPDATED,
            data=pos.to_dict(),
            source="paper_executor",
        )
        self._save_open_positions()
        return True

    # ── Partial close ──────────────────────────────────────────

    def partial_close(self, symbol: str, reduce_pct: float) -> bool:
        """
        Close a fraction of the first (oldest) position for a symbol.

        reduce_pct: 0.0 to 1.0 (e.g., 0.5 closes 50% of position)
        If reduce_pct >= 0.99, calls full close instead.
        Returns True on success, False otherwise.
        """
        pos_list = self._positions.get(symbol, [])
        pos = pos_list[0] if pos_list else None
        if pos is None:
            logger.warning("PaperExecutor: partial_close — position not found for %s", symbol)
            return False

        # Validate reduce_pct
        if reduce_pct <= 0.0 or reduce_pct > 1.0:
            logger.warning(
                "PaperExecutor: partial_close — invalid reduce_pct %.2f for %s",
                reduce_pct, symbol
            )
            return False

        # If nearly 100%, just do full close
        if reduce_pct >= 0.99:
            return self.close_position(symbol)

        # Calculate partial quantity
        original_qty  = pos.quantity
        close_qty     = original_qty * reduce_pct
        close_price   = pos.current_price
        new_qty       = original_qty - close_qty

        # Calculate P&L for closed fraction
        if pos.side == "buy":
            pnl_usdt = (close_price - pos.entry_price) * close_qty
        else:
            pnl_usdt = (pos.entry_price - close_price) * close_qty

        # Capture sizing info BEFORE modifying the position
        _orig_entry_sz = float(getattr(pos, "entry_size_usdt", None) or pos.size_usdt)
        _close_sz_usdt = round(pos.size_usdt * reduce_pct, 2)   # USDT portion being closed

        # ── Update position: reduce quantity AND size_usdt proportionally ──
        # size_usdt must shrink so that available_capital, portfolio heat, and
        # drawdown_pct all reflect the smaller remaining position correctly.
        pos.quantity  = new_qty
        pos.size_usdt = pos.size_usdt * (1.0 - reduce_pct)

        # ── Realise P&L into capital ────────────────────────────────────────
        # Full closes (close_position) already do this via _close_position().
        # Partial closes must do it here so equity curve stays accurate.
        self._capital += pnl_usdt
        if self._capital > self._peak_capital:
            self._peak_capital = self._capital

        # ── Record partial-close trade for full Trade History transparency ──
        # pnl_pct is expressed relative to the CLOSED fraction only.
        _partial_pnl_pct = round(
            pnl_usdt / _close_sz_usdt * 100 if _close_sz_usdt > 0 else 0.0, 4
        )
        _partial_duration_s = int(
            (datetime.utcnow() - pos.opened_at).total_seconds()
        ) if pos.opened_at else 0

        partial_trade = {
            "symbol":           symbol,
            "side":             pos.side,
            "entry_price":      pos.entry_price,
            "exit_price":       close_price,
            "stop_loss":        pos.stop_loss,
            "take_profit":      pos.take_profit,
            "size_usdt":        _close_sz_usdt,     # size of this partial close
            "entry_size_usdt":  round(_orig_entry_sz, 2),  # full original position
            "exit_size_usdt":   _close_sz_usdt,            # portion closed now
            "pnl_pct":          _partial_pnl_pct,
            "pnl_usdt":         round(pnl_usdt, 2),
            "exit_reason":      "partial_close",
            "score":            pos.score,
            "rationale":        pos.rationale,
            "regime":           pos.regime,
            "models_fired":     pos.models_fired,
            "timeframe":        pos.timeframe,
            "duration_s":       _partial_duration_s,
            "opened_at":        pos.opened_at.isoformat() if pos.opened_at else "",
            "closed_at":        datetime.utcnow().isoformat(),
            "entry_expected":   getattr(pos, "entry_expected", None),
            "expected_value":   getattr(pos, "expected_value", None),
            "risk_amount_usdt": 0.0,
            "expected_rr":      0.0,
            "symbol_weight":    float(getattr(pos, "symbol_weight",  1.0) or 1.0),
            "adjusted_score":   float(getattr(pos, "adjusted_score", pos.score or 0.0) or 0.0),
        }
        self._closed_trades.append(partial_trade)
        self._save_trade_to_db(partial_trade)

        # Persist updated state so restart sees correct position size
        self._save_open_positions()

        logger.info(
            "PaperExecutor: partial close for %s | closed %.2f%% (%.2f USDT) @ %.4f | "
            "remaining qty=%.8f | P&L=%.2f USDT | capital=%.2f",
            symbol, reduce_pct * 100, _close_sz_usdt, close_price, new_qty,
            pnl_usdt, self._capital,
        )
        bus.publish(Topics.TRADE_CLOSED, data=partial_trade, source="paper_executor")
        bus.publish(
            Topics.POSITION_UPDATED,
            data=pos.to_dict(),
            source="paper_executor",
        )
        return True

    def reset(self, initial_capital: float = 100_000.0) -> None:
        """
        Reset the paper account: clear all positions, trade history,
        and all persistent data stores.  Restores initial capital.
        After reset + restart, the system starts completely clean.
        """
        self._positions.clear()
        self._closed_trades.clear()
        self._initial_capital = initial_capital
        self._capital         = initial_capital
        self._peak_capital    = initial_capital
        # Persist the empty positions to disk so they survive restarts
        self._save_open_positions()

        # ── 1. Wipe SQLite paper_trades table ────────────────────
        try:
            from core.database.engine import engine as _engine
            from sqlalchemy import text as _text
            with _engine.connect() as conn:
                deleted = conn.execute(_text("DELETE FROM paper_trades"))
                conn.commit()
                logger.info("PaperExecutor reset: cleared %d rows from paper_trades",
                            deleted.rowcount if deleted.rowcount >= 0 else 0)
        except Exception as exc:
            logger.warning("PaperExecutor reset: could not clear paper_trades DB: %s", exc)

        # ── 2. Wipe TradeMonitor JSON file ───────────────────────
        try:
            from core.monitoring.trade_monitor import get_trade_monitor
            tm = get_trade_monitor()
            tm._recent_trades.clear()
            tm._save()
            logger.info("PaperExecutor reset: cleared trade_monitor.json")
        except Exception as exc:
            logger.debug("PaperExecutor reset: trade_monitor clear skipped: %s", exc)

        # ── 3. Wipe adaptive learning data ───────────────────────
        try:
            from core.learning.trade_outcome_store import get_outcome_store
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            # Clear L1 tracker
            tracker = get_outcome_tracker()
            tracker._outcomes.clear()
            tracker._save()
            # Clear outcome store (truncate the JSONL file)
            store = get_outcome_store()
            store._path.write_text("")
            store._trades.clear()
            logger.info("PaperExecutor reset: cleared L1 learning data")
        except Exception as exc:
            logger.debug("PaperExecutor reset: L1 learning clear skipped: %s", exc)

        try:
            from core.learning.level2_tracker import get_level2_tracker
            l2 = get_level2_tracker()
            # Clear all internal dicts (attribute names vary by version)
            for attr in ("_cells", "_exit_r", "_entry_rr", "_data", "_records"):
                if hasattr(l2, attr):
                    getattr(l2, attr).clear()
            l2._save()
            logger.info("PaperExecutor reset: cleared L2 learning data")
        except Exception as exc:
            logger.debug("PaperExecutor reset: L2 learning clear skipped: %s", exc)

        logger.info("PaperExecutor: account reset (capital=%.2f)", initial_capital)

    # ── DB persistence helpers ─────────────────────────────

    def _save_trade_to_db(self, trade: dict) -> None:
        """Persist a closed trade to the paper_trades table (best-effort)."""
        try:
            from core.database.engine import get_session
            from core.database.models import PaperTrade
            with get_session() as s:
                s.add(PaperTrade(
                    symbol          = trade["symbol"],
                    side            = trade["side"],
                    regime          = trade.get("regime", ""),
                    timeframe       = trade.get("timeframe", ""),
                    entry_price     = trade["entry_price"],
                    exit_price      = trade["exit_price"],
                    stop_loss       = trade.get("stop_loss"),
                    take_profit     = trade.get("take_profit"),
                    size_usdt       = trade["size_usdt"],
                    entry_size_usdt = trade.get("entry_size_usdt", trade["size_usdt"]),
                    exit_size_usdt  = trade.get("exit_size_usdt",  trade["size_usdt"]),
                    pnl_usdt        = trade["pnl_usdt"],
                    pnl_pct         = trade["pnl_pct"],
                    score           = trade.get("score", 0.0),
                    exit_reason     = trade.get("exit_reason", ""),
                    models_fired    = trade.get("models_fired") or [],
                    rationale       = trade.get("rationale", ""),
                    duration_s      = trade.get("duration_s", 0),
                    opened_at       = trade.get("opened_at", ""),
                    closed_at       = trade.get("closed_at", ""),
                ))
        except Exception as exc:
            logger.warning("PaperExecutor: DB write failed: %s", exc)

    def _load_history(self) -> None:
        """
        Restore closed-trade history from SQLite on startup.

        Replays the equity curve to reconstruct *_capital* and
        *_peak_capital* accurately, so drawdown and P&L stats are
        correct even after a restart.
        """
        try:
            from core.database.engine import get_session
            from core.database.models import PaperTrade
            with get_session() as s:
                rows = (
                    s.query(PaperTrade)
                    .order_by(PaperTrade.created_at)
                    .all()
                )
                # Convert to plain dicts while session is open — avoids
                # DetachedInstanceError when lazy attributes are accessed
                # after the session context manager exits.
                trade_dicts = [row.to_dict() for row in rows]

            if not trade_dicts:
                return

            # Replay closed-trade history
            for td in trade_dicts:
                self._closed_trades.append(td)

            # Replay equity curve to recover current capital and peak
            equity = self._initial_capital
            peak   = self._initial_capital
            for t in self._closed_trades:
                equity += t.get("pnl_usdt", 0.0)
                if equity > peak:
                    peak = equity

            self._capital      = equity
            self._peak_capital = peak

            logger.info(
                "PaperExecutor: loaded %d historical trade(s) from DB; "
                "capital=%.2f, peak=%.2f",
                len(trade_dicts), self._capital, self._peak_capital,
            )
        except Exception as exc:
            logger.warning(
                "PaperExecutor: could not load trade history from DB: %s", exc
            )

    def _apply_slippage(self, price: float, side: str) -> float:
        """Apply realistic market slippage and spread to a fill price."""
        slippage = random.uniform(self._SLIPPAGE_MIN, self._SLIPPAGE_MAX)
        if side == "buy":
            return price * (1.0 + slippage + self._SPREAD_HALF)
        else:
            return price * (1.0 - slippage - self._SPREAD_HALF)

    def get_closed_trades(self) -> list[dict]:
        """Return a copy of the closed-trade history list."""
        return list(self._closed_trades)

    def get_stats(self) -> dict:
        """Return a comprehensive stats dict for display in the UI."""
        closed = self._closed_trades
        n      = len(closed)
        wins   = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)
        losses = n - wins
        total_pnl = sum((t.get("pnl_usdt") or 0) for t in closed)
        pnl_list  = [(t.get("pnl_usdt") or 0) for t in closed]
        pnl_pct_l = [(t.get("pnl_pct")   or 0) for t in closed]
        best      = max(pnl_list) if pnl_list else 0.0
        worst     = min(pnl_list) if pnl_list else 0.0
        avg_pnl   = total_pnl / n if n else 0.0
        avg_dur_s = (
            sum(t.get("duration_s", 0) for t in closed) / n if n else 0.0
        )
        gross_win  = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss else 0.0

        # Average realized R:R based on configured SL/TP
        rr_list = []
        for t in closed:
            entry = t.get("entry_price", 0.0)
            sl    = t.get("stop_loss",   0.0)
            tp    = t.get("take_profit", 0.0)
            side  = t.get("side", "buy")
            if entry > 0 and sl > 0 and tp > 0:
                if side == "buy":
                    risk, reward = entry - sl, tp - entry
                else:
                    risk, reward = sl - entry, entry - tp
                if risk > 0:
                    rr_list.append(reward / risk)
        avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0.0

        # Trades per day / per week
        span_days = 0.0
        if n >= 2:
            try:
                from datetime import datetime as _dt
                ts = []
                for t in closed:
                    for k in ("opened_at", "closed_at"):
                        v = t.get(k)
                        if isinstance(v, str) and v:
                            ts.append(_dt.fromisoformat(v).timestamp())
                        elif isinstance(v, _dt):
                            ts.append(v.timestamp())
                if len(ts) >= 2:
                    span_days = (max(ts) - min(ts)) / 86400
            except Exception:
                pass
        trades_per_day  = n / span_days  if span_days > 0 else 0.0
        trades_per_week = trades_per_day * 7

        # Long vs short breakdown
        long_trades  = [t for t in closed if t.get("side") == "buy"]
        short_trades = [t for t in closed if t.get("side") == "sell"]
        long_pnl     = sum(t.get("pnl_usdt", 0) for t in long_trades)
        short_pnl    = sum(t.get("pnl_usdt", 0) for t in short_trades)

        return {
            # Core stats (backward-compatible)
            "total_trades":       n,
            "win_rate":           round(wins  / n * 100, 2) if n else 0.0,
            "loss_rate":          round(losses / n * 100, 2) if n else 0.0,
            "total_pnl_usdt":     round(total_pnl, 2),
            "wins":               wins,
            "losses":             losses,
            "best_trade_usdt":    round(best, 2),
            "worst_trade_usdt":   round(worst, 2),
            "avg_pnl_usdt":       round(avg_pnl, 2),
            "avg_duration_s":     round(avg_dur_s),
            "profit_factor":      profit_factor,
            "open_positions":     sum(len(v) for v in self._positions.values()),
            "drawdown_pct":       round(self.drawdown_pct, 4),
            "available_capital":  round(self.available_capital, 2),
            # Extended stats
            "avg_rr":             round(avg_rr, 3),
            "gross_win_usdt":     round(gross_win, 2),
            "gross_loss_usdt":    round(gross_loss, 2),
            "trades_per_day":     round(trades_per_day, 2),
            "trades_per_week":    round(trades_per_week, 2),
            "span_days":          round(span_days, 1),
            "long_trades":        len(long_trades),
            "short_trades":       len(short_trades),
            "long_pnl_usdt":      round(long_pnl, 2),
            "short_pnl_usdt":     round(short_pnl, 2),
        }

    def get_production_status(self) -> dict:
        """
        Return a concise production monitoring snapshot.
        Called by the Risk Management page and System Health page each refresh.

        Returns
        -------
        dict with keys:
          capital_usdt         : float — current total capital
          peak_capital_usdt    : float — peak capital since start
          total_return_pct     : float — (capital/initial - 1) * 100
          drawdown_pct         : float — current drawdown from peak
          circuit_breaker_on   : bool  — True when drawdown >= 10%
          portfolio_heat_pct   : float — current committed risk as % of capital
          open_positions       : int   — total open positions
          open_symbols         : list  — symbols with open positions
          last_10_outcomes     : list  — last 10 trade outcomes ["W"/"L"]
          current_losing_streak: int   — consecutive losses (most recent)
          total_trades         : int   — all-time closed trades
          session_pnl_usdt     : float — P&L since startup
        """
        closed = self._closed_trades
        n = len(closed)

        # Portfolio heat: sum of (risk per open trade) as % of capital
        heat = 0.0
        if self._capital > 0:
            for pos_list in self._positions.values():
                for p in pos_list:
                    if p.stop_loss and p.stop_loss > 0 and p.entry_price > 0:
                        stop_dist = abs(p.entry_price - p.stop_loss)
                        qty = p.size_usdt / p.entry_price if p.entry_price > 0 else 0
                        heat += (qty * stop_dist) / self._capital * 100
                    else:
                        # Fallback: assume 1% risk
                        heat += (p.size_usdt / self._capital) * 0.01 * 100

        # Last 10 outcomes
        last10 = []
        streak = 0
        for t in reversed(closed[-10:]):
            won = (t.get("pnl_usdt") or 0) > 0
            last10.insert(0, "W" if won else "L")

        # Losing streak (from most recent)
        for t in reversed(closed):
            won = (t.get("pnl_usdt") or 0) > 0
            if not won:
                streak += 1
            else:
                break

        dd = self.drawdown_pct

        return {
            "capital_usdt":          round(self._capital, 2),
            "peak_capital_usdt":     round(self._peak_capital, 2),
            "total_return_pct":      round((self._capital / self._initial_capital - 1) * 100, 2) if self._initial_capital > 0 else 0.0,
            "drawdown_pct":          round(dd, 2),
            "circuit_breaker_on":    dd >= self._dd_circuit_breaker_pct,
            "portfolio_heat_pct":    round(heat, 2),
            "open_positions":        sum(len(v) for v in self._positions.values()),
            "open_symbols":          list(self._positions.keys()),
            "last_10_outcomes":      last10,
            "current_losing_streak": streak,
            "total_trades":          n,
            "session_pnl_usdt":      round(self._capital - self._initial_capital, 2),
        }

    def _close_position(self, symbol: str, exit_price: float, reason: str, pos: Optional[PaperPosition] = None) -> None:
        if pos is None:
            pos_list = self._positions.get(symbol, [])
            if not pos_list:
                return
            pos = pos_list[0]
        # Remove THIS specific position from the list
        pos_list = self._positions.get(symbol, [])
        if pos in pos_list:
            pos_list.remove(pos)
        if not pos_list and symbol in self._positions:
            del self._positions[symbol]
        # Apply exit slippage
        exit_side = "sell" if pos.side == "buy" else "buy"
        exit_fill = self._apply_slippage(exit_price, exit_side)
        pnl_pct  = ((exit_fill - pos.entry_price) / pos.entry_price * 100) if pos.side == "buy" else ((pos.entry_price - exit_fill) / pos.entry_price * 100)
        pnl_usdt = pos.size_usdt * pnl_pct / 100

        # Calculate actual duration in seconds
        duration_s = (datetime.utcnow() - pos.opened_at).total_seconds()

        # Pre-compute R metrics so they're available both in the trade dict
        # and in the learning / monitor blocks below.
        _entry_p  = pos.entry_price or 0.0
        _sl_p     = pos.stop_loss   or 0.0
        _tp_p     = pos.take_profit or 0.0
        _sz       = pos.size_usdt   or 0.0
        _risk_usdt_pre = getattr(pos, "risk_amount_usdt", 0.0) or 0.0
        _exp_rr_pre    = getattr(pos, "expected_rr",      0.0) or 0.0
        # If stored values aren't available, recompute from close-time data
        if _risk_usdt_pre <= 0 and _entry_p > 0 and _sl_p > 0 and _sz > 0:
            _risk_usdt_pre = round(abs(_entry_p - _sl_p) / _entry_p * _sz, 4)
        if _exp_rr_pre <= 0 and _entry_p > 0 and _sl_p > 0 and _tp_p > 0:
            _r = abs(_entry_p - _sl_p); _rw = abs(_tp_p - _entry_p)
            if _r > 0: _exp_rr_pre = round(_rw / _r, 4)

        # entry_size_usdt: original capital deployed when this position was opened.
        # For full closes it equals pos.size_usdt; for positions that had a prior
        # partial_close() call it will be larger (the original full size).
        _entry_sz = float(getattr(pos, "entry_size_usdt", None) or pos.size_usdt)
        # exit_size_usdt: the portion of the position actually closed in this call.
        # For a normal full close this equals pos.size_usdt.
        # For the "remainder" leg after a prior partial close it will be smaller
        # than entry_size_usdt.
        _exit_sz  = pos.size_usdt

        trade = {
            "symbol":           symbol,
            "side":             pos.side,
            "entry_price":      pos.entry_price,
            "exit_price":       exit_fill,
            "stop_loss":        pos.stop_loss,
            "take_profit":      pos.take_profit,
            "size_usdt":        pos.size_usdt,
            "entry_size_usdt":  round(_entry_sz, 2),
            "exit_size_usdt":   round(_exit_sz,  2),
            "pnl_pct":          round(pnl_pct, 4),
            "pnl_usdt":         round(pnl_usdt, 2),
            "exit_reason":      reason,
            "score":            pos.score,
            "rationale":        pos.rationale,
            "regime":           pos.regime,
            "models_fired":     pos.models_fired,
            "timeframe":        pos.timeframe,
            "duration_s":       int(duration_s),
            "opened_at":        pos.opened_at.isoformat(),
            "closed_at":        datetime.utcnow().isoformat(),
            # Enriched fields for Level-2 learning
            "entry_expected":   getattr(pos, "entry_expected", None),
            "expected_value":   getattr(pos, "expected_value", None),
            # Trade execution verification fields (Session 26)
            "risk_amount_usdt": _risk_usdt_pre,
            "expected_rr":      _exp_rr_pre,
            "symbol_weight":    float(getattr(pos, "symbol_weight",  1.0) or 1.0),
            "adjusted_score":   float(getattr(pos, "adjusted_score", pos.score or 0.0) or 0.0),
        }
        self._closed_trades.append(trade)
        self._capital      += pnl_usdt   # realize P&L
        self._peak_capital  = max(self._peak_capital, self._capital)

        # ── Feed outcome to adaptive learning loop (Level 1 + Level 2) ────
        # Level-1: global per-model win-rate (TradeOutcomeTracker).
        # Level-2: contextual win-rates by (model×regime) and (model×asset).
        # Both are non-fatal; a failure here must never block trade recording.
        _won        = pnl_pct > 0
        _models     = pos.models_fired or []
        _regime_str = pos.regime or "unknown"
        try:
            from core.meta_decision.confluence_scorer import get_outcome_tracker
            if _models:
                get_outcome_tracker().record(_models, won=_won)
        except Exception as _lp_exc:
            logger.debug("PaperExecutor: L1 learning record failed (non-fatal): %s", _lp_exc)
        try:
            from core.learning.level2_tracker import get_level2_tracker as _get_l2
            from core.learning.trade_outcome_store import get_outcome_store as _get_store
            # Use pre-computed risk values from the trade dict (calculated above)
            _pnl_u      = trade.get("pnl_usdt", 0.0) or 0.0
            _risk_usdt  = trade.get("risk_amount_usdt", 0.0) or 0.0
            _realized_r  = round(_pnl_u / _risk_usdt, 4) if _risk_usdt > 0 else None
            _expected_rr = trade.get("expected_rr") or None
            # Stamp realized_r back into trade dict for all downstream consumers
            trade["realized_r"] = _realized_r
            _get_l2().record(
                models      = _models,
                won         = _won,
                regime      = _regime_str,
                symbol      = symbol,
                score       = trade.get("score", 0.0),
                exit_reason = reason,
                realized_r  = _realized_r,
                expected_rr = _expected_rr,
            )
            _get_store().record(trade)
        except Exception as _l2_exc:
            logger.debug("PaperExecutor: L2 learning record failed (non-fatal): %s", _l2_exc)

        # ── Feed CalibratorMonitor (Session 23) ──────────────────────────
        # Record the prediction that was made at entry time vs the actual outcome.
        # The predicted probability is stored on the position as 'win_prob' if
        # the calibrator was used; fall back to score-based sigmoid estimate.
        try:
            from core.learning.calibrator_monitor import get_calibrator_monitor
            _pred_prob = float(getattr(pos, "win_prob", None) or (pos.score or 0.5))
            get_calibrator_monitor().record(
                predicted_prob=_pred_prob,
                actual_win=_won,
            )
        except Exception as _cm_exc:
            logger.debug("PaperExecutor: calibrator monitor record failed (non-fatal): %s", _cm_exc)

        # ── Feed trade monitor for 75-trade checkpoint metrics ────
        try:
            from core.monitoring.trade_monitor import get_trade_monitor
            get_trade_monitor().record_trade(
                score=trade.get("score", 0.0),
                models_fired=_models,
                won=_won,
                exit_reason=reason,
                realized_r=_realized_r,
                pnl_usdt=trade.get("pnl_usdt", 0.0),
                regime=_regime_str,
                symbol=symbol,
            )
        except Exception as _mon_exc:
            logger.debug("PaperExecutor: trade monitor record failed (non-fatal): %s", _mon_exc)

        # ── Enhanced trade log (Phase 1 feature extraction dataset) ──
        try:
            from core.analytics.trade_log import log_trade as _log_trade
            _utc_hour = pos.opened_at.hour if pos.opened_at else None
            _log_trade(
                symbol=symbol, side=pos.side, direction=pos.side,
                entry_price=pos.entry_price, exit_price=exit_fill,
                stop_loss=pos.stop_loss, take_profit=pos.take_profit,
                size_usdt=pos.size_usdt, regime=pos.regime or "unknown",
                regime_confidence=float(getattr(pos, "regime_confidence", 0.0) or 0.0),
                confluence_score=float(pos.score or 0.0),
                models_fired=pos.models_fired or [], timeframe=pos.timeframe or "1h",
                pnl_pct=float(trade.get("pnl_pct") or 0.0),
                pnl_usdt=float(trade.get("pnl_usdt") or 0.0),
                exit_reason=reason, realized_r=_realized_r,
                utc_hour_at_entry=_utc_hour,
                opened_at=trade.get("opened_at"), closed_at=trade.get("closed_at"),
            )
        except Exception as _tl_exc:
            logger.debug("PaperExecutor: trade log write failed (non-fatal): %s", _tl_exc)

        # ── Model performance tracking (Phase 2) ──
        try:
            from core.analytics.model_performance_tracker import get_model_performance_tracker
            get_model_performance_tracker().record(
                models_fired=pos.models_fired or [],
                won=_won,
                realized_r=_realized_r,
                regime=pos.regime or "unknown",
            )
        except Exception as _mp_exc:
            logger.debug("PaperExecutor: model perf tracker failed (non-fatal): %s", _mp_exc)

        # ── Live vs Backtest tracker (Session 26) ──
        try:
            from core.monitoring.live_vs_backtest import get_live_vs_backtest_tracker
            get_live_vs_backtest_tracker().record(trade)
        except Exception as _lvb_exc:
            logger.debug("PaperExecutor: live_vs_backtest record failed (non-fatal): %s", _lvb_exc)

        logger.info(
            "PaperExecutor: CLOSED %s @ %.4f | reason=%s | PnL=%.2f%%  (%.2f USDT) | R=%.2f",
            symbol, exit_price, reason, pnl_pct, pnl_usdt,
            _realized_r if _realized_r is not None else 0.0,
        )
        bus.publish(Topics.TRADE_CLOSED, data=trade, source="paper_executor")
        self._save_trade_to_db(trade)
        self._save_open_positions()


# ── Module singleton ──────────────────────────────────────
paper_executor = PaperExecutor()
