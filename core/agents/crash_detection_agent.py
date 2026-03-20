# ============================================================
# NEXUS TRADER — Crash Detection Agent  (Sprint 13)
#
# Aggregates 7 signal categories into a single Crash Score (0–10).
# Triggers graduated 4-tier defensive response via CrashDefenseController.
#
# Signal Categories & Weights:
#   1. Derivatives Risk          (0.25) — funding, OI, liquidations
#   2. Liquidity Risk            (0.20) — order book depth, bid/ask spread
#   3. Whale / On-Chain          (0.20) — large transfers, exchange inflows
#   4. Stablecoin Signals        (0.10) — SSR, stablecoin supply changes
#   5. Technical Breakdown       (0.10) — price vs key MAs, RSI extremes
#   6. Sentiment Extremes        (0.10) — Fear & Greed, social sentiment
#   7. Macro Risk                (0.05) — DXY surge, yield spike, SPX crash
#
# Crash Score Formula:
#   Score = sum(category_score_i * weight_i) * 10
#   Each category_score is 0.0–1.0 (1.0 = maximum crash signal)
#
# Severity Thresholds:
#   NORMAL       (score < 5.0)  — no action
#   DEFENSIVE    (score ≥ 5.0)  — monitor, reduce new entries
#   HIGH_ALERT   (score ≥ 7.0)  — halt new longs, tighten stops
#   EMERGENCY    (score ≥ 8.0)  — close 50% longs, read-only
#   SYSTEMIC     (score ≥ 9.0)  — close all, safe mode
#
# Poll interval: 60s (fast enough for emerging crash detection)
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from core.agents.base_agent import BaseAgent
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60  # fast poll for crash detection
_COINGECKO_CACHE_TTL = 300  # 5-minute cache — CoinGecko free tier: max ~30 req/min

# Category weights — must sum to 1.0
CATEGORY_WEIGHTS = {
    "derivatives":  0.25,
    "liquidity":    0.20,
    "whale_onchain":0.20,
    "stablecoin":   0.10,
    "technical":    0.10,
    "sentiment":    0.10,
    "macro":        0.05,
}

# Severity tiers with action thresholds
TIER_NORMAL    = "NORMAL"
TIER_DEFENSIVE = "DEFENSIVE"
TIER_HIGH_ALERT = "HIGH_ALERT"
TIER_EMERGENCY = "EMERGENCY"
TIER_SYSTEMIC  = "SYSTEMIC"

TIER_THRESHOLDS = {
    TIER_SYSTEMIC:  9.0,
    TIER_EMERGENCY: 8.0,
    TIER_HIGH_ALERT: 7.0,
    TIER_DEFENSIVE:  5.0,
    TIER_NORMAL:     0.0,
}


def score_to_tier(score: float) -> str:
    """Convert crash score (0–10) to severity tier."""
    if score >= TIER_THRESHOLDS[TIER_SYSTEMIC]:
        return TIER_SYSTEMIC
    if score >= TIER_THRESHOLDS[TIER_EMERGENCY]:
        return TIER_EMERGENCY
    if score >= TIER_THRESHOLDS[TIER_HIGH_ALERT]:
        return TIER_HIGH_ALERT
    if score >= TIER_THRESHOLDS[TIER_DEFENSIVE]:
        return TIER_DEFENSIVE
    return TIER_NORMAL


