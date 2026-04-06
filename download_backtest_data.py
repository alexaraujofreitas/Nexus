"""
NexusTrader — Historical Data Downloader
========================================
Run this script ONCE on your Windows machine (inside the NexusTrader Python env).
It will download 4 years of OHLCV data from Binance (fallback: Bybit) and save
parquet files into backtest_data/ next to this script.

Usage:
    cd path/to/NexusTrader
    python download_backtest_data.py

Requirements (already in NexusTrader env):
    pip install ccxt pandas pyarrow

Expected runtime: 25–45 minutes including 5m (Binance), 40–70 minutes (Bybit fallback).
Expected output:  ~20 parquet files, ~400–700 MB total.
"""

import ccxt
import pandas as pd
import time
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Detect parquet support; fall back to CSV if pyarrow is missing
try:
    import pyarrow  # noqa
    USE_PARQUET = True
except ImportError:
    USE_PARQUET = False
    print("  ⚠  pyarrow not found — saving as CSV (run 'pip install pyarrow' for faster files)")
    print()

# ─── Configuration ─────────────────────────────────────────────
SYMBOLS     = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
    "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
    "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
]
TIMEFRAMES  = ["4h", "1h", "15m", "5m"]
YEARS_BACK  = 4
BATCH_SIZE  = 1000
RETRY_LIMIT = 5
RATE_SLEEP  = 0.15   # seconds between requests (well below limits)

SAVE_DIR = Path(__file__).parent / "backtest_data"
SAVE_DIR.mkdir(exist_ok=True)
# ───────────────────────────────────────────────────────────────

def get_since_ms(years: int) -> int:
    dt = datetime.now(timezone.utc) - timedelta(days=365 * years)
    return int(dt.timestamp() * 1000)

def make_exchange(exchange_id: str):
    ex = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "timeout": 30000,
    })
    # Try to load markets (validates connectivity)
    ex.load_markets()
    return ex

def download_full(exchange, symbol: str, tf: str, since_ms: int) -> pd.DataFrame:
    """Paginated OHLCV download with retry, returns DataFrame."""
    all_candles = []
    cur = since_ms

    while True:
        for attempt in range(RETRY_LIMIT):
            try:
                batch = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=BATCH_SIZE)
                break
            except ccxt.RateLimitExceeded:
                wait = 2 ** attempt
                print(f"    Rate limited, waiting {wait}s…")
                time.sleep(wait)
            except Exception as e:
                if attempt == RETRY_LIMIT - 1:
                    raise
                time.sleep(2)

        if not batch:
            break

        all_candles.extend(batch)
        # Progress tick
        last_dt = datetime.fromtimestamp(batch[-1][0] / 1000, tz=timezone.utc)
        print(f"    {len(all_candles):>7,} bars  (up to {last_dt.strftime('%Y-%m-%d')})", end="\r")

        if len(batch) < BATCH_SIZE:
            break
        cur = batch[-1][0] + 1
        time.sleep(RATE_SLEEP)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["ts","open","high","low","close","volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("dt").drop(columns=["ts"])
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df.astype(float)

def cache_path(symbol: str, tf: str) -> Path:
    ext = "parquet" if USE_PARQUET else "csv"
    return SAVE_DIR / f"{symbol.replace('/', '_')}_{tf}.{ext}"

def save_df(df: pd.DataFrame, path: Path):
    if USE_PARQUET:
        df.to_parquet(path)
    else:
        df.to_csv(path)

def load_df(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

def main():
    since_ms = get_since_ms(YEARS_BACK)
    since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
    print(f"\n{'='*60}")
    print(f"  NexusTrader Historical Data Downloader")
    print(f"  Target: {YEARS_BACK} years back → {since_dt.strftime('%Y-%m-%d')}")
    print(f"  Symbols: {', '.join(SYMBOLS)}")
    print(f"  TFs: {', '.join(TIMEFRAMES)}")
    print(f"  Save dir: {SAVE_DIR}")
    print(f"{'='*60}\n")

    # ── Try exchanges in order ─────────────────────────────────
    exchange_order = ["binance", "bybit"]
    exchange = None

    for ex_id in exchange_order:
        print(f"  Connecting to {ex_id}…", end=" ")
        try:
            exchange = make_exchange(ex_id)
            print(f"✓  ({ex_id})")
            used_exchange = ex_id
            break
        except Exception as e:
            print(f"✗  {e}")
            exchange = None

    if exchange is None:
        print("\n  ERROR: Could not connect to any exchange.")
        print("  → Ensure your internet connection is working.")
        print("  → Disable VPN or switch to a non-Japan exit node.")
        sys.exit(1)

    # ── Download loop ──────────────────────────────────────────
    total   = len(SYMBOLS) * len(TIMEFRAMES)
    done    = 0
    summary = {}

    for sym in SYMBOLS:
        summary[sym] = {}
        for tf in TIMEFRAMES:
            done += 1
            fpath = cache_path(sym, tf)
            # Also check alternate extension in case format changed
            alt_ext = "csv" if USE_PARQUET else "parquet"
            alt_path = SAVE_DIR / f"{sym.replace('/', '_')}_{tf}.{alt_ext}"
            found_path = fpath if fpath.exists() else (alt_path if alt_path.exists() else None)
            if found_path:
                df = load_df(found_path)
                print(f"  [{done}/{total}] CACHED  {sym:12s} {tf:4s}  "
                      f"{len(df):>8,} bars  "
                      f"{df.index[0].date()} → {df.index[-1].date()}")
                summary[sym][tf] = {
                    "bars": len(df), "from": str(df.index[0].date()),
                    "to": str(df.index[-1].date()), "source": "cache"
                }
                continue

            print(f"  [{done}/{total}] Downloading {sym:12s} {tf:4s}…")
            try:
                df = download_full(exchange, sym, tf, since_ms)
            except Exception as e:
                print(f"\n    ⚠  FAILED: {e}")
                summary[sym][tf] = {"error": str(e)}
                continue

            if df.empty:
                print(f"\n    ⚠  Empty response for {sym} {tf}")
                summary[sym][tf] = {"error": "empty"}
                continue

            save_df(df, fpath)
            print(f"\n    ✓  {len(df):,} bars saved  "
                  f"({df.index[0].date()} → {df.index[-1].date()})  "
                  f"→ {fpath.name}")
            summary[sym][tf] = {
                "bars": len(df), "from": str(df.index[0].date()),
                "to": str(df.index[-1].date()), "source": used_exchange
            }

    # ── Summary ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  DOWNLOAD COMPLETE — Summary")
    print(f"{'─'*60}")
    for sym, tfs in summary.items():
        for tf, info in tfs.items():
            if "error" in info:
                print(f"  {sym:12s} {tf:4s}  ⚠ FAILED: {info['error']}")
            else:
                print(f"  {sym:12s} {tf:4s}  {info['bars']:>8,} bars  "
                      f"{info['from']} → {info['to']}  [{info['source']}]")

    # Save summary
    import json
    summary_file = SAVE_DIR / "download_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Summary saved → {summary_file}")

    total_files = sum(1 for _ in SAVE_DIR.glob("*.parquet"))
    total_size  = sum(f.stat().st_size for f in SAVE_DIR.glob("*.parquet")) / (1024**2)
    print(f"  Files: {total_files}   Total size: {total_size:.0f} MB")
    print(f"{'='*60}\n")
    print("  ✅  Data ready. Claude can now run the full backtest.")
    print()

if __name__ == "__main__":
    main()
