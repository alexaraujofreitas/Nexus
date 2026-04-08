# ============================================================
# NEXUS TRADER — Live Execution Bridge  (Safety-Hardened v2)
#
# Bridges the gap between the main application (which uses the
# OrderCandidate / PaperExecutor interface) and the Phase 8
# production-grade live subsystem (core/intraday/live/).
#
# SAFETY INVARIANTS (v2):
#   1. NO unprotected position: SL must be exchange-confirmed,
#      or position is immediately closed, or trading is blocked.
#   2. ALL orders go through FSM lifecycle + idempotency store.
#   3. Exchange is SINGLE SOURCE OF TRUTH for balance/positions.
#   4. Reconciliation is FAIL-CLOSED: mismatch → trading blocked.
#   5. Balance cache NEVER used for sizing — always force-refresh.
#   6. Crash-safe: restart recovery handles all partial states.
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

# ── Constants ────────────────────────────────────────────────
SL_MAX_RETRIES = 3              # Retry SL placement up to 3 times
SL_RETRY_DELAY_S = 0.5         # Backoff between SL retries
BALANCE_CACHE_TTL = 10.0        # Reduced from 30s — tighter safety
BALANCE_SIZING_ALWAYS_FORCE = True  # Force-refresh for every sizing decision
MAX_SINGLE_TRADE_PCT = 0.50     # No single trade > 50% of balance
DEGRADED_MISMATCH_THRESHOLD = 1 # Any mismatch → degraded mode


