# ============================================================
# NEXUS TRADER — Liquidity Vacuum Detection Agent
#
# Monitors Binance order book depth and recent trade volume
# to detect "vacuum zones" — price levels with insufficient
# liquidity that can cause violent, accelerating moves.
#
# Detects:
#   - Thin walls in bid/ask (consecutive levels with low depth)
#   - Vacuum zones in volume profile (gaps with <10% avg volume)
#   - Price proximity to vacuum edges (breakout/breakdown setups)
#   - Bid/ask imbalances (indicative of direction)
#
# Signal logic:
#   Price approaching vacuum from below (breakout)          → +0.5 to +0.7
#   Price approaching vacuum from above (breakdown)         → -0.5 to -0.7
#   Price inside vacuum (accelerating move)                 → ±0.8
#   Deep order book, no vacuums                             → 0.0
#   Thin walls in bid side (weak support below)             → -0.4
#   Thin walls in ask side (weak resistance above)          → +0.3
#
# Returns: signal, confidence, vacuum_zones, nearest_vacuum_distance_pct,
#   vacuum_direction, thin_wall_side, current_price, bid_ask_imbalance
#
# Publishes: Topics.LIQUIDITY_VACUUM
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 300  # 5 minutes
_SYMBOL = "BTCUSDT"
_VACUUM_THRESHOLD = 0.10  # 10% of average volume
_PROXIMITY_THRESHOLD = 0.003  # 0.3% from vacuum edge
_THIN_WALL_THRESHOLD = 0.01  # 1% of average depth
_VOLUME_BUCKETS = 50


