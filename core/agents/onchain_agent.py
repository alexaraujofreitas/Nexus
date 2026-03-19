# ============================================================
# NEXUS TRADER — On-Chain Exchange Flow Agent
#
# Monitors Bitcoin/ETH on-chain exchange inflows/outflows using
# multiple data sources:
#  • CoinGecko (price, volume)
#  • mempool.space (BTC fees, mempool stats)
#  • blockchain.info (BTC network stats)
#  • CoinGecko 7d price change (on-chain sentiment proxy)
#
# Signal logic:
#   Large positive price + high volume      → exchange inflow (bearish, -0.4 to -0.8)
#   Price declining + volume spike          → capitulation (bullish, +0.3 to +0.6)
#   Price stable + volume decreasing        → accumulation (mildly bullish, +0.2 to +0.4)
#   Steady rise + below-avg volume          → weak momentum (+0.1)
#   High mempool congestion (>100MB)        → mild bearish (-0.2)
#   High tx volume on blockchain.info       → bullish (+0.3)
#   Low fee revenue + low tx count          → accumulation phase (+0.2)
#
# Graceful degradation: if sources fail, emit signal based on available data
#
# Publishes: Topics.ONCHAIN_UPDATED
# ============================================================
from __future__ import annotations

import json
import logging
import urllib.request
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_base_symbols, get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 3600  # 1 hour


