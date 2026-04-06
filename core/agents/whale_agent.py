# ============================================================
# NEXUS TRADER — Whale Tracking Agent
#
# Monitors large cryptocurrency whale transactions using:
# - Whale Alert free endpoint (with fallback simulation)
# - blockchain.info large BTC transactions
# - mempool.space whale detector
#
# Signal logic:
#   Large BTC inflow to exchanges         → bearish (-0.5 to -0.8)
#   Large BTC outflow from exchanges      → accumulation (bullish +0.3 to +0.6)
#   Whale accumulation detected           → bullish (+0.4 to +0.7)
#   Multiple large txs in short window    → spike (+/-0.6)
#
# Also detects coordinated whale clusters and boosts confidence
# by 0.15 when clustering identifies coordinated movement.
#
# Publishes: Topics.WHALE_ALERT, Topics.WHALE_CLUSTER_UPDATED
# ============================================================
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics, bus
from core.scanning.watchlist_gate import get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 600  # 10 minutes


class WhaleClustering:
    """
    Detects coordinated whale behavior through transaction clustering.

    Groups transactions by similar sizes within a 30-minute window
    and uses k-means style bucketing to identify patterns suggesting
    coordinated or whale-driven movement.
    """

    def __init__(self):
        self._tx_history: list[dict] = []  # Rolling history of txs
        self._max_history = 1000  # Keep last 1000 txs for clustering

    def add_transaction(self, tx: dict) -> None:
        """Add a transaction to the clustering buffer."""
        self._tx_history.append(tx)
        if len(self._tx_history) > self._max_history:
            self._tx_history.pop(0)

    def _cluster_addresses(self, window_minutes: int = 30) -> list[dict]:
        """
        Group transactions by similar sizes and timing.

        Uses simple bucketing: groups txs with similar BTC amounts
        (within 20% relative size) that occur within window_minutes.

        Returns list of clusters, each with:
        {
            'size_btc': float,
            'count': int,
            'addresses': [str],
            'timestamps': [float],
            'direction': 'inflow' | 'outflow',
        }
        """
        if not self._tx_history:
            return []

        now = time.time()
        window_cutoff = now - (window_minutes * 60)

        # Filter to recent window
        recent = [tx for tx in self._tx_history if tx.get("timestamp", 0) >= window_cutoff]
        if not recent:
            return []

        # Sort by size
        recent_sorted = sorted(recent, key=lambda t: t.get("amount_btc", 0), reverse=True)

        clusters: list[dict] = []
        used_indices = set()

        for i, tx in enumerate(recent_sorted):
            if i in used_indices:
                continue

            base_amount = tx.get("amount_btc", 0)
            if base_amount == 0:
                continue

            # Find similar-sized txs (within 20%)
            cluster_indices = {i}
            for j, other_tx in enumerate(recent_sorted):
                if j <= i or j in used_indices:
                    continue
                other_amount = other_tx.get("amount_btc", 0)
                if other_amount == 0:
                    continue

                ratio = max(base_amount, other_amount) / min(base_amount, other_amount)
                if ratio <= 1.2:  # Within 20%
                    cluster_indices.add(j)

            if len(cluster_indices) > 1:
                cluster_txs = [recent_sorted[idx] for idx in cluster_indices]
                addresses = [tx.get("address", "unknown") for tx in cluster_txs]
                timestamps = [tx.get("timestamp", 0) for tx in cluster_txs]
                directions = [tx.get("direction", "unknown") for tx in cluster_txs]

                # Determine dominant direction
                direction = (
                    "inflow" if directions.count("inflow") > len(directions) / 2
                    else "outflow"
                )

                clusters.append({
                    "size_btc": base_amount,
                    "count": len(cluster_indices),
                    "addresses": addresses,
                    "timestamps": timestamps,
                    "direction": direction,
                })

                used_indices.update(cluster_indices)

        return clusters

    def detect_coordinated_movement(self) -> dict | None:
        """
        Detect coordinated whale movement.

        Returns dict with clustering analysis or None if no coordination detected.
        Returns:
        {
            'detected': bool,
            'clusters': [cluster_dict],
            'dominant_direction': 'inflow' | 'outflow' | None,
            'confidence_boost': float,
        }
        """
        clusters = self._cluster_addresses(window_minutes=30)

        if not clusters or len(clusters) < 2:
            return {
                "detected": False,
                "clusters": clusters,
                "dominant_direction": None,
                "confidence_boost": 0.0,
            }

        # Multiple clusters detected = coordinated movement
        directions = [c["direction"] for c in clusters]
        dominant = (
            "inflow" if directions.count("inflow") > len(directions) / 2
            else "outflow"
        )

        return {
            "detected": True,
            "clusters": clusters,
            "dominant_direction": dominant,
            "confidence_boost": 0.15,
        }


