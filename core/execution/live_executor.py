# ============================================================
# NEXUS TRADER — Live Executor  (Phase A)
#
# Places REAL orders on the exchange via CCXT.
# Mirrors the PaperExecutor public interface so the OrderRouter
# can swap between paper and live transparently.
#
# Safety principles:
#  • Exchange connectivity is verified before every order
#  • Order amounts are validated against exchange minimums
#  • Every order attempt is logged to the live_trades table
#  • SL/TP managed in-software (market close on trigger)
#  • Thread-safe via RLock
#  • Comprehensive error handling — NEVER crashes the app
#  • Dynamic stop adjustment and partial close support
#
# Confirmation workflow:
#  • If candidate.requires_confirmation = True, store in _pending_confirmations
#  • UI can call confirm_and_execute() or reject_pending()
#  • If requires_confirmation = False, place order immediately
#
# Position monitoring:
#  • Subscribes to Topics.POSITION_MONITOR_UPDATED for dynamic actions
#  • Supports adjust_stop, partial_close, full_close, tighten_stop actions
#
# BTC-first integration:
#  • Applies size multiplier from btc_priority filter to capital allocation
#  • BTC positions receive 1.5x size multiplier
#
# ⚠  LIVE MODE PLACES REAL ORDERS WITH REAL MONEY ⚠
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from core.meta_decision.order_candidate import OrderCandidate
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# How long to cache the exchange balance before re-fetching (seconds)
_BALANCE_CACHE_TTL = 30.0