class OnChainAgent(BaseAgent):
    """
    Monitors on-chain exchange flows for BTC and ETH using multiple sources.

    Tracks price changes, volume patterns, mempool stats, and network activity
    to infer exchange flow sentiment and on-chain network health.
    """

    def __init__(self, parent=None):
        super().__init__("onchain", parent)
        self._symbols = ["bitcoin", "ethereum"]
        self._cache: dict[str, dict] = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.ONCHAIN_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Update symbols from config if changed."""
        try:
            from config.settings import settings
            new_symbols = settings.get("agents.onchain.symbols", ["bitcoin", "ethereum"])
            if isinstance(new_symbols, list):
                self._symbols = new_symbols
        except Exception as exc:
            logger.debug("OnChainAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, dict]:
        """
        Fetch price, volume, mempool, and network data from multiple sources.
        Merges CoinGecko, mempool.space, and blockchain.info data.
        """
        results: dict[str, dict] = {}
        symbols = get_base_symbols()  # Use watchlist-gated symbols

        # Fetch mempool and blockchain stats (BTC-specific, called once)
        mempool_data = self._fetch_mempool_data()
        blockchain_data = self._fetch_blockchain_stats()

        for symbol in symbols:
            try:
                data = self._fetch_coingecko(symbol)
                if data:
                    # Merge mempool/blockchain data for BTC
                    if symbol.lower() == "bitcoin":
                        if mempool_data:
                            data["mempool"] = mempool_data
                        if blockchain_data:
                            data["blockchain"] = blockchain_data
                    results[symbol] = data
                    get_api_call_counter().record("coingecko")
            except Exception as exc:
                logger.debug("OnChainAgent: fetch %s failed — %s", symbol, exc)

        return results

    def process(self, raw: dict[str, dict]) -> dict:
        """
        Compute exchange flow signals based on price, volume, mempool, and network patterns.
        Returns aggregate signal across symbols.

        Weighting:
        - CoinGecko signals: 40%
        - Mempool data: 30%
        - Blockchain.info stats: 30%
        """
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "symbols": {}, "count": 0}

        symbols_result = {}
        for symbol, data in raw.items():
            # Skip non-dict entries (e.g. metadata scalars stored in the raw dict)
            if not isinstance(data, dict):
                continue
            signal, confidence, direction, metadata = self._compute_signal(data)
            symbols_result[symbol] = {
                **data,
                "signal": signal,
                "confidence": confidence,
                "direction": direction,
                "metadata": metadata,
            }
            self._cache[symbol] = symbols_result[symbol]

        # Aggregate
        signals = [v["signal"] for v in symbols_result.values()]
        confs = [v["confidence"] for v in symbols_result.values()]
        avg_signal = sum(signals) / len(signals) if signals else 0.0
        avg_conf = sum(confs) / len(confs) if confs else 0.0

        logger.info(
            "OnChainAgent: %d symbols | avg_signal=%.3f | avg_conf=%.2f",
            len(raw), avg_signal, avg_conf,
        )

        return {
            "signal": round(avg_signal, 4),
            "confidence": round(avg_conf, 4),
            "symbols": symbols_result,
            "count": len(symbols_result),
        }

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(data: dict) -> tuple[float, float, str, dict]:
        """
        Infer on-chain sentiment from price change, volume patterns,
        mempool stats, and network activity.

        Returns (signal, confidence, direction, metadata).
        """
        price_change_24h = data.get("price_change_pct_24h", 0.0)
        price_change_7d = data.get("price_change_pct_7d", 0.0)
        volume_24h = data.get("volume_24h", 0.0)
        avg_volume_30d = data.get("avg_volume_30d", 0.0)

        metadata = {
            "price_change_24h": price_change_24h,
            "price_change_7d": price_change_7d,
            "volume_24h": volume_24h,
            "avg_volume_30d": avg_volume_30d,
        }

        direction = "neutral"

        # Avoid division by zero
        if avg_volume_30d == 0:
            avg_volume_30d = 1.0

        volume_ratio = volume_24h / avg_volume_30d if avg_volume_30d else 1.0

        # ── CoinGecko-based signals (40% weight in final computation) ──
        coingecko_signal = 0.0
        coingecko_conf = 0.0

        # Large positive price + high volume → exchange inflow (bearish)
        if price_change_24h > 5.0 and volume_ratio > 1.3:
            coingecko_signal = -0.65
            coingecko_conf = 0.75
            direction = "bearish"
            metadata["reason"] = "Large price rise + high volume (exchange inflow signal)"

        # Price declining + volume spike → capitulation (bullish)
        elif price_change_24h < -3.0 and volume_ratio > 1.5:
            coingecko_signal = 0.5
            coingecko_conf = 0.70
            direction = "bullish"
            metadata["reason"] = "Price decline + volume spike (capitulation/flush)"

        # Price stable + volume decreasing → accumulation
        elif -2.0 <= price_change_24h <= 2.0 and volume_ratio < 0.8:
            coingecko_signal = 0.35
            coingecko_conf = 0.55
            direction = "bullish"
            metadata["reason"] = "Stable price + low volume (accumulation phase)"

        # Steady rise + below-average volume → weak momentum
        elif 2.0 < price_change_24h <= 5.0 and volume_ratio < 1.0:
            coingecko_signal = 0.10
            coingecko_conf = 0.40
            direction = "mildly bullish"
            metadata["reason"] = "Steady rise + low volume (weak momentum)"

        else:
            coingecko_signal = 0.0
            coingecko_conf = 0.30
            direction = "neutral"
            metadata["reason"] = "No significant CoinGecko pattern detected"

        # ── Mempool signals (30% weight) ──
        mempool_signal = 0.0
        mempool_conf = 0.0
        if "mempool" in data and data["mempool"]:
            mp = data["mempool"]
            congestion = mp.get("congestion_level", "low")
            if congestion == "high":
                mempool_signal = -0.2
                mempool_conf = 0.60
                metadata["mempool_reason"] = "High mempool congestion (>100MB) — network stressed"
            else:
                mempool_signal = 0.0
                mempool_conf = 0.40

        # ── Blockchain.info signals (30% weight) ──
        blockchain_signal = 0.0
        blockchain_conf = 0.0
        if "blockchain" in data and data["blockchain"]:
            bc = data["blockchain"]
            tx_24h = bc.get("tx_count_24h", 0)
            # Simplified heuristic: if tx_count is significantly above normal, bullish
            if tx_24h > 500000:  # High network activity
                blockchain_signal = 0.3
                blockchain_conf = 0.50
                metadata["blockchain_reason"] = "High tx volume on-chain (>500k/24h) — bullish"
            elif tx_24h < 200000:
                blockchain_signal = 0.2
                blockchain_conf = 0.40
                metadata["blockchain_reason"] = "Low tx volume (accumulation phase)"
            else:
                blockchain_signal = 0.0
                blockchain_conf = 0.30

        # ── Aggregate with weights: CoinGecko 40%, Mempool 30%, Blockchain 30% ──
        if mempool_conf > 0 or blockchain_conf > 0:
            weighted_signal = (
                (coingecko_signal * 0.4) +
                (mempool_signal * 0.3) +
                (blockchain_signal * 0.3)
            )
            weighted_conf = (coingecko_conf + mempool_conf + blockchain_conf) / 3.0
        else:
            # Fallback to CoinGecko only
            weighted_signal = coingecko_signal
            weighted_conf = coingecko_conf

        return round(weighted_signal, 4), round(weighted_conf, 4), direction, metadata

    # ── Data fetching ──────────────────────────────────────────

    @staticmethod
    def _fetch_coingecko(symbol: str) -> dict | None:
        """
        Fetch market data from CoinGecko /coins/{id}/market_chart endpoint.

        symbol: "bitcoin" or "ethereum"
        Returns dict with price_change_24h, price_change_7d, volume_24h, avg_volume_30d.
        """
        url = (
            f"https://api.coingecko.com/api/v3/coins/{symbol}/market_chart"
            "?vs_currency=usd&days=30&interval=daily"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            prices = data.get("prices", [])
            volumes = data.get("total_volumes", [])

            if not prices or not volumes:
                return None

            # Most recent price and prices 24h and 7d ago
            current_price = prices[-1][1] if prices else 0
            price_24h_ago = prices[-2][1] if len(prices) > 1 else current_price
            price_7d_ago = prices[-8][1] if len(prices) > 7 else current_price

            price_change_pct_24h = (
                ((current_price - price_24h_ago) / price_24h_ago * 100.0)
                if price_24h_ago
                else 0.0
            )
            price_change_pct_7d = (
                ((current_price - price_7d_ago) / price_7d_ago * 100.0)
                if price_7d_ago
                else 0.0
            )

            # Volume 24h and 30d average
            volume_24h = volumes[-1][1] if volumes else 0
            avg_volume_30d = sum(v[1] for v in volumes) / len(volumes) if volumes else 0

            return {
                "symbol": symbol,
                "current_price": current_price,
                "price_change_pct_24h": round(price_change_pct_24h, 2),
                "price_change_pct_7d": round(price_change_pct_7d, 2),
                "volume_24h": volume_24h,
                "avg_volume_30d": avg_volume_30d,
            }

        except (URLError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.debug("OnChainAgent: CoinGecko fetch failed for %s — %s", symbol, exc)
            return None

    @staticmethod
    def _fetch_mempool_data() -> dict | None:
        """
        Fetch mempool data from mempool.space API.

        Returns dict with:
        - fee_fastestFee, fee_halfHourFee (current fee rates in sat/vB)
        - congestion_level (low/medium/high based on mempool size)
        """
        try:
            # Fetch recommended fees
            with urllib.request.urlopen(
                "https://mempool.space/api/v1/fees/recommended", timeout=10
            ) as response:
                text = response.read().decode("utf-8")
                fees = json.loads(text)

            # Fetch mempool stats
            with urllib.request.urlopen(
                "https://mempool.space/api/mempool", timeout=10
            ) as response:
                text = response.read().decode("utf-8")
                mempool = json.loads(text)

            # Determine congestion level
            # mempool.vsize is in vbytes; 50MB baseline = 50_000_000 vbytes
            mempool_size = mempool.get("vsize", 0)
            if mempool_size > 100_000_000:  # >100MB
                congestion_level = "high"
            elif mempool_size > 50_000_000:  # >50MB
                congestion_level = "medium"
            else:
                congestion_level = "low"

            return {
                "fee_fastestFee": fees.get("fastestFee"),
                "fee_halfHourFee": fees.get("halfHourFee"),
                "mempool_size_vbytes": mempool_size,
                "tx_count": mempool.get("count", 0),
                "congestion_level": congestion_level,
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("OnChainAgent: mempool.space fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_blockchain_stats() -> dict | None:
        """
        Fetch BTC network stats from blockchain.info.

        Returns dict with:
        - tx_count_24h: number of transactions in last 24h
        - volume_btc_24h: estimated transaction volume in BTC
        - fees_btc_24h: total fees collected in BTC
        """
        try:
            with urllib.request.urlopen(
                "https://api.blockchain.info/stats", timeout=10
            ) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            return {
                "tx_count_24h": data.get("n_tx", 0),
                "volume_btc_24h": data.get("estimated_transaction_volume", 0),
                "fees_btc_24h": data.get("total_fees_btc", 0),
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("OnChainAgent: blockchain.info fetch failed — %s", exc)
            return None

    def get_symbol_signal(self, symbol: str) -> dict:
        """Return latest cached signal for a symbol."""
        if symbol in self._cache:
            return dict(self._cache[symbol])
        return {"signal": 0.0, "confidence": 0.0, "stale": True}


# ── Module-level singleton (initialised by AgentCoordinator) ──
onchain_agent: OnChainAgent | None = None
