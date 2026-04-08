# ============================================================
# NEXUS TRADER — Scalping Agent
#
# High-frequency micro-structure based signals using order book
# imbalance, trade flow, and micro breakouts on 1-minute timeframe.
# Generates very short-term (15-minute) scalping opportunities.
# ============================================================
from __future__ import annotations

import logging
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)


class ScalpingAgent(BaseAgent):
    """
    Generates short-term scalping signals (15-minute timeframe) using
    order book microstructure, trade flow analysis, and price action.

    Combines:
    - Order book imbalance (bid/ask ratio)
    - Taker buy ratio from recent trades
    - Micro breakouts on 1-minute bars
    - VWAP deviation analysis

    All factors must align for high-confidence signal.
    """

    def __init__(self, name: str = "scalp", parent=None):
        super().__init__(name, parent)
        # Cache current price by symbol for VWAP calculation
        self._current_prices: dict[str, float] = {}
        # Reuse HTTP session for connection pooling (M-22)
        self._session = requests.Session()

    # ── Abstract interface implementation ──────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.SCALP_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return 60

    def fetch(self) -> dict:
        """
        Fetch market microstructure data from Binance for all major symbols.
        Returns dict with order book, trades, and klines data.
        """
        # Focus on major symbols for scalping
        symbols = [
            "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
            "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
            "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
            "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
        ]

        data = {}

        for symbol in symbols:
            try:
                binance_symbol = symbol.replace("/", "")

                # Fetch order book (20 levels)
                ob = self._fetch_order_book(binance_symbol)

                # Fetch recent trades (50 trades)
                trades = self._fetch_recent_trades(binance_symbol)

                # Fetch 1-minute klines (30 bars)
                klines = self._fetch_klines(binance_symbol, "1m", 30)

                # Fetch current price
                ticker = self._fetch_ticker(binance_symbol)

                data[symbol] = {
                    "orderbook": ob,
                    "trades": trades,
                    "klines": klines,
                    "current_price": ticker,
                }

                self._current_prices[symbol] = ticker

            except Exception as exc:
                logger.debug("ScalpingAgent: fetch failed for %s: %s", symbol, exc)
                data[symbol] = {
                    "orderbook": None,
                    "trades": None,
                    "klines": None,
                    "current_price": self._current_prices.get(symbol, 0),
                }

        return {"symbols": symbols, "data": data}

    def process(self, raw: dict) -> dict:
        """
        Process market data and generate scalping signals.
        Returns best opportunity signal across all symbols.
        """
        symbols = raw.get("symbols", [])
        data = raw.get("data", {})

        best_signal = None
        best_confidence = 0.0

        for symbol in symbols:
            symbol_data = data.get(symbol, {})

            if not all(symbol_data.values()):
                continue

            signal = self._analyze_symbol(symbol, symbol_data)

            if signal and signal.get("confidence", 0) > best_confidence:
                best_signal = signal
                best_confidence = signal.get("confidence", 0)

        if not best_signal:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "symbol": None,
                "obi": 0.0,
                "taker_buy_ratio": 0.5,
                "vwap_deviation_pct": 0.0,
                "micro_breakout_direction": 0,
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "valid_until": None,
            }

        return best_signal

    # ── Symbol analysis ───────────────────────────────────────────

    def _analyze_symbol(self, symbol: str, symbol_data: dict) -> Optional[dict]:
        """Analyze single symbol and return scalping signal if warranted."""
        ob = symbol_data.get("orderbook")
        trades = symbol_data.get("trades")
        klines = symbol_data.get("klines")
        current_price = symbol_data.get("current_price", 0)

        if not all([ob, trades, klines, current_price > 0]):
            return None

        # Calculate signal components
        obi = self._calculate_order_book_imbalance(ob)
        taker_buy_ratio = self._calculate_taker_buy_ratio(trades)
        vwap_dev = self._calculate_vwap_deviation(klines, current_price)
        breakout_dir = self._detect_micro_breakout(klines)

        # Consensus required: all factors must align
        bullish_votes = 0
        bearish_votes = 0

        # OBI: order book imbalance
        if obi > 0.3:
            bullish_votes += 1
        elif obi < -0.3:
            bearish_votes += 1

        # Taker buy ratio
        if taker_buy_ratio > 0.6:
            bullish_votes += 1
        elif taker_buy_ratio < 0.4:
            bearish_votes += 1

        # Micro breakout
        if breakout_dir > 0:
            bullish_votes += 1
        elif breakout_dir < 0:
            bearish_votes += 1

        # VWAP deviation (small effect)
        if vwap_dev > 0.1:
            bullish_votes += 0.5
        elif vwap_dev < -0.1:
            bearish_votes += 0.5

        # Determine signal
        signal_value = 0.0
        direction = 0

        if bullish_votes >= 3.0:
            signal_value = bullish_votes / 4.0
            direction = 1
        elif bearish_votes >= 3.0:
            signal_value = bearish_votes / 4.0
            direction = -1
        else:
            return None  # No consensus

        # Calculate entry, stop, take profit
        entry_price = current_price
        if direction > 0:
            stop_loss = entry_price * 0.9985  # -0.15%
            take_profit = entry_price * 1.0030  # +0.30%
        else:
            stop_loss = entry_price * 1.0015  # +0.15%
            take_profit = entry_price * 0.9970  # -0.30%

        valid_until = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

        return {
            "signal": signal_value if direction > 0 else -signal_value,
            "confidence": min(0.95, signal_value * 0.8),
            "has_data": True,
            "symbol": symbol,
            "direction": direction,
            "obi": obi,
            "taker_buy_ratio": taker_buy_ratio,
            "vwap_deviation_pct": vwap_dev,
            "micro_breakout_direction": breakout_dir,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timeframe": "1m",
            "valid_seconds": 120,
            "valid_until": valid_until,
        }

    # ── Signal component calculations ─────────────────────────────

    def _calculate_order_book_imbalance(self, orderbook: dict) -> float:
        """
        Calculate order book imbalance.
        OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        Range: -1.0 to 1.0
        """
        try:
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])

            bid_volume = sum(float(b[1]) for b in bids)
            ask_volume = sum(float(a[1]) for a in asks)

            if bid_volume + ask_volume == 0:
                return 0.0

            obi = (bid_volume - ask_volume) / (bid_volume + ask_volume)
            return float(obi)

        except Exception as exc:
            logger.debug("ScalpingAgent: OBI calculation failed: %s", exc)
            return 0.0

    def _calculate_taker_buy_ratio(self, trades: list) -> float:
        """
        Calculate ratio of buyer-initiated trades.
        taker_buy_ratio = buy_trades / total_trades
        Range: 0.0 to 1.0
        """
        try:
            if not trades:
                return 0.5

            buy_trades = sum(1 for t in trades if t.get("m") is False)  # not maker = taker buy
            total = len(trades)

            if total == 0:
                return 0.5

            return float(buy_trades) / float(total)

        except Exception as exc:
            logger.debug("ScalpingAgent: taker ratio calculation failed: %s", exc)
            return 0.5

    def _calculate_vwap_deviation(self, klines: list, current_price: float) -> float:
        """
        Calculate deviation of current price from VWAP.
        Returns: (price - vwap) / vwap * 100
        Positive: overbot, Negative: oversold
        """
        try:
            if not klines or current_price <= 0:
                return 0.0

            # Calculate VWAP from klines
            # kline: [time, open, high, low, close, volume, ...]
            total_volume = 0.0
            total_tp_volume = 0.0

            for kline in klines:
                close = float(kline[4])
                volume = float(kline[7])  # Quote asset volume
                tp = close  # Typical price simplified to close
                total_tp_volume += tp * volume
                total_volume += volume

            if total_volume == 0:
                return 0.0

            vwap = total_tp_volume / total_volume
            deviation = (current_price - vwap) / vwap * 100.0

            return float(deviation)

        except Exception as exc:
            logger.debug("ScalpingAgent: VWAP calculation failed: %s", exc)
            return 0.0

    def _detect_micro_breakout(self, klines: list) -> int:
        """
        Detect micro breakouts on 1-minute bars.
        Returns: 1 for breakout above 30-bar high, -1 for below low, 0 for none
        """
        try:
            if len(klines) < 2:
                return 0

            # Get last close
            current_close = float(klines[-1][4])

            # Calculate 30-bar high and low (exclude current bar)
            prev_bars = klines[:-1]
            highs = [float(k[2]) for k in prev_bars]
            lows = [float(k[3]) for k in prev_bars]

            bar_30_high = max(highs)
            bar_30_low = min(lows)

            # Check for breakout
            if current_close > bar_30_high:
                return 1
            elif current_close < bar_30_low:
                return -1
            else:
                return 0

        except Exception as exc:
            logger.debug("ScalpingAgent: micro breakout detection failed: %s", exc)
            return 0

    # ── Data fetching from Binance ────────────────────────────────

    def _fetch_order_book(self, symbol: str) -> Optional[dict]:
        """Fetch order book from Binance (20 levels)."""
        try:
            url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20"
            resp = self._session.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("ScalpingAgent: order book fetch failed for %s: %s", symbol, exc)
            return None

    def _fetch_recent_trades(self, symbol: str) -> Optional[list]:
        """Fetch recent trades from Binance (50 trades)."""
        try:
            url = f"https://api.binance.com/api/v3/trades?symbol={symbol}&limit=50"
            resp = self._session.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("ScalpingAgent: trades fetch failed for %s: %s", symbol, exc)
            return None

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> Optional[list]:
        """Fetch candlesticks from Binance."""
        try:
            url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval={interval}&limit={limit}"
            )
            resp = self._session.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("ScalpingAgent: klines fetch failed for %s: %s", symbol, exc)
            return None

    def _fetch_ticker(self, symbol: str) -> float:
        """Fetch current price from Binance ticker."""
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            resp = self._session.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0))
        except Exception as exc:
            logger.debug("ScalpingAgent: ticker fetch failed for %s: %s", symbol, exc)
            return 0.0


# ── Module singleton ──────────────────────────────────────────
scalp_agent: Optional[ScalpingAgent] = None