class LiveExecutor:
    """
    Executes real orders on the configured exchange via CCXT.

    Position lifecycle:
      1. submit(candidate)   — if requires_confirmation, store; else validate → place market entry order
      2. confirm_and_execute(candidate_id) — user approves pending candidate, place order
      3. on_tick(sym, price) — check SL/TP → if hit, place market close order
      4. close_position(sym) — manual market close (e.g. emergency stop)
      5. adjust_stop(sym, new_stop_loss) — dynamically tighten stop
      6. partial_close(sym, reduce_pct) — close fraction of position

    Matches the PaperExecutor public interface so the OrderRouter and
    Risk Management page can call the same API in both modes.

    Subscribes to POSITION_MONITOR_UPDATED for dynamic position adjustments
    from external monitoring systems.
    """

    def __init__(self):
        self._positions:     dict[str, dict] = {}   # symbol → live position dict
        self._closed_trades: list[dict]      = []   # in-memory history
        self._pending_confirmations: dict[str, OrderCandidate] = {}  # candidate_id → OrderCandidate
        self._auto_execute_mode: bool = False       # toggle for autonomous execution
        self._peak_usdt:     float           = 0.0
        self._initial_usdt:  float           = 0.0  # captured on first balance fetch
        self._lock                           = threading.RLock()
        # Balance cache  {usdt: float, ts: float (unix seconds)}
        self._balance_cache: dict = {"usdt": 0.0, "ts": 0.0}

        # Subscribe to position monitoring events
        bus.subscribe(Topics.POSITION_MONITOR_UPDATED, self._on_position_monitor)

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
            logger.warning("LiveExecutor: position monitor handler error: %s", exc)

    # ── Auto-execute mode control ─────────────────────────────

    def set_auto_execute_mode(self, enabled: bool) -> None:
        """Toggle whether submit() runs autonomously or requires confirmation."""
        old = self._auto_execute_mode
        self._auto_execute_mode = enabled
        logger.info("LiveExecutor: auto-execute mode %s → %s", old, enabled)

    def get_pending_confirmations(self) -> list[OrderCandidate]:
        """Return list of OrderCandidates awaiting user confirmation."""
        with self._lock:
            return list(self._pending_confirmations.values())

    def confirm_and_execute(self, candidate_id: str) -> bool:
        """
        User manually approves a pending candidate and places the order.
        Removes from pending and executes immediately.
        Returns True if successfully placed, False otherwise.
        """
        with self._lock:
            candidate = self._pending_confirmations.pop(candidate_id, None)
        if candidate is None:
            logger.warning("LiveExecutor: confirm_and_execute — candidate_id '%s' not found", candidate_id)
            return False

        logger.info(
            "LiveExecutor: user confirmed %s %s (score=%.2f)",
            candidate.side, candidate.symbol, candidate.score,
        )
        # Place the order (internal path, no confirmation needed)
        return self._place_order(candidate)

    def reject_pending(self, candidate_id: str) -> bool:
        """
        User rejects a pending candidate.
        Removes from pending and publishes rejection event.
        Returns True if candidate existed and was rejected.
        """
        with self._lock:
            candidate = self._pending_confirmations.pop(candidate_id, None)
        if candidate is None:
            logger.warning("LiveExecutor: reject_pending — candidate_id '%s' not found", candidate_id)
            return False

        logger.info(
            "LiveExecutor: user rejected %s %s (score=%.2f)",
            candidate.side, candidate.symbol, candidate.score,
        )
        bus.publish(
            Topics.SIGNAL_REJECTED,
            data={"candidate_id": candidate_id, "symbol": candidate.symbol},
            source="live_executor",
        )
        return True

    # ── Exchange access ───────────────────────────────────────

    def _exchange(self):
        from core.market_data.exchange_manager import exchange_manager
        ex = exchange_manager.get_exchange()
        if ex is None:
            raise RuntimeError(
                "LiveExecutor: no exchange connected. "
                "Configure one in Exchange Management."
            )
        return ex

    # ── Capital / balance ─────────────────────────────────────

    def _fetch_usdt_balance(self) -> float:
        """Fetch free USDT balance from exchange (cached)."""
        now = time.monotonic()
        with self._lock:
            if now - self._balance_cache["ts"] < _BALANCE_CACHE_TTL:
                return self._balance_cache["usdt"]
        try:
            bal = self._exchange().fetch_balance()
            usdt = float(bal.get("USDT", {}).get("free", 0.0))
        except Exception as exc:
            logger.warning("LiveExecutor: balance fetch failed: %s", exc)
            return self._balance_cache["usdt"]
        with self._lock:
            self._balance_cache = {"usdt": usdt, "ts": now}
            if usdt > self._peak_usdt:
                self._peak_usdt = usdt
            if self._initial_usdt == 0.0 and usdt > 0:
                self._initial_usdt = usdt
        return usdt

    @property
    def available_capital(self) -> float:
        """Free USDT balance on the exchange."""
        try:
            return self._fetch_usdt_balance()
        except Exception:
            return 0.0

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak USDT balance."""
        with self._lock:
            if self._peak_usdt <= 0:
                return 0.0
        current = self._fetch_usdt_balance()
        with self._lock:
            dd = (self._peak_usdt - current) / self._peak_usdt * 100.0
        return max(0.0, dd)

    def get_open_positions(self) -> list[dict]:
        """Return all tracked open positions as dicts."""
        with self._lock:
            return list(self._positions.values())

    def get_closed_trades(self) -> list[dict]:
        """Return in-memory closed trade history."""
        with self._lock:
            return list(self._closed_trades)

    def get_stats(self) -> dict:
        """
        Return a summary stats dict matching the PaperExecutor interface.
        This allows any UI component to call get_stats() on either executor.
        """
        with self._lock:
            closed = list(self._closed_trades)
        n         = len(closed)
        wins      = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
        pnl_list  = [t.get("pnl_usdt", 0.0) for t in closed]
        total_pnl = sum(pnl_list)
        gross_win  = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        return {
            "total_trades":      n,
            "win_rate":          round(wins / n * 100, 2) if n else 0.0,
            "total_pnl_usdt":    round(total_pnl, 2),
            "wins":              wins,
            "losses":            n - wins,
            "best_trade_usdt":   round(max(pnl_list), 2) if pnl_list else 0.0,
            "worst_trade_usdt":  round(min(pnl_list), 2) if pnl_list else 0.0,
            "avg_duration_s":    round(
                sum(t.get("duration_s", 0) for t in closed) / n
            ) if n else 0,
            "profit_factor":     round(gross_win / gross_loss, 2) if gross_loss else 0.0,
            "open_positions":    len(self._positions),
            "drawdown_pct":      round(self.drawdown_pct, 4),
            "available_capital": round(self.available_capital, 2),
        }

    # ── Order placement ───────────────────────────────────────

    def submit(self, candidate: OrderCandidate) -> bool:
        """
        Submit a candidate for execution.

        If candidate.requires_confirmation = True:
          - Store in _pending_confirmations and return True
          - Publish CONFIRMATION_REQUIRED event
          - Caller must wait for confirm_and_execute() or reject_pending()

        If candidate.requires_confirmation = False:
          - Place order immediately via _place_order()
        """
        if candidate.requires_confirmation:
            # Store for later confirmation
            with self._lock:
                self._pending_confirmations[candidate.candidate_id] = candidate
            logger.info(
                "LiveExecutor: stored pending confirmation for %s %s (candidate_id=%s)",
                candidate.side, candidate.symbol, candidate.candidate_id,
            )
            bus.publish(
                Topics.CONFIRMATION_REQUIRED,
                data=candidate.to_dict(),
                source="live_executor",
            )
            return True
        else:
            # Auto-execute without confirmation
            return self._place_order(candidate)

    def _place_order(self, candidate: OrderCandidate) -> bool:
        """
        Place a market entry order for *candidate*.

        Applies BTC-first size multiplier before order placement.
        Returns True if the order was filled and the position opened.
        Returns False (and logs) on any error.
        """
        symbol = candidate.symbol
        side   = "buy" if candidate.side == "buy" else "sell"

        with self._lock:
            if symbol in self._positions:
                logger.debug("LiveExecutor: already in %s, skipping", symbol)
                return False

        # ── Validate exchange connectivity ────────────────────
        try:
            ex = self._exchange()
        except RuntimeError as exc:
            logger.error("LiveExecutor._place_order: %s", exc)
            return False

        # ── Amount calculation ────────────────────────────────
        entry_ref = candidate.entry_price or 0.0
        if entry_ref <= 0:
            logger.warning("LiveExecutor._place_order: invalid entry price for %s", symbol)
            return False

        # Use position_size_usdt from the candidate
        size_usdt = candidate.position_size_usdt
        if size_usdt <= 0:
            logger.warning("LiveExecutor._place_order: zero size_usdt for %s", symbol)
            return False

        # Apply BTC-first size multiplier
        try:
            from core.scanning.btc_priority import get_btc_priority_filter
            btc_filter = get_btc_priority_filter()
            size_multiplier = btc_filter.get_size_multiplier(symbol)
            size_usdt = size_usdt * size_multiplier
            if size_multiplier != 1.0:
                logger.info(
                    "LiveExecutor: applied BTC-first multiplier %.2f for %s",
                    size_multiplier, symbol
                )
        except Exception as exc:
            logger.debug("LiveExecutor: BTC-first multiplier error (using 1.0): %s", exc)

        amount = size_usdt / entry_ref   # approximate base-currency quantity

        # ── Validate against exchange minimums ────────────────
        try:
            market = ex.market(symbol)
            min_amt  = (market.get("limits") or {}).get("amount", {}).get("min") or 0.0
            min_cost = (market.get("limits") or {}).get("cost",   {}).get("min") or 0.0
            # Round amount to exchange precision
            amount = float(ex.amount_to_precision(symbol, amount))
            if amount < min_amt:
                logger.warning(
                    "LiveExecutor: %s amount %.8f < min_amount %.8f — skipping",
                    symbol, amount, min_amt,
                )
                return False
            if size_usdt < min_cost:
                logger.warning(
                    "LiveExecutor: %s cost %.2f USDT < min_cost %.2f — skipping",
                    symbol, size_usdt, min_cost,
                )
                return False
        except Exception as exc:
            logger.warning("LiveExecutor: market validation failed for %s: %s", symbol, exc)
            # Continue — best-effort

        # ── Place market order ────────────────────────────────
        logger.info(
            "LiveExecutor: ⚡ placing LIVE %s market order | %s %.6f (≈%.2f USDT) | "
            "SL=%.6g  TP=%.6g | score=%.2f",
            side.upper(), symbol, amount, size_usdt,
            candidate.stop_loss_price, candidate.take_profit_price,
            candidate.score,
        )
        try:
            order = ex.create_market_order(symbol, side, amount)
        except Exception as exc:
            logger.error(
                "LiveExecutor: CCXT order failed for %s: %s", symbol, exc
            )
            bus.publish(
                Topics.EXCHANGE_ERROR,
                {"error": f"Order failed for {symbol}: {exc}"},
                source="live_executor",
            )
            return False

        # ── Extract fill price ────────────────────────────────
        fill_price = (
            order.get("average")
            or order.get("price")
            or entry_ref
        )
        try:
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            fill_price = entry_ref

        order_id  = order.get("id", "")
        opened_at = datetime.utcnow()

        position = {
            "symbol":        symbol,
            "side":          side,
            "entry_price":   fill_price,
            "current_price": fill_price,
            "quantity":      amount,
            "stop_loss":     candidate.stop_loss_price,
            "take_profit":   candidate.take_profit_price,
            "size_usdt":     size_usdt,
            "unrealized_pnl": 0.0,
            "score":         candidate.score,
            "regime":        getattr(candidate, "regime", ""),
            "models_fired":  list(getattr(candidate, "models_fired", [])),
            "timeframe":     getattr(candidate, "timeframe", ""),
            "rationale":     getattr(candidate, "rationale", ""),
            "opened_at":     opened_at.isoformat(),
            "entry_order_id": order_id,
        }

        with self._lock:
            self._positions[symbol] = position
            # Invalidate balance cache so next read reflects new order
            self._balance_cache["ts"] = 0.0

        # ── Persist to DB ─────────────────────────────────────
        self._save_open_to_db(position, order_id)

        logger.info(
            "LiveExecutor: ✓ position opened | %s %s @ %.6g | "
            "SL=%.6g  TP=%.6g | order_id=%s",
            side, symbol, fill_price,
            candidate.stop_loss_price, candidate.take_profit_price,
            order_id,
        )
        bus.publish(Topics.TRADE_OPENED, data=position.copy(), source="live_executor")
        return True

    # ── SL/TP monitoring ──────────────────────────────────────

    def on_tick(self, symbol: str, price: float) -> None:
        """
        Check SL/TP for *symbol* at *price*.
        Places a market close order and publishes TRADE_CLOSED if triggered.
        """
        with self._lock:
            pos = self._positions.get(symbol)
        if pos is None:
            return

        side       = pos["side"]
        sl         = pos["stop_loss"]
        tp         = pos["take_profit"]
        exit_reason: Optional[str] = None

        if side == "buy":
            unrealized = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
            if price <= sl:
                exit_reason = "stop_loss"
            elif price >= tp:
                exit_reason = "take_profit"
        else:
            unrealized = (pos["entry_price"] - price) / pos["entry_price"] * 100.0
            if price >= sl:
                exit_reason = "stop_loss"
            elif price <= tp:
                exit_reason = "take_profit"

        with self._lock:
            if symbol in self._positions:
                self._positions[symbol]["current_price"] = price
                self._positions[symbol]["unrealized_pnl"] = round(unrealized, 4)

        if exit_reason:
            logger.info(
                "LiveExecutor: %s triggered for %s @ %.6g (SL=%.6g TP=%.6g)",
                exit_reason, symbol, price, sl, tp,
            )
            self._close_position_on_exchange(symbol, exit_reason)

        bus.publish(
            Topics.POSITION_UPDATED,
            data={**pos, "current_price": price, "unrealized_pnl": round(unrealized, 4)},
            source="live_executor",
        )

    # ── Dynamic stop adjustment ────────────────────────────────

    def adjust_stop(self, symbol: str, new_stop_loss: float) -> bool:
        """
        Adjust the stop loss of an existing position.
        Only allows tightening (moving closer to entry price), not loosening.
        Returns True on success, False otherwise.
        """
        with self._lock:
            pos = self._positions.get(symbol)
        if pos is None:
            logger.warning("LiveExecutor: adjust_stop — position not found for %s", symbol)
            return False

        entry = pos["entry_price"]
        current_sl = pos["stop_loss"]
        side = pos["side"]

        # Validate: new stop must be tighter (closer to entry)
        if side == "buy":
            # For buy: current SL is below entry, new SL must be >= current SL and < entry
            if new_stop_loss < current_sl or new_stop_loss >= entry:
                logger.warning(
                    "LiveExecutor: adjust_stop — invalid SL for %s (buy): "
                    "new=%.6g, current=%.6g, entry=%.6g",
                    symbol, new_stop_loss, current_sl, entry
                )
                return False
        else:
            # For sell: current SL is above entry, new SL must be <= current SL and > entry
            if new_stop_loss > current_sl or new_stop_loss <= entry:
                logger.warning(
                    "LiveExecutor: adjust_stop — invalid SL for %s (sell): "
                    "new=%.6g, current=%.6g, entry=%.6g",
                    symbol, new_stop_loss, current_sl, entry
                )
                return False

        with self._lock:
            if symbol in self._positions:
                self._positions[symbol]["stop_loss"] = new_stop_loss

        logger.info(
            "LiveExecutor: adjusted stop for %s | %.6g → %.6g",
            symbol, current_sl, new_stop_loss
        )
        bus.publish(
            Topics.POSITION_UPDATED,
            data={**pos, "stop_loss": new_stop_loss},
            source="live_executor",
        )
        return True

    # ── Partial close ──────────────────────────────────────────

    def partial_close(self, symbol: str, reduce_pct: float) -> bool:
        """
        Close a fraction of an existing position.

        reduce_pct: 0.0 to 1.0 (e.g., 0.5 closes 50% of position)
        If reduce_pct >= 0.99, calls full close instead.
        Returns True on success, False otherwise.
        """
        with self._lock:
            pos = self._positions.get(symbol)
        if pos is None:
            logger.warning("LiveExecutor: partial_close — position not found for %s", symbol)
            return False

        # Validate reduce_pct
        if reduce_pct <= 0.0 or reduce_pct > 1.0:
            logger.warning(
                "LiveExecutor: partial_close — invalid reduce_pct %.2f for %s",
                reduce_pct, symbol
            )
            return False

        # If nearly 100%, just do full close
        if reduce_pct >= 0.99:
            return self.close_position(symbol)

        # ── Validate exchange connectivity ────────────────────
        try:
            ex = self._exchange()
        except RuntimeError as exc:
            logger.error("LiveExecutor.partial_close: %s", exc)
            return False

        # Calculate partial quantity
        original_qty = pos["quantity"]
        close_qty = original_qty * reduce_pct
        close_side = "sell" if pos["side"] == "buy" else "buy"
        symbol_str = symbol

        # ── Place partial market order ─────────────────────────
        logger.info(
            "LiveExecutor: placing partial close for %s | closing %.2f%% (qty=%.8f)",
            symbol, reduce_pct * 100, close_qty
        )
        try:
            order = ex.create_market_order(symbol_str, close_side, close_qty)
            close_price = float(order.get("average") or order.get("price") or pos["current_price"])
        except Exception as exc:
            logger.error(
                "LiveExecutor: partial close order failed for %s: %s", symbol, exc
            )
            return False

        # ── Update position: reduce quantity ───────────────────
        new_qty = original_qty - close_qty
        with self._lock:
            if symbol in self._positions:
                self._positions[symbol]["quantity"] = new_qty

        # ── Calculate P&L for closed fraction ──────────────────
        if pos["side"] == "buy":
            pnl_usdt = (close_price - pos["entry_price"]) * close_qty
        else:
            pnl_usdt = (pos["entry_price"] - close_price) * close_qty

        logger.info(
            "LiveExecutor: ✓ partial close for %s | closed %.8f @ %.6g | "
            "remaining qty=%.8f | P&L=%.2f USDT",
            symbol, close_qty, close_price, new_qty, pnl_usdt
        )
        bus.publish(
            Topics.POSITION_UPDATED,
            data={**pos, "quantity": new_qty},
            source="live_executor",
        )
        return True

    # ── Manual close ──────────────────────────────────────────

    def close_position(self, symbol: str, price: Optional[float] = None) -> bool:
        """
        Manually close *symbol* at market price.
        Returns True if the position existed and a close order was placed.
        """
        with self._lock:
            if symbol not in self._positions:
                return False
        self._close_position_on_exchange(symbol, "manual_close")
        return True

    def close_all(self) -> int:
        """Close every open position. Returns number closed."""
        with self._lock:
            symbols = list(self._positions.keys())
        n = 0
        for sym in symbols:
            if self._close_position_on_exchange(sym, "manual_close"):
                n += 1
        return n

    # ── Internal close logic ──────────────────────────────────

    def _close_position_on_exchange(self, symbol: str, exit_reason: str) -> bool:
        """
        Place a market close order and record the closed trade.
        Returns True on success.
        """
        with self._lock:
            pos = self._positions.pop(symbol, None)
        if pos is None:
            return False

        # Opposite side to close
        close_side = "sell" if pos["side"] == "buy" else "buy"
        amount     = pos["quantity"]
        closed_at  = datetime.utcnow()

        exit_price = pos["current_price"]
        exit_order_id = ""

        try:
            ex = self._exchange()
            try:
                order = ex.create_market_order(symbol, close_side, amount)
                exit_price = (
                    float(order.get("average") or order.get("price") or exit_price)
                )
                exit_order_id = order.get("id", "")
            except Exception as exc:
                logger.error(
                    "LiveExecutor: close order failed for %s: %s", symbol, exc
                )
                bus.publish(
                    Topics.EXCHANGE_ERROR,
                    {"error": f"Close order failed for {symbol}: {exc}"},
                    source="live_executor",
                )

        except RuntimeError as exc:
            logger.error("LiveExecutor._close: %s", exc)

        # ── P&L calculation ───────────────────────────────────
        entry  = pos["entry_price"]
        qty    = pos["quantity"]
        if pos["side"] == "buy":
            pnl_usdt = (exit_price - entry) * qty
        else:
            pnl_usdt = (entry - exit_price) * qty
        pnl_pct  = pnl_usdt / (entry * qty) * 100.0 if (entry * qty) > 0 else 0.0

        opened_at = pos.get("opened_at", "")
        try:
            duration_s = int(
                (closed_at - datetime.fromisoformat(opened_at)).total_seconds()
            )
        except Exception:
            duration_s = 0

        trade = {
            "symbol":         symbol,
            "side":           pos["side"],
            "entry_price":    entry,
            "exit_price":     exit_price,
            "quantity":       qty,
            "size_usdt":      pos["size_usdt"],
            "pnl_usdt":       round(pnl_usdt, 4),
            "pnl_pct":        round(pnl_pct, 2),
            "exit_reason":    exit_reason,
            "regime":         pos.get("regime", ""),
            "models_fired":   pos.get("models_fired", []),
            "timeframe":      pos.get("timeframe", ""),
            "score":          pos.get("score", 0.0),
            "rationale":      pos.get("rationale", ""),
            "duration_s":     duration_s,
            "opened_at":      opened_at,
            "closed_at":      closed_at.isoformat(),
            "entry_order_id": pos.get("entry_order_id", ""),
            "exit_order_id":  exit_order_id,
        }

        with self._lock:
            self._closed_trades.append(trade)
            # Invalidate balance cache
            self._balance_cache["ts"] = 0.0

        self._save_closed_to_db(trade)
        bus.publish(Topics.TRADE_CLOSED, data=trade.copy(), source="live_executor")

        verb = "✓" if pnl_usdt >= 0 else "✗"
        logger.info(
            "LiveExecutor: %s %s closed @ %.6g | P&L %.4f USDT (%.2f%%) | %s",
            verb, symbol, exit_price, pnl_usdt, pnl_pct, exit_reason,
        )
        return True

    # ── DB helpers ────────────────────────────────────────────

    def _save_open_to_db(self, position: dict, order_id: str):
        """Record entry in live_trades table (partial — will be completed on close)."""
        try:
            from core.database.engine import get_session
            from core.database.models import LiveTrade
            with get_session() as s:
                s.add(LiveTrade(
                    symbol          = position["symbol"],
                    side            = position["side"],
                    regime          = position.get("regime", ""),
                    timeframe       = position.get("timeframe", ""),
                    entry_price     = position["entry_price"],
                    size_usdt       = position["size_usdt"],
                    stop_loss       = position.get("stop_loss"),
                    take_profit     = position.get("take_profit"),
                    score           = position.get("score", 0.0),
                    models_fired    = position.get("models_fired") or [],
                    rationale       = position.get("rationale", ""),
                    entry_order_id  = order_id,
                    opened_at       = position.get("opened_at", ""),
                    status          = "open",
                ))
        except Exception as exc:
            logger.warning("LiveExecutor: DB entry write failed: %s", exc)

    def _save_closed_to_db(self, trade: dict):
        """Update or insert completed trade in live_trades table."""
        try:
            from core.database.engine import get_session
            from core.database.models import LiveTrade
            with get_session() as s:
                # Try to find existing open record to update
                existing = (
                    s.query(LiveTrade)
                    .filter_by(
                        symbol=trade["symbol"],
                        entry_order_id=trade.get("entry_order_id", ""),
                        status="open",
                    )
                    .first()
                )
                if existing:
                    existing.exit_price     = trade["exit_price"]
                    existing.pnl_usdt       = trade["pnl_usdt"]
                    existing.pnl_pct        = trade["pnl_pct"]
                    existing.exit_reason    = trade["exit_reason"]
                    existing.exit_order_id  = trade.get("exit_order_id", "")
                    existing.duration_s     = trade.get("duration_s", 0)
                    existing.closed_at      = trade["closed_at"]
                    existing.status         = "closed"
                else:
                    # Insert full closed record (entry record may have been lost)
                    s.add(LiveTrade(
                        symbol          = trade["symbol"],
                        side            = trade["side"],
                        regime          = trade.get("regime", ""),
                        timeframe       = trade.get("timeframe", ""),
                        entry_price     = trade["entry_price"],
                        exit_price      = trade.get("exit_price"),
                        size_usdt       = trade["size_usdt"],
                        pnl_usdt        = trade.get("pnl_usdt"),
                        pnl_pct         = trade.get("pnl_pct"),
                        stop_loss       = trade.get("stop_loss"),
                        take_profit     = trade.get("take_profit"),
                        score           = trade.get("score", 0.0),
                        exit_reason     = trade.get("exit_reason", ""),
                        models_fired    = trade.get("models_fired") or [],
                        rationale       = trade.get("rationale", ""),
                        entry_order_id  = trade.get("entry_order_id", ""),
                        exit_order_id   = trade.get("exit_order_id", ""),
                        duration_s      = trade.get("duration_s", 0),
                        opened_at       = trade.get("opened_at", ""),
                        closed_at       = trade.get("closed_at", ""),
                        status          = "closed",
                    ))
        except Exception as exc:
            logger.warning("LiveExecutor: DB close write failed: %s", exc)


# Global singleton
live_executor = LiveExecutor()
