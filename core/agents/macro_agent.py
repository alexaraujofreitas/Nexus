# ============================================================
# NEXUS TRADER — Macro Intelligence Agent  (Sprint 4)
#
# Aggregates macro-economic signals relevant to crypto markets:
#
#   1. Fear & Greed Index (Alternative.me — free, no key)
#      Extreme Fear  (<20) → contrarian bullish
#      Extreme Greed (>80) → contrarian bearish
#
#   2. DXY (US Dollar Index) via yfinance
#      Rising DXY → bearish for BTC/crypto (risk-off)
#      Falling DXY → bullish for BTC/crypto (risk-on)
#
#   3. US10Y Yield via yfinance (^TNX)
#      Rising yields → bearish (tightening, risk-off)
#      Falling yields → bullish (easing, risk-on)
#
#   4. FRED API (optional — free, key required but gracefully skipped)
#      CPI YoY change, Fed Funds Rate direction
#      High/rising rates = bearish for risk assets
#
# Outputs:
#   macro_risk_score: float [0,1]  — 0=benign, 1=extreme risk
#   regime_bias: "risk_on" | "risk_off" | "neutral"
#   signal: float [-1,+1]          — negative=bearish, positive=bullish
#   confidence: float [0,1]
#
# Publishes: Topics.MACRO_UPDATED
# Poll interval: 3600s (1 hour) — macro data changes slowly
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 3600  # 1 hour — macro data is slow-moving

# Fear & Greed thresholds
_FNG_EXTREME_FEAR  = 20
_FNG_FEAR          = 35
_FNG_GREED         = 65
_FNG_EXTREME_GREED = 80

# DXY 5-day momentum thresholds (%)
_DXY_STRONG_RISE  =  1.5
_DXY_MILD_RISE    =  0.5
_DXY_MILD_FALL    = -0.5
_DXY_STRONG_FALL  = -1.5

# 10Y yield momentum thresholds (basis points)
_YIELD_STRONG_RISE  = 15
_YIELD_MILD_RISE    =  5
_YIELD_MILD_FALL    = -5
_YIELD_STRONG_FALL  = -15


