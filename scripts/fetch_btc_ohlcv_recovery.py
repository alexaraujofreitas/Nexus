"""
scripts/fetch_btc_ohlcv_recovery.py
=====================================
Safe BTC full data recovery script.

Fetches BTC 1m, 5m, 15m, 1h, 4h OHLCV bars + funding rates DIRECTLY from
Bybit for a specific date range, saving them to data/validation/.

Does NOT touch ETH, SOL, XRP, BNB files, OI files, or any other symbol's data.

WHY THIS EXISTS
---------------
fetch_historical_data_v2.py derives 1h/4h by resampling from 1m.  When Bybit
rate-limits the 1m fetch it silently truncates the dataset, and the in-progress
write leaves a file with a valid PAR1 header but missing footer (corrupted).

This script fetches each timeframe directly and atomically.

ESTIMATED RUNTIME (from 2023-11-09 to today, ~2.3 years)
----------------------------------------------------------
Timeframe   Bars      API calls   ~Time
1m          1,209,600   1,210       10 min
5m            241,920     242        2 min
15m            80,640      81       40 sec
1h             20,160      21       10 sec
4h              5,040       6        3 sec
funding        ~5,100     ~52       30 sec
Total:                  ~1,612      ~13 min

USAGE
-----
# Recover all TFs back to match OI start date (default)
python scripts/fetch_btc_ohlcv_recovery.py

# Recover to a specific start date
python scripts/fetch_btc_ohlcv_recovery.py --start-date 2023-11-09

# Dry run (prints what would be fetched, no writes)
python scripts/fetch_btc_ohlcv_recovery.py --dry-run

# Only specific timeframes
python scripts/fetch_btc_ohlcv_recovery.py --tfs 1m 5m 15m

SAFE BY DESIGN
--------------
- Writes to a .tmp file first; renames to final path only on success.
- Never touches any non-BTC parquets.
- If the target parquet already contains MORE history than what would be
  fetched, merges the two and keeps the union (no data loss).
- Funding uses the same write-then-rename pattern.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# ─── Config ──────────────────────────────────────────────────────────────────

BYBIT_BASE        = "https://api.bybit.com"
BYBIT_KLINE_LIMIT = 1000
BYBIT_FUND_LIMIT  = 200        # Bybit funding history max per call
SLEEP_BETWEEN_REQ = 0.5        # 2 req/sec — far under 120 req/min limit
RETRY_BACKOFF     = [5, 10, 30, 60, 120]
MAX_RETRIES       = 5

VAL_DIR           = Path(__file__).parent.parent / "data" / "validation"
DEFAULT_START     = "2023-11-09"     # earliest Bybit OI usually available
SYMBOL            = "BTC"

# All OHLCV timeframes supported; each is fetched independently
TF_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240"}
DEFAULT_TFS = ["1m", "5m", "15m", "1h", "4h"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("btc_ohlcv_recovery")

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _bybit_get(path: str, params: dict, retry: int = 0) -> dict:
    """GET request to Bybit with exponential backoff for HTTP and JSON rate limits."""
    url = BYBIT_BASE + path
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
            logger.warning("HTTP 429 — sleeping %ds (retry %d)", wait, retry + 1)
            time.sleep(wait)
            return _bybit_get(path, params, retry + 1)
        if resp.status_code >= 500:
            if retry >= MAX_RETRIES:
                raise RuntimeError(
                    f"Server error {resp.status_code} after {MAX_RETRIES} retries"
                )
            wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
            logger.warning("Server error %d — sleeping %ds", resp.status_code, wait)
            time.sleep(wait)
            return _bybit_get(path, params, retry + 1)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError as e:
        if retry >= MAX_RETRIES:
            raise
        wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
        logger.warning("Connection error — sleeping %ds: %s", wait, e)
        time.sleep(wait)
        return _bybit_get(path, params, retry + 1)


def _check_rate_limit(data: dict) -> bool:
    """Return True if the response is a JSON-level rate limit that should be retried."""
    if data.get("retCode") == 0:
        return False
    msg = data.get("retMsg", "").lower()
    return any(k in msg for k in ("too many", "rate limit", "visit"))


# ─── OHLCV fetch ─────────────────────────────────────────────────────────────

def fetch_ohlcv(symbol: str, tf: str, start_ts_ms: int) -> pd.DataFrame:
    """
    Fetch OHLCV from Bybit, backwards from now to start_ts_ms.
    Handles JSON-level rate limit errors with backoff.
    """
    slug      = symbol + "USDT"
    interval  = TF_MAP[tf]
    end_ts    = int(datetime.now(timezone.utc).timestamp() * 1000)
    chunk_end = end_ts
    all_rows  = []
    total_calls = 0

    logger.info(
        "[%s][%s] Fetching from %s to now…", symbol, tf,
        datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
    )

    while chunk_end > start_ts_ms:
        params = {
            "category": "linear",
            "symbol":   slug,
            "interval": interval,
            "end":      chunk_end,
            "limit":    BYBIT_KLINE_LIMIT,
        }
        data = _bybit_get("/v5/market/kline", params)

        # JSON-level rate limit — back off and retry
        if _check_rate_limit(data):
            wait = 60
            logger.warning(
                "JSON rate limit: '%s' — sleeping %ds then retrying",
                data.get("retMsg", ""), wait,
            )
            time.sleep(wait)
            continue  # retry same chunk_end without advancing

        if data.get("retCode") != 0:
            logger.error("[%s][%s] API error: %s", symbol, tf, data.get("retMsg", ""))
            break

        candles = data.get("result", {}).get("list", [])
        if not candles:
            break

        rows = [
            {
                "timestamp": int(c[0]),
                "open":  float(c[1]), "high": float(c[2]),
                "low":   float(c[3]), "close": float(c[4]),
                "volume": float(c[5]),
            }
            for c in candles
        ]
        all_rows.extend(rows)
        total_calls += 1

        earliest  = min(r["timestamp"] for r in rows)
        chunk_end = earliest - 1

        if total_calls % 50 == 0:
            dt = datetime.fromtimestamp(earliest / 1000, tz=timezone.utc)
            logger.info(
                "[%s][%s] Progress: %s | rows so far: %d | API calls: %d",
                symbol, tf, dt.strftime("%Y-%m-%d"), len(all_rows), total_calls,
            )

        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        raise RuntimeError(f"No data fetched for {symbol} {tf}")

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    cutoff = pd.Timestamp(start_ts_ms, unit="ms", tz="UTC")
    df = df[df["timestamp"] >= cutoff].reset_index(drop=True)

    logger.info(
        "[%s][%s] Fetched %d bars | %s → %s | %d API calls",
        symbol, tf, len(df),
        df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
        df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
        total_calls,
    )
    return df


# ─── Funding fetch ────────────────────────────────────────────────────────────

def fetch_funding(symbol: str, start_ts_ms: int) -> pd.DataFrame:
    """
    Fetch funding rate history from Bybit for the given symbol.
    Bybit returns funding every 8 hours; ~2.3 years ≈ 5,100 entries ≈ 26 API calls.
    """
    slug      = symbol + "USDT"
    end_ts    = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor    = None
    all_rows  = []
    total_calls = 0

    logger.info(
        "[%s][funding] Fetching from %s to now…", symbol,
        datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
    )

    while True:
        params: dict = {
            "category": "linear",
            "symbol":   slug,
            "limit":    BYBIT_FUND_LIMIT,
            "startTime": start_ts_ms,
            "endTime":   end_ts,
        }
        if cursor:
            params["cursor"] = cursor

        data = _bybit_get("/v5/market/funding/history", params)

        if _check_rate_limit(data):
            wait = 60
            logger.warning("JSON rate limit (funding) — sleeping %ds", wait)
            time.sleep(wait)
            continue

        if data.get("retCode") != 0:
            logger.error("[%s][funding] API error: %s", symbol, data.get("retMsg", ""))
            break

        result = data.get("result", {})
        entries = result.get("list", [])
        if not entries:
            break

        for e in entries:
            all_rows.append({
                "timestamp":    int(e["fundingRateTimestamp"]),
                "funding_rate": float(e["fundingRate"]),
            })

        total_calls += 1
        cursor = result.get("nextPageCursor")
        if not cursor:
            break

        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        raise RuntimeError(f"No funding data fetched for {symbol}")

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    cutoff = pd.Timestamp(start_ts_ms, unit="ms", tz="UTC")
    df = df[df["timestamp"] >= cutoff].reset_index(drop=True)

    logger.info(
        "[%s][funding] Fetched %d entries | %s → %s | %d API calls",
        symbol, len(df),
        df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
        df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
        total_calls,
    )
    return df


# ─── Merge helper ─────────────────────────────────────────────────────────────

def _merge_with_existing(new_df: pd.DataFrame, path: Path) -> pd.DataFrame:
    """
    If an existing valid parquet is present, merge new data with it, keeping
    the union (no data loss).  If the existing file is corrupted, use new only.
    """
    if not path.exists():
        return new_df

    try:
        old_df = pd.read_parquet(path)
        # Validate — ensure we can parse timestamps
        ts_col = "timestamp"
        if ts_col not in old_df.columns:
            raise ValueError("No timestamp column in existing file")
        if old_df[ts_col].dt.tz is None:
            old_df[ts_col] = old_df[ts_col].dt.tz_localize("UTC")
        merged = pd.concat([old_df, new_df], ignore_index=True)
        merged = (
            merged.sort_values(ts_col)
            .drop_duplicates(ts_col)
            .reset_index(drop=True)
        )
        logger.info(
            "  Merged with existing %s → %d bars (was %d, new %d)",
            path.name, len(merged), len(old_df), len(new_df),
        )
        return merged
    except Exception as e:
        logger.warning(
            "  Existing %s could not be merged (%s) — using new data only",
            path.name, e,
        )
        return new_df


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _safe_write(df: pd.DataFrame, path_out: Path) -> None:
    """Write parquet via temp-file then atomic rename.  Never corrupts on failure."""
    path_tmp = path_out.with_suffix(".parquet.tmp")
    df.to_parquet(path_tmp, index=False)
    path_tmp.replace(path_out)
    size = path_out.stat().st_size
    logger.info(
        "  Saved %s (%s) — %d rows | %s → %s",
        path_out.name, _human_size(size), len(df),
        df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
        df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safe BTC full data recovery (1m/5m/15m/1h/4h + funding)"
    )
    parser.add_argument(
        "--start-date", default=DEFAULT_START,
        help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})",
    )
    parser.add_argument(
        "--tfs", nargs="+", default=DEFAULT_TFS,
        choices=list(TF_MAP.keys()),
        help="Timeframes to fetch (default: all OHLCV TFs)",
    )
    parser.add_argument(
        "--skip-funding", action="store_true",
        help="Skip funding rate recovery",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be fetched without writing any files",
    )
    args = parser.parse_args()

    start_dt    = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts_ms = int(start_dt.timestamp() * 1000)

    logger.info("BTC Full Data Recovery")
    logger.info("  Symbol:       %s", SYMBOL)
    logger.info("  Start date:   %s", args.start_date)
    logger.info("  TFs:          %s", ", ".join(args.tfs))
    logger.info("  Funding:      %s", "SKIP" if args.skip_funding else "YES")
    logger.info("  Output dir:   %s", VAL_DIR)
    logger.info("  Dry run:      %s", args.dry_run)
    logger.info("  NOTE: Only BTC files are written — all other symbols untouched")
    logger.info("=" * 60)

    VAL_DIR.mkdir(parents=True, exist_ok=True)

    slug = SYMBOL + "USDT"
    failed: list[str] = []

    # ── OHLCV timeframes ──────────────────────────────────────────────────────
    for tf in args.tfs:
        path_out = VAL_DIR / f"{slug}_{tf}.parquet"
        logger.info("Processing %s %s → %s", SYMBOL, tf, path_out.name)

        if args.dry_run:
            logger.info("  DRY RUN — would fetch and save %s", path_out.name)
            continue

        try:
            df = fetch_ohlcv(SYMBOL, tf, start_ts_ms)
            df = _merge_with_existing(df, path_out)
            _safe_write(df, path_out)
        except Exception as e:
            logger.error("  FAILED %s %s: %s", SYMBOL, tf, e)
            failed.append(f"{tf}")

    # ── Funding rates ─────────────────────────────────────────────────────────
    if not args.skip_funding:
        path_out = VAL_DIR / f"{slug}_funding.parquet"
        logger.info("Processing %s funding → %s", SYMBOL, path_out.name)

        if args.dry_run:
            logger.info("  DRY RUN — would fetch and save %s", path_out.name)
        else:
            try:
                df = fetch_funding(SYMBOL, start_ts_ms)
                df = _merge_with_existing(df, path_out)
                _safe_write(df, path_out)
            except Exception as e:
                logger.error("  FAILED %s funding: %s", SYMBOL, e)
                failed.append("funding")

    # ── Summary ───────────────────────────────────────────────────────────────
    if not args.dry_run:
        logger.info("=" * 60)
        if failed:
            logger.error("Recovery PARTIAL — failed: %s", ", ".join(failed))
        else:
            logger.info("Recovery COMPLETE — all files written successfully")
        logger.info("Files in %s:", VAL_DIR)
        for p in sorted(VAL_DIR.glob(f"{slug}_*.parquet")):
            logger.info("  %s  (%s)", p.name, _human_size(p.stat().st_size))
        logger.info("")
        logger.info("Next steps:")
        logger.info("  1. Verify BTC OI: python -c \"import pandas as pd; df=pd.read_parquet('data/validation/BTCUSDT_oi.parquet'); print(len(df), 'OI rows', df.timestamp.min(), '->', df.timestamp.max())\"")
        logger.info("  2. Verify data completeness: python scripts/fetch_historical_data_v3.py --dry-run")


if __name__ == "__main__":
    main()
