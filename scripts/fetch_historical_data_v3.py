"""
scripts/fetch_historical_data_v3.py
=====================================
Smart gap-filling historical data fetch for ETH, SOL, XRP, BNB.

BTC is SKIPPED by default (already complete).  The script inspects each existing
parquet, determines exactly which date ranges are absent, and only fetches the
missing portion.  Existing valid data is ALWAYS preserved via merge-then-rename.

STRATEGY
--------
OHLCV (1m):
  - If file is valid → read existing [min_ts, max_ts]
  - Backward fill: fetch start_date → (existing_min − 1 m) from Bybit
  - Forward fill:  fetch (existing_max + 1 m) → now  [if stale > 2 h]
  - Merge union, resample to 5m / 15m / 1h / 4h, write all via tmp→rename

Funding:
  - Skip if existing file has >= 4 000 rows (effectively complete, 8 h cadence)
  - Otherwise fetch full history from start_date and merge

OI (Bybit open-interest):
  - Skipped by default (each symbol ~90 min)
  - Enable with --fetch-oi
  - Skip if existing file has >= 1 000 rows

SAFETY GUARANTEES
-----------------
  Write-then-rename on every save -- rate-limit interruptions cannot corrupt
  Merge union, never overwrite -- no existing data is ever deleted
  BTC files are never touched
  Funding complete (>= 4 000 rows) -> zero API calls wasted

ESTIMATED RUNTIMES (2022-06-06 start, from scratch)
----------------------------------------------------
OHLCV 1m + resample:
  ETH backward  ~14 min  (1 657 000 bars)
  SOL backward  ~ 5 min  (  629 000 bars)
  XRP backward  ~14 min  (1 657 000 bars)
  BNB full      ~16 min  (1 958 000 bars)
  Total OHLCV   ~49 min

OI (--fetch-oi, runs after OHLCV):
  Per symbol    ~90 min x 4  ~6 h  (run overnight)

USAGE
-----
# OHLCV + funding only (default, ~49 min)
python scripts/fetch_historical_data_v3.py

# Same but also fetch OI (~6 h, run overnight)
python scripts/fetch_historical_data_v3.py --fetch-oi

# Specific symbols only
python scripts/fetch_historical_data_v3.py --symbols ETH BNB

# Custom start date (default: 2022-06-06, matching BTC OI history)
python scripts/fetch_historical_data_v3.py --start-date 2022-06-06

# Dry run -- print gaps without fetching
python scripts/fetch_historical_data_v3.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BYBIT_BASE        = "https://api.bybit.com"
BYBIT_KLINE_LIMIT = 1000
BYBIT_FUND_LIMIT  = 200
BYBIT_OI_LIMIT    = 200
SLEEP_BETWEEN_REQ = 0.5        # 2 req/s -- well under Bybit 120 req/min
RETRY_BACKOFF     = [5, 10, 30, 60, 120]
MAX_RETRIES       = 5

STALE_THRESHOLD_H = 2          # forward-fill if existing data is older than this
FUNDING_COMPLETE  = 4_000      # skip funding fetch if file has >= this many rows
OI_COMPLETE       = 1_000      # skip OI fetch if file has >= this many rows

VAL_DIR       = Path(__file__).parent.parent / "data" / "validation"
DEFAULT_START = "2022-06-06"
DEFAULT_SYMS  = ["ETH", "SOL", "XRP", "BNB"]

TF_MAP = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
RESAMPLE_AGG = {"open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"}
RESAMPLE_RULES = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_v3")

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _bybit_get(path: str, params: dict, retry: int = 0) -> dict:
    url = BYBIT_BASE + path
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
            logger.warning("HTTP 429 -- sleeping %ds (retry %d)", wait, retry + 1)
            time.sleep(wait)
            return _bybit_get(path, params, retry + 1)
        if resp.status_code >= 500:
            if retry >= MAX_RETRIES:
                raise RuntimeError(f"Server {resp.status_code} after {MAX_RETRIES} retries")
            wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
            logger.warning("Server %d -- sleeping %ds", resp.status_code, wait)
            time.sleep(wait)
            return _bybit_get(path, params, retry + 1)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError as e:
        if retry >= MAX_RETRIES:
            raise
        wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
        logger.warning("Connection error -- sleeping %ds: %s", wait, e)
        time.sleep(wait)
        return _bybit_get(path, params, retry + 1)


def _is_rate_limited(data: dict) -> bool:
    if data.get("retCode") == 0:
        return False
    msg = data.get("retMsg", "").lower()
    return any(k in msg for k in ("too many", "rate limit", "visit"))

# ---------------------------------------------------------------------------
# Parquet inspection
# ---------------------------------------------------------------------------

def inspect_parquet(path: Path):
    """
    Returns (is_valid, min_ts, max_ts, row_count).
    is_valid=False if file missing or corrupted.
    """
    if not path.exists():
        return False, None, None, 0
    try:
        df = pd.read_parquet(path)
        if df.empty or "timestamp" not in df.columns:
            return False, None, None, 0
        ts = pd.to_datetime(df["timestamp"], utc=True)
        return True, ts.min(), ts.max(), len(df)
    except Exception:
        return False, None, None, 0

# ---------------------------------------------------------------------------
# Core fetch: OHLCV for a specific time window
# ---------------------------------------------------------------------------

def fetch_ohlcv_window(
    symbol: str, start_ts_ms: int, end_ts_ms: int
) -> pd.DataFrame:
    """
    Fetch 1m OHLCV from Bybit backwards from end_ts_ms down to start_ts_ms.
    Handles JSON-level rate limits with retry.
    """
    slug     = symbol + "USDT"
    chunk_end = end_ts_ms
    all_rows: list[dict] = []
    calls = 0

    start_s = datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_s   = datetime.fromtimestamp(end_ts_ms   / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    logger.info("[%s][1m] Fetching %s -> %s ...", symbol, start_s, end_s)

    while chunk_end > start_ts_ms:
        params = {
            "category": "linear", "symbol": slug,
            "interval": "1", "end": chunk_end,
            "limit": BYBIT_KLINE_LIMIT,
        }
        data = _bybit_get("/v5/market/kline", params)

        if _is_rate_limited(data):
            logger.warning("[%s][1m] JSON rate limit -- sleeping 60s then retrying", symbol)
            time.sleep(60)
            continue  # retry same chunk_end, do NOT advance

        if data.get("retCode") != 0:
            logger.error("[%s][1m] API error: %s", symbol, data.get("retMsg", ""))
            break

        candles = data.get("result", {}).get("list", [])
        if not candles:
            break

        for c in candles:
            ts_ms = int(c[0])
            if ts_ms < start_ts_ms:
                continue
            all_rows.append({
                "timestamp": ts_ms,
                "open":  float(c[1]), "high": float(c[2]),
                "low":   float(c[3]), "close": float(c[4]),
                "volume": float(c[5]),
            })

        calls += 1
        earliest  = min(int(c[0]) for c in candles)
        chunk_end = earliest - 1

        if calls % 100 == 0:
            dt = datetime.fromtimestamp(earliest / 1000, tz=timezone.utc)
            logger.info("[%s][1m] Progress: %s | rows: %d | calls: %d",
                        symbol, dt.strftime("%Y-%m-%d"), len(all_rows), calls)

        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        logger.warning("[%s][1m] No rows fetched in window %s -> %s", symbol, start_s, end_s)
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    logger.info("[%s][1m] Fetched %d rows | %s -> %s | %d API calls",
                symbol, len(df),
                df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
                df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
                calls)
    return df

# ---------------------------------------------------------------------------
# Funding fetch
# ---------------------------------------------------------------------------

def fetch_funding_all(symbol: str, start_ts_ms: int) -> pd.DataFrame:
    slug   = symbol + "USDT"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = None
    rows: list[dict] = []
    calls = 0

    logger.info("[%s][funding] Fetching from %s ...", symbol,
                datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

    while True:
        params: dict = {
            "category": "linear", "symbol": slug,
            "limit": BYBIT_FUND_LIMIT,
            "startTime": start_ts_ms, "endTime": now_ms,
        }
        if cursor:
            params["cursor"] = cursor

        data = _bybit_get("/v5/market/funding/history", params)
        if _is_rate_limited(data):
            time.sleep(60)
            continue
        if data.get("retCode") != 0:
            logger.error("[%s][funding] API error: %s", symbol, data.get("retMsg", ""))
            break

        result  = data.get("result", {})
        entries = result.get("list", [])
        if not entries:
            break

        for e in entries:
            rows.append({
                "timestamp":    int(e["fundingRateTimestamp"]),
                "funding_rate": float(e["fundingRate"]),
            })

        calls += 1
        cursor = result.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(SLEEP_BETWEEN_REQ)

    if not rows:
        raise RuntimeError(f"No funding data fetched for {symbol}")

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    logger.info("[%s][funding] %d rows | %s -> %s | %d calls",
                symbol, len(df),
                df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
                df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
                calls)
    return df

# ---------------------------------------------------------------------------
# OI fetch
# ---------------------------------------------------------------------------

def fetch_oi_all(symbol: str, start_ts_ms: int) -> pd.DataFrame:
    slug   = symbol + "USDT"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = None
    rows: list[dict] = []
    page   = 0

    logger.info("[%s][OI] Fetching from %s (this takes ~90 min) ...", symbol,
                datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

    while True:
        params: dict = {
            "category":     "linear", "symbol": slug,
            "intervalTime": "15min",  "limit":  BYBIT_OI_LIMIT,
            "startTime":    start_ts_ms, "endTime": now_ms,
        }
        if cursor:
            params["cursor"] = cursor

        data = _bybit_get("/v5/market/open-interest", params)
        if _is_rate_limited(data):
            time.sleep(60)
            continue
        if data.get("retCode") != 0:
            logger.error("[%s][OI] API error: %s", symbol, data.get("retMsg", ""))
            break

        result  = data.get("result", {})
        entries = result.get("list", [])
        if not entries:
            break

        for e in entries:
            rows.append({
                "timestamp": int(e["timestamp"]),
                "oi":        float(e["openInterest"]),
            })

        page  += 1
        cursor = result.get("nextPageCursor")

        if page % 50 == 0:
            earliest_ms = min(int(e["timestamp"]) for e in entries)
            dt = datetime.fromtimestamp(earliest_ms / 1000, tz=timezone.utc)
            logger.info("[%s][OI] page %d, earliest: %s", symbol, page, dt.isoformat())

        if not cursor:
            break
        time.sleep(SLEEP_BETWEEN_REQ)

    if not rows:
        raise RuntimeError(f"No OI data fetched for {symbol}")

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    logger.info("[%s][OI] %d rows | %s -> %s | %d pages",
                symbol, len(df),
                df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
                df["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
                page)
    return df

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _safe_write(df: pd.DataFrame, path: Path) -> None:
    """Atomic write via temp file -> rename. Never corrupts on interruption."""
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)
    size_mb = path.stat().st_size / 1_048_576
    logger.info("  Saved %-30s %5.1f MB | %d rows | %s -> %s",
                path.name, size_mb, len(df),
                df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
                df["timestamp"].iloc[-1].strftime("%Y-%m-%d"))


def _merge_union(existing: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    """Union-merge two DataFrames on timestamp, dedup, sort."""
    if existing is None or existing.empty:
        return new_df
    merged = (
        pd.concat([existing, new_df], ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )
    return merged


def _load_valid(path: Path) -> pd.DataFrame | None:
    """Return DataFrame if parquet is valid, else None."""
    valid, *_ = inspect_parquet(path)
    if not valid:
        return None
    df = pd.read_parquet(path)
    if "timestamp" in df.columns and df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    return df

# ---------------------------------------------------------------------------
# Per-symbol processors
# ---------------------------------------------------------------------------

def process_ohlcv(symbol: str, slug: str, start_ts_ms: int, dry_run: bool) -> None:
    """
    Smart gap-fill 1m OHLCV then resample to 5m / 15m / 1h / 4h.
    Only fetches the missing windows -- never re-downloads existing data.
    """
    path_1m = VAL_DIR / f"{slug}_1m.parquet"
    now_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)
    stale_cutoff_ms = now_ms - STALE_THRESHOLD_H * 3_600_000

    valid, existing_min, existing_max, n_rows = inspect_parquet(path_1m)

    backward_start_ms: int | None = None
    backward_end_ms:   int | None = None
    forward_start_ms:  int | None = None

    if not valid:
        # No usable file -> full fetch from start_date to now
        backward_start_ms = start_ts_ms
        backward_end_ms   = now_ms
        logger.info("[%s][1m] No valid file -> full fetch from %s",
                    symbol,
                    datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
    else:
        existing_min_ms = int(existing_min.timestamp() * 1000)
        existing_max_ms = int(existing_max.timestamp() * 1000)

        if existing_min_ms > start_ts_ms:
            backward_start_ms = start_ts_ms
            backward_end_ms   = existing_min_ms - 60_000   # 1 min before existing start
            logger.info("[%s][1m] Backward gap: %s -> %s  (%d existing rows kept)",
                        symbol,
                        datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                        existing_min.strftime("%Y-%m-%d"),
                        n_rows)
        else:
            logger.info("[%s][1m] History already reaches %s -- no backward fill needed",
                        symbol, existing_min.strftime("%Y-%m-%d"))

        if existing_max_ms < stale_cutoff_ms:
            forward_start_ms = existing_max_ms + 60_000
            logger.info("[%s][1m] Forward gap from %s to now",
                        symbol, existing_max.strftime("%Y-%m-%d %H:%M"))

        if backward_start_ms is None and forward_start_ms is None:
            logger.info("[%s][1m] Already up to date -- skipping OHLCV", symbol)
            return

    if dry_run:
        if backward_start_ms is not None:
            logger.info("  DRY RUN [%s][1m] would backward-fetch: %s -> %s",
                        symbol,
                        datetime.fromtimestamp(backward_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                        datetime.fromtimestamp(backward_end_ms   / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        if forward_start_ms is not None:
            logger.info("  DRY RUN [%s][1m] would forward-fetch: %s -> now",
                        symbol,
                        datetime.fromtimestamp(forward_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        return

    existing_1m = _load_valid(path_1m)

    # Backward fill
    backward_df = None
    if backward_start_ms is not None:
        backward_df = fetch_ohlcv_window(symbol, backward_start_ms, backward_end_ms)

    # Forward fill
    forward_df = None
    if forward_start_ms is not None:
        forward_df = fetch_ohlcv_window(symbol, forward_start_ms, now_ms)

    # Merge: backward + existing + forward
    df_1m = existing_1m
    if backward_df is not None and not backward_df.empty:
        df_1m = _merge_union(df_1m, backward_df)
    if forward_df is not None and not forward_df.empty:
        df_1m = _merge_union(df_1m, forward_df)

    if df_1m is None or df_1m.empty:
        logger.error("[%s] 1m empty after merge -- skipping", symbol)
        return

    _safe_write(df_1m, path_1m)

    # Resample to higher TFs from the complete merged 1m
    df_idx = df_1m.set_index("timestamp").sort_index()
    for tf, rule in RESAMPLE_RULES.items():
        path_tf = VAL_DIR / f"{slug}_{tf}.parquet"
        resampled = (
            df_idx.resample(rule).agg(RESAMPLE_AGG)
            .dropna(subset=["open"])
            .reset_index()
        )
        # Merge with any existing higher-TF data to preserve rows outside 1m window
        existing_tf = _load_valid(path_tf)
        if existing_tf is not None:
            resampled = _merge_union(existing_tf, resampled)
        _safe_write(resampled, path_tf)


def process_funding(symbol: str, slug: str, start_ts_ms: int, dry_run: bool) -> None:
    path = VAL_DIR / f"{slug}_funding.parquet"
    valid, _, _, n_rows = inspect_parquet(path)

    if valid and n_rows >= FUNDING_COMPLETE:
        logger.info("[%s][funding] Complete (%d rows) -- skipping", symbol, n_rows)
        return

    status = f"only {n_rows} rows" if valid else "MISSING"
    logger.info("[%s][funding] %s -- fetching ...", symbol, status)

    if dry_run:
        logger.info("  DRY RUN [%s][funding] would fetch from %s",
                    symbol,
                    datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        return

    df_new  = fetch_funding_all(symbol, start_ts_ms)
    existing = _load_valid(path)
    _safe_write(_merge_union(existing, df_new), path)


def process_oi(symbol: str, slug: str, start_ts_ms: int, dry_run: bool) -> None:
    path = VAL_DIR / f"{slug}_oi.parquet"
    valid, _, _, n_rows = inspect_parquet(path)

    if valid and n_rows >= OI_COMPLETE:
        logger.info("[%s][OI] Complete (%d rows) -- skipping", symbol, n_rows)
        return

    status = f"only {n_rows} rows" if valid else "MISSING"
    logger.info("[%s][OI] %s -- fetching (~90 min) ...", symbol, status)

    if dry_run:
        logger.info("  DRY RUN [%s][OI] would fetch from %s (~90 min)",
                    symbol,
                    datetime.fromtimestamp(start_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
        return

    df_new   = fetch_oi_all(symbol, start_ts_ms)
    existing = _load_valid(path)
    _safe_write(_merge_union(existing, df_new), path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart gap-fill OHLCV + funding (+ optional OI) for ETH/SOL/XRP/BNB"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMS,
                        help=f"Symbols to process (default: {DEFAULT_SYMS})")
    parser.add_argument("--start-date", default=DEFAULT_START,
                        help=f"Earliest date to target (default: {DEFAULT_START})")
    parser.add_argument("--fetch-oi", action="store_true",
                        help="Also fetch OI -- ~90 min per symbol, run overnight")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be fetched without writing anything")
    args = parser.parse_args()

    if "BTC" in args.symbols:
        logger.warning("BTC removed from list -- use fetch_btc_ohlcv_recovery.py for BTC")
        args.symbols = [s for s in args.symbols if s != "BTC"]

    if not args.symbols:
        logger.error("No symbols to process after filtering. Exiting.")
        return

    start_dt    = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts_ms = int(start_dt.timestamp() * 1000)

    logger.info("fetch_historical_data_v3")
    logger.info("  Symbols:    %s", ", ".join(args.symbols))
    logger.info("  Start date: %s  (target earliest date for all files)", args.start_date)
    logger.info("  Fetch OI:   %s", "YES (~90 min/symbol)" if args.fetch_oi else "NO (use --fetch-oi to enable)")
    logger.info("  Dry run:    %s", args.dry_run)
    logger.info("  Output dir: %s", VAL_DIR)
    logger.info("=" * 60)

    VAL_DIR.mkdir(parents=True, exist_ok=True)
    t0     = time.time()
    failed = []

    for symbol in args.symbols:
        slug = symbol + "USDT"
        logger.info("")
        logger.info(">>> %s", symbol)
        logger.info("-" * 40)

        try:
            process_ohlcv(symbol, slug, start_ts_ms, args.dry_run)
        except Exception as e:
            logger.error("[%s] OHLCV failed: %s", symbol, e, exc_info=True)
            failed.append(f"{symbol}/OHLCV")

        try:
            process_funding(symbol, slug, start_ts_ms, args.dry_run)
        except Exception as e:
            logger.error("[%s] Funding failed: %s", symbol, e)
            failed.append(f"{symbol}/funding")

        if args.fetch_oi:
            try:
                process_oi(symbol, slug, start_ts_ms, args.dry_run)
            except Exception as e:
                logger.error("[%s] OI failed: %s", symbol, e)
                failed.append(f"{symbol}/OI")

    elapsed = (time.time() - t0) / 60
    logger.info("")
    logger.info("=" * 60)
    logger.info("Done in %.1f min", elapsed)

    if failed:
        logger.error("Failed: %s", ", ".join(failed))
    else:
        logger.info("All items succeeded")

    logger.info("")
    logger.info("-- File inventory --")
    for symbol in args.symbols:
        slug = symbol + "USDT"
        for suffix in ["_1m", "_5m", "_15m", "_1h", "_4h", "_funding", "_oi"]:
            p = VAL_DIR / f"{slug}{suffix}.parquet"
            if p.exists():
                valid, mn, mx, n = inspect_parquet(p)
                if valid:
                    logger.info("  %-32s %5.1f MB  %9d rows  %s -> %s",
                                p.name, p.stat().st_size / 1_048_576, n,
                                mn.strftime("%Y-%m-%d"), mx.strftime("%Y-%m-%d"))
                else:
                    logger.warning("  %-32s CORRUPTED", p.name)
            else:
                logger.warning("  %-32s MISSING", p.name)

    if not args.fetch_oi:
        logger.info("")
        logger.info("OI not fetched. To fetch OI for all 4 symbols (~6 h total):")
        logger.info("  python scripts/fetch_historical_data_v3.py --fetch-oi")


if __name__ == "__main__":
    main()
