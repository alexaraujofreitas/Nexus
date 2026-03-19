# ============================================================
# NEXUS TRADER — Sector Rotation Agent  (Sprint 4 — Part D)
#
# Tracks macro sector momentum and capital rotation patterns
# to determine whether institutional money is flowing toward
# or away from crypto / risk assets.
#
# Approach:
#   - Monitor key sector ETFs via yfinance (5-day momentum)
#   - Risk-ON sectors rising → bullish for crypto
#   - Risk-OFF sectors rising → bearish for crypto
#   - BTC dominance trend (from CoinGecko) as alt-coin rotation signal
#
# Sector ETF mapping (all free via yfinance):
#   Risk-ON  (rising = bullish crypto):
#     XLK  — Technology
#     QQQ  — Nasdaq 100
#     ARKK — Innovation
#
#   Risk-OFF (rising = bearish crypto):
#     GLD  — Gold (safe haven)
#     TLT  — 20Y Treasury (flight to safety)
#     VIX  — Volatility Index (fear gauge)
#     XLU  — Utilities (defensive)
#
# Publishes: Topics.SOCIAL_SIGNAL (source="sector_rotation")
# Poll interval: 3600s (1 hour) — sector momentum is slow
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Any
import math

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 14400  # 4 hours

# (ticker, weight, direction)
# direction: +1 = rising is bullish for crypto; -1 = rising is bearish
_RISK_ON_TICKERS: list[tuple[str, float, int]] = [
    ("QQQ",  1.0, +1),   # Nasdaq 100 — closest proxy for tech/risk appetite
    ("XLK",  0.8, +1),   # Technology sector
    ("ARKK", 0.6, +1),   # Innovation/high-beta (correlates strongly with BTC)
]
_RISK_OFF_TICKERS: list[tuple[str, float, int]] = [
    ("GLD",  0.9, -1),   # Gold — risk-off safe haven
    ("TLT",  0.8, -1),   # Long bonds — flight to safety
    ("^VIX", 1.0, -1),   # CBOE Volatility Index — fear gauge (rising = risk-off)
    ("XLU",  0.5, -1),   # Utilities — defensive sector
]
_ALL_TICKERS = _RISK_ON_TICKERS + _RISK_OFF_TICKERS


