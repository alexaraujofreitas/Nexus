"""
scripts/fetch_historical_data_v2.py — Extended Historical Data Fetcher
=======================================================================
Fetches and saves ALL timeframes needed for NexusTrader model testing:
  1m, 5m, 15m, 1h, 4h OHLCV + funding rates + open interest

DEFAULT MODE (BTC-only, 4-year window):
  Fetches BTCUSDT data from 2022-03-25 to present — matching existing OHLCV.
  Existing 4-year OHLCV parquets are reused (not re-fetched).
  OI and funding are fetched/refreshed as needed.
  NOTE: Bybit OI API typically provides ~2.5 years of history regardless of the
  requested window; the script fetches as far back as the API allows.

FULL MULTI-SYMBOL MODE:
  Use --symbols ETH SOL XRP BNB (or all 5) to process additional symbols.

STRATEGY:
  - For BTC/ETH/SOL 1m: already exists → skip re-fetch, resample to higher TFs
  - For XRP/BNB 1m:     fetch from Bybit (same method as original fetch script)
  - Higher TFs (5m/15m/1h/4h): derived by resampling from 1m (more reliable
    than fetching separately — avoids gaps and TF alignment issues)
  - Funding + OI: fetch from Bybit; Bybit OI history typically ~2.5 years

OUTPUT (saved to data/validation/):
  {SYM}USDT_1m.parquet        — 1-minute OHLCV
  {SYM}USDT_5m.parquet        — 5-minute OHLCV  (resampled)
  {SYM}USDT_15m.parquet       — 15-minute OHLCV (resampled)
  {SYM}USDT_1h.parquet        — 1-hour OHLCV    (resampled)
  {SYM}USDT_4h.parquet        — 4-hour OHLCV    (resampled)
  {SYM}USDT_funding.parquet   — 8h funding rates
  {SYM}USDT_oi.parquet        — Open interest (5m resolution)
  fetch_v2_progress.json      — Checkpoint (safe to resume with --resume)

USAGE (Windows CMD / PowerShell):
  cd C:\\path\\to\\NexusTrader

  # Default: BTC only, 3 years (2023-03-25 → now)
  python scripts/fetch_historical_data_v2.py

  # BTC only, explicit start date (most precise — overrides --years)
  python scripts/fetch_historical_data_v2.py --start-date 2022-03-25

  # Re-fetch BTC OI from scratch (fetches as far back as Bybit allows, ~2.5yr)
  python scripts/fetch_historical_data_v2.py --force-refetch

  # Resume an interrupted run
  python scripts/fetch_historical_data_v2.py --resume

  # Multi-symbol: add other tokens once BTC is confirmed
  python scripts/fetch_historical_data_v2.py --symbols BTC ETH SOL XRP BNB

  # Resample only (no API calls) — regenerate higher TFs from existing 1m
  python scripts/fetch_historical_data_v2.py --resample-only

EXPECTED RUNTIME (BTC-only 3yr):
  OHLCV:   ~0 min (existing 4yr parquet reused)
  OI:      ~30–60 min (Bybit 5min OI, ~2.5yr available, 200 rows/request)
  Funding: ~5–10 min

RATE LIMIT:
  Bybit: 120 req/min → using 40 req/min with 1.5s sleep between requests
  Automatic exponential backoff on 429/503 errors
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_v2")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT_DIR    = Path(__file__).parent.parent
VAL_DIR     = ROOT_DIR / "data" / "validation"
PROGRESS    = VAL_DIR / "fetch_v2_progress.json"

# ── Config ─────────────────────────────────────────────────────────────────────
# Default: BTC only, 4 years — matches existing OHLCV parquets.
# Override with --symbols and --years / --start-date on the CLI.
SYMBOLS      = ["BTC"]
YEARS        = 4
TIMEFRAMES   = ["1m", "5m", "15m", "1h", "4h"]
RESAMPLE_TFS = ["5m", "15m", "1h", "4h"]   # derived from 1m

# Explicit start timestamp (ms, UTC). Set via --start-date; None = use YEARS.
START_TS_MS: int | None = None

BYBIT_BASE   = "https://api.bybit.com"
SLEEP_BETWEEN_REQ = 1.5    # seconds (40 req/min)
MAX_RETRIES       = 5
RETRY_BACKOFF     = [2, 4, 8, 16, 32]    # seconds

# Bybit limit per request
BYBIT_KLINE_LIMIT = 1000   # max candles per API call

# Resample aggregation rules
RESAMPLE_RULES = {
    "open":   "first",
    "high":   "max",
    "low":    "min",
    "close":  "last",
    "volume": "sum",
}


# ══════════════════════════════════════════════════════════════════════════════
#  PROGRESS TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {}


def save_progress(prog: dict) -> None:
    PROGRESS.write_text(json.dumps(prog, indent=2))


def is_done(prog: dict, key: str) -> bool:
    return prog.get(key, False) is True


def mark_done(prog: dict, key: str) -> None:
    prog[key] = True
    save_progress(prog)


# ══════════════════════════════════════════════════════════════════════════════
#  BYBIT API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _bybit_get(path: str, params: dict, retry: int = 0) -> dict:
    """GET request to Bybit API with exponential backoff."""
    import requests
    url = BYBIT_BASE + path
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = RETRY_BACKOFF[min(retry, len(RETRY_BACKOFF) - 1)]
            logger.warning("Rate limited — sleeping %ds (retry %d)", wait, retry + 1)
            time.sleep(wait)
            return _bybit_get(path, params, retry + 1)
        if resp.status_code >= 500:
            if retry >= MAX_RETRIES:
                raise RuntimeError(f"Bybit server error {resp.status_code} after {MAX_RETRIES} retries")
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


def _bybit_tf(tf: str) -> str:
    """Convert our TF notation to Bybit interval parameter."""
    mapping = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
    return mapping[tf]


def fetch_bybit_ohlcv(symbol: str, tf: str, years: int = 3,
                      start_ts_ms: int | None = None) -> pd.DataFrame:
    """
    Fetch OHLCV from Bybit for a given symbol and timeframe.
    Fetches backwards from now, in chunks of 1000 candles.

    If start_ts_ms is provided it is used as the absolute start (ms UTC);
    otherwise the start is computed as now - years*365 days.
    """
    slug     = symbol + "USDT"
    interval = _bybit_tf(tf)
    end_ts   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = start_ts_ms if start_ts_ms is not None else \
               int((datetime.now(timezone.utc) - timedelta(days=years * 365)).timestamp() * 1000)

    all_rows = []
    chunk_end = end_ts
    total_calls = 0

    logger.info("[%s][%s] Fetching %d years from Bybit…", symbol, tf, years)

    while chunk_end > start_ts:
        params = {
            "category": "linear",
            "symbol":   slug,
            "interval": interval,
            "end":      chunk_end,
            "limit":    BYBIT_KLINE_LIMIT,
        }
        data = _bybit_get("/v5/market/kline", params)

        if data.get("retCode") != 0:
            msg = data.get("retMsg", "")
            # Bybit returns rate-limit errors as JSON 200 with retCode != 0.
            # Back off and retry rather than silently truncating the dataset.
            if any(k in msg.lower() for k in ("too many", "rate limit", "visit")):
                wait = 60
                logger.warning(
                    "[%s][%s] API rate limit (JSON-level): '%s' — sleeping %ds",
                    symbol, tf, msg, wait,
                )
                time.sleep(wait)
                continue   # retry same chunk_end
            logger.error("[%s][%s] API error: %s", symbol, tf, msg)
            break

        candles = data.get("result", {}).get("list", [])
        if not candles:
            break

        rows = []
        for c in candles:
            ts    = int(c[0])
            open_ = float(c[1])
            high  = float(c[2])
            low   = float(c[3])
            close = float(c[4])
            vol   = float(c[5])
            rows.append({"timestamp": ts, "open": open_, "high": high,
                          "low": low, "close": close, "volume": vol})

        all_rows.extend(rows)
        total_calls += 1

        # Next chunk ends at the earliest timestamp in this batch
        earliest = min(r["timestamp"] for r in rows)
        chunk_end = earliest - 1

        if total_calls % 20 == 0:
            dt = datetime.fromtimestamp(earliest / 1000, tz=timezone.utc)
            logger.info("[%s][%s] Progress: %s | rows so far: %d",
                        symbol, tf, dt.strftime("%Y-%m-%d"), len(all_rows))

        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        raise RuntimeError(f"No data fetched for {symbol} {tf}")

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    # Trim to start_ts
    cutoff = pd.Timestamp(start_ts, unit="ms", tz="UTC")
    df = df[df["timestamp"] >= cutoff].reset_index(drop=True)

    logger.info("[%s][%s] Fetched %d candles from %s to %s",
                symbol, tf, len(df),
                df["timestamp"].iloc[0].strftime("%Y-%m-%d"),
                df["timestamp"].iloc[-1].strftime("%Y-%m-%d"))
    return df


def fetch_bybit_funding(symbol: str, years: int = 3,
                        start_ts_ms: int | None = None) -> pd.DataFrame:
    """Fetch 8h funding rate history from Bybit.

    If start_ts_ms is provided it is used as the absolute start (ms UTC);
    otherwise the start is computed as now - years*365 days.
    """
    slug  = symbol + "USDT"
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = start_ts_ms if start_ts_ms is not None else \
               int((datetime.now(timezone.utc) - timedelta(days=years * 365)).timestamp() * 1000)

    all_rows = []
    chunk_end = end_ts
    prev_earliest: int | None = None   # infinite-loop guard
    page = 0
    MAX_PAGES = 5000                   # 5000 × 200 × 8h = ~11 years max
    logger.info("[%s] Fetching funding rates…", symbol)

    while chunk_end > start_ts and page < MAX_PAGES:
        params = {
            "category":  "linear",
            "symbol":    slug,
            "startTime": start_ts,
            "endTime":   chunk_end,
            "limit":     200,
        }
        data = _bybit_get("/v5/market/funding/history", params)
        if data.get("retCode") != 0:
            logger.error("[%s] Funding API error: %s", symbol, data.get("retMsg"))
            break

        items = data.get("result", {}).get("list", [])
        if not items:
            break

        rows = []
        for item in items:
            ts = int(item["fundingRateTimestamp"])
            rows.append({
                "timestamp":    ts,
                "funding_rate": float(item["fundingRate"]),
            })

        all_rows.extend(rows)
        earliest = min(r["timestamp"] for r in rows)

        # Guard: if earliest didn't move, the endpoint has no more history
        if earliest == prev_earliest:
            logger.info("[%s] Funding: reached end of available history at page %d", symbol, page)
            break
        prev_earliest = earliest

        chunk_end = earliest - 1
        page += 1
        if page % 50 == 0:
            logger.info("[%s] Funding page %d, earliest so far: %s", symbol, page,
                        pd.Timestamp(earliest, unit="ms", tz="UTC").isoformat())
        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        logger.warning("[%s] No funding data — using zeros", symbol)
        return pd.DataFrame(columns=["timestamp", "funding_rate"])

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    cutoff = pd.Timestamp(start_ts, unit="ms", tz="UTC")
    df = df[df["timestamp"] >= cutoff].reset_index(drop=True)
    logger.info("[%s] Funding: %d records", symbol, len(df))
    return df


def fetch_bybit_oi(symbol: str, years: int = 3,
                   start_ts_ms: int | None = None) -> pd.DataFrame:
    """Fetch open interest history (5m resolution) from Bybit.

    NOTE: Bybit's OI endpoint has a limited history window.  In practice
    ~2.5 years of 5m OI history has been observed (vs the nominal 730-day
    figure in the docs).  The loop exits as soon as the API returns empty
    results OR the earliest returned timestamp stops moving (end-of-history
    sentinel), whichever comes first.

    If start_ts_ms is provided it is used as the absolute start (ms UTC);
    otherwise the start is computed as now - years*365 days.
    """
    slug  = symbol + "USDT"
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = start_ts_ms if start_ts_ms is not None else \
               int((datetime.now(timezone.utc) - timedelta(days=years * 365)).timestamp() * 1000)

    all_rows = []
    chunk_end = end_ts
    prev_earliest: int | None = None   # infinite-loop guard
    page = 0
    # 5-min OI: 200 items × 5 min = 1000 min = ~16.7 h per page
    # 730 days / 0.694 days ≈ 1052 pages maximum; 2000 is a generous safety cap
    MAX_PAGES = 2000
    logger.info("[%s] Fetching open interest…", symbol)

    while chunk_end > start_ts and page < MAX_PAGES:
        params = {
            "category":    "linear",
            "symbol":      slug,
            "intervalTime": "5min",
            "startTime":   start_ts,
            "endTime":     chunk_end,
            "limit":       200,
        }
        data = _bybit_get("/v5/market/open-interest", params)
        if data.get("retCode") != 0:
            logger.error("[%s] OI API error: %s", symbol, data.get("retMsg"))
            break

        items = data.get("result", {}).get("list", [])
        if not items:
            logger.info("[%s] OI: empty response at page %d — end of available history", symbol, page)
            break

        rows = []
        for item in items:
            ts = int(item["timestamp"])
            rows.append({
                "timestamp": ts,
                "oi": float(item["openInterest"]),
            })

        all_rows.extend(rows)
        earliest = min(r["timestamp"] for r in rows)

        # Guard: if earliest didn't move, the endpoint has no more history
        if earliest == prev_earliest:
            logger.info("[%s] OI: reached end of available history at page %d", symbol, page)
            break
        prev_earliest = earliest

        chunk_end = earliest - 1
        page += 1
        if page % 50 == 0:
            logger.info("[%s] OI page %d, earliest so far: %s", symbol, page,
                        pd.Timestamp(earliest, unit="ms", tz="UTC").isoformat())
        time.sleep(SLEEP_BETWEEN_REQ)

    if not all_rows:
        logger.warning("[%s] No OI data — using zeros", symbol)
        return pd.DataFrame(columns=["timestamp", "oi"])

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    cutoff = pd.Timestamp(start_ts, unit="ms", tz="UTC")
    df = df[df["timestamp"] >= cutoff].reset_index(drop=True)
    logger.info("[%s] OI: %d records", symbol, len(df))
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  RESAMPLING
# ══════════════════════════════════════════════════════════════════════════════

def resample_ohlcv(df_1m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """
    Resample 1m OHLCV to target timeframe.
    df_1m must have 'timestamp' column (datetime, UTC-aware).
    Returns DataFrame with 'timestamp' column.
    """
    freq_map = {
        "5m":  "5min",
        "15m": "15min",
        "1h":  "1h",
        "4h":  "4h",
    }
    freq = freq_map.get(target_tf)
    if not freq:
        raise ValueError(f"Unknown target TF: {target_tf}")

    df = df_1m.set_index("timestamp").sort_index()
    df_rs = df.resample(freq).agg(RESAMPLE_RULES).dropna(subset=["close"])
    df_rs.index.name = "timestamp"
    result = df_rs.reset_index()
    logger.info("Resampled 1m → %s: %d bars", target_tf, len(result))
    return result


def validate_ohlcv(df: pd.DataFrame, symbol: str, tf: str) -> dict:
    """Basic data quality checks."""
    if df.empty:
        return {"ok": False, "error": "Empty DataFrame"}

    n          = len(df)
    first_ts   = df["timestamp"].iloc[0]
    last_ts    = df["timestamp"].iloc[-1]
    span_days  = (last_ts - first_ts).total_seconds() / 86400

    # Check for gaps (consecutive timestamps should differ by tf interval)
    tf_secs    = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    expected_s = tf_secs.get(tf, 60)
    diffs      = df["timestamp"].diff().dt.total_seconds().dropna()
    gap_mask   = diffs > expected_s * 2.5
    n_gaps     = int(gap_mask.sum())
    pct_gaps   = n_gaps / max(n, 1) * 100

    # NaN checks
    n_nan = int(df[["open", "high", "low", "close", "volume"]].isna().sum().sum())

    # Price sanity
    price_ok = (df["high"] >= df["low"]).all() and (df["close"] > 0).all()

    quality = {
        "ok":          pct_gaps < 5.0 and n_nan == 0 and price_ok,
        "n_bars":      n,
        "span_days":   round(span_days, 1),
        "first":       str(first_ts)[:10],
        "last":        str(last_ts)[:10],
        "n_gaps":      n_gaps,
        "pct_gaps":    round(pct_gaps, 2),
        "n_nan":       n_nan,
        "price_ok":    bool(price_ok),
    }

    status = "✅ OK" if quality["ok"] else "⚠️  WARN"
    logger.info("[%s][%s] %s — %d bars | %s → %s | gaps=%d (%.1f%%)",
                symbol, tf, status, n, quality["first"], quality["last"],
                n_gaps, pct_gaps)
    return quality


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_symbol(symbol: str, prog: dict, args) -> dict:
    """
    Full pipeline for one symbol:
      1. Ensure 1m data exists (fetch if missing or --force-refetch)
      2. Resample 1m → 5m, 15m, 1h, 4h
      3. Fetch/validate funding and OI
    Returns dict with quality report for all TFs.
    """
    slug    = symbol + "USDT"
    reports = {}

    # ── Step 1: 1m OHLCV ────────────────────────────────────────────────────
    path_1m = VAL_DIR / f"{slug}_1m.parquet"
    key_1m  = f"{slug}_1m_done"

    if is_done(prog, key_1m) and path_1m.exists() and not args.force_refetch:
        logger.info("[%s] 1m already exists (%s) — skipping fetch",
                    symbol, _human_size(path_1m.stat().st_size))
        df_1m = pd.read_parquet(path_1m)
        # Ensure timezone-aware timestamps
        if df_1m["timestamp"].dt.tz is None:
            df_1m["timestamp"] = df_1m["timestamp"].dt.tz_localize("UTC")
    else:
        if args.resample_only:
            if not path_1m.exists():
                logger.error("[%s] 1m not found and --resample-only is set — skipping", symbol)
                return {"error": "1m_not_found"}
            df_1m = pd.read_parquet(path_1m)
            if df_1m["timestamp"].dt.tz is None:
                df_1m["timestamp"] = df_1m["timestamp"].dt.tz_localize("UTC")
        else:
            logger.info("[%s] Fetching 1m OHLCV from Bybit…", symbol)
            df_1m = fetch_bybit_ohlcv(symbol, "1m", years=YEARS,
                                      start_ts_ms=START_TS_MS)
            # Write to a temp file first; only replace the target once the fetch
            # completes without error — prevents wiping good existing data on failure.
            path_1m_tmp = path_1m.with_suffix(".parquet.tmp")
            df_1m.to_parquet(path_1m_tmp, index=False)
            path_1m_tmp.replace(path_1m)
            logger.info("[%s] Saved 1m → %s (%s)",
                        symbol, path_1m.name, _human_size(path_1m.stat().st_size))
            mark_done(prog, key_1m)

    reports["1m"] = validate_ohlcv(df_1m, symbol, "1m")

    # ── Step 2: Resample to higher TFs ──────────────────────────────────────
    for tf in RESAMPLE_TFS:
        path_tf = VAL_DIR / f"{slug}_{tf}.parquet"
        key_tf  = f"{slug}_{tf}_done"

        if is_done(prog, key_tf) and path_tf.exists() and not args.force_resample:
            logger.info("[%s][%s] Already exists (%s) — skipping",
                        symbol, tf, _human_size(path_tf.stat().st_size))
            df_tf = pd.read_parquet(path_tf)
        else:
            df_tf = resample_ohlcv(df_1m, tf)
            df_tf.to_parquet(path_tf, index=False)
            logger.info("[%s][%s] Saved → %s (%s)",
                        symbol, tf, path_tf.name, _human_size(path_tf.stat().st_size))
            mark_done(prog, key_tf)

        reports[tf] = validate_ohlcv(df_tf, symbol, tf)

    # ── Step 3: Funding rates ────────────────────────────────────────────────
    path_fund = VAL_DIR / f"{slug}_funding.parquet"
    key_fund  = f"{slug}_funding_done"

    if is_done(prog, key_fund) and path_fund.exists() and not args.force_refetch:
        logger.info("[%s] Funding already exists — skipping", symbol)
    elif not args.resample_only:
        df_fund = fetch_bybit_funding(symbol, years=YEARS, start_ts_ms=START_TS_MS)
        df_fund.to_parquet(path_fund, index=False)
        logger.info("[%s] Saved funding → %s", symbol, path_fund.name)
        mark_done(prog, key_fund)
    else:
        logger.info("[%s] Skipping funding fetch (--resample-only)", symbol)

    # ── Step 4: Open interest ────────────────────────────────────────────────
    path_oi = VAL_DIR / f"{slug}_oi.parquet"
    key_oi  = f"{slug}_oi_done"

    if is_done(prog, key_oi) and path_oi.exists() and not args.force_refetch:
        logger.info("[%s] OI already exists — skipping", symbol)
    elif not args.resample_only:
        df_oi = fetch_bybit_oi(symbol, years=YEARS, start_ts_ms=START_TS_MS)
        df_oi.to_parquet(path_oi, index=False)
        logger.info("[%s] Saved OI → %s", symbol, path_oi.name)
        mark_done(prog, key_oi)
    else:
        logger.info("[%s] Skipping OI fetch (--resample-only)", symbol)

    return reports


def _human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def write_summary_report(all_reports: dict, path: Path) -> None:
    lines = [
        "=" * 70,
        "NexusTrader Historical Data — Fetch v2 Summary Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 70, "",
    ]
    for symbol, sym_reports in all_reports.items():
        lines.append(f"\n{symbol}USDT:")
        if "error" in sym_reports:
            lines.append(f"  ERROR: {sym_reports['error']}")
            continue
        for tf in TIMEFRAMES:
            r = sym_reports.get(tf, {})
            if not r:
                lines.append(f"  {tf:4s}: NOT RUN")
                continue
            ok = "✅" if r.get("ok") else "⚠️ "
            lines.append(
                f"  {tf:4s}: {ok} {r.get('n_bars', 0):>8,} bars | "
                f"{r.get('first', '?')} → {r.get('last', '?')} | "
                f"gaps={r.get('n_gaps', 0)} ({r.get('pct_gaps', 0):.1f}%)"
            )

    lines += ["", "=" * 70,
              "All files saved to: data/validation/",
              ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Summary report → %s", path)


def main():
    global YEARS  # declared first — used below in add_argument defaults

    parser = argparse.ArgumentParser(
        description="Fetch all timeframes for all symbols (NexusTrader historical data v2)"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=SYMBOLS,
        help=f"Symbols to process (default: {' '.join(SYMBOLS)})"
    )
    parser.add_argument(
        "--years", type=int, default=YEARS,
        help=f"Years of history to fetch (default: {YEARS})"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from previous run (uses fetch_v2_progress.json)"
    )
    parser.add_argument(
        "--resample-only", action="store_true",
        help="Skip all API calls; only resample existing 1m files to higher TFs"
    )
    parser.add_argument(
        "--force-refetch", action="store_true",
        help="Re-fetch 1m data even if it already exists"
    )
    parser.add_argument(
        "--force-resample", action="store_true",
        help="Re-resample even if higher-TF files already exist"
    )
    parser.add_argument(
        "--start-date", type=str, default=None,
        metavar="YYYY-MM-DD",
        help="Explicit start date for all fetches (overrides --years). "
             "E.g. --start-date 2022-03-25 fetches from that exact date to now."
    )
    args = parser.parse_args()

    YEARS = args.years

    # Resolve START_TS_MS from --start-date if provided
    global START_TS_MS
    if args.start_date:
        try:
            sd = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            START_TS_MS = int(sd.timestamp() * 1000)
            logger.info("Start date override: %s → %d ms", args.start_date, START_TS_MS)
        except ValueError:
            logger.error("Invalid --start-date format '%s', expected YYYY-MM-DD. Using --years instead.",
                         args.start_date)
            START_TS_MS = None

    VAL_DIR.mkdir(parents=True, exist_ok=True)

    prog = load_progress() if args.resume else {}
    if not args.resume:
        logger.info("Starting fresh run (use --resume to continue an interrupted run)")
    else:
        done_keys = [k for k, v in prog.items() if v]
        logger.info("Resuming — %d items already done", len(done_keys))

    logger.info("Symbols: %s", args.symbols)
    if START_TS_MS:
        effective_start = datetime.fromtimestamp(START_TS_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        logger.info("Start: %s (--start-date) | Timeframes: %s", effective_start, TIMEFRAMES)
    else:
        logger.info("Years: %d (≈ start %s) | Timeframes: %s",
                    args.years,
                    (datetime.now(timezone.utc) - timedelta(days=args.years * 365)).strftime("%Y-%m-%d"),
                    TIMEFRAMES)
    logger.info("Output: %s", VAL_DIR)

    if args.resample_only:
        logger.info("MODE: resample-only (no API calls)")
    else:
        logger.info("MODE: fetch + resample")

    # Install requests if needed
    try:
        import requests  # noqa
    except ImportError:
        logger.error("requests not installed. Run: pip install requests")
        return

    all_reports = {}
    start_time  = time.time()

    for symbol in args.symbols:
        logger.info("=" * 60)
        logger.info("Processing %s…", symbol)
        try:
            all_reports[symbol] = process_symbol(symbol, prog, args)
        except Exception as e:
            logger.error("[%s] FAILED: %s", symbol, e)
            all_reports[symbol] = {"error": str(e)}

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("All done in %.1f min", elapsed / 60)

    # Final inventory
    logger.info("\n── File inventory ──────────────────────────────")
    total_bytes = 0
    for sym in args.symbols:
        slug = sym + "USDT"
        for tf in TIMEFRAMES:
            p = VAL_DIR / f"{slug}_{tf}.parquet"
            if p.exists():
                sz = p.stat().st_size
                total_bytes += sz
                logger.info("  %-30s %s", p.name, _human_size(sz))
    logger.info("  Total: %s", _human_size(total_bytes))

    # Write summary report
    report_path = VAL_DIR / "fetch_v2_report.txt"
    write_summary_report(all_reports, report_path)

    logger.info("\nNext steps:")
    logger.info("  Check report:            data/validation/fetch_v2_report.txt")


if __name__ == "__main__":
    main()
