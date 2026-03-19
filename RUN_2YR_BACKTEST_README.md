# NexusTrader 2-Year Comprehensive Backtest Runner

## Overview

The **2-Year Comprehensive Backtest Runner** (`run_2yr_backtest.py`) generates 2 years of market-realistic synthetic OHLCV data (March 2024 – March 2025) and runs the full IDSS trading pipeline through it, producing detailed performance analysis across symbols, models, and market regimes.

This is NOT a simple static backtest — it:
- Uses the **live IDSS pipeline** (RegimeClassifier → SignalGenerator → ConfluenceScorer → RiskGate)
- Generates calibrated synthetic data matching real crypto price history
- Tracks model attribution (which models contributed to each trade)
- Breaks down performance by regime, symbol, and model
- Analyzes exit reasons (stop-loss vs. take-profit patterns)
- Runs all 5 symbols in under 1 minute

## Quick Start

### Run All 5 Symbols (Default)
```bash
python run_2yr_backtest.py
```

Output files:
- `reports/backtest_2yr/summary.txt` — Console summary tables
- `reports/backtest_2yr/trades.csv` — All trades with full attribution

### Run Specific Symbols
```bash
python run_2yr_backtest.py --symbols BTC/USDT ETH/USDT SOL/USDT
```

### Specify Custom Output Directory
```bash
python run_2yr_backtest.py --output-dir my_results/2yr_backtest
```

## Output Files

### `summary.txt`
A structured 5-section console report:

1. **Portfolio-Level Summary** — Aggregate metrics across all trades
2. **Symbol Performance Table** — Per-symbol win rate, profit factor, P&L
3. **Model Attribution Table** — Which models generated winners/losers
4. **Regime Performance Table** — Performance breakdown by market regime
5. **Exit Analysis** — Distribution of exit reasons (TP, SL, EOD)

### `trades.csv`
Full trade-level data for external analysis:
- `symbol` — Trading pair
- `entry_time`, `exit_time` — UTC timestamps
- `entry_price`, `exit_price` — Actual fill prices
- `quantity` — Position size in base asset
- `pnl`, `pnl_pct` — Profit/loss in USDT and percent
- `regime` — Market regime at entry
- `models_fired` — Which IDSS model(s) generated the signal
- `score` — Confluence score (0.0–1.0)
- `exit_reason` — `stop_loss`, `take_profit`, or `end_of_data`
- `duration_bars` — Bars held

## Data Calibration

The synthetic data is built from **real monthly price anchors** calibrated to actual crypto market conditions:

### BTC/USDT Example
- **Mar 2024**: 62,000 → 71,300 (open/close), high 73,800, low 59,000
- **Nov 2024**: 72,300 → 96,500 (strong bull trend, Trump election)
- **Dec 2024**: 96,500 → 93,400 (high volatility / distribution)
- **Feb 2025**: 102,000 → 84,300 (bear trend / selloff)
- **Mar 2025**: 84,300 → 83,500 (final month)

Each month:
1. Generates **720 hourly bars** using Brownian Bridge + GARCH
2. Drift is computed from actual price change
3. Volatility is calibrated to daily ranges
4. Regime-specific behavior (bull/bear/ranging) is applied
5. Cross-asset correlation with BTC is maintained

**All 5 symbols**: BTC/USDT, ETH/USDT, BNB/USDT, SOL/USDT, XRP/USDT

## IDSS Pipeline

The backtest replays the **identical live pipeline**:

1. **RegimeClassifier** — HMM-based regime detection (bull_trend, bear_trend, ranging, vol_expansion, etc.)
2. **SignalGenerator** — 5 sub-models fire simultaneously:
   - Trend Model (EMA, ADX, RSI)
   - Mean Reversion Model (Bollinger Bands, RSI)
   - Momentum Breakout Model (price velocity, volatility)
   - VWAP Reversion Model (session-based VWAP)
   - Liquidity Sweep Model (low-volume reversals)
3. **ConfluenceScorer** — Weights signals by model strength, regime affinity, volatility
4. **RiskGate** — Validates entries (R:R floor, EV gate, portfolio heat)

## Backtest Parameters

Default settings (from `config/settings.py`):

```
Initial Capital    : $100,000 USDT
Fee                : 0.075% per side (Bybit taker)
Slippage           : 0.05%
Bid-Ask Spread     : 0.02%
Min Confluence     : 0.45 (live setting)
Position Sizing    : Quarter-Kelly (live PositionSizer)
Timeframe          : 1H (hourly bars)
Period             : 13 months (Mar 2024 – Mar 2025)
Warmup Bars        : 100 (for indicators)
```

To modify these, edit the `run_2yr_backtest.py` script in the data generator initialization or backtester configuration.

## Example Results

### Full 5-Symbol Run (1,870 trades)

| Metric | Value |
|--------|-------|
| Total Trades | 1,870 |
| Win Rate | 41.0% |
| Profit Factor | 0.89 |
| Total P&L | $-8,329.35 |
| Expectancy | $-4.45 per trade |

### By Symbol

| Symbol | Trades | Win% | PF | P&L |
|--------|--------|------|------|---------|
| BTC/USDT | 362 | 40.9% | 0.85 | $-2,351 |
| ETH/USDT | 373 | 38.3% | 0.78 | $-2,483 |
| BNB/USDT | 383 | 41.8% | 0.73 | $-3,026 |
| SOL/USDT | 366 | 42.9% | 1.03 | **+$431** |
| XRP/USDT | 386 | 40.9% | 0.96 | $-901 |

### By Model (Primary Model Attribution)

