# ============================================================
# NEXUS TRADER — Universe Filter
#
# Applies liquidity, volatility, spread, and volume filters
# to a list of symbols before scanning.
# Uses live ticker data from the exchange manager.
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


class UniverseFilter:
    """
    Filters a symbol list to only those meeting execution-quality criteria.

    Parameters
    ----------
    min_volume_usdt    : 24h volume minimum in USDT (default 1 000 000)
    max_spread_pct     : max bid-ask spread as % of mid price (default 0.3%)
    min_atr_pct        : min ATR as % of price — avoids dead markets (default 0.2%)
    max_atr_pct        : max ATR as % of price — avoids uncontrollable vol (default 15%)
    top_n              : after filtering, keep only top N by volume (0 = keep all)
    """

    def __init__(
        self,
        min_volume_usdt: float = 1_000_000.0,
        max_spread_pct:  float = 0.3,
        min_atr_pct:     float = 0.2,
        max_atr_pct:     float = 15.0,
        top_n:           int   = 0,
    ):
        self.min_volume_usdt = min_volume_usdt
        self.max_spread_pct  = max_spread_pct
        self.min_atr_pct     = min_atr_pct
        self.max_atr_pct     = max_atr_pct
        self.top_n           = top_n

    def apply(
        self,
        symbols: list[str],
        tickers: dict,           # from ccxt.fetch_tickers() — symbol → ticker dict
        feature_dfs: Optional[dict[str, pd.DataFrame]] = None,  # symbol → indicator df
    ) -> list[str]:
        """
        Filter symbols by liquidity, spread, and volatility.
        Returns qualifying symbols sorted by 24h volume descending.

        Parameters
        ----------
        symbols     : candidate symbols
        tickers     : dict of ticker data {symbol: {bid, ask, quoteVolume, ...}}
        feature_dfs : optional indicator DataFrames for ATR filter
        """
        qualified: list[tuple[str, float]] = []  # (symbol, volume)

        for sym in symbols:
            ticker = tickers.get(sym)
            if not ticker:
                logger.debug("Filter: no ticker for %s — skipped", sym)
                continue

            # ── Volume filter ──────────────────────────────────
            volume = ticker.get("quoteVolume") or ticker.get("baseVolume", 0.0)
            if volume is None:
                volume = 0.0
            volume = float(volume)
            if volume < self.min_volume_usdt:
                logger.debug("Filter: %s volume %.0f < min %.0f", sym, volume, self.min_volume_usdt)
                continue

            # ── Spread filter ──────────────────────────────────
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if bid and ask and float(bid) > 0:
                spread_pct = (float(ask) - float(bid)) / float(bid) * 100.0
                if spread_pct > self.max_spread_pct:
                    logger.debug("Filter: %s spread %.3f%% > max %.3f%%", sym, spread_pct, self.max_spread_pct)
                    continue

            # ── ATR/volatility filter ──────────────────────────
            if feature_dfs and sym in feature_dfs:
                df = feature_dfs[sym]
                if "atr_14" in df.columns and len(df) > 0:
                    last_close = float(df["close"].iloc[-1])
                    atr14      = float(df["atr_14"].iloc[-1])
                    if last_close > 0:
                        atr_pct = atr14 / last_close * 100.0
                        if atr_pct < self.min_atr_pct:
                            logger.debug("Filter: %s ATR%%=%.3f < min", sym, atr_pct)
                            continue
                        if atr_pct > self.max_atr_pct:
                            logger.debug("Filter: %s ATR%%=%.3f > max", sym, atr_pct)
                            continue

            qualified.append((sym, volume))

        # Sort by volume descending
        qualified.sort(key=lambda x: x[1], reverse=True)

        # Apply top-N cap
        if self.top_n > 0:
            qualified = qualified[: self.top_n]

        result = [sym for sym, _ in qualified]
        logger.info("UniverseFilter: %d/%d symbols passed", len(result), len(symbols))
        return result
