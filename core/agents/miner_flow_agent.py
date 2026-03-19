# ============================================================
# NEXUS TRADER — Miner Flow Agent
#
# Monitors Bitcoin miner behavior through on-chain signals:
# fee revenue, hashrate concentration, and price correlation.
# Uses FREE APIs: mempool.space and CoinGecko.
#
# Signal logic:
#   High fee revenue + price rising        → miners holding (bullish, +0.3 to +0.5)
#   Low fee revenue + price falling        → capitulation risk (bearish, -0.4 to -0.6)
#   Hash rate concentration > 60%          → centralization risk (bearish, -0.2)
#   Fee revenue spike (>2x weekly avg)     → block demand surge (bullish, +0.4)
#   Use 7-day fee average for deviation baseline
#
# Publishes: Topics.MINER_FLOW_UPDATED
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

_POLL_SECONDS = 43200  # 12 hours


class MinerFlowAgent(BaseAgent):
    """
    Monitors Bitcoin miner behavior through on-chain metrics.

    Tracks fee revenue, hashrate distribution, and miner selling pressure
    to infer accumulation vs. distribution phases.
    """

    def __init__(self, parent=None):
        super().__init__("miner_flow", parent)
        self._cache: dict[str, Any] = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.MINER_FLOW_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Update config if changed."""
        try:
            from config.settings import settings
            # Optional: allow configuration of poll interval
            poll_override = settings.get("agents.miner_flow.poll_interval_seconds", None)
            if isinstance(poll_override, int):
                pass  # Could override _POLL_SECONDS here if needed
        except Exception as exc:
            logger.debug("MinerFlowAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch miner metrics from mempool.space and BTC price from CoinGecko."""
        results: dict[str, Any] = {}

        try:
            # Fetch 1m pool hashrate distribution
            pool_data = self._fetch_mempool_pools()
            if pool_data:
                results["pool_distribution"] = pool_data
                get_api_call_counter().record("mempool.space")
        except Exception as exc:
            logger.debug("MinerFlowAgent: pool distribution fetch failed — %s", exc)

        try:
            # Fetch 1 week of block fee data
            fee_data = self._fetch_mempool_fees()
            if fee_data:
                results["fee_data"] = fee_data
                get_api_call_counter().record("mempool.space")
        except Exception as exc:
            logger.debug("MinerFlowAgent: fee data fetch failed — %s", exc)

        try:
            # Fetch BTC price and volume for correlation
            price_data = self._fetch_coingecko_bitcoin()
            if price_data:
                results["price_data"] = price_data
                get_api_call_counter().record("coingecko")
        except Exception as exc:
            logger.debug("MinerFlowAgent: Bitcoin price fetch failed — %s", exc)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Compute miner flow signals from fee revenue, hashrate, and price.
        Returns signal, confidence, fee metrics, and miner behavior classification.
        """
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "fee_revenue_btc": 0.0,
                "avg_fee_btc": 0.0,
                "fee_deviation_pct": 0.0,
                "top_pool_concentration": 0.0,
                "miner_behavior": "neutral",
                "metadata": {},
            }

        signal, confidence, fee_data, behavior, metadata = self._compute_signal(raw)

        logger.info(
            "MinerFlowAgent: signal=%.3f | confidence=%.2f | behavior=%s",
            signal, confidence, behavior,
        )

        self._cache = {
            "signal": signal,
            "confidence": confidence,
            "fee_revenue_btc": fee_data.get("fee_revenue_btc", 0.0),
            "avg_fee_btc": fee_data.get("avg_fee_btc", 0.0),
            "fee_deviation_pct": fee_data.get("fee_deviation_pct", 0.0),
            "top_pool_concentration": fee_data.get("top_pool_concentration", 0.0),
            "miner_behavior": behavior,
            "metadata": metadata,
        }

        return self._cache

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(raw: dict[str, Any]) -> tuple[float, float, dict, str, dict]:
        """
        Infer miner behavior from fee revenue, hashrate concentration, and price.

        Returns (signal, confidence, fee_data, miner_behavior, metadata).
        """
        pool_data = raw.get("pool_distribution", {})
        fee_data = raw.get("fee_data", {})
        price_data = raw.get("price_data", {})

        signal = 0.0
        confidence = 0.0
        behavior = "neutral"
        metadata: dict[str, Any] = {}

        # Extract fee metrics
        current_fee_revenue = fee_data.get("current_fee_revenue_btc", 0.0)
        avg_fee_revenue = fee_data.get("avg_fee_revenue_7d_btc", 0.0)
        fee_deviation_pct = fee_data.get("fee_deviation_pct", 0.0)

        # Extract price data
        price_change_24h = price_data.get("price_change_pct_24h", 0.0)
        current_price = price_data.get("current_price", 0.0)

        # Extract pool concentration
        top_pool_concentration = pool_data.get("top_3_concentration_pct", 0.0)

        fee_data_out = {
            "fee_revenue_btc": current_fee_revenue,
            "avg_fee_btc": avg_fee_revenue,
            "fee_deviation_pct": fee_deviation_pct,
            "top_pool_concentration": top_pool_concentration,
        }

        metadata = {
            "current_fee_revenue_btc": current_fee_revenue,
            "avg_fee_revenue_7d_btc": avg_fee_revenue,
            "fee_deviation_pct": fee_deviation_pct,
            "top_pool_concentration": top_pool_concentration,
            "price_change_24h": price_change_24h,
            "current_price": current_price,
        }

        # Signal 1: High fee revenue + price rising → miners holding (bullish)
        if current_fee_revenue > avg_fee_revenue * 1.2 and price_change_24h > 1.0:
            signal = 0.45
            confidence = 0.70
            behavior = "accumulating"
            metadata["reason"] = "High fee revenue + price rising (miners accumulating)"

        # Signal 2: Low fee revenue + price falling → capitulation risk (bearish)
        elif current_fee_revenue < avg_fee_revenue * 0.8 and price_change_24h < -2.0:
            signal = -0.50
            confidence = 0.65
            behavior = "distributing"
            metadata["reason"] = "Low fee revenue + price decline (miner capitulation risk)"

        # Signal 3: Fee revenue spike (>2x weekly avg) → block demand surge (bullish)
        elif current_fee_revenue > avg_fee_revenue * 2.0:
            signal = 0.40
            confidence = 0.75
            behavior = "accumulating"
            metadata["reason"] = "Fee revenue spike >2x (block demand surge)"

        # Signal 4: Hashrate concentration > 60% → centralization risk (slight bearish)
        elif top_pool_concentration > 60.0:
            signal = -0.20
            confidence = 0.60
            behavior = "neutral"
            metadata["reason"] = "Hashrate concentration >60% (centralization risk)"

        # Signal 5: Fee deviation extreme (>1.5x) + price neutral → caution
        elif fee_deviation_pct > 150.0 and -1.0 <= price_change_24h <= 1.0:
            signal = 0.15
            confidence = 0.50
            behavior = "neutral"
            metadata["reason"] = "Extreme fee deviation (block congestion likely)"

        else:
            signal = 0.0
            confidence = 0.30
            behavior = "neutral"
            metadata["reason"] = "No significant miner flow pattern detected"

        return (
            round(signal, 4),
            round(confidence, 4),
            fee_data_out,
            behavior,
            metadata,
        )

    # ── Data Fetching ──────────────────────────────────────────

    @staticmethod
    def _fetch_mempool_pools() -> dict | None:
        """
        Fetch 1m mining pool hashrate distribution from mempool.space.

        Returns dict with top_3_concentration_pct.
        """
        url = "https://mempool.space/api/v1/mining/pools/1m"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            # data is a list of pools with 'name', 'share' (%)
            pools = data.get("pools", [])
            if not pools:
                return None

            # Calculate top 3 pool concentration
            shares = sorted([p.get("share", 0.0) for p in pools], reverse=True)
            top_3_concentration = sum(shares[:3])

            return {
                "pools": pools[:10],  # Top 10 pools
                "top_3_concentration_pct": round(top_3_concentration, 2),
            }

        except (URLError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.debug("MinerFlowAgent: mempool pools fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_mempool_fees() -> dict | None:
        """
        Fetch 1 week of block fee data from mempool.space.

        Returns dict with current_fee_revenue_btc, avg_fee_revenue_7d_btc, fee_deviation_pct.
        """
        url = "https://mempool.space/api/v1/mining/blocks/fees/1w"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            # data may be a list directly (mempool.space returns a JSON array)
            # or a dict with a 'blocks' key — handle both shapes
            if isinstance(data, list):
                blocks = data
            else:
                blocks = data.get("blocks", [])
            if not blocks:
                return None

            # Extract fee revenue in BTC (convert from sats)
            # Prefer totalFees (actual sats collected) over avgFee * estimated vsize
            fee_revenues = []
            for b in blocks:
                total_fees_sats = b.get("totalFees", None)
                if total_fees_sats is not None:
                    fee_revenues.append(total_fees_sats / 1e8)  # sats → BTC
                else:
                    # Fallback: avgFee (sats/vB) × median block vsize (~1_500_000 vB)
                    avg_fee_per_vb = b.get("avgFee", 0.0)
                    fee_revenues.append(avg_fee_per_vb * 1_500_000 / 1e8)

            if not fee_revenues:
                return None

            current_fee_revenue = fee_revenues[0]  # Most recent
            avg_fee_revenue_7d = sum(fee_revenues) / len(fee_revenues)
            fee_deviation_pct = (
                ((current_fee_revenue - avg_fee_revenue_7d) / avg_fee_revenue_7d * 100.0)
                if avg_fee_revenue_7d > 0
                else 0.0
            )

            return {
                "current_fee_revenue_btc": round(current_fee_revenue, 6),
                "avg_fee_revenue_7d_btc": round(avg_fee_revenue_7d, 6),
                "fee_deviation_pct": round(fee_deviation_pct, 2),
                "block_count": len(blocks),
            }

        except (URLError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.debug("MinerFlowAgent: mempool fees fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_coingecko_bitcoin() -> dict | None:
        """
        Fetch Bitcoin price and volume from CoinGecko.

        Returns dict with current_price, price_change_pct_24h, volume_24h.
        """
        url = (
            "https://api.coingecko.com/api/v3/coins/bitcoin"
            "?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            market_data = data.get("market_data", {})
            if not market_data:
                return None

            current_price = market_data.get("current_price", {}).get("usd", 0.0)
            price_change_24h = market_data.get("price_change_percentage_24h", 0.0)
            volume_24h = market_data.get("total_volume", {}).get("usd", 0.0)

            return {
                "current_price": current_price,
                "price_change_pct_24h": round(price_change_24h, 2),
                "volume_24h": volume_24h,
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("MinerFlowAgent: CoinGecko Bitcoin fetch failed — %s", exc)
            return None

    def get_last_signal(self) -> dict:
        """Return latest cached miner flow signal."""
        if self._cache:
            return dict(self._cache)
        return {
            "signal": 0.0,
            "confidence": 0.0,
            "fee_revenue_btc": 0.0,
            "avg_fee_btc": 0.0,
            "fee_deviation_pct": 0.0,
            "top_pool_concentration": 0.0,
            "miner_behavior": "neutral",
            "metadata": {},
            "stale": True,
        }


# ── Module-level singleton (initialised by AgentCoordinator) ──
miner_flow_agent: MinerFlowAgent | None = None