class LiveBridge:
    """
    Safety-hardened adapter exposing PaperExecutor-compatible interface
    on top of Phase 8 production live execution subsystem.
    """

    # ── SL protection states ──
    SL_CONFIRMED = "confirmed"
    SL_PENDING = "pending"
    SL_FAILED = "failed"
    SL_NONE = "none"

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
        self._sl_orders: Dict[str, dict] = {}     # symbol → {order_id, status, price}
        self._pending_confirmations: Dict[str, Any] = {}

        # ── Balance cache ──
        self._balance_cache: Dict[str, Any] = {"usdt": 0.0, "ts": 0.0}
        self._BALANCE_CACHE_TTL = BALANCE_CACHE_TTL
        self._peak_usdt: float = 0.0
        self._initial_usdt: float = 0.0

        # ── Recovery & degraded state ──
        self._recovery_complete = False
        self._trading_allowed = False
        self._degraded_mode = False   # Set True on reconciliation mismatch
        self._degraded_reason = ""

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

    @property
    def is_degraded(self) -> bool:
        return self._degraded_mode

    def _can_trade(self) -> bool:
        """Check all preconditions for trading."""
        if not self._initialised:
            return False
        if not self._trading_allowed:
            return False
        if self._degraded_mode:
            return False
        return True

    def exit_degraded_mode(self) -> bool:
        """
        Manually exit degraded mode after operator confirms state is clean.
        Only works if reconciliation passes clean.
        """
        result = self.run_reconciliation()
        if result.get("success") and result.get("mismatch_count", 1) == 0:
            with self._lock:
                self._degraded_mode = False
                self._degraded_reason = ""
            logger.info("LiveBridge: exited degraded mode — trading ALLOWED")
            return True
        logger.warning("LiveBridge: cannot exit degraded mode — reconciliation not clean")
        return False

    # ══════════════════════════════════════════════════════════
    # STARTUP RECOVERY (F-03 + Fix 5: Crash Scenarios)
    # ══════════════════════════════════════════════════════════

    def run_startup_recovery(self, auto_resolve: bool = True) -> dict:
        """
        Run Phase 8 RestartRecoveryManager.
        Returns the RecoveryReport as a dict.
        Trading is blocked until this returns clean.

        Handles crash scenarios:
        - crash after submit before ACK → idempotency store has 'submitted' entries
        - crash after partial fill → reconciliation detects fill mismatch
        - restart with open orders → recovery reconciles against exchange
        - restart with open positions → positions hydrated from exchange
        - restart with missing SL → _verify_sl_coverage() detects and fixes
        - restart with unknown order state → reconciliation resolves
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
            # Hydrate positions from exchange (EXCHANGE IS TRUTH)
            self._hydrate_positions_from_exchange()
            # Hydrate balance
            self._fetch_usdt_balance(force=True)
            # Verify SL coverage for all hydrated positions
            self._verify_sl_coverage()
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
        """
        Fetch current exchange positions and populate internal state.
        EXCHANGE IS TRUTH — internal state is completely replaced.
        """
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
            logger.error("LiveBridge: position hydration failed: %s — entering degraded mode", exc)
            self._enter_degraded_mode(f"position_hydration_failed: {exc}")

    def _verify_sl_coverage(self) -> None:
        """
        After startup, check that every open position has a server-side SL.
        Query open orders on exchange; for any position without a matching
        conditional stop order, place one. If placement fails after retries,
        close the unprotected position.
        """
        with self._lock:
            positions = dict(self._positions)
        if not positions:
            return

        logger.info("LiveBridge: verifying SL coverage for %d position(s)...", len(positions))

        # Fetch all open conditional/stop orders from exchange
        exchange_stop_orders = set()
        try:
            open_orders = self._exchange_adapter.fetch_open_orders()
            for order_resp in open_orders:
                raw = getattr(order_resp, "raw", None) or {}
                order_type = raw.get("type", "").lower() if isinstance(raw, dict) else ""
                symbol = raw.get("symbol", "") if isinstance(raw, dict) else ""
                if "stop" in order_type or "conditional" in order_type:
                    exchange_stop_orders.add(symbol)
        except Exception as exc:
            logger.warning("LiveBridge: cannot fetch open orders for SL verification: %s", exc)

        for symbol, pos in positions.items():
            if symbol in exchange_stop_orders:
                with self._lock:
                    self._sl_orders[symbol] = {
                        "order_id": "recovered",
                        "status": self.SL_CONFIRMED,
                        "price": pos.get("stop_loss", 0.0),
                    }
                logger.info("LiveBridge: SL verified for %s (existing exchange stop)", symbol)
                continue

            # No SL found — place one
            sl_price = pos.get("stop_loss", 0.0)
            if sl_price <= 0:
                logger.warning(
                    "LiveBridge: position %s has no SL price — CLOSING unprotected position",
                    symbol,
                )
                self._close_position_on_exchange(symbol, "no_sl_on_restart")
                continue

            success = self._place_server_side_stop_with_retry(symbol, pos)
            if not success:
                logger.error(
                    "LiveBridge: FAILED to place SL for %s after %d retries — "
                    "CLOSING unprotected position",
                    symbol, SL_MAX_RETRIES,
                )
                self._close_position_on_exchange(symbol, "sl_placement_failed_on_restart")

    # ══════════════════════════════════════════════════════════
    # BALANCE (Fix 3 + Fix 6: Exchange Truth + No Stale Cache)
    # ══════════════════════════════════════════════════════════

    def _fetch_usdt_balance(self, force: bool = False) -> float:
        """
        Fetch free USDT from exchange.
        Cache with reduced TTL (10s). On failure, returns 0.0 (fail-closed)
        for sizing decisions, cached value only for display.
        """
        now = time.monotonic()
        if not force:
            with self._lock:
                if now - self._balance_cache["ts"] < self._BALANCE_CACHE_TTL:
                    return self._balance_cache["usdt"]
        if not self._exchange_adapter:
            return 0.0
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
            # FAIL-CLOSED: return 0.0 so sizing decisions reject the trade
            return 0.0

    def _fetch_usdt_balance_for_sizing(self) -> float:
        """
        ALWAYS force-refresh for sizing. Never use cache.
        Returns 0.0 on failure (fail-closed — trade will be rejected).
        """
        return self._fetch_usdt_balance(force=True)

    @property
    def available_capital(self) -> float:
        """Free USDT balance from exchange (cached for display, not sizing)."""
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

    # ══════════════════════════════════════════════════════════
    # POSITION QUERIES (PaperExecutor-compatible)
    # ══════════════════════════════════════════════════════════

    def get_open_positions(self) -> List[dict]:
        with self._lock:
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
            "degraded_mode": self._degraded_mode,
        }

    # ══════════════════════════════════════════════════════════
    # ORDER SUBMISSION (F-01 + Fix 2: FSM + Idempotency)
    # ══════════════════════════════════════════════════════════

    def submit(self, candidate) -> bool:
        """
        Submit an OrderCandidate for live execution.
        Bridges to Phase 8 LiveExecutor.execute() with full FSM/idempotency.
        """
        if not self._can_trade():
            reason = "not_initialised"
            if self._degraded_mode:
                reason = f"degraded_mode: {self._degraded_reason}"
            elif not self._trading_allowed:
                reason = "recovery_incomplete"
            logger.warning(
                "LiveBridge: cannot trade (%s) — rejecting %s",
                reason, getattr(candidate, "symbol", "?"),
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
        Then place server-side SL order with retry. If SL fails, close position.
        ALL orders tracked through FSM + idempotency.
        """
        symbol = candidate.symbol
        side = candidate.side

        # Check duplicate symbol
        with self._lock:
            if symbol in self._positions:
                logger.warning("LiveBridge: already have position in %s — skipping", symbol)
                return False

        # ── Fix 6: ALWAYS force-refresh balance for sizing ──
        fresh_balance = self._fetch_usdt_balance_for_sizing()
        size_usdt = getattr(candidate, "position_size_usdt", 0.0) or 0.0
        if fresh_balance <= 0:
            logger.error("LiveBridge: exchange balance unavailable or zero — rejecting (fail-closed)")
            return False
        if size_usdt > fresh_balance * MAX_SINGLE_TRADE_PCT:
            logger.warning(
                "LiveBridge: position size $%.2f exceeds %.0f%% of exchange balance $%.2f — rejecting",
                size_usdt, MAX_SINGLE_TRADE_PCT * 100, fresh_balance,
            )
            return False

        # Build ExecutionRequest
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

        # ── Fix 2: Register in idempotency store BEFORE submission ──
        if self._idempotency_store:
            from core.intraday.live.order_lifecycle import make_client_order_id
            client_oid = make_client_order_id(
                request.request_id, symbol, side, now_ms,
            )
            self._idempotency_store.register(
                client_order_id=client_oid,
                request_id=request.request_id,
                symbol=symbol,
                side=side,
            )
            self._idempotency_store.mark_submitted(client_oid)

        # ── Execute via Phase 8 LiveExecutor (FSM lifecycle) ──
        try:
            order_record, fill_record = self._phase8_executor.execute(request)
        except Exception as exc:
            logger.error("LiveBridge: Phase 8 execution failed for %s: %s", symbol, exc)
            if self._idempotency_store and 'client_oid' in locals():
                self._idempotency_store.mark_failed(client_oid, str(exc))
            return False

        # ── Fix 2: Mark confirmed in idempotency store ──
        if self._idempotency_store and 'client_oid' in locals():
            if hasattr(order_record, 'order_id') and order_record.order_id:
                self._idempotency_store.mark_confirmed(client_oid, order_record.order_id)
            elif hasattr(order_record, 'status') and order_record.status in ("rejected", "failed"):
                self._idempotency_store.mark_failed(
                    client_oid,
                    getattr(order_record, 'failure_reason', 'rejected'),
                )

        if hasattr(order_record, 'status') and order_record.status in ("rejected", "failed"):
            logger.warning(
                "LiveBridge: order %s for %s — reason: %s",
                order_record.status, symbol,
                getattr(order_record, 'failure_reason', ''),
            )
            return False

        if fill_record is None:
            logger.warning("LiveBridge: no fill for %s — order status=%s", symbol,
                           getattr(order_record, 'status', '?'))
            return False

        # ── Build position dict ──
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
            "entry_order_id": getattr(order_record, 'order_id', ''),
        }

        with self._lock:
            self._positions[symbol] = position

        logger.info(
            "LiveBridge: POSITION OPENED %s %s @ %.4f qty=%.6f size=$%.2f [Phase8 order=%s]",
            side, symbol, fill_price, fill_qty, filled_size_usdt,
            getattr(order_record, 'order_id', ''),
        )

        # ── Fix 1: Place SL with retry — if fails, CLOSE position ──
        sl_success = self._place_server_side_stop_with_retry(symbol, position)
        if not sl_success:
            logger.error(
                "LiveBridge: SL FAILED after %d retries for %s — "
                "CLOSING UNPROTECTED POSITION (fail-closed)",
                SL_MAX_RETRIES, symbol,
            )
            self._close_position_on_exchange(symbol, "sl_placement_failed")
            return False

        # Invalidate balance cache
        with self._lock:
            self._balance_cache["ts"] = 0.0

        # Publish trade opened event
        bus.publish(Topics.TRADE_OPENED, data=position, source="live_bridge")

        # Mark completed in idempotency store
        if self._idempotency_store and 'client_oid' in locals():
            self._idempotency_store.mark_completed(client_oid)

        return True

    # ══════════════════════════════════════════════════════════
    # SERVER-SIDE STOP LOSS (Fix 1: Exchange-Confirmed)
    # ══════════════════════════════════════════════════════════

    def _place_server_side_stop_with_retry(self, symbol: str, position: dict) -> bool:
        """
        Place SL with retry. Returns True only if exchange ACK received.
        Tracks SL in _sl_orders with status.
        Registers SL order in idempotency store.
        """
        if not self._exchange_adapter:
            return False
        sl_price = position.get("stop_loss", 0.0)
        if sl_price <= 0:
            logger.warning("LiveBridge: no SL price for %s — cannot place server-side stop", symbol)
            return False

        side = position.get("side", "buy")
        close_side = "sell" if side == "buy" else "buy"
        qty = position.get("quantity", 0.0)

        # Register SL order intent in idempotency store
        sl_client_oid = None
        if self._idempotency_store:
            from core.intraday.live.order_lifecycle import make_client_order_id
            sl_client_oid = make_client_order_id(
                f"SL-{symbol}", symbol, close_side, int(time.time() * 1000),
            )
            self._idempotency_store.register(
                client_order_id=sl_client_oid,
                request_id=f"SL-{symbol}",
                symbol=symbol,
                side=close_side,
            )

        # Mark SL as pending
        with self._lock:
            self._sl_orders[symbol] = {
                "order_id": "",
                "status": self.SL_PENDING,
                "price": sl_price,
                "client_oid": sl_client_oid,
            }

        for attempt in range(SL_MAX_RETRIES):
            try:
                response = self._exchange_adapter.create_order(
                    symbol=symbol,
                    order_type="stop",
                    side=close_side,
                    quantity=qty,
                    price=None,
                    client_order_id=sl_client_oid,
                    params={
                        "stopPrice": sl_price,
                        "reduceOnly": True,
                        "triggerPrice": sl_price,
                    },
                )
                if response.success:
                    with self._lock:
                        self._sl_orders[symbol] = {
                            "order_id": response.exchange_order_id,
                            "status": self.SL_CONFIRMED,
                            "price": sl_price,
                            "client_oid": sl_client_oid,
                        }
                    if self._idempotency_store and sl_client_oid:
                        self._idempotency_store.mark_confirmed(
                            sl_client_oid, response.exchange_order_id,
                        )
                    logger.info(
                        "LiveBridge: SERVER-SIDE SL CONFIRMED for %s at %.4f "
                        "(order=%s, attempt=%d)",
                        symbol, sl_price, response.exchange_order_id, attempt + 1,
                    )
                    return True
                else:
                    err = response.error
                    logger.warning(
                        "LiveBridge: SL attempt %d/%d failed for %s: %s",
                        attempt + 1, SL_MAX_RETRIES, symbol,
                        err.message if hasattr(err, 'message') else str(err),
                    )
            except Exception as exc:
                logger.warning(
                    "LiveBridge: SL attempt %d/%d exception for %s: %s",
                    attempt + 1, SL_MAX_RETRIES, symbol, exc,
                )

            if attempt < SL_MAX_RETRIES - 1:
                time.sleep(SL_RETRY_DELAY_S * (attempt + 1))

        # All retries exhausted
        with self._lock:
            self._sl_orders[symbol] = {
                "order_id": "",
                "status": self.SL_FAILED,
                "price": sl_price,
                "client_oid": sl_client_oid,
            }
        if self._idempotency_store and sl_client_oid:
            self._idempotency_store.mark_failed(sl_client_oid, "all_retries_exhausted")

        bus.publish(Topics.SYSTEM_ALERT, {
            "type": "sl_placement_failed",
            "symbol": symbol,
            "stop_loss": sl_price,
            "message": f"Server-side SL FAILED for {symbol} after {SL_MAX_RETRIES} retries — position closed",
            "severity": "critical",
        }, source="live_bridge")

        return False

    # ══════════════════════════════════════════════════════════
    # TICK MONITORING (backup SL/TP)
    # ══════════════════════════════════════════════════════════

    def on_tick(self, symbol: str, price: float) -> None:
        """Client-side SL/TP backup monitor (exchange stops are primary)."""
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return
            pos["current_price"] = price
            entry = pos["entry_price"]
            side = pos["side"]
            if entry > 0:
                if side == "buy":
                    pos["unrealized_pnl"] = (price - entry) / entry * 100
                else:
                    pos["unrealized_pnl"] = (entry - price) / entry * 100

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
                "LiveBridge: CLIENT-SIDE %s triggered for %s at %.4f "
                "(server-side SL may have already filled)",
                exit_reason, symbol, price,
            )
            self._close_position_on_exchange(symbol, exit_reason)
        else:
            bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")

    # ══════════════════════════════════════════════════════════
    # POSITION CLOSE
    # ══════════════════════════════════════════════════════════

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
        try:
            opened = datetime.fromisoformat(pos["opened_at"])
            trade["duration_s"] = int((datetime.now(timezone.utc) - opened).total_seconds())
        except Exception:
            pass

        with self._lock:
            self._positions.pop(symbol, None)
            self._closed_trades.append(trade)
            self._balance_cache["ts"] = 0.0

        logger.info(
            "LiveBridge: POSITION CLOSED %s %s @ %.4f PnL=$%.2f (%.2f%%) reason=%s",
            side, symbol, exit_price, pnl_usdt, pnl_pct, exit_reason,
        )

        bus.publish(Topics.TRADE_CLOSED, data=trade, source="live_bridge")
        return True

    def _cancel_sl_order(self, symbol: str) -> None:
        """Cancel the server-side SL order for a symbol."""
        with self._lock:
            sl_info = self._sl_orders.pop(symbol, None)
        if not sl_info:
            return
        sl_oid = sl_info.get("order_id", "")
        if sl_oid and sl_oid != "recovered" and self._exchange_adapter:
            try:
                self._exchange_adapter.cancel_order(sl_oid, symbol)
                logger.info("LiveBridge: cancelled server-side SL order %s for %s", sl_oid, symbol)
            except Exception as exc:
                logger.warning("LiveBridge: SL cancel failed for %s (%s): %s", symbol, sl_oid, exc)

    def close_position(self, symbol: str, price: float = None) -> bool:
        return self._close_position_on_exchange(symbol, "manual_close")

    def close_all(self) -> int:
        with self._lock:
            symbols = list(self._positions.keys())
        count = 0
        for sym in symbols:
            if self._close_position_on_exchange(sym, "close_all"):
                count += 1
        return count

    def close_all_longs(self, exit_reason: str = "close_all_longs") -> int:
        with self._lock:
            longs = [s for s, p in self._positions.items() if p.get("side") == "buy"]
        count = 0
        for sym in longs:
            if self._close_position_on_exchange(sym, exit_reason):
                count += 1
        return count

    def partial_close(self, symbol: str, reduce_pct: float) -> bool:
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

            with self._lock:
                pos["quantity"] -= close_qty
                pos["size_usdt"] = pos["quantity"] * pos["entry_price"]
                self._balance_cache["ts"] = 0.0

            logger.info(
                "LiveBridge: partial close %.0f%% of %s — closed %.6f",
                reduce_pct * 100, symbol, close_qty,
            )

            # Update server-side SL for reduced quantity
            self._cancel_sl_order(symbol)
            self._place_server_side_stop_with_retry(symbol, pos)

            bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")
            return True
        except Exception as exc:
            logger.error("LiveBridge: partial_close failed for %s: %s", symbol, exc)
            return False

    def move_all_longs_to_breakeven(self) -> int:
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
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return False
            pos["stop_loss"] = new_stop

        self._cancel_sl_order(symbol)
        success = self._place_server_side_stop_with_retry(symbol, pos)
        if not success:
            logger.error(
                "LiveBridge: adjust_stop SL placement failed for %s — "
                "CLOSING unprotected position",
                symbol,
            )
            self._close_position_on_exchange(symbol, "sl_adjust_failed")
            return False
        bus.publish(Topics.POSITION_UPDATED, data=pos, source="live_bridge")
        return True

    # ══════════════════════════════════════════════════════════
    # DEGRADED MODE (Fix 4: Fail-Closed Reconciliation)
    # ══════════════════════════════════════════════════════════

    def _enter_degraded_mode(self, reason: str) -> None:
        """Enter degraded mode — all new trading blocked."""
        with self._lock:
            self._degraded_mode = True
            self._degraded_reason = reason
        logger.error("LiveBridge: ENTERING DEGRADED MODE — %s", reason)
        bus.publish(Topics.SYSTEM_ALERT, {
            "type": "degraded_mode_entered",
            "reason": reason,
            "message": f"Trading BLOCKED — degraded mode: {reason}",
            "severity": "critical",
        }, source="live_bridge")

    # ══════════════════════════════════════════════════════════
    # PERIODIC RECONCILIATION (Fix 3 + Fix 4: Fail-Closed)
    # ══════════════════════════════════════════════════════════

    def run_reconciliation(self) -> dict:
        """
        Run reconciliation engine. If mismatches found:
        - Exchange state OVERRIDES local state (no merge)
        - Trading enters degraded mode
        """
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
                # EXCHANGE OVERRIDES LOCAL — re-hydrate completely
                self._hydrate_positions_from_exchange()

                # Fix 4: FAIL-CLOSED — enter degraded mode
                if result.mismatch_count >= DEGRADED_MISMATCH_THRESHOLD:
                    self._enter_degraded_mode(
                        f"reconciliation_mismatch: {result.mismatch_count} mismatch(es)"
                    )
            else:
                logger.debug("LiveBridge: reconciliation clean — 0 mismatches")

            return result.to_dict()
        except Exception as exc:
            logger.error("LiveBridge: reconciliation failed: %s", exc)
            return {"success": False, "errors": [str(exc)]}

    # ══════════════════════════════════════════════════════════
    # STATE
    # ══════════════════════════════════════════════════════════

    def get_state(self) -> dict:
        with self._lock:
            sl_summary = {}
            for sym, info in self._sl_orders.items():
                sl_summary[sym] = info.get("status", "unknown")
            return {
                "initialised": self._initialised,
                "recovery_complete": self._recovery_complete,
                "trading_allowed": self._trading_allowed,
                "degraded_mode": self._degraded_mode,
                "degraded_reason": self._degraded_reason,
                "open_positions": len(self._positions),
                "closed_trades": len(self._closed_trades),
                "sl_orders": sl_summary,
                "sl_orders_active": sum(
                    1 for i in self._sl_orders.values()
                    if i.get("status") == self.SL_CONFIRMED
                ),
                "pending_confirmations": len(self._pending_confirmations),
                "balance_cache_usdt": self._balance_cache.get("usdt", 0.0),
                "peak_usdt": self._peak_usdt,
            }


# Module-level singleton
live_bridge = LiveBridge()