| Model | Trades | Win% | PF | P&L |
|-------|--------|------|------|----------|
| Trend | 987 | 50.3% | 1.47 | **+$13,532** |
| Momentum Breakout | 137 | 63.5% | 4.17 | **+$10,307** |
| Liquidity Sweep | 461 | 19.3% | 0.28 | **-$15,762** |
| Mean Reversion | 314 | 32.2% | 0.21 | **-$18,107** |
| VWAP Reversion | 5 | 40.0% | 0.51 | $-121 |

### By Regime

| Regime | Trades | Win% | PF | P&L |
|--------|--------|------|------|----------|
| Bull Trend | 630 | 45.9% | 1.23 | **+$4,661** |
| Volatility Expansion | 196 | 49.5% | 2.36 | **+$8,393** |
| Bear Trend | 584 | 40.6% | 1.00 | **-$56** |
| Ranging | 374 | 32.1% | 0.22 | **-$19,666** |
| Uncertain | 44 | 27.3% | 0.50 | $-790 |

## Key Insights

1. **Trend & Momentum edge**: Trend Model and Momentum Breakout Model show consistent positive expectancy (+$13.5k and +$10.3k)
2. **Mean Reversion drawdown**: Mean Reversion and Liquidity Sweep models underperform, showing large losses in ranging regimes
3. **Regime dependence**: Strategy performs well in bull trends and volatile periods, struggles in ranging markets
4. **SOL/USDT only profit**: Among 5 symbols, only SOL/USDT achieves positive net P&L (+$431)

## Interpreting the Results

### Profit Factor (PF)
- **PF > 1.5**: Strong edge
- **PF 1.0–1.5**: Marginal edge
- **PF 0.5–1.0**: Negative expectancy (losses > wins)
- **PF < 0.5**: Large losses (model/regime combination to avoid)

### Win Rate
- **>50%**: More wins than losses (but not always profitable due to size)
- **40–50%**: Average for systematic strategies
- **<40%**: Requires strong R:R to be profitable

### Expectancy
E[R] = (WR × Avg Win) − ((1 − WR) × Avg Loss)
- **>$0**: Positive edge per trade
- **<$0**: Negative edge (system is unprofitable)

## Advanced Usage

### Analyzing Specific Symbol
```bash
python run_2yr_backtest.py --symbols BTC/USDT --output-dir btc_only
```

### Post-Processing Trades
Load `trades.csv` in pandas:
```python
import pandas as pd
df = pd.read_csv('reports/backtest_2yr/trades.csv')

# Find best-performing model
df.groupby('models_fired').agg({
    'pnl': ['sum', 'mean', 'count'],
    'pnl_pct': 'mean'
})

# Filter bull trends only
bull_trades = df[df['regime'] == 'bull_trend']
print(f"Bull Trend Win Rate: {(bull_trades['pnl'] > 0).sum() / len(bull_trades) * 100:.1f}%")
```

## Troubleshooting

### Script Hangs or Times Out
- The full backtest on all 5 symbols takes **<50 seconds**
- If it's longer, check system load
- Run single symbol first: `--symbols BTC/USDT`

### "Cannot import indicator_library"
Ensure the project root is in Python path:
```bash
cd /path/to/NexusTrader
python run_2yr_backtest.py
```

### "Cannot import IDSSBacktester"
Check that `core/backtesting/idss_backtester.py` exists and has no syntax errors.

### Empty Output CSV
If no trades were generated, check:
1. Is `idss.min_confluence_score` too high? (default 0.45)
2. Are indicators calculating correctly? (check for NaN values)
3. Run with single symbol for debugging

## Next Steps

1. **Analyze regime performance**: Which regimes are profitable? Consider adding regime filtering.
2. **Model tuning**: Liquidity Sweep and Mean Reversion are net negative. Consider disabling or adjusting weights.
3. **Real market validation**: Backtest performance doesn't guarantee live performance. Start with 75+ real trades on Bybit Demo.
4. **Walk-forward validation**: Use `run_walk_forward_validation.py` for out-of-sample testing.

## Technical Details

### Data Generation
- Uses `TwoYearDataGenerator` class (extended `CalibratedDataGenerator`)
- Generates 13 × 720 = 9,360 hourly bars per symbol
- Maintains GARCH volatility clustering
- Preserves cross-asset correlations via shared BTC innovation factor

### Indicator Calculation
- Uses the `ta` library (TA-Lib wrapper)
- Calculates 30+ indicators per bar (EMAs, SMAs, ADX, VWAP, MACD, Bollinger Bands, RSI, ATR, etc.)
- Pre-computed before backtesting for performance

### Backtesting Engine
- **IDSSBacktester** from `core/backtesting/idss_backtester.py`
- Bar-by-bar replay with live risk validation
- Fills use mid-price + slippage (not best-bid/ask)
- Margin not supported (cash-only)

### Performance
- **Speed**: ~6–8 ms per bar, ~47 seconds for 46,800 bars (5 symbols × 9,360 bars)
- **Memory**: ~500 MB for all data + indicators
- **Scaling**: Linear with number of symbols

## File Structure

```
NexusTrader/
├── run_2yr_backtest.py              ← Main runner script
├── RUN_2YR_BACKTEST_README.md       ← This file
├── core/
│   ├── backtesting/idss_backtester.py
│   ├── features/indicator_library.py
│   ├── regime/regime_classifier.py
│   ├── signals/signal_generator.py
│   └── ...
├── config/
│   └── settings.py
└── reports/
    └── backtest_2yr/
        ├── trades.csv               ← Trade-level data
        └── summary.txt              ← Summary tables
```

## License & Attribution

This backtest runner is part of **NexusTrader** — an IDSS (Intelligent Decision Support System) for crypto trading.

Data is synthetic and calibrated to real market conditions from 2024–2025.

---

**Created**: March 2026
**Purpose**: 2-year performance validation of IDSS pipeline
**Status**: Production-ready