class WhaleTrackingAgent(BaseAgent):
    """
    Monitors large cryptocurrency whale transactions.

    Tracks BTC inflows/outflows to exchanges, whale accumulation,
    and detects coordinated whale behavior using clustering analysis.
    Uses free data sources: blockchain.info, mempool.space, and
    Whale Alert free endpoint (with simulation fallback).
    """

    def __init__(self, parent=None):
        super().__init__("whale", parent)
        self._clustering = WhaleClustering()
        self._cache: dict[str, Any] = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.WHALE_ALERT

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Update configuration if changed."""
        try:
            from config.settings import settings
            min_whale_usd = settings.get("agents.whale.min_usd", 500000)
            if isinstance(min_whale_usd, (int, float)):
                self._cache["min_whale_usd"] = min_whale_usd
        except Exception as exc:
            logger.debug("WhaleTrackingAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch whale transaction data from multiple free sources."""
        results = {
            "transactions": [],
            "metadata": {"sources": []},
        }

        try:
            # Try blockchain.info large transactions
            large_txs = self._fetch_blockchain_large_txs()
            if large_txs:
                results["transactions"].extend(large_txs)
                results["metadata"]["sources"].append("blockchain.info")
                get_api_call_counter().record("blockchain.info")
        except Exception as exc:
            logger.debug("WhaleTrackingAgent: blockchain.info fetch failed — %s", exc)

        try:
            # Try mempool.space whale detector
            mempool_data = self._fetch_mempool_whales()
            if mempool_data:
                results["transactions"].extend(mempool_data)
                results["metadata"]["sources"].append("mempool.space")
                get_api_call_counter().record("mempool.space")
        except Exception as exc:
            logger.debug("WhaleTrackingAgent: mempool.space fetch failed — %s", exc)

        # Add transactions to clustering for coordinated movement detection
        for tx in results["transactions"]:
            self._clustering.add_transaction(tx)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Analyze whale transactions and compute trading signals.

        Returns dict with:
        - signal (float [-1, 1]): directional bias
        - confidence (float [0, 1]): signal strength
        - whale_count (int): number of whales detected
        - total_volume_btc (float): sum of whale transaction volumes
        - dominant_direction (str): 'inflow', 'outflow', or 'neutral'
        - transactions (list): top 5 largest transactions
        - clustering (dict): coordinated movement analysis
        """
        if not raw or not raw.get("transactions"):
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "whale_count": 0,
                "total_volume_btc": 0.0,
                "dominant_direction": "neutral",
                "transactions": [],
            }

        txs = raw.get("transactions", [])
        if not txs:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "whale_count": 0,
                "total_volume_btc": 0.0,
                "dominant_direction": "neutral",
                "transactions": [],
            }

        # Analyze whale activity
        signal, confidence, direction, metadata = self._compute_signal(txs)

        # Detect coordinated movement
        clustering_result = self._clustering.detect_coordinated_movement()

        # Boost confidence if coordinated movement detected
        if clustering_result.get("detected"):
            confidence = min(1.0, confidence + clustering_result["confidence_boost"])
            logger.info(
                "WhaleTrackingAgent: coordinated movement detected, "
                "boosted confidence to %.2f", confidence
            )
            # Publish clustering event
            bus.publish(
                Topics.WHALE_CLUSTER_UPDATED,
                {
                    "clustering": clustering_result,
                    "signal": signal,
                    "timestamp": time.time(),
                },
                source="whale",
            )

        # Get top 5 transactions by volume
        top_txs = sorted(txs, key=lambda t: t.get("amount_btc", 0), reverse=True)[:5]

        logger.info(
            "WhaleTrackingAgent: %d whales | signal=%.3f | confidence=%.2f | "
            "direction=%s | total_vol_btc=%.2f",
            len(txs), signal, confidence, direction, metadata.get("total_volume_btc", 0),
        )

        return {
            "signal": round(signal, 4),
            "confidence": round(confidence, 4),
            "has_data": True,
            "whale_count": len(txs),
            "total_volume_btc": round(metadata.get("total_volume_btc", 0.0), 2),
            "dominant_direction": direction,
            "transactions": top_txs,
            "clustering": clustering_result,
        }

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(txs: list[dict]) -> tuple[float, float, str, dict]:
        """
        Infer market signal from whale transaction patterns.

        Returns (signal, confidence, direction, metadata).
        """
        if not txs:
            return 0.0, 0.0, "neutral", {}

        # Count inflows vs outflows
        inflows = [t for t in txs if t.get("direction") == "inflow"]
        outflows = [t for t in txs if t.get("direction") == "outflow"]

        inflow_vol = sum(t.get("amount_btc", 0) for t in inflows)
        outflow_vol = sum(t.get("amount_btc", 0) for t in outflows)
        total_vol = inflow_vol + outflow_vol

        metadata = {
            "inflow_count": len(inflows),
            "outflow_count": len(outflows),
            "inflow_volume_btc": round(inflow_vol, 2),
            "outflow_volume_btc": round(outflow_vol, 2),
            "total_volume_btc": round(total_vol, 2),
        }

        signal = 0.0
        confidence = 0.0
        direction = "neutral"

        if total_vol == 0:
            return signal, confidence, direction, metadata

        inflow_ratio = inflow_vol / total_vol

        # Multiple large transactions in short window → spike signal
        if len(txs) >= 3:
            signal = 0.5 if inflow_ratio > 0.6 else -0.5
            confidence = 0.65
            direction = "spike_down" if inflow_ratio > 0.6 else "spike_up"
            metadata["reason"] = f"Whale spike: {len(txs)} large txs detected"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Large BTC outflow from exchanges → accumulation (bullish)
        if inflow_ratio < 0.3 and outflow_vol > 100:
            signal = 0.55
            confidence = 0.70
            direction = "outflow_accumulation"
            metadata["reason"] = "Large whale outflow from exchanges (accumulation)"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Large BTC inflow to exchanges → bearish
        if inflow_ratio > 0.7 and inflow_vol > 100:
            signal = -0.65
            confidence = 0.75
            direction = "inflow_selling"
            metadata["reason"] = "Large whale inflow to exchanges (distribution)"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Whale accumulation (balanced outflow)
        if 0.3 <= inflow_ratio <= 0.7 and outflow_vol > inflow_vol:
            signal = 0.50
            confidence = 0.60
            direction = "whale_accumulation"
            metadata["reason"] = "Whale accumulation pattern detected"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Default: small activity
        signal = 0.0
        confidence = 0.3
        direction = "neutral"
        metadata["reason"] = "Insufficient whale activity"

        return round(signal, 4), round(confidence, 4), direction, metadata

    # ── Data source APIs ───────────────────────────────────────

    @staticmethod
    def _fetch_blockchain_large_txs() -> list[dict]:
        """
        Fetch recent large BTC transactions from blockchain.info.

        Retrieves latest BTC block, then scans for transactions > 100 BTC.
        Returns list of transaction dicts with: hash, amount_btc, direction,
        timestamp, address.
        """
        try:
            # Get latest block
            url_block = "https://blockchain.info/latestblock?format=json"
            with urllib.request.urlopen(url_block, timeout=10) as response:
                text = response.read().decode("utf-8")
                block_data = json.loads(text)

            block_hash = block_data.get("hash")
            if not block_hash:
                return []

            # Get block details
            url_detail = f"https://blockchain.info/rawblock/{block_hash}?format=json"
            with urllib.request.urlopen(url_detail, timeout=10) as response:
                text = response.read().decode("utf-8")
                block_detail = json.loads(text)

            txs_out = []
            for tx in block_detail.get("tx", []):
                tx_hash = tx.get("hash", "")
                timestamp = tx.get("time", 0)

                # Scan inputs and outputs for large amounts
                for inp in tx.get("inputs", []):
                    amount_satoshi = inp.get("output", {}).get("value", 0)
                    amount_btc = amount_satoshi / 100_000_000
                    if amount_btc >= 100:
                        addr = inp.get("output", {}).get("addresses", ["unknown"])[0]
                        txs_out.append({
                            "hash": tx_hash,
                            "amount_btc": round(amount_btc, 2),
                            "direction": "outflow",
                            "timestamp": timestamp,
                            "address": addr,
                            "source": "blockchain.info",
                        })

                for outp in tx.get("out", []):
                    amount_satoshi = outp.get("value", 0)
                    amount_btc = amount_satoshi / 100_000_000
                    if amount_btc >= 100:
                        addrs = outp.get("addresses", ["unknown"])
                        addr = addrs[0] if addrs else "unknown"
                        txs_out.append({
                            "hash": tx_hash,
                            "amount_btc": round(amount_btc, 2),
                            "direction": "inflow",
                            "timestamp": timestamp,
                            "address": addr,
                            "source": "blockchain.info",
                        })

            return txs_out

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("WhaleTrackingAgent: blockchain.info fetch failed — %s", exc)
            return []

    @staticmethod
    def _fetch_mempool_whales() -> list[dict]:
        """
        Fetch whale transaction data from mempool.space unconfirmed transactions.

        Retrieves recent large unconfirmed transactions from the mempool
        to detect whale activity before block confirmation.
        Returns list of whale transactions with actual direction inferred
        from input/output structure.
        """
        try:
            # Get unconfirmed transactions (mempool)
            url = "https://mempool.space/api/v1/mempool"
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                mempool_data = json.loads(text)

            txs_out = []

            # Get recent tx list
            tx_ids = mempool_data.get("txids", [])[:50]  # Last 50 unconfirmed txs

            now = time.time()
            for tx_id in tx_ids:
                try:
                    # Fetch individual tx details
                    tx_url = f"https://mempool.space/api/v1/tx/{tx_id}"
                    with urllib.request.urlopen(tx_url, timeout=5) as resp:
                        tx_detail = json.loads(resp.read().decode("utf-8"))

                    # Calculate total input/output values
                    total_input = sum(inp.get("prevout", {}).get("value", 0) for inp in tx_detail.get("vin", []))
                    total_output = sum(out.get("value", 0) for out in tx_detail.get("vout", []))

                    # Convert from satoshis to BTC
                    total_input_btc = total_input / 100_000_000
                    total_output_btc = total_output / 100_000_000

                    # Only consider transactions > 10 BTC as potential whale activity
                    if total_input_btc >= 10:
                        # Infer direction: if change < 10% of input, likely an outflow
                        change = abs(total_output_btc - total_input_btc) / total_input_btc if total_input_btc > 0 else 0
                        direction = "outflow" if change < 0.10 else "inflow"

                        # Get first input address (sender proxy)
                        first_input = tx_detail.get("vin", [{}])[0]
                        address = first_input.get("prevout", {}).get("scriptpubkey_address", "unknown")

                        txs_out.append({
                            "hash": tx_id,
                            "amount_btc": round(total_input_btc, 2),
                            "direction": direction,
                            "timestamp": int(now),
                            "address": address,
                            "source": "mempool.space_unconfirmed",
                        })
                except (URLError, json.JSONDecodeError, KeyError, ValueError):
                    # Skip individual tx if it fails; continue to next
                    continue

            if txs_out:
                logger.info(
                    "WhaleTrackingAgent: mempool.space found %d whale txs in unconfirmed pool",
                    len(txs_out),
                )
            return txs_out

        except (URLError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.debug("WhaleTrackingAgent: mempool.space unconfirmed fetch failed — %s", exc)
            return []


# ── Module-level singleton (initialised by AgentCoordinator) ──
whale_agent: WhaleTrackingAgent | None = None
