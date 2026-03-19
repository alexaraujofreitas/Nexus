# HistoricalDataLoader — Quick Start Guide

## Installation & Verification

```bash
# Verify it's installed
python3 -c "from core.backtesting import HistoricalDataLoader; print('✅ Ready')"

# Run tests (should show 36/36 passing)
pytest tests/backtesting/test_data_loader.py -v
```

---

## 5-Minute Tutorial

### Example 1: Fetch and Inspect Data

```python
from core.backtesting import HistoricalDataLoader

# Create loader
loader = HistoricalDataLoader()

# Fetch BTC hourly data for January 2024
df = loader.fetch_ohlcv(
    symbol="BTC/USDT",
    timeframe="1h",
    start_date="2024-01-01",
    end_date="2024-01-31",
    min_bars=500  # Require at least 500 bars
)

# Inspect the data
print(f"Shape: {df.shape}")  # (rows, columns)
print(df.head())
print(df.tail())

# Check data source metadata
info = loader.get_last_fetch_info()
print(f"Source: {info.primary_source}")
print(f"Bars: {info.total_bars}")
print(f"Fetch time: {info.fetch_duration_s:.2f}s")
```

**Output:**
```
Shape: (744, 5)
                            open      high       low     close  volume
timestamp
2024-01-01 00:00:00+00:00  42166.9  42285.0  42046.0  42146.9   1234.5
2024-01-01 01:00:00+00:00  42147.0  42400.0  42100.0  42350.0   1456.2
...

Source: bybit
Bars: 744
Fetch time: 1.23s
```

---

### Example 2: Use with Backtester

```python
from core.backtesting import HistoricalDataLoader, IDSSBacktester
from core.indicators.indicator_library import IndicatorLibrary

# Fetch data
loader = HistoricalDataLoader()
df = loader.fetch_ohlcv("ETH/USDT", "4h", "2024-01-01", "2024-03-01")

# Add indicators (required for IDSS)
ind_lib = IndicatorLibrary()
df = ind_lib.add_all_indicators(df, timeframe="4h")

# Backtest
backtester = IDSSBacktester(min_confluence_score=0.45)
result = backtester.run(df, symbol="ETH/USDT", timeframe="4h")

# Results
print(f"Total trades: {result['num_trades']}")
print(f"Win rate: {result['win_rate']:.1%}")
print(f"Profit factor: {result['profit_factor']:.2f}")
```

---

### Example 3: Batch Fetch Multiple Symbols

```python
from core.backtesting import HistoricalDataLoader, InsufficientDataError

loader = HistoricalDataLoader()
symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
data = {}

for symbol in symbols:
    try:
        df = loader.fetch_ohlcv(
            symbol, "1d", "2024-01-01", "2024-12-31", min_bars=365
        )
        data[symbol] = df
        print(f"✅ {symbol}: {len(df)} bars")
    except InsufficientDataError as e:
        print(f"⚠️  {symbol}: {e.bars_available} bars (needed {e.bars_needed})")

print(f"\nLoaded {len(data)}/{len(symbols)} symbols")
```

---

## Common Tasks

### Task 1: Check Available Data Range

```python
loader = HistoricalDataLoader()
start, end = loader.get_available_range("BTC/USDT", "1d")
print(f"Available: {start.date()} to {end.date()}")
```

### Task 2: Inspect Data Quality

```python
loader = HistoricalDataLoader()
df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-01-31")
info = loader.get_last_fetch_info()

print(f"Total bars: {info.total_bars}")
print(f"Gaps found: {info.gaps_found}")
print(f"Duplicates: {info.duplicates_removed}")
print(f"Data quality: {'✅ Good' if info.gaps_found == 0 else '⚠️  Check gaps'}")
```

### Task 3: Handle Errors Gracefully

```python
from core.backtesting import InsufficientDataError

loader = HistoricalDataLoader()

try:
    df = loader.fetch_ohlcv(
        "LOWVOLUME/USDT", "5m", "2020-01-01", "2024-01-01", min_bars=10000
    )
except InsufficientDataError as e:
    print(f"Need: {e.bars_needed} bars")
    print(f"Got: {e.bars_available} bars")
    print(f"From: {e.source}")
    # Fallback: use less strict requirements
    df = loader.fetch_ohlcv(
        "LOWVOLUME/USDT", "5m", "2024-01-01", "2024-01-31", min_bars=100
    )
```

### Task 4: Get DataFrame Ready for Analysis

```python
loader = HistoricalDataLoader()

# Fetch
df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")

# DataFrame is already clean:
# - Index is UTC timezone datetime
# - No gaps, duplicates, or NaN values
# - All OHLCV values are positive floats
# - Sorted chronologically

# Add your own indicators
import pandas as pd
df['sma_20'] = df['close'].rolling(20).mean()
df['rsi'] = ...  # your RSI calculation

# Ready for analysis/backtesting
print(df.info())
```

---

## Supported Timeframes

```
1m   = 1 minute
5m   = 5 minutes
15m  = 15 minutes
30m  = 30 minutes
1h   = 1 hour
4h   = 4 hours
1d   = 1 day
```

---

## Troubleshooting

### Error: "Insufficient data for BTC/USDT/1h: needed 300, got 0"
- Check internet connection
- Verify symbol exists: `exchange_manager.get_symbols()`
- Try a different date range (may be outside available data)
- Check exchange status: https://status.bybit.com

### Error: "Found gaps larger than Xms"
- This is expected for 5m+ timeframes on weekends
- Use larger `min_bars` to compensate
- Check if date range includes non-trading hours (weekends, holidays)

### Error: "fetch_ohlcv timed out after 15.0s"
- Bybit API was slow; try again (usually succeeds on retry)
- Use different timeframe (less data per request)
- Try during off-peak hours (not UTC 08:00-10:00)

---

## API Cheat Sheet

```python
# Create loader
loader = HistoricalDataLoader()
loader = HistoricalDataLoader(use_exchange_manager=False)  # Force direct

# Fetch data
df = loader.fetch_ohlcv(symbol, timeframe, start_date, end_date, min_bars=300)
df = loader.fetch_ohlcv("BTC/USDT", "1h", "2024-01-01", "2024-12-31")

# Get range
start, end = loader.get_available_range(symbol, timeframe)

# Get metadata
info = loader.get_last_fetch_info()
print(info.primary_source)      # "bybit" or "binance"
print(info.fallback_used)       # True/False
print(info.total_bars)          # int
print(info.fetch_duration_s)    # float

# Get singleton
from core.backtesting import get_historical_data_loader
loader = get_historical_data_loader()

# Exception handling
from core.backtesting import InsufficientDataError
try:
    df = loader.fetch_ohlcv(...)
except InsufficientDataError as e:
    print(e.bars_needed, e.bars_available, e.source)
```

---

## Next Steps

1. **Read Full Documentation:** `docs/HISTORICAL_DATA_LOADER.md`
2. **Review Implementation:** `core/backtesting/data_loader.py`
3. **Study Tests:** `tests/backtesting/test_data_loader.py`
4. **Explore Examples:** Check the docstrings in the source code

---

## Support

- **Questions?** See `docs/HISTORICAL_DATA_LOADER.md` § Troubleshooting
- **API details?** Check docstrings in `core/backtesting/data_loader.py`
- **Examples?** Review `tests/backtesting/test_data_loader.py`
- **Status?** Run: `pytest tests/backtesting/test_data_loader.py -v`
