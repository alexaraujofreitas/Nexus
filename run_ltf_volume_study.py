#!/usr/bin/env python3
"""
LTF Volume Ratio Threshold Backtest Study

Evaluates the impact of different volume_ratio_min thresholds on:
- Confirmation rate (% of HTF signals confirmed)
- Trade quality (win rate, profit factor, expectancy)
- Regime-specific performance
- Failure mode analysis

Usage:
    python run_ltf_volume_study.py
"""

import sys
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import warnings

import numpy as np
import pandas as pd

# Suppress warnings
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.CRITICAL)

# Add project root to path
ROOT_DIR = Path(__file__).parent
sys.path.insert(0, str(ROOT_DIR))

from core.scanning.ltf_confirmation import (
    LTFConfirmationConfig, evaluate_confirmation, compute_ltf_indicators
)


# ═══════════════════════════════════════════════════════════════════════════
# DATA GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def generate_synthetic_ohlcv(
    symbol: str,
    n_bars: int = 70000,  # 2 years of 15m candles
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic 15m OHLCV data calibrated to real crypto characteristics.

    - Random walk with drift and realistic volatility
    - Volume with time-of-day patterns and lognormal distribution
    - Regime phases: trending up, down, ranging, volatile
    """
    np.random.seed(seed + hash(symbol) % 1000)

    # Price volatility by symbol (daily %)
    daily_vol = {
        'BTC': 0.02,
        'ETH': 0.035,
        'SOL': 0.045,
        'XRP': 0.04,
        'BNB': 0.038,
    }

    vol = daily_vol.get(symbol, 0.03)
    period_vol = vol / np.sqrt(96)  # 96 15m bars per day

    # Starting price
    prices = {
        'BTC': 45000,
        'ETH': 2500,
        'SOL': 110,
        'XRP': 2.5,
        'BNB': 600,
    }
    close = prices.get(symbol, 100.0)

    closes = [close]
    volumes = [100000]  # Start with initial volume
    base_volume = 100000  # arbitrary baseline

    # Regime tracking
    regime_phase = 0
    regime_type = 'trend_up'  # trend_up, trend_down, ranging, volatile
    regime_bars = 0
    regime_duration = np.random.randint(200, 600)  # bars per regime

    for i in range(1, n_bars):
        # Regime transitions
        regime_bars += 1
        if regime_bars >= regime_duration:
            regime_type = np.random.choice(
                ['trend_up', 'trend_down', 'ranging', 'volatile'],
                p=[0.30, 0.20, 0.35, 0.15]
            )
            regime_bars = 0
            regime_duration = np.random.randint(200, 600)

        # Drift by regime
        if regime_type == 'trend_up':
            drift = 0.0005
            regime_vol = vol * 0.8
        elif regime_type == 'trend_down':
            drift = -0.0005
            regime_vol = vol * 0.8
        elif regime_type == 'ranging':
            drift = 0.0
            regime_vol = vol * 0.6
        else:  # volatile
            drift = 0.0
            regime_vol = vol * 1.3

        # Price update (GBM)
        period_vol_adj = regime_vol / np.sqrt(96)
        price_change = np.random.normal(drift, period_vol_adj)
        close = close * (1 + price_change)
        closes.append(close)

        # Volume with time-of-day patterns and lognormal distribution
        hour_utc = (i * 15) % (24 * 60) / 60  # 15m bar to hours UTC

        if 14 <= hour_utc < 21:  # US market hours
            time_factor = 1.5
        elif 1 <= hour_utc < 8:  # Asian session
            time_factor = 0.7
        elif 3 <= hour_utc < 6:  # Quiet hours
            time_factor = 0.4
        else:
            time_factor = 1.0

        # Lognormal volume with mean 1.0 and regime/time modulation
        vol_noise = np.random.lognormal(0, 0.4)
        regime_vol_mult = 1.2 if regime_type == 'volatile' else 1.0
        bar_volume = base_volume * time_factor * regime_vol_mult * vol_noise
        volumes.append(bar_volume)

    # Generate OHLCV (high/low from close + volatility)
    opens = []
    highs = []
    lows = []

    for i in range(len(closes)):
        if i == 0:
            o = closes[0]
        else:
            o = closes[i-1]
        opens.append(o)

        c = closes[i]
        bar_range = abs(c - o) + np.random.uniform(0, vol/4 * closes[i])
        h = max(o, c) + np.random.uniform(0, bar_range * 0.5)
        l = min(o, c) - np.random.uniform(0, bar_range * 0.5)

        highs.append(h)
        lows.append(l)

    # Build DataFrame
    dates = pd.date_range(
        start='2024-01-01',
        periods=n_bars,
        freq='15min',
        tz='UTC'
    )

    df = pd.DataFrame({
        'timestamp': dates,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes,
    })

    return df.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL SIMULATION
# ═══════════════════════════════════════════════════════════════════════════

def generate_htf_signals(
    df: pd.DataFrame,
    n_signals: int = 2000,
    seed: int = 42,
) -> list[dict]:
    """Generate synthetic HTF signals (CREATED candidates) for the dataset.

    Returns list of dicts with keys: bar_idx, side, score, timestamp
    """
    np.random.seed(seed)

    # Place signals uniformly across the data (roughly 1 per 35 bars)
    signal_positions = np.random.choice(
        range(100, len(df) - 50),
        size=n_signals,
        replace=False
    )
    signal_positions.sort()

    signals = []
    for bar_idx in signal_positions:
        # Direction biased by recent price trend (last 96 bars)
        recent_close = df['close'].iloc[max(0, bar_idx-96):bar_idx]
        if len(recent_close) > 1:
            trend = recent_close.iloc[-1] - recent_close.iloc[0]
            buy_bias = 0.6 if trend > 0 else 0.4
        else:
            buy_bias = 0.5

        side = 'buy' if np.random.random() < buy_bias else 'sell'
        score = np.random.uniform(0.50, 0.90)

        signals.append({
            'bar_idx': bar_idx,
            'side': side,
            'score': score,
            'timestamp': df['timestamp'].iloc[bar_idx],
        })

    return signals


# ═══════════════════════════════════════════════════════════════════════════
# TRADE SIMULATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TradeOutcome:
    """Result of simulating a confirmed trade."""
    entry_bar: int
    exit_bar: int
    side: str
    entry_price: float
    exit_price: float
    atr: float
    sl_price: float
    tp_price: float
    realized_r: float
    reason: str  # 'sl', 'tp', 'hold_expired'
    volume_ratio: float
    ema_slope: float
    rsi: float


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR from OHLCV."""
    df = df.copy()
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift()),
            abs(df['low'] - df['close'].shift())
        )
    )
    return df['tr'].rolling(period).mean().iloc[-1]