class SectorRotationAgent(BaseAgent):
    """
    Monitors sector ETF momentum to identify macro risk-on/risk-off shifts.

    When institutional capital rotates into risk assets (QQQ, XLK, ARKK),
    crypto typically benefits.  When capital flows to defensives (GLD, TLT, XLU),
    crypto typically suffers.
    """

    def __init__(self, parent=None):
        super().__init__("sector_rotation", parent)
        self._lock  = threading.RLock()
        self._cache: dict = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.SOCIAL_SIGNAL  # source="sector_rotation" differentiates

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}

        # Sector ETFs via yfinance
        try:
            raw["etfs"] = self._fetch_etf_momentum()
        except Exception as exc:
            logger.warning("SectorRotationAgent: ETF fetch failed — %s", exc)

        # BTC dominance trend
        try:
            raw["btc_dominance"] = self._fetch_btc_dominance()
        except Exception as exc:
            logger.debug("SectorRotationAgent: BTC dominance fetch skipped — %s", exc)

        return raw

    def process(self, raw: dict) -> dict:
        if not raw:
            return {
                "signal": 0.0, "confidence": 0.0,
                "rotation_bias": "neutral", "source": "sector_rotation",
            }

        etf_data: dict[str, dict] = {}
        risk_on_scores: list[float] = []
        risk_off_scores: list[float] = []
        conf_scores: list[float] = []
        dominant_sectors: list[str] = []

        # ── ETF momentum ──────────────────────────────────────
        etfs: dict[str, float] = raw.get("etfs", {})  # ticker → 5d change %

        for (ticker, weight, direction) in _RISK_ON_TICKERS:
            if ticker not in etfs:
                continue
            chg_pct = etfs[ticker]
            # Compute 5-day momentum percentage
            momentum_pct = chg_pct

            # Normalize to [-1, +1] based on magnitude and sign
            if abs(momentum_pct) < 0.5:
                norm_momentum = 0.0
                conf = 0.20
            elif abs(momentum_pct) < 1.5:
                norm_momentum = (momentum_pct / 1.5) * 0.3
                conf = 0.45
            elif abs(momentum_pct) < 3.0:
                norm_momentum = (momentum_pct / 3.0) * 0.55
                conf = 0.60
            else:
                norm_momentum = (momentum_pct / abs(momentum_pct)) * 0.75
                conf = 0.75

            sig = norm_momentum * weight * direction
            risk_on_scores.append(sig)
            conf_scores.append(conf)

            etf_data[ticker] = {
                "change_pct_5d": round(momentum_pct, 3),
                "signal": round(sig, 4),
                "direction": "risk_on",
                "weight": weight,
            }
            if momentum_pct > 0:
                dominant_sectors.append(ticker)

        for (ticker, weight, direction) in _RISK_OFF_TICKERS:
            if ticker not in etfs:
                continue
            chg_pct = etfs[ticker]
            momentum_pct = chg_pct

            # Normalize similarly
            if abs(momentum_pct) < 0.5:
                norm_momentum = 0.0
                conf = 0.20
            elif abs(momentum_pct) < 1.5:
                norm_momentum = (momentum_pct / 1.5) * 0.3
                conf = 0.45
            elif abs(momentum_pct) < 3.0:
                norm_momentum = (momentum_pct / 3.0) * 0.55
                conf = 0.60
            else:
                norm_momentum = (momentum_pct / abs(momentum_pct)) * 0.75
                conf = 0.75

            # For risk-off: negative is good (defensives rising = bearish)
            sig = norm_momentum * weight * direction
            risk_off_scores.append(sig)
            conf_scores.append(conf)

            etf_data[ticker] = {
                "change_pct_5d": round(momentum_pct, 3),
                "signal": round(sig, 4),
                "direction": "risk_off",
                "weight": weight,
            }

        # Compute aggregate scores
        risk_on_score = sum(risk_on_scores) / len(risk_on_scores) if risk_on_scores else 0.0
        risk_off_score = abs(sum(risk_off_scores) / len(risk_off_scores)) if risk_off_scores else 0.0
        net_rotation_score = risk_on_score - risk_off_score

        # Apply tanh normalization: tanh(x / 3) * 0.8
        if net_rotation_score != 0:
            signal = math.tanh(net_rotation_score / 3.0) * 0.8
        else:
            signal = 0.0

        # Incorporate BTC dominance
        btc_dom = raw.get("btc_dominance")
        btc_dominance = 0.0
        if btc_dom is not None:
            btc_dominance = btc_dom.get("current", 0.0)
            dom_chg = btc_dom.get("change_7d", 0.0)
            # Falling dominance (altseason) → +0.1 to signal
            if dom_chg < -1.0:
                signal += 0.1
            elif dom_chg > 1.0:
                signal -= 0.05

        # Clamp signal
        signal = max(-1.0, min(1.0, signal))

        # Confidence based on data availability
        confidence = (sum(conf_scores) / len(conf_scores) if conf_scores else 0.5) * 0.55

        rotation_bias = (
            "risk_on"  if signal >  0.15 else
            "risk_off" if signal < -0.15 else
            "neutral"
        )

        # Top 3 movers
        sorted_etfs = sorted(
            [(t, abs(etf_data[t]["signal"])) for t in etf_data],
            key=lambda x: x[1],
            reverse=True
        )
        dominant_sectors = [t for t, _ in sorted_etfs[:3]]

        result = {
            "signal":         round(signal, 4),
            "confidence":     round(confidence, 4),
            "rotation_bias":  rotation_bias,
            "risk_on_score":  round(risk_on_score, 4),
            "risk_off_score": round(risk_off_score, 4),
            "net_rotation_score": round(net_rotation_score, 4),
            "btc_dominance":  round(btc_dominance, 2),
            "dominant_sectors": dominant_sectors,
            "etf_data":       etf_data,
            "source":         "sector_rotation",
        }
        with self._lock:
            self._cache = result

        logger.info(
            "SectorRotationAgent: signal=%+.3f | conf=%.2f | bias=%s | net_rot=%+.3f",
            signal, confidence, rotation_bias, net_rotation_score,
        )
        return result

    # ── Data fetchers ─────────────────────────────────────────

    def _fetch_etf_momentum(self) -> dict[str, float]:
        """
        Fetch 5-day price momentum for each sector ETF.
        Tries yfinance first, falls back to Yahoo Finance JSON API.
        """
        result: dict[str, float] = {}
        tickers = [t for t, _, _ in _ALL_TICKERS]

        # Try yfinance first
        try:
            import yfinance as yf
            data = yf.download(
                " ".join(tickers),
                period="10d",
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=True,
            )
            close = data.get("Close", data) if hasattr(data, "get") else data
            for ticker in tickers:
                try:
                    if ticker in close.columns:
                        series = close[ticker].dropna()
                    else:
                        series = close.dropna()
                    if len(series) >= 2:
                        start = float(series.iloc[0])
                        end   = float(series.iloc[-1])
                        if start != 0:
                            result[ticker] = (end - start) / abs(start) * 100.0
                except Exception:
                    pass
            # If we got results, return
            if result:
                return result
        except ImportError:
            logger.debug("yfinance not available, falling back to Yahoo Finance JSON API")
        except Exception as exc:
            logger.debug("SectorRotationAgent: yfinance failed — %s, trying fallback", exc)

        # Fallback: Yahoo Finance JSON API (no library needed)
        import urllib.request, json as _json
        for ticker in tickers:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = _json.loads(resp.read().decode())

                chart = data.get("chart", {})
                result_list = chart.get("result", [])
                if not result_list:
                    continue

                indicators = result_list[0].get("indicators", {})
                quotes = indicators.get("quote", [])
                if not quotes:
                    continue

                closes = quotes[0].get("close", [])
                # Filter out None values
                closes = [c for c in closes if c is not None]

                if len(closes) >= 2:
                    start = float(closes[0])
                    end = float(closes[-1])
                    if start != 0:
                        result[ticker] = (end - start) / abs(start) * 100.0
            except Exception as exc:
                logger.debug("SectorRotationAgent: Fallback fetch for %s failed — %s", ticker, exc)

        return result

    def _fetch_btc_dominance(self) -> dict:
        """Fetch BTC dominance change from CoinGecko global endpoint."""
        import urllib.request, json as _json
        url = "https://api.coingecko.com/api/v3/global"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "NexusTrader/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())

        mkt = data.get("data", {})
        dom = mkt.get("market_cap_percentage", {})
        # CoinGecko global doesn't give 7d change directly;
        # store current and compute change from cache on next poll
        current = float(dom.get("btc", 50.0))
        with self._lock:
            prev = self._cache.get("_btc_dom_prev", current)
        change_7d = current - prev
        with self._lock:
            self._cache["_btc_dom_prev"] = current
        return {"current": current, "change_7d": change_7d}

    # ── Public API ────────────────────────────────────────────

    def get_rotation_signal(self) -> dict:
        with self._lock:
            if self._cache:
                c = dict(self._cache)
                c.pop("_btc_dom_prev", None)
                return c
        return {
            "signal": 0.0, "confidence": 0.0,
            "rotation_bias": "neutral", "stale": True,
        }


# ── Module-level singleton ────────────────────────────────────
sector_rotation_agent: SectorRotationAgent | None = None