class CrashDetectionAgent(BaseAgent):
    """
    Consolidates crash signals from all other agents into a single
    Crash Score (0-10) and severity tier. Triggers CrashDefenseController
    when tier escalates.

    Data sourced from:
    - Other agent signals via EventBus (funding, order book, onchain, macro, sentiment)
    - Direct API calls for missing crash-specific signals (SSR, BTC.D, F&G)
    """

    def __init__(self, parent=None):
        super().__init__("crash_detection", parent)
        self._lock = threading.RLock()

        # Latest signals from other agents
        self._latest_signals: dict[str, dict] = {}
        self._current_score: float = 0.0
        self._current_tier: str = TIER_NORMAL
        self._score_history: list[tuple[datetime, float]] = []

        # CoinGecko API caches — shared by _fetch_btc_dominance and _fetch_stablecoin_ratio
        # to avoid 429 rate-limit errors (3 calls/min without cache → 3 calls/5min with cache)
        self._btc_dominance_cache: Optional[dict] = None
        self._btc_dominance_cache_ts: float = 0.0
        self._ssr_cache: Optional[dict] = None
        self._ssr_cache_ts: float = 0.0
        # Shared global data cache (both methods call /api/v3/global — share it)
        self._coingecko_global_cache: Optional[dict] = None
        self._coingecko_global_cache_ts: float = 0.0

        # Subscribe to other agent signals for aggregation
        bus.subscribe(Topics.FUNDING_RATE_UPDATED, self._on_agent_signal)
        bus.subscribe(Topics.ORDERBOOK_SIGNAL, self._on_agent_signal)
        bus.subscribe(Topics.OPTIONS_SIGNAL, self._on_agent_signal)
        bus.subscribe(Topics.MACRO_UPDATED, self._on_agent_signal)
        bus.subscribe(Topics.SOCIAL_SIGNAL, self._on_agent_signal)
        bus.subscribe(Topics.SENTIMENT_SIGNAL, self._on_agent_signal)
        bus.subscribe(Topics.STABLECOIN_UPDATED, self._on_stablecoin_signal)
        # Note: geopolitical events arrive via Topics.SOCIAL_SIGNAL with source="geopolitical"
        # and are handled by _on_agent_signal already registered above

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.CRASH_SCORE_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def _on_agent_signal(self, event) -> None:
        """Cache latest signal from each agent for crash score computation."""
        try:
            data = event.data if hasattr(event, "data") else {}
            source = data.get("source", event.topic)
            with self._lock:
                self._latest_signals[source] = data
        except Exception:
            pass

    def _on_stablecoin_signal(self, event) -> None:
        """
        Handle stablecoin updates — escalate crash score on depeg events.
        A depegging stablecoin is a systemic crash precursor.
        """
        try:
            data = event.data if hasattr(event, "data") else {}
            with self._lock:
                self._latest_signals["stablecoin_agent"] = data
                # Cache depeg count for next score computation
                depegs = data.get("depegs", [])
                self._latest_signals["_depeg_count"] = len(depegs)
                if depegs:
                    names = [d.get("name", "?") for d in depegs]
                    logger.warning(
                        "CrashDetectionAgent: Stablecoin depeg detected! %s",
                        ", ".join(names),
                    )
        except Exception:
            pass

    def fetch(self) -> dict:
        """Gather crash-specific signals that no other agent provides."""
        raw: dict[str, Any] = {}

        # BTC Dominance (crash precursor — rising dominance = altcoin panic)
        try:
            raw["btc_dominance"] = self._fetch_btc_dominance()
        except Exception as exc:
            logger.debug("CrashDetectionAgent: BTC dominance fetch failed — %s", exc)

        # Stablecoin Supply Ratio (falling SSR = less buying power = bearish)
        try:
            raw["ssr"] = self._fetch_stablecoin_ratio()
        except Exception as exc:
            logger.debug("CrashDetectionAgent: SSR fetch failed — %s", exc)

        # Fear & Greed (direct fetch for crash scoring)
        try:
            raw["fear_greed"] = self._fetch_fear_greed()
        except Exception as exc:
            logger.debug("CrashDetectionAgent: F&G fetch failed — %s", exc)

        # Copy cached agent signals (thread-safe)
        with self._lock:
            raw["agent_signals"] = dict(self._latest_signals)

        return raw

    def process(self, raw: dict) -> dict:
        """Compute 7-category crash score and determine severity tier."""
        agent_signals = raw.get("agent_signals", {})
        components: dict[str, dict] = {}

        # ── Category 1: Derivatives Risk (0.25) ──────────────
        deriv_score = self._score_derivatives(agent_signals, components)

        # ── Category 2: Liquidity Risk (0.20) ────────────────
        liquidity_score = self._score_liquidity(agent_signals, components)

        # ── Category 3: Whale / On-Chain (0.20) ──────────────
        onchain_score = self._score_onchain(agent_signals, raw.get("ssr"), components)

        # ── Category 4: Stablecoin Signals (0.10) ────────────
        stablecoin_score = self._score_stablecoin(raw.get("ssr"), raw.get("btc_dominance"), components)

        # ── Category 5: Technical Breakdown (0.10) ────────────
        technical_score = self._score_technical(agent_signals, components)

        # ── Category 6: Sentiment Extremes (0.10) ────────────
        sentiment_score = self._score_sentiment(agent_signals, raw.get("fear_greed"), components)

        # ── Category 7: Macro Risk (0.05) ────────────────────
        macro_score = self._score_macro(agent_signals, components)

        # ── Composite Crash Score (0–10) ──────────────────────
        raw_score = (
            deriv_score    * CATEGORY_WEIGHTS["derivatives"]   +
            liquidity_score* CATEGORY_WEIGHTS["liquidity"]     +
            onchain_score  * CATEGORY_WEIGHTS["whale_onchain"] +
            stablecoin_score* CATEGORY_WEIGHTS["stablecoin"]   +
            technical_score* CATEGORY_WEIGHTS["technical"]     +
            sentiment_score* CATEGORY_WEIGHTS["sentiment"]     +
            macro_score    * CATEGORY_WEIGHTS["macro"]
        )
        crash_score = round(min(10.0, max(0.0, raw_score * 10.0)), 2)

        # ── Tier determination ────────────────────────────────
        new_tier = score_to_tier(crash_score)

        with self._lock:
            # Score history is updated before velocity calculation
            self._score_history.append((datetime.now(timezone.utc), crash_score))
            if len(self._score_history) > 120:
                self._score_history = self._score_history[-120:]
            self._current_score = crash_score

        # ── Stablecoin depeg amplifier ────────────────────────
        depeg_count = 0
        with self._lock:
            depeg_count = self._latest_signals.get("_depeg_count", 0)
        if depeg_count > 0:
            # Each depegged stablecoin adds 1.0 to the score (systemic risk)
            crash_score = min(10.0, crash_score + depeg_count * 1.0)
            new_tier = score_to_tier(crash_score)

        # ── Crash score velocity (2-hour rate of change) ──────
        velocity = self._compute_velocity(crash_score)

        # Tier upgrade if velocity is rapid (score rising >2 pts/2h)
        if velocity > 2.0 and new_tier not in (TIER_EMERGENCY, TIER_SYSTEMIC):
            tier_order = [TIER_NORMAL, TIER_DEFENSIVE, TIER_HIGH_ALERT, TIER_EMERGENCY, TIER_SYSTEMIC]
            current_idx = tier_order.index(new_tier) if new_tier in tier_order else 0
            new_tier = tier_order[min(current_idx + 1, len(tier_order) - 1)]

        # Persist the FINAL tier (including velocity upgrade and depeg)
        # so the next cycle's old_tier comparison is accurate.
        with self._lock:
            old_tier = self._current_tier
            self._current_tier = new_tier

        # Publish tier change event if escalated
        if new_tier != old_tier:
            bus.publish(Topics.CRASH_TIER_CHANGED, {
                "old_tier": old_tier,
                "new_tier": new_tier,
                "score":    crash_score,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, source="crash_detection")
            if velocity > 2.0:
                logger.warning(
                    "CrashDetectionAgent: TIER CHANGED %s → %s | score=%.2f | velocity=%.2f pts/2h",
                    old_tier, new_tier, crash_score, velocity,
                )
            else:
                logger.warning(
                    "CrashDetectionAgent: TIER CHANGED %s → %s | score=%.2f",
                    old_tier, new_tier, crash_score,
                )

            # Trigger defensive controller if escalating
            if new_tier != TIER_NORMAL:
                self._trigger_defense(new_tier, crash_score, components)

        # ── Position size multiplier ──────────────────────────
        # Maps tier to a position sizing factor the risk gate can apply
        _TIER_MULTIPLIERS = {
            TIER_NORMAL:    1.00,
            TIER_DEFENSIVE: 0.65,
            TIER_HIGH_ALERT: 0.35,
            TIER_EMERGENCY: 0.10,
            TIER_SYSTEMIC:  0.00,
        }
        position_size_multiplier = _TIER_MULTIPLIERS.get(new_tier, 1.00)

        # Confidence: based on data availability
        available_categories = sum([
            deriv_score > 0, liquidity_score > 0,
            onchain_score > 0, sentiment_score > 0, macro_score > 0
        ])
        confidence = min(1.0, 0.40 + (available_categories * 0.12))

        logger.info(
            "CrashDetectionAgent: score=%.2f | tier=%s | velocity=%+.2f | "
            "pos_mult=%.2f | conf=%.2f",
            crash_score, new_tier, velocity, position_size_multiplier, confidence,
        )

        return {
            "signal":                  round(-(crash_score / 10.0), 4),  # negative = bearish
            "confidence":              round(confidence, 4),
            "crash_score":             crash_score,
            "tier":                    new_tier,
            "velocity_2h":             round(velocity, 3),
            "position_size_multiplier": position_size_multiplier,
            "components":              components,
            "source":                  "crash_detection",
        }

    # ── Category scorers ──────────────────────────────────────

    def _score_derivatives(self, signals: dict, components: dict) -> float:
        """Score derivatives risk: funding rate, OI, liquidations."""
        score = 0.0
        details: dict = {}

        # Funding rate signal (negative funding = longs paying shorts = bearish)
        funding = signals.get("funding_rate", {})
        if funding:
            rate = funding.get("funding_rate", 0.0) or 0.0
            oi_change = funding.get("oi_change_pct", 0.0) or 0.0
            # High negative funding → trapped longs → crash risk
            if rate < -0.06:
                score += 0.60
            elif rate < -0.03:
                score += 0.35
            elif rate > 0.10:
                score += 0.40  # extreme positive funding → bubble
            # Rising OI + price falling = dangerous
            if oi_change < -10:
                score += 0.25
            details["funding_rate"] = rate
            details["oi_change_pct"] = oi_change

        # Liquidation flow (large long liquidations = cascade risk)
        liq = signals.get("liquidation_flow", {})
        if liq:
            liq_signal = liq.get("signal", 0.0) or 0.0
            if liq_signal < -0.60:
                score += 0.50  # heavy long liquidations
            elif liq_signal < -0.30:
                score += 0.25
            details["liquidation_signal"] = liq_signal

        # Options / volatility surface
        options = signals.get("options_flow", {})
        if options:
            iv_skew = options.get("iv_skew", 0.0) or 0.0
            put_call = options.get("put_call_ratio", 1.0) or 1.0
            if iv_skew < -0.15:  # puts more expensive = crash hedging
                score += 0.30
            if put_call > 1.5:
                score += 0.20
            details["iv_skew"] = iv_skew
            details["put_call"] = put_call

        components["derivatives"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_liquidity(self, signals: dict, components: dict) -> float:
        """Score liquidity risk: order book depth, bid-ask spread."""
        score = 0.0
        details: dict = {}

        ob = signals.get("order_book", {})
        if ob:
            imbalance = ob.get("imbalance", 0.0) or 0.0
            bid_wall  = ob.get("bid_wall", 0.0) or 0.0
            ask_wall  = ob.get("ask_wall", 0.0) or 0.0
            ob_signal = ob.get("signal", 0.0) or 0.0

            # Heavy ask-side imbalance = liquidity drying up
            if imbalance < -0.4:
                score += 0.55
            elif imbalance < -0.2:
                score += 0.30

            # Ask wall much larger than bid wall = sell pressure
            if ask_wall > 0 and bid_wall > 0:
                wall_ratio = ask_wall / (bid_wall + 1e-9)
                if wall_ratio > 2.0:
                    score += 0.30
                elif wall_ratio > 1.5:
                    score += 0.15

            # Weak order book signal
            if ob_signal < -0.50:
                score += 0.25

            details["imbalance"] = imbalance
            details["ob_signal"] = ob_signal

        components["liquidity"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_onchain(self, signals: dict, ssr_data: Optional[dict], components: dict) -> float:
        """Score whale/on-chain risk: large exchange inflows, whale moves."""
        score = 0.0
        details: dict = {}

        onchain = signals.get("onchain", {})
        if onchain:
            oc_signal = onchain.get("signal", 0.0) or 0.0
            if oc_signal < -0.50:
                score += 0.55  # heavy exchange inflows (sell pressure)
            elif oc_signal < -0.25:
                score += 0.30
            details["onchain_signal"] = oc_signal

        # SSR (Stablecoin Supply Ratio) — low ratio means less stablecoin buying power
        if ssr_data:
            ssr_value = ssr_data.get("ssr", None)
            if ssr_value is not None:
                if ssr_value < 3.0:  # very low stablecoin relative to BTC mcap
                    score += 0.40
                elif ssr_value < 5.0:
                    score += 0.20
                details["ssr"] = ssr_value

        components["whale_onchain"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_stablecoin(self, ssr_data: Optional[dict], btc_dom: Optional[dict], components: dict) -> float:
        """Score stablecoin signals: SSR, BTC dominance surge."""
        score = 0.0
        details: dict = {}

        # Rapidly rising BTC dominance = altcoin panic / flight to BTC before selling
        if btc_dom:
            dominance     = btc_dom.get("btc_dominance_pct", 50.0)
            dom_change_7d = btc_dom.get("change_7d_pct", 0.0)
            if dom_change_7d > 3.0:  # BTC.D surging = altcoin capitulation
                score += 0.50
            elif dom_change_7d > 1.5:
                score += 0.25
            if dominance > 55:  # historically crash-correlated high
                score += 0.20
            details["btc_dominance_pct"] = dominance
            details["dom_change_7d"] = dom_change_7d

        # Stablecoin supply falling = redemptions = less market liquidity
        if ssr_data:
            ssr_change = ssr_data.get("ssr_change_7d", 0.0) or 0.0
            if ssr_change < -15:  # SSR dropped significantly
                score += 0.35
            elif ssr_change < -8:
                score += 0.15
            details["ssr_change_7d"] = ssr_change

        components["stablecoin"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_technical(self, signals: dict, components: dict) -> float:
        """Score technical breakdown signals from volatility surface."""
        score = 0.0
        details: dict = {}

        # Volatility surface: high IV spread = market pricing crash risk
        vs = signals.get("volatility_surface", {})
        if vs:
            iv_spread = vs.get("iv_spread", 0.0) or 0.0
            vs_signal = vs.get("signal", 0.0) or 0.0
            if iv_spread > 0.15:  # large IV premium = crash hedging
                score += 0.50
            elif iv_spread > 0.08:
                score += 0.25
            if vs_signal < -0.50:
                score += 0.30
            details["iv_spread"] = iv_spread

        components["technical"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_sentiment(self, signals: dict, fng_data: Optional[dict], components: dict) -> float:
        """Score sentiment extremes: F&G, social sentiment."""
        score = 0.0
        details: dict = {}

        # Fear & Greed — extreme greed near top = crash risk
        if fng_data:
            fng_value = fng_data.get("value", 50)
            if fng_value >= 85:   # extreme greed = market top signal
                score += 0.55
            elif fng_value >= 75:
                score += 0.30
            elif fng_value <= 10:  # extreme fear = panic selling (already crashing)
                score += 0.40
            elif fng_value <= 20:
                score += 0.20
            details["fear_greed"] = fng_value

        # Social sentiment — negative spike = panic
        social = signals.get("social_sentiment", {})
        if social:
            soc_signal = social.get("signal", 0.0) or 0.0
            if soc_signal < -0.70:
                score += 0.35
            elif soc_signal < -0.40:
                score += 0.15
            details["social_signal"] = soc_signal

        # News sentiment
        news = signals.get("news", {})
        if news:
            news_signal = news.get("signal", 0.0) or 0.0
            if news_signal < -0.60:
                score += 0.25

        components["sentiment"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    def _score_macro(self, signals: dict, components: dict) -> float:
        """Score macro risk: DXY surge, yield spike, SPX crash."""
        score = 0.0
        details: dict = {}

        macro = signals.get("macro", {})
        if macro:
            macro_risk = macro.get("macro_risk_score", 0.5) or 0.5
            macro_signal = macro.get("signal", 0.0) or 0.0
            regime_bias = macro.get("regime_bias", "neutral")

            if macro_risk > 0.75:
                score += 0.60
            elif macro_risk > 0.60:
                score += 0.30

            if regime_bias == "risk_off":
                score += 0.25

            # Check for DXY surge and yield spike in components
            components_data = macro.get("components", {})
            dxy_data = components_data.get("dxy", {})
            yield_data = components_data.get("us10y", {})
            if dxy_data.get("change_pct_5d", 0) > 2.0:
                score += 0.25  # DXY surge
            if yield_data.get("change_bp_5d", 0) > 20:
                score += 0.20  # yield spike

            details["macro_risk"] = macro_risk
            details["regime_bias"] = regime_bias

        # Geopolitical
        geo = signals.get("geopolitical", {})
        if geo:
            geo_signal = geo.get("signal", 0.0) or 0.0
            if geo_signal < -0.60:
                score += 0.25
            details["geopolitical"] = geo_signal

        components["macro"] = {
            "score": round(min(1.0, score), 4),
            "details": details,
        }
        return min(1.0, score)

    # ── Data fetchers ─────────────────────────────────────────

    def _fetch_btc_dominance(self) -> dict:
        """
        Fetch BTC dominance from CoinGecko global data (free).

        Cached for _COINGECKO_CACHE_TTL seconds (5 min) to avoid 429 rate-limit
        errors — the agent polls every 60s but CoinGecko data changes slowly.
        """
        # Return cached result if still fresh
        with self._lock:
            if (self._btc_dominance_cache is not None and
                    time.time() - self._btc_dominance_cache_ts < _COINGECKO_CACHE_TTL):
                logger.debug("CrashDetectionAgent: BTC dominance served from cache")
                return self._btc_dominance_cache

        global_data = self._fetch_coingecko_global()

        market_cap_pct = global_data.get("market_cap_percentage", {})
        btc_dominance = market_cap_pct.get("btc", 50.0)
        mcap_change = global_data.get("market_cap_change_percentage_24h_usd", 0.0)

        result = {
            "btc_dominance_pct": round(btc_dominance, 2),
            "change_7d_pct": round(mcap_change * 0.5, 2),  # 24h as proxy
            "total_market_cap_usd": global_data.get("total_market_cap", {}).get("usd", 0),
        }

        with self._lock:
            self._btc_dominance_cache = result
            self._btc_dominance_cache_ts = time.time()

        return result

    def _fetch_stablecoin_ratio(self) -> dict:
        """
        Fetch Stablecoin Supply Ratio (SSR) from CoinGecko.
        SSR = BTC Market Cap / Stablecoin Market Cap
        Low SSR = relatively less stablecoin liquidity = bearish

        Cached for _COINGECKO_CACHE_TTL seconds (5 min) to avoid 429 rate-limit
        errors. Reuses the shared global cache from _fetch_coingecko_global() to
        avoid a duplicate /api/v3/global call that _fetch_btc_dominance already made.
        """
        import urllib.request, json as _json

        # Return cached result if still fresh
        with self._lock:
            if (self._ssr_cache is not None and
                    time.time() - self._ssr_cache_ts < _COINGECKO_CACHE_TTL):
                logger.debug("CrashDetectionAgent: SSR served from cache")
                return self._ssr_cache

        # Get BTC market cap (separate endpoint — not in /global)
        url_btc = (
            "https://api.coingecko.com/api/v3/coins/bitcoin"
            "?localization=false&tickers=false&market_data=true"
            "&community_data=false&developer_data=false"
        )
        req = urllib.request.Request(url_btc, headers={
            "Accept": "application/json",
            "User-Agent": "NexusTrader/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            btc_data = _json.loads(resp.read().decode())

        btc_mcap = btc_data.get("market_data", {}).get("market_cap", {}).get("usd", 0)

        # Reuse shared global cache — avoids a duplicate /api/v3/global call
        global_data = self._fetch_coingecko_global()

        total_mcap = global_data.get("total_market_cap", {}).get("usd", 1)
        stable_pct = global_data.get("market_cap_percentage", {}).get("usdt", 5.0)
        stable_pct += global_data.get("market_cap_percentage", {}).get("usdc", 3.0)
        stable_pct += global_data.get("market_cap_percentage", {}).get("busd", 0.5)
        stable_mcap = total_mcap * (stable_pct / 100.0)

        ssr = (btc_mcap / stable_mcap) if stable_mcap > 0 else 10.0

        result = {
            "ssr": round(ssr, 2),
            "btc_mcap_usd": btc_mcap,
            "stable_mcap_usd": stable_mcap,
            "ssr_change_7d": 0.0,  # Would need historical to compute; placeholder
        }

        with self._lock:
            self._ssr_cache = result
            self._ssr_cache_ts = time.time()

        return result

    def _fetch_coingecko_global(self) -> dict:
        """
        Fetch /api/v3/global from CoinGecko with a shared 5-minute cache.

        Both _fetch_btc_dominance() and _fetch_stablecoin_ratio() need this
        endpoint. Sharing the cache cuts the call rate from 2/poll to 1/5min.
        """
        import urllib.request, json as _json

        with self._lock:
            if (self._coingecko_global_cache is not None and
                    time.time() - self._coingecko_global_cache_ts < _COINGECKO_CACHE_TTL):
                return self._coingecko_global_cache

        url = "https://api.coingecko.com/api/v3/global"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "NexusTrader/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())

        global_data = data.get("data", {})

        with self._lock:
            self._coingecko_global_cache = global_data
            self._coingecko_global_cache_ts = time.time()

        return global_data

    def _fetch_fear_greed(self) -> dict:
        """Fetch Fear & Greed Index from Alternative.me (free)."""
        import urllib.request, json as _json
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        entries = data.get("data", [{}])
        latest = entries[0] if entries else {}
        return {
            "value": int(latest.get("value", 50)),
            "label": latest.get("value_classification", "Neutral"),
        }

    def _compute_velocity(self, current_score: float) -> float:
        """
        Compute crash score velocity: change over last 2 hours.
        Returns points/2h (positive = rising crash risk, negative = improving).
        """
        with self._lock:
            history = list(self._score_history)

        if len(history) < 2:
            return 0.0

        now_dt = datetime.now(timezone.utc)
        cutoff_2h = now_dt.timestamp() - 7200.0  # 2 hours ago

        # Find most recent entry from 2h ago
        old_score = None
        for entry_dt, entry_score in history:
            if entry_dt.timestamp() <= cutoff_2h:
                old_score = entry_score

        if old_score is None:
            # Not enough history; use earliest available entry
            earliest_dt, old_score = history[0]
            elapsed_hrs = max(0.001, (now_dt - earliest_dt).total_seconds() / 3600.0)
            # Normalise to 2h equivalent
            return (current_score - old_score) / elapsed_hrs * 2.0

        return current_score - old_score

    def _trigger_defense(self, tier: str, score: float, components: dict) -> None:
        """Trigger the CrashDefenseController for the given tier."""
        try:
            from core.risk.crash_defense_controller import get_crash_defense_controller
            controller = get_crash_defense_controller()
            controller.respond_to_tier(tier, score, components)
        except Exception as exc:
            logger.error("CrashDetectionAgent: defense trigger failed — %s", exc)

    # ── Public API ────────────────────────────────────────────

    def get_crash_state(self) -> dict:
        """Return current crash score and tier (thread-safe)."""
        with self._lock:
            return {
                "score": self._current_score,
                "tier":  self._current_tier,
                "stale": self.is_stale,
            }

    def get_score_history(self) -> list[tuple[datetime, float]]:
        """Return recent score history for charting."""
        with self._lock:
            return list(self._score_history)


# ── Module-level singleton ────────────────────────────────────
crash_detection_agent: CrashDetectionAgent | None = None
