# ============================================================
# NEXUS TRADER — Live Execution Bridge
#
# Bridges the gap between the main application (which uses the
# OrderCandidate / PaperExecutor interface) and the Phase 8
# production-grade live subsystem (core/intraday/live/).
#
# This bridge:
#   1. Exposes the same interface as PaperExecutor so callers
#      (scanner, risk page, crash defense, UI) work unchanged
#   2. Converts OrderCandidate → ExecutionRequest → Phase 8
#      LiveExecutor.execute()
#   3. Places server-side SL orders on exchange after entry fill
#   4. Fetches balance/positions from exchange (not internal sim)
#   5. Supports periodic reconciliation via ReconciliationEngine
#   6. Supports startup recovery via RestartRecoveryManager
#
# Thread safety: RLock guards all mutable state.
# ============================================================
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# Sentinel: lazily initialised when set_components() is called
_UNINIT = "UNINITIALISED"


class LiveBridge:
    """
    Adapter exposing the PaperExecutor-compatible interface on top of
    the Phase 8 production live execution subsystem.
    """

    def __init__(self):
        self._lock = threading.RLock()

        # ── Phase 8 components (set via set_components) ──
        self._exchange_adapter = None       # ExchangeAdapter
        self._idempotency_store = None      # IdempotencyStore
        self._phase8_executor = None        # Phase 8 LiveExecutor
        self._reconciliation_engine = None  # OrderReconciliationEngine
        self._recovery_manager = None       # RestartRecoveryManager

        # ── State ──
        self._positions: Dict[str, dict] = {}     # symbol → position dict
        self._closed_trades: List[dict] = []
        self._sl_orders: Dict[str, str] = {}      # symbol → exchange SL order ID
        self._pending_confirmations: Dict[str, Any] = {}

        # ── Balance cache ──
        self._balance_cache: Dict[str, Any] = {"usdt": 0.0, "ts": 0.0}
        self._BALANCE_CACHE_TTL = 30.0
        self._peak_usdt: float = 0.0
        self._initial_usdt: float = 0.0

        # ── Recovery state ──
        self._recovery_complete = False
        self._trading_allowed = False

        self._initialised = False

    # ── Initialisation ────────────────────────────────────────
    def set_components(
        self,
        exchange_adapter,
        idempotency_store,
        phase8_executor,
        reconciliation_engine,
        recovery_manager,
    ) -> None:
        """Inject Phase 8 components after exchange connects."""
        with self._lock:
            self._exchange_adapter = exchange_adapter
            self._idempotency_store = idempotency_store
            self._phase8_executor = phase8_executor
            self._reconciliation_engine = reconciliation_engine
            self._recovery_manager = recovery_manager
            self._initialised = True
        logger.info("LiveBridge: Phase 8 components injected — bridge is ACTIVE")

    @property
    def is_initialised(self) -> bool:
        return self._initialised

    # ── Startup Recovery (F-03) ───────────────────────────────
    def run_startup_recovery(self, auto_resolve: bool = True) -> dict:
        """
        Run Phase 8 RestartRecoveryManager.
        Returns the RecoveryReport as a dict.
        Trading is blocked until this returns clean.
        """
        if not self._initialised:
            logger.error("LiveBridge: cannot run recovery — components not initialised")
            return {"trading_allowed": False, "errors": ["components_not_initialised"]}

        logger.info("LiveBridge: === STARTUP RECOVERY BEGIN ===")

        with self._lock:
            internal_orders = {}
            if self._phase8_executor:
                internal_orders = self._phase8_executor.get_all_orders()
            internal_positions = {}  # clean start — will be rebuilt from exchange

        report = self._recovery_manager.recover(
            internal_orders=internal_orders,
            internal_positions=internal_positions,
            auto_resolve=auto_resolve,
        )

        with self._lock:
            self._recovery_complete = report.success
            self._trading_allowed = report.trading_allowed

        if report.trading_allowed:
            logger.info(
                "LiveBridge: RECOVERY COMPLETE — trading ALLOWED | "
                "exchange_positions=%d exchange_balance=%.2f orders_recovered=%d",
                report.exchange_positions,
                report.exchange_balance_usdt,
                report.orders_recovered,
            )
            # Hydrate positions from exchange
            self._hydrate_positions_from_exchange()
            # Hydrate balance
            self._fetch_usdt_balance(force=True)
        else:
            logger.error(
                "LiveBridge: RECOVERY INCOMPLETE — trading BLOCKED | "
                "phase=%s errors=%s",
                report.phase, report.errors,
            )
            bus.publish(Topics.SYSTEM_ALERT, {
                "type": "recovery_failed",
                "message": f"Startup recovery failed: {report.errors}",
                "severity": "critical",
            }, source="live_bridge")

        return report.to_dict()

    def _hydrate_positions_from_exchange(self) -> None:
        """Fetch current exchange positions and populate internal state."""
        if not self._exchange_adapter:
            return
        try:
            exchange_positions = self._exchange_adapter.fetch_positions()
            with self._lock:
                self._positions.clear()
                for ep in exchange_positions:
                    symbol = ep.get("symbol", "")
                    qty = float(ep.get("contracts", 0) or ep.get("contractSize", 0) or 0)
                    if qty == 0:
                        continue
                    side_raw = ep.get("side", "long")
                    side = "buy" if side_raw == "long" else "sell"
                    entry_price = float(ep.get("entryPrice", 0) or 0)
                    unrealised = float(ep.get("unrealizedPnl", 0) or 0)
                    # Build position dict matching PaperExecutor interface
                    self._positions[symbol] = {
                        "symbol": symbol,
                        "side": side,
                        "entry_price": entry_price,
                        "current_price": float(ep.get("markPrice", entry_price) or entry_price),
                        "quantity": abs(qty),
                        "stop_loss": float(ep.get("stopLoss", 0) or 0),
                        "take_profit": float(ep.get("takeProfit", 0) or 0),
                        "size_usdt": abs(qty) * entry_price,
                        "entry_size_usdt": abs(qty) * entry_price,
                        "unrealized_pnl": unrealised,
                        "score": 0.0,
                        "regime": "",
                        "models_fired": [],
                        "timeframe": "",
                        "rationale": "hydrated_from_exchange",
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "entry_order_id": "",
                    }
                logger.info(
                    "LiveBridge: hydrated %d position(s) from exchange",
                    len(self._positions),
                )
        except Exception as exc:
            logger.error("LiveBridge: position hydration failed: %s", exc)

    # ── Balance (F-04) ────────────────────────────────────────
    def _fetch_usdt_balance(self, force: bool = False) -> float:
        """Fetch free USDT from exchange with 30-second cache."""
        now = time.monotonic()
        if not force:
            with self._lock:
                if now - self._balance_cache["ts"] < self._BALANCE_CACHE_TTL:
                    return self._balance_cache["usdt"]
        if not self._exchange_adapter:
            return self._balance_cache.get("usdt", 0.0)
        try:
            bal = self._exchange_adapter.fetch_balance()
            usdt = float(bal.get("USDT", {}).get("free", 0.0) if isinstance(bal.get("USDT"), dict)
                         else bal.get("free", {}).get("USDT", 0.0))
            with self._lock:
                self._balance_cache = {"usdt": usdt, "ts": now}
                if usdt > self._peak_usdt:
                    self._peak_usdt = usdt
                if self._initial_usdt == 0.0 and usdt > 0:
                    self._initial_usdt = usdt
            return usdt
        except Exception as exc:
            logger.warning("LiveBridge: balance fetch failed: %s", exc)
            return self._balance_cache.get("usdt", 0.0)

    @property
    def available_capital(self) -> float:
        """Free USDT balance from exchange (not simulated)."""
        if not self._initialised:
            return 0.0
        try:
            return self._fetch_usdt_balance()
        except Exception:
            return 0.0

    @property
    def drawdown_pct(self) -> float:
        """Drawdown from peak exchange balance."""
        if not self._initialised or self._peak_usdt <= 0:
            return 0.0
        current = self._fetch_usdt_balance()
        with self._lock:
            dd = (self._peak_usdt - current) / self._peak_usdt * 100.0
        return max(0.0, dd)

    # ── Position queries (PaperExecutor-compatible) ───────────
    def get_open_positions(self) -> List[dict]:
        with self._lock:
            # Return flat list (PaperExecutor returns list, not dict)
            return list(self._positions.values())

    def get_closed_trades(self) -> List[dict]:
        with self._lock:
            return list(self._closed_trades)

    def get_stats(self) -> dict:
        """Summary stats matching PaperExecutor interface."""
        with self._lock:
            closed = list(self._closed_trades)
        n = len(closed)
        wins = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
        pnl_list = [t.get("pnl_usdt", 0.0) for t in closed]
        total_pnl = sum(pnl_list)
        gross_win = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        return {
            "total_trades": n,
            "win_rate": round(wins / n * 100, 2) if n else 0.0,
            "total_pnl_usdt": round(total_pnl, 2),
            "wins": wins,
            "losses": n - wins,
            "best_trade_usdt": round(max(pnl_list), 2) if pnl_list else 0.0,
            "worst_trade_usdt": round(min(pnl_list), 2) if pnl_list else 0.0,
            "avg_duration_s": round(sum(t.get("duration_s", 0) for t in closed) / n) if n else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0.0,
            "open_positions": len(self._positions),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "available_capital": round(self.available_capital, 2),
        }

    # ── Order Submission (F-01) ───────────────────────────────
    def submit(self, candidate) -> bool:
        """
        Submit an OrderCandidate for live execution.
        Bridges to Phase 8 LiveExecutor.execute().
        """
        if not self._initialised:
            logger.error("LiveBridge: cannot submit — components not initialised")
            return False
        if not self._trading_allowed:
            logger.warning(
                "LiveBridge: trading NOT allowed (recovery incomplete) — rejecting %s",
                getattr(candidate, "symbol", "?"),
            )
            return False

        if getattr(candidate, "requires_confirmation", False):
            with self._lock:
                cid = getattr(candidate, "candidate_id", None) or str(uuid.uuid4())
                self._pending_confirmations[cid] = candidate
            logger.info(
                "LiveBridge: stored pending confirmation for %s %s (id=%s)",
                candidate.side, candidate.symbol, cid,
            )
            bus.publish(Topics.CONFIRMATION_REQUIRED, data=candidate.to_dict(), source="live_bridge")
            return True

        return self._execute_candidate(candidate)

    def confirm_and_execute(self, candidate_id: str) -> bool:
        """User manually approves a pending candidate."""
        with self._lock:
            candidate = self._pending_confirmations.pop(candidate_id, None)
        if candidate is None:
            logger.warning("LiveBridge: confirm_and_execute — candidate_id '%s' not found", candidate_id)
            return False
        return self._execute_candidate(candidate)

    def reject_pending(self, candidate_id: str) -> bool:
        """User rejects a pending candidate."""
        with self._lock:
            candidate = self._pending_confirmations.pop(candidate_id, None)
        if candidate is None:
            return False
        bus.publish(Topics.SIGNAL_REJECTED, data={"candidate_id": candidate_id}, source="live_bridge")
        return True

    def get_pending_confirmations(self) -> list:
        with self._lock:
            return list(self._pending_confirmations.values())

    def _execute_candidate(self, candidate) -> bool:
        """
        Convert OrderCandidate → ExecutionRequest and call Phase 8 LiveExecutor.
        Then place server-side SL order (F-02).
        """
        symbol = candidate.symbol
        side = candidate.side  # "buy" or "sell"

        # Check duplicate symbol
        with self._lock:
            if symbol in self._positions:
                logger.warning("LiveBridge: already have position in %s — skipping", symbol)
                return False

        # ── F-04: Fresh balance check before sizing ──
        fresh_balance = self._fetch_usdt_balance(force=True)
        size_usdt = getattr(candidate, "position_size_usdt", 0.0) or 0.0
        if size_usdt > fresh_balance * 0.5:  # Hard safety cap: no single trade > 50% of balance
            logger.warning(
                "LiveBridge: position size $%.2f exceeds 50%% of exchange balance $%.2f — rejecting",
                size_usdt, fresh_balance,
            )
            return False
        if fresh_balance <= 0:
            logger.error("LiveBridge: exchange balance is zero or negative — rejecting")
            return False

        # Build ExecutionRequest-like object for Phase 8 LiveExecutor
        try:
            from core.intraday.execution_contracts import ExecutionRequest, Side
        except ImportError:
            logger.error("LiveBridge: cannot import ExecutionRequest — Phase 8 contracts missing")
            return False

        entry_price = getattr(candidate, "entry_price", 0.0) or 0.0
        if entry_price <= 0:
            logger.error("LiveBridge: invalid entry_price=%.6f for %s", entry_price, symbol)
            return False

        quantity = size_usdt / entry_price if entry_price > 0 else 0.0
        now_ms = int(time.time() * 1000)

        try:
            request = ExecutionRequest(
                request_id=str(uuid.uuid4()),
                decision_id=getattr(candidate, "candidate_id", "") or str(uuid.uuid4()),
                trigger_id=getattr(candidate, "candidate_id", "") or "",
                setup_id="",
                symbol=symbol,
                side=Side.BUY if side == "buy" else Side.SELL,
                entry_price=entry_price,
                stop_loss=getattr(candidate, "stop_loss_price", 0.0) or 0.0,
                take_profit=getattr(candidate, "take_profit_price", 0.0) or 0.0,
                size_usdt=size_usdt,
                quantity=quantity,
                strategy_name=",".join(getattr(candidate, "models_fired", [])),
                strategy_class="idss",
                regime=getattr(candidate, "regime", ""),
                created_at_ms=now_ms,
            )
        except Exception as exc:
            logger.error("LiveBridge: failed to build ExecutionRequest: %s", exc)
            return False

        # ── Execute via Phase 8 LiveExecutor ──
        try:
            order_record, fill_record = self._phase8_executor.execute(request)
        except Exception as exc:
            logger.error("LiveBridge: Phase 8 execution failed for %s: %s", symbol, exc)
            return False

        if order_record.status in ("rejected", "failed"):
            logger.warning(
                "LiveBridge: order %s for %s — reason: %s",
                order_record.status, symbol, order_record.failure_reason,
            )
            return False

        if fill_record is None:
            logger.warning("LiveBridge: no fill for %s — order status=%s", symbol, order_record.status)
            return False

        # ── Build position dict (PaperExecutor-compatible) ──
        fill_price = fill_record.price
        fill_qty = fill_record.quantity
        filled_size_usdt = fill_price * fill_qty
        opened_at = datetime.now(timezone.utc).isoformat()

        position = {
            "symbol": symbol,
            "side": side,
            "entry_price": fill_price,
            "current_price": fill_price,
            "quantity": fill_qty,
            "stop_loss": getattr(candidate, "stop_loss_price", 0.0) or 0.0,
            "take_profit": getattr(candidate, "take_profit_price", 0.0) or 0.0,
            "size_usdt": filled_size_usdt,
            "entry_size_usdt": filled_size_usdt,
            "unrealized_pnl": 0.0,
            "score": getattr(candidate, "score", 0.0),
            "regime": getattr(candidate, "regime", ""),
            "models_fired": list(getattr(candidate, "models_fired", [])),
            "timeframe": getattr(candidate, "timeframe", ""),
            "rationale": getattr(candidate, "rationale", ""),
            "opened_at": opened_at,
            "entry_order_id": order_record.order_id,
        }

        with self._lock:
            self._positions[symbol] = position

        logger.info(
            "LiveBridge: POSITION OPENED %s %s @ %.4f qty=%.6f size=$%.2f [Phase8 order=%s]",
            side, symbol, fill_price, fill_qty, filled_size_usdt, order_record.order_id,
        )

        # ── F-02: Place server-side SL order ──
        self._place_server_side_stop(symbol, position)

        # Invalidate balance cache
        with self._lock:
            self._balance_cache["ts"] = 0.0

        # Publish trade opened event
        bus.publish(Topics.TRADE_OPENED, data=position, source="live_bridge")

        return True

    # ── Server-Side Stop Loss (F-02) ──────────────────────────
    def _place_server_side_stop(self, symbol: str, position: dict) -> None:
        """
        Place a stop-market order on the exchange as server-side protection.
        If this fails, log a CRITICAL warning but don't block the trade.
        """
        if not self._exchange_adapter:
            return
        sl_price = position.get("stop_loss", 0.0)
        if sl_price <= 0:
            logger.warning("LiveBridge: no SL price for %s — server-side stop NOT placed", symbol)
            return

        side = position.get("side", "buy")
        close_side = "sell" if side == "buy" else "buy"
        qty = position.get("quantity", 0.0)

        try:
            response = self._exchange_adapter.create_order(
                symbol=symbol,
                order_type="stop",
                side=close_side,
                quantity=qty,
                price=None,  # market stop
                params={
                    "stopPrice": sl_price,
                    "reduceOnly": True,
                    "triggerPrice": sl_price,
                },
            )
            if response.success:
                with self._lock:
                    self._sl_orders[symbol] = response.exchange_order_id
                logger.info(
                    "LiveBridge: SERVER-SIDE SL placed for %s at %.4f (order=%s)",
                    symbol, sl_price, response.exchange_order_id,
                )
            else:
                err = response.error
                logger.error(
                    "LiveBridge: FAILED to place server-side SL for %s at %.4f — %s",
                    symbol, sl_price, err.message if err else "unknown",
                )
                bus.publish(Topics.SYSTEM_ALERT, {
                    "type": "sl_placement_failed",
                    "symbol": symbol,
                    "stop_loss": sl_price,
                    "message": f"Server-side SL FAILED for {symbol}",
                    "severity": "critical",
                }, source="live_bridge")
        except Exception as exc:
            logger.error(
                "LiveBridge: exception placing server-side SL for %s: %s", symbol, exc,
            )

    # ── Tick Monitoring (backup SL/TP) ────────────────────────
    def on_tick(self, symbol: str, price: float) -> None:
        """Client-side SL/TP backup monitor (exchange stops are primary)."""
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return
            pos["current_price"] = price
            entry = pos["entry_price"]
            side = pos["side"]
            # Compute unrealized PnL %
            if entry > 0:
                if side == "buy":
                    pos["unrealized_pnl"] = (price - entry) / entry * 100
                else:
                    pos["unrealized_pnl"] = (entry - price) / entry * 100

        # Check SL/TP (client-side backup — server-side is primary)
        sl = pos.get("stop_loss", 0.0)
        tp = pos.get("take_profit", 0.0)
        exit_reason = None

        if side == "buy":
            if sl > 0 and price <= sl:
                exit_reason = "stop_loss"
            elif tp > 0 and price >= tp:
                exit_reason = "take_profit"
        else:
            if sl > 0 and price >= sl:
                exit_reason = "stop_loss"
            elif tp > 0 and price <= tp:
                exit_reason = "take_profit"

        if exit_reason:
            logger.info(
                "LiveBridge: CLIENT-SIDE %s triggered for %s at %.4f (server-side SL may have already filled)",
                exit_reason, symbol, price,
            )
            self._close_position_on_exchange(symbol, exit_reason)
        else:
            bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")

    # ── Position Close ────────────────────────────────────────
    def _close_position_on_exchange(self, symbol: str, exit_reason: str = "manual_close") -> bool:
        """Close a position via market order on the exchange."""
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return False

        if not self._exchange_adapter:
            logger.error("LiveBridge: no exchange adapter — cannot close %s", symbol)
            return False

        side = pos["side"]
        close_side = "sell" if side == "buy" else "buy"
        qty = pos["quantity"]

        try:
            response = self._exchange_adapter.create_order(
                symbol=symbol,
                order_type="market",
                side=close_side,
                quantity=qty,
                params={"reduceOnly": True},
            )
            if not response.success:
                logger.error("LiveBridge: close order failed for %s: %s", symbol, response.error)
                return False

            exit_price = response.avg_price if response.avg_price > 0 else pos["current_price"]

        except Exception as exc:
            logger.error("LiveBridge: exception closing %s: %s", symbol, exc)
            return False

        # Cancel server-side SL if still active
        self._cancel_sl_order(symbol)

        # Calculate PnL
        entry = pos["entry_price"]
        if side == "buy":
            pnl_usdt = (exit_price - entry) * qty
        else:
            pnl_usdt = (entry - exit_price) * qty
        pnl_pct = (pnl_usdt / (entry * qty)) * 100 if entry * qty > 0 else 0.0

        trade = {
            **pos,
            "exit_price": exit_price,
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": exit_reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": 0,
        }
        # Compute duration
        try:
            opened = datetime.fromisoformat(pos["opened_at"])
            trade["duration_s"] = int((datetime.now(timezone.utc) - opened).total_seconds())
        except Exception:
            pass

        with self._lock:
            self._positions.pop(symbol, None)
            self._closed_trades.append(trade)
            self._balance_cache["ts"] = 0.0  # invalidate

        logger.info(
            "LiveBridge: POSITION CLOSED %s %s @ %.4f PnL=$%.2f (%.2f%%) reason=%s",
            side, symbol, exit_price, pnl_usdt, pnl_pct, exit_reason,
        )

        bus.publish(Topics.TRADE_CLOSED, data=trade, source="live_bridge")
        return True

    def _cancel_sl_order(self, symbol: str) -> None:
        """Cancel the server-side SL order for a symbol."""
        with self._lock:
            sl_oid = self._sl_orders.pop(symbol, None)
        if sl_oid and self._exchange_adapter:
            try:
                self._exchange_adapter.cancel_order(sl_oid, symbol)
                logger.info("LiveBridge: cancelled server-side SL order %s for %s", sl_oid, symbol)
            except Exception as exc:
                logger.warning("LiveBridge: SL cancel failed for %s (%s): %s", symbol, sl_oid, exc)

    def close_position(self, symbol: str, price: float = None) -> bool:
        return self._close_position_on_exchange(symbol, "manual_close")

    def close_all(self) -> int:
        """Close all open positions."""
        with self._lock:
            symbols = list(self._positions.keys())
        count = 0
        for sym in symbols:
            if self._close_position_on_exchange(sym, "close_all"):
                count += 1
        return count

    def close_all_longs(self, exit_reason: str = "close_all_longs") -> int:
        """Close all long (buy) positions."""
        with self._lock:
            longs = [s for s, p in self._positions.items() if p.get("side") == "buy"]
        count = 0
        for sym in longs:
            if self._close_position_on_exchange(sym, exit_reason):
                count += 1
        return count

    def partial_close(self, symbol: str, reduce_pct: float) -> bool:
        """Partially close a position."""
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return False

        if not self._exchange_adapter:
            return False

        side = pos["side"]
        close_side = "sell" if side == "buy" else "buy"
        close_qty = pos["quantity"] * reduce_pct

        try:
            response = self._exchange_adapter.create_order(
                symbol=symbol,
                order_type="market",
                side=close_side,
                quantity=close_qty,
                params={"reduceOnly": True},
            )
            if not response.success:
                return False

            close_price = response.avg_price if response.avg_price > 0 else pos["current_price"]

            with self._lock:
                pos["quantity"] -= close_qty
                pos["size_usdt"] = pos["quantity"] * pos["entry_price"]
                self._balance_cache["ts"] = 0.0

            logger.info(
                "LiveBridge: partial close %.0f%% of %s — closed %.6f @ %.4f",
                reduce_pct * 100, symbol, close_qty, close_price,
            )
            bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")
            return True
        except Exception as exc:
            logger.error("LiveBridge: partial_close failed for %s: %s", symbol, exc)
            return False

    def move_all_longs_to_breakeven(self) -> int:
        """Move all long SLs to entry price (breakeven)."""
        with self._lock:
            longs = [(s, dict(p)) for s, p in self._positions.items() if p.get("side") == "buy"]
        count = 0
        for sym, pos in longs:
            entry = pos.get("entry_price", 0)
            if entry > 0:
                self.adjust_stop(sym, entry)
                count += 1
        return count

    def adjust_stop(self, symbol: str, new_stop: float) -> bool:
        """Adjust SL for a position and update server-side stop."""
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return False
            pos["stop_loss"] = new_stop

        # Cancel old SL and place new one
        self._cancel_sl_order(symbol)
        self._place_server_side_stop(symbol, pos)
        bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")
        return True

    # ── Periodic Reconciliation (F-06) ────────────────────────
    def run_reconciliation(self) -> dict:
        """Run reconciliation engine and return result."""
        if not self._initialised or not self._reconciliation_engine:
            return {"success": False, "errors": ["not_initialised"]}

        with self._lock:
            internal_orders = {}
            if self._phase8_executor:
                internal_orders = self._phase8_executor.get_all_orders()
            internal_positions = {
                sym: {"symbol": sym, "side": p["side"], "quantity": p["quantity"]}
                for sym, p in self._positions.items()
            }

        try:
            result = self._reconciliation_engine.reconcile(
                internal_orders=internal_orders,
                internal_positions=internal_positions,
                auto_resolve=True,
            )

            if result.has_mismatches:
                logger.warning(
                    "LiveBridge: RECONCILIATION found %d mismatch(es) — %s",
                    result.mismatch_count,
                    [m.to_dict() for m in result.mismatches],
                )
                # Re-hydrate positions from exchange on any mismatch
                self._hydrate_positions_from_exchange()

                bus.publish(Topics.SYSTEM_ALERT, {
                    "type": "reconciliation_mismatch",
                    "mismatch_count": result.mismatch_count,
                    "message": f"Reconciliation found {result.mismatch_count} mismatch(es)",
                    "severity": "high" if result.mismatch_count > 1 else "medium",
                }, source="live_bridge")
            else:
                logger.debug("LiveBridge: reconciliation clean — 0 mismatches")

            return result.to_dict()
        except Exception as exc:
            logger.error("LiveBridge: reconciliation failed: %s", exc)
            return {"success": False, "errors": [str(exc)]}

    # ── State for debugging ───────────────────────────────────
    def get_state(self) -> dict:
        with self._lock:
            return {
                "initialised": self._initialised,
                "recovery_complete": self._recovery_complete,
                "trading_allowed": self._trading_allowed,
                "open_positions": len(self._positions),
                "closed_trades": len(self._closed_trades),
                "sl_orders_active": len(self._sl_orders),
                "pending_confirmations": len(self._pending_confirmations),
                "balance_cache_usdt": self._balance_cache.get("usdt", 0.0),
                "peak_usdt": self._peak_usdt,
            }


# Module-level singleton
live_bridge = LiveBridge()
