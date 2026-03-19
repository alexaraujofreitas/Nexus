"""
Closed-Candle Guard — prevents forming-candle contamination.

Exchange OHLCV endpoints typically return N-1 fully closed candles plus
1 still-forming candle as the last element.  If that forming bar enters the
signal pipeline its unstable close/high/low/volume will create unreliable
indicator values and phantom signals.

This module provides a single reusable function that inspects the last bar
of an OHLCV result and drops it when it belongs to the currently-forming
candle period.

Usage:
    from core.scanning.closed_candle_guard import enforce_closed_candles
    raw = exchange.fetch_ohlcv(symbol, "1h", limit=300)
    raw, dropped = enforce_closed_candles(raw, "1h")
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Timeframe → duration in seconds ──────────────────────────────────────
_TF_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "1d": 86400, "1w": 604800,
}


def enforce_closed_candles(
    ohlcv: list[list],
    timeframe: str,
    *,
    now_ms: Optional[int] = None,
    log_symbol: str = "",
) -> tuple[list[list], bool]:
    """Drop the last bar if it is the currently-forming candle.

    Parameters
    ----------
    ohlcv : list[list]
        Raw OHLCV rows from the exchange.  Each row is
        ``[timestamp_ms, open, high, low, close, volume]``.
    timeframe : str
        Candle timeframe string (e.g. ``"1h"``, ``"15m"``).
    now_ms : int, optional
        Current UTC epoch in milliseconds.  Defaults to ``time.time() * 1000``.
    log_symbol : str, optional
        Symbol name used in log messages for tracing.

    Returns
    -------
    (cleaned_ohlcv, was_dropped) : tuple[list[list], bool]
        ``cleaned_ohlcv`` has the forming bar removed when detected.
        ``was_dropped`` is ``True`` when a bar was removed.
    """
    if not ohlcv:
        return ohlcv, False

    tf_s = _TF_SECONDS.get(timeframe)
    if tf_s is None:
        logger.warning(
            "ClosedCandleGuard: unknown timeframe '%s' — cannot enforce; "
            "passing data through unchanged",
            timeframe,
        )
        return ohlcv, False

    tf_ms = tf_s * 1000
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    last_bar = ohlcv[-1]
    bar_open_ms = int(last_bar[0])

    # A closed candle's open timestamp + timeframe duration should be
    # ≤ ``now_ms``.  If ``bar_open_ms + tf_ms > now_ms`` the candle has
    # not closed yet → it is the forming bar.
    bar_close_ms = bar_open_ms + tf_ms

    if bar_close_ms > now_ms:
        # Forming candle detected — drop it
        pct_formed = (now_ms - bar_open_ms) / tf_ms * 100
        logger.info(
            "ClosedCandleGuard: %s [%s] dropped forming candle "
            "(bar_open=%d, close_due=%d, now=%d, %.0f%% formed, bars_before=%d)",
            log_symbol, timeframe, bar_open_ms, bar_close_ms, now_ms,
            pct_formed, len(ohlcv),
        )
        return ohlcv[:-1], True

    # Last bar is closed — pass through unchanged
    logger.debug(
        "ClosedCandleGuard: %s [%s] last bar is closed "
        "(bar_open=%d, bar_close=%d, now=%d, bars=%d)",
        log_symbol, timeframe, bar_open_ms, bar_close_ms, now_ms, len(ohlcv),
    )
    return ohlcv, False
