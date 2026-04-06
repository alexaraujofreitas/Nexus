# ============================================================
# NEXUS TRADER — Options Flow Agent  (Sprint 3)
#
# Polls Deribit's PUBLIC REST API for BTC and ETH options data.
# Deribit is the dominant venue for crypto options and provides
# free, unauthenticated access to all market data.
#
# Signals computed:
#   1. Put/Call Ratio (PCR) — sentiment indicator
#      PCR < 0.7 → bullish sentiment (more calls than puts)
#      PCR > 1.3 → bearish sentiment (more puts than calls)
#
#   2. Max Pain — strike price where maximum options expire worthless
#      Price gravitates toward max pain before expiry.
#      If current price > max_pain → signal to sell
#      If current price < max_pain → signal to buy
#
#   3. Gamma Exposure (GEX) — market maker hedging pressure
#      Positive GEX → market makers buy dips/sell rallies (dampening)
#      Negative GEX → market makers amplify moves (accelerating)
#
#   4. IV Skew — directional bias from implied volatility
#      Negative skew (puts more expensive) → fear/bearish positioning
#      Positive skew (calls more expensive) → greed/bullish positioning
#
# Publishes: Topics.OPTIONS_SIGNAL
# Only produces signals for BTC and ETH — other assets fall back to neutral.
# ============================================================
from __future__ import annotations

import logging
import math
import threading
from typing import Any
from datetime import datetime, timezone, timedelta

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 900   # 15 minutes — options markets are slower
_DERIBIT_URL  = "https://www.deribit.com/api/v2"

# Expiry selection: prefer next weekly expiry for relevance
_MAX_DAYS_TO_EXPIRY = 35   # Use options expiring within 35 days

# PCR thresholds
_PCR_STRONG_BULL = 0.60
_PCR_MODERATE_BULL = 0.80
_PCR_MODERATE_BEAR = 1.20
_PCR_STRONG_BEAR = 1.40

# Max pain distance thresholds (% from current price)
_MAX_PAIN_STRONG = 3.0    # > 3% from max pain → strong pull
_MAX_PAIN_WEAK   = 1.0    # > 1% from max pain → weak pull


