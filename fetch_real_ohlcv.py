#!/usr/bin/env python
"""
Fetch real historical OHLCV data from Bybit (public, no API key needed)
for Phase 5b backtesting validation.

Fetches 1H candles for 20 symbols covering ~6 months (Sep 2024 – Feb 2025).
This period covers:
  - Sep-Oct 2024: sideways / accumulation (BTC ~58-65k)
  - Nov 2024: strong bull trend (BTC 65k → 98k)
  - Dec 2024: high volatility / distribution (BTC 90-108k)
  - Jan 2025: ranging / correction (BTC 90-105k)
  - Feb 2025: bear trend / selloff (BTC 105k → 84k)

Saves to data/real_ohlcv/ as CSV files.
"""
import os
import sys
import time
import ccxt
import pandas as pd

sys.path.insert(0, ".")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "TRX/USDT", "DOGE/USDT", "ADA/USDT", "BCH/USDT", "HYPE/USDT",
    "LINK/USDT", "XLM/USDT", "AVAX/USDT", "HBAR/USDT", "SUI/USDT",
    "NEAR/USDT", "ICP/USDT", "ONDO/USDT", "ALGO/USDT", "RENDER/USDT",
]
TIMEFRAME = "1h"
# Sep 1, 2024 to Feb 28, 2025 (~6 months, ~4320 1H bars per symbol)
SINCE = int(pd.Timestamp("2024-09-01 00:00:00+00:00").timestamp() * 1000)
UNTIL = int(pd.Timestamp("2025-02-28 23:59:59+00:00").timestamp() * 1000)

OUT_DIR = "data/real_ohlcv"


def fetch_all_ohlcv(exchange, symbol, timeframe, since, until):
    """Paginated fetch of all OHLCV candles between since and until."""
    all_candles = []
    current_since = since
    limit = 1000  # Bybit max per request

    while current_since < until:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=limit)
        except Exception as e:
            print(f"  Error fetching {symbol} at {current_since}: {e}")
            time.sleep(2)
            continue

        if not candles:
            break

        # Filter to only candles before 'until'
        candles = [c for c in candles if c[0] < until]
        if not candles:
            break

        all_candles.extend(candles)
        last_ts = candles[-1][0]

        if last_ts <= current_since:
            break  # No progress
        current_since = last_ts + 1  # Next batch starts after last candle

        time.sleep(0.3)  # Rate limit courtesy

    return all_candles


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # Use Bybit public API (no keys needed for OHLCV)
    exchange = ccxt.bybit({"enableRateLimit": True})
    exchange.load_markets()

    for sym in SYMBOLS:
        print(f"Fetching {sym} {TIMEFRAME} from Bybit...")
        candles = fetch_all_ohlcv(exchange, sym, TIMEFRAME, SINCE, UNTIL)
        if not candles:
            print(f"  WARNING: No candles returned for {sym}")
            continue

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        df = df[~df.index.duplicated(keep="first")]
        df = df.sort_index()

        fname = sym.replace("/", "_") + f"_{TIMEFRAME}.csv"
        fpath = os.path.join(OUT_DIR, fname)
        df.to_csv(fpath)

        date_range = f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}"
        print(f"  {sym}: {len(df)} bars, {date_range}")
        print(f"  Saved to {fpath}")

    print("\nDone! All data saved to", OUT_DIR)


if __name__ == "__main__":
    main()
