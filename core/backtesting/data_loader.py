# ============================================================
# NEXUS TRADER — Historical Data Loader for Backtesting
#
# Fetches real OHLCV candle data from Bybit (primary) with
# Binance fallback. All data is sourced from real exchanges —
# no synthetic or mocked data is ever returned.
#
# Usage:
#   loader = HistoricalDataLoader()
#   df = loader.fetch_ohlcv("BTC/USDT", "1h",
#                           start_date="2024-01-01",
#                           end_date="2024-12-31",
#                           min_bars=300)
# ============================================================
from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import ccxt
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

logger = logging.getLogger(__name__)

# Timeframe to seconds mapping
TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Default rate limit sleep between API requests
RATE_LIMIT_SLEEP = 0.5

# Fetch timeout per request (seconds)
FETCH_TIMEOUT = 15

# Minimum bars required for warm-up
DEFAULT_MIN_BARS = 300


class InsufficientDataError(Exception):
    """Raised when historical data is insufficient for backtesting.

    Attributes:
        bars_needed: Minimum bars required
        bars_available: Bars actually fetched
        source: Which exchange was attempted ("bybit", "binance", or both)
    """

    def __init__(self, message: str, bars_needed: int, bars_available: int, source: str):
        super().__init__(message)
        self.bars_needed = bars_needed
        self.bars_available = bars_available
        self.source = source


@dataclass
class DataSourceInfo:
    """Metadata about the data source used for a backtest."""

    primary_source: str  # "bybit" or "binance"
    fallback_used: bool  # True if primary failed
    fallback_source: str  # "" or "binance"
    fallback_reason: str  # "" or explanation
    total_bars: int
    date_range_start: str  # ISO format
    date_range_end: str  # ISO format
    gaps_found: int
    duplicates_removed: int
    fetch_duration_s: float


