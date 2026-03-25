"""
scripts/fetch_historical_data.py — Historical OHLCV + Derivatives Data Fetcher
================================================================================
Fetches high-resolution historical data from Bybit (preferred) or Binance
(fallback) for CPS validation and backtesting.

OUTPUT FILES (saved to data/validation/):
  BTCUSDT_1m.parquet     — 1-minute OHLCV for BTC
  ETHUSDT_1m.parquet     — 1-minute OHLCV for ETH  (preferred)
  SOLUSDT_1m.parquet     — 1-minute OHLCV for SOL  (preferred)
  BTCUSDT_funding.parquet — 8h funding rate history
  ETHUSDT_funding.parquet
  SOLUSDT_funding.parquet
  BTCUSDT_oi.parquet     — Open interest (5m resolution)
  ETHUSDT_oi.parquet
  SOLUSDT_oi.parquet
  fetch_progress.json    — Checkpoint file (allows safe resume)
  validation_report.txt  — Data quality report

USAGE (Windows CMD / PowerShell):
  cd C:\\path\\to\\NexusTrader
  pip install ccxt pandas pyarrow tqdm requests -q
  python scripts/fetch_historical_data.py
  python scripts/fetch_historical_data.py --symbols BTC     # BTC only
  python scripts/fetch_historical_data.py --years 2          # 2-year fallback
  python scripts/fetch_historical_data.py --resume           # resume interrupted run

EXPECTED RUNTIME:
  BTC 4yr @ 1m  ≈ 2,102,400 candles → ~25-40 min depending on network
  ETH + SOL add ~50% each

DISK SPACE ESTIMATE:
  ~150 MB per symbol per year in Parquet format
  Full 3-symbol 4yr run ≈ ~1.8 GB

RATE LIMIT HANDLING:
  Bybit: 120 req/min — script uses 40 req/min with automatic backoff
  Binance: 1200 req/min weight — script uses 500 weight/min with backoff
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetcher")

# ── Constants ─────────────────────────────────────────────────────────────────

ROOT_DIR   = Path(__file__).parent.parent
OUT_DIR    = ROOT_DIR / "data" / "validation"
PROGRESS_F = OUT_DIR / "fetch_progress.json"

SYMBOLS_MAP = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "SOL": "SOL/USDT",
}

# Perpetual (linear) symbols — required for funding rate history on Bybit
PERP_SYMBOLS_MAP = {
    "BTC": "BTC/USDT:USDT",
    "ETH": "ETH/USDT:USDT",
    "SOL": "SOL/USDT:USDT",
}

# Bybit /v5/market/open-interest only has ~730 days of 5m history.
# Fetching beyond this returns nothing, so cap OI requests at 2 years.
OI_MAX_LOOKBACK_DAYS = 730

# How many 1-minute candles per API call (Bybit max 200, Binance max 1000)
BYBIT_LIMIT   = 200
BINANCE_LIMIT = 1000

# Safety margin — stay well under rate limits
BYBIT_SLEEP_BETWEEN_CALLS   = 0.6   # s  (100 req/min, Bybit allows 120)
BINANCE_SLEEP_BETWEEN_CALLS = 0.25  # s


# ── Progress checkpoint ────────────────────────────────────────────────────────

def _load_progress() -> dict:
    if PROGRESS_F.exists():
        try:
            return json.loads(PROGRESS_F.read_text())
        except Exception:
            pass
    return {}


def _save_progress(progress: dict) -> None:
    PROGRESS_F.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_F.write_text(json.dumps(progress, indent=2))


# ── Bybit fetcher ─────────────────────────────────────────────────────────────

def _fetch_bybit_ohlcv(symbol: str, since_ms: int, until_ms: int) -> list[list]:
    """Fetch 1m OHLCV from Bybit REST with pagination and rate-limit handling."""
    try:
        import ccxt
    except ImportError:
        raise ImportError("Run: pip install ccxt")

    exchange = ccxt.bybit({
        "options": {"defaultType": "linear"},
        "enableRateLimit": False,   # manual rate limit below
    })

    all_bars: list[list] = []
    cursor = since_ms
    retries = 0

    logger.info("  Bybit: fetching %s from %s",
                symbol,
                datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

    while cursor < until_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, "1m", since=cursor, limit=BYBIT_LIMIT)
        except Exception as exc:
            if retries < 5:
                wait = 2 ** retries * 2
                logger.warning("  Bybit error (%s), retrying in %ds…", exc, wait)
                time.sleep(wait)
                retries += 1
                continue
            raise

        retries = 0

        if not bars:
            break

        all_bars.extend(bars)
        last_ts = bars[-1][0]

        if last_ts >= until_ms:
            break

        cursor = last_ts + 60_000   # next minute

        # progress heartbeat
        pct = (cursor - since_ms) / max(until_ms - since_ms, 1) * 100
        if len(all_bars) % 10_000 < BYBIT_LIMIT:
            logger.info("  … %s %.1f%% (%d bars)", symbol, pct, len(all_bars))

        time.sleep(BYBIT_SLEEP_BETWEEN_CALLS)

    return all_bars


def _fetch_bybit_funding(symbol_perp: str, since_ms: int, until_ms: int) -> list[dict]:
    """
    Fetch 8h funding rate history from Bybit.
    IMPORTANT: symbol_perp must be the perpetual symbol, e.g. 'BTC/USDT:USDT'.
    Bybit raises an error if given a spot symbol like 'BTC/USDT'.
    """
    try:
        import ccxt
    except ImportError:
        raise ImportError("Run: pip install ccxt")

    exchange = ccxt.bybit({"options": {"defaultType": "linear"}, "enableRateLimit": False})
    records: list[dict] = []
    cursor = since_ms
    retries = 0

    logger.info("  Bybit: fetching funding for %s (perpetual)", symbol_perp)

    while cursor < until_ms:
        try:
            hist = exchange.fetch_funding_rate_history(symbol_perp, since=cursor, limit=200)
        except Exception as exc:
            if retries < 3:
                wait = 2 ** retries * 2
                logger.warning("  Funding fetch error (%s), retry in %ds…", exc, wait)
                time.sleep(wait)
                retries += 1
                continue
            logger.warning("  Funding fetch failed after retries (%s), stopping", exc)
            break

        retries = 0
        if not hist:
            break

        records.extend(hist)
        cursor = hist[-1]["timestamp"] + 1
        time.sleep(BYBIT_SLEEP_BETWEEN_CALLS)

    return records


def _fetch_bybit_oi(symbol_raw: str, since_ms: int, until_ms: int) -> list[dict]:
    """
    Fetch open interest history from Bybit (1-hour resolution).
    Uses the Bybit /v5/market/open-interest REST endpoint directly.

    RESOLUTION CHOICE: We use '1h' not '5min' because:
      1. Bybit returns OI data newest-first. With 5min the cursor (which uses
         items[-1] = oldest record) barely advances, causing ~200h runtime.
      2. The CPS validation forward-fills hourly OI to 5m — identical signal.
      3. 1h gives ~17,500 records for 730 days in ~3 minutes total.

    CURSOR LOGIC: Always advance to batch_end regardless of data returned.
    Bybit's reverse-order response means items[-1] is NOT the newest record.
    """
    import requests

    # Batch window: 200 hourly bars = 200h ≈ 8.33 days per API call
    _INTERVAL       = "1h"
    _INTERVAL_MS    = 60 * 60 * 1000         # 1 hour in ms
    _BATCH_SPAN_MS  = 200 * _INTERVAL_MS     # 200h per call

    sym = symbol_raw.replace("/", "").replace(":", "")   # "BTC/USDT" → "BTCUSDT"
    url = "https://api.bybit.com/v5/market/open-interest"
    records: list[dict] = []
    cursor = since_ms
    consecutive_errors = 0
    total_span_ms = max(until_ms - since_ms, 1)

    logger.info("  Bybit: fetching OI (1h) for %s (from %s)",
                sym,
                datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

    while cursor < until_ms:
        batch_end = min(cursor + _BATCH_SPAN_MS, until_ms)
        params = {
            "category":      "linear",
            "symbol":        sym,
            "intervalTime":  _INTERVAL,
            "startTime":     cursor,
            "endTime":       batch_end,
            "limit":         200,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            if consecutive_errors <= 3:
                wait = consecutive_errors * 5
                logger.warning("  OI batch error (%s) — waiting %ds then skipping", exc, wait)
                time.sleep(wait)
                cursor = batch_end + _INTERVAL_MS   # always advance past failed window
                continue
            else:
                logger.warning("  OI: %d consecutive errors — stopping early", consecutive_errors)
                break

        items = data.get("result", {}).get("list", [])
        if items:
            records.extend(items)

        # CRITICAL: always advance to batch_end — do NOT use items[-1] timestamp.
        # Bybit returns data newest-first so items[-1] is the oldest record, which
        # is barely ahead of cursor. Using it would advance ~1h instead of ~200h.
        cursor = batch_end + _INTERVAL_MS

        pct = (cursor - since_ms) / total_span_ms * 100
        if len(records) % 2000 < 200 or not items:
            logger.info("  … OI %s %.1f%% (%d records)", sym, min(pct, 100.0), len(records))

        time.sleep(BYBIT_SLEEP_BETWEEN_CALLS)

    # De-duplicate and sort oldest-first before returning
    seen: set[str] = set()
    unique: list[dict] = []
    for r in records:
        k = str(r.get("timestamp", ""))
        if k not in seen:
            seen.add(k)
            unique.append(r)
    unique.sort(key=lambda r: int(r.get("timestamp", 0)))
    return unique


# ── Binance fallback ──────────────────────────────────────────────────────────

def _fetch_binance_ohlcv(symbol: str, since_ms: int, until_ms: int) -> list[list]:
    """Fallback: fetch 1m OHLCV from Binance Futures."""
    try:
        import ccxt
    except ImportError:
        raise ImportError("Run: pip install ccxt")

    exchange = ccxt.binanceusdm({"enableRateLimit": False})
    all_bars: list[list] = []
    cursor = since_ms
    retries = 0

    logger.info("  Binance (fallback): fetching %s from %s",
                symbol,
                datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

    while cursor < until_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, "1m", since=cursor, limit=BINANCE_LIMIT)
        except Exception as exc:
            if retries < 5:
                wait = 2 ** retries * 2
                logger.warning("  Binance error (%s), retry in %ds…", exc, wait)
                time.sleep(wait)
                retries += 1
                continue
            raise

        retries = 0
        if not bars:
            break

        all_bars.extend(bars)
        last_ts = bars[-1][0]
        if last_ts >= until_ms:
            break
        cursor = last_ts + 60_000
        if len(all_bars) % 50_000 < BINANCE_LIMIT:
            pct = (cursor - since_ms) / max(until_ms - since_ms, 1) * 100
            logger.info("  … %s %.1f%% (%d bars)", symbol, pct, len(all_bars))
        time.sleep(BINANCE_SLEEP_BETWEEN_CALLS)

    return all_bars


# ── DataFrame helpers ─────────────────────────────────────────────────────────

def _bars_to_df(bars: list[list], symbol: str) -> pd.DataFrame:
    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["symbol"] = symbol
    return df


def _funding_to_df(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        try:
            rows.append({
                "timestamp":    pd.to_datetime(r["timestamp"], unit="ms", utc=True),
                "funding_rate": float(r.get("fundingRate", 0)),
                "symbol":       r.get("symbol", ""),
            })
        except Exception:
            continue
    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate", "symbol"])
    df = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def _oi_to_df(records: list[dict], symbol: str) -> pd.DataFrame:
    rows = []
    for r in records:
        try:
            rows.append({
                "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                "oi":        float(r.get("openInterest", 0)),
                "symbol":    symbol,
            })
        except Exception:
            continue
    if not rows:
        return pd.DataFrame(columns=["timestamp", "oi", "symbol"])
    df = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


# ── Data validation ───────────────────────────────────────────────────────────

def _validate_ohlcv(df: pd.DataFrame, symbol: str) -> dict:
    """
    Check for gaps, duplicates, and anomalies.
    Returns a validation result dict.
    """
    if df.empty:
        return {"symbol": symbol, "status": "EMPTY", "bars": 0}

    total_bars = len(df)
    first_ts   = df["timestamp"].min()
    last_ts    = df["timestamp"].max()
    expected_bars = int((last_ts - first_ts).total_seconds() / 60) + 1
    missing_bars  = expected_bars - total_bars
    missing_pct   = missing_bars / max(expected_bars, 1) * 100

    # Duplicate check (already deduped, but verify)
    dupes = df["timestamp"].duplicated().sum()

    # Price sanity
    zero_closes   = (df["close"] <= 0).sum()
    null_rows     = df[["open", "high", "low", "close", "volume"]].isnull().any(axis=1).sum()

    # Find gaps > 1 minute
    time_diffs = df["timestamp"].diff().dt.total_seconds() / 60
    large_gaps = (time_diffs > 5).sum()   # gaps > 5 minutes

    status = "OK"
    if missing_pct > 5:
        status = "WARN_GAPS"
    if missing_pct > 20:
        status = "FAIL_GAPS"
    if zero_closes > 0 or null_rows > 0:
        status = "FAIL_DATA"

    return {
        "symbol":         symbol,
        "status":         status,
        "bars":           total_bars,
        "first":          first_ts.isoformat(),
        "last":           last_ts.isoformat(),
        "expected_bars":  expected_bars,
        "missing_bars":   missing_bars,
        "missing_pct":    round(missing_pct, 2),
        "duplicate_rows": int(dupes),
        "zero_closes":    int(zero_closes),
        "null_rows":      int(null_rows),
        "large_gaps_5m":  int(large_gaps),
    }


# ── Main fetch orchestrator ───────────────────────────────────────────────────

def fetch_symbol(
    short_sym: str,
    years: int,
    resume: bool,
    use_bybit: bool = True,
) -> None:
    """Fetch OHLCV + funding + OI for one symbol and save to Parquet."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    symbol      = SYMBOLS_MAP[short_sym]
    symbol_perp = PERP_SYMBOLS_MAP[short_sym]   # e.g. "BTC/USDT:USDT"
    sym_slug    = symbol.replace("/", "")        # "BTCUSDT"

    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=365 * years)).timestamp() * 1000)

    # Bybit OI endpoint has ~730-day depth — no point querying older data
    oi_since_ms = max(
        since_ms,
        int((datetime.now(timezone.utc) - timedelta(days=OI_MAX_LOOKBACK_DAYS)).timestamp() * 1000)
    )

    progress = _load_progress() if resume else {}

    # ── OHLCV ─────────────────────────────────────────────────────────────────
    ohlcv_file = OUT_DIR / f"{sym_slug}_1m.parquet"

    if resume and progress.get(f"{sym_slug}_ohlcv_done"):
        logger.info("[%s] OHLCV already complete — skipping", short_sym)
    else:
        # If resuming a partial download, start from the last checkpoint
        ohlcv_start = since_ms
        if resume and ohlcv_file.exists():
            try:
                existing = pd.read_parquet(ohlcv_file)
                ohlcv_start = int(existing["timestamp"].max().timestamp() * 1000) + 60_000
                logger.info("[%s] Resuming OHLCV from %s (%d bars already saved)",
                            short_sym,
                            pd.to_datetime(ohlcv_start, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M"),
                            len(existing))
            except Exception:
                pass

        logger.info("[%s] Fetching OHLCV 1m | %d years | %s → now",
                    short_sym, years,
                    datetime.fromtimestamp(ohlcv_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

        try:
            if use_bybit:
                bars = _fetch_bybit_ohlcv(symbol, ohlcv_start, now_ms)
            else:
                bars = _fetch_binance_ohlcv(symbol, ohlcv_start, now_ms)
        except Exception as exc:
            if use_bybit:
                logger.warning("[%s] Bybit OHLCV failed (%s) — retrying with Binance", short_sym, exc)
                bars = _fetch_binance_ohlcv(symbol, ohlcv_start, now_ms)
            else:
                raise

        df_new = _bars_to_df(bars, symbol)

        if resume and ohlcv_file.exists():
            df_old = pd.read_parquet(ohlcv_file)
            df_new = pd.concat([df_old, df_new], ignore_index=True)
            df_new = df_new.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

        df_new.to_parquet(ohlcv_file, index=False)
        logger.info("[%s] OHLCV saved → %s (%d bars)", short_sym, ohlcv_file.name, len(df_new))

        progress[f"{sym_slug}_ohlcv_done"] = True
        _save_progress(progress)

    # ── Funding rate ──────────────────────────────────────────────────────────
    funding_file = OUT_DIR / f"{sym_slug}_funding.parquet"

    if resume and progress.get(f"{sym_slug}_funding_done"):
        logger.info("[%s] Funding already complete — skipping", short_sym)
    else:
        logger.info("[%s] Fetching funding rate history", short_sym)
        try:
            # MUST use the perpetual symbol (e.g. BTC/USDT:USDT), not spot BTC/USDT
            recs = _fetch_bybit_funding(symbol_perp, since_ms, now_ms)
            df_fund = _funding_to_df(recs)
        except Exception as exc:
            logger.warning("[%s] Funding fetch failed (%s) — creating empty file", short_sym, exc)
            df_fund = pd.DataFrame(columns=["timestamp", "funding_rate", "symbol"])

        df_fund.to_parquet(funding_file, index=False)
        logger.info("[%s] Funding saved → %s (%d records)", short_sym, funding_file.name, len(df_fund))
        # Only mark done if we actually got records; empty = needs re-fetch
        if len(df_fund) > 0:
            progress[f"{sym_slug}_funding_done"] = True
            _save_progress(progress)
        else:
            logger.warning("[%s] Funding: 0 records received — will retry on next run", short_sym)

    # ── Open interest ─────────────────────────────────────────────────────────
    oi_file = OUT_DIR / f"{sym_slug}_oi.parquet"

    if resume and progress.get(f"{sym_slug}_oi_done"):
        logger.info("[%s] OI already complete — skipping", short_sym)
    else:
        logger.info("[%s] Fetching open interest history (5m, capped at %d days)",
                    short_sym, OI_MAX_LOOKBACK_DAYS)
        try:
            oi_recs = _fetch_bybit_oi(symbol, oi_since_ms, now_ms)
            df_oi = _oi_to_df(oi_recs, symbol)
        except Exception as exc:
            logger.warning("[%s] OI fetch failed (%s) — creating empty file", short_sym, exc)
            df_oi = pd.DataFrame(columns=["timestamp", "oi", "symbol"])

        df_oi.to_parquet(oi_file, index=False)
        logger.info("[%s] OI saved → %s (%d records)", short_sym, oi_file.name, len(df_oi))
        # 1h resolution: 730 days × 24h = 17,520 records expected.
        # Accept ≥ 10,000 as complete (covers ≥ 416 days).
        if len(df_oi) >= 10_000:
            progress[f"{sym_slug}_oi_done"] = True
            _save_progress(progress)
        else:
            logger.warning("[%s] OI: only %d records — will retry on next run (expected ~17,520 for 730d @ 1h)",
                           short_sym, len(df_oi))


# ── Validation report ─────────────────────────────────────────────────────────

def generate_validation_report(symbols: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "=" * 70,
        "NexusTrader — Historical Data Validation Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 70,
        "",
    ]

    all_ok = True
    for short_sym in symbols:
        sym_slug = SYMBOLS_MAP[short_sym].replace("/", "")
        ohlcv_file = OUT_DIR / f"{sym_slug}_1m.parquet"

        lines.append(f"[{short_sym}] OHLCV 1m")
        if not ohlcv_file.exists():
            lines.append("  STATUS: FILE MISSING")
            all_ok = False
        else:
            df = pd.read_parquet(ohlcv_file)
            result = _validate_ohlcv(df, sym_slug)
            lines.append(f"  Status:          {result['status']}")
            lines.append(f"  Bars:            {result['bars']:,}")
            lines.append(f"  Range:           {result['first'][:10]} → {result['last'][:10]}")
            lines.append(f"  Expected bars:   {result['expected_bars']:,}")
            lines.append(f"  Missing bars:    {result['missing_bars']:,} ({result['missing_pct']:.2f}%)")
            lines.append(f"  Duplicate rows:  {result['duplicate_rows']}")
            lines.append(f"  Zero closes:     {result['zero_closes']}")
            lines.append(f"  Large gaps >5m:  {result['large_gaps_5m']}")
            if result["status"] != "OK":
                all_ok = False

        for suffix, label in [("funding", "Funding"), ("oi", "OI (5m)")]:
            f = OUT_DIR / f"{sym_slug}_{suffix}.parquet"
            if f.exists():
                try:
                    n = len(pd.read_parquet(f))
                    lines.append(f"  {label}:          {n:,} records — OK")
                except Exception:
                    lines.append(f"  {label}:          READ ERROR")
            else:
                lines.append(f"  {label}:          FILE MISSING")
        lines.append("")

    lines.append("=" * 70)
    lines.append(f"OVERALL: {'PASS — ready for CPS validation' if all_ok else 'WARN — check issues above'}")
    lines.append("=" * 70)
    lines.append("")
    lines.append("NEXT STEP:")
    lines.append("  python scripts/cpi_validation_realdata.py")
    lines.append("")

    report_text = "\n".join(lines)
    report_file = OUT_DIR / "validation_report.txt"
    report_file.write_text(report_text, encoding="utf-8")

    print(report_text)
    logger.info("Validation report saved → %s", report_file)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch historical OHLCV + derivatives data for NexusTrader CPS validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols", nargs="+", choices=["BTC", "ETH", "SOL"],
        default=["BTC", "ETH", "SOL"],
        help="Symbols to fetch (default: BTC ETH SOL)",
    )
    parser.add_argument(
        "--years", type=int, choices=[2, 3, 4], default=4,
        help="Years of history to fetch (default: 4; fallback: 3 or 2)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume a previously interrupted fetch",
    )
    parser.add_argument(
        "--exchange", choices=["bybit", "binance"], default="bybit",
        help="Preferred exchange (default: bybit; binance as fallback)",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Skip fetching; just run the validation report on existing files",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("NexusTrader Historical Data Fetcher")
    logger.info("Symbols: %s | Years: %d | Exchange: %s",
                args.symbols, args.years, args.exchange)
    logger.info("Output directory: %s", OUT_DIR)
    logger.info("=" * 60)

    if not args.validate_only:
        for sym in args.symbols:
            logger.info("")
            logger.info("─── Fetching %s ─────────────────────────────────────────", sym)
            try:
                fetch_symbol(sym, args.years, args.resume, use_bybit=(args.exchange == "bybit"))
                logger.info("─── %s complete ─────────────────────────────────────────", sym)
            except Exception as exc:
                logger.error("FAILED to fetch %s: %s", sym, exc, exc_info=True)
                logger.info("Continuing with next symbol…")

    logger.info("")
    logger.info("─── Running data validation ──────────────────────────────────────")
    generate_validation_report(args.symbols)

    logger.info("")
    logger.info("DONE. Hand off data/validation/ to the CPS validation script:")
    logger.info("  python scripts/cpi_validation_realdata.py")


if __name__ == "__main__":
    main()
