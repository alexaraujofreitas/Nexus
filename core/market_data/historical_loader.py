# ============================================================
# NEXUS TRADER — Historical OHLCV Data Loader
# Bulk downloads candle history from exchange to SQLite
# ============================================================

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from PySide6.QtCore import QThread, Signal

from core.market_data.exchange_manager import exchange_manager
from core.database.engine import get_session
from core.database.models import OHLCV, Asset

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


class HistoricalLoaderWorker(QThread):
    """
    Background worker that downloads full OHLCV history for a symbol/timeframe.
    Emits progress signals for the UI progress bar.

    Parameters
    ----------
    since_ms : int, optional
        If provided, download only candles newer than this Unix-ms timestamp
        (gap-fill mode).  Takes precedence over ``days_back``.
    """
    progress    = Signal(int, int, str)   # current, total, message
    finished    = Signal(str, int)        # symbol, rows_saved
    error       = Signal(str, str)        # symbol, error_message

    def __init__(self, symbol: str, timeframe: str,
                 days_back: int = 365, asset_id: Optional[int] = None,
                 since_ms: Optional[int] = None):
        super().__init__()
        self.symbol    = symbol
        self.timeframe = timeframe
        self.days_back = days_back
        self.asset_id  = asset_id
        self.since_ms  = since_ms      # explicit start timestamp (gap-fill)
        self._stop     = False

    def stop(self):
        self._stop = True

    def run(self):
        ex = exchange_manager.get_exchange()
        if not ex:
            self.error.emit(self.symbol, "No active exchange")
            return

        if self.asset_id is None:
            self.error.emit(self.symbol, "Asset not found in database")
            return

        try:
            # Determine start timestamp (gap-fill since_ms takes priority)
            if self.since_ms is not None:
                since_ms = self.since_ms
                # Estimate gap size for progress bar
                gap_ms = int(datetime.utcnow().timestamp() * 1000) - since_ms
                tf_minutes = _timeframe_to_minutes(self.timeframe)
                total_candles = max(1, int(gap_ms / (tf_minutes * 60_000)))
            else:
                since_dt = datetime.utcnow() - timedelta(days=self.days_back)
                since_ms  = int(since_dt.timestamp() * 1000)
                # Estimate total candles for progress bar
                tf_minutes = _timeframe_to_minutes(self.timeframe)
                total_candles = int((self.days_back * 24 * 60) / tf_minutes)
            self.progress.emit(0, total_candles, f"Starting download: {self.symbol} {self.timeframe}")

            all_candles = []
            current_since = since_ms

            while not self._stop:
                batch = ex.fetch_ohlcv(
                    self.symbol, self.timeframe,
                    since=current_since, limit=BATCH_SIZE
                )
                if not batch:
                    break

                all_candles.extend(batch)
                self.progress.emit(
                    len(all_candles), total_candles,
                    f"Downloaded {len(all_candles):,} candles..."
                )

                # If we got fewer than batch size, we've reached the end
                if len(batch) < BATCH_SIZE:
                    break

                # Advance the cursor past the last candle
                current_since = batch[-1][0] + 1

                # Rate limit respect
                self.msleep(ex.rateLimit)

            if self._stop:
                self.error.emit(self.symbol, "Download cancelled")
                return

            if not all_candles:
                self.error.emit(self.symbol, "No candles returned from exchange")
                return

            # Save to database (upsert)
            self.progress.emit(len(all_candles), total_candles, "Saving to database...")
            rows_saved = self._save_to_db(all_candles)
            self.finished.emit(self.symbol, rows_saved)

        except Exception as e:
            logger.error("Historical loader error for %s: %s", self.symbol, e, exc_info=True)
            self.error.emit(self.symbol, str(e))

    def _save_to_db(self, candles: list) -> int:
        """Insert candles into SQLite, skipping duplicates."""
        saved = 0
        batch_size = 500

        with get_session() as session:
            # Get existing timestamps to avoid duplicates
            existing = set(
                r[0] for r in session.query(OHLCV.timestamp)
                .filter_by(asset_id=self.asset_id, timeframe=self.timeframe)
                .all()
            )

            new_rows = []
            for c in candles:
                ts = datetime.utcfromtimestamp(c[0] / 1000)
                if ts not in existing:
                    new_rows.append(OHLCV(
                        asset_id=self.asset_id,
                        timeframe=self.timeframe,
                        timestamp=ts,
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]),
                    ))

            # Bulk insert in batches
            for i in range(0, len(new_rows), batch_size):
                session.bulk_save_objects(new_rows[i:i + batch_size])
                session.flush()
            saved = len(new_rows)

        return saved