class HistoricalDataLoader:
    """Fetches real historical OHLCV data from exchanges for backtesting.

    Primary source: Active exchange (currently Bybit via exchange_manager)
    Fallback: Binance public API (ccxt.binance())

    CRITICAL: Never returns synthetic, mocked, or fabricated data.
    All candles must come from real exchange history.

    Parameters
    ----------
    use_exchange_manager : bool
        If True (default), attempts to use exchange_manager for primary source.
        If False, forces Bybit via ccxt.
    """

    def __init__(self, use_exchange_manager: bool = True):
        self._use_exchange_manager = use_exchange_manager
        self._lock = threading.Lock()
        self._last_fetch_info: Optional[DataSourceInfo] = None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,  # ISO format "2024-01-01"
        end_date: str,  # ISO format "2024-12-31"
        min_bars: int = DEFAULT_MIN_BARS,
    ) -> pd.DataFrame:
        """Fetch OHLCV data from active exchange with Binance fallback.

        Parameters
        ----------
        symbol : str
            Trading pair (e.g., "BTC/USDT")
        timeframe : str
            Candle interval: "1m", "5m", "15m", "30m", "1h", "4h", "1d"
        start_date : str
            Start date in ISO format (e.g., "2024-01-01")
        end_date : str
            End date in ISO format (e.g., "2024-12-31")
        min_bars : int
            Minimum bars required; raises InsufficientDataError if not met

        Returns
        -------
        pd.DataFrame
            DataFrame with index=timestamp (UTC), columns=[open, high, low, close, volume]

        Raises
        ------
        InsufficientDataError
            If fetched bars are < min_bars after all attempts and validation
        """
        if timeframe not in TF_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}. Supported: {list(TF_SECONDS.keys())}")

        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date)

        if start_ts >= end_ts:
            raise ValueError(f"start_date ({start_date}) must be before end_date ({end_date})")

        fetch_start_time = time.time()

        # Attempt primary source (exchange_manager → Bybit)
        primary_candles = []
        primary_error = None

        try:
            primary_candles = self._fetch_from_exchange_manager(
                symbol, timeframe, start_ts, end_ts
            )
            logger.info(
                "Fetched %d candles from exchange_manager (Bybit) for %s/%s",
                len(primary_candles),
                symbol,
                timeframe,
            )
        except Exception as e:
            primary_error = str(e)
            logger.warning("Primary source (exchange_manager) failed: %s", primary_error)

        # If primary succeeded and has enough bars, use it
        if len(primary_candles) >= min_bars:
            df = self._candles_to_dataframe(primary_candles, timeframe)
            valid, issues = self._validate_candles(primary_candles, timeframe, min_bars)
            if valid:
                self._record_fetch_info(
                    primary_source="bybit",
                    fallback_used=False,
                    candles=primary_candles,
                    fetch_duration_s=time.time() - fetch_start_time,
                )
                logger.info("OHLCV fetch successful: %d bars from Bybit (%s/%s)",
                           len(primary_candles), symbol, timeframe)
                return df
            else:
                logger.warning("Primary validation failed: %s", "; ".join(issues))

        # Fallback to Binance
        logger.info("Attempting fallback to Binance for %s/%s", symbol, timeframe)
        fallback_candles = []
        fallback_error = None

        try:
            fallback_candles = self._fetch_from_binance(symbol, timeframe, start_ts, end_ts)
            logger.info(
                "Fetched %d candles from Binance fallback for %s/%s",
                len(fallback_candles),
                symbol,
                timeframe,
            )
        except Exception as e:
            fallback_error = str(e)
            logger.error("Fallback source (Binance) also failed: %s", fallback_error)

        # Decide which source to use (prefer primary if both returned data)
        candles_to_use = primary_candles if len(primary_candles) >= len(fallback_candles) else fallback_candles
        source_used = "bybit" if len(primary_candles) >= len(fallback_candles) else "binance"
        fallback_reason = (
            primary_error if len(primary_candles) < len(fallback_candles) else ""
        )

        # Final validation
        valid, issues = self._validate_candles(candles_to_use, timeframe, min_bars)
        if not valid or len(candles_to_use) < min_bars:
            error_msg = (
                f"Insufficient data for {symbol}/{timeframe}: "
                f"needed {min_bars}, got {len(candles_to_use)}. "
                f"Issues: {'; '.join(issues) if issues else 'None'}"
            )
            logger.error(error_msg)
            raise InsufficientDataError(
                error_msg,
                bars_needed=min_bars,
                bars_available=len(candles_to_use),
                source=source_used,
            )

        # Success
        self._record_fetch_info(
            primary_source="bybit",
            fallback_used=source_used != "bybit",
            fallback_reason=fallback_reason,
            candles=candles_to_use,
            fetch_duration_s=time.time() - fetch_start_time,
        )

        df = self._candles_to_dataframe(candles_to_use, timeframe)
        logger.info(
            "OHLCV fetch successful: %d bars from %s (fallback=%s, duration=%.2fs)",
            len(candles_to_use),
            source_used,
            source_used != "bybit",
            time.time() - fetch_start_time,
        )
        return df

    def get_available_range(self, symbol: str, timeframe: str) -> tuple[datetime, datetime]:
        """Return the earliest and latest available data for a symbol/TF.

        Attempts to query both primary (exchange_manager) and Binance,
        returning the widest range available.

        Returns
        -------
        tuple[datetime, datetime]
            (earliest, latest) timestamps in UTC
        """
        ranges = []

        # Try primary source
        try:
            ex = self._get_primary_exchange()
            if ex:
                # Fetch 1 candle to get the earliest timestamp
                data = ex.fetch_ohlcv(symbol, timeframe, limit=1)
                if data:
                    earliest = datetime.fromtimestamp(data[0][0] / 1000, tz=timezone.utc)
                    # Fetch from a far future date to get the latest
                    far_future = int((datetime.now(timezone.utc).timestamp() + 86400 * 365) * 1000)
                    data = ex.fetch_ohlcv(
                        symbol, timeframe, since=far_future - 86400 * 30 * 1000, limit=1000
                    )
                    if data:
                        latest = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc)
                        ranges.append((earliest, latest))
        except Exception as e:
            logger.debug("Could not determine range from primary source: %s", e)

        # Try Binance
        try:
            binance = ccxt.binance()
            data = binance.fetch_ohlcv(symbol, timeframe, limit=1)
            if data:
                earliest = datetime.fromtimestamp(data[0][0] / 1000, tz=timezone.utc)
                far_future = int((datetime.now(timezone.utc).timestamp() + 86400 * 365) * 1000)
                data = binance.fetch_ohlcv(
                    symbol, timeframe, since=far_future - 86400 * 30 * 1000, limit=1000
                )
                if data:
                    latest = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc)
                    ranges.append((earliest, latest))
        except Exception as e:
            logger.debug("Could not determine range from Binance: %s", e)

        if not ranges:
            # Fallback: return a wide default range
            logger.warning("Could not determine available range for %s/%s", symbol, timeframe)
            return (
                datetime(2020, 1, 1, tzinfo=timezone.utc),
                datetime.now(timezone.utc),
            )

        # Return the widest range
        earliest = min(r[0] for r in ranges)
        latest = max(r[1] for r in ranges)
        logger.info("Available range for %s/%s: %s to %s", symbol, timeframe, earliest, latest)
        return earliest, latest

    def get_last_fetch_info(self) -> Optional[DataSourceInfo]:
        """Return metadata from the most recent fetch_ohlcv() call."""
        with self._lock:
            return self._last_fetch_info

    # ── Private Methods ────────────────────────────────────────────────

    def _get_primary_exchange(self) -> Optional[ccxt.Exchange]:
        """Return the active exchange from exchange_manager, or None."""
        if not self._use_exchange_manager:
            return None

        try:
            from core.market_data.exchange_manager import ExchangeManager

            em = ExchangeManager()
            return em.get_exchange()
        except Exception as e:
            logger.debug("Could not get exchange_manager: %s", e)
            return None

    def _fetch_from_exchange_manager(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list:
        """Fetch from the active exchange (Bybit via exchange_manager)."""
        ex = self._get_primary_exchange()
        if not ex:
            raise RuntimeError("exchange_manager not available; cannot fetch from primary source")

        return self._fetch_from_exchange(ex, symbol, timeframe, start_ms, end_ms)

    def _fetch_from_binance(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list:
        """Fetch from Binance public API (no API keys required)."""
        binance = ccxt.binance()
        return self._fetch_from_exchange(binance, symbol, timeframe, start_ms, end_ms)

    def _fetch_from_exchange(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list:
        """Internal pagination loop to fetch all candles in date range.

        Fetches in batches of ~200–1000 candles, respecting rate limits.
        Timeout per request: 15 seconds via ThreadPoolExecutor.

        Parameters
        ----------
        exchange : ccxt.Exchange
            CCXT exchange instance (Bybit, Binance, etc.)
        symbol : str
            Trading pair
        timeframe : str
            Candle interval
        start_ms : int
            Start timestamp in milliseconds
        end_ms : int
            End timestamp in milliseconds

        Returns
        -------
        list
            List of [timestamp_ms, open, high, low, close, volume] candles
        """
        all_candles = []
        since = start_ms

        # Estimate batch size: most exchanges return 200–1000 candles per request
        tf_seconds = TF_SECONDS.get(timeframe, 3600)
        batch_size = min(1000, max(100, int(86400 / tf_seconds)))  # One day's worth
        batch_limit = min(1000, batch_size + 100)  # CCXT limit cap

        exchange_name = exchange.id if hasattr(exchange, "id") else "unknown"
        logger.debug(
            "Starting fetch loop: %s, symbol=%s, tf=%s, batch_limit=%d",
            exchange_name,
            symbol,
            timeframe,
            batch_limit,
        )

        while since < end_ms:
            logger.debug(
                "Fetching %s batch: since=%s (epoch ms)",
                exchange_name,
                since,
            )

            try:
                # Use ThreadPoolExecutor with timeout to prevent hangs
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        lambda: exchange.fetch_ohlcv(
                            symbol, timeframe, since=since, limit=batch_limit
                        )
                    )
                    try:
                        batch = future.result(timeout=FETCH_TIMEOUT)
                    except FuturesTimeoutError:
                        logger.warning(
                            "Fetch timeout (%.1fs) from %s for %s/%s at since=%d",
                            FETCH_TIMEOUT,
                            exchange_name,
                            symbol,
                            timeframe,
                            since,
                        )
                        raise TimeoutError(
                            f"fetch_ohlcv timed out after {FETCH_TIMEOUT}s"
                        )

            except Exception as e:
                logger.error(
                    "Fetch failed from %s for %s/%s: %s (fetched %d candles so far)",
                    exchange_name,
                    symbol,
                    timeframe,
                    e,
                    len(all_candles),
                )
                raise

            if not batch:
                logger.debug("No more data from %s (empty batch)", exchange_name)
                break

            all_candles.extend(batch)
            last_ts = batch[-1][0]

            # Advance since for next batch
            since = last_ts + (tf_seconds * 1000)

            logger.debug(
                "Batch complete: %d candles, last_ts=%s, advancing to %s",
                len(batch),
                last_ts,
                since,
            )

            # Rate limit: sleep between requests to avoid hitting limits
            time.sleep(RATE_LIMIT_SLEEP)

            # Stop if we've reached the end date
            if last_ts >= end_ms:
                logger.debug("Reached end_ms; stopping fetch loop")
                break

        logger.info(
            "Fetch complete from %s: %d total candles for %s/%s",
            exchange_name,
            len(all_candles),
            symbol,
            timeframe,
        )
        return all_candles

    def _validate_candles(
        self, candles: list, timeframe: str, min_bars: int
    ) -> tuple[bool, list[str]]:
        """Validate candle data quality.

        Parameters
        ----------
        candles : list
            List of [ts_ms, o, h, l, c, v] from exchange
        timeframe : str
            Candle interval
        min_bars : int
            Minimum required bars

        Returns
        -------
        tuple[bool, list[str]]
            (is_valid, list_of_issues)
        """
        issues = []

        if len(candles) < min_bars:
            issues.append(
                f"Insufficient bars: {len(candles)} < {min_bars}"
            )
            return False, issues

        if len(candles) == 0:
            issues.append("No candles fetched")
            return False, issues

        # Check for duplicates and out-of-order timestamps
        timestamps = [c[0] for c in candles]
        if len(timestamps) != len(set(timestamps)):
            duplicates = len(timestamps) - len(set(timestamps))
            issues.append(f"Found {duplicates} duplicate timestamps")

        if timestamps != sorted(timestamps):
            issues.append("Candles not in chronological order")
            return False, issues

        # Check for NaN or invalid OHLCV values
        tf_seconds = TF_SECONDS.get(timeframe, 3600)
        expected_interval_ms = tf_seconds * 1000
        max_gap_ms = expected_interval_ms * 3  # Allow up to 3x gap (e.g., weekend)

        gap_count = 0
        for i in range(1, len(candles)):
            gap = candles[i][0] - candles[i - 1][0]
            if gap > max_gap_ms:
                gap_count += 1

        if gap_count > 0:
            issues.append(f"Found {gap_count} gaps larger than {max_gap_ms}ms")

        # Check for NaN values
        for i, c in enumerate(candles):
            ts, o, h, l, c_val, v = c
            if any(isinstance(x, float) and np.isnan(x) for x in [o, h, l, c_val, v]):
                issues.append(f"Candle {i} contains NaN values")
                return False, issues
            if any(x <= 0 for x in [o, h, l, c_val]):
                issues.append(f"Candle {i} contains non-positive OHLC values")
                return False, issues
            if v < 0:
                issues.append(f"Candle {i} has negative volume")
                return False, issues

        # All checks passed
        return len(issues) == 0, issues

    def _candles_to_dataframe(self, candles: list, timeframe: str) -> pd.DataFrame:
        """Convert raw candle list to DataFrame with proper types and index.

        Parameters
        ----------
        candles : list
            List of [ts_ms, o, h, l, c, v]
        timeframe : str
            Candle interval (used for gap-filling if needed)

        Returns
        -------
        pd.DataFrame
            Index: timestamp (UTC), Columns: [open, high, low, close, volume]
        """
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        timestamps = []
        data = {"open": [], "high": [], "low": [], "close": [], "volume": []}

        for ts_ms, o, h, l, c, v in candles:
            timestamps.append(datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc))
            data["open"].append(float(o))
            data["high"].append(float(h))
            data["low"].append(float(l))
            data["close"].append(float(c))
            data["volume"].append(float(v))

        df = pd.DataFrame(data, index=pd.DatetimeIndex(timestamps, name="timestamp"))
        df.index = df.index.tz_convert("UTC")

        logger.debug(
            "Converted %d candles to DataFrame: %s to %s",
            len(candles),
            df.index[0],
            df.index[-1],
        )
        return df

    def _parse_iso_date(self, date_str: str) -> int:
        """Parse ISO date string to milliseconds since epoch.

        Parameters
        ----------
        date_str : str
            ISO format date (e.g., "2024-01-01")

        Returns
        -------
        int
            Milliseconds since epoch (UTC)
        """
        try:
            if len(date_str) == 10:  # "2024-01-01"
                dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            else:  # Full ISO format with time
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError as e:
            raise ValueError(f"Invalid date format: {date_str}. Use ISO format (YYYY-MM-DD)") from e

    def _record_fetch_info(
        self,
        primary_source: str,
        fallback_used: bool,
        candles: list,
        fetch_duration_s: float,
        fallback_reason: str = "",
    ) -> None:
        """Record metadata from the fetch for auditing."""
        if not candles:
            return

        timestamps = [datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc) for c in candles]
        start_ts = timestamps[0].isoformat()
        end_ts = timestamps[-1].isoformat()

        # Count gaps and duplicates
        gaps = sum(
            1
            for i in range(1, len(candles))
            if candles[i][0] - candles[i - 1][0] > TF_SECONDS.get("4h", 14400) * 1000
        )

        ts_list = [c[0] for c in candles]
        duplicates = len(ts_list) - len(set(ts_list))

        info = DataSourceInfo(
            primary_source=primary_source,
            fallback_used=fallback_used,
            fallback_source="binance" if fallback_used else "",
            fallback_reason=fallback_reason,
            total_bars=len(candles),
            date_range_start=start_ts,
            date_range_end=end_ts,
            gaps_found=gaps,
            duplicates_removed=duplicates,
            fetch_duration_s=fetch_duration_s,
        )

        with self._lock:
            self._last_fetch_info = info

        logger.debug(
            "Fetch info recorded: bars=%d, source=%s, fallback=%s, gaps=%d, "
            "duplicates=%d, duration=%.2fs",
            info.total_bars,
            info.primary_source,
            info.fallback_used,
            info.gaps_found,
            info.duplicates_removed,
            info.fetch_duration_s,
        )


# Convenience function for singleton-like behavior
_loader_instance: Optional[HistoricalDataLoader] = None
_loader_lock = threading.Lock()


def get_historical_data_loader() -> HistoricalDataLoader:
    """Return a singleton HistoricalDataLoader instance."""
    global _loader_instance
    if _loader_instance is None:
        with _loader_lock:
            if _loader_instance is None:
                _loader_instance = HistoricalDataLoader()
    return _loader_instance
