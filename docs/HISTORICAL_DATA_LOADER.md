# HistoricalDataLoader — Usage Guide

## Overview

The `HistoricalDataLoader` module (`core/backtesting/data_loader.py`) provides a production-ready solution for fetching real OHLCV candle data from exchanges for backtesting and analysis.

**Key Features:**
- Fetches real data from Bybit (primary) with Binance fallback
- Comprehensive data validation (gaps, duplicates, NaN detection)
- Automatic pagination for large date ranges
- Rate limit handling (0.5s sleep between requests)
- Timeout protection (15s per request)
- Metadata recording (data source, gaps, duplicates, fetch duration)
- Thread-safe singleton instance

**Critical Guarantee:** Never returns synthetic, mocked, or fabricated data. All candles come directly from real exchange APIs.

---

## Quick Start

### Basic Usage

```python
from core.backtesting.data_loader import HistoricalDataLoader

# Create a loader instance
loader = HistoricalDataLoader()

# Fetch BTC/USDT daily data for 2024
df = loader.fetch_ohlcv(
    symbol="BTC/USDT",
    timeframe="1d",
    start_date="2024-01-01",
    end_date="2024-12-31",
    min_bars=300  # Require at least 300 bars
)

# DataFrame has index=timestamp (UTC), columns=[open, high, low, close, volume]
print(f"Fetched {len(df)} candles")
print(df.head())
```

### Using the Singleton

```python
from core.backtesting.data_loader import get_historical_data_loader

# Get the global singleton instance
loader = get_historical_data_loader()

# Use it normally
df = loader.fetch_ohlcv("ETH/USDT", "4h", "2023-01-01", "2024-01-01")
```

### With Backtester

```python
from core.backtesting import HistoricalDataLoader, IDSSBacktester

# Fetch data
loader = HistoricalDataLoader()
df = loader.fetch_ohlcv("SOL/USDT", "1h", "2024-01-01", "2024-03-01", min_bars=300)

# Add indicators (required for IDSS pipeline)
from core.indicators.indicator_library import IndicatorLibrary
ind_lib = IndicatorLibrary()
df = ind_lib.add_all_indicators(df, timeframe="1h")

# Run backtest
backtester = IDSSBacktester(min_confluence_score=0.45)
result = backtester.run(df, symbol="SOL/USDT", timeframe="1h")

print(f"Trades: {result['num_trades']}")
print(f"Win rate: {result['win_rate']:.1%}")
```

---

## API Reference

### `HistoricalDataLoader(use_exchange_manager=True)`

Initialize a data loader.

**Parameters:**
- `use_exchange_manager` (bool): If `True`, attempts to use `exchange_manager` for the primary source. If `False`, forces Bybit via ccxt. Default: `True`.

**Example:**
```python
# Use active exchange from exchange_manager (recommended)
loader = HistoricalDataLoader()

# Force Bybit directly
loader = HistoricalDataLoader(use_exchange_manager=False)
```

---

### `fetch_ohlcv(symbol, timeframe, start_date, end_date, min_bars=300)`

Fetch OHLCV candles from primary source (Bybit) with Binance fallback.

**Parameters:**
- `symbol` (str): Trading pair (e.g., `"BTC/USDT"`, `"ETH/USDT"`)
- `timeframe` (str): Candle interval
  - Supported: `"1m"`, `"5m"`, `"15m"`, `"30m"`, `"1h"`, `"4h"`, `"1d"`
- `start_date` (str): ISO format start date (e.g., `"2024-01-01"`)
- `end_date` (str): ISO format end date (e.g., `"2024-12-31"`)
- `min_bars` (int): Minimum bars required. Raises `InsufficientDataError` if not met. Default: `300`

**Returns:**
- `pd.DataFrame`: DataFrame with index=timestamp (UTC), columns=[open, high, low, close, volume]

**Raises:**
- `ValueError`: If timeframe is invalid or start_date >= end_date
- `InsufficientDataError`: If fetched bars < min_bars after all attempts

**Example:**
```python
df = loader.fetch_ohlcv(
    symbol="BTC/USDT",
    timeframe="1h",
    start_date="2024-01-01",
    end_date="2024-01-31",
    min_bars=500
)

# Guaranteed to have at least 500 bars (or raises InsufficientDataError)
assert len(df) >= 500
assert df.index.tz is not None  # UTC timezone
assert all(col in df.columns for col in ["open", "high", "low", "close", "volume"])
```

---

### `get_available_range(symbol, timeframe)`

Query the earliest and latest available data for a symbol/timeframe pair.

**Parameters:**
- `symbol` (str): Trading pair
- `timeframe` (str): Candle interval

**Returns:**
- `tuple[datetime, datetime]`: (earliest, latest) timestamps in UTC