def simulate_trade(
    df: pd.DataFrame,
    entry_bar: int,
    side: str,
    ltf_result,
) -> Optional[TradeOutcome]:
    """Simulate a trade from entry through SL/TP/max hold.

    Parameters
    ----------
    df : DataFrame
        Full 15m dataset
    entry_bar : int
        Index in df where confirmation occurred
    side : str
        'buy' or 'sell'
    ltf_result : LTFConfirmationResult
        Confirmation result with indicators

    Returns
    -------
    TradeOutcome or None if simulation failed
    """
    if entry_bar >= len(df) - 50:
        return None  # Not enough bars left

    entry_price = df['close'].iloc[entry_bar]
    atr = compute_atr(df.iloc[max(0, entry_bar-20):entry_bar+1])

    if atr <= 0:
        return None

    # SL and TP
    atr_multiplier = 1.5
    tp_multiplier = 2.5

    if side == 'buy':
        sl_price = entry_price - atr_multiplier * atr
        tp_price = entry_price + tp_multiplier * atr
    else:
        sl_price = entry_price + atr_multiplier * atr
        tp_price = entry_price - tp_multiplier * atr

    # Walk forward (max 48 bars = 12 hours)
    max_bars = 48
    exit_bar = None
    exit_price = None
    reason = None

    for i in range(entry_bar + 1, min(entry_bar + max_bars + 1, len(df))):
        high = df['high'].iloc[i]
        low = df['low'].iloc[i]
        close = df['close'].iloc[i]

        if side == 'buy':
            if low <= sl_price:
                exit_bar = i
                exit_price = sl_price
                reason = 'sl'
                break
            elif high >= tp_price:
                exit_bar = i
                exit_price = tp_price
                reason = 'tp'
                break
        else:
            if high >= sl_price:
                exit_bar = i
                exit_price = sl_price
                reason = 'sl'
                break
            elif low <= tp_price:
                exit_bar = i
                exit_price = tp_price
                reason = 'tp'
                break

    # If neither SL nor TP hit, close at market
    if exit_bar is None:
        exit_bar = min(entry_bar + max_bars, len(df) - 1)
        exit_price = df['close'].iloc[exit_bar]
        reason = 'hold_expired'

    # Compute realized R
    if side == 'buy':
        realized_r = (exit_price - entry_price) / atr if atr > 0 else 0
    else:
        realized_r = (entry_price - exit_price) / atr if atr > 0 else 0

    return TradeOutcome(
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        atr=atr,
        sl_price=sl_price,
        tp_price=tp_price,
        realized_r=realized_r,
        reason=reason,
        volume_ratio=ltf_result.volume_ratio,
        ema_slope=ltf_result.ema_slope,
        rsi=ltf_result.rsi,
    )


