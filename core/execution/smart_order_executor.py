# ============================================================
# NEXUS TRADER — Smart Order Executor
#
# Implements maker-first order placement strategy:
#   1. Post limit order inside the spread (maker)
#   2. Wait up to `fill_timeout_seconds` for fill
#   3. If not filled and score >= aggressive_threshold, cross spread (taker)
#   4. If not filled and score < aggressive_threshold, cancel and skip
#
# Fee model (Binance perps defaults):
#   Maker rebate: -0.02% (earn fee)
#   Taker fee:    +0.04%
# ============================================================
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SmartOrderExecutor:
    """
    Implements intelligent maker-first order routing.
    Prefers limit orders (maker) over market orders (taker) to minimize fees.
    """

    def __init__(
        self,
        fill_timeout_seconds: float = 30,
        aggressive_score_threshold: float = 0.75,
        maker_fee: float = -0.0002,
        taker_fee: float = 0.0004,
    ):
        """
        Initialize the smart order executor.

        Args:
            fill_timeout_seconds: Time to wait for limit order fill before escalating
            aggressive_score_threshold: Score threshold to allow taker orders (>= threshold uses taker)
            maker_fee: Fee (as decimal) for maker orders; negative = rebate
            taker_fee: Fee (as decimal) for taker orders
        """
        self.fill_timeout_seconds = fill_timeout_seconds
        self.aggressive_score_threshold = aggressive_score_threshold
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

    def place_entry(self, exchange, candidate, side: str) -> dict:
        """
        Place an entry order using the maker-first strategy.

        Args:
            exchange: CCXT exchange instance
            candidate: OrderCandidate object with symbol, score, entry_price
            side: "buy" or "sell"

        Returns:
            dict with keys:
                - filled: bool (whether order was filled)
                - order_id: str (limit order ID)
                - fill_price: float (actual fill price)
                - fee_type: str ("maker" or "taker")
                - reason: str (if not filled, reason why)
        """
        symbol = candidate.symbol
        amount = candidate.position_size_usdt / candidate.entry_price if candidate.entry_price > 0 else 0.0

        if amount <= 0:
            logger.warning("SmartOrderExecutor: invalid amount for %s", symbol)
            return {"filled": False, "reason": "invalid_amount"}

        # ── Step 1: Compute bid/ask and limit price ────────────────
        try:
            ticker = exchange.fetch_ticker(symbol)
            bid = float(ticker.get("bid", 0.0))
            ask = float(ticker.get("ask", 0.0))

            if bid <= 0 or ask <= 0 or ask <= bid:
                logger.warning("SmartOrderExecutor: invalid spread for %s (bid=%.6g, ask=%.6g)", symbol, bid, ask)
                return {"filled": False, "reason": "invalid_spread"}

            spread = ask - bid
            spread_pct = (spread / bid) * 100.0 if bid > 0 else 0.0

            # Limit price positioned 30% inside the spread
            if side.lower() == "buy":
                # Post closer to bid side (lower limit price to save on entry)
                limit_price = ask - spread * 0.3
            else:
                # Post closer to ask side (higher limit price to save on exit)
                limit_price = bid + spread * 0.3

            logger.info(
                "SmartOrderExecutor: %s maker order for %s | spread=%.2f%% | limit=%.6g | amount=%.8f",
                side.upper(), symbol, spread_pct, limit_price, amount
            )

        except Exception as exc:
            logger.error("SmartOrderExecutor: ticker fetch failed for %s: %s", symbol, exc)
            return {"filled": False, "reason": "ticker_fetch_error"}

        # ── Step 2: Place limit order ──────────────────────────────
        order_id = None
        try:
            order = exchange.create_limit_order(symbol, side.lower(), amount, limit_price)
            order_id = order.get("id")
            logger.debug("SmartOrderExecutor: limit order placed | order_id=%s", order_id)
        except Exception as exc:
            logger.error("SmartOrderExecutor: limit order creation failed for %s: %s", symbol, exc)
            return {"filled": False, "reason": "limit_order_creation_failed"}

        # ── Step 3: Poll for fill (up to fill_timeout_seconds) ─────
        fill_price = None
        start_time = time.time()
        poll_interval = 1.0  # Check every 1 second

        while time.time() - start_time < self.fill_timeout_seconds:
            try:
                order_status = exchange.fetch_order(order_id, symbol)
                if order_status.get("status") == "closed":
                    # Order filled!
                    fill_price = float(order_status.get("average", limit_price))
                    logger.info(
                        "SmartOrderExecutor: ✓ limit order filled @ %.6g | order_id=%s",
                        fill_price, order_id
                    )
                    return {
                        "filled": True,
                        "order_id": order_id,
                        "fill_price": fill_price,
                        "fee_type": "maker",
                    }
            except Exception as exc:
                logger.debug("SmartOrderExecutor: order status check error: %s", exc)

            time.sleep(poll_interval)

        # ── Step 4: Limit order did not fill; decide next action ───
        logger.info(
            "SmartOrderExecutor: limit order not filled within %d seconds for %s",
            self.fill_timeout_seconds, symbol
        )

        # Check if score warrants escalation to taker (market order)
        if candidate.score >= self.aggressive_score_threshold:
            logger.info(
                "SmartOrderExecutor: score=%.2f >= threshold=%.2f; escalating to taker order for %s",
                candidate.score, self.aggressive_score_threshold, symbol
            )

            # Cancel the limit order first
            try:
                exchange.cancel_order(order_id, symbol)
                logger.debug("SmartOrderExecutor: cancelled limit order %s", order_id)
            except Exception as exc:
                logger.warning("SmartOrderExecutor: cancel failed (may be filled): %s", exc)

            # Place market order
            try:
                market_order = exchange.create_market_order(symbol, side.lower(), amount)
                market_fill_price = float(market_order.get("average", candidate.entry_price))
                market_order_id = market_order.get("id")
                logger.info(
                    "SmartOrderExecutor: ✓ market order filled @ %.6g | order_id=%s",
                    market_fill_price, market_order_id
                )
                return {
                    "filled": True,
                    "order_id": market_order_id,
                    "fill_price": market_fill_price,
                    "fee_type": "taker",
                }
            except Exception as exc:
                logger.error("SmartOrderExecutor: market order failed for %s: %s", symbol, exc)
                return {"filled": False, "reason": "market_order_creation_failed"}

        else:
            # Score too low for taker; cancel and skip
            logger.info(
                "SmartOrderExecutor: score=%.2f < threshold=%.2f; cancelling order for %s",
                candidate.score, self.aggressive_score_threshold, symbol
            )
            try:
                exchange.cancel_order(order_id, symbol)
            except Exception as exc:
                logger.debug("SmartOrderExecutor: cancel error: %s", exc)

            return {
                "filled": False,
                "order_id": order_id,
                "reason": "limit_not_filled_score_too_low",
            }

    def place_stop_loss(self, exchange, symbol: str, side: str, stop_price: float, amount: float) -> dict:
        """
        Place a stop-market order for risk management.

        Args:
            exchange: CCXT exchange instance
            symbol: Trading pair symbol
            side: "buy" or "sell" (opposite of entry)
            stop_price: Trigger price for the stop
            amount: Quantity to close

        Returns:
            dict with filled status and order details
        """
        try:
            # Most exchanges use stop_market_order; fallback to create_order with params
            order = exchange.create_order(
                symbol,
                "stop_market",
                side.lower(),
                amount,
                None,
                {"stopPrice": stop_price},
            )
            order_id = order.get("id")
            logger.info(
                "SmartOrderExecutor: stop-loss order placed | %s | stop_price=%.6g | order_id=%s",
                symbol, stop_price, order_id
            )
            return {
                "filled": False,  # Stop orders aren't filled until triggered
                "order_id": order_id,
                "stop_price": stop_price,
                "status": "pending",
            }
        except Exception as exc:
            logger.error("SmartOrderExecutor: stop-loss order failed for %s: %s", symbol, exc)
            return {"filled": False, "reason": "stop_loss_creation_failed"}

    def estimate_round_trip_cost(self, position_size_usdt: float, fee_type: str = "maker") -> float:
        """
        Estimate total fees for a round-trip trade (entry + exit).

        Args:
            position_size_usdt: Position size in USDT
            fee_type: "maker" or "taker" (applies to both entry and exit)

        Returns:
            Estimated cost in USDT
        """
        fee = self.maker_fee if fee_type == "maker" else self.taker_fee
        # Entry cost + exit cost (both same direction, so double the fee impact)
        total_cost_pct = abs(fee) * 2
        return position_size_usdt * total_cost_pct

    def preferred_fee_type(self, score: float) -> str:
        """
        Determine the preferred fee type (execution strategy) based on score.

        Args:
            score: Signal confluence score (0.0 to 1.0)

        Returns:
            "maker" if score < threshold, else "taker_ok"
        """
        return "maker" if score < self.aggressive_score_threshold else "taker_ok"