def load_ohlcv_from_db(
    asset_id:   int,
    timeframe:  str,
    limit:      int = 2000,
    start_date: Optional[datetime] = None,
    end_date:   Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Load OHLCV data from SQLite as a pandas DataFrame.

    Parameters
    ----------
    asset_id   : DB asset ID
    timeframe  : e.g. "1h"
    limit      : max rows returned when no date range is given (default 2000)
    start_date : inclusive lower bound (UTC datetime); if given, ``limit`` is ignored
    end_date   : inclusive upper bound (UTC datetime); defaults to now when start given

    Returns columns: open, high, low, close, volume  (indexed by timestamp)
    """
    with get_session() as session:
        q = (
            session.query(OHLCV)
            .filter_by(asset_id=asset_id, timeframe=timeframe)
        )
        if start_date is not None:
            q = q.filter(OHLCV.timestamp >= start_date)
        if end_date is not None:
            q = q.filter(OHLCV.timestamp <= end_date)

        if start_date is not None:
            # Date-range query: fetch all matching rows in order
            rows = q.order_by(OHLCV.timestamp.asc()).all()
        else:
            # No date range: fetch the newest `limit` rows then reverse
            rows = q.order_by(OHLCV.timestamp.desc()).limit(limit).all()
            rows = list(reversed(rows))
        data = [
            {
                "timestamp": r.timestamp,
                "open":      r.open,
                "high":      r.high,
                "low":       r.low,
                "close":     r.close,
                "volume":    r.volume,
            }
            for r in rows
        ]

    if not data:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.set_index("timestamp", inplace=True)
    return df


class HistoricalDataWorker(QThread):
    """
    Downloads OHLCV history from the currently active exchange for backtesting.

    The worker automatically:
    - Uses whatever exchange the user has configured (Bybit, Binance, OKX, etc.)
    - Reuses any existing Asset entry for the symbol (regardless of exchange)
    - Creates a new Asset under the active exchange if none exists
    - Saves downloaded candles to SQLite, skipping duplicates

    Parameters
    ----------
    symbol     : CCXT-format symbol, e.g. "BTC/USDT"
    timeframe  : e.g. "1h", "4h", "1d"
    start_date : UTC datetime for the start of the download window
    end_date   : UTC datetime for the end of the download window
    """
    progress = Signal(int, int, str)   # current, total, message
    finished = Signal(str, int)        # symbol, rows_saved
    error    = Signal(str, str)        # symbol, error_message

    # Maximum candles per API request
    _CANDLES_PER_REQUEST = 1000

    def __init__(self, symbol: str, timeframe: str,
                 start_date: datetime, end_date: datetime):
        super().__init__()
        self.symbol     = symbol
        self.timeframe  = timeframe
        self.start_date = start_date
        self.end_date   = end_date
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        ex = exchange_manager.get_exchange()
        if not ex:
            self.error.emit(self.symbol,
                            "No active exchange — connect an exchange in Exchange Management first.")
            return

        ex_name = getattr(ex, "name", None) or getattr(ex, "id", "Exchange")

        if self.symbol not in (ex.markets or {}):
            self.error.emit(self.symbol,
                            f"'{self.symbol}' is not available on {ex_name}. "
                            "Check the symbol format, e.g. BTC/USDT.")
            return

        asset_id = self._ensure_asset()
        if not asset_id:
            self.error.emit(self.symbol, "Could not create asset record in database")
            return

        since_ms = int(self.start_date.timestamp() * 1000)
        end_ms   = int(self.end_date.timestamp()   * 1000)
        tf_min   = _timeframe_to_minutes(self.timeframe)
        total_candles = max(1, int((end_ms - since_ms) / (tf_min * 60_000)))

        self.progress.emit(0, total_candles,
                           f"Connecting to {ex_name}: {self.symbol} {self.timeframe}…")

        all_candles   = []
        current_since = since_ms

        while not self._stop:
            batch = ex.fetch_ohlcv(
                self.symbol, self.timeframe,
                since=current_since, limit=self._CANDLES_PER_REQUEST
            )
            if not batch:
                break

            # Trim candles past the requested end date
            batch = [c for c in batch if c[0] <= end_ms]
            all_candles.extend(batch)

            self.progress.emit(
                len(all_candles), total_candles,
                f"Downloading from {ex_name}: {len(all_candles):,} candles…"
            )

            # Done when batch was short (no more data) or we reached end_ms
            if len(batch) < self._CANDLES_PER_REQUEST or (all_candles and all_candles[-1][0] >= end_ms):
                break

            current_since = all_candles[-1][0] + 1
            self.msleep(ex.rateLimit)

        if self._stop:
            self.error.emit(self.symbol, "Download cancelled")
            return

        if not all_candles:
            self.error.emit(self.symbol, f"No candles returned from {ex_name} for the selected range")
            return

        self.progress.emit(len(all_candles), total_candles, "Saving to database…")
        rows_saved = self._save_to_db(all_candles, asset_id)
        self.finished.emit(self.symbol, rows_saved)

    def _ensure_asset(self) -> Optional[int]:
        """
        Return the asset_id to use for storing data.

        Priority:
        1. Any existing Asset with this symbol (re-use — keeps data in one place)
        2. Create a new Asset under the currently active exchange DB record
        """
        from core.database.models import Exchange, Asset
        try:
            with get_session() as session:
                # Re-use any existing asset (from any exchange) if available
                existing = session.query(Asset).filter_by(symbol=self.symbol).first()
                if existing:
                    return existing.id

                # Use the active exchange DB record; fall back to a generic placeholder
                exch = session.query(Exchange).filter_by(is_active=True).first()
                if not exch:
                    ex = exchange_manager.get_exchange()
                    ex_id   = getattr(ex, "id",   "unknown") if ex else "unknown"
                    ex_name = getattr(ex, "name", ex_id)     if ex else "Historical Data"
                    exch = session.query(Exchange).filter_by(name=f"{ex_name} (Historical)").first()
                    if not exch:
                        exch = Exchange(
                            name=f"{ex_name} (Historical)",
                            exchange_id=ex_id,
                            sandbox_mode=False,
                            is_active=False,
                        )
                        session.add(exch)
                        session.flush()

                parts = self.symbol.split("/")
                base  = parts[0] if parts else self.symbol
                quote = parts[1] if len(parts) > 1 else "USDT"

                asset = Asset(
                    exchange_id   = exch.id,
                    symbol        = self.symbol,
                    base_currency = base,
                    quote_currency= quote,
                    is_active     = True,
                )
                session.add(asset)
                session.flush()
                return asset.id
        except Exception as e:
            logger.error("HistoricalDataWorker._ensure_asset: %s", e, exc_info=True)
            return None

    def _save_to_db(self, candles: list, asset_id: int) -> int:
        """Insert candles into SQLite, skipping duplicates."""
        saved      = 0
        batch_size = 500

        with get_session() as session:
            existing = set(
                r[0] for r in session.query(OHLCV.timestamp)
                .filter_by(asset_id=asset_id, timeframe=self.timeframe)
                .all()
            )
            new_rows = []
            for c in candles:
                ts = datetime.utcfromtimestamp(c[0] / 1000)
                if ts not in existing:
                    new_rows.append(OHLCV(
                        asset_id  = asset_id,
                        timeframe = self.timeframe,
                        timestamp = ts,
                        open      = float(c[1]),
                        high      = float(c[2]),
                        low       = float(c[3]),
                        close     = float(c[4]),
                        volume    = float(c[5]),
                    ))

            for i in range(0, len(new_rows), batch_size):
                session.bulk_save_objects(new_rows[i:i + batch_size])
                session.flush()
            saved = len(new_rows)

        logger.info("HistoricalDataWorker: saved %d candles for %s %s",
                    saved, self.symbol, self.timeframe)
        return saved


# Backward-compatible alias
BinanceHistoricalWorker = HistoricalDataWorker


class MultiTFHistoricalWorker(QThread):
    """
    Downloads OHLCV history from the active exchange for **multiple timeframes** sequentially.

    Wraps ``HistoricalDataWorker`` logic for each TF and emits combined
    progress so the UI can show a single progress bar covering all fetches.

    Signals
    -------
    progress(current, total, message)
    finished(symbol, {tf: rows_saved})
    error(symbol, message)
    """
    progress = Signal(int, int, str)      # current, total, message
    finished = Signal(str, dict)          # symbol, {tf: rows_saved}
    error    = Signal(str, str)           # symbol, error_message

    def __init__(self, symbol: str, timeframes: list,
                 start_date: datetime, end_date: datetime):
        super().__init__()
        self.symbol      = symbol
        self.timeframes  = list(timeframes)   # e.g. ["1m", "5m", "15m", "1h"]
        self.start_date  = start_date
        self.end_date    = end_date
        self._stop       = False

    def stop(self):
        self._stop = True

    def run(self):
        ex = exchange_manager.get_exchange()
        if not ex:
            self.error.emit(self.symbol,
                            "No active exchange — connect an exchange in Exchange Management first.")
            return

        ex_name = getattr(ex, "name", None) or getattr(ex, "id", "Exchange")

        if self.symbol not in (ex.markets or {}):
            self.error.emit(
                self.symbol,
                f"'{self.symbol}' is not available on {ex_name}. "
                "Check the symbol format, e.g. BTC/USDT."
            )
            return

        # Resolve asset_id once (shared across all TFs for the same symbol)
        asset_id = self._ensure_asset()
        if not asset_id:
            self.error.emit(self.symbol, "Could not create asset record in database")
            return

        since_ms = int(self.start_date.timestamp() * 1000)
        end_ms   = int(self.end_date.timestamp()   * 1000)

        # Pre-compute total candle estimate (sum across all TFs) for progress bar
        total_all = sum(
            max(1, int((end_ms - since_ms) / (_timeframe_to_minutes(tf) * 60_000)))
            for tf in self.timeframes
        )

        downloaded_so_far = 0
        results: dict = {}

        for tf_idx, tf in enumerate(self.timeframes):
            if self._stop:
                self.error.emit(self.symbol, "Download cancelled")
                return

            tf_min = _timeframe_to_minutes(tf)
            tf_total = max(1, int((end_ms - since_ms) / (tf_min * 60_000)))
            all_candles: list = []
            current_since = since_ms

            self.progress.emit(
                downloaded_so_far, total_all,
                f"Fetching {tf} data ({tf_idx + 1}/{len(self.timeframes)})…"
            )

            while not self._stop:
                try:
                    batch = ex.fetch_ohlcv(
                        self.symbol, tf,
                        since=current_since, limit=1000
                    )
                except Exception as fetch_err:
                    logger.warning("MultiTFHistoricalWorker: fetch error for %s %s: %s",
                                   self.symbol, tf, fetch_err)
                    break

                if not batch:
                    break

                batch = [c for c in batch if c[0] <= end_ms]
                all_candles.extend(batch)

                self.progress.emit(
                    downloaded_so_far + len(all_candles), total_all,
                    f"Fetching {tf}: {len(all_candles):,} candles…"
                )

                if len(batch) < 1000 or (all_candles and all_candles[-1][0] >= end_ms):
                    break

                current_since = all_candles[-1][0] + 1
                self.msleep(ex.rateLimit)

            if all_candles:
                self.progress.emit(
                    downloaded_so_far + len(all_candles), total_all,
                    f"Saving {tf} to database…"
                )
                rows_saved = self._save_to_db(all_candles, asset_id, tf)
                results[tf] = rows_saved
            else:
                results[tf] = 0
                logger.warning("MultiTFHistoricalWorker: no candles for %s %s", self.symbol, tf)

            downloaded_so_far += tf_total

        self.finished.emit(self.symbol, results)

    def _ensure_asset(self) -> Optional[int]:
        from core.database.models import Exchange, Asset
        try:
            with get_session() as session:
                existing = session.query(Asset).filter_by(symbol=self.symbol).first()
                if existing:
                    return existing.id

                exch = session.query(Exchange).filter_by(is_active=True).first()
                if not exch:
                    ex = exchange_manager.get_exchange()
                    ex_id   = getattr(ex, "id",   "unknown") if ex else "unknown"
                    ex_name = getattr(ex, "name", ex_id)     if ex else "Historical Data"
                    exch = session.query(Exchange).filter_by(name=f"{ex_name} (Historical)").first()
                    if not exch:
                        exch = Exchange(
                            name=f"{ex_name} (Historical)",
                            exchange_id=ex_id,
                            sandbox_mode=False,
                            is_active=False,
                        )
                        session.add(exch)
                        session.flush()

                parts = self.symbol.split("/")
                base  = parts[0] if parts else self.symbol
                quote = parts[1] if len(parts) > 1 else "USDT"
                asset = Asset(
                    exchange_id    = exch.id,
                    symbol         = self.symbol,
                    base_currency  = base,
                    quote_currency = quote,
                    is_active      = True,
                )
                session.add(asset)
                session.flush()
                return asset.id
        except Exception as e:
            logger.error("MultiTFHistoricalWorker._ensure_asset: %s", e, exc_info=True)
            return None

    def _save_to_db(self, candles: list, asset_id: int, timeframe: str) -> int:
        saved = 0
        batch_size = 500
        with get_session() as session:
            existing = set(
                r[0] for r in session.query(OHLCV.timestamp)
                .filter_by(asset_id=asset_id, timeframe=timeframe)
                .all()
            )
            new_rows = []
            for c in candles:
                ts = datetime.utcfromtimestamp(c[0] / 1000)
                if ts not in existing:
                    new_rows.append(OHLCV(
                        asset_id  = asset_id,
                        timeframe = timeframe,
                        timestamp = ts,
                        open      = float(c[1]),
                        high      = float(c[2]),
                        low       = float(c[3]),
                        close     = float(c[4]),
                        volume    = float(c[5]),
                    ))
            for i in range(0, len(new_rows), batch_size):
                session.bulk_save_objects(new_rows[i:i + batch_size])
                session.flush()
            saved = len(new_rows)
        logger.info("MultiTFHistoricalWorker: saved %d candles for %s %s",
                    saved, self.symbol, timeframe)
        return saved


# Backward-compatible alias
BinanceMultiTFWorker = MultiTFHistoricalWorker


def _timeframe_to_minutes(tf: str) -> int:
    mapping = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "6h": 360,
        "12h": 720, "1d": 1440, "1w": 10080,
    }
    return mapping.get(tf, 60)