**Example:**
```python
earliest, latest = loader.get_available_range("BTC/USDT", "1d")
print(f"Available data: {earliest} to {latest}")
```

---

### `get_last_fetch_info()`

Return metadata from the most recent `fetch_ohlcv()` call.

**Returns:**
- `DataSourceInfo`: Object containing metadata, or `None` if no fetch has been made

**DataSourceInfo Fields:**
- `primary_source` (str): `"bybit"` or `"binance"`
- `fallback_used` (bool): Whether fallback was used
- `fallback_source` (str): `""` or `"binance"`
- `fallback_reason` (str): Explanation if fallback was used
- `total_bars` (int): Total candles fetched
- `date_range_start` (str): ISO format
- `date_range_end` (str): ISO format
- `gaps_found` (int): Number of gaps larger than 3× timeframe
- `duplicates_removed` (int): Duplicate timestamps detected
- `fetch_duration_s` (float): Total fetch time in seconds

**Example:**
```python
df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-01-31")

info = loader.get_last_fetch_info()
print(f"Source: {info.primary_source}")
print(f"Bars: {info.total_bars}")
print(f"Fallback used: {info.fallback_used}")
print(f"Duration: {info.fetch_duration_s:.2f}s")
if info.gaps_found > 0:
    print(f"Warning: {info.gaps_found} gaps found")
```

---

## Exception Handling

### `InsufficientDataError`

Raised when data cannot meet the `min_bars` requirement.

**Attributes:**
- `bars_needed` (int): Minimum bars required
- `bars_available` (int): Bars actually fetched
- `source` (str): Which source was attempted

**Example:**
```python
from core.backtesting.data_loader import InsufficientDataError

try:
    df = loader.fetch_ohlcv("ALTCOIN/USDT", "5m", "2020-01-01", "2024-01-01", min_bars=1000)
except InsufficientDataError as e:
    print(f"Need {e.bars_needed}, got {e.bars_available} from {e.source}")
    # Handle gracefully: e.g., use a different symbol or timeframe
```

---

## Validation and Data Quality

The loader performs automatic validation on all fetched data:

### Checks Performed
1. **Sufficient bars**: `len(candles) >= min_bars`
2. **No duplicates**: Each timestamp appears only once
3. **Chronological order**: Timestamps strictly increasing
4. **No gaps > 3× timeframe**: Allows for market closes, but flags larger gaps
5. **No NaN values**: All OHLCV values are numeric
6. **Positive prices**: Open, High, Low, Close > 0
7. **Non-negative volume**: Volume >= 0

### Example: Inspecting Validation Results

The `fetch_ohlcv()` method will raise `InsufficientDataError` with detailed issue messages if validation fails.

```python
from core.backtesting.data_loader import InsufficientDataError

try:
    df = loader.fetch_ohlcv("BTC/USDT", "1d", "2024-01-01", "2024-12-31", min_bars=365)
except InsufficientDataError as e:
    print(f"Validation failed: {e}")
    # Output might be:
    # "Insufficient data for BTC/USDT/1d: needed 365, got 250.
    #  Issues: Found 2 gaps larger than 259200000ms; Found 1 duplicate timestamps"
```

---

## Performance Characteristics

### Fetch Speed
- **1h timeframe, 1 month data**: ~2-3 seconds
- **4h timeframe, 1 year data**: ~1-2 seconds
- **1d timeframe, 5 years data**: ~1-2 seconds

(Actual times depend on network, exchange load, and system performance)

### Rate Limiting
- Sleep 0.5s between requests to respect exchange rate limits
- Bybit allows ~200-1000 candles per request depending on timeframe
- Binance similar limits

### Timeout Protection
- Each request has a 15-second timeout
- If Bybit hangs, falls back to Binance
- If both hang, raises `InsufficientDataError`

---

## Common Patterns

### Pattern 1: Backtest Multiple Symbols

```python
loader = HistoricalDataLoader()
symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

results = {}
for symbol in symbols:
    try:
        df = loader.fetch_ohlcv(
            symbol, "1h", "2024-01-01", "2024-03-01", min_bars=300
        )
        # Process...
        results[symbol] = len(df)
    except InsufficientDataError as e:
        print(f"Skipping {symbol}: {e}")

print(f"Successfully loaded {len(results)}/{len(symbols)} symbols")
```

### Pattern 2: Incremental Data Fetch

```python
from datetime import datetime, timedelta

loader = HistoricalDataLoader()
start = datetime(2024, 1, 1)
end = datetime(2024, 1, 7)

while start < end:
    period_end = min(start + timedelta(days=1), end)
    df = loader.fetch_ohlcv(
        "BTC/USDT", "1h",
        start.strftime("%Y-%m-%d"),
        period_end.strftime("%Y-%m-%d"),
        min_bars=20  # Smaller per-period min
    )
    print(f"{start.date()}: {len(df)} bars")
    start = period_end
```