class OptionsFlowAgent(BaseAgent):
    """
    Fetches and analyses options market data from Deribit.

    Only BTC/USDT and ETH/USDT receive meaningful signals.
    Other symbols get a passthrough neutral signal.
    The signals represent institutional positioning intelligence.
    """

    def __init__(self, parent=None):
        super().__init__("options_flow", parent)
        self._cache: dict[str, dict] = {}   # "BTC" / "ETH" → signal dict
        self._lock  = threading.RLock()

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.OPTIONS_SIGNAL

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict:
        """Fetch options data for BTC and ETH from Deribit."""
        results = {}
        for base in ("BTC", "ETH"):
            try:
                options = self._fetch_options_chain(base)
                if options:
                    results[base] = options
            except Exception as exc:
                logger.warning("OptionsFlowAgent: %s fetch error — %s", base, exc)
        return results

    def process(self, raw: dict) -> dict:
        if not raw:
            return {"signal": 0.0, "confidence": 0.0, "has_data": False, "assets": {}, "count": 0}

        with self._lock:
            for base, options_data in raw.items():
                analysis = self._analyse_options(base, options_data)
                self._cache[base] = analysis

            cache_snapshot = dict(self._cache)

        signals     = [v["signal"]     for v in cache_snapshot.values()]
        confidences = [v["confidence"] for v in cache_snapshot.values()]
        avg_signal  = sum(signals)     / len(signals)     if signals     else 0.0
        avg_conf    = sum(confidences) / len(confidences) if confidences else 0.0

        logger.info(
            "OptionsFlowAgent: %d assets | BTC=%s | ETH=%s",
            len(cache_snapshot),
            f"{cache_snapshot.get('BTC', {}).get('signal', 0):+.2f}" if "BTC" in cache_snapshot else "N/A",
            f"{cache_snapshot.get('ETH', {}).get('signal', 0):+.2f}" if "ETH" in cache_snapshot else "N/A",
        )

        return {
            "signal":     round(avg_signal, 4),
            "confidence": round(avg_conf,   4),
            "has_data": True,
            "assets":     cache_snapshot,
            "count":      len(cache_snapshot),
        }

    # ── Black-Scholes helpers ─────────────────────────────────

    @staticmethod
    def _parse_expiry(instrument_name: str) -> float:
        """
        Parse expiry timestamp (seconds) from Deribit instrument name.
        Format: BTC-28MAR25-90000-C  →  expiry part = '28MAR25'
        Returns 0.0 if parsing fails.
        """
        _MONTH_MAP = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
            "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
            "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        try:
            parts = instrument_name.split("-")
            if len(parts) < 4:
                return 0.0
            date_str = parts[1]  # e.g. "28MAR25"
            # Parse day, month abbreviation (3 chars), 2-digit year
            day = int(date_str[:2])
            mon = _MONTH_MAP.get(date_str[2:5].upper(), 0)
            yr  = int(date_str[5:]) + 2000  # "25" → 2025
            if mon == 0:
                return 0.0
            expiry_dt = datetime(yr, mon, day, 8, 0, 0, tzinfo=timezone.utc)
            return expiry_dt.timestamp()
        except (ValueError, IndexError):
            return 0.0

    @staticmethod
    def _bsm_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
        """
        Compute Black-Scholes gamma (same for calls and puts).

        Args:
            S: underlying price (index_price)
            K: strike price
            T: time to expiry in years (must be > 0)
            sigma: implied volatility as a decimal (e.g. 0.80 for 80%)
            r: risk-free rate (default 0.0 for crypto)
        Returns:
            gamma (float), or 0.0 on invalid input
        """
        if S <= 0 or K <= 0 or T <= 1e-6 or sigma <= 1e-6:
            return 0.0
        try:
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
            # Standard normal PDF: N'(d1)
            N_prime_d1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2.0 * math.pi)
            return N_prime_d1 / (S * sigma * sqrt_T)
        except (ValueError, ZeroDivisionError, OverflowError):
            return 0.0

    # ── Options chain fetching ────────────────────────────────

    def _fetch_options_chain(self, base: str) -> list[dict]:
        """
        Fetch all active options for a base currency using Deribit's
        get_book_summary_by_currency endpoint — ONE request instead of
        one-per-instrument.  Returns up to a few thousand rows covering
        all strikes and expiries; we filter to near-term inside here.
        """
        import urllib.request, json as _json

        url = (
            f"{_DERIBIT_URL}/public/get_book_summary_by_currency"
            f"?currency={base}&kind=option"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Accept":     "application/json",
                "User-Agent": "NexusTrader/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())

        summaries = data.get("result", [])
        if not summaries:
            return []

        # Filter to near-term expiries using creation_timestamp embedded in
        # instrument name (e.g. BTC-28MAR25-90000-C → parse month/year).
        # Simpler: accept everything — PCR / max-pain / IV-skew all work across
        # all expiries, and we have no individual-ticker latency to worry about.
        now_ms  = datetime.now(timezone.utc).timestamp() * 1000
        cutoff  = now_ms + _MAX_DAYS_TO_EXPIRY * 86400 * 1000

        options_data = []
        for item in summaries:
            name = item.get("instrument_name", "")
            if not name:
                continue

            # Determine type: Deribit names end with -C or -P
            opt_type = "call" if name.endswith("-C") else "put"

            # Parse strike from name:  BTC-28MAR25-90000-C → parts[2] = "90000"
            parts = name.split("-")
            try:
                strike = float(parts[2]) if len(parts) >= 4 else 0.0
            except (ValueError, IndexError):
                continue
            if strike <= 0:
                continue

            # Skip far-future expiries when creation_timestamp is available
            creation_ts = item.get("creation_timestamp", 0) or 0
            if creation_ts > cutoff:
                continue  # This field is actually expiry on some API versions

            # IV: prefer mid of bid_iv/ask_iv, fall back to mark_iv
            bid_iv  = float(item.get("bid_iv",  0) or 0)
            ask_iv  = float(item.get("ask_iv",  0) or 0)
            mark_iv = float(item.get("mark_iv", 0) or 0)
            iv = (bid_iv + ask_iv) / 2.0 if (bid_iv > 0 and ask_iv > 0) else mark_iv

            # Parse actual expiry from instrument name (creation_ts is unreliable)
            expiry_ts = self._parse_expiry(name)
            if expiry_ts == 0.0:
                expiry_ts = creation_ts  # fallback

            # Time to expiry in years for BSM gamma
            now_ts    = datetime.now(timezone.utc).timestamp()
            T_years   = max(0.0, (expiry_ts - now_ts) / (365.25 * 86400))
            sigma     = iv / 100.0 if iv > 1.0 else iv  # normalise % → decimal
            index_price = float(item.get("underlying_price", 0) or 0)

            # Compute BSM gamma (valid for both calls and puts)
            gamma = self._bsm_gamma(
                S=index_price, K=strike, T=T_years, sigma=sigma
            ) if (index_price > 0 and sigma > 0 and T_years > 0) else 0.0

            options_data.append({
                "name":        name,
                "strike":      strike,
                "type":        opt_type,
                "expiry_ts":   expiry_ts,
                "oi":          float(item.get("open_interest",   0) or 0),
                "volume":      float(item.get("volume",          0) or 0),
                "iv":          iv,
                "delta":       0.0,   # not in summary endpoint
                "gamma":       gamma, # computed via BSM
                "last_price":  float(item.get("mark_price",      0) or 0),
                "index_price": index_price,
            })

        logger.debug(
            "OptionsFlowAgent: %s — %d options loaded in single request",
            base, len(options_data),
        )
        return options_data

    # ── Signal analysis ───────────────────────────────────────

    def _analyse_options(self, base: str, options: list[dict]) -> dict:
        if not options:
            return {"signal": 0.0, "confidence": 0.0, "error": "no_data"}

        calls = [o for o in options if o["type"] == "call"]
        puts  = [o for o in options if o["type"] == "put"]

        # Current index price (use from any option)
        index_price = next((o["index_price"] for o in options if o["index_price"] > 0), 0.0)

        # ── Put/Call Ratio (by volume) ────────────────────────
        call_vol = sum(o["volume"] for o in calls)
        put_vol  = sum(o["volume"] for o in puts)
        pcr      = (put_vol / call_vol) if call_vol > 0.01 else 1.0

        pcr_signal, pcr_conf = self._pcr_signal(pcr)

        # ── Max Pain ──────────────────────────────────────────
        max_pain = self._compute_max_pain(calls, puts)
        mp_signal, mp_conf = self._max_pain_signal(index_price, max_pain)

        # ── IV Skew ───────────────────────────────────────────
        skew_signal, skew_conf = self._iv_skew_signal(calls, puts, index_price)

        # ── Gamma Exposure ────────────────────────────────────
        gex_signal, gex_conf = self._gamma_exposure_signal(options, index_price)

        # ── Combine signals (weighted average) ────────────────
        weights = [
            (pcr_signal,   pcr_conf,   1.0),   # PCR — primary sentiment
            (mp_signal,    mp_conf,    0.8),   # Max pain — gravity effect
            (skew_signal,  skew_conf,  0.7),   # IV skew — positioning
            (gex_signal,   gex_conf,   0.6),   # GEX — market maker flow
        ]
        total_weight = sum(w * c for _, c, w in weights)
        combined_sig = (
            sum(s * c * w for s, c, w in weights) / total_weight
            if total_weight > 0 else 0.0
        )
        combined_conf = (
            sum(c * w for _, c, w in weights) / sum(w for _, _, w in weights)
        )

        return {
            "signal":      round(combined_sig, 4),
            "confidence":  round(combined_conf, 4),
            "pcr":         round(pcr, 4),
            "max_pain":    round(max_pain, 2),
            "index_price": round(index_price, 2),
            "pcr_signal":  round(pcr_signal, 4),
            "mp_signal":   round(mp_signal, 4),
            "skew_signal": round(skew_signal, 4),
            "gex_signal":  round(gex_signal, 4),
            "direction":   "bullish" if combined_sig > 0 else ("bearish" if combined_sig < 0 else "neutral"),
        }

    def _pcr_signal(self, pcr: float) -> tuple[float, float]:
        if pcr <= _PCR_STRONG_BULL:
            return +0.8, 0.80   # Very bullish PCR (more calls)
        elif pcr <= _PCR_MODERATE_BULL:
            return +0.4, 0.60
        elif pcr >= _PCR_STRONG_BEAR:
            return -0.8, 0.80   # Very bearish PCR (many puts)
        elif pcr >= _PCR_MODERATE_BEAR:
            return -0.4, 0.60
        return 0.0, 0.30

    def _compute_max_pain(self, calls: list[dict], puts: list[dict]) -> float:
        """
        Compute the strike where option sellers (market makers) lose the least.
        At expiry, price tends to gravitate toward this level.
        """
        all_strikes = sorted(set(
            o["strike"] for o in calls + puts if o["strike"] > 0
        ))
        if not all_strikes:
            return 0.0

        min_pain = float("inf")
        max_pain_strike = all_strikes[0]

        for test_strike in all_strikes:
            # Loss to call writers if expired at test_strike
            call_loss = sum(
                max(0, test_strike - o["strike"]) * o["oi"]
                for o in calls
            )
            # Loss to put writers
            put_loss = sum(
                max(0, o["strike"] - test_strike) * o["oi"]
                for o in puts
            )
            total_pain = call_loss + put_loss
            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = test_strike

        return float(max_pain_strike)

    def _max_pain_signal(self, current_price: float, max_pain: float) -> tuple[float, float]:
        if current_price <= 0 or max_pain <= 0:
            return 0.0, 0.20
        distance_pct = (max_pain - current_price) / current_price * 100.0
        if distance_pct >= _MAX_PAIN_STRONG:
            return +0.7, 0.75   # Price well below max pain → expected to rise
        elif distance_pct >= _MAX_PAIN_WEAK:
            return +0.35, 0.55
        elif distance_pct <= -_MAX_PAIN_STRONG:
            return -0.7, 0.75   # Price well above max pain → expected to fall
        elif distance_pct <= -_MAX_PAIN_WEAK:
            return -0.35, 0.55
        return 0.0, 0.30

    def _iv_skew_signal(
        self, calls: list[dict], puts: list[dict], index_price: float
    ) -> tuple[float, float]:
        if index_price <= 0:
            return 0.0, 0.20

        # Compare IV of ~25-delta puts vs ~25-delta calls (skew measure)
        otm_puts  = [p for p in puts  if p["strike"] < index_price * 0.97 and p["iv"] > 0]
        otm_calls = [c for c in calls if c["strike"] > index_price * 1.03 and c["iv"] > 0]

        if not otm_puts or not otm_calls:
            return 0.0, 0.20

        avg_put_iv  = sum(p["iv"] for p in otm_puts)  / len(otm_puts)
        avg_call_iv = sum(c["iv"] for c in otm_calls) / len(otm_calls)

        skew = avg_put_iv - avg_call_iv   # positive = put skew (fear)
        if skew > 15:
            return -0.6, 0.70   # Strong put skew = bearish fear
        elif skew > 7:
            return -0.3, 0.50
        elif skew < -15:
            return +0.6, 0.70   # Call skew = bullish greed
        elif skew < -7:
            return +0.3, 0.50
        return 0.0, 0.30

    def _gamma_exposure_signal(
        self, options: list[dict], index_price: float
    ) -> tuple[float, float]:
        """
        Compute net Gamma Exposure (GEX) from BSM gammas.

        Convention: dealers are short calls (negative call gamma) and
        typically long puts (positive put gamma).
        Net dealer GEX = sum(put_gamma * oi) - sum(call_gamma * oi)
        - Positive GEX → dealers long net gamma → buy dips/sell rallies (range-bound)
        - Negative GEX → dealers short net gamma → amplify price moves (trending)
        """
        if index_price <= 0:
            return 0.0, 0.20

        # Focus on options within ±10% of current price (most relevant to hedging)
        near_options = [
            o for o in options
            if o["gamma"] > 0 and abs(o["strike"] - index_price) / index_price < 0.10
        ]

        if not near_options:
            return 0.0, 0.20

        # Net dealer GEX: dealers sell calls → short call gamma, buy puts → long put gamma
        call_gex = sum(o["gamma"] * o["oi"] for o in near_options if o["type"] == "call")
        put_gex  = sum(o["gamma"] * o["oi"] for o in near_options if o["type"] == "put")
        net_gex  = put_gex - call_gex   # positive = dealer long gamma

        # Normalise against a benchmark of combined GEX
        total_gex = call_gex + put_gex
        if total_gex < 1e-12:
            return 0.0, 0.20

        gex_ratio = net_gex / total_gex  # range: roughly -1 to +1

        # Positive GEX → range-bound → dampens directional signals (bearish bias skew)
        if gex_ratio > 0.30:
            return -0.15, 0.45  # Positive dealer gamma: slight dampener (mean-reversion)
        elif gex_ratio < -0.30:
            return 0.15, 0.45   # Negative dealer gamma: vol expansion likely (trend signal)
        else:
            return 0.0, 0.30

    # ── Public API ────────────────────────────────────────────

    def get_symbol_signal(self, symbol: str) -> dict:
        """Return options signal for a given trading symbol."""
        with self._lock:
            for base in ("BTC", "ETH"):
                if symbol.startswith(base):
                    return self._cache.get(base, {"signal": 0.0, "confidence": 0.0, "stale": True})
        return {"signal": 0.0, "confidence": 0.0, "stale": True}


# ── Module-level singleton ────────────────────────────────────
options_flow_agent: OptionsFlowAgent | None = None