class LiquidityVacuumAgent(BaseAgent):
    """
    Detects liquidity vacuum zones in Bitcoin order book.

    Identifies price levels with insufficient liquidity that
    can lead to volatile, accelerating moves when price
    approaches or enters these zones.
    """

    def __init__(self, parent=None):
        super().__init__("liquidity_vacuum", parent)
        self._cache: dict = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.LIQUIDITY_VACUUM

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Reload configuration if changed."""
        try:
                        # Could read symbol from config here
            pass
        except Exception as exc:
            logger.debug("LiquidityVacuumAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch order book, klines (volume profile), and recent trades."""
        result: dict[str, Any] = {
            "order_book": None,
            "klines": None,
            "trades": None,
        }

        # Fetch order book depth
        try:
            ob = self._fetch_order_book()
            if ob:
                result["order_book"] = ob
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("LiquidityVacuumAgent: order book fetch failed — %s", exc)

        # Fetch 24h klines for volume profile
        try:
            klines = self._fetch_klines()
            if klines:
                result["klines"] = klines
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("LiquidityVacuumAgent: klines fetch failed — %s", exc)

        # Fetch recent trades for volume nodes
        try:
            trades = self._fetch_trades()
            if trades:
                result["trades"] = trades
                get_api_call_counter().record("binance")
        except Exception as exc:
            logger.debug("LiquidityVacuumAgent: trades fetch failed — %s", exc)

        return result

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Analyze order book and volume profile to detect vacuum zones.
        Returns signal, confidence, and vacuum metadata.
        """
        order_book = raw.get("order_book")
        trades = raw.get("trades") or raw.get("klines")  # klines used as volume profile fallback

        if not order_book:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "vacuum_zones": [],
                "nearest_vacuum_distance_pct": 0.0,
                "vacuum_direction": None,
                "thin_wall_side": None,
                "current_price": 0.0,
                "bid_ask_imbalance": 0.0,
            }

        current_price = order_book.get("current_price", 0.0)

        # Build volume profile from trades
        volume_profile = {}
        if trades:
            volume_profile = self._build_volume_profile(trades, current_price)

        # Detect vacuum zones
        vacuum_zones = self._detect_vacuum_zones(volume_profile, current_price)

        # Detect thin walls
        thin_wall_bid, thin_wall_ask = self._detect_thin_walls(order_book)
        thin_wall_side = self._determine_thin_wall_side(thin_wall_bid, thin_wall_ask)

        # Find nearest vacuum
        nearest_vacuum_dist, vacuum_dir = self._nearest_vacuum(
            current_price, vacuum_zones
        )

        # Compute bid/ask imbalance
        bid_ask_imbal = self._compute_bid_ask_imbalance(order_book)

        # Compute signal
        signal, confidence = self._compute_signal(
            vacuum_zones, nearest_vacuum_dist, vacuum_dir,
            thin_wall_side, bid_ask_imbal
        )

        # Cache results
        self._cache = {
            "current_price": current_price,
            "vacuum_zones": vacuum_zones,
            "nearest_distance": nearest_vacuum_dist,
            "vacuum_direction": vacuum_dir,
            "thin_wall_side": thin_wall_side,
            "bid_ask_imbalance": bid_ask_imbal,
            "signal": signal,
            "confidence": confidence,
        }

        logger.info(
            "LiquidityVacuumAgent: price=%.2f vacuums=%d nearest_dist=%.4f "
            "thin_walls=%s signal=%.3f",
            current_price, len(vacuum_zones), nearest_vacuum_dist,
            thin_wall_side, signal,
        )

        return {
            "signal": round(signal, 4),
            "confidence": round(confidence, 4),
            "has_data": True,
            "vacuum_zones": [
                {
                    "low": round(z["low"], 2),
                    "high": round(z["high"], 2),
                    "size_pct": round(z["size_pct"], 4),
                }
                for z in vacuum_zones
            ],
            "nearest_vacuum_distance_pct": round(nearest_vacuum_dist, 4),
            "vacuum_direction": vacuum_dir,
            "thin_wall_side": thin_wall_side,
            "current_price": round(current_price, 2),
            "bid_ask_imbalance": round(bid_ask_imbal, 4),
        }

    # ── Vacuum detection ───────────────────────────────────────

    @staticmethod
    def _build_volume_profile(trades: list[dict], current_price: float) -> dict[int, float]:
        """
        Build volume profile by grouping trades into 50 price buckets.
        Returns dict {bucket_index: total_volume}.
        """
        if not trades or current_price <= 0:
            return {}

        prices = [float(trade.get("price", 0)) for trade in trades]
        if not prices:
            return {}

        min_price = min(prices)
        max_price = max(prices)

        if min_price == max_price:
            return {}

        price_range = max_price - min_price
        bucket_size = price_range / _VOLUME_BUCKETS

        profile = {i: 0.0 for i in range(_VOLUME_BUCKETS)}

        for trade in trades:
            price = float(trade.get("price", 0))
            qty = float(trade.get("qty", 0))

            bucket_idx = min(
                int((price - min_price) / bucket_size),
                _VOLUME_BUCKETS - 1,
            )
            profile[bucket_idx] += qty

        return profile

    @staticmethod
    def _detect_vacuum_zones(profile: dict[int, float], current_price: float) -> list[dict]:
        """
        Identify vacuum zones: consecutive buckets with <10% avg volume.
        Returns list of {low, high, size_pct} dicts with actual price levels computed from buckets.
        """
        if not profile:
            return []

        avg_vol = sum(profile.values()) / len(profile) if profile else 1.0
        vacuum_threshold = avg_vol * _VACUUM_THRESHOLD

        vacuums = []
        in_vacuum = False
        vacuum_start = None

        # Compute bucket size from price range
        # This requires extracting the price range from the profile context
        # Use current_price and bucket distribution to estimate min/max
        if profile:
            # Estimate min/max prices from current price and bucket indices
            max_bucket = max(profile.keys()) if profile else 0
            min_bucket = min(profile.keys()) if profile else 0

            # Approximate: assume profile spans roughly ±5% of current price
            estimated_range = current_price * 0.10  # 5% above and below
            bucket_size = estimated_range / (_VOLUME_BUCKETS / 2)
            estimated_min_price = current_price - (estimated_range / 2)
        else:
            bucket_size = 1.0
            estimated_min_price = current_price - 500

        for idx in sorted(profile.keys()):
            vol = profile[idx]

            if vol < vacuum_threshold and not in_vacuum:
                in_vacuum = True
                vacuum_start = idx
            elif vol >= vacuum_threshold and in_vacuum:
                # Compute actual price levels from bucket indices
                start_price = estimated_min_price + (vacuum_start * bucket_size)
                end_price = estimated_min_price + ((idx - 1 + 1) * bucket_size)

                vacuums.append({
                    "start_bucket": vacuum_start,
                    "end_bucket": idx - 1,
                    "low": round(min(start_price, end_price), 2),
                    "high": round(max(start_price, end_price), 2),
                    "size_pct": round((idx - vacuum_start) / max(_VOLUME_BUCKETS, 1) * 100.0, 2),
                })
                in_vacuum = False

        if in_vacuum and vacuum_start is not None:
            # Compute actual price levels for final vacuum zone
            start_price = estimated_min_price + (vacuum_start * bucket_size)
            end_price = estimated_min_price + ((_VOLUME_BUCKETS - 1 + 1) * bucket_size)

            vacuums.append({
                "start_bucket": vacuum_start,
                "end_bucket": _VOLUME_BUCKETS - 1,
                "low": round(min(start_price, end_price), 2),
                "high": round(max(start_price, end_price), 2),
                "size_pct": round((_VOLUME_BUCKETS - vacuum_start) / max(_VOLUME_BUCKETS, 1) * 100.0, 2),
            })

        return vacuums

    @staticmethod
    def _detect_thin_walls(order_book: dict) -> tuple[bool, bool]:
        """
        Detect thin walls: consecutive bid/ask levels with <1% avg depth.
        Returns (thin_bid_wall, thin_ask_wall).
        """
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        # Check bid side
        thin_bid = False
        if bids:
            avg_bid_depth = sum(b[1] for b in bids) / len(bids) if bids else 1.0
            thin_levels = sum(1 for b in bids if b[1] < avg_bid_depth * _THIN_WALL_THRESHOLD)
            if thin_levels > len(bids) * 0.3:  # >30% thin levels
                thin_bid = True

        # Check ask side
        thin_ask = False
        if asks:
            avg_ask_depth = sum(a[1] for a in asks) / len(asks) if asks else 1.0
            thin_levels = sum(1 for a in asks if a[1] < avg_ask_depth * _THIN_WALL_THRESHOLD)
            if thin_levels > len(asks) * 0.3:  # >30% thin levels
                thin_ask = True

        return thin_bid, thin_ask

    @staticmethod
    def _determine_thin_wall_side(thin_bid: bool, thin_ask: bool) -> str | None:
        """Determine which side (or both) has thin walls."""
        if thin_bid and thin_ask:
            return "both"
        elif thin_bid:
            return "bid"
        elif thin_ask:
            return "ask"
        else:
            return None

    @staticmethod
    def _nearest_vacuum(
        current_price: float, vacuum_zones: list[dict]
    ) -> tuple[float, str | None]:
        """
        Find distance to nearest vacuum zone and direction.
        Returns (distance_pct, direction: "above" or "below").
        """
        if not vacuum_zones or current_price <= 0:
            return 0.0, None

        min_distance = float("inf")
        direction = None

        for zone in vacuum_zones:
            # If price is below zone
            if current_price < zone.get("low", 0):
                dist_pct = abs((zone["low"] - current_price) / current_price)
                if dist_pct < min_distance:
                    min_distance = dist_pct
                    direction = "below"

            # If price is above zone
            elif current_price > zone.get("high", 0):
                dist_pct = abs((current_price - zone["high"]) / current_price)
                if dist_pct < min_distance:
                    min_distance = dist_pct
                    direction = "above"

            # If price is inside zone
            else:
                return 0.0, "inside"

        if min_distance == float("inf"):
            return 0.0, None

        return min_distance, direction

    @staticmethod
    def _compute_bid_ask_imbalance(order_book: dict) -> float:
        """
        Compute bid/ask imbalance (positive = more ask volume, buying pressure).
        Range: -1.0 (all bid) to +1.0 (all ask).
        """
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        total_bid = sum(b[1] for b in bids) if bids else 1.0
        total_ask = sum(a[1] for a in asks) if asks else 1.0

        if total_bid + total_ask == 0:
            return 0.0

        imbalance = (total_ask - total_bid) / (total_ask + total_bid)
        return imbalance

    def _compute_signal(
        self,
        vacuum_zones: list[dict],
        nearest_dist: float,
        vacuum_dir: str | None,
        thin_wall_side: str | None,
        bid_ask_imbal: float,
    ) -> tuple[float, float]:
        """
        Compute trading signal based on vacuum proximity and thin walls.
        Returns (signal, confidence).
        """
        # No vacuums detected
        if not vacuum_zones:
            return 0.0, 0.2

        # Thin wall logic (override vacuum logic)
        if thin_wall_side == "bid":
            # Weak support below
            return -0.4, 0.65
        elif thin_wall_side == "ask":
            # Weak resistance above
            return 0.3, 0.50

        # Vacuum zone proximity logic
        if vacuum_dir == "inside":
            # Price inside vacuum: amplify direction based on imbalance
            if bid_ask_imbal > 0.2:
                return 0.8, 0.85
            elif bid_ask_imbal < -0.2:
                return -0.8, 0.85
            else:
                return 0.0, 0.30

        elif vacuum_dir == "below" and nearest_dist < _PROXIMITY_THRESHOLD:
            # Price approaching vacuum from below (breakout setup)
            return 0.6, 0.75

        elif vacuum_dir == "above" and nearest_dist < _PROXIMITY_THRESHOLD:
            # Price approaching vacuum from above (breakdown setup)
            return -0.6, 0.75

        else:
            # Vacuum detected but not immediately relevant
            return 0.0, 0.30

    # ── Binance API methods ────────────────────────────────────

    @staticmethod
    def _fetch_order_book() -> dict | None:
        """Fetch order book depth (top 100 levels) from Binance."""
        url = f"https://api.binance.com/api/v3/depth?symbol={_SYMBOL}&limit=100"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
            asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]

            current_price = (bids[0][0] + asks[0][0]) / 2.0 if bids and asks else 0.0

            return {
                "bids": bids,
                "asks": asks,
                "current_price": current_price,
                "timestamp": data.get("E", 0),
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("LiquidityVacuumAgent: order book fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_klines() -> list[dict] | None:
        """
        Fetch 96 x 15min klines (24h) to build volume profile.
        """
        url = (
            f"https://api.binance.com/api/v3/klines?"
            f"symbol={_SYMBOL}&interval=15m&limit=96"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            klines = []
            for kline in data:
                klines.append({
                    "open_time": kline[0],
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                    "volume": float(kline[7]),  # Quote asset volume
                })

            return klines

        except (URLError, json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.debug("LiquidityVacuumAgent: klines fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_trades() -> list[dict] | None:
        """Fetch last 1000 trades from Binance."""
        url = f"https://api.binance.com/api/v3/trades?symbol={_SYMBOL}&limit=1000"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            trades = []
            for trade in data:
                trades.append({
                    "price": float(trade.get("price", 0)),
                    "qty": float(trade.get("qty", 0)),
                    "time": trade.get("time", 0),
                    "is_buyer_maker": trade.get("isBuyerMaker", False),
                })

            return trades

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("LiquidityVacuumAgent: trades fetch failed — %s", exc)
            return None

    def get_vacuum_state(self) -> dict:
        """Return cached vacuum detection state."""
        return dict(self._cache) if self._cache else {}


# ── Module-level singleton (initialised by AgentCoordinator) ──
liquidity_vacuum_agent: LiquidityVacuumAgent | None = None