### Pattern 3: Cache and Reuse

```python
import pickle

loader = HistoricalDataLoader()

# Fetch once
df = loader.fetch_ohlcv("BTC/USDT", "1d", "2020-01-01", "2024-01-01", min_bars=1000)

# Save to disk
with open("btc_daily.pkl", "wb") as f:
    pickle.dump(df, f)

# Reuse later
with open("btc_daily.pkl", "rb") as f:
    df = pickle.load(f)
```

---

## Troubleshooting

### Problem: "Insufficient data for BTC/USDT/5m: needed 300, got 0"

**Cause:** Both Bybit and Binance failed to return any data.

**Solutions:**
1. Check your internet connection
2. Verify the symbol exists on the exchange (e.g., use `exchange_manager.get_symbols()`)
3. Check exchange API status at https://status.bybit.com or https://www.binancestatus.com
4. Try a different date range

### Problem: "Found gaps larger than Xms"

**Cause:** Market was closed (weekends, holidays) or data has gaps.

**Solutions:**
1. This is often expected. Use a larger `min_bars` to compensate for missing days
2. Check the date range — weekends have no crypto trading
3. For forex/stocks, avoid weekends and holidays

### Problem: "Found N duplicate timestamps"

**Cause:** Exchange returned multiple candles with the same timestamp (rare but possible during API issues).

**Solutions:**
1. Retry the fetch
2. Use a different timeframe
3. Report to exchange support if it persists

### Problem: "fetch_ohlcv timed out after 15.0s"

**Cause:** Bybit or Binance API was slow or overloaded.

**Solutions:**
1. Retry the fetch (they usually succeed on second attempt)
2. Try during off-peak hours (not UTC 08:00-10:00, peak trading time)
3. Use Binance directly: `loader = HistoricalDataLoader(use_exchange_manager=False)`

---

## Integration with IDSSBacktester

The loader is designed to work seamlessly with `IDSSBacktester`:

```python
from core.backtesting import HistoricalDataLoader, IDSSBacktester
from core.indicators.indicator_library import IndicatorLibrary

# Fetch real data
loader = HistoricalDataLoader()
df = loader.fetch_ohlcv(
    "BTC/USDT", "1h",
    "2024-01-01", "2024-03-31",
    min_bars=500
)

# Add required indicators
ind_lib = IndicatorLibrary()
df = ind_lib.add_all_indicators(df, timeframe="1h")

# Run IDSS backtest with real data
backtester = IDSSBacktester(min_confluence_score=0.45)
result = backtester.run(df, symbol="BTC/USDT", timeframe="1h")

print(f"Traded on real data from {loader.get_last_fetch_info().primary_source}")
```

---

## Testing

The loader includes 36 unit tests covering:
- Exception handling
- Data validation logic
- Fallback behavior
- Metadata recording
- Large datasets
- Edge cases

Run tests:
```bash
pytest tests/backtesting/test_data_loader.py -v
```

---

## Implementation Notes

### Thread Safety
The loader uses a threading lock (`_lock`) to protect singleton state and metadata recording. It is safe to call from multiple threads.

### Error Recovery
- If primary source (Bybit) fails, automatically falls back to Binance
- If both fail, raises a clear `InsufficientDataError` with details
- No partial data is returned (all-or-nothing guarantee)

### Exchange Manager Integration
If `use_exchange_manager=True`, the loader respects the active exchange setting:
- If Bybit Demo is active, fetches from Demo (paper trading data)
- If Bybit Live is active, fetches from Live
- Falls back to Binance if the primary source is unavailable

### Rate Limiting
- Respects exchange rate limits with 0.5s sleep between requests
- Bybit and Binance can handle ~2 requests per second; this implementation uses 0.5s = 2 req/s
- No risk of hitting rate limit bans

---

## Future Enhancements

Potential improvements (not currently implemented):
- Caching layer to avoid re-fetching same date ranges
- Multi-threaded concurrent fetching for multiple symbols
- Direct SQL queries for local historical data warehouses
- Support for other timeframes (e.g., "2h", "8h")
- Bid/ask spread data from order books (currently only OHLCV)

---

## License & Attribution

This module is part of **NexusTrader**, a professional quantitative trading system.

**Key Design Principles:**
1. Real data only — never synthetic or mocked
2. Fail-safe validation — all data is validated before use
3. Production-ready — error handling, timeouts, rate limiting
4. Transparent — metadata recording and logging
5. Reliable fallback — Bybit → Binance ensures availability