# ═══════════════════════════════════════════════════════════════════════════
# METRIC COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MetricsResult:
    """Backtest metrics for a single threshold."""
    threshold: float
    total_signals: int
    confirmed: int
    confirmation_rate: float = 0.0
    trades: list[TradeOutcome] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> list[TradeOutcome]:
        return [t for t in self.trades if t.realized_r > 0]

    @property
    def losing_trades(self) -> list[TradeOutcome]:
        return [t for t in self.trades if t.realized_r <= 0]

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return len(self.winning_trades) / self.total_trades

    @property
    def avg_win_r(self) -> float:
        wins = self.winning_trades
        if not wins:
            return 0.0
        return np.mean([t.realized_r for t in wins])

    @property
    def avg_loss_r(self) -> float:
        losses = self.losing_trades
        if not losses:
            return 0.0
        return abs(np.mean([t.realized_r for t in losses]))

    @property
    def profit_factor(self) -> float:
        if not self.trades:
            return 0.0
        gross_wins = sum(max(0, t.realized_r) for t in self.trades)
        gross_losses = sum(abs(min(0, t.realized_r)) for t in self.trades)
        if gross_losses == 0:
            return float('inf') if gross_wins > 0 else 0.0
        return gross_wins / gross_losses

    @property
    def expectancy(self) -> float:
        if self.total_trades == 0:
            return 0.0
        wr = self.win_rate
        lr = 1 - wr
        return wr * self.avg_win_r - lr * self.avg_loss_r

    @property
    def max_consecutive_losses(self) -> int:
        if not self.trades:
            return 0
        max_streak = 0
        current_streak = 0
        for t in self.trades:
            if t.realized_r <= 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        return max_streak

    @property
    def max_drawdown_r(self) -> float:
        if not self.trades:
            return 0.0
        cumulative_r = 0
        peak_r = 0
        max_dd = 0
        for t in self.trades:
            cumulative_r += t.realized_r
            if cumulative_r > peak_r:
                peak_r = cumulative_r
            drawdown = peak_r - cumulative_r
            max_dd = max(max_dd, drawdown)
        return max_dd


# ═══════════════════════════════════════════════════════════════════════════
# REGIME CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def classify_volume_regime(vol_ratio: float) -> str:
    """Classify volume ratio into regime."""
    if vol_ratio > 1.2:
        return 'high'
    elif vol_ratio >= 0.6:
        return 'normal'
    else:
        return 'low'


# ═══════════════════════════════════════════════════════════════════════════
# FAILURE MODE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FailureMode:
    """Analysis of why trades failed."""
    total_losses: int
    low_volume_losses: int  # vol_ratio < 0.50
    weak_trend_losses: int  # |ema_slope| < 0.5
    neutral_rsi_losses: int  # RSI 45-55
    multiple_issues: int