class MacroAgent(BaseAgent):
    """
    Aggregates macroeconomic signals into a single macro_risk_score
    and directional bias for the orchestrator to use as a regime filter.

    Data sources (all free, no mandatory API keys):
    - Alternative.me Fear & Greed Index (REST)
    - Yahoo Finance: DXY (DX-Y.NYB) and US10Y (^TNX)
    - FRED API (optional, skipped gracefully if key absent)
    """

    def __init__(self, parent=None):
        super().__init__("macro", parent)
        self._lock = threading.RLock()
        self._last_macro: dict = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.MACRO_UPDATED

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict:
        """Fetch all macro data sources concurrently where possible."""
        raw: dict[str, Any] = {}

        # Fear & Greed Index
        try:
            raw["fng"] = self._fetch_fear_greed()
        except Exception as exc:
            logger.warning("MacroAgent: Fear & Greed fetch failed — %s", exc)

        # DXY — US Dollar Index
        try:
            raw["dxy"] = self._fetch_yfinance("DX-Y.NYB", days=10)
        except Exception as exc:
            logger.debug("MacroAgent: DXY fetch failed — %s", exc)

        # US 10-Year Treasury Yield
        try:
            raw["us10y"] = self._fetch_yfinance("^TNX", days=10)
        except Exception as exc:
            logger.debug("MacroAgent: US10Y fetch failed — %s", exc)

        # S&P 500 (risk sentiment proxy)
        try:
            raw["spx"] = self._fetch_yfinance("^GSPC", days=5)
        except Exception as exc:
            logger.debug("MacroAgent: SPX fetch failed — %s", exc)

        # VIX (CBOE Volatility Index — fear gauge)
        try:
            raw["vix"] = self._fetch_yfinance("^VIX", days=10)
        except Exception as exc:
            logger.debug("MacroAgent: VIX fetch failed — %s", exc)

        # FRED (optional — requires FRED_API_KEY in settings)
        try:
            raw["fred"] = self._fetch_fred()
        except Exception as exc:
            logger.debug("MacroAgent: FRED fetch skipped — %s", exc)

        # BTC Dominance
        try:
            raw["btc_dominance"] = self._fetch_btc_dominance()
        except Exception as exc:
            logger.debug("MacroAgent: BTC dominance fetch failed — %s", exc)

        # Stablecoin Supply Ratio
        try:
            raw["ssr"] = self._fetch_ssr()
        except Exception as exc:
            logger.debug("MacroAgent: SSR fetch failed — %s", exc)

        return raw

    def process(self, raw: dict) -> dict:
        if not raw:
            return {
                "signal": 0.0,
                "confidence": 0.0,
                "has_data": False,
                "macro_risk_score": 0.5,
                "regime_bias": "neutral",
                "components": {},
            }

        components: dict[str, dict] = {}
        weighted_signals: list[tuple[float, float, float]] = []  # (signal, conf, weight)

        # ── Fear & Greed ─────────────────────────────────────
        if "fng" in raw:
            fng_sig, fng_conf = self._score_fng(raw["fng"])
            components["fear_greed"] = {
                "value": raw["fng"].get("value", 50),
                "label": raw["fng"].get("value_classification", "Neutral"),
                "signal": round(fng_sig, 4),
                "confidence": round(fng_conf, 4),
            }
            weighted_signals.append((fng_sig, fng_conf, 1.0))

        # ── DXY ──────────────────────────────────────────────
        if "dxy" in raw:
            dxy_sig, dxy_conf = self._score_dxy(raw["dxy"])
            dxy_data = raw["dxy"]
            components["dxy"] = {
                "current": round(dxy_data.get("current", 0), 4),
                "change_pct_5d": round(dxy_data.get("change_pct", 0), 4),
                "signal": round(dxy_sig, 4),
                "confidence": round(dxy_conf, 4),
            }
            weighted_signals.append((dxy_sig, dxy_conf, 0.9))

        # ── US10Y Yield ───────────────────────────────────────
        if "us10y" in raw:
            yield_sig, yield_conf = self._score_yield(raw["us10y"])
            y_data = raw["us10y"]
            components["us10y"] = {
                "current": round(y_data.get("current", 0), 4),
                "change_bp_5d": round(y_data.get("change_bps", 0), 2),
                "signal": round(yield_sig, 4),
                "confidence": round(yield_conf, 4),
            }
            weighted_signals.append((yield_sig, yield_conf, 0.8))

        # ── SPX momentum ─────────────────────────────────────
        if "spx" in raw:
            spx_sig, spx_conf = self._score_equity(raw["spx"])
            components["spx"] = {
                "change_pct_5d": round(raw["spx"].get("change_pct", 0), 4),
                "signal": round(spx_sig, 4),
                "confidence": round(spx_conf, 4),
            }
            weighted_signals.append((spx_sig, spx_conf, 0.6))

        # ── VIX (fear gauge) ─────────────────────────────────
        if "vix" in raw:
            vix_sig, vix_conf = self._score_vix(raw["vix"])
            components["vix"] = {
                "current":      round(raw["vix"].get("current", 0), 2),
                "change_pct_5d": round(raw["vix"].get("change_pct", 0), 4),
                "signal":       round(vix_sig, 4),
                "confidence":   round(vix_conf, 4),
            }
            weighted_signals.append((vix_sig, vix_conf, 0.7))

        # ── FRED ─────────────────────────────────────────────
        if "fred" in raw and raw["fred"]:
            fred_sig, fred_conf = self._score_fred(raw["fred"])
            components["fred"] = {
                **raw["fred"],
                "signal": round(fred_sig, 4),
                "confidence": round(fred_conf, 4),
            }
            weighted_signals.append((fred_sig, fred_conf, 0.7))

        # ── BTC Dominance ────────────────────────────────────
        if "btc_dominance" in raw and raw["btc_dominance"]:
            btc_dom_sig, btc_dom_conf = self._score_btc_dominance(raw["btc_dominance"])
            components["btc_dominance"] = {
                "pct":    round(raw["btc_dominance"].get("pct", 50.0), 2),
                "change_7d": round(raw["btc_dominance"].get("change_7d", 0.0), 2),
                "signal": round(btc_dom_sig, 4),
                "confidence": round(btc_dom_conf, 4),
            }
            weighted_signals.append((btc_dom_sig, btc_dom_conf, 0.5))

        # ── Stablecoin Supply Ratio ───────────────────────────
        if "ssr" in raw and raw["ssr"]:
            ssr_sig, ssr_conf = self._score_ssr(raw["ssr"])
            components["ssr"] = {
                "value":  round(raw["ssr"].get("ssr", 10.0), 2),
                "signal": round(ssr_sig, 4),
                "confidence": round(ssr_conf, 4),
            }
            weighted_signals.append((ssr_sig, ssr_conf, 0.5))

        # ── Combined signal ───────────────────────────────────
        if not weighted_signals:
            combined_sig  = 0.0
            combined_conf = 0.0
        else:
            total_wc = sum(w * c for _, c, w in weighted_signals)
            combined_sig = (
                sum(s * c * w for s, c, w in weighted_signals) / total_wc
                if total_wc > 0 else 0.0
            )
            combined_conf = (
                sum(c * w for _, c, w in weighted_signals) /
                sum(w for _, _, w in weighted_signals)
            )

        # ── Macro risk score: map signal [-1,+1] → risk [0,1] ─
        # Negative signal (bearish macro) = high risk → macro_risk_score near 1
        # Positive signal (bullish macro) = low risk  → macro_risk_score near 0
        macro_risk_score = (1.0 - combined_sig) / 2.0   # linear mapping

        # ── Regime bias: 4-indicator vote model ───────────────
        # Each primary indicator (DXY, Equity, Yields, VIX) casts a vote:
        #   risk_on  = +1   (DXY falling, equities rising, yields falling, VIX falling)
        #   risk_off = -1   (DXY rising, equities falling, yields spiking, VIX rising)
        #   neutral  =  0   (inconclusive)
        # Net score ≥ +1 → risk_on | ≤ -1 → risk_off | else neutral
        regime_votes: list[int] = []
        regime_explanation_parts: list[str] = []

        if "dxy" in components:
            dxy_sig = components["dxy"]["signal"]
            if dxy_sig > 0.1:
                regime_votes.append(+1)
                regime_explanation_parts.append("DXY falling (+1 risk_on)")
            elif dxy_sig < -0.1:
                regime_votes.append(-1)
                regime_explanation_parts.append("DXY rising (-1 risk_off)")
            else:
                regime_votes.append(0)
                regime_explanation_parts.append("DXY neutral (0)")

        if "spx" in components:
            spx_sig = components["spx"]["signal"]
            if spx_sig > 0.1:
                regime_votes.append(+1)
                regime_explanation_parts.append("Equities rising (+1 risk_on)")
            elif spx_sig < -0.1:
                regime_votes.append(-1)
                regime_explanation_parts.append("Equities falling (-1 risk_off)")
            else:
                regime_votes.append(0)
                regime_explanation_parts.append("Equities neutral (0)")

        if "us10y" in components:
            yield_sig = components["us10y"]["signal"]
            if yield_sig > 0.1:
                regime_votes.append(+1)
                regime_explanation_parts.append("Yields declining (+1 risk_on)")
            elif yield_sig < -0.1:
                regime_votes.append(-1)
                regime_explanation_parts.append("Yields rising (-1 risk_off)")
            else:
                regime_votes.append(0)
                regime_explanation_parts.append("Yields stable (0)")

        if "vix" in components:
            vix_sig = components["vix"]["signal"]
            if vix_sig > 0.1:
                regime_votes.append(+1)
                regime_explanation_parts.append("VIX falling (+1 risk_on)")
            elif vix_sig < -0.1:
                regime_votes.append(-1)
                regime_explanation_parts.append("VIX rising (-1 risk_off)")
            else:
                regime_votes.append(0)
                regime_explanation_parts.append("VIX neutral (0)")

        macro_score = sum(regime_votes)   # range: -4 to +4
        if macro_score >= 1:
            regime_bias = "risk_on"
        elif macro_score <= -1:
            regime_bias = "risk_off"
        else:
            regime_bias = "neutral"

        macro_explanation = (
            f"Macro regime vote: {' | '.join(regime_explanation_parts)} → "
            f"score {macro_score:+d} → {regime_bias.upper()}"
            if regime_explanation_parts
            else "Insufficient data for regime classification"
        )

        with self._lock:
            self._last_macro = {
                "signal":           combined_sig,
                "confidence":       combined_conf,
                "macro_risk_score": macro_risk_score,
                "regime_bias":      regime_bias,
                "macro_bias":       regime_bias,       # alias for compatibility
                "macro_score":      macro_score,
                "explanation":      macro_explanation,
                "components":       components,
            }

        logger.info(
            "MacroAgent: signal=%+.3f | conf=%.2f | bias=%s | score=%+d | risk=%.2f",
            combined_sig, combined_conf, regime_bias, macro_score, macro_risk_score,
        )

        return {
            "signal":           round(combined_sig, 4),
            "confidence":       round(combined_conf, 4),
            "has_data": True,
            "macro_risk_score": round(macro_risk_score, 4),
            "regime_bias":      regime_bias,
            "macro_bias":       regime_bias,
            "macro_score":      macro_score,
            "explanation":      macro_explanation,
            "components":       components,
        }

    # ── Data fetchers ─────────────────────────────────────────

    def _fetch_fear_greed(self) -> dict:
        """Fetch Fear & Greed index from Alternative.me (free, no key)."""
        import urllib.request, json as _json
        url = "https://api.alternative.me/fng/?limit=2&format=json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        entries = data.get("data", [])
        if not entries:
            raise ValueError("No Fear & Greed data returned")
        latest = entries[0]
        return {
            "value": int(latest.get("value", 50)),
            "value_classification": latest.get("value_classification", "Neutral"),
            "timestamp": latest.get("timestamp", ""),
        }

    def _fetch_yfinance(self, ticker: str, days: int = 10) -> dict:
        """Fetch recent price data for a ticker via yfinance.

        Requires: pip install yfinance
        """
        try:
            import yfinance as yf
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "yfinance is not installed. Run: pip install yfinance"
            )
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{days}d", interval="1d")
        if hist.empty or len(hist) < 2:
            raise ValueError(f"Insufficient data for {ticker}")
        current  = float(hist["Close"].iloc[-1])
        start    = float(hist["Close"].iloc[0])
        change_pct = (current - start) / abs(start) * 100.0 if start != 0 else 0.0
        # For yield: express change in basis points
        change_bps = (current - start) * 100.0
        return {
            "ticker": ticker,
            "current": current,
            "change_pct": change_pct,
            "change_bps": change_bps,
        }

    def _fetch_fred(self) -> dict | None:
        """
        Fetch FRED data for CPI and Fed Funds Rate.
        Requires FRED_API_KEY in settings — silently skipped if absent.
        """
        try:
            from core.security.key_vault import key_vault
            fred_key = key_vault.load("agents.fred_api_key") or ""
        except Exception:
            fred_key = ""

        if not fred_key:
            return None

        import urllib.request, json as _json

        def _get_series(series_id: str, count: int = 3) -> list[float]:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}&api_key={fred_key}&file_type=json"
                f"&sort_order=desc&limit={count}"
            )
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            obs = data.get("observations", [])
            return [float(o["value"]) for o in obs if o.get("value") not in (".", None)]

        result: dict = {}

        try:
            cpi_vals = _get_series("CPIAUCSL", 3)
            if len(cpi_vals) >= 2:
                result["cpi_change"] = cpi_vals[0] - cpi_vals[1]
                result["cpi_latest"] = cpi_vals[0]
        except Exception:
            pass

        try:
            ffr_vals = _get_series("FEDFUNDS", 3)
            if len(ffr_vals) >= 2:
                result["ffr_latest"] = ffr_vals[0]
                result["ffr_change"] = ffr_vals[0] - ffr_vals[1]
        except Exception:
            pass

        return result if result else None

    # ── Signal scorers ────────────────────────────────────────

    def _score_fng(self, fng: dict) -> tuple[float, float]:
        """Convert Fear & Greed value to contrarian signal.

        Session 51 fix: the neutral zone (35-65) no longer returns flat 0.0.
        Instead, it produces a proportional contrarian signal scaled within
        the zone, so FNG=36 is slightly bullish and FNG=64 is slightly bearish.
        This ensures the macro agent always contributes a non-zero signal
        when FNG data is available.
        """
        value = fng.get("value", 50)
        if value <= _FNG_EXTREME_FEAR:
            return +0.75, 0.80   # Extreme fear = contrarian buy
        elif value <= _FNG_FEAR:
            return +0.40, 0.60
        elif value >= _FNG_EXTREME_GREED:
            return -0.75, 0.80   # Extreme greed = contrarian sell
        elif value >= _FNG_GREED:
            return -0.40, 0.60
        # Neutral zone (35-65): proportional contrarian micro-signal
        # Maps 35→+0.20, 50→0.0, 65→-0.20 (linear interpolation)
        midpoint = (_FNG_FEAR + _FNG_GREED) / 2.0  # 50
        half_range = (_FNG_GREED - _FNG_FEAR) / 2.0  # 15
        offset = (midpoint - value) / half_range  # +1 at fear end, -1 at greed end
        micro_signal = round(offset * 0.20, 4)
        return micro_signal, 0.40  # Confidence 0.40 (was 0.30) — passes gate

    def _score_dxy(self, dxy: dict) -> tuple[float, float]:
        """Rising DXY = bearish crypto (risk-off). Falling DXY = bullish.

        Session 51 fix: neutral zone now produces proportional micro-signal
        instead of flat 0.0.
        """
        chg = dxy.get("change_pct", 0.0)
        if chg >= _DXY_STRONG_RISE:
            return -0.70, 0.75
        elif chg >= _DXY_MILD_RISE:
            return -0.35, 0.55
        elif chg <= _DXY_STRONG_FALL:
            return +0.70, 0.75
        elif chg <= _DXY_MILD_FALL:
            return +0.35, 0.55
        # Neutral zone: proportional signal within ±0.5% range
        micro_signal = round(-chg / _DXY_MILD_RISE * 0.15, 4)  # inverse: rising DXY = bearish
        return micro_signal, 0.35

    def _score_yield(self, y_data: dict) -> tuple[float, float]:
        """Rising yields = bearish risk assets. Falling yields = bullish.

        Session 51 fix: neutral zone produces proportional micro-signal.
        """
        bps = y_data.get("change_bps", 0.0)
        if bps >= _YIELD_STRONG_RISE:
            return -0.65, 0.70
        elif bps >= _YIELD_MILD_RISE:
            return -0.30, 0.50
        elif bps <= _YIELD_STRONG_FALL:
            return +0.65, 0.70
        elif bps <= _YIELD_MILD_FALL:
            return +0.30, 0.50
        # Neutral zone: proportional signal within ±5 bps range
        micro_signal = round(-bps / _YIELD_MILD_RISE * 0.12, 4)
        return micro_signal, 0.35

    def _score_vix(self, vix_data: dict) -> tuple[float, float]:
        """
        VIX signal: rising VIX = fear = risk_off → bearish for crypto.
        Uses 5-day % change in VIX level (not absolute level alone).
        VIX level also considered: VIX > 30 = crisis zone regardless of direction.
        """
        chg_pct = vix_data.get("change_pct", 0.0)
        current = vix_data.get("current", 20.0)

        # Absolute crisis: VIX > 30 → significant fear
        if current > 35:
            return -0.70, 0.80
        elif current > 25:
            base_sig = -0.35
            base_conf = 0.60
        elif current < 13:
            # Complacency zone: very low VIX → contrarian warning
            base_sig = -0.15
            base_conf = 0.45
        else:
            base_sig = 0.0
            base_conf = 0.25

        # Adjust for 5d momentum (directional vote)
        if chg_pct >= 15.0:   # VIX spiking: fear increasing fast
            return -0.65, 0.75
        elif chg_pct >= 5.0:
            return min(0.0, base_sig) - 0.30, max(base_conf, 0.60)
        elif chg_pct <= -10.0:  # VIX collapsing: fear dissipating
            return +0.50, 0.65
        elif chg_pct <= -5.0:
            return +0.25, 0.50

        return round(base_sig, 4), round(base_conf, 4)

    def _score_equity(self, spx: dict) -> tuple[float, float]:
        """SPX momentum as crypto risk-on/off proxy.

        Session 51 fix: neutral zone produces proportional micro-signal.
        """
        chg = spx.get("change_pct", 0.0)
        if chg >= 2.0:
            return +0.50, 0.55
        elif chg >= 0.5:
            return +0.25, 0.40
        elif chg <= -2.0:
            return -0.50, 0.55
        elif chg <= -0.5:
            return -0.25, 0.40
        # Neutral zone: proportional signal within ±0.5% range
        micro_signal = round(chg / 0.5 * 0.10, 4)  # positive equity = bullish
        return micro_signal, 0.30

    def _score_fred(self, fred: dict) -> tuple[float, float]:
        """Score macro policy environment from FRED data."""
        if not fred:
            return 0.0, 0.20
        sig   = 0.0
        count = 0
        # Rising CPI → more rate hikes ahead → bearish
        if "cpi_change" in fred:
            sig += -0.3 if fred["cpi_change"] > 0 else +0.2
            count += 1
        # Rising FFR → tightening → bearish
        if "ffr_change" in fred:
            sig += -0.4 if fred["ffr_change"] > 0 else +0.3
            count += 1
        if count == 0:
            return 0.0, 0.20
        return round(sig / count, 4), 0.60

    def _fetch_btc_dominance(self) -> dict:
        """Fetch BTC dominance from CoinGecko global endpoint."""
        import urllib.request, json as _json
        url = "https://api.coingecko.com/api/v3/global"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "NexusTrader/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
        global_data = data.get("data", {})
        pct = global_data.get("market_cap_percentage", {}).get("btc", 50.0)
        change_24h = global_data.get("market_cap_change_percentage_24h_usd", 0.0)
        return {
            "pct": round(pct, 2),
            "change_7d": round(change_24h * 3.5, 2),  # approximate 7d from 24h
        }

    def _fetch_ssr(self) -> dict:
        """Fetch Stablecoin Supply Ratio from CoinGecko."""
        import urllib.request, json as _json
        url = "https://api.coingecko.com/api/v3/global"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "NexusTrader/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
        global_data = data.get("data", {})
        total_mcap = global_data.get("total_market_cap", {}).get("usd", 1e12)
        pcts = global_data.get("market_cap_percentage", {})
        stable_pct = (
            pcts.get("usdt", 5.0) +
            pcts.get("usdc", 3.0) +
            pcts.get("busd", 0.5) +
            pcts.get("dai", 0.3)
        )
        btc_pct = pcts.get("btc", 40.0)
        btc_mcap = total_mcap * btc_pct / 100.0
        stable_mcap = total_mcap * stable_pct / 100.0
        ssr = btc_mcap / stable_mcap if stable_mcap > 0 else 10.0
        return {"ssr": round(ssr, 2), "stable_pct": round(stable_pct, 2)}

    def _score_btc_dominance(self, data: dict) -> tuple[float, float]:
        """Rising BTC dominance = risk-off altcoin panic = bearish for alts."""
        pct = data.get("pct", 50.0)
        change_7d = data.get("change_7d", 0.0)
        sig = 0.0
        if pct > 55 and change_7d > 2:
            sig = -0.60  # BTC dominance surging = crypto-wide risk-off
        elif pct > 50 and change_7d > 1:
            sig = -0.30
        elif pct < 40 and change_7d < -1:
            sig = +0.30  # altcoin season
        return sig, 0.55

    def _score_ssr(self, data: dict) -> tuple[float, float]:
        """Low SSR = less stablecoin buying power = bearish."""
        ssr = data.get("ssr", 10.0)
        if ssr < 4.0:
            return -0.50, 0.65
        elif ssr < 7.0:
            return -0.20, 0.50
        elif ssr > 15.0:
            return +0.30, 0.55
        return 0.0, 0.30

    # ── Public API ────────────────────────────────────────────

    def get_macro_signal(self) -> dict:
        """Return the latest cached macro signal dict."""
        with self._lock:
            if self._last_macro:
                return dict(self._last_macro)
        return {
            "signal":           0.0,
            "confidence":       0.0,
            "macro_risk_score": 0.5,
            "regime_bias":      "neutral",
            "macro_bias":       "neutral",
            "macro_score":      0,
            "explanation":      "No macro data available",
            "stale":            True,
        }


# ── Module-level singleton ────────────────────────────────────
macro_agent: MacroAgent | None = None
