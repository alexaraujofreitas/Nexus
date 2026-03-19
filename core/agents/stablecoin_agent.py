# ============================================================
# NEXUS TRADER — Stablecoin Liquidity Agent
#
# Monitors stablecoin market caps, supplies, and flows using:
# - CoinGecko stablecoin market data (free, no auth)
# - DefiLlama stablecoin supply data (free, no auth)
#
# Signal logic:
#   Total stablecoin supply growing fast (>2% in 24h)  → bullish +0.5 (dry powder)
#   USDT/USDC supply shrinking                          → bearish -0.4 (capital leaving)
#   Stablecoin dominance rising vs BTC                  → bearish -0.3 (risk-off)
#   DAI supply spike                                    → bullish +0.3 (leverage in DeFi)
#   Any stablecoin depegging (price < 0.99 or > 1.01)  → critical -0.9 (systemic risk)
#
# Publishes: Topics.STABLECOIN_UPDATED
# ============================================================
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any
from urllib.error import URLError

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics
from core.scanning.watchlist_gate import get_api_call_counter

logger = logging.getLogger(__name__)

_POLL_SECONDS = 7200  # 2 hours


class StablecoinLiquidityAgent(BaseAgent):
    """
    Monitors stablecoin market dynamics and liquidity flows.

    Tracks total stablecoin supply, individual stablecoin market caps
    (USDT, USDC, DAI, BUSD, FRAX, TUSD), depegging events, and infers
    market liquidity conditions from stablecoin dynamics.

    Uses free APIs: CoinGecko and DefiLlama.
    """

    def __init__(self, parent=None):
        super().__init__("stablecoin", parent)
        # Include "bitcoin" to fetch BTC market cap for dominance calculation.
        # NOTE: "binance-usd" (BUSD) was removed — Binance wound down BUSD in 2023 and
        # DeFi Llama now reports it as depegged (~$0 liquidity), generating a false
        # stablecoin depeg crash signal every 2 hours.
        self._stablecoin_ids = ["tether", "usd-coin", "dai", "frax", "true-usd", "bitcoin"]
        self._cache: dict[str, dict] = {}
        self._supply_history: list[dict] = []  # Track supply over time for 24h change

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.STABLECOIN_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def on_settings_changed(self) -> None:
        """Update configuration if changed."""
        try:
            from config.settings import settings
            stablecoins = settings.get("agents.stablecoin.ids", self._stablecoin_ids)
            if isinstance(stablecoins, list):
                self._stablecoin_ids = stablecoins
        except Exception as exc:
            logger.debug("StablecoinLiquidityAgent: settings update error: %s", exc)

    def fetch(self) -> dict[str, Any]:
        """Fetch stablecoin data from CoinGecko and DefiLlama."""
        results = {
            "stablecoins": {},
            "total_supply": None,
            "metadata": {"sources": []},
        }

        try:
            # Fetch individual stablecoin data from CoinGecko
            coingecko_data = self._fetch_coingecko_stablecoins()
            if coingecko_data:
                results["stablecoins"].update(coingecko_data)
                results["metadata"]["sources"].append("coingecko")
                get_api_call_counter().record("coingecko")
        except Exception as exc:
            logger.debug("StablecoinLiquidityAgent: CoinGecko fetch failed — %s", exc)

        try:
            # Fetch total stablecoin supply from DefiLlama
            defillama_data = self._fetch_defillama_stablecoins()
            if defillama_data:
                results["total_supply"] = defillama_data.get("total_supply_usd")
                results["stablecoins"].update(defillama_data.get("stablecoins", {}))
                results["metadata"]["sources"].append("defillama")
                get_api_call_counter().record("defillama")
        except Exception as exc:
            logger.debug("StablecoinLiquidityAgent: DefiLlama fetch failed — %s", exc)

        # Track supply history for 24h change calculation
        if results["total_supply"]:
            self._supply_history.append({
                "timestamp": time.time(),
                "total_supply": results["total_supply"],
            })
            # Keep last 100 entries (roughly 50 hours at 30-minute intervals)
            if len(self._supply_history) > 100:
                self._supply_history.pop(0)

        return results

    def process(self, raw: dict[str, Any]) -> dict:
        """
        Analyze stablecoin data and compute liquidity signals.

        Returns dict with:
        - signal (float [-1, 1]): directional bias
        - confidence (float [0, 1]): signal strength
        - total_supply_usd (float): total stablecoin market cap
        - net_flow_24h_usd (float): supply change in last 24h
        - depegs (list): stablecoins with price deviations
        - supply_change_pct (float): percentage change in supply
        - dominant_flow_direction (str): 'inflow', 'outflow', or 'stable'
        """
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "total_supply_usd": 0.0,
                "net_flow_24h_usd": 0.0,
                "depegs": [],
                "supply_change_pct": 0.0,
                "dominant_flow_direction": "stable",
            }

        stablecoins = raw.get("stablecoins", {})
        total_supply = raw.get("total_supply", 0.0) or 0.0

        # Compute 24h supply change
        supply_change_pct = self._compute_supply_change_pct()
        net_flow_usd = self._estimate_net_flow_24h(total_supply, supply_change_pct)

        # Check for depegging events
        depegs = self._check_depegs(stablecoins)

        # Compute signal
        signal, confidence, direction, metadata = self._compute_signal(
            total_supply, supply_change_pct, stablecoins, depegs
        )

        logger.info(
            "StablecoinLiquidityAgent: supply=%.0f | change=%.2f%% | "
            "signal=%.3f | confidence=%.2f | depegs=%d",
            total_supply, supply_change_pct, signal, confidence, len(depegs),
        )

        return {
            "signal": round(signal, 4),
            "confidence": round(confidence, 4),
            "total_supply_usd": round(total_supply, 0),
            "net_flow_24h_usd": round(net_flow_usd, 0),
            "depegs": depegs,
            "supply_change_pct": round(supply_change_pct, 2),
            "dominant_flow_direction": direction,
            "metadata": metadata,
        }

    # ── Signal logic ───────────────────────────────────────────

    @staticmethod
    def _compute_signal(
        total_supply: float,
        supply_change_pct: float,
        stablecoins: dict[str, dict],
        depegs: list[dict],
    ) -> tuple[float, float, str, dict]:
        """
        Infer market liquidity signal from stablecoin dynamics.

        Returns (signal, confidence, direction, metadata).
        """
        metadata = {
            "supply_change_pct": supply_change_pct,
            "depeg_count": len(depegs),
        }

        signal = 0.0
        confidence = 0.0
        direction = "stable"

        # Critical: Depegging event (systemic risk)
        if depegs:
            signal = -0.9
            confidence = 0.95
            direction = "depeg_alert"
            depeg_names = [d.get("name", "unknown") for d in depegs]
            metadata["reason"] = f"Depegging detected: {', '.join(depeg_names)}"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Fast supply growth (>2% in 24h) → bullish (dry powder entering)
        if supply_change_pct > 2.0:
            signal = 0.50
            confidence = 0.70
            direction = "inflow"
            metadata["reason"] = "Fast stablecoin supply growth (dry powder entering)"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # USDT/USDC supply shrinking → bearish (capital leaving)
        usdt = stablecoins.get("tether", {})
        usdc = stablecoins.get("usd-coin", {})
        usdt_change = usdt.get("change_24h_pct", 0.0)
        usdc_change = usdc.get("change_24h_pct", 0.0)

        if usdt_change < -0.5 and usdc_change < -0.5:
            signal = -0.40
            confidence = 0.65
            direction = "outflow"
            metadata["reason"] = "USDT/USDC supply shrinking (capital leaving)"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Stablecoin dominance rising vs BTC → bearish (risk-off)
        total_btc_usd = stablecoins.get("_bitcoin_market_cap", 0.0)
        if total_btc_usd > 0 and total_supply > 0:
            stablecoin_dominance = total_supply / (total_supply + total_btc_usd)
            if stablecoin_dominance > 0.35:
                signal = -0.30
                confidence = 0.55
                direction = "risk_off"
                metadata["reason"] = "High stablecoin dominance vs BTC (risk-off sentiment)"
                return round(signal, 4), round(confidence, 4), direction, metadata

        # DAI supply spike → bullish (leverage entering DeFi)
        dai = stablecoins.get("dai", {})
        dai_change = dai.get("change_24h_pct", 0.0)
        if dai_change > 1.5:
            signal = 0.30
            confidence = 0.60
            direction = "defi_leverage"
            metadata["reason"] = "DAI supply spike (leverage/borrowing in DeFi)"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Moderate supply growth (0.5% - 2%) → mildly bullish
        if 0.5 <= supply_change_pct <= 2.0:
            signal = 0.25
            confidence = 0.50
            direction = "inflow"
            metadata["reason"] = "Moderate stablecoin supply growth"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Moderate supply decline (-0.5% - 0%) → mildly bearish
        if -0.5 <= supply_change_pct < 0.0:
            signal = -0.15
            confidence = 0.45
            direction = "outflow"
            metadata["reason"] = "Moderate stablecoin supply decline"
            return round(signal, 4), round(confidence, 4), direction, metadata

        # Default: stable
        signal = 0.0
        confidence = 0.3
        direction = "stable"
        metadata["reason"] = "Stablecoin conditions stable, no significant flow"

        return round(signal, 4), round(confidence, 4), direction, metadata

    def _check_depegs(self, stablecoins: dict[str, dict]) -> list[dict]:
        """
        Check for stablecoin depegging events (price < 0.99 or > 1.01).

        Returns list of depegged stablecoins with metadata.
        """
        depegs = []
        for name, data in stablecoins.items():
            # Skip non-dict entries (e.g. _bitcoin_market_cap float key)
            if not isinstance(data, dict):
                continue
            price = data.get("price_usd", 1.0)
            if price < 0.99 or price > 1.01:
                depegs.append({
                    "name": name,
                    "price": round(price, 4),
                    "deviation_pct": round(((price - 1.0) / 1.0) * 100, 2),
                })
        return depegs

    def _compute_supply_change_pct(self) -> float:
        """
        Compute percentage change in total stablecoin supply over 24h.

        Returns percentage change.
        """
        if len(self._supply_history) < 2:
            return 0.0

        now = time.time()
        cutoff_24h = now - (24 * 3600)

        # Find supply from 24h ago
        old_supply = None
        for entry in self._supply_history:
            if entry["timestamp"] <= cutoff_24h:
                old_supply = entry["total_supply"]

        if old_supply is None or old_supply == 0:
            return 0.0

        latest_supply = self._supply_history[-1]["total_supply"]
        change_pct = ((latest_supply - old_supply) / old_supply) * 100
        return change_pct

    @staticmethod
    def _estimate_net_flow_24h(total_supply: float, change_pct: float) -> float:
        """Estimate net USD flow in last 24h based on supply change."""
        if total_supply == 0:
            return 0.0
        return (change_pct / 100.0) * total_supply

    # ── CoinGecko API ──────────────────────────────────────────

    def _fetch_coingecko_stablecoins(self) -> dict[str, dict] | None:
        """
        Fetch stablecoin market data from CoinGecko.

        Returns dict mapping coin IDs to market data:
        {
            'tether': {'price_usd': 1.0, 'market_cap_usd': ..., 'change_24h_pct': ...},
            'usd-coin': {...},
            ...
        }
        """
        ids_str = ",".join(self._stablecoin_ids)
        url = (
            f"https://api.coingecko.com/api/v3/coins/markets"
            f"?vs_currency=usd&ids={ids_str}&order=market_cap_desc"
        )

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            results = {}
            for coin in data:
                coin_id = coin.get("id")
                if not coin_id:
                    continue

                # Bitcoin is fetched solely for BTC market cap dominance calc
                if coin_id == "bitcoin":
                    results["_bitcoin_market_cap"] = coin.get("market_cap", 0.0) or 0.0
                    continue

                results[coin_id] = {
                    "name": coin.get("name", ""),
                    "symbol": coin.get("symbol", "").upper(),
                    "price_usd": coin.get("current_price", 0.0),
                    "market_cap_usd": coin.get("market_cap", 0.0) or 0.0,
                    "change_24h_pct": coin.get("price_change_percentage_24h", 0.0) or 0.0,
                    "volume_24h_usd": coin.get("total_volume", 0.0) or 0.0,
                }

            return results if results else None

        except (URLError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("StablecoinLiquidityAgent: CoinGecko fetch failed — %s", exc)
            return None

    @staticmethod
    def _fetch_defillama_stablecoins() -> dict[str, Any] | None:
        """
        Fetch total stablecoin supply from DefiLlama.

        Returns dict with:
        {
            'total_supply_usd': float,
            'stablecoins': {
                'stablecoin_name': {'supply_usd': float, 'chain': str},
                ...
            }
        }
        """
        url = "https://stablecoins.llama.fi/stablecoins?includePrices=true"

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                text = response.read().decode("utf-8")
                data = json.loads(text)

            stablecoins_data = data.get("peggedAssets", [])
            if not stablecoins_data:
                return None

            total_supply = 0.0
            stablecoins = {}

            for sc in stablecoins_data:
                name = sc.get("name", "unknown")
                symbol = sc.get("symbol", "")

                # Sum circulating amounts
                # DefiLlama chainCirculating values are nested dicts:
                # {"Ethereum": {"current": {"peggedUSD": 1234.56}}, ...}
                # Extract the first numeric leaf value from each chain entry
                chains = sc.get("chainCirculating", {})
                circulating = 0.0
                for chain_val in chains.values():
                    if isinstance(chain_val, (int, float)):
                        circulating += float(chain_val)
                    elif isinstance(chain_val, dict):
                        # Recurse one level: {"current": {"peggedUSD": 1234.56}}
                        inner = chain_val.get("current", chain_val)
                        if isinstance(inner, (int, float)):
                            circulating += float(inner)
                        elif isinstance(inner, dict):
                            for v in inner.values():
                                if isinstance(v, (int, float)):
                                    circulating += float(v)
                                    break
                total_supply += circulating

                stablecoins[name.lower().replace(" ", "")] = {
                    "name": name,
                    "symbol": symbol,
                    "supply_usd": circulating,
                    "chains": list(chains.keys()),
                }

            return {
                "total_supply_usd": total_supply,
                "stablecoins": stablecoins,
            }

        except (URLError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.debug("StablecoinLiquidityAgent: DefiLlama fetch failed — %s", exc)
            return None


# ── Module-level singleton (initialised by AgentCoordinator) ──
stablecoin_agent: StablecoinLiquidityAgent | None = None