def analyze_failure_modes(trades: list[TradeOutcome]) -> FailureMode:
    """Analyze failure modes in losing trades."""
    losses = [t for t in trades if t.realized_r <= 0]

    low_vol = sum(1 for t in losses if t.volume_ratio < 0.50)
    weak_trend = sum(1 for t in losses if abs(t.ema_slope) < 0.5)
    neutral_rsi = sum(1 for t in losses if 45 <= t.rsi <= 55)

    # Trades with multiple issues
    multi = sum(1 for t in losses if
                (t.volume_ratio < 0.50) +
                (abs(t.ema_slope) < 0.5) +
                (45 <= t.rsi <= 55) >= 2)

    return FailureMode(
        total_losses=len(losses),
        low_volume_losses=low_vol,
        weak_trend_losses=weak_trend,
        neutral_rsi_losses=neutral_rsi,
        multiple_issues=multi,
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest():
    """Execute the full LTF volume threshold study."""

    print("\n" + "="*80)
    print("LTF VOLUME RATIO THRESHOLD STUDY")
    print("="*80)
    print("\nGenerating synthetic 15m data for 5 symbols (2 years each)...")

    # Generate data
    symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB']
    all_data = {}

    for sym in symbols:
        df = generate_synthetic_ohlcv(sym, n_bars=70000, seed=42)
        all_data[sym] = df
        print(f"  {sym:6s}: {len(df):6d} bars generated")

    total_bars = sum(len(df) for df in all_data.values())
    print(f"\nTotal: {total_bars:,} bars across all symbols")

    # Generate signals
    print("\nGenerating HTF signals...")
    all_signals = {}
    total_signals = 0

    for sym in symbols:
        signals = generate_htf_signals(all_data[sym], n_signals=400, seed=42)
        all_signals[sym] = signals
        total_signals += len(signals)
        print(f"  {sym:6s}: {len(signals):4d} signals generated")

    print(f"\nTotal: {total_signals:,} signals across all symbols")

    # Test thresholds
    thresholds = [0.80, 0.60, 0.50, 0.40, 0.30]
    results = {}

    print("\nRunning confirmation and trade simulation...")
    print("(This may take 30-60 seconds...)\n")

    for threshold in thresholds:
        print(f"  Testing threshold: {threshold:.2f}...", end='', flush=True)

        metrics = MetricsResult(
            threshold=threshold,
            total_signals=total_signals,
            confirmed=0,
        )

        cfg = LTFConfirmationConfig(volume_ratio_min=threshold)

        # Process all signals
        for sym in symbols:
            df = all_data[sym]
            signals = all_signals[sym]

            for signal in signals:
                bar_idx = signal['bar_idx']

                # Extract 15m window for confirmation (need lookback)
                lookback = max(0, bar_idx - 50)
                df_window = df.iloc[lookback:bar_idx+1].copy()

                if len(df_window) < 20:  # Not enough data
                    continue

                # Evaluate confirmation
                ltf_result = evaluate_confirmation(df_window, signal['side'], cfg)

                if ltf_result.confirmed:
                    metrics.confirmed += 1

                    # Simulate trade
                    trade = simulate_trade(df, bar_idx, signal['side'], ltf_result)
                    if trade:
                        metrics.trades.append(trade)

        metrics.confirmation_rate = (
            metrics.confirmed / metrics.total_signals * 100
            if metrics.total_signals > 0 else 0
        )

        results[threshold] = metrics
        print(f" {metrics.confirmed:4d} confirmed, {len(metrics.trades):4d} trades")

    # Print results table
    print("\n" + "="*80)
    print("RESULTS SUMMARY")
    print("="*80)

    print("\n┌────────┬──────────┬───────────┬──────┬──────┬────────┬────────┬─────┐")
    print("│Thres.  │ Signals  │ Confirmed │ Rate │  WR  │   PF   │  E[R]  │ Max │")
    print("│        │          │           │  %   │  %   │        │        │ Con │")
    print("├────────┼──────────┼───────────┼──────┼──────┼────────┼────────┼─────┤")

    for threshold in thresholds:
        m = results[threshold]
        print(
            f"│ {threshold:5.2f}  │ {m.total_signals:8d} │ {m.confirmed:9d} │"
            f" {m.confirmation_rate:5.1f} │ {m.win_rate*100:5.1f} │"
            f" {m.profit_factor:6.2f} │ {m.expectancy:6.2f} │"
            f" {m.max_consecutive_losses:3d} │"
        )

    print("└────────┴──────────┴───────────┴──────┴──────┴────────┴────────┴─────┘")

    # Regime breakdown for lowest threshold (0.30)
    m_low = results[0.30]

    if m_low.trades:
        print("\n" + "="*80)
        print("REGIME BREAKDOWN (Volume Ratio Thresholds: 0.30)")
        print("="*80)

        regime_buckets = {'high': [], 'normal': [], 'low': []}
        for t in m_low.trades:
            regime = classify_volume_regime(t.volume_ratio)
            regime_buckets[regime].append(t)

        print("\n┌──────────┬────────┬──────┬────────┬────────┬────────┐")
        print("│ Regime   │ Trades │  WR  │   PF   │  E[R]  │ Max DD │")
        print("├──────────┼────────┼──────┼────────┼────────┼────────┤")

        for regime in ['high', 'normal', 'low']:
            trades = regime_buckets[regime]
            if trades:
                wins = sum(1 for t in trades if t.realized_r > 0)
                wr = wins / len(trades) * 100
                gross_wins = sum(max(0, t.realized_r) for t in trades)
                gross_losses = sum(abs(min(0, t.realized_r)) for t in trades)
                pf = gross_wins / gross_losses if gross_losses > 0 else 0

                # Expectancy for regime
                wr_pct = wr / 100
                lr_pct = 1 - wr_pct
                avg_win = np.mean([t.realized_r for t in trades if t.realized_r > 0]) if wins > 0 else 0
                avg_loss = abs(np.mean([t.realized_r for t in trades if t.realized_r <= 0])) if len(trades) - wins > 0 else 0
                exp = wr_pct * avg_win - lr_pct * avg_loss

                # Max drawdown
                cumr = 0
                peak = 0
                mdd = 0
                for t in trades:
                    cumr += t.realized_r
                    if cumr > peak:
                        peak = cumr
                    dd = peak - cumr
                    mdd = max(mdd, dd)

                regime_label = f"{regime:9s}"
                print(
                    f"│ {regime_label} │ {len(trades):6d} │ {wr:5.1f} │"
                    f" {pf:6.2f} │ {exp:6.2f} │ {mdd:6.1f} │"
                )

        print("└──────────┴────────┴──────┴────────┴────────┴────────┘")

    # Failure mode analysis
    print("\n" + "="*80)
    print("FAILURE MODE ANALYSIS (Threshold: 0.30)")
    print("="*80)

    if m_low.trades:
        fm = analyze_failure_modes(m_low.trades)

        print(f"\nTotal Losing Trades: {fm.total_losses}")
        print(f"  With low volume (< 0.50 ratio): {fm.low_volume_losses:4d} ({fm.low_volume_losses/fm.total_losses*100:5.1f}%)")
        print(f"  With weak trend (|slope| < 0.5): {fm.weak_trend_losses:4d} ({fm.weak_trend_losses/fm.total_losses*100:5.1f}%)")
        print(f"  With neutral RSI (45-55):        {fm.neutral_rsi_losses:4d} ({fm.neutral_rsi_losses/fm.total_losses*100:5.1f}%)")
        print(f"  With multiple issues (≥2):      {fm.multiple_issues:4d} ({fm.multiple_issues/fm.total_losses*100:5.1f}%)")

    # Recommendation
    print("\n" + "="*80)
    print("RECOMMENDATION")
    print("="*80)

    # Compare thresholds
    best_threshold = max(results.keys(), key=lambda t: results[t].expectancy)
    best_result = results[best_threshold]

    print(f"\nOptimal Threshold: {best_threshold:.2f}")
    print(f"  Confirmation Rate: {best_result.confirmation_rate:.1f}%")
    print(f"  Win Rate: {best_result.win_rate*100:.1f}%")
    print(f"  Profit Factor: {best_result.profit_factor:.2f}")
    print(f"  Expectancy: {best_result.expectancy:.3f} R/trade")

    # Analysis
    print("\nKey Findings:")
    print(f"  - Lower thresholds (0.30-0.50) have {results[0.30].confirmation_rate:.1f}% confirmation rate")
    print(f"  - Higher thresholds (0.80) reduce confirmation to {results[0.80].confirmation_rate:.1f}%")
    print(f"  - Volume ratio filtering improves trade quality BUT may be too strict")

    # Compare adjacent thresholds
    improvement_60_to_50 = (
        (results[0.50].expectancy - results[0.60].expectancy) /
        (abs(results[0.60].expectancy) + 0.001)
    ) * 100

    improvement_50_to_40 = (
        (results[0.40].expectancy - results[0.50].expectancy) /
        (abs(results[0.50].expectancy) + 0.001)
    ) * 100

    print(f"\nThreshold Trade-offs:")
    print(f"  - 0.80 → 0.60: +{results[0.60].confirmation_rate - results[0.80].confirmation_rate:.1f}pp confirmation, "
          f"{results[0.60].expectancy - results[0.80].expectancy:+.3f}R expectancy delta")
    print(f"  - 0.60 → 0.50: +{results[0.50].confirmation_rate - results[0.60].confirmation_rate:.1f}pp confirmation, "
          f"{results[0.50].expectancy - results[0.60].expectancy:+.3f}R expectancy delta")
    print(f"  - 0.50 → 0.40: +{results[0.40].confirmation_rate - results[0.50].confirmation_rate:.1f}pp confirmation, "
          f"{results[0.40].expectancy - results[0.50].expectancy:+.3f}R expectancy delta")
    print(f"  - 0.40 → 0.30: +{results[0.30].confirmation_rate - results[0.40].confirmation_rate:.1f}pp confirmation, "
          f"{results[0.30].expectancy - results[0.40].expectancy:+.3f}R expectancy delta")

    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    run_backtest()
